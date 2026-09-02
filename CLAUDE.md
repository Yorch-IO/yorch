# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Three things, and one of them is **not** in this checkout:

- **`docaget/`** — a working, measured document-indexing engine (Python package
  `docagent`). Extracts, chunks with byte-exact spans, LLM-corrects, embeds via
  Vertex AI, indexes into Qdrant with hybrid dense+BM25/RRF retrieval, learns
  per-document-family profiles, and measures retrieval quality against a
  synthetic eval set. Driven by a CLI, orchestrated by LangGraph. It has been
  used on a real ~75-document Spanish theology corpus.
- **`../yorch-tauri-backend`** — a **second HTTP control plane**, in its own
  checkout beside this one. NestJS, multi-tenant, Cognito-authenticated: the
  *paid* product. The FastAPI plane in `worker/` is the *free, self-managed*
  one, and it is not going away. Both serve the same 26 paths with the same
  payloads, both read the same Postgres, Memgraph, Qdrant and Temporal worker,
  and the desktop app picks one with a backend-mode setting. That repository has
  its own `CLAUDE.md`; the entries below are the parts that constrain work
  *here*.
- **`app/` + `worker/` + `infra/`** — **Company Brain**, a Tauri v2 desktop app
  being built *around* that engine: a durable Temporal pipeline with an approval
  gate before anything is paid for, a Postgres catalog for provenance and cost,
  and a UI that makes the pre-index workflow inspectable.

The directory is spelled **`docaget`**; the Python package and CLI inside it are
spelled **`docagent`**. This is a typo in the directory name and it is
load-bearing — every path must use `docaget/`.

Design plan: `~/.claude/plans/write-a-plan-to-shiny-eclipse.md`.
Current build status, verified commands and known blockers: `doc/COMPANY_BRAIN.md`.

## Commands

Prerequisites live in `~/.local/bin` (`uv`) and `~/.cargo/bin` (Rust); add both
to `PATH`. No linter or formatter is configured in any sub-project — the gates
are the test suites and `tsc`.

`cargo` additionally needs `PKG_CONFIG_PATH` pointed at the local Tauri sysroot
(`~/.local/tauri-sysroot/prefix/usr/lib/x86_64-linux-gnu/pkgconfig`) or the build
fails at `javascriptcoregtk-4.1`. `--release` used to be the second requirement,
because the debug profile's `staticlib` output had filled this disk; it is not
any more, and reaching for it in the dev loop is now the expensive mistake — see
*The dev profile is tuned; the release profile is deliberately not* below.

```bash
# Engine
cd docaget && uv sync && uv run pytest -q          # 194 passed, 15 skipped
uv run pytest tests/test_invariants.py::test_inv01_char_span_is_byte_exact -q
uv run docagent index --dry-run libro.pdf          # free structural preview
uv run docagent index libro.pdf                    # spends money
uv run docagent query "pregunta" | profiles | diag

# Worker (Temporal workflows + control API)
cd worker && uv sync && uv run pytest -q           # 550 passed, 78 skipped
# With the stack up the graph/ and catalog/ integration tests run instead of
# skipping; that figure was last taken as 528 passed, 12 skipped, before the 34
# audit tests were added. The Bolt port must come from `docker`, not
# `infra/.env`: BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789. Do **not** point that
# run at a disposable BRAIN_QDRANT_COLLECTION to be safe — tests/answering read
# the real index and 12 of them fail against an empty collection.
# graph/ and catalog/ are integration tests: they skip, naming the URL they
# tried, when Memgraph or Postgres is down, and neither fixture wipes anything.
uv run pytest tests/unit/test_artifacts.py -k rewritten -q

# Audit one indexed version, read-only. The three stores as the app publishes
# them; `infra/.env` names the ports and the password but no DATABASE_URL,
# because the containers build theirs from compose — and 5432 is a *different*
# Postgres. `--measure` re-runs the recorded recall measurement.
BRAIN_WORKSPACE_DIR=~/.local/share/io.sek.companybrain/workspace \
BRAIN_QDRANT_URL=http://127.0.0.1:6433 \
BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789 \
BRAIN_DATABASE_URL="postgresql://brain:$BRAIN_PG_PASSWORD@127.0.0.1:5532/brain" \
uv run python scripts/audit_version.py ver_… [--measure] [--json out.json]

# Desktop app
cd app && npm install
npm run typecheck && npx vitest run && npm run build   # 348 passed
npx vitest run -t "define no key"                  # single test by name
COMPANY_BRAIN_REPO_ROOT=/home/jjimenez/yorch npm run tauri dev

# Rust — needs the Linux system libraries (see doc/COMPANY_BRAIN.md, Blocked)
# and PKG_CONFIG_PATH set, or the `soup3-sys` build script fails first. No
# `--release`: the tuned dev profile runs this gate in 60s at 412% CPU.
export PKG_CONFIG_PATH=~/.local/tauri-sysroot/prefix/usr/lib/x86_64-linux-gnu/pkgconfig
cd app/src-tauri && cargo test                     # 93 passed

# Rebuild the image the API and worker actually run. **All three overlays.**
# Without `dev` the `--build` recreates the containers and changes nothing;
# without `adc` the containers come up with no credentials and the first paid
# stage dies on `DefaultCredentialsError` — see the entry below.
cd infra && docker compose -f docker-compose.yaml -f docker-compose.dev.yaml \
  -f docker-compose.adc.yaml up -d --build api worker
```

**Verify with real exit codes.** `npm run typecheck | tail -6 && echo CLEAN`
reports the exit status of `tail`, not `tsc`, and will print CLEAN over a
failure. Redirect to a file and check `$?` instead.

### Running the stack

Host dev mode is the fast loop and needs no custom image — only the four stock
services run in Docker, with the worker and API on the host. Full commands and
port table are in `doc/COMPANY_BRAIN.md`. All ports are loopback-bound and
deliberately avoid every upstream default (6433/6434, 5532, 7333, 8380, 8787) so
the stack cannot collide with the `sociologia-qdrant` container `docaget/`'s own
compose file starts.

## Architecture

### The layering, and why the app never speaks Python

```
React ──invoke/Channel──▶ Rust (src-tauri) ──HTTP/WS──▶ FastAPI control API
                          docker compose                Temporal client
                          OS keychain                   Postgres catalog
                          file staging                  Memgraph projection
                          typed proxy                         │
                                                        Temporal worker
                                                        (docagent activities)
```

`temporalio` is Python and has no production Rust SDK, so Rust cannot start
workflows. Everything the UI needs goes through the control API, and Rust
proxies each call as an explicit `#[tauri::command]` — no generic pass-through.
That is what lets the webview keep a `default-src 'self'` CSP with no localhost
exception, and it keeps the Rust surface to what only Rust can do: Docker
lifecycle, keychain, native dialogs, filesystem.

Catalog access is *also* behind the control API rather than a Rust database
driver, so there is one owner of the schema and one set of models.

### Rules that are not obvious from any single file

- **Bulk data never enters a Temporal payload.** Payloads cap around 2 MB and
  history persists to Postgres for the whole retention period. Activities write
  to `brainworker/artifacts.py` and return an `ArtifactRef` (relative path,
  sha256, size). Paths stay workspace-relative because the container sees
  `/workspace` and the app sees an app-data directory.
- **API keys never enter a Temporal payload either.** They live in the OS
  keychain, are materialised into a `0600` file mounted read-only, and reach
  activities as process state. Workflows pass a `provider_id`. Vertex AI is a
  step further and has no key at all: `brainworker/providers/gemini.py` uses
  Application Default Credentials, so a workflow carries only a project id and a
  model name.
- **Memgraph does not enforce read-only.** A `CREATE` inside a
  `default_access_mode="READ"` session succeeds on 3.12.0 — Bolt's access mode is
  a Neo4j routing hint and Memgraph's RBAC is Enterprise. What stops a question
  writing is that a planner returns a *template id plus typed parameters*, never
  Cypher, and `graph/queries.py` validates the template registry at import.
  Never add a `Graph.query` overload that takes Cypher.
- **Gemini Enterprise refuses API keys; ADC is the only auth that works.** Both
  `docagent/vertex.py` and `brainworker/providers/gemini.py` use it. Never
  reintroduce a key path — the service rejects it, so no configuration helps.
- **`location` is `global`, never a region.** Gemini 3.x publishes only there;
  on `us-central1` every 3.x id 404s while 2.5 answers 200, so regionalising
  silently pins the whole product to Gemini 2.5.
- **One text per embedding request.** `gemini-embedding-2` returns a single
  embedding for a request carrying four, with no error. Batching does not fail —
  it silently drops. Throughput comes from concurrency.
- **The cost estimate is a range, because one number could not hold both rules.**
  "Never undershoot" and "never overshoot wildly" are both real harms, and
  semantics is a mean over documents that vary by more than 2x — so covering the
  worst meant exceeding twice the smallest. `StageEstimate` now carries
  `output_tokens_high`/`usd_high` and each invariant binds a different end: the
  low figure stays within reach of a typical document, the high one covers the
  worst. `OUTPUT_SPREAD` is 1.86 for semantics and absent everywhere else, because
  every other stage's figure is already a ceiling and widening it would
  over-report twice. The gate renders one figure when the ends agree.
  1.91 is the whole corpus measured, and **the ceiling has moved every time the
  corpus grew: 801, then 1175, then 1210.** That last one arrived two days after
  the previous measurement, in a 37-document batch. A single number could never
  have been set this high — at 1.91x it is the "over-reporting wildly" failure —
  and it is reachable only because the reach test binds the low end. Re-run the
  query in `doc/COMPANY_BRAIN.md` after any large import; do not trust the
  constant to have kept up.
- **The gate's numbers come from the image, not from the repository.** The worker
  and the API run `company-brain-worker:dev`, with the code baked in. A constant
  corrected in `activities/ingest.py` changes nothing at a real gate until the
  image is rebuilt — and **`--build` is a silent no-op without the dev overlay**,
  because the base compose file names an `image:` and the `build:` section lives
  only in `docker-compose.dev.yaml`. **And a third `-f` is needed for the run to
  get anywhere**: `docker-compose.adc.yaml` is applied *conditionally* by the
  desktop app — `compose_args()` adds it only when `find_adc()` located a file,
  because compose creates a directory at a bind mount's missing source — so a
  stack rebuilt by hand does not get it and every paid stage dies on
  `DefaultCredentialsError`. Measured 2026-08-31: the first real eval-set run
  failed at `embedding` for exactly this, having spent nothing and reported the
  cause honestly. The whole command is
  `docker compose -f docker-compose.yaml -f docker-compose.dev.yaml -f docker-compose.adc.yaml up -d --build api worker`;
  drop the second `-f` and compose recreates the containers, reports success, and
  leaves the old code serving; drop the third and it serves new code that cannot
  authenticate. Note `/health` stays green either way — the provider row is free
  and only says whether a project id is set. `POST /provider/probe` is what
  spends and therefore what actually knows. This is not hypothetical: a
  2026-08-22 batch billed 8 of 34 documents above the range's high end and the
  audit first read that as the spread being too small — the deployed image was
  still on `OUTPUT_SPREAD = 1.27` while the repository had 1.86. Before drawing a
  conclusion from a live estimate, check what the container actually holds:
  `docker exec company-brain-api-1 python -c "from brainworker.activities import ingest; print(ingest.OUTPUT_SPREAD)"`.
- **A cost estimate must over-report, and prompt overhead is why it didn't.**
  A generation call pays for its system instruction, schema and JSON envelope
  *per call*, so the miss scales with call count — semantic extraction makes one
  per chunk. Measured on the first real run: correction 924 real against 420
  estimated, semantic output 526 against 63.
- **Prices are keyed by exact model id, never by family prefix.** A prefix match
  would charge `gemini-embedding-2` at `gemini-embedding-001`'s rate. A missing
  price yields `None`, rendered "sin precio", never zero. Sourced 2026-08-20 from
  third-party aggregators — measured counts, second-hand multipliers.
- **Reasoning tokens are billed as output and `candidates_token_count` excludes
  them.** `Usage.output_tokens` includes thinking; `thinking_tokens` is a subset
  of it, not an addition. Ignoring them under-reported a real run by ~4x, and
  `max_output_tokens` is measured against reasoning too — a small budget returns
  `MAX_TOKENS` with no text.
- **Reasoning budget is per stage, and each value was measured.** Off for
  planning (classification), correction (already gated by `verify()`) and
  semantics (measured *better* without: 13 concepts against 8, at 40% of the
  cost). **On for answering, decided against a neutral measurement** — a
  deliberate bias toward caution in the stage that either cites or fabricates.
  `thinking_for()` also resolves the engine's own stage names, so
  `stage="correct"` does not silently miss.
- **Tests must never write to the `brain` Qdrant collection.** Set
  `BRAIN_QDRANT_COLLECTION` to a disposable name and drop it. Leftover test
  points once outnumbered real ones 105 to 5 and silently turned eight retrieval
  tests into skips.
- **Which generation stage costs most depends on `thinking_budget`.** Reasoning
  on: correction > semantics. Reasoning off: semantics > correction. Both dwarf
  embedding either way, which is what the gate's position rests on.
- **Correction goes through `providers/adapter.py`, never a reimplementation.**
  `docagent.correct.verify()` discards a correction that loses a scripture
  reference, a number or a proper noun, and its per-paragraph cache survives an
  interrupted run. Both exist because of measured failures; a rewrite drops them.
- **Qdrant point ids are `point_id(version_id, chunk_index)`**, not the engine's
  path-hashed `doc_id_for()`. That is what makes re-indexing converge and a
  duplicate file add nothing — and what makes the activity idempotent under
  Temporal retries.
- **Semantic extraction is one chunk per call.** Batching is cheaper and wrong:
  every relation must carry the `source_chunk_id` a person can check, and a model
  given ten chunks attributes claims to the wrong one.
- **A claim carries a quote the code found in the chunk, or it carries no span.**
  The claim's `text` is the model's paraphrase; `quote` is a span the extractor
  located in the chunk's own text, and `quote_char_start/end` index the corrected
  byte stream like every other offset here. Matching tolerates whitespace and
  nothing else — a fixed accent or a dropped word is a quote the document does not
  contain. A quote that does not check out costs the claim its span, not its
  existence, and `Semantics.claims_verified` reports how many survived, because a
  claim nobody can check must not look like one that can. The idea is graphrag's
  `Claim Source Text`; the checking is not — nothing there verifies it.
  **Measured 2026-08-21: 98.7% and 99.2% of claims across two runs carried a
  quote the code found.** The feature rested on an unmeasured assumption about
  whether the model copies verbatim; it does.
- **A claim's `status` says what the document *does* with it**: `afirma`, `niega`,
  `atribuido`, or `sin_estado` for one extracted before the field existed. There
  is no safe default: a text expounding the doctrine it is about to rebut
  enunciates it in the same words as one who holds it, so `sin_estado` must never
  render as `afirma`. Spanish on the wire like the chunk kinds. Measured on a real
  document: **14% of claims were not plain assertions** — 10 `atribuido` and 1
  `niega` out of 79.
- **`related_documents` returns which concepts are shared, not only how many.**
  The traversal that counts them has already found them, so `collect` costs the
  server nothing over `count` and saves a request per selection on the client —
  which is what lets the Graph screen answer "*which* 57?" and light the shared
  nodes without inventing an edge. Capped at 200 per document, with
  `shared_concepts` still the true count so a shorter list reads as truncated
  rather than as fewer. **This is the one template whose payload grows with the
  product of two documents' concept sets**; anything similar needs a cap too.
