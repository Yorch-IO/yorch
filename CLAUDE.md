# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Two things, in one checkout:

- **`docaget/`** — a working, measured document-indexing engine (Python package
  `docagent`). Extracts, chunks with byte-exact spans, LLM-corrects, embeds via
  Vertex AI, indexes into Qdrant with hybrid dense+BM25/RRF retrieval, learns
  per-document-family profiles, and measures retrieval quality against a
  synthetic eval set. Driven by a CLI, orchestrated by LangGraph. It has been
  used on a real ~75-document Spanish theology corpus.
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
fails at `javascriptcoregtk-4.1`, and `--release` because the debug profile's
`staticlib` output has filled this disk.

```bash
# Engine
cd docaget && uv sync && uv run pytest -q          # 142 passed, 13 skipped
uv run pytest tests/test_invariants.py::test_inv01_char_span_is_byte_exact -q
uv run docagent index --dry-run libro.pdf          # free structural preview
uv run docagent index libro.pdf                    # spends money
uv run docagent query "pregunta" | profiles | diag

# Worker (Temporal workflows + control API)
cd worker && uv sync && uv run pytest -q           # 347 passed, 59 skipped
# 406 passed and nothing skipped with the stack up — and the Bolt port must come
# from `docker`, not `infra/.env`: BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789
# graph/ and catalog/ are integration tests: they skip, naming the URL they
# tried, when Memgraph or Postgres is down, and neither fixture wipes anything.
uv run pytest tests/unit/test_artifacts.py -k rewritten -q

# Desktop app
cd app && npm install
npm run typecheck && npx vitest run && npm run build
npx vitest run -t "define no key"                  # single test by name
COMPANY_BRAIN_REPO_ROOT=/home/jjimenez/yorch npm run tauri dev

# Rust — needs the Linux system libraries (see doc/COMPANY_BRAIN.md, Blocked)
cd app/src-tauri && cargo test --release
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
  corrected in `activities/ingest.py` changes nothing at a real gate until
  `docker compose … up -d --build api worker` runs. This is not hypothetical: a
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

## What is not built yet

Verified against the code on 2026-08-20 and again on 2026-08-21, not remembered.
Ordered by what it costs to leave alone. Details and measurements live in
`doc/COMPANY_BRAIN.md`.

**The eval-set stage is a switch with no activity behind it.** It appears in
`StageOptions`, in the cost estimator and in `artifacts.KINDS`, and is not
implemented. This is the piece that would let answer quality be *measured*
instead of argued about, and here is what that costs, concretely:

- **Gleaning stays off against its own measurement.** One pass buys +52% claims
  and +40% concepts for +70% cost — a decent rate — and the default is still zero
  because nothing can score whether the extra 41 claims are *better*. The
  measurement exists; the thing that would act on it does not.
- **`thinking_for("answering")` was settled on caution** because there was
  nothing to measure with, and it is the only stage where reasoning is left on.
- **Profile quality rests on a structural outcome rather than recall** — the
  entry directly below, which points back here for the same reason.

Three decisions, one missing activity. graphrag's `question_gen_system_prompt.py`
is the shape of the generator; the scoring half is ours to design.

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

**214 orphan `Claim` nodes are still in the graph.** Left by a removal path that
predates the current one — `_DELETE_VERSION` deletes claims before chunks, so
nothing live produces them. Until 2026-08-21 all 214 were *visible*, across 155
concepts including "Jesús" and "Pablo": `claims_about_concept` matched on `ABOUT`
alone, so each surfaced with a `source_chunk_id` the UI offers as "check the
source" and which resolves to nothing. That code is fixed — the template requires
the chunk, which is how `claims_for_chunks` always reached them — which is why
this sits here and not in the defect list below. What remains is debris in real
data, and deleting it is a decision rather than a change.

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
no `test` key. `App.test.tsx`, `AskScreen.test.tsx` and `GraphScreen.test.tsx`
render components; `askSession.test.ts` and `radial.test.ts` test the Ask reducer
and the graph's layout arithmetic as plain functions. It paid for itself on its
first run: `content.current?.scrollTo({top: 0})` throws in jsdom, so the shell's
scroll reset assigns `scrollTop` instead.

**Where a screen's geometry lives is a testability decision, not a tidiness
one.** jsdom implements no SVG layout — no `getBBox`, no resolved `transform` —
so a rendered test can assert that a node exists and carries the attributes it
was given, and nothing at all about whether two labels overlap. That is why the
graph's radial arithmetic is `lib/radial.ts` rather than module constants inside
`GraphScreen.tsx`: `radial.test.ts` asserts the properties that matter (no
concept label meets a document card, no label meets another, the two rings are
offset by half the outer step so no document sits on a concept's ray) directly
on the numbers, at the 12-and-8 counts that actually ship. Label footprints are
estimated from a character count at the stylesheet's font size, which is enough
when the question is whether two boxes are nowhere near each other.

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

## Known defects, not yet fixed

Distinct from the list above: this is shipped code that is wrong, not features
that are missing. Each was found by running the thing, and each is recorded
rather than fixed because the fix is somebody's decision or sits in another
session's files.

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
- **`Cost.usd` is annotated `float | None` and holds `Decimal`.** The column is
  `numeric(12, 6)`, so psycopg returns `Decimal` and arithmetic against a float
  raises `TypeError: unsupported operand type(s) for -: 'float' and
  'decimal.Decimal'`. Harmless wherever it is only serialised; a trap the first
  time anything compares or sums one. `repo.py:116`.

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

## Other agent configs detected

`~/.codex/config.toml` and `~/.gemini/settings.json` exist on this machine. If
you want their MCP servers, slash commands, subagents, skills or instructions
brought into Claude Code, reply `/import` to scan and list what is importable,
then `/import --yes=<digest>` using the digest the scan prints. If `/import` is
unavailable on this surface, run `claude import` from a terminal.