- **In the Graph tab's *document* view, a click on an outer document *compares*,
  it does not re-centre.** Re-centring moved to an explicit button in the pane,
  because the question people arrive with is "*which* concepts do these two
  share" and the
  click should answer it. Selecting lights the shared concepts, fades the rest —
  fades, never hides: dimming them away would delete half the comparison — and
  the pane lists all of them by name. The two "only in" lists it shows beside
  them are **sound but not exhaustive**: each version returns its top concepts,
  so what is listed really is unique to that side, but there may be more neither
  response mentioned. The pane says so.
- **The library graph's volume control is degree, and confidence is not.**
  Measured on the real corpus 2026-08-25 (67 books, `lib_teologia`): raising
  `confidence_floor` from 0.6 to 0.9 removes **3%** of the `MENTIONS` edges — the
  extractor is confident about nearly everything it proposes — while
  `min_documents = 2` removes **84%**, taking 10,835 concepts to 1,719 and 15,367
  edges to 6,251. Everything degree removes is a concept only one book mentions,
  which cannot join two books, which is the one thing an overview is for. So
  `library_mentions` filters on the concept's degree and the floor is kept only
  because the badge promises one. The planning assumption was "hundreds of
  concepts"; it was wrong by twenty times, and nothing but running the query
  would have said so.
  **The default is 3 since 2026-09-01, not 2.** Re-measured that day after the
  orphan sweep: 12,823 concepts, **2,034 at degree ≥ 2 and 858 at ≥ 3** — the
  same 16% surviving `≥ 2` as in August, so the ratio is stable in this corpus
  and the August figures can still be read as current. 2 was chosen as "the
  subgraph that has edges between books at all", which is the right *definition*
  and was still two thousand nodes on a 630px canvas. The next step up is ≥ 5 at
  322, which begins reading as a map of general topics rather than of the bridges
  between these particular books. The number lives in one place — the
  `library_mentions` template's `Param` default — and both planes derive their
  route default from it (`_template_default`, `templateDefault`), which
  `queries.parity.spec.ts` compares. On the client it lives in
  `app/src/lib/graphModel.ts` as `THRESHOLDS` and `DEFAULT_THRESHOLD`, imported
  by the screen rather than repeated in it, because the derivation and the
  control must not disagree about what the stops are.
  **Since 2026-09-01 the degree filter no longer reaches the server at all.**
  The app fetches `min_documents = 1` once and derives every threshold from that
  envelope — see *The degree filter is an equality, not an approximation* below.
  So the request never carries a threshold, and the template's default matters
  only to a caller that is not this app.
- **`library_mentions` aggregates twice, and its `ORDER BY` names the returned
  aliases.** The first `WITH` collapses chunks into one weight per (book,
  concept); the second counts books per concept so the degree filter can apply to
  the *concept* while the per-book weights survive in a `collect`. A single pass
  would have to choose between the two. And ordering by `k.id` fails at *query*
  time with `Unbound variable: k` — the RETURN aggregates, so the pattern
  variables are out of scope by then. **The validator does not catch this**: it
  reads labels and keywords, not scope. Only a test against a real Memgraph does,
  which is what `tests/graph/test_library_overview.py` is for.
- **A template may raise its own row ceiling only if the planner cannot name
  it.** `MAX_LIMIT = 200` exists because unbounded traversal on a dense concept
  graph takes the desktop app down. A whole-library canvas needs 20,000 rows, so
  `Param.cap` overrides the ceiling per parameter and `Template.planner_visible`
  keeps those templates out of `catalogue()` — out of a model's reach, and out of
  the planning prompt's token budget. The two are one decision, and
  `test_every_template_that_raises_its_ceiling_is_hidden_from_the_planner` is
  what keeps them from being separated. `get()` and `bind()` are untouched, so
  the API still calls them by id exactly as it calls any other.
  **`mention_limit` is 60,000 since 2026-09-01, and it became load-bearing that
  day.** It was 20,000, set against 15,367 rows measured 2026-08-24. Re-measured
  on the same library, now 73 books: **17,814 rows at `min_documents = 1` with
  the floor at 0.5 — 89% of the cap.** That used to be an occasional query and is
  now the standing envelope the app fetches on every floor change, and the
  symptom of going over would not be a refusal but a *silently smaller graph at
  every threshold*. 60,000 is about 240 books at today's 247 rows per book, and
  it is also above the hard ceiling on rows at any floor: a row is a distinct
  (version, concept) pair and the whole graph holds 27,991 `MENTIONS`, so
  lowering the floor can no longer cut this library. `default` moves with `cap`
  because no caller has ever passed the parameter, so the default is the
  operative number and `truncated.edges` is compared against it. The fork in
  `../yorch-tauri-backend/src/graph/queries.ts` carries both numbers and its
  comment; `queries.parity.spec.ts` compares them field for field against the
  Python registry dumped live, **verified by breaking it** — one side at 55,000
  fails with the exact diff.
- **`/project-summary` degrades instead of failing, and never reports zero for
  "could not ask".** Every other read turns a down Memgraph into a 503, which is
  right for a screen whose whole content is the graph. Home is the screen the app
  opens on, and a 500 there says nothing about the half that *was* readable — so
  each leg carries its own `available` flag and its figures stay `null`. Zero
  would be a claim about the corpus, and it sends a reader to look for a broken
  extractor instead of a stopped container. `recent_runs` is `null` when the
  catalog could not be read and `[]` when it answered and nothing has run.
- **`document_version.page_count` is a column nothing writes.** `register_version`
  (`activities/ingest.py`) runs *before* extraction, so the page count is not
  knowable there. The summary therefore reports `versions_with_pages` beside the
  sum, and the UI renders "0 of 69 versions record one" rather than a zero —
  a measurement rather than a hardcoded absence, so the figure starts working by
  itself the day something fills the column in. The enabling step is one write
  after extraction; nobody has done it.
- **The overview's node list comes from the graph, not from the catalog.** A
  canvas that draws the projection must list what the projection contains;
  sourcing books from Postgres and edges from Memgraph would let the two disagree
  silently, and the disagreement would render as a book with no concepts —
  indistinguishable from one whose semantics were never extracted. A document
  indexed but never projected therefore does not appear there. The Library screen
  is the catalog's own view and stays so.
- **A concept's `documents` is the degree the database counted, not the number of
  edges in the payload.** "Dios" is mentioned by 59 books and a capped response
  carries a handful of its edges; recomputing the degree client-side would
  understate every concept on screen, and the degree is exactly the number a
  reader uses to decide whether a concept joins anything.
- **`INVOLVES` is the graph's only concept-to-concept path.** Every other route
  between two concepts runs through a chunk that mentions both, which is
  co-occurrence, not a relation anybody stated. A claim carries a second concept
  when the fragment relates two, and `claims_between_concepts` traverses it in
  either direction — which one the extractor made the subject is an artefact of
  the sentence. Hung off the `Claim` rather than drawn between the concepts
  directly, because an edge cannot carry the verified quote. Optional in the
  schema on purpose: requiring it would invite the model to invent the relation
  the prompt tells it not to. `SUPPORTS`, `CONTRADICTS` and `RELATED_TO` are
  declared beside it and have never been projected by anything — vocabulary, not
  a contract.
- **Every path that reaches a concept must appear in `_CANDIDATE_CONCEPTS`.**
  Orphan collection walks `MENTIONS`, a claim's `ABOUT` *and* its `INVOLVES`.
  Adding a third way to reach a concept without adding it here is how claim-only
  concepts were orphaned once already; the same hole was closed for `INVOLVES`
  before it could be dug, with a test that fails when the clause is removed.
- **A claim reaches the answering prompt as a `lectura`, never as a source.**
  `answer.SYSTEM` states that the chunk's text wins any disagreement, and the
  model still cites by `chunk_id` — `Citation` has one namespace. Without that
  rule this is model output re-entering the context as though it were the
  document. Capped at `CLAIMS_PER_CHUNK`, because claims compete with the chunk's
  own text for the attention that keeps a citation accurate.
- **Gleaning exists, is off, and the default is now measured.** `Gemini.max_gleaning`
  runs extra extraction passes over the same chunk *in the same conversation*.
  Measured 2026-08-21 on a 16-chunk document: one pass buys **+52% claims and
  +40% concepts for +70% cost**. It stays at zero anyway, because nothing here
  can score whether the extra claims are *better* — the eval-set stage that would
  is still a switch with no activity behind it, so turning it on buys measured
  volume against unmeasured quality. The "are there more?" question rides in the
  schema rather than costing a second call, which is what nano-graphrag and
  graphrag need only because their extraction returns delimited text.
- **A concept's description is free; condensing it is not.** Each chunk's
  description is appended to `Concept.description_raw`, filtered against what is
  there so a Temporal retry converges — `SET list = list + new` is the one write
  in `projection.py` that would otherwise double. Below `CONDENSE_TOKENS` the
  concatenation *is* the description and costs nothing; above it, one call per
  concept, and above `CONDENSE_SOURCE_CAP` sources nothing at all, so a concept
  fifty chunks mention is not re-summarised on every import. Behind
  `StageOptions.condense_descriptions` with its own estimator entry and its own
  `semantics-condense` ledger row, because it spends.
- **No answer ships without a citation the code verified.** The model cites by
  chunk id; every id is checked against the evidence it was given, and one it
  invented is dropped. An answer left with no surviving citations becomes
  `insufficient_evidence` — never a warning attached to an answer. graphrag asks
  for the same thing in its prompt (`[Data: Claims (2, 7, +more)]`) and checks
  none of it; the checking is the product.
- **Retrieval's citation lookup is not gated on the planner's decision.** What
  `plan.template_id`/`plan.concepts` gate is the *expansion* — the extra chunks.
  `_expand` used to return before `_attach_citations` when the planner chose no
  traversal, and since `answer._verify` drops any citation whose chunk has no
  locator, a question the vector index had answered came back as "no verifiable
  citation". Fixed, with `test_a_vector_only_plan_still_resolves_its_locators`
  standing on it.
- **A database dump alone is not a backup.** Bulk data never enters a Temporal
  payload, so `chunks.jsonl` and `semantics.json` live in the workspace and
  Postgres holds only an `ArtifactRef` — a relative path, a sha256 and a size.
  Dump the catalog without the workspace and every `rebuild` has a row pointing
  at nothing; the only way back is re-running correction, the expensive stage.
  `infra/backup.sh` takes all four legs and verifies each `run_artifact` row
  against its file, which is how 79 of 238 pruned artifacts were found — none of
  them belonging to an active version, and nobody had noticed.
- **`CREATE SNAPSHOT` on Memgraph destroys snapshot history as a side effect.**
  It keeps `--storage-snapshot-retention-count` and evicts the oldest, so at the
  stock 3 three backups wipe out everything older than the first of them. That
  cost a pre-deletion snapshot on 2026-08-21, 24 minutes after it was taken.
  `infra/docker-compose.yaml` raises the count; a backup must never be the thing
  that removes the state you would want to go back to.
- **The worker waits for the API, because the API owns the migrations.**
  `api`'s lifespan applies the catalog migrations before it serves, which is what
  keeps the schema and the code reading it from ever disagreeing. The worker
  writes artifacts and costs to that catalog and migrates nothing, so
  `infra/docker-compose.yaml` makes `worker` depend on `api` being *healthy* —
  otherwise a cold start races a worker against an unmigrated schema. The
  coupling is startup-only on purpose: bookkeeping writes are best-effort, so an
  API that dies later must not stop the worker, and `depends_on` says nothing
  about that case.
- **Asking is two calls: hand the question over, then collect it.** `POST /ask`
  returns a `question_id` and `GET /ask/{id}` brings the answer, because the
  single synchronous call lost one. A real question against the church-history
  library outran the app's 180s `ASK_TIMEOUT`; reqwest reports an expired
  timeout with the same "error sending request for url" text it uses for a
  refused connection, so the UI blamed an unreachable control API while the API
  log read `POST /ask 200 OK`. The answer was computed, was billed, and was
  discarded under a message pointing at the wrong thing. The store is the API
  process's own memory, bounded and reaped, and is safe **only because uvicorn
  runs a single worker** (`entrypoint.py`) — passing `workers=N` there would
  send a poll to a process that never saw the question.
- **`off_corpus` and `insufficient_evidence` are different states.** The first
  means nothing cleared `topicality_gate`'s dense floor, the second means the
  corpus was searched and came up short. They have different fixes.
- **The approval gate is bounded and its timeout is a rejection.** Seven days,
  because a user may close the lid on Friday; not unbounded, because a workflow
  that never reports an outcome accumulates in the namespace. A timeout costs
  nothing and leaves the document importable, so it is not a failure.
- **Artifact and cost recording is best-effort and must never fail its stage.**
  Use `Catalog(..., pooled=False)` for those writes: a pool retries a refused
  connection in the background, so a catalog that is merely down turns each
  bookkeeping write into a full-timeout stall rather than one immediate error.
- **One `DocumentVersion` per distinct sha256, not per import.** A byte-identical
  file at a second path becomes a second `HAS_VERSION` edge, not a second set of
  chunks. This is the deliberate fix for `doc_id_for()` hashing the path, and it
  is enforced by a `UNIQUE` constraint on `document_version.content_sha256`.
- **The worker runs in Docker, not as a bundled sidecar.** Shipping Python 3.13
  plus PyMuPDF wheels across three OSes is the largest packaging risk in the
  project and buys nothing, since Docker is already required.
- **`docagent` resolves `profiles/`, `cache/` and `state/` relative to the
  process CWD.** Setting `WORKDIR /workspace` relocates all three with no change
  to its code. `config.configure()` chdirs for the same reason on the host.
- **The control API binds `127.0.0.1` by default.** It has no authentication;
  it is safe only because nothing off the machine can route to it. The container
  opts into `0.0.0.0` explicitly via `BRAIN_API_HOST`, because Docker forwards
  the host's loopback to the container's own interface.
- **Serde renames on the serialize side only** for types that deserialize from
  the Python API and re-serialize to the webview (`control.rs`), so both ends
  stay idiomatic without a translation struct.
- **Removal writes projections first and the catalog last.** The catalog is the
  source of truth and the other two are derived from it, so a crash after the
  catalog row is gone leaves points and nodes nothing can find again to retry.
  The reverse order leaves the operation *retryable*, which is why removal is a
  request handler rather than a workflow. `brainworker/removal.py` owns the
  order so no caller can get it wrong.
- **Destructive Cypher is a server-owned literal, never a template.**
  `graph/queries.py` is the read surface a planner may name by id; removal lives
  in `graph/projection.py` beside the other writes. A template id is a string a
  model can return, so nothing destructive may become one.
- **A version two documents hold survives removing either.** Byte-identical
  files at two paths are two document slots and one `document_version`. Removal
  deletes a version only when no `document_version_link` survives — and
  re-points the surviving points' `document_id` payload, which otherwise names a
  document that no longer exists.
- **Orphan `Concept` collection is scoped to the removed version's own
  concepts**, reached through `MENTIONS` *and* through a Claim's `ABOUT`.
  Following only `MENTIONS` left every claim-only concept behind; an unscoped
  `MATCH (k:Concept) WHERE NOT (k)--()` would delete another run's concepts
  during the window `project_concepts` opens before `project_semantic_edges`
  attaches them.
- **`citation_id` is keyed on the locator, and the locator embeds the title**, so
  re-projecting under a changed title used to mint a second `Citation` per chunk.
  `project_structure` now prunes citations of the chunks it just projected that
  it did not produce.
- **Reindex and rebuild are different verbs with different bills.** Reindex
  re-runs the pipeline through the normal gate; rebuild replays
  `chunks.jsonl` and `semantics.json`, so only embedding can spend. Rebuild is
  *not* "reindex with correction off": correction changes the text's length, so
  that produces different `char_span`s and a silently different index.

- **A `char_span` indexes one of three streams, and nothing records which.** A
  run can leave `raw.txt` (extraction before any rule), `extracted.txt`
  (extraction with the learned profile's rules applied — a *second* pass the
  pipeline makes whenever a profile is adopted) and `corrected.txt` side by side,
  and the offsets belong to exactly one of them. No artifact, column or event
  says so. Choosing by precedence reads as obvious and is a guess, and it fails
  in the expensive direction: measured on `ver_0ebf4f0b50a27202db3fcca6`,
  **`raw.txt` verifies 8 of 600 spans where `extracted.txt` verifies 600 of
  600** — a byte-exact index that a precedence rule would report as broken.
  Anything checking a span against a file has to score every stream present and
  choose by the numbers; `auditversion.choose_stream` is the one that does, and
  it prints the losers' scores beside the winner's so a total failure is
  distinguishable from a slight offset.

- **The embedding model belongs to the collection, not to the engine.**
  `docagent.vertex.EMBED_MODEL` is `gemini-embedding-001`, which is what the CLI
  writes into `docagent_v2`; `BRAIN_EMBEDDING_MODEL` is `gemini-embedding-2`,
  which is what this worker has written into all 5,335 points of `brain`. Both
  are 3,072 wide, so **Qdrant accepts either into either without an error and the
  cosine between them means nothing** — a healthy log over a corrupt ranking. So
  `docagent/runner.py` names no model constant at all and takes an `Embedder`
  instead; `index_chunks` raises `VectorSpaceMismatch` when the embedder and the
  writer disagree, before a single request is paid for; and the worker logs the
  divergence once at startup, because nothing downstream can see it — both halves
  of a single run always agree with each other.

- **The engine embeds and Yorch stamps.** `runner.index_chunks` hands
  `IndexRow`s to an injected `PointWriter`, and `brainworker/indexing.py` is the
  only place that derives a point id or builds a payload. The engine must never
  learn what a tenant is: `doc_id_for()` hashes a filename, and a point written
  into `brain` without a `tenant_id` is unreachable by either plane with its
  embedding already paid for. That is also why `version_scope()` is one function
  — the tail prune, the removal filter and the evaluation's scope are the same
  predicate, and two call sites that disagreed would score a document against
  another document's chunks.

- **A measurement without a scope is refused.** `runner.evaluate` and
  `tune_once` raise `UnscopedEvaluation` on an empty filter rather than searching
  the whole collection, because the figure that comes back would be a fact about
  the corpus wearing the name of one document.

- **A reused profile's eval set is dropped unless it was written from this
  document.** The fingerprint groups by structure and structure is not subject
  matter, so book B scored against book A's questions returns a recall of 0 that
  means nothing about either. The engine drops it in `n_load_profile`;
  `paid.build_evalset` has to apply the same rule or the fix does not travel.

- **A structural collision withholds activation, and nothing else does.** When
  the inherited heading rules and the built-in ones disagree about how many
  chapters a document has, the run indexes everything and skips
  `activate_version`: the index exists, the graph is projected, the bill is
  reported, and the *previous* version stays answerable. Rule-learning falling
  back to defaults is deliberately not in that list — after three failed attempts
  the engine adopts rules that were themselves measured on a real book, and
  blocking there would refuse to publish documents whose only fault is being
  ordinary. The two ways out are `POST /libraries/{id}/versions/{id}/activate`,
  which costs nothing because the index is already paid for, and re-importing
  with `ignore_profile`, which pays for a full run. **`run.state = 'blocked'`
  is a real value**, added in `20260831140000_run_blocked` because none of the
  other five was honest: the run did every stage and paid for them, so it did
  not fail; it withheld the last step, so it was not a plain success; and nobody
  cancelled it. It is the same distinction the gate timeout already makes at the
  other end. **Exercised on the running stack 2026-08-31** by giving a document
  a profile that read 1 chapter where the defaults read 8: `blocked` /
  `structural_mismatch`, the reason in the row, the prior version still active,
  and `POST …/activate` promoting it afterwards — 404 with
  `version_not_found`, never 403, for another library's.

- **Tuning is one candidate, never a loop.** The free half — `min_score`,
  `per_section`, dense-only — changes nothing in the index and is tried
  exhaustively; the paid half re-cuts the document, so every chunk is a new
  string, none of them is in the embedding cache, and it is a second full
  indexing pass. A revert re-chunks and re-indexes rather than only rewriting the
  profile: the engine's measured bug was that reverting the profile left the
  *collection* holding the candidate's chunks, and three rounds drifted
  0.729 → 0.762 → 0.700 having each reverted.

- **The engine's stateful directories are parameters now, not the CWD.**
  `profiles.load/save`, `correct.correct_paragraphs` and the new
  `docagent/embedcache.py` all take an explicit root, and `docagent/workspace.py`
  carries the three together. `os.chdir` is process-global and the worker runs
  activities concurrently, so it could never serve two organisations at once —
  which is why `CLAUDE.md` listed profiles as the open one of the three gaps
  tenancy does not cover. `profile_dir()` passes
  `Paths.for_tenant(tenant).profiles`, and the legacy tenant keeps the volume
  root, so the 23 profiles already on disk are untouched. **A non-legacy tenant's
  existing profile is orphaned by this** and will be re-learned once, for about
  $0.006.

- **The correction cache is one file per paragraph.** It was one JSON rewritten
  after every batch, which is correct for one process and lossy for two: the
  second writer's dict was assembled before the first one wrote. The legacy
  `paragraphs.json` is still read and never written, so nothing already paid for
  is re-spent.

- **The worker's embedding path has a cache and the engine's quota patience.**
  `Provider.embed` had neither: `cache/` under the workspace was empty from the
  day it was created while `profiles/` beside it held 23 files, and `_call`
  retried three times at 1.5^n — about seven seconds against a *per-minute*
  quota bucket. `CachedEmbedder` fronts `Provider.embed` with
  `docagent.embedcache` (the checks still run; it is not a second path to the
  API), and 429s now get six attempts at 2/8/32/60/60 honouring `Retry-After`.
  Paid activities get two Temporal attempts and each one runs the whole stage, so
  without the cache the second attempt re-paid for every vector of the first.

### Tenancy, and the rules it added here

Two planes over one set of stores. Everything below is a constraint on code in
this repository; the plane itself is documented in its own checkout.

**Taken end to end on 2026-08-28**, which is what makes the gaps listed under
"What is not built yet" gaps rather than guesses. A second organisation was
seeded (`acme`), given a Cognito user and a library, and a real 226 KB PDF was
uploaded through `POST /uploads`, indexed through the approval gate — **$0.1255
billed against a $0.135–$0.19 estimate**, so the range over-reported, which is
the direction it is supposed to fail — and answered, with 5 verified citations
carrying byte-exact locators. Isolation was checked in both directions against
the running stores afterwards: legacy holds 72 documents, 69 indexed versions,
4,721 Qdrant points and 69 `DocumentVersion` nodes, all unchanged; acme holds 1
document, 1 indexed version and 13 points; and **no row, node or point in any of
the three stores lacks a tenant**. Three real leaks were found by doing this —
the entries below on field defaults, on listings and on library ids — none of
them failed anything, which is why none of them had been found by reading.

**Those legacy figures are historical as of 2026-08-31**, when that whole corpus
was migrated to a second organisation (`preprod`) — see *Moving a corpus between
organisations* below. Legacy now holds 0 documents, 0 versions, 0 nodes and 0
points; `preprod` holds 74 documents, 70 indexed versions, 47,860 nodes and 4,724
points; acme is untouched at 1 document, 1 indexed version and 13 points. The
measurement above is left as it was taken, because what it verified — that
isolation holds in both directions — is what made the migration safe to attempt.

- **The error body's `detail.kind` is a client contract.** `app/src/lib/api.ts`
  parses it and keys its guidance map on it, so both planes emit
  `{"detail": {"kind": …, "message": …}}` and an unmatched route emits FastAPI's
  bare `{"detail": "Not Found"}` — a kind the guidance map has never heard of is
  worse than no kind. The paid plane reproduces four FastAPI quirks for the same
  reason: `/ingest`'s embedded body against `reindex`'s bare one, `approved` as
  a lowercase *string*, `@property` fields absent from every payload, and
  `/runs/{id}` omitting `semantics` rather than zeroing it.

- **Derived ids are salted with the tenant, except the legacy one's.**
  `version_id` and `concept_id` take a tenant; everything downstream (`sec_`,
  `chk_`, `clm_`, `cit_`) inherits it. `_salt()` returns nothing for the legacy
  tenant, and that branch is load-bearing rather than tidy: salting
  unconditionally would have invalidated every id in the graph and every Qdrant
  point at once, and the projections can only be rebuilt from artifacts.
  **Measured 2026-08-26: 69 indexed versions, all 69 with a `chunks` artifact
  recorded, and only 31 with the file still on disk.** The other 38 would have
  needed the full pipeline re-run, correction included, against source files
  many of them no longer record. `test_the_legacy_tenants_ids_are_exactly_what_they_were`
  is what stops somebody tidying the branch away.
  **That measurement is now historical**: those 69 versions left the legacy
  tenant on 2026-08-31, so on this installation the exemption protects nothing.
  The branch stays anyway, and not out of sentiment — it is the id contract for
  any installation whose legacy tenant is still populated, and the free plane
  still writes under that tenant here, so removing it would change the ids of
  whatever is indexed there next. What the migration did prove is that the
  fear behind the exemption was survivable after all: every salted id turned out
  to be recomputable from the stores themselves, no artifact required.

- **A salted id is not authorization.** It is `digest(tenant, content)`, and a
  tenant id is a value its own members hold — it travels in `X-Tenant-Id`. A
  member of A who has the same file as B can recompute B's `ver_`. Salting stops
  collisions; only a predicate refuses a read. That is why
  `graph/queries.py::validate_template` **refuses to load a template that does
  not use `$tenant_id`** — checked as a used parameter, not by looking for a
  `WHERE`, because where the predicate belongs differs per template.

- **`ALLOWED_FILTERS` must never contain `tenant_id`.** It is not a narrowing a
  caller may request; it is the scope the caller is confined to, and
  `retrieve.search` assigns it after the allowlist so a smuggled key loses
  anyway. Two guards for one property, because this is the property.

- **The workspace is per organisation, and the legacy root excludes
  `tenants/`.** `Paths.for_tenant` gives the legacy tenant the volume itself —
  same reasoning as the ids: a `run_artifact` row stores a workspace-relative
  path. `contains()` therefore has a second clause, and it is not symmetry:
  without it the one organisation that predates tenancy could read every one
  that came after. A test found that after the first version shipped.
  `stage_source` checks it before hashing, because that activity is handed a
  path and reads whatever is there.

- **A tenant default on a *field* is the same mistake as one on a column, and
  it cost a whole organisation's graph.** `VersionNode.tenant_id` defaulted to
  `LEGACY_TENANT_ID` with the same honest reasoning the columns had — a replay
  of an old `semantics.json` should land where its rows already are. All four
  sites that build one forgot to pass it: the three in `activities/ingest.py`
  and the `IngestRequest` that `load_rebuild_inputs` reassembles, two lines
  above a comment that gets it right for `Registered`. Measured on a real import
  2026-08-28: a paying organisation's `Document`, `DocumentVersion`, 5
  `Section`s, 13 `Chunk`s and 13 `Citation`s went into the legacy tenant's graph
  under an **unsalted** version id, while its 50 `Concept`s went in correctly and
  were orphaned. **Nothing failed.** Retrieval returned the right chunks, with
  an empty `locator` and no claims — and since `answer._verify` drops a citation
  whose chunk has no locator, the answer came back with none. That reads as a
  graph nobody has projected yet, not as a leak. The field is required now, and
  a caller that means the legacy tenant says so — one word at the two sites where
  that is true. Four tests pin the four sites, because "required" stops a *new*
  site being written without a tenant and says nothing about one passing the
  wrong one.

- **A listing has no id to resolve ownership through, so the predicate is the
  whole boundary.** `Catalog.libraries`, `project_totals` and `recent_runs`
  enumerated across every organisation; the free plane's `/libraries` listed a
  paid tenant's libraries the moment one existed. All three take a required
  `tenant_id` now and the FastAPI plane names `LEGACY_TENANT_ID` at the call
  site, because that plane *is* that organisation and saying so is what makes it
  a decision — an organisation that is **empty** since 2026-08-31, so the free
  plane answers `/project-summary` with zeros and `available: true`. That is the
  degradation working, not a failure, and it is the accepted consequence of
  moving the corpus rather than copying it. `project_totals` carries the predicate in each of its eight scalar
  subqueries — there is no join to hang one outer filter on — so the test seeds
  two organisations and asserts each figure rather than the row.

- **The library id is chosen by the client, so `ensure_library` checks who owns
  it.** It arrives on every `IngestRequest` as a plain string, and
  `lib_teologia` is a value two organisations pick independently. The upsert's
  `ON CONFLICT (id) DO UPDATE` now carries `WHERE library.tenant_id =
  EXCLUDED.tenant_id` and raises `LibraryOwnedByAnother` when no row comes back.
  Without it the second organisation's ingest renamed the first's library and
  attached its documents to a row *neither* could list, since every listing
  filters on `tenant_id` and the two then disagree.

- **No column carries a tenant default any more.** Phase 1 used one so the free
  plane and every activity could keep writing untouched; phase 2 threaded a
  tenant through `IngestRequest`, `Registered` and the eight `repo.py` writers
  and took it off. Three of the eight — `run_artifact`, `cost_entry`,
  `profile_warning` — *derive* it in the INSERT from the run or version they
  hang off, so an artifact and its run cannot disagree about who owns them.

**Three things tenancy does not cover, and why.** `PROFILE_DIR` and `CACHE_DIR` in
`docagent` are module constants relative to the process CWD, and the worker runs
activities concurrently, so a chdir per run is unsafe. The correction cache is
content-addressed (`sha256(prompt_version + text)`), so sharing an entry requires
already holding that paragraph — a saving, not a channel. **Profiles are the open
one**: a profile reused across organisations by structural fingerprint carries
its `header_patterns`, which derive from a book's running header and are often
its title. The `evalset` and `scores` do *not* travel — `n_load_profile` drops
them when `learned_from` names a different file, and across organisations it
always does. Closing it needs `docagent` to accept an explicit root instead of
resolving from the CWD; it was judged not to block the paid plane, and this
paragraph is the record of that judgement.

**Run artifacts are the third, and the one most likely to be misread as
covered.** `Paths.for_tenant` exists and is used — but on the *inbox*, by
`stage_source`'s containment check, and nowhere else. All twelve sites that build
an `ArtifactStore` pass `settings.workspace`, so `rel_path` is measured from the
volume root and every organisation's run artifacts share `<workspace>/runs/`.
Unlike the correction cache this is a real crossing rather than a saving: the
files are readable by anyone holding the root. `for_tenant`'s own docstring used
to claim the opposite, and on 2026-08-31 that sentence sent a migration to move 34
run directories into a tenant subtree, where 157 of 159 `chunks`/`semantics` files
became unreachable and `rebuild` was broken until they were moved back —
`test_a_reference_is_relative_to_the_workspace` is the behaviour to trust here,
not prose. Closing it is those twelve call sites plus a move per organisation.

### Moving a corpus between organisations

Done once, on 2026-08-31: the whole legacy corpus — 72 documents, 69 indexed
versions, 48,247 graph nodes, 4,721 Qdrant points, 524 artifact rows and $32.18
of ledger — moved to `preprod`. Recorded here because the next one will be
tempted by the cheap version, and the cheap version is wrong.

- **An `UPDATE tenant_id` is not a move.** `_salt()` folds the tenant into `ver_`
  and `con_`, and everything downstream inherits it, so relabelling leaves the
  destination holding ids it would never compute. The next import then mints a
  second `ver_` for a book already there and a second `con_` for a concept
  already there — the graph forks at exactly the join it exists to provide.
- **Every derived id is recomputable from what the stores already hold**, which
  is what makes a faithful move possible at all: `content_sha256` on
  `DocumentVersion`, `path` on `Section`, `ordinal` on `Chunk`, `canonical` on
  `Concept`, `text` + `source_chunk_id` on `Claim`, `locator` on `Citation`, and
  the Qdrant point from `uuid5(version_id:chunk_index)`. No artifact is needed
  and nothing is re-embedded, so the move cost **$0**. That matters against the
  obvious alternative: rebuilding under the new tenant reaches only the versions
  whose artifacts survive, which here was **31 of 69**.
- **The check that makes it trustworthy is recomputing the id you already have.**
  Before writing anything, re-derive each node's *source* id from its own
  properties and compare it with the stored one. It proves the derivation rather
  than assuming it, and it costs one read: **48,247 nodes accounted for, zero
  mismatches**, and afterwards 47,867 re-checked against the destination tenant,
  also zero.
- **Edges carry ids too.** `MENTIONS`, `ABOUT` and `INVOLVES` each store a
  `source_chunk_id` property — ~49,000 of them. A re-salt that touches only nodes
  leaves the graph pointing at chunk ids that no longer exist.
- **Re-salting into an organisation that already holds a concept is a merge, not
  a rename.** The legacy id of `dios` *becomes* the id `preprod` already had, so
  the edges have to move and the loser deleted. Two collided; `Dios` came out as
  one node with 685 chunks.
- **Some debris cannot be re-salted, because the datum that derives it is gone**:
  214 orphan `Claim`s (their `source_chunk_id` names no chunk), 127 `Citation`s
  with no `CITES` edge, 1 `Chunk` with no `version_id`. Deleted, along with 65
  concepts only those claims reached. Carrying them would have reintroduced
  exactly the unreproducible ids the whole exercise avoids.
- **Projections first, catalog last**, for the reason `brainworker/removal.py`
  gives: a crash before the catalog is written leaves the move retryable. The
  catalog is also the only store that can still answer once the graph is done —
  it holds the `content_sha256` the version map is rebuilt from.
- **The inbox moves; run artifacts do not.** `stage_source` refuses a
  `source_path` outside `Paths.for_tenant`, so the inbox *must* follow the tenant
  and `document.source_path` must be rewritten with it. `ArtifactStore` is tenant
  blind, so `runs/` must stay at the volume root. Getting this backwards broke
  `rebuild` for the whole corpus until the directories were moved back; see the
  third gap under *Three things tenancy does not cover*.
- **Anything in flight is left incoherent.** An `awaiting_approval` run holds the
  pre-migration `version_id` in its workflow state, so approving it after the move
  writes old-shaped ids back. Terminate it and re-import; the free gate has cost
  nothing yet.

### Why the approval gate sits where it does

From `docaget/costo.json`, a real measured run: correction $0.0334, eval-set
generation $0.0131, embedding $0.0033. **Correction dominates, not embedding.**
So the free gate runs before correction, every stage is individually switchable
there, and a second optional gate shows the correction diff before the remaining
spend. Prices in `ledger.py` are third-party multipliers over measured token
counts — repeat that caveat wherever a dollar figure is shown.

Note the ordering constraint: correction runs *before* chunking because it
changes the text's length, which would invalidate every `char_span`. Previewed
chunks are therefore not the final chunks when correction is on.

## Both checkouts are in git now, and it is worth knowing why that mattered

Resolved 2026-08-29. Kept here rather than deleted, because the reason it was
urgent is a property of this repository and not of that week.

- `../yorch-tauri-backend` is a git repository with a clean tree.
- This checkout is on `feat/paid-plane-integration`, branched from `main`, with
  the whole tenancy working tree committed as one "as verified" commit.

Before that, `git checkout -- <file>` here reverted to a commit predating the
entire feature rather than to the last edit, and on 2026-08-28 it silently wiped
every phase-2 change in `worker/brainworker/graph/projection.py` — 25 tenant
references, `VersionNode.tenant_id` and `backfill_tenant`. It was recovered only
because the code is baked into `company-brain-worker:dev` and could be read back
out with `docker exec … cat /usr/local/lib/python3.13/site-packages/…`. That was
luck standing in for a backup, and it is the reason the first act of the next
piece of work was a commit.

`docaget/cache/embed/` is ignored: 1538 raw float32 blobs, regenerable.
`cache/correct/` stays tracked beside it — a correction costs generation tokens
and its cache is what survives an interrupted run.

## What is not built yet

Verified against the code on 2026-08-20, again on 2026-08-21, and again against
the running stores on 2026-08-28, not remembered.
Ordered by what it costs to leave alone. Details and measurements live in
`doc/COMPANY_BRAIN.md`.

**The eval-set stage is built, and nothing has run it against Vertex yet.**
It was "a switch with no activity behind it" until 2026-08-31. `build_evalset`,
`evaluate_index` and `persist_profile_scores` now exist in `activities/paid.py`,
the `evalset` and `scores` artifact kinds are written, `/runs/{id}` and the
Library's version rows render the figures, and the engine's own tail —
`build_evalset`, `measure`, `noise_floor`, `noise_margin` — is reached through
`docagent/runner.py` rather than reimplemented. Tuning came with it, bounded to
one chunking candidate.

**It has now run against Vertex, once.** `taller-de-tarsis.txt` — a synthetic
4,629-character document written for the purpose, 8 chunks, in the otherwise
empty legacy tenant as `lib_pruebas`. **The first measured recall this product
has ever produced for any document:**

```
recall@1 0.75 · recall@5 1.000 · MRR@10 0.875
dense-only 1.000 · noise floor 0.5149 · 8 questions, 0 misses · margin ±0.078
```

Two things that run confirmed and one it broke. The eval-set generator
**paraphrases rather than copies** — "¿Qué requisito debía cumplir un escriba
según la pauta epistolar formulada en Quíos en el 390?" for a passage that says
"la regla de los tres lectores… hacia el año 390" — which is why dense-only
equals hybrid and the leakage note reads "no sign". And the measurement's scope
reached every search, including the noise floor: `{tenant_id, version_id}`, in
the artifact, checked.

**What it broke was the estimate, and in the forbidden direction.** Quoted
$0.0338, billed **$0.0650**. Input over-reported by 2.6x, which is fine; output
under-reported by **3.6x** — 968 tokens per call against 270 quoted — and since
output is priced at five times input the whole quote came in at half the bill.
`EVALSET_OUTPUT_PER_CALL` was a guess at the length of a question and is now
400, measured; the input side takes the document's own average chunk instead of
the 2,600-character cap. Re-quoted at a free gate afterwards: **$0.0848 against
the $0.0650 spent, 1.31x over**, which is the direction the rule demands.
`test_the_eval_set_estimate_covers_what_the_first_real_run_billed` holds both
ends. One document, eight calls; a second may move it again.

The three decisions below are therefore *unblocked*, and still unsettled — one
synthetic document is not a corpus:

- **Gleaning stays off against its own measurement.** One pass buys +52% claims
  and +40% concepts for +70% cost. There is now something that could score
  whether the extra 41 claims are better; nobody has scored them.
- **`thinking_for("answering")` was settled on caution** because there was
  nothing to measure with. There is now.
- **Profile quality rests on a structural outcome rather than recall** — the
  entry directly below. A profile's `scores` are written back into it now, so
  the family accumulates measurement across documents; no family has any yet.

**And the sample size is a real limit, not a detail.** `EVAL_SAMPLE` is 40, and
with σ ≈ 0.358 the bootstrap margin needs about 80 questions to resolve a +0.040
MRR effect. So a tuning round at 40 measures the index honestly and refuses
almost every candidate — which is the design working, and is also a full second
embedding pass spent on a question the sample cannot answer. Turning `tune` on
therefore raises the sample to `EVAL_SAMPLE_TUNING` and the gate prices both
halves.

**Profile quality is unmeasured.** Learning works and is verified end to end —
a real document went from 0 detected chapters to a 9-section, two-level table of
contents for $0.0043 — but "the profile improved the index" rests on the
structural outcome, not on recall. The eval-set stage is still a switch with no
activity behind it, so there is nothing to score answer quality with. This is
the same reason `doc/CLAUDE.md` says structural diagnostics rather than recall
are what detect a bad profile. See `doc/COMPANY_BRAIN.md`, "Profiles: learned,
applied, and measured".

**Folder watching does not exist.** `source_folder` and its repository methods
are there; there is no scan workflow, no add/change/delete detection, and
nothing ever calls `Catalog.mark_absent`. Its delete detection now has somewhere
to point: permanent removal exists.

**Citations do not open anything.** They carry a byte-exact locator, and the plan
asks for opening the original source at the cited location. Nothing does. A
claim's verified `quote` now supplies the other half — a span inside the chunk
rather than the whole chunk — but it indexes the *corrected* stream, so pointing
at the original PDF still needs a mapping nothing computes.

**Nothing records which profile a run used, so the estimate cannot learn per
family.** `run` has no profile column and only 1 of 37 runs left a `profile.json`
artifact, so the question "do document families explain the 26% spread in semantic
output?" cannot even be asked of the data — checked on 2026-08-21 before building
the per-family figure this repository had been recommending to itself. The
enabling step is one column written where the profile is resolved; until it
exists, "per-family would be tighter" is a hypothesis, and `OUTPUT_SPREAD` is what
the corpus actually supports.

**214 orphan `Claim` nodes are still in the graph** — counted again on
2026-08-28, still 214. Left by a removal path that
predates the current one — `_DELETE_VERSION` deletes claims before chunks, so
nothing live produces them. Until 2026-08-21 all 214 were *visible*, across 155
concepts including "Jesús" and "Pablo": `claims_about_concept` matched on `ABOUT`
alone, so each surfaced with a `source_chunk_id` the UI offers as "check the
source" and which resolves to nothing. That code is fixed — the template requires
the chunk, which is how `claims_for_chunks` always reached them — which is why
this sits here and not in the defect list below. What remains is debris in real
data, and deleting it is a decision rather than a change.

**The OS keychain holds the session; the provider half has nothing to hold.**
`app/src-tauri/src/keychain.rs` stores secrets in Secret Service / Keychain
Services / Credential Manager and falls back to the `0600` file the app used
before, reporting which one answered rather than inferring it. The PKCE session
moved there on 2026-08-28, migrating `session.json` on the next write and
deleting it — the entry is keyed by a digest of the app data directory, because
a bare `"session"` key is per-user and not per-install, so two installs would
sign each other out and the test suite clobbered the developer's own entry
before doing exactly that.

The provider half is **not pending, it is empty**, and that was checked rather
than assumed: `ensure_secrets_file` creates `secrets.env` and *nothing in the
codebase ever writes to it*. Vertex has no key at all — it uses ADC, a mounted
file — so the comment in that function describes a pipe with nothing in it. The
day a provider with a key exists, it is two `keychain::Entry` calls and the file
is materialised from the entry at launch.

**`secret_store` is reported and rendered nowhere.** `BackendInfo` carries
`"keychain"` or `"file"` — a token rather than prose, so the wording can live in
the two i18n bundles the way an error `kind` does. No screen reads it and neither
bundle has the key, so the docstring's claim that "the UI can tell the user" is
currently false. It is one key per bundle and one line on the Services screen.

**The Ask history is persisted; nothing else in the UI is.** It goes to
localStorage beside the selected library, entries capped at 20, guarded the same
way — some webview configurations make `localStorage` throw outright, and a
failure there has to degrade to "nothing remembered" rather than to a blank
screen. A question still in flight is persisted with its `question_id`, so
relaunching resumes collecting it instead of paying for it twice.

**Nobody has clicked the Library screen's new verbs, or the new shell.** The
endpoints behind Reindexar, Reconstruir and Eliminar were each exercised end to
end against the running stack — Eliminar and Reconstruir again on 2026-08-21,
the first through the measurement's own teardown (16 Qdrant points, 79 claims and
30 orphan concepts collected, the 27 concepts other documents share left alone)
and the second on a pre-change artifact — and `tsc`, `vitest`, `vite build` and
`cargo test --release` pass. But the buttons themselves have not been pressed in
the window. The same still goes for the sidebar shell and Ask's three columns: 35
tests render them, and nothing has *looked* at them. Two things have been looked
at since, both by the throwaway-vitest-plus-headless-Chrome recipe below: the
claims panel with its status badges, quotes and concept description, and the
gate's estimate table — where looking caught two defects `vitest` cannot see, a
`white-space: nowrap` that pushed the table past a 520px viewport and a range that
broke with its dash alone on a line. What tests cannot see is
what is left — three-column sizing at a real window width, the stacked layout
below 60rem, and dark mode.

**Three things had never been touched by a person as of 2026-08-28** — the
backend switch on the Services screen, the hosted-UI sign-in, and the upload
path on the Import screen — and **one of them was on 2026-08-30**. A file was
chosen in a real window through the new chooser, and the Import screen rendered
its name and path; pressing Preview reached the paid plane and came back
`409 tenant_scope_pending`, which the error panel rendered with its guidance.
So the chooser, the staging call and the error path are exercised. The upload
has still never *succeeded* from the window, because the account driving it
belongs to the legacy organisation and that is precisely the case the paid
plane refuses — see the defect below on that kind carrying two meanings.

**The backend switch is still unwitnessed.** The library picker's refetch on a
change of plane (`lib/backend.tsx`) is covered by
`lib/backend.test.tsx`, whose three switching tests fail when the fix is
removed — checked by removing it — and nobody has yet watched the picker change
from `lib_teologia · 72 documentos` to another plane's shelf in a window.

**Drag and drop is in the same position, and cannot leave it by testing.** The
handler is bound to Tauri's own webview event, which neither jsdom nor a static
screenshot can fire — the same limitation as the graph's wheel handler. What
`ImportScreen.choose.test.tsx` can assert, and does, is that the screen renders
whole when the subscription fails.

**The sign-in is the one that matters, because nothing has exercised its actual
mechanism.** The PKCE flow has tests for the RFC 7636 vector, for `state`
validation and for the shape of the authorization URL, and it has never opened a
browser. The loopback listener on port 8789, the redirect coming back, the code
exchange against the real hosted UI: none of that has run. Every token used to
verify the paid plane so far was minted with
`aws cognito-idp admin-initiate-auth`, which goes nowhere near it. So "login
works" is currently a claim about three functions, not about signing in.

One data point arrived on 2026-08-30 and is deliberately not called a
verification: a user created with `admin-create-user` moved from
`FORCE_CHANGE_PASSWORD` to `CONFIRMED`, which means somebody completed a
password change in Cognito. Whether that went through this app's loopback
listener or through the hosted UI opened by hand is not recorded anywhere, and
the difference is the whole question.

**Inicio and the library graph are in the same position as of 2026-08-25.** Both
endpoints were exercised against the running stack — `/project-summary` with
Memgraph deliberately stopped, to see the degradation rather than assume it, and
`/libraries/{id}/graph` at three thresholds, whose row counts match the figures
measured straight out of Bolt. 45 tests render the two screens and both were
screenshotted at 1440 and 900 in both themes. **The buttons have not been pressed
in a real window**: pan and wheel-zoom in particular are pointer behaviour that
neither jsdom nor a static screenshot can exercise, and the wheel handler is
bound imperatively with `{ passive: false }` precisely because React's `onWheel`
cannot `preventDefault` — if that binding is wrong, the page scrolls instead of
the canvas zooming, and nothing in the suite would say so.

**That last sentence turned out to be describing a live defect, and it was found
by reading rather than by pressing.** The binding *was* wrong: the effect
depended on `[zoomBy]`, whose identity never changed, so it ran once at mount —
when the `<svg>` had not been rendered — and never again. **The wheel had never
zoomed this canvas.** It is a callback ref now, which cannot be scheduled before
its node exists, and it zooms towards the pointer rather than the centre. The
test for it is the first one the wheel has ever had, and it fails against both
the old binding and a centred zoom.

**Three questions about the library graph are still open, and all three need the
window.** As of 2026-09-01 the screen has been rebuilt — one envelope in memory,
five layouts from a worker, edges on a canvas, an imperative drag — and 348 tests
plus a screenshot pass at 1440 and 900 in both themes cover what they can. What
they cannot:
1. **Whether 12,626 SVG nodes pan at 60 fps in WebKitGTK.** The edges left the
   DOM, which removes 17,814 elements, and a drag is now one attribute write per
   frame — but ~13,000 elements in one SVG is still an unmeasured amount for that
   engine. The contingency is designed and not built: draw the concepts below
   the current band on the canvas too, keep SVG for the books, the top band and
   anything selected, and hit-test through a uniform grid over the atlas.
2. **The edge colour and alpha in both themes.** A canvas cannot resolve a
   custom property, so the tokens are read with `getComputedStyle` and re-read on
   a `prefers-color-scheme` change — and a static `innerHTML` dump has no canvas
   pixels, so the screenshot pass cannot see the result. A missing token paints
   nothing rather than a plausible grey, deliberately, so the failure is loud
   when somebody does look.
3. **Whether the wheel fix works with a real wheel**, and whether the 300 ms
   tween reads as a movement rather than a shuffle at the real 40-48 px.

And the caveat that makes this awkward: the free plane serves the **legacy**
tenant, which since 2026-08-31 holds only `lib_pruebas` — 2 documents, 1 indexed
version. The 73-book corpus is in `preprod`, reachable only through the paid
plane. So the volume questions above need the cloud plane signed in; anything
else is a synthetic payload, and should be reported as one.

**The Graph screen is the exception, and how it was looked at is worth
copying.** Its redesign on 2026-08-21 was rendered to static HTML from a
throwaway vitest file (`render` plus `document.body.innerHTML`, the stylesheet
inlined, the theme's own `:root` token block re-applied because
`prefers-color-scheme` answers the OS and headless Chrome will not fake it), then
screenshotted with `google-chrome-stable --headless --screenshot` at four widths
in both themes. Three defects came out of that and none of them was visible to
`vitest`: the legend's swatches painted SVG-default black because they lacked
the `shape` class the fill rules key on; edges at `opacity: 0.35` washed out on
a white background; and `.graph-layout`'s two-column rule sat *below* the 76rem
media query in the file, so at equal specificity it won and the layout stayed
two-column down to a 96px canvas. A media query is not a stronger rule, only a
conditional one — `.explore-panes` and `.ask-columns` work solely because they
are declared above theirs. Anything new added to that query's selector list must
be too, or carry its own query below its own rule.

**The same recipe was run on 2026-08-25 for Inicio and the library graph, and
found three more.** Books on the overview painted **white on white**: the
`--graph-doc-*` tokens read as paper on a 140px card and as a *hole* on an 18px
mark, so every hub had a gap at its centre where the book should be — they use
the centre tokens now, which is also why dark mode inverts correctly rather than
staying light-mode blue. The filter checkboxes had borrowed the document view's
legend strings and read "Document sharing concepts". And the restructure had
silently dropped the legend, so nothing explained size or thickness; it is back,
rendering `DocumentGraph`'s own exported `Swatch` so one stylesheet moves both —
with its own wording, because the same four marks mean different things here
(`size` is a degree, not a mention count). Two harness lessons worth reusing: the
shell is a full-height grid with its own scroller, so a shot file must override
`.app{height:auto}` and `main.content{overflow:visible}` or everything below the
fold is cropped out of the screenshot; and headless Chrome does not default to
light, so pass `--blink-settings=preferredColorScheme=1` and inject the dark
tokens by hand for the dark shots.

**Two more, from the 2026-09-01 run on the rebuilt library graph — and both were
defects in the shot file rather than in the screen, which is its own lesson: a
harness that renders the page wrong will happily tell you the page is wrong.**
`.app` is a grid of `15rem 1fr`, so with no sidebar in the dump `main` lands in
the *first* column and the whole screen renders 225 px wide; the file has to
override `grid-template-columns` and not only `height`. And the dark token block
already contains its own `:root`, so wrapping it in another produces
`:root { :root { … } }` — invalid, silently ignored, and **the dark shots come
out pixel-identical to the light ones**. That one is worth naming because the
failure looks like a pass.

A third thing the same run settled, and the only way to settle it in a static
dump: **tint the canvas.** A `<canvas>` has no pixels in an `innerHTML` dump, so
its stacking cannot be seen — give `.graph-edges` a translucent background in the
shot file only, and if the nodes are visible over the wash the canvas is
underneath. A flat rectangle with nothing on it is the failure.

One measurement the screenshots produced and nobody has acted on: at the
narrowest two-column width the media query allows (1216px, since `rem` in a
media query resolves against the *initial* 16px root size and not the
stylesheet's 15px), the canvas is about 630px wide, so its 11px labels render at
roughly 7.9px. Legible, but only just. Collapsing the graph at 90rem instead
would end that band; 76rem was chosen deliberately to match the other screens.

**The component test stack exists as of 2026-08-21.** `@testing-library/react`
and `jsdom` are installed and `vite.config.ts` sets `environment: "jsdom"` —
which is why its `defineConfig` now comes from `vitest/config` rather than
`vite`, since `tsconfig.json` includes that file and vite's own config type has
no `test` key. `App.test.tsx`, `AskScreen.test.tsx`, `HomeScreen.test.tsx` and
the two under `screens/graph/` render components; `askSession.test.ts`,
`radial.test.ts` and `force.test.ts` test the Ask reducer and the two graph
layouts' arithmetic as plain functions. It paid for itself on its
first run: `content.current?.scrollTo({top: 0})` throws in jsdom, so the shell's
scroll reset assigns `scrollTop` instead. **jsdom implements no scrolling at
all**, and that has now bitten twice: `scrollIntoView` is simply *absent* there
rather than throwing, so the Graph screen calls it optionally
(`detail.current?.scrollIntoView?.(…)`) and a test asserts the guard by asserting
the click does not throw. Anything that scrolls needs the same treatment.

**`cleanup()` runs between tests but not inside one.** Vitest exposes no global
`afterEach` here, so RTL never registers its automatic cleanup and each test file
calls it by hand — `AskScreen.test.tsx` explains that where it does it. What that
does *not* cover is a single test that renders twice: `screen` queries the whole
document and finds both trees, which surfaces as "found multiple elements" or as
an assertion passing against the previous render. Assert through each render's
own `container` there; `ImportScreen.test.tsx` does.

**Where a screen's geometry lives is a testability decision, not a tidiness
one.** jsdom implements no SVG layout — no `getBBox`, no resolved `transform` —
so a rendered test can assert that a node exists and carries the attributes it
was given, and nothing at all about whether two labels overlap. That is why the
graph's arithmetic lives in `lib/radial.ts` and `lib/force.ts` rather than in
module constants inside the components: `radial.test.ts` asserts the properties
that matter for the document view (no concept label meets a document card, no
label meets another, the two rings are offset by half the outer step so no
document sits on a concept's ray) directly on the numbers, at the 12-and-8 counts
that actually ship. Label footprints are estimated from a character count at the
stylesheet's font size, which is enough when the question is whether two boxes
are nowhere near each other.

**`force.ts` is the same decision applied to a simulation, and it is seeded for
exactly that reason.** A force-directed layout that started from `Math.random`
could be asserted on only for not throwing. Each node's starting point comes from
a hash of its own id, and the bodies are sorted by id before the first tick —
because float addition is not associative, so visiting the same nodes in a
different order lands them tens of pixels apart and "same library, same picture"
would be nearly true rather than true. With that, `force.test.ts` asserts what a
rendered test cannot: no two nodes overlap, everything lands inside the viewBox
after `fit()`, connected pairs end closer than unconnected ones on average, and
an empty graph produces no NaN. It also runs the real library's shape — 67 books
and 1,719 concepts — to keep the quadratic version from coming back: repulsion
goes through a uniform grid, and the whole settle takes about 0.4 s.

**The library graph's rewrite added six more modules on the same principle**, and
the split is worth knowing before looking for a behaviour in the component:
`graphModel.ts` is the envelope as typed arrays plus every derivation over it
(the degree filter, the adjacency CSR, the histogram, the search index, the
composed view filters); `graphAtlas.ts` is the chain of layouts and its message
protocol, and imports no React because **the worker imports it**;
`graphAtlas.worker.ts` is a message pump with no logic in it, because jsdom
defines no `Worker` and it is the one file the suite cannot execute;
`viewport.ts` is the arithmetic of looking — `fitBox`, `clientToView`, `zoomAt`,
`panBy` — which exists because jsdom implements neither `getScreenCTM` nor
`createSVGPoint` and returns zeros from `getBoundingClientRect`, so a zoom that
read the pointer through the SVG's own matrix could not be asserted at all;
`graphPainter.ts` draws the edges through an **injected** context, because
jsdom's `getContext("2d")` returns `null` and that makes the null case a test
rather than a crash; and `tween.ts` is the interpolation, whose NaN rules a CSS
transition cannot express. The hooks that touch the DOM — `useAtlas`,
`useEdgeCanvas`, `useLayoutTween`, `libraryGraphStore` — hold no decisions worth
asserting and are the thin part on purpose.

The two older suites still scan *source text* — `i18n.test.ts` for unused keys,
`LibraryScreen.test.ts` for the property that a permanent removal cannot fire
without passing through the confirm branch. They are no longer the only option,
and the caveat `LibraryScreen.test.ts` writes about itself (it cannot catch a
confirm panel that renders with dead buttons) is now fixable rather than
inherent. Rewriting them is separate work nobody has done.

**Smaller:** OCR raises `NotImplementedError` on purpose in
`providers/adapter.py` (it needs its own spend gate); the second approval gate
returns correction *counts* rather than a diff, so the UI cannot show what
changed; DOCX, PPTX, XLSX and CSV have never been run end to end (PDF and TXT
have), and the owner has deprioritised checking them.

### The audit trail: what a run did, and how it is kept

Added 2026-09-01. `run.stage` answers "where is this now" and destroys the answer
to "what did it do", because it is one column each transition overwrites — and
**eight of `IngestWorkflow`'s stages never reached it at all**, so the free half
of the pipeline left no trace once a run ended. `cost_entry.created_at` was the
only per-stage timestamp that survived, for the eight stages that spend and no
others. The product could bill $10 across sixteen stages and afterwards answer
exactly one question about it: the aggregate.

- **`run_event` is one row per *transition*, not a start row and an end row.** A
  stage's duration is the next row's `at` minus its own, and the terminal row —
  the only one carrying an `outcome` — closes the last stage. That makes "started
  and never ended" unrepresentable, which matters because it is exactly the shape
  a crashed run would leave and exactly what would be indistinguishable from a
  stage still working. The terminal row names the stage the run was *in*:
  "it failed" is half an answer and "it failed in `semantics`" is the whole one.
- **Idempotency lives in the workflow, not in the database.** `seq` is a counter
  on the workflow object and `at` is `workflow.now()`, so a retried transition
  carries the number *and the timestamp* it carried the first time and
  `ON CONFLICT (run_id, seq) DO NOTHING` drops it. `now()` in SQL would move
  under a retry and silently stretch the previous stage's measured duration.
- **The write rides along with the one that sets `run.stage`**, in one
  connection. Two activities would let a retry land one without the other and
  leave the trail disagreeing with the cursor about the same moment.
- **`staging` and `registering` precede the `run` row**, and `_insert_event`
  derives its tenant from that row — so an event written before it is *silently
  dropped*. The workflow buffers those two and flushes them once
  `register_document` returns. Nothing else in the pipeline needs this, because
  everything from `extracting` onward already depends on the catalog.
- **Removals and activations get run rows now** (`brainworker/audit.py`), which
  needed `run_kind_check` widened exactly as `20260831160000_run_kind_ask` did
  for questions: `record_cost` derives its tenant from the run, so no run row
  means no bookkeeping of any kind. Those two are not workflows, so their `seq`
  and `at` are taken locally — there is no replay to be deterministic for — and
  every write is best-effort, because a removal refused by a blinking catalog is
  worse than one that failed to record itself.
- **The raw Temporal history is a second, separate route**, and it is the only
  source that shows what no application code recorded — the heartbeat timeout, the
  closed attempt, the second identical pass. It is fetched only when a person
  expands the panel, and **`available: false` is not an empty list**: "the history
  aged out" and "this run did nothing" must not render the same, which is
  `/project-summary`'s own rule applied to a leg that expires rather than one
  that is down.
- **`event_type` on a `HistoryEvent` is a plain `int`.** `getattr(t, "name",
  str(t))` reads as careful and silently yields `"3"`, which matches nothing in
  the allowlist, so the whole history filters to an empty list and the route
  reports a run that did nothing. Found by translating a *real* history in
  `test_the_raw_history_translates_against_a_real_temporal`; a hand-built double
  would have agreed with the assumption. Use `EventType.Name(...)`.

### Auditing the index a run left behind

The trail above answers "what did this run do". `worker/scripts/audit_version.py`
answers the question underneath it — **is the index still coherent with its own
artifacts** — for any `ver_…`, in five legs, writing nothing. Added 2026-09-02;
the measurements it produced are in `doc/COMPANY_BRAIN.md`.

- **The comparators are pure and live in `brainworker/auditversion.py`.** The
  same testability decision `radial.ts` and `force.ts` embody on the other side
  of the product: what is worth asserting is the comparison, and a test that
  needed a Postgres, a Memgraph and a Qdrant standing up to check a set
  difference would run rarely enough to be worth nothing. 34 tests, no store.
- **It imports every identity rather than deriving one.** A point id, a payload
  scope and a `claim_id` are the things under test, so an audit that computed
  them itself could only confirm its own arithmetic. `version_scope`,
  `graph.schema`, `docagent.qdrant.point_id`, `auditlog.build`,
  `stages.COST_STAGES` and `projection._CANDIDATE_CONCEPTS` are all reused —
  the last one deliberately, so a fourth route to a concept added there and not
  here is loud rather than silent.
- **The Cypher is server-owned literals, guarded.** `Graph.write` is the raw
  route this codebase already uses for server-owned *reads* (`projection.py`
  reads its removal candidates through it), and Memgraph would not have enforced
  `default_access_mode="READ"` anyway. What makes it safe is that the statement
  is a literal here; `assert_read_only` refuses one that acquires a write clause,
  matched on word boundaries because a guard that flags `OFFSET` for `SET` is one
  somebody turns off.
- **The stale check is a set difference, not an estimate.** A run's own
  `semantics.json` names exactly what it produced, so `left_behind` is
  computable — and `missing` is reported beside it, because something the run
  made and the graph lacks is a projection that did not finish, which no count of
  the graph alone can see.
- **Every leg carries its own `available`.** `/project-summary`'s rule per leg:
  a stopped Memgraph renders as "could not ask", naming the URL it tried, while
  artifacts and the ledger still answer. A leg that could not answer carries no
  figures at all, so one cannot be quoted by accident.
- **`--measure` is the only thing that can spend, and usually does not.**
  Measured on the first real audit: **0 embedding tokens**, because the
  `evaluating` stage had already embedded those same questions under that same
  model and `docagent.embedcache` keys on (model, width, task, text). Reported
  rather than assumed — a non-zero figure there is real spend.

**What it found first is that a version's bill is not one run's bill.** Grouping
`cost_entry` by stage *across* a version's runs is what makes a stage charged in
more than one of them visible, and on `ver_0cde0e3196d06e4259a32a52` the eval set
was generated twice for **$0.5753 + $0.5710**, the first of them inside a run that
was cancelled. **$1.1965 of that document's $3.7572 — 31.8% — bought nothing**,
and the index that exists cost $2.5607. Nothing in the code is wrong about that;
cancelling costs what it costs. There was simply no way to see it, and no screen
shows it yet.

`Qdrant.scroll(filters)` was added to the engine for this: `scroll_all` returns
payloads and drops the **id**, and the id is the only thing that can say whether
a longer previous chunking left a tail behind — a stale point's payload is
perfectly well-formed.

### One stage, three spellings

`brainworker/stages.py` and its fork `src/runs/stages.ts` exist because the
audit view has to join three unrelated sets of string literals: what the
workflow calls a stage (`learning`, `correcting`, `evaluating`), what a *charge*
calls it (`profile`, `correction`, `evalset`+`evaluation`), and what an
*artifact* calls it (`proposal`, `corrected_text`, `scores_candidate`). The
overlap between the first two is three words.

**Nothing was renamed**, and that is the decision: `learning` is in the `stage`
column of every run this installation has done and `correction` is in every
`cost_entry` row. The mapping is the fix. `stages.parity.spec.ts` dumps the
Python module live at test time — never a committed fixture, for the reason
`queries.parity.spec.ts` records about its own going stale.

A charge that maps to no stage is **not dropped**: the three a question makes
belong to no pipeline stage, so they land in a trailing `stage: null` group. That
is what keeps the ledger's totals equal to `total_cost` rather than quietly less
than the bill.

### The import queue needs no client persistence, and that is not an accident

Enqueuing *is* starting the workflow: the free stages cost nothing and the gate
is where money is decided, so every queued item is a `run` row from the moment it
exists. There is nothing to keep in `localStorage`, nothing to reconcile, and
nothing that can drift — a relaunch, a crash or another machine all see the same
queue. That is what let a drop of five books become five imports, each parked at
its own gate for up to seven days, replacing a screen that kept `[first]` and
counted the rest as "ignored".

`ImportScreen` used to hold one run in `useState` and forget it on approval; a
window reload, a plane switch or a second import silently dropped it, and after
approving there was no feedback at all.

### The library graph reads from memory now, and what that cost to get right

Rewritten 2026-09-01. The degree control was a server filter, so every change was
a round trip *and* a 320-tick force simulation run synchronously inside a
`useMemo` during render. Measured on the real corpus (73 books, `lib_teologia`):
209 ms of layout at the default threshold, 497 ms at ≥ 2, **3,859 ms at ≥ 1**.
Measured against the same data once it is in memory: **deriving a threshold's
whole view is 0.88 ms.** The work was never the filtering.

**The degree filter is an equality, not an approximation.** `library_mentions`
computes `documents = count(v)` in its second `WITH` and applies
`WHERE documents >= $min_documents` only afterwards, so a concept's degree does
not depend on the threshold it was fetched at. For one
`(library, tenant, confidence_floor)` the server's answer at k is exactly the
`min_documents = 1` envelope's rows with `documents >= k`.
`graphModel.test.ts` reimplements the route's own folding and asserts it, rather
than asserting the implementation against itself.
It survives truncation too, which is not obvious: the `ORDER BY` begins with
`documents DESC`, **the same key the filter uses**, so a threshold's rows are a
prefix of the ordering and `LIMIT` cuts the same tail from both. Reordering that
clause to `mentions DESC, documents DESC` would break this in silence, which is
why a test names the reason. What does *not* survive is the flag:
`truncated.edges` is derived per threshold, never copied, or a whole picture
gets "recortado" printed over it.

**The confidence floor stays a server filter, and that is not laziness.** A
concept's degree is counted *after* the floor predicate, so no client can
re-derive it from an envelope fetched at another floor. Each floor gets its own
entry in the RAM store instead; the second visit to one costs nothing.

**The envelope is 3.48 MB and it does not go to localStorage.** Measured:
17,814 rows, 183 ms end to end, `JSON.parse` 10.5 ms, and about **3.7 MB net per
entry** once the response objects are read into typed arrays and dropped — which
is where 2.5 MB of uninterned edge id strings goes. `libraryGraphStore.ts` holds
four (`FLOORS` offers five; four is "the floor you are on plus the three you
tried"). It is a cache because refetching is *wasteful*, not because it is
expensive, which is the opposite of why the Ask history is persisted.

**A module, not a context, and the plane is in the key unconditionally.**
`App.tsx` remounts every screen on a plane change, so a provider mounted inside
that key would die exactly when the cache is most valuable. A module survives the
remount — that is the feature — and the identity in the key is what stops it
being a leak between organisations. It deliberately does **not** go through
`scopedKey`: that helper leaves the local plane's key bare to avoid orphaning
localStorage entries written before scoping existed, and a cache has nothing to
orphan.

**The layouts are a chain, not five independent runs.** `settle`'s
`k = SPACING * sqrt(W*H/n)` scales with the node count, so two runs over
different subsets of one library are globally different pictures. Measured on the
real degree distribution, both fitted to the same canvas: **a threshold change
moved every surviving node 162 px of a 1,452 px diagonal — 11%.** That is a
shuffle, not a filter, and no easing hides it; aligning the two layouts with the
best rotation and scale only reached 110 px, so they differ structurally. Nor
does one shared fit help — it made it *worse*, 214 px, because the survivors
occupy a different fraction of the field at every threshold.

So `graphAtlas.ts` computes them as a ladder, each seeded from the last, and
`settle` grew an additive `start`/`heat` for it (every existing `force.test.ts`
assertion holds when they are absent):

| | | measured |
|---|---|---|
| 1 | the default threshold, cold | 226 ms — this is the first picture |
| 2 | the base at the widest threshold, seeded from it | 5,531 ms |
| 3 | every other threshold, relaxed from the base, 60 ticks | 141 ms for four |

**40-48 px between thresholds instead of 162**, in 5.9 s of worker rather than
7.5. And the relaxation is legibility, not polish: filtering the base without it
leaves a mean nearest-neighbour distance of **6 px against 16 px**, because
high-degree concepts cluster in the middle, so a naive filter gives a clot rather
than a map. The default view is emitted twice — cold at 226 ms and again once the
base exists — which moves it 63 px, once, early, and is the price of it belonging
to the same chain as everything else.

**jsdom defines no `Worker`, so the inline path is the tested one.**
`graphAtlas.worker.ts` is a message pump with no logic in it for exactly that
reason; `atlasSteps` is a generator so the worker runs it to completion and a
host without one steps it between timeouts. The worst case is the behaviour that
shipped before any of this, staged and reporting progress, never a blank canvas.
**The CSP question is settled by measurement, not argument**: `vite build` emits
`graphAtlas.worker-*.js` as its own 4.5 kB file referenced by URL, and `grep
blob:` over the bundle returns zero — so `default-src 'self'` with no
`worker-src` permits it by fallback.

**Edges are painted, nodes are not.** At the widest threshold that is 17,814
`<line>` elements gone from a single SVG. They carry no click, no hover and no
accessible name, so the accessibility tree loses nothing and every DOM assertion
still reads the nodes. Two things this cost:
- **`lineWidth` is context state**, so a width per edge forces one `stroke()`
  per edge. Quantised into six buckets it is ≤6 calls, and ≤12 with a selection
  (faded pass first, lit second — the paint-order contract as a draw order).
- **`scale(value, max, min, span)` in `radial.ts` takes a *span*.** The call the
  component made, `scale(e.mentions, maxEdge, 0.6, 2.4)`, draws widths from 0.6
  to **3.0**. Reading that argument as an upper bound is a mistake worth making
  only once; the constant is named `SPAN` now.

**The stage, and the trap under it.** An absolutely positioned element paints in
step 8 of the painting algorithm and a static in-flow one in step 4, so
positioning *only* the canvas puts it above the nodes and the picture becomes a
flat wash. Both are positioned, and the ground, border and gesture cursor moved
to `.graph-stage` because an opaque background on the SVG would hide the canvas
under it. A test asserts the DOM shape the stylesheet selects and **fails if the
canvas is placed after the SVG**.
And the near-miss worth keeping: `DocumentGraph` still hangs its SVG straight off
`.graph-main`, so narrowing that rule to `.graph-main > .graph-stage >
.graph-canvas` would have taken its canvas down to a replaced element's default
300x150 — silently, because nothing in this project can see it. The stylesheet
serves both shapes.

**Continuous interactions never go through React; discrete ones may.** A drag
fires a pointermove per frame and each `setPan` re-executed the render function
and reconciled every node, so the picture moved by rebuilding the tree that draws
it. A frame now costs one `setAttribute` on the group and one canvas repaint,
whatever the graph holds, and state is set once on release. Hover is the same
story: every `onMouseEnter` was a `setState`, so sweeping the pointer
re-rendered the screen once per node crossed — and during a drag, on top of the
pan. It is one delegated listener and **one** overlay `<text>` now, suppressed
while dragging.
**Zoom stays in state on purpose.** It is discrete, and each concept cancels the
scale on itself, which is a write per node a render already does correctly.
Making it imperative would mean moving the node radius into a CSS custom
property, and whether WebKitGTK resolves `r` from `calc()` is not something this
project can find out from a test.

**The selection's transition belongs to the small side.** It sat on the dim, so
selecting one node started an opacity animation on every element in the canvas at
once. The dim lands at once now and the lit nodes are what animate, of which
there are at most a few hundred — which also reads better, since attention should
snap to what was lit.

**And the tween's NaN rules follow from what the arrays mean.** A node the source
layout never placed has nowhere to travel *from*, so it appears at its
destination rather than flying in from the origin; a node the destination does
not place stays unplaced rather than sliding to a corner it was never in. A CSS
transition can express neither, which is why `tween.ts` is written by hand. It
runs in `useLayoutEffect`: React has already rendered the new coordinates by the
time an effect runs, and a passive one runs *after* paint, so the picture would
jump to the destination and then animate back from where it used to be.

### Three defects a screenshot found and no assertion could

The recipe in *The Graph screen is the exception* was run again on 2026-09-01 for
the queue and the ledger, and earned its keep a third time:

- A queue row read **"esperando aprobación · esperando aprobación"** — the state
  and the stage are the same word there — and **"completada · terminando"** for a
  finished run, because `activity.stage.*` is a present-continuous progress
  label. The stage now renders only while the run is going and only when it says
  something the state did not.
- The **kind badge stretched the full width of the row below 76rem**, reading as
  a text field: a stacked grid cell fills its `1fr` column without
  `justify-self: start`. jsdom lays out no grid, so nothing in the suite could
  see it.
- The ledger put the **charges toggle in the "Produced" column**, so a row read
  `semantics 1 cargo(s)` as though the charge were an artifact — and printed
  "sin precio" twice when every entry was unpriced.

All three are pinned by tests now (`ImportQueue.test.tsx`), but none of them
would have been written without looking.

**And the extraction that produced `GateReview.tsx` mangled twelve translation
keys**: a blanket `gate.profile` → `report.profile` rename hit the string
literals as well as the property accesses. The i18n dead-key test caught every
one. That test is not bookkeeping.

## Known defects, not yet fixed

Distinct from the list above: this is shipped code that is wrong, not features
that are missing. Each was found by running the thing, and each is recorded
rather than fixed because the fix is somebody's decision or sits in another
session's files.

- **`DocumentGraph` shows a label where it means a count.** `DocumentGraph.tsx:671`
  renders `t("graph.shared", { count: item.sharedConcepts })` under every outer
  document card. `graph.shared` is `"Concepts in"` / `"Conceptos en"` and carries
  no `{{count}}`, so the number is silently dropped and the card reads as a
  stray label. The key it wants is `explore.shared`, which is
  `"{{count}} shared"` / `"{{count}} compartidos"`. Found 2026-09-01 while
  retiring the two keys the degree `<select>` used, and **nearly made worse**:
  `graph.shared` was also that select's label, so it looked retired too.
  The lesson generalises past this one line — **the i18n scan sees a key nothing
  reads and is blind to a read with no key**, so deleting a key is not covered by
  the same test that stops you adding a dead one. `DocumentGraph` was out of
  scope for that session's work, which is why this is recorded rather than fixed.
- **`DocumentGraph`'s wheel listener is never attached, and its pan runs 1.9x
  fast.** Both are the same two defects the library view had, in the same shapes.
  The effect at `DocumentGraph.tsx:509-518` depends on `[zoomBy]`, whose identity
  never changes, so it runs once at mount — behind a guard that has not rendered
  the `<svg>` yet — finds a null ref and never runs again. And `pan`
  (`:519-523`) is added inside a group whose units are viewBox units while being
  fed raw client pixels, so a drag moves the graph about twice as far as the
  hand. The library view's fixes are a callback ref and a division by
  `fit.scale * zoom`, both in `app/src/lib/viewport.ts`, and they port directly.
  Left alone because that view was explicitly out of scope.
- **`CONCEPT_RADIUS = 9` does not mean 18 px, and `geometry.ts:4` says it does.**
  It is 18 *viewBox units*, which is 18 x the drawn scale: about 9.5 CSS px at
  the narrowest two-column width the layout allows and 15 px at 1440. Same root
  cause as the recorded 7.9px label defect, and the same one-line fix would close
  both — a `--graph-fit` custom property carrying the scale, with `font-size:
  calc(11px / var(--graph-fit))`. Not done, because it lands in the same place as
  the CSS-`r` question below and neither can be judged without the window.

- **`tenant_scope_pending` carries two opposite meanings, so its guidance is
  wrong for one of them.** The kind was minted for "this view is not segmented
  by organisation yet, so it answers only for the legacy one" — a refusal aimed
  at *other* tenants. The paid plane's `uploads.controller` reuses it for the
  reverse: `ctx.tenantId === LEGACY_TENANT_ID` is refused, because that
  organisation's corpus lives at the root of the volume rather than under
  `tenants/<id>/` and is written by the local plane directly. `GUIDANCE` in
  `app/src/lib/api.ts` keys on the kind alone, so a member of the legacy
  organisation is told the thing is "only available for the original one" —
  which is the organisation that was just refused. The server's own message is
  accurate; the advice layered over it is not, and the advice is read first.
  Found 2026-08-30 by importing a file with the paid plane selected. The fix is
  a distinct kind in the paid plane (`../yorch-tauri-backend`) with its own key
  in both i18n bundles; nothing in this checkout can do better than make the
  string vaguer.
- **`ports::revalidate` has no caller, so relaunching the app orphans its own
  containers.** `AppState::stack()` (`app/src-tauri/src/lib.rs:48`) always calls
  `ports::allocate(Ports::default())`. A running stack holds its ports, so the
  allocator skips them and hands back a *different* set, which `write_env` then
  writes to `infra/.env` — after which every Rust command addresses services
  that are not there. Observed 2026-08-20: `.env` naming 7788/6433/5532/8788
  while the containers were published on 7789/6435/5533/8787. `revalidate` exists
  for exactly this and `cargo` already reports it as dead code. The fix is to
  call it when the stack is down, which is the case its own doc comment
  describes.
- **One catalog, two workspaces, so `rebuild` fails for half the library.**
  `infra/.env` names `~/.local/share/io.sek.companybrain/workspace`; a stack
  started by hand from `infra/` gets compose's `./workspace` default. One
  Postgres holds `run_artifact` rows written under both, so a rebuild of a
  document indexed under the other one dies with `artifact is missing:
  runs/…/chunks.jsonl` while the file sits in the other tree. Observed
  2026-08-21 on `1.-Doctrina-del-Hombre`. Same root as the ports defect above —
  the app rewrites `.env` on launch and a hand-started stack need not agree with
  it. Artifact paths are workspace-relative by design, so copying the run
  directory across is a valid workaround and the sha256 verification proves it.
  **Made diagnosable rather than fixed**: `ArtifactStore.resolve` now names the
  workspace it looked under, and `can_rebuild` stats the file instead of trusting
  the catalog row — so the button is disabled with a reason rather than failing
  when pressed, which is what that endpoint's own docstring says it is for.
- **The containerised worker creates the workspace subdirectories as root.**
  `inbox/`, `runs/`, `cache/` and `profiles/` under the app's workspace end up
  owned by `root:root`, so the host user cannot write into them — which means
  host dev mode, the documented fast loop, cannot share a workspace with a stack
  that has already run. Verification on 2026-08-20 had to use a scratch
  workspace for this reason.
- **A citation's locator names the *version's* title, which can outlive the
  document that supplied it.** `_locator` (`graph/projection.py:354`) builds from
  `version.title`, set once at first projection. Two documents sharing one
  version therefore cite whichever title projected first, and removing that
  document leaves citations naming it until something re-projects. Re-projection
  does now correct it — the citation prune replaces the stale node rather than
  adding to it — so a rebuild is the workaround.
- **A rebuild's semantics stage is visibly free, and it should not be.**
  `activities/rebuild.py:213` builds a `Spend(stage="semantics-replay", …)` on
  the returned `Semantics` and **never calls `_charge`**, so no `cost_entry` row
  is written — despite the comment claiming it "keeps the stage visible in the
  run's ledger". Invisible until 2026-09-01, when the audit ledger started
  rendering a per-stage cost and a rebuild's `replaying semantics` row came back
  empty. The mapping is already in `stages.COST_STAGES`, so the fix is one
  `_charge` call; recorded rather than done because a replay's real cost is a
  question about whether re-projecting an artifact spends at all, and nobody has
  measured it.

- **The i18n dead-key scan matches on a prefix, so one key can hide another.**
  `isUsed` looks for the literal key text anywhere in the source, and
  `"import.startBatch"` contains `"import.start"` — so `import.start` survived
  as a dead key that the test reported as used. Found on 2026-09-01 and removed
  by hand. Any key that is a prefix of another has the same hole. The fix is to
  match on a word boundary, or to require the key to appear inside quotes.

- **`Cost.usd` is annotated `float | None` and holds `Decimal`.** The column is
  `numeric(12, 6)`, so psycopg returns `Decimal` and arithmetic against a float
  raises `TypeError: unsupported operand type(s) for -: 'float' and
  'decimal.Decimal'`. Harmless wherever it is only serialised; a trap the first
  time anything compares or sums one. `repo.py:116`.

- **The free plane's `/reindex` finds another organisation's document and then
  re-indexes it as the legacy one.** `reindex_document` looks the document up
  with `catalog.document(document_id, library_id=library_id)` — **no tenant**
  (`main.py:1259`) — and then builds an `IngestRequest` with
  `tenant_id=LEGACY_TENANT_ID`. Every *listing* took a required tenant when that
  hole was closed; this lookup has an id, so it was left alone, and an id is not
  authorization. The result is not a refusal but a silent cross-tenant write: the
  document's chunks, sections and citations would be projected into the legacy
  graph under unsalted ids, which is exactly the `VersionNode.tenant_id` failure
  already recorded above, reached from a different direction. Found 2026-08-31
  while re-indexing a `preprod` document; the run was started through Temporal
  with a hand-built request instead. `rebuild_document` is worth checking for the
  same shape. The paid plane is unaffected — it carries `@ActiveTenant()`.

- **`header_patterns` cannot do anything for this corpus, and the validator does
  not know that.** Only `extract/pdf_text.py` and `extract/pdf_ocr.py` call
  `rules.header_res()`; the plain extractor is contractually forbidden from
  touching the bytes, because `char_span` indexes the file itself — its docstring
  says so. Every document here arrives as a `.corrected.txt`, so **running
  headers are never stripped from any of them**. This is not cosmetic: the
  running headers are what make a level-2 heading pattern look undiscriminating,
  so the validator rejects it and the document is chunked with *no* sections at
  all. Measured 2026-08-31 on `01_RetoDeDios_INT-S.pdf.corrected.txt`: the
  rejected pattern matched 22.6% of paragraphs, and of its 325 hits **203 were
  real sub-headings appearing exactly once** — the book has ~190 sections and got
  0. Worked around in that document's profile by excluding the 28 repeated
  heading-shaped lines inside `heading_l2_pattern` itself, which is the wrong
  place for it: it is per-document text living in a rule keyed by a *structural*
  fingerprint, so it travels to any document that shares one. The fix is either a
  plain extractor that can strip without moving offsets, or a validator that
  discounts repeated lines before judging a pattern.

- **`_topical_overlap` reports 0.0 for every plain-text document, and 0.0 is the
  value that means "most dangerous".** It compares the current document's
  `repeated_lines` and `first_lines` against the *filename stem* of the profile's
  `learned_from` (`activities/ingest.py:1251`). Only `pdf_text.py` ever fills
  those two evidence fields, so for a `.txt` both are empty, the "no basis to
  judge" branch returns 0.0 — and `ProfileWarning.similarity` documents a low
  value as the dangerous case: *same structure, unrelated subject matter, so the
  wrong heading rules get applied*. The branch's own comment says reporting 0.0
  "would assert 'unrelated', which is a stronger claim than the evidence
  supports", and then does it anyway. Measured 2026-08-31: a document re-indexed
  with **its own** profile raised a 0.0-similarity collision warning naming its
  own filename in `collides_with`. This is the same blind spot as the fixed
  "a document's own profile read as a structural collision" entry below, still
  open in the *warning* path, and it lands at the gate — the moment a person
  decides whether to spend, and where the advice it implies is `ignore_profile`,
  which throws the profile away and pays for a full run. The check is the one
  `build_evalset` and `n_load_profile` already make: `learned_from == source_key`.
  A "cannot tell" needs to be distinguishable from a measured zero, as
  `/project-summary` already does with `available`.

- **A re-index leaves the previous run's semantics in the graph, and they now
  name chunks whose text has moved.** The vector side prunes — `QdrantWriter.
  prune_tail` runs on every index — and `project_structure` prunes the citations
  of the chunks it just projected. Nothing prunes `Claim` nodes or `MENTIONS`
  edges: `project_claims` and `project_semantic_edges` MERGE, so a second
  extraction over a different cutting *adds*. Measured 2026-09-01 on
  `ver_0b71d21eeb3228f54437d9cf`, re-indexed with a corrected profile that took
  it from 600 chunks to 631: the graph held 5,001 claims, the run extracted
  3,055, and the total came to **8,043 — 13 converged and 4,988 (62%) were left
  behind**. `MENTIONS` the same way: 4,057 of 7,554 (53.7%) stale.
  This is not inert debris like the 214 orphan claims recorded above. `claim_id`
  is `f(chunk_id, text)` and `chunk_id` is `f(version_id, index)`, so re-chunking
  keeps every id *alive* while the text underneath it changes — the stale claims
  are attached to chunks that no longer contain the quote they carry, and they
  are indistinguishable from good ones at read time. That is precisely the state
  `Semantics.claims_verified` exists to keep visible, arrived at from a direction
  it does not cover: the quote was verified, against a chunk that has since been
  re-cut.
  **A rebuild has the same shape** — `replay_semantics` replays an artifact into
  the same MERGE — and is worth checking. The fix mirrors `remove_version`
  exactly and is four statements: take `_CANDIDATE_CONCEPTS` *before* deleting,
  delete the claims and `MENTIONS` this run did not produce, then
  `_COLLECT_ORPHAN_CONCEPTS` over the candidates. The set is computable with no
  guesswork, because the run's own `semantics.json` names exactly what it
  produced.
  **That measurement no longer reproduces, and the mechanism is untouched.**
  Re-measured 2026-09-02 on the same version by two independent routes — the
  `DERIVED_FROM` traversal and a property match on `source_chunk_id` — the graph
  holds **3,055 claims against the 3,055 the re-index produced, 0 left behind**,
  and 3,497 `MENTIONS` against 3,497. The 4,988 were deleted; **what deleted them
  is recorded nowhere**, and nothing in `projection.py` changed: the module's only
  claim `DELETE` is still `_DELETE_VERSION`, which removes a whole version. So the
  defect stands and its figure is historical — the next re-index of any version
  will rebuild it. Two `semantics.json` still sit side by side in that version's
  run directories (2,965 from the failed attempt, 3,055 from the re-index), which
  is what makes the arithmetic checkable at all.

- **A profile's `retrieval` block is measured, persisted, and never read by the
  thing it describes.** `answering/retrieve.py` uses module constants —
  `MIN_SCORE = 0.60` (line 37) and `PER_SECTION = 2` (line 44) — so the only
  reader of `profile.retrieval` is `evaluate_index`. Per-family retrieval tuning
  is therefore a number in a file: `propose_tuning`'s *free* half exists to
  choose exactly these three parameters, and choosing them changes no answer.
  Measured 2026-09-01 on `ver_0b71d21eeb3228f54437d9cf` (631 chunks, the 80
  questions the profile already owns): sweeping `per_section` × `min_score`
  found `per_section = 2` already optimal and `min_score` 0.60 → 0.55 worth
  **recall@5 0.8750 → 0.9125 and MRR@10 0.7827 → 0.7929**. The profile records
  0.55 now. Nothing answers differently.
  The fix is not to move the constants — they are global, so one document's
  measurement would re-tune every document in both organisations — but to have
  `retrieve.search` read the retrieval block of the version it is scoped to,
  keeping the constants as the default for a version with no profile.

  **And a rule the sweep produced, worth not relearning: a dense floor below the
  measured noise floor is not a tuning win.** `min_score = 0.50` scored best of
  everything tried (MRR@10 0.7990) and is *wrong*: the noise floor for this index
  is 0.5153, which is what a **wrong** chunk scores, so 0.50 wins the metric by
  admitting exactly what the floor was measured to exclude. Any automated search
  over `min_score` needs `noise_floor` as a hard lower bound, or it will
  reliably pick the value that cannot tell a hit from a miss — and it will look
  like an improvement, because the eval questions all have a right answer to
  find and none of them is off-corpus.

## Eight defects that were recorded here and are now fixed

Kept because each fix carries a rule worth not relearning.

- **A question spent money and nothing recorded it.** `SELECT count(*) FROM
  cost_entry WHERE run_id LIKE 'ask-%'` was **0** across the whole catalog while
  the ledger held $32 of indexing across four stages. The reason it needed a
  migration rather than only Python is the interesting part: `record_cost` takes
  no tenant and derives one in the INSERT from the run the charge hangs off, so
  that a charge and its run cannot disagree about who owns them — **no run row
  therefore meant no cost row**, and a run row needed a kind `run_kind_check`
  allowed. `20260831160000_run_kind_ask` adds `'ask'`, `AskWorkflow` opens the
  row before it asks and closes it after, and questions now appear in
  `recent_runs` beside imports. Measured on the first real one: planning
  $0.003051, `ask-embedding` $0.000003, answering $0.010157 — **$0.013211**.
  That middle row is the query's own vector, which `Answer.spend` never carried,
  so even a fixed ledger would have under-reported every question by it.

- **Adding a parameter to an activity broke the call site that did not pass it.**
  `evaluate_index` grew a sixth parameter with a default, and the five-argument
  baseline call then died on `'builtin_function_or_method' object has no
  attribute 'path'` — Temporal maps payloads onto parameters **by arity**, so the
  converter gave up and passed raw dicts, and `evalset.items` became
  `dict.items`. The same failure this repository already records as `'dict'
  object has no attribute 'source_path'`, reached from the other direction. Every
  call site passes every argument now. **The test doubles were what hid it**: an
  untyped `*args` double accepts any arity, so five workflow tests passed against
  a workflow the real converter could not run. They are typed like the real
  activities now, and all five fail when the argument is dropped again.

- **A document's own profile read as a structural collision.** The
  `heading_disagreement` check asks whether *inherited* rules see a different
  outline than the built-in ones — and a profile reused on the document it was
  learned from is not inherited from anywhere. Measured on
  `01_RetoDeDios_INT-S.pdf`, whose own learned pattern reads **38 chapters where
  the defaults read 8**: exactly the improvement a profile exists to provide, and
  it withheld activation from every re-index of any document that had learned
  one. The `default == 0` escape beside it covers only the case where the
  built-in detector finds nothing; here it found eight.

- **The second gate cost a document the profile it had just paid to learn.**
  `IngestWorkflow._run` reassigned `decision` — the `ProfileDecision` every later
  stage reads — to the `Approval` the second gate returned, so `chunk_final` was
  handed an `Approval` where it expects a decision. Temporal's converter coerced
  it into a defaulted `ProfileDecision` instead of failing, which means the run
  succeeded, the chunk count looked ordinary, and the document was silently
  chunked with the engine's built-in rules. **Nothing could see it**: the only
  symptom was a table of contents worse than the rest of its family's. The
  variable is `review` now, and
  `test_the_second_gate_does_not_cost_the_document_its_profile` fails when the
  shadowing comes back. Found by a later stage reading `decision.source` and
  getting an attribute an `Approval` does not have — which failed the workflow
  task, which Temporal retries for ever, so the first visible form of a
  four-month-old silent bug was a hung test.

- **A re-index that produced fewer chunks left the old tail in Qdrant.** Point
  ids are `point_id(version_id, chunk_index)`, so `upsert` overwrites `0..n-1`
  and never removes anything beyond them: a corrected profile that chunks more
  coarsely left the previous chunking's tail alive, carrying `char_span`s into a
  byte stream nothing holds. `removal.py` deletes a whole version and was the
  only other delete in the package. `QdrantWriter.prune_tail` runs on every
  index, not only after a tuning revert, and it is scoped to the version — an
  unscoped one would delete the back of every other book on the shelf.

- **A run's terminal state is a different question from its stage**, and
  `/runs/{id}` now answers both. A `stage` query against a failed workflow hands
  back the last value it recorded, so an ingest whose retries were exhausted
  reported `"stage": "learning"` indefinitely while `describe().status` already
  read FAILED. The half nobody had connected: `ImportScreen` polls the *gate*,
  reads a 409 as "keep waiting", and a run that died before publishing one
  answers 409 for as long as anybody asks — so the screen span forever. Both
  planes report `state`; the Rust client proxies `GET /runs/{workflow_id}`,
  which was one of three routes it did not; and `state: null` means "nobody
  could say", which the screen must read as *keep waiting*, never as *failed*.
- **A library id is not a name.** `ensure_library` was handed the id in both
  positions, so a library seeded as «Teología» read as `lib_teologia` in every
  picker from its first import. `IngestRequest.library_name` carries it now, and
  **empty means "leave it alone"** — tested against the parameter rather than
  against `EXCLUDED.name`, because the insert folds an empty name into the id
  and by the `DO UPDATE` the two are indistinguishable. The picker renders the
  name too: fixing the write without the read would have left the same string
  on screen.

- **An `async` activity with no `await` in it froze the whole worker, and the
  heartbeat that was supposed to notice could not be sent.** `extract_semantics`
  was `async def` around `_extract_passes`, a *synchronous* per-chunk network
  call, and `runner.py` builds the `Worker` with no `activity_executor` — so the
  activity ran directly on the worker's only event loop and held it for the whole
  document. `activity.heartbeat()` only **records**; the loop is what flushes it.
  Nothing was flushed, `PAID_HEARTBEAT_TIMEOUT` fired at minute five on an
  activity that was working perfectly, and the blocked coroutine could not see
  the cancellation either — cancellation is delivered through the same loop — so
  it finished all 600 chunks, projected them, wrote `semantics.json` and billed
  $4.72 into an attempt Temporal had closed 45 minutes earlier. `_PAID_RETRY`
  then ran the entire stage again, identically. **Measured 2026-08-31 on
  `ver_0b71d21eeb3228f54437d9cf`: $10.017265 spent, $9.4539 of it semantics
  charged twice, and the run ended `failed`.** The corroborating symptom is worth
  recognising: the worker log flushed dozens of `query task not found, or already
  expired` all at one timestamp — every workflow query the UI had polled while
  the loop was blocked. It was not the activity that was frozen, it was the
  worker. `docker inspect` said `restarts=0`, which rules out the orphaning case
  the heartbeat had just been added for.
  The fix is one `await asyncio.to_thread`, which is the pattern
  `activities/removing.py` and `asking.py` already use and state the reason for:
  *"a thread keeps the activity's own event loop free, which is what lets
  Temporal heartbeat and cancel it."* Awaiting once per chunk restores the ~5s
  interval the timeout was calibrated against.
  `test_extraction_leaves_the_event_loop_free_to_heartbeat` measures the loop
  itself — a ticker that must get a turn while extraction is in flight — because
  no other test in the suite can see blocking: they all call activities as plain
  functions. **Verified by reverting the fix: the ticker gets 0 turns.**
  The constant was never the bug and is unchanged.
  **Remaining exposure, deliberately not widened into this fix:** the projection
  block and `_condense_descriptions` still run inline on the loop. Projection is
  seconds; condensation is one generation call per concept and would re-create
  this exactly — it is only safe because `condense_descriptions` is off.
  A generalisation of this: **anything expensive that lands in the stores before
  its activity returns is not protected by the run's outcome.** Everything this
  run paid for is in Qdrant and Memgraph — 600 points, 600 chunks, 2,937
  concepts, 5,001 claims — under a version the catalog still calls `pending`.
  And because extraction is not deterministic and `claim_id` keys on the model's
  own paraphrase, the two attempts **MERGE'd as a union rather than converging**:
  the graph holds 5,001 claims where `semantics.json` records 2,965. Paying twice
  does not produce the same document twice.

- **The one operation that publishes an index already paid for was unreachable
  by everyone who pays.** `activation.activate_version` took
  `tenant_id: str = LEGACY_TENANT_ID`, and the FastAPI route called it without
  one. That default does both halves of the damage the `VersionNode.tenant_id`
  entry above describes, because this function both checks the tenant and writes
  it: the catalog lookup is the authorization predicate, so the default *refused*
  every other organisation with a 404; and `proj.activate` builds a `VersionNode`
  from it, so a caller who did mean another organisation would have written the
  activation into the legacy graph. The paid plane made the point moot by not
  serving the route at all — the one path the FastAPI plane had and it did not.
  So the escape hatch that
  `activation.py` documents at length, and that the app already wires end to end
  (`lib.rs:746` → `control.rs:1330` → `POST …/activate`), could not be reached
  for any version in `preprod` — which since 2026-08-31 is every version on this
  installation. Found while diagnosing the run above, where it was the $0 way out
  of a $10 failure. The parameter is required and keyword-only now, both call
  sites name their tenant, and the paid plane reaches the same Python through a
  new `ActivationWorkflow` — thin, exactly like `RemovalWorkflow` and for the
  same recorded reason: the catalog-first-then-graph ordering lives in one module
  so a second implementation cannot drift from it. The activity is registered as
  `promote_version`, because `activate_version` is already an activity name and a
  collision surfaces as a worker accepting a task and then failing it.

## Working on the engine (`docaget/`)

Read `doc/CLAUDE.md` first — it is the engine's own agent-facing document and
records ~25 design decisions each stated as *decision plus the measurement that
forced it*, the 14 inherited invariants, the four tuning-loop state bugs, and
three investigation logs. It is not auto-loaded from the repo root.

Keep changes surgical and keep its tests green; the CLI and LangGraph graph stay
working, because they are the cheapest regression test available.

Two conventions worth matching: every claim carries its measurement, and "no
measured baseline" is stated rather than hidden.

### The test corpus

`docaget/tests/corpus.py` resolves which document the property-based tests run
against: the Go reference (`../sociologia/output_corrected_peluquiado.txt`) if
present, otherwise the largest `libros/**/*.corrected.txt` of at least 120 KB.
The choice is printed in the pytest header — a property failure is only
actionable if you know which document produced it. `DOCAGENT_TEST_CORPUS`
overrides it and fails the session loudly if unusable.

`test_port_fidelity.py` deliberately does **not** use the resolver: it asserts
counts measured from the original Go implementation (328 chunks, kinds
309/10/9), so it is meaningful against that one file and meaningless against any
other. Tests marked `reference_corpus` are in the same category and skip on a
substitute.

When adding a test that touches a real document, ask whether you are asserting a
*property of the code* or a *fact about that document*. Several existing tests
silently required a book with 201+ chunks, or one containing both question and
footnote chunks.

### Defects that must be surfaced, not inherited

`doc/CLAUDE.md` documents failures the metrics provably cannot detect. The most
important: **profile fingerprints are structural, not topical**, so unrelated
document families collide and one gets chunked with another's heading rules.
Recall cannot see it, because the synthetic eval questions are generated from
the very chunks the wrong rules produced. The app's answer is to raise an
explicit warning at the gate from a structural comparison. Others: `doc_id_for()`
hashes the *path*, so byte-identical duplicates index twice and compete in
ranking; the heading guard accepts non-contiguous sequences like `[2,5,6,7]`;
the BM25 tokenizer drops tokens under 3 characters, so `"Gén. 2:15"` reduces to
`gen`; and the recall@5 target of 0.85 is a chosen threshold with no measured
baseline behind it.

Chunk `kind` values stay Spanish on the wire (`cuerpo`, `preguntas`, `nota`,
`tabla_fila`, `tabla_resumen`, `diapositiva`). They are stored in payloads and
used in Qdrant filters; renaming them breaks every existing collection. The UI
maps them to localised labels.

## Working on the app

- **The dev profile is tuned; the release profile is deliberately not.** The
  symptom was a build pinning one core at 100% while the other fifteen idled,
  and the cause was `[profile.release]`'s `codegen-units = 1` and `lto = true`
  being paid on every rebuild of `tauri dev --release`. Neither is a defect:
  one codegen unit means LLVM optimizes each crate on a single thread, and fat
  LTO pulls the bitcode of all 513 locked packages into one serial process. They
  are simply the wrong settings for a loop you run all day, and they stay on
  `release` because that is where the shipped binary is made. `[profile.dev]`
  now sets `opt-level = 0` for this crate with `opt-level = 3` for
  `package."*"`, so the dependencies are optimized once and cached while this
  crate stays cheap to rebuild — the app feels like the release build under a
  plain `tauri dev`. Measured on the 16-thread box: a full dev build is
  **50.8 s at 654% CPU with 13 `rustc` processes running at once**, against a
  single `rustc` for minutes before. Do not "fix" the release profile to match.
- **`debug = "line-tables-only"` is what keeps the dev profile off this disk.**
  Full DWARF, not the optimization level, is what made the debug `staticlib`
  845 MB and `target/debug` 5.8 G — the reason `--release` was once mandatory
  here. Line tables still put file and line in every backtrace, which is all
  this crate's panics are ever read for, and cut the `staticlib` to **352 MB**
  and the `cdylib` from 244 MB to **82 MB**. `target/debug` still measures 6.8 G
  because it holds the pre-change artifacts alongside the new ones; a
  `cargo clean` in `app/src-tauri` reclaims that once, and is the only reason to
  run one.
- All Tauri logic lives in `lib.rs`; `main.rs` is a passthrough, because Tauri
  replaces `main()` on mobile targets. Every command must appear in
  `generate_handler![]` or it 404s silently from the frontend.
- Capabilities are deny-by-default and the grant is deliberately minimal. A
  plugin permission often needs a *pair*: `opener:allow-open-url` permits calling
  the command while `opener:allow-default-urls` supplies the scope it is checked
  against — granting only the first denies every URL at runtime with no visible
  error. Read the plugin's own `permissions/` manifests rather than guessing
  identifiers.
- Errors carry a machine-readable `kind` so the UI can offer a fix, not just
  echo a message. New kinds with actionable advice go in the `GUIDANCE` map in
  `app/src/lib/api.ts`.
- **The Vertex project reaches the containers through `.env`; ADC reaches them
  through a conditional overlay.** `docker-compose.adc.yaml` is applied by
  `compose_args()` only when `find_adc()` located a file, because compose creates
  a *directory* at a bind mount's missing source. The project id is read back
  from `.env` on every launch like the Postgres password, since `write_env`
  rewrites the whole file. Compose interpolates at `up` time, so changing either
  needs the stack recreated.
- Both `en` and `es` bundles are shipped, and tests enforce key parity, matching
  interpolation placeholders, and that no key goes unused. UI language is a
  separate setting from a collection's *content* language.
- **The seven screens are all mounted at once and only one is shown**, and the
  tab order is the order of the work: `home`, `stack`, `library`, `explore`,
  `graph`, `import`, `ask`. Home is the default because "what is in here?" is the
  question a person arrives with — Services was the landing screen only for want
  of anything else. Home and Services are the two that own no library, so the
  picker is off both; Home's figures are project-wide, so a picker there would
  offer a choice that changes nothing.
- **Every screen may take a `go`, and only Home reads it.** A component declaring
  no parameters is assignable to `(props: { go: (tab: Tab) => void }) => JSX`, so
  the other six are unchanged by its existence. Home needs it because a stopped
  stack is a *state* it reports with a way out of it, and the way out is the
  Services tab. It imports `Tab` with `import type`, so the cycle back to `App`
  is erased at compile time and the prop still cannot name a tab that does not
  exist.
- **Home probes the stack before it touches the control API.** `stackStatus()` is
  a local compose call and free; reaching a control API that is not running costs
  the full request timeout before it says anything, and that would be the landing
  screen's first act on every cold launch. A stopped stack renders as a notice
  with a button, never as the red error panel — the containers being down is the
  ordinary state of a freshly opened app.
- **The Graph tab is a shell over two views, and both stay mounted.**
  `screens/graph/LibraryGraph.tsx` is the whole library and opens by default;
  `screens/graph/DocumentGraph.tsx` is the older radial view, reached by
  selecting a book and pressing "ver este libro". Re-entering the overview must
  not re-run the simulation and leaving it must not throw away a pan, which is
  the same reason the tabs are hidden rather than unmounted. The shell owns the
  heading and the intro, because it is what knows which view is showing.
- **Switching backend mode invalidates the whole app, and until 2026-08-30 it
  invalidated nothing.** `LibrariesProvider` fetched once, in its mount effect,
  so after a switch the picker still held the other plane's libraries and every
  screen still showed the other plane's data. `lib/backend.tsx` now owns the
  chosen plane and derives an `identity` from mode, address, organisation **and
  email** — a different person signing in must not inherit the previous one's
  questions — and `App` puts that identity in each screen's `key`, so changing
  plane remounts them. Services is deliberately excluded: it is the screen
  holding the switch, and remounting it under the user's own hand would take
  away the draft they just saved. Stored keys are scoped on the identity, and
  **the local plane's key stays bare** — the same decision as `_salt()` returning
  nothing for the legacy tenant, and for the same reason: scoping
  unconditionally would orphan every history and remembered library that exists
  today. The selection is dropped *before* the refetch, because an id that
  exists in both planes is exactly the case that must not cross silently.
- **A path typed by hand could never import anything, in either mode.** The
  volume makes one set of bytes out of two filesystems and two *namespaces* out
  of one: the app sees the workspace at its app-data directory, the container
  sees it at `/workspace`. So a host path passed Rust's `metadata` read and died
  at the worker's containment check, and a container path died at `metadata`
  first. Nothing translated between them — `IngestRequest`'s comment claims the
  API does, and the FastAPI `/ingest` passes `source_path` straight through — so
  the import screen could not succeed with any string. The 72 legacy documents
  all record `/workspace/inbox/libros/…`; they were imported over HTTP, never
  through this screen. `stage_source` copies into the workspace inbox now and
  returns the container path, which is the shape cloud mode always returned.
  `source_key` still derives from the path the user *picked*, not from where the
  copy landed: every staged file lands in the same inbox, and the graph takes a
  document's identity from `document_id(library, source_key)`.
- **The file chooser is opened by Rust, which is why no capability was added.**
  A capability grants the *webview* the right to invoke a plugin command, and
  the webview never invokes one — it invokes `pick_source`, an explicit
  `#[tauri::command]` like everything else Rust does on its behalf. The npm
  half of `@tauri-apps/plugin-dialog` is therefore not a dependency; only the
  crate is. Drag and drop goes through Tauri's own webview event rather than
  React's `onDrop`, because the browser's `File` carries no path and a path is
  the only thing either plane can use — and the subscription is allowed to fail,
  so a host without a Tauri webview degrades to "no drag and drop" rather than
  to a blank panel. A name already taken in the inbox is resolved by *content*,
  never overwritten: two different books can honestly both be called
  `capitulo 1.pdf`.
- **The wheel handler is bound imperatively, with `{ passive: false }`.** React's
  `onWheel` is passive and cannot `preventDefault`, so the page scrolls out from
  under the canvas instead of the canvas zooming. Buttons cover zoom in, out and
  reset as well, so nothing on that screen is wheel-only.
- **A new grid carries its own `@media (max-width: 76rem)` block below its own
  rule.** `.explore-panes` and `.ask-columns` work only because they are declared
  *above* the shared query; anything declared below it must repeat the query, as
  `.graph-layout` and `.run-row` do. A media query is not a stronger rule, only a
  conditional one.

## Other agent configs detected

`~/.codex/config.toml` and `~/.gemini/settings.json` exist on this machine. If
you want their MCP servers, slash commands, subagents, skills or instructions
brought into Claude Code, reply `/import` to scan and list what is importable,
then `/import --yes=<digest>` using the digest the scan prints. If `/import` is
unavailable on this surface, run `claude import` from a terminal.
