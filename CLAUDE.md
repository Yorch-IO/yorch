# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

**Four things, and two of them are not in this checkout.** That sentence used to
read "three things, and one of them", and the arithmetic was load-bearing: it is
why the conversations feature shipped to both planes and to the desktop app on
2026-09-06 and **missed the Angular web client entirely**, which consumes the
same paths and has its own screen for every other feature. A plan that says
"both planes" is a plan about two backends and says nothing about how many
clients there are. Count them here.

- **`docaget/`** — a working, measured document-indexing engine (Python package
  `docagent`). Extracts, chunks with byte-exact spans, LLM-corrects, embeds via
  Vertex AI, indexes into Qdrant with hybrid dense+BM25/RRF retrieval, learns
  per-document-family profiles, and measures retrieval quality against a
  synthetic eval set. Driven by a CLI, orchestrated by LangGraph. It has been
  used on a real ~75-document Spanish theology corpus.
- **`../yorch-tauri-backend`** — a **second HTTP control plane**, in its own
  checkout beside this one. NestJS, multi-tenant, Cognito-authenticated: the
  *paid* product. The FastAPI plane in `worker/` is the *free, self-managed*
  one, and it is not going away. Both serve the same 28 paths with the same
  payloads, both read the same Postgres, Memgraph, Qdrant and Temporal worker,
  and the desktop app picks one with a backend-mode setting. That repository has
  its own `CLAUDE.md`; the entries below are the parts that constrain work
  *here*.
- **`app/` + `worker/` + `infra/`** — **Company Brain**, a Tauri v2 desktop app
  being built *around* that engine: a durable Temporal pipeline with an approval
  gate before anything is paid for, a Postgres catalog for provenance and cost,
  and a UI that makes the pre-index workflow inspectable.
- **`../yorch-angular-frontend`** — the **web client**, in its own checkout,
  and the one most easily forgotten because nothing in this repository imports
  it. Angular 20+ standalone components with signals and `httpResource`, served
  against the *paid* plane through `environment.apiBase = '/api'` proxied to
  8788 — it has no local mode and no Rust, so it talks to NestJS directly and
  therefore sees **snake_case payloads** where the desktop client sees camelCase
  (the Rust proxy renames on the way out). Its `core/models/contract.spec.ts`
  validates captured fixtures against hand-written models rather than
  enumerating the API surface, so **a path it has never heard of is invisible to
  it and its suite stays green** — which is the mechanism by which "stale" here
  does not look like "broken". Its test command is
  `npx ng test --watch=false`; the `--browsers` flag now wants a Vitest browser
  provider that is not installed. Plan:
  `~/.claude/plans/angular-web-chat.md`.

The directory is spelled **`docaget`**; the Python package and CLI inside it are
spelled **`docagent`**. This is a typo in the directory name and it is
load-bearing — every path must use `docaget/`.

Design plan: `~/.claude/plans/write-a-plan-to-shiny-eclipse.md`.
Current build status, verified commands and known blockers: `doc/COMPANY_BRAIN.md`.

**Video indexing has its own document: `doc/VIDEO.md`.** Read it before touching
`workflows/video.py`, `activities/video.py`, `videosource.py`,
`docagent/transcript.py`, or the `start_s`/`end_s` fields on `ChunkNode`. It is
not loaded automatically, and it holds the rules that are not visible from any
one file — why the *paragraph index* carries a chunk's timestamp rather than a
byte offset, why the locator drops the video's title, why a Temporal retry must
not charge Amazon a second time, and why waiting for a transcription job is a
workflow timer and never a polling activity.

**Reading a YouTube channel has its own document: `doc/CHANNEL.md`.** Read it
before touching `youtube.py`, `channelstore.py`, anything under
`brainworker/channel/`, `workflows/channel.py`, `ChannelScreen.tsx`,
`lib/channel.ts`, `lib/channels.tsx` or `lib/keywords.ts`. It holds the rules
that are not visible from any one file — why a channel *is* a library, why the
catalogue is a file and the indexed state is Postgres, why the batch is a sum
on a screen and not a gate, why the transcript pass reads the *uncorrected*
stream, why the quote widens its input where every other quote in this product
widens only its output, and — since later the same day — why one tick drives
both the probe and the approval, why the probe cap is a budget across rounds,
why the title-keyword filter reaches Discover but nothing else, and why a
sync of any size saves page by page and stops at the first known page *only*
once the catalogue has reached the end of the playlist, and why a parked probe
is reachable both from the screen that started it and from the import queue —
which now spans every library, because a channel's runs live in a library the
picker is never on. **Built 2026-09-16 for the free
plane and the desktop app only**; the paid plane holds the migration and the
stage-vocabulary fork and serves no route, and the Angular client has nothing.
That is a decision on the record, made with the count at the top of this file
in view, not an omission.

**Indexing a customer's S3 bucket has its own document: `doc/BUCKET.md`.** Read
it before touching `s3source.py`, `audioprobe.py`, `bucketstore.py`,
`activities/bucket.py`, `workflows/bucket.py`, `workflows/timed.py`,
`scripture.py`, `app/src-tauri/src/whisper.rs`, `lib/localTranscriber.tsx`,
`BucketScreen.tsx` or the paid plane's `src/buckets/`. It holds what is not
visible from any one file — why a bucket is a library and its catalogue a file
while Postgres owns what is indexed, why the quote is bound to the etag it was
made for, why Transcribe runs on *our* copy in *our* account, why there is a
run per object rather than a batch gate, why the transcript is written back to
the customer's bucket (a re-index then costs $0 in transcription), and the
whole local-transcriber half: why the engine is chosen **before** quoting, why
`awaiting_transcript` earned a seventh run state, and why the speed that
justifies it is measured rather than derived. **Built 2026-09-16/17 for the
paid plane and both clients.** The free plane serves no route by decision, and
says so.

**Recasting a document into another literary genre has its own document:
`doc/TRANSFORM.md`.** Read it before touching `brainworker/transform/`,
`activities/transform.py`, `workflows/transform.py`, `workflows/tracked.py`,
`TransformScreen.tsx`, `TransformGateReview.tsx` or the paid plane's
`src/transform/`. It holds what is not visible from any one file — why there are
**two** gates and why the first quote is explicitly a projection while the second
is a contract the planner is held to, why each gate has its own query, route,
type and component rather than reusing one, why two LangGraph `StateGraph`s run
*inside* activities and never cross the Temporal boundary, why chapters are
composed one at a time rather than fanned out, why the continuity a chapter
hands the next travels as an artifact rather than in a payload, why the research
budget is the dense-floor `supported` count and why zero of it is a real answer,
why the source document is excluded by a post-filter and never by widening
`ALLOWED_FILTERS`, why the prompt has **four** layers and why a convention that
binds every genre is not a seventh rule, why the bibliography is rendered by a
pure function with no model in the path, and why coverage is the one property that can hold a model to
the document it was given. **Built 2026-09-18 for the paid plane and the desktop
app only.** The free plane serves no route by decision and there is no Angular
screen; both are decisions on the record rather than omissions. **Its free half
has run against the real corpus and its paid half has not**: the probe, the
estimate and both gates were exercised on two real books for $0.000366 total,
which found three defects no test could reach — and every dollar figure still
rests on constants in `transform/estimate.py` that are guesses and say so.

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
cd docaget && uv sync && uv run pytest -q          # 256 passed, 16 skipped
uv run pytest tests/test_invariants.py::test_inv01_char_span_is_byte_exact -q
uv run docagent index --dry-run libro.pdf          # free structural preview
uv run docagent index libro.pdf                    # spends money
uv run docagent query "pregunta" | profiles | diag

# Worker (Temporal workflows + control API)
cd worker && uv sync && uv run pytest -q           # stack up: 1665 passed
# Measured 2026-09-18 with the stack **up** and BRAIN_MEMGRAPH_URL pointed at
# the port `docker` publishes (below). Without that variable 92 graph and
# retrieval tests skip against the default 7788 — measured again the same day:
# 1325 passed, 92 skipped. A skip, not a failure, and
# the stack-down figures below have not been re-measured since.
# Before that: 988 on 2026-09-06. With it down, graph/ and catalog/
# skip instead — 637 passed / 150 skipped the last time that was actually run
# (2026-09-05, before conversations added 20 more catalog tests). The
# stack-down figure has not been re-measured since; do not trust it to the digit.
# The Bolt port must come from `docker`, not
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

# How much a perfect reranker could recover, from the eval sets on disk, ≈$0;
# `--rerank semantic-ranker-default-005` also measures the real one ($1 per
# 1,000 questions). This is what put reranking on two levels and not three.
uv run python scripts/rerank_ceiling.py ver_… [ver_… …] [--rerank MODEL --levels brief,standard]

# Measure the speech rate the video gate projects a correction bill from.
# Free — captions and metadata cost nothing and no transcription job is started.
# See doc/VIDEO.md; the constant it feeds was a guess until 2026-09-05.
uv run python scripts/measure_speech_rate.py --queries "predicación" -n 12

# Desktop app
cd app && npm install
# The bundled yt-dlp is not in git — 40 MB per platform, a release about
# monthly. Fetch it before `tauri build`, which fails at the bundler without
# it, or `tauri dev`, which starts and then answers `ytdlp_missing`.
./src-tauri/binaries/fetch.sh                      # this machine's triple
# And the transcriber the app runs on the person's own GPU. Built from source
# on Linux and macOS (cmake, pinned commit), downloaded on Windows. `--cuda`
# when nvcc is there; the models are not bundled and are fetched on first use.
./src-tauri/binaries/fetch-whisper.sh              # this machine's triple
npm run typecheck && npx vitest run && npm run build   # 684 passed
npx vitest run -t "define no key"                  # single test by name
COMPANY_BRAIN_REPO_ROOT=/home/kheiron/yorch npm run tauri dev

# The window needs this on a host with no GPU compositing, or the binary starts,
# reports `Running`, and maps no window at all — with nothing in the log to say
# why. Measured on this machine 2026-09-05.
WEBKIT_DISABLE_COMPOSITING_MODE=1 COMPANY_BRAIN_REPO_ROOT=/home/kheiron/yorch \
  npm run tauri dev

# Rust — needs the Linux system libraries (see doc/COMPANY_BRAIN.md, Blocked)
# and PKG_CONFIG_PATH set, or the `soup3-sys` build script fails first. No
# `--release`: the tuned dev profile runs this gate in 60s at 412% CPU.
export PKG_CONFIG_PATH=~/.local/tauri-sysroot/prefix/usr/lib/x86_64-linux-gnu/pkgconfig
cd app/src-tauri && cargo test                     # 164 passed, 2 ignored
# Two tests are `#[ignore]`d because they talk to the bundled binaries — one to
# YouTube, one to whisper.cpp — and they are the only things that can say those
# binaries work at all. The whisper one found a timeout the unit tests could
# not: a CUDA build takes 38.7 s to name its device. Run them on purpose:
#   COMPANY_BRAIN_REPO_ROOT=/home/kheiron/yorch cargo test -- --ignored

# Rebuild the image the API and worker actually run. **All three overlays.**
# Without `dev` the `--build` recreates the containers and changes nothing;
# without `adc` the containers come up with no credentials and the first paid
# stage dies on `DefaultCredentialsError` — see the entry below.
cd infra && docker compose -f docker-compose.yaml -f docker-compose.dev.yaml \
  -f docker-compose.adc.yaml up -d --build api worker
```

**The Angular suite cannot run on this machine.** `@angular/build:unit-test` is
driven through the CLI, which requires Node >= 22.22.3; this box has v20.19.4, so
`npx ng test --watch=false` and `npx ng build` both refuse before loading
anything. What *does* work is the compiler directly — but note
`tsc -p tsconfig.json` checks **nothing**, because that file is `"files": []`
plus project references. Use `npx tsc --noEmit -p tsconfig.app.json` and
`-p tsconfig.spec.json`, which is two commands and the only typecheck available
here. Neither checks templates.

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
- **The graph converges only because something prunes it; `MERGE` alone does
  not.** The ids make a re-index *overwrite* on both sides — a point id is
  `point_id(version_id, chunk_index)`, a `claim_id` is `f(chunk_id, text)` — and
  that is exactly why a re-index leaves debris rather than dropping it: the
  document's ids survive while what they point at changes. Qdrant has
  `QdrantWriter.prune_tail`; the graph has `projection.prune_semantics`, called
  by `extract_semantics` and `replay_semantics` **after** they project, never
  before. Anything new that projects a version's semantics has to call it too,
  and the reason is not tidiness: a stale claim is attached to a chunk that no
  longer contains its quote and reads exactly like a good one.
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
- **The worker waits for the API, and the schema belongs to Prisma.**
  `infra/docker-compose.yaml` makes `worker` depend on `api` being *healthy*, so
  a cold start cannot race a worker against a schema that is not there. The
  coupling is startup-only on purpose: bookkeeping writes are best-effort, so an
  API that dies later must not stop the worker, and `depends_on` says nothing
  about that case.
  **This paragraph used to say the API's lifespan applied the migrations, and it
  had been wrong since the paid plane arrived** — corrected 2026-09-06.
  Ownership moved to `../yorch-tauri-backend/prisma/migrations/`, applied by the
  `migrate` compose service; `catalog/migrations.py` now *verifies* and says so
  in its own docstring, applying nothing in production and reading the Prisma
  files only for the Python tests. `require_schema` is a membership test rather
  than a `>=`, because a database migrated ahead of the code is the ordinary
  state during a rollout. Adding a table therefore means a Prisma migration, a
  `schema.prisma` model, a bump to `REQUIRED_MIGRATION`, **and** an edit to the
  literal list in `worker/tests/catalog/test_migrations.py` — which is spelled
  out rather than derived precisely so an unintended migration fails the suite.
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
### Conversations, and the rules that are not visible from any one file

Added 2026-09-06. Multi-turn asking over the *same* retrieval path a one-shot
question uses — `POST /chat`, `POST /chat/{id}/turn`, and an SSE stream per
turn, on both planes. `brainworker/chat/`, `workflows/chat.py`,
`activities/chatting.py`, `src/chat/` on the paid side, `ChatScreen.tsx` and
`lib/chatSession.ts` in the app.

- **A follow-up is rewritten into a standalone question, and that is the whole
  mechanism.** `retrieve.search` embeds the text it is given and has no memory,
  so "¿y su muerte?" would retrieve on the pronoun. `chat/rewrite.py` resolves
  the referents against a bounded window and hands the result to
  `answering.service.ask` unchanged — which is what lets every figure measured
  against the one-shot path keep holding. A first turn makes **no call and no
  charge**; only follow-ups pay. A failed rewrite degrades to the raw message
  rather than failing the turn, the same shape as `service._style`.
  Measured live: `resume eso en una frase` → *"¿Cómo se puede resumir en una
  frase la doctrina de la justificación por la fe?"*, so it converts an
  instruction and not only a pronoun.
- **What streams is a draft; the `done` event is authoritative and replaces it.**
  `citas` is the *last* field in `answer.SCHEMA`, so `_verify` cannot run until
  the envelope closes — a turn can stream fluent, cited-looking prose and still
  come back `insufficient_evidence` because the model cited chunks it was never
  shown. `chatSession`'s reducer replaces rather than merges, and a refused
  turn's empty answer wins over whatever was on screen. Appending would have
  created the one thing this product must not do: unverified text under a
  heading that says it was checked.
- **Temporal owns the session; the catalog owns the record.** `ChatWorkflow`
  carries only a bounded window (`WINDOW_TURNS`) and `continue_as_new` keeps even
  that from growing, because a transcript in workflow state is bulk data in the
  one place this codebase has a standing rule against — and would die with the
  retention that already makes `ask.service.ts` answer `question_not_found`.
  Going quiet ends the session *normally*; the next turn reseeds a new one from
  `conversation_turn`. Note the consequence a client must handle: after a
  `continue_as_new` the `turn` query answers `running` for a turn the previous
  run answered, for ever. The settled turn is in the catalog and that is where
  the collect path reads it.
- **The stream relay is a table because it replays.** An activity cannot stream
  to an HTTP response, and the two obvious channels were rejected on their own
  terms — heartbeats are throttled to progress rather than tokens, and a
  notification nobody heard is gone. `?since=N` is what makes a reader who
  reloads resume where they were, which is the `/ask` two-call lesson arriving
  from the streaming direction. Rows are flushed every 40 characters or 250 ms
  and deleted on settle.
- **Streamed prose is about 8% of the wait, which is why stages exist.**
  Measured 2026-09-06 before they were built: on `brief` a turn was **10.0 s**
  end to end and the relay filled in **0.8 s** at the very end; a `standard`
  turn on the same library took **75 s**. The cause is in the token counts — a
  297-character answer cost **1247 output tokens**, so most were reasoning, and
  `answering` has thinking on deliberately, so the model emits no visible text
  until it has finished thinking and then flushes almost at once. The reader had
  one unchanging line over four things that can each take seconds, and a slow
  stage looked exactly like a hung one.
  Stage events were built the same day, and the timeline of a real `brief` turn
  is now the argument for them:

  | at | event | |
  |---|---|---|
  | 0.20 s | `planning` | |
  | 3.01 s | `retrieving` | the planning call took 2.8 s |
  | 3.42 s | `evidence` | `chunks=4 dense=2` |
  | 3.42 s | `generating` | |
  | 13.48 s | first token | **the model reasoned for 10.1 s** |
  | 13.89 s | last of 5 tokens | 436 characters in 0.41 s |
  | 15.29 s | `done` | |

  So the dominant cost of a turn is reasoning before the first visible token,
  and nothing could see that before. `evidence` reports the dense-floor count
  `search` was already computing and discarding, so it costs no tokens and no
  round trip.
- **A stage travels as one event type carrying a name, never one type per
  stage.** Both planes map the relay row's `kind` onto `type: "stage"` and pass
  the payload through, and every client already ignores an event type it does
  not know — so a stage added in the worker reaches an older window as a `stage`
  it can render or ignore, and needs no change in TypeScript, Rust or the paid
  plane. The client falls back to the generic line for a name it has no label
  for, which is the failure the sidebar's raw `nav.chat` demonstrated.
- **`text` on a relay row means prose the reader sees, and nothing else.** A
  stage's payload goes in `detail`. Squeezing a stage name into `text` would
  have it concatenated into the middle of an answer by any client that appends
  deltas without checking the kind — and three tests, one per layer, exist to
  say so.
- **An unmeasured count is not a zero.** `dense` absent means the probe never
  ran; `dense: 0` is a real fact about the corpus and worth printing. The two
  render differently, which is `/project-summary`'s `available` rule applied to
  a figure rather than to a leg.
- **`ChatTurn.tenant_id` is required, unlike `Question.tenant_id`.** A tenant
  default on a *field* is what put a paying organisation's graph into the legacy
  tenant with nothing failing anywhere; `Question` carries that default because
  it predates the rule. A dataclass added afterwards does not get the excuse, and
  `chat.parity.spec.ts` asserts Python still refuses to supply one.
- **`open_turn` is one statement that is the ownership predicate, the tenant
  derivation and the sequence assignment at once.** Splitting any of the three
  out would make an id authorization, which the free plane's `/reindex` already
  records as a silent cross-tenant write. The primary key settles the race two
  concurrent turns can lose.
- **The wire is silent for most of a turn, so both planes ping.** This is the
  same measurement as the stage timeline read from the connection's side, and it
  was a live defect until 2026-09-06. Nothing is emitted between `generating`
  and the first prose, because reasoning tokens produce no text and
  `FieldStreamer` withholds a 60-character tail on top of that — measured against
  Vertex: **8.4 s on a `brief` turn, 11 s on two `standard` ones, and 43 s on an
  `off_corpus` turn**, which returns before `compose` is reached and therefore
  streams nothing at all, ever. A client cannot tell that from a dead
  connection, and the desktop app's `CHAT_STREAM_IDLE` was measuring the model's
  reasoning rather than the socket.
  `{"type": "ping"}` every `STREAM_PING_SECONDS` (10 s, forked as `PING_MS` on
  the paid plane), reset by every *real* event and not only by another ping — so
  a stream carrying prose never interleaves one. It is a real event and not an
  SSE comment because Nest's `@Sse()` serialises a `MessageEvent` and cannot
  write `: keepalive`, and one shape from both planes is worth more than a
  comment on one of them. **It needs no client change**, which is the payoff of
  the rule directly above: Rust, React and Angular all treat an unrecognised
  `type` as data. It carries **no `seq`** — it is not a position in the relay, so
  a reader resuming with `?since=` can never advance past unread prose.
  Verified live on both planes: one ping 10.08 s into the paid plane's silence
  and four at 10 s intervals across the free plane's 43 s.
- **A broken stream is not a failed turn, and the advice is opposite.** The turn
  keeps generating in the worker and lands in `conversation_turn` either way —
  that is the whole reason a disconnect does not cancel it — so rendering "this
  turn could not be answered" over one that is still running is the recorded
  `ASK_TIMEOUT` failure in a new shape: an answer computed, billed, and reported
  as lost. Seen in the real window on 2026-09-06 against the paid plane. Both
  screens key on the kind rather than on the state (`LOST_STREAM` =
  `control_stream_stalled`, `turn_abandoned`) and, more importantly, **ask the
  server what happened** instead of deciding: one free re-read of the
  conversation, never a re-ask, which would pay for the turn twice. The client's
  own guess stands only if that read fails too. Note `AppErrorKind` in
  `app/src/lib/api.ts` had never declared `control_stream_stalled`, so no
  guidance could be keyed on a kind Rust had been returning since the feature
  shipped.
- **A refusal's `reason` is the only thing that says which refusal it was, and
  `_settle` was dropping it.** `error` went out as `None` for every state, so
  four different facts with four different remedies rendered as one line reading
  "not enough evidence": nothing cleared the dense floor, the model cited nothing
  verifiable, the search returned no fragment at all, and the model could not
  compose an envelope. `answer.py` states the rule this restores — "no answer"
  with nothing to look at is indistinguishable from a broken index. It rides in
  `error` as `{kind: state, message: reason}` rather than in a column of its own,
  because both clients already render `error.message` under a settled turn, so
  it needs no migration and no payload change; and because `kind` being the
  *state* means the guidance maps have never heard of it and add nothing, which
  is correct — a refusal must not borrow a failure's advice. Verified live: an
  `off_corpus` turn now stores "ningún fragmento de esta biblioteca supera el
  umbral de similitud".

- **`off_corpus` and `insufficient_evidence` are different states.** The first
  means nothing cleared `topicality_gate`'s dense floor, the second means the
  corpus was searched and came up short. They have different fixes.
- **Asking has an effort level, and what it may *not* reach is the interesting
  half.** `brief`/`standard`/`thorough` move `top_k`, the fused candidate limit,
  the per-leg prefetch width and `claims_per_chunk` together;
  `answering/effort.py` is the only place those numbers appear, and `standard`
  reproduces exactly what the product served before the levels existed —
  asserted against the constants it replaced rather than against literals, so
  adopting it changed nothing until somebody moved the control. **Measured
  2026-09-03 on `preprod`/`lib_teologia`, four questions:** evidence 4/8/16 as
  designed, verified citations **4.50 / 5.50 / 9.25**, and **$0.0228 / $0.0316 /
  $0.0407** — so `thorough` roughly doubles the citations for 1.8x the bill, and
  `thorough` beat `standard` on every one of the four.
  **The wire carries the level name, never the numbers.** A client that sent
  figures could ask for two hundred chunks on a paid call, and the paid plane
  would have to police a table it does not own. Its fork
  (`src/ask/effort.ts`) is three strings and *no default*, guarded by
  `ask.parity.spec.ts` — which also compares the `Question` dataclass field by
  field, because `ask.service.ts` hand-builds that payload and Temporal's
  converter **silently drops a key naming no field**; for `tenant_id` that is a
  cross-tenant read with no error anywhere.
  A bad level is refused by FastAPI's own `Literal` validation — 422 with a
  list — never by a hand-raised 400 carrying a `kind`, because the paid plane's
  filter renders `class-validator` failures in that same FastAPI shape on
  purpose. A hand-rolled kind would have made the two planes answer one bad
  request two different ways.
- **The fused candidates are reranked at `brief` and `standard`, and not at
  `thorough` — and which levels is a measurement, not a budget.** Added
  2026-09-21 after the product had had *no* reranker of any kind. Measured
  first, built second, on the 640 eval questions the eight books already carry
  (`worker/scripts/rerank_ceiling.py`, ≈$0 because every question vector is
  cached): a *perfect* reranker over the fused list could recover **+0.100
  recall at `brief`, +0.073 at `standard`, +0.020 at `thorough`** against a
  pooled bootstrap margin of about ±0.014 — so `thorough` was inside the noise
  before a model was ever called, because 48 of 120 already holds nearly
  everything RRF reached. Then the real one, Vertex AI's Ranking API
  (`semantic-ranker-default-005`): **+0.094 and +0.066**, sixteen of sixteen
  (book, level) pairs up and none down, MRR inside the served window up about
  0.2, p50 0.22 s. It sits at exactly one line — between `q.search` and
  `diversify` in `retrieve.search` — and reorders only what RRF already
  reached, which is what keeps `off_corpus` meaning what it means.
  Four things that are decisions. **It runs after the topicality gate**: the
  first live question after it shipped was a refusal that had paid $0.001 to
  reorder its own examples. **A ranking failure keeps the fused order and
  books nothing** — losing 0.07 of recall on one question is a better trade
  than refusing it. **The score is passed through, never re-normalised**
  (RAGFlow's `_normalize_rank` contract: min-max only an unbounded provider),
  and the reranker reads `text`, never `embed_text`. And **it is billed per
  query, not per token** — `$1.00 / 1,000` queries of up to 100 records, read
  off the pricing page rather than a third-party table, so `ask-rerank` rows
  carry zero tokens and a dollar figure; the stage is declared in
  `ASK_COST_STAGES` and forked into `src/runs/stages.ts`. No key, no new
  dependency, `locations/global`, a different service
  (`discoveryengine.googleapis.com`) that needed no extra role here — checked
  by calling it. `BRAIN_RERANK_MODEL=` (empty) turns it off everywhere
  without touching the ladder.
- **Two retrieval knobs are deliberately off the effort ladder, for two
  different reasons.** `MIN_SCORE` is the topicality floor and the recorded
  sweep already settled it: 0.50 scored best of everything tried and is wrong,
  because that index's noise floor is 0.5153. `PER_SECTION` *looks* like a
  volume control and is not — **`diversify` backfills in score order when the
  cap leaves it short, so `diversify(hits, 2, 16)` returns 16, not 6** — so
  raising it would only buy back the near-duplicates the cap was measured to
  remove, and it already has a per-version claimant in a profile's `retrieval`
  block. It was in the table until that was measured.
  `top_k` survives as an explicit override, now clamped to `[1, 32]`, which
  closed a live hole rather than a theoretical one: it was an unbounded int
  straight from the request body, and **`top_k = 0` returned all 40 fused
  candidates rather than none**, because `diversify` compares
  `len(out) == top_k` only *after* appending.
- **The effort ladder scaled what the model *reads* and not what it writes, and
  that gap was invisible until somebody asked.** Measured 2026-09-03 on the real
  corpus: evidence 4/8/16 and citations 4.50/5.50/9.25, with the answer itself
  stuck between 705 and 1346 characters — and on "¿Quién fue Jesucristo?",
  `brief` came back **954 characters against `standard`'s 705 and `thorough`'s
  798**. The widest level wrote the second-shortest answer. Nothing in
  `answer.SYSTEM` mentions length and `max_output_tokens` is unset, so the prose
  was never connected to anything: its length was noise. `thorough` spent its
  extra tokens on *reasoning* (4084 against 2635) and then wrote the same
  summary.
  So each level now carries a `style` — appended **below** the six answering
  rules by `effort.compose_system`, with a sentence between them saying the
  rules win. Re-measured after: **278 / 660 / 1719 characters**, monotone at
  last, and `thorough` came back organised by theme. The style is written as
  *coverage* ("develop each point the fragments support"), never as a word
  count, for a reason that is easy to miss: `answer._verify` checks that a
  citation names a retrieved chunk and **never that a sentence is supported**,
  so "write more" enlarges the one surface nothing verifies.
- **The citation count was measured up the curve, and the curve turns — so
  `thorough` is 48 chunks, not the largest number the clamp allows.** Reported
  as "only 5 citations for ¿Quién fue Jesucristo?", which turned out to be two
  separate facts. Five *is* `standard`'s number, so the first answer was that
  the question had not been asked at the widest level. The second is that
  `thorough` at 16 chunks produced only 12.3 citations, and **nothing was being
  dropped**: 0 invented, 0 without a locator, 0 duplicates — every citation the
  model returned survived `_verify`. The ceiling was the model choosing to
  attribute 11 or 12 of the 16 it was given, which on inspection was a *sound*
  judgement: among the uncited were a table-of-contents line
  (`LA DOCTRINA DE JESUCRISTO ......... 98`) and two chunks about the etymology
  of "revelación". Forcing it to cite all 16 would have meant citing an index.
  So the lever was evidence, not wording. Three runs per point, and the first
  pass with one run per point had said it saturated at 24 — the spread at a
  single setting is about ±3 citations, wide enough to invent a plateau:

  | `top_k` | 16 | 24 | 32 | 48 | 64 |
  |---|---|---|---|---|---|
  | citations | 12.3 | 14.7 | 17.0 | **19.5** | 15.0 |
  | USD | 0.044 | 0.060 | 0.075 | 0.079 | 0.085 |

  64 is not noise — 14 and 16 against 48's 20 and 19 — and it is
  `Question.top_k`'s own warning arriving: past some width the model reads more
  and attributes less. `MAX_TOP_K` went 32 → 96 with it, because a clamp equal
  to the widest level is not a backstop, it is the level's value written twice.
  The prose does **not** lengthen (about 1,300-1,450 characters at 48 against
  1,605 at 16): the extra evidence buys attribution density, not length. On the
  reported question the shipped default now returns **23 citations against 5**,
  for $0.0820 against $0.0242 — so `thorough` is about 3.4x a `standard`
  question, which is a real per-question cost and the reason it is opt-in.
- **The style is per organisation and editable; the rules are neither.**
  `answer_style` holds one row per (organisation, level) and **only for a level
  somebody edited**, so an absent row means "use the built-in default" — which
  is what makes restoring a default a `DELETE` rather than a lookup of what the
  default used to be, lets a level added later need no backfill, and lets an
  improved default in `effort.py` reach everyone who never overrode it. How
  developed an answer should be is a house style; that the answer may not use
  general knowledge is not, and the six rules are unreachable from any screen.
  The default wording is forked into `../yorch-tauri-backend/src/ask/effort.ts`
  because that plane serves the same settings screen and cannot call Python to
  fill a text box — prose is a set of strings, which is what the fork convention
  permits, and `ask.parity.spec.ts` compares it verbatim (verified by breaking
  it). The length cap is a *field constraint* on both planes rather than a
  hand-raised error, so both answer an oversized body with FastAPI's own
  422-with-a-list, the same decision as `Question.effort`'s `Literal`.
- **Thin evidence steps the *style* down, never the search — and the obvious
  signal for "thin" is inert.** By the time the count is known the evidence has
  been retrieved and paid for, so what narrows is how developed the answer is.
  The first rule keyed on how many chunks reached the prompt, and measurement
  killed it: **every on-corpus question fills the width it asked for, however
  narrow**, because the fused RRF output carries no score floor (invariant #8).
  "¿Qué dice el texto sobre el Cireneo?" was handed all 48. The only thing below
  the width was `off_corpus`, where there is no answer to style anyway.
  What discriminates is how many cleared the **dense** floor, which the
  topicality gate was already computing and throwing away — so `search` runs
  that probe at full width instead of `limit=1`, same round trip and no tokens,
  and reports the count. Measured: 48 of 48 for "¿Quién fue Jesucristo?", 27 for
  "apokalypsis", **3** for "el Cireneo".
  **It steps down only when that is drastically thin** — below the narrowest
  level's own `top_k` — never on a sliding scale, and the measurement is what
  settles that rather than taste. Narrower prose carries fewer citations, so
  demoting mid-sized questions would take citations from the ones that have
  material. And the middle of this signal does not predict citations at all:
  "apokalypsis" cleared the floor 27 times, kept `thorough`, and the model
  returned **2 citations in 293 characters** — obeying the style's own "extend
  only as far as the fragments go" without being told twice. So the rule is a
  second guard over behaviour that already self-regulates, not the only one.
  Either signal can fire it: the dense count catches a narrow question against a
  large corpus, the evidence count catches a small library.
- **`asdict` serialises declared fields and nothing else, which is how a field
  can exist and be invisible.** `Answer.style_effort` was briefly set by
  `service.ask` as a loose attribute rather than declared on the dataclass.
  Python allows that, so the answered path worked and every test passed — while
  `/ask/{id}`, which returns `asdict(outcome.answer)`, dropped it from every
  response. There was no error anywhere; there was a field the UI could never
  see. It surfaced only because the `off_corpus` path returns *before* the
  assignment, so reading it there raised `AttributeError` during a live probe.
  `test_the_style_level_survives_serialisation` asserts through `asdict` rather
  than on the object, because reading the attribute directly is exactly what did
  not catch it.
- **A fixed reasoning budget can be a reduction, and that is why every level
  names none.** `thorough` shipped at 8192 on the reasoning that the widest
  level should think hardest. A/B on the real corpus 2026-09-03, four questions
  with its 16 chunks held constant: **8192 gave 7.00 citations and 3322 output
  tokens; leaving it unset gave 7.75 and 3582** — the default won 3, tied 1 and
  never lost, spending *more* output on 3 of 4. `None` sends no `ThinkingConfig`
  at all, so the model picks per question: a literal does not raise that
  **dynamic** value, it caps it. The number meant to buy more care was buying
  less, and the only symptom was one citation fewer on an answer that still
  looked perfectly grounded. The field stays, threaded and inert like
  `SearchOpts.prefetch_limit` was; any number named there has to beat the
  model's own choice, not merely look generous. Note the guard this needs:
  `BRAIN_THINKING_ANSWERING` **and** a global `BRAIN_THINKING_BUDGET` both
  outrank a level, the second because `answering` is deliberately absent from
  the stage map so a global reaches it.
- **The answering call has an output ceiling and no reasoning budget, and those
  are two different decisions.** `answer.MAX_OUTPUT_TOKENS` is 16,384 — 4.5x the
  widest output ever measured here — and it bounds the *bill*, because a call
  with dynamic thinking can spend the model's whole 65,536-token ceiling
  reasoning and return no text at all: measured once at $0.497373 for nothing.
  The reasoning budget stays unset at every effort level, because a fixed one
  was measured *worse*. A ceiling four times the widest real answer cannot
  reduce a real answer; a budget named in `ThinkingConfig` caps a value the
  model would otherwise choose. `_prepare`'s warning against setting
  `max_output_tokens` is about a number near the answer's length, and stands.
  When it is hit the call raises `TruncatedResponse` and the turn says the model
  ran out of room — never "not enough evidence", which is a claim about the
  corpus.
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
  **The consequence, which looks like a bug in the file and is not:
  `persist_profile_scores` moves `learned_from` along with the eval set**, so a
  family profile ends up naming the *last* document that measured it. Read on
  2026-09-02, `01-retodedios-int-s-pdf-corrected-72d480dc.json` carried
  `learned_from: 03-ElFrutoEterno…`, El Fruto Eterno's 80 questions and its
  scores, over a `heading_l2_pattern` and a `validation_notes` list that were
  hand-revised for *RetoDeDios* — including an exclusion list of that book's own
  28 repeated lines. The drop rule is what keeps this safe to read, and the file
  is a chimera to look at: the rules belong to one book and the measurement to
  another, and nothing in it says so.

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
  with `ignore_profile`, which pays for a full run. **The first one has a button
  since 2026-09-03**, in the import queue's own withheld row (`ImportQueue.tsx`),
  because until then the escape hatch existed at every layer and was reachable
  from no screen: measured on the run that prompted it, $4.1219 spent, 444 points
  in Qdrant, 2,466 claims projected, and nothing to press. It is offered for
  `structural_mismatch` **and for nothing else** — `pending` alone does not mean
  "complete index", since four versions in this catalog are `pending` because
  their run was *cancelled*, and keying on the version's state rather than on the
  run's reason would publish one of those. **`run.state = 'blocked'`
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

### EPUB export, and the rules that are not visible from any one file

Added 2026-09-15. An optional `epub` stage between `projecting` and `embedding`
on both the ingest and video workflows, a standalone `EpubWorkflow` for the
versions indexed before it existed, a download route on both planes, and a
title/author a model reads once and a person can overrule. `brainworker/epub.py`,
`bookexport.py`, `bookmeta.py`, `booking.py`, `activities/exporting.py`,
`workflows/epub.py`; `src/libraries/epub.service.ts` and `src/runs/download.ts`
on the paid side; `LibraryScreen.tsx` and `RunAudit.tsx` in the app,
`features/library/library.ts` in the web client.

- **The book is built from `chunks.jsonl`, not from the text stream, and that is
  what makes it one code path.** A chunk row carries `chapter` and `section`,
  which is the outline the chunker detected and **the only place it survives** —
  `build_chunks` *consumes* a heading paragraph rather than emitting it, so
  re-walking `corrected.txt` would mean re-running heading detection with rules
  this module would have to be handed. A DOCX, PPTX or XLSX has no text stream
  at all, only `structured_chunks`. A video has neither and has cues. One reader
  covers all three, through `indexing.StoredChunk.from_row` — the reader that
  exists so two writers cannot drift.
  The cost is honesty about what the index holds: a document whose chapters were
  never detected becomes one untitled chapter, because that is exactly what was
  indexed and what every breadcrumb on it says. The EPUB is the first surface on
  which a reader can *see* that.
- **`overlap` must never reach the page, and it is the field a rewrite reaches
  for.** It is the previous chunk's tail, carried so retrieval can show a
  fragment in context, and it is non-empty on about four rows in five — a
  renderer that emitted it would duplicate a paragraph on nearly every page,
  which reads as a corrupt book rather than as a bug. `embed_text` is worse: the
  breadcrumb and the overlap concatenated ahead of the text. Both are pinned by
  a test that plants a distinctive string and asserts it is absent from the
  archive's bytes.
- **The archive is byte-identical across builds, on purpose.** `dcterms:modified`
  is required by EPUB 3 and reading the clock would change the sha256 on every
  build — which the catalog records and `read_bytes` verifies on every download,
  so "regenerate" would look like a different book each time. The date a book
  was made is not a fact this product tracks; the version it was made from is,
  and that is in `dc:identifier` (`uuid5` of the version id, so a reader's
  library deduplicates rather than accumulating copies).
- **Stdlib only.** `worker/pyproject.toml` keeps a "no compiler in the image"
  property, so `ebooklib` and `lxml` are both out. The container, OPF and
  navigation shapes are ported from `scripts/generate_reto_de_dios_epub.py`,
  which has built a real book from a Notion export since 2026-08-24; its
  Markdown parser is *not* ported, because the input here is already structured.
  `mimetype` is written first and **uncompressed** — that is how a reader
  identifies the container without unzipping, and the first thing every
  validator checks.
- **One stage name on all three paths, so no `ARTIFACT_STAGE_OVERRIDES` entry is
  needed.** `ARTIFACT_STAGES` is keyed by artifact name alone and can therefore
  name one writer; calling the stage `epub` in `INGEST_STAGES`, `VIDEO_STAGES`
  and the new `EPUB_STAGES` makes that one answer right everywhere. `evidence`
  is the recorded cost of getting this wrong — attributed to a stage no video
  run has, it rendered under the trailing `stage: null` heading for months.
- **The metadata call is the only thing that can spend, and usually does not.**
  `document.author` is a column nothing has ever written and `document.title` is
  the picked file's stem, so a library of 74 books renders as
  `01_RetoDeDios_INT-S`. One bounded call over the first 6,000 characters fixes
  both — **once per document ever**, and never at all for a video, because
  `register_video` already fills the author from the channel. Estimated at
  ~$0.0045; the packaging itself gets no `StageEstimate` at all, because a stage
  absent from `COST_STAGES` renders `cost: null` in the audit and a `$0.000000`
  row would say "the charge was lost" instead of "this was free".
- **`None` is an answer, and the prompt says so.** A title page that names no
  author must come back as no author rather than a guess: the value goes into
  the catalog and the catalog is what a person then edits, so a plausible
  invention is harder to notice and correct than a blank. `_clean` also refuses
  the literal strings a model writes instead of nothing — `"null"`, `"none"`,
  `"desconocido"` — because a book whose author is `null` would be printed on a
  cover.
- **What the model writes is never allowed to disagree with a person.**
  `fill_document_metadata` moves the title only while it is still *exactly* the
  filename stem the import derived (computed with the same
  `PurePosixPath(source_key).stem` `stage_source` used, not guessed at with a
  heuristic) and the author only from NULL. `PATCH /libraries/{id}/documents/{id}`
  is the other half and is unconditional — and writing a value is also what
  stops the model being paid to guess again.
- **The download is allowlisted to one kind, and that is the decision.** Every
  other artifact is a working file — the text streams, `semantics.json`, an eval
  set carrying the corpus's own questions — and the free plane has **no
  authentication**: it is safe only because nothing off the machine can route to
  it. A generic `/artifacts/{name}` would make a misconfigured port the only
  thing between a stranger and the whole corpus, for a feature that needs exactly
  one file. `DOWNLOADABLE` is forked on both planes.
- **The digest is checked before the bytes are served**, which is the one thing
  distinguishing this from a static mount: `run_artifact` records what the run
  wrote, and a file that no longer hashes to it is not the book the catalog is
  describing. Serving it anyway is a well-formed answer about the wrong thing.
  `artifact_changed` is a 409, not a 404.
- **Tenancy comes from the `run_artifact` row, never from the path.** Run
  artifacts are deliberately *not* tenant-scoped on disk — `Paths.for_tenant`
  says so in its own docstring, and all twelve `ArtifactStore` call sites pass
  the volume root — so every organisation's files share `<workspace>/runs/`. The
  predicate on the paid plane's query is the whole boundary, exactly as
  `status()` already does it.
- **`Content-Disposition` carries both filename forms, always.** An HTTP header
  is Latin-1 and «Teología» is not, so the accented name rides in RFC 5987's
  `filename*` and the transliterated one is the `filename=` an older client
  reads. Sending only the first renames the book; only the second loses the
  accents for everybody. `download.parity.spec.ts` compares the two planes'
  answers over eight real titles, because the desktop app can be pointed at
  either and a book saved under two names is two files for one document.
- **The standalone build goes through a workflow on *both* planes**, unlike
  removal and activation where the FastAPI plane calls the module directly. It
  writes an artifact and it can spend, and `record_artifact` and `record_cost`
  both derive their tenant from the run row they hang off — so both belong in an
  activity. `run.kind = 'epub'` needed `20260915120000_run_kind_epub` for the
  same reason `ask` and `chat` needed theirs: **no run row means no bookkeeping
  of any kind**.
- **Both planes await the result rather than handing back a run to poll.** The
  build is a file read, a render and a file write, and the caller's very next
  act is to download it — a run id would make every client implement a wait for
  something already done. Same shape as `activate`.
- **A catalog that is merely down costs the book its title, not its existence.**
  Every catalog read in `activities/exporting.py` is `pooled=False` and wrapped:
  a pool retries a refused connection in the background, so a catalog that is
  down turns one immediate error into a ten-second stall. Measured: the activity
  suite went **50 s → 0.45 s** with the connection refused, and a test asserts
  the bound rather than trusting the comment.
- **The switch is appended and defaulted, so no `workflow.patched` is needed.**
  A gate parked for seven days decodes a payload that never carried `build_epub`
  as `False`, so neither command is issued and the replay's sequence is
  unchanged. The one patch in this codebase guards an activity inserted at the
  *head* of a workflow, which is the case a default cannot cover.

**Measured against the real workspace on 2026-09-15, before anything was seen in
a window.** All 54 runs that still hold a `chunks.jsonl` were rendered: **none
raised and none leaked an overlap**, and the 686 XHTML and OPF documents they
produced all parse as XML — which is the one class of error a reader shows as a
broken page rather than as a message. The largest, a 598-chunk book of 1,156 KB
of chunks, builds in **14 ms** to a 209 KB archive and is byte-identical on a
rebuild; **396 of its 598 rows carry an overlap and not one reached the page**.
The single timed run renders as one chapter with an `0:01` marker, which is the
transcript shape.
That corpus also demonstrates the honesty cost in the first entry above: a run
predating the 2026-09-03 heading fixes produces chapters called `2. Ibídem.` and
`6. Ibídem, p. 120.` — the recorded citation-as-heading defect, rendered exactly
as the index holds it. The book is not wrong; the index is, and this is the
first surface on which anybody can see it.
`epubcheck` has **not** been run: there is no jar on this machine. Parsing as
XML is a weaker check and is not a substitute.

**Two defects were found by building this, both in clients, both live.**

- **The Angular gate discarded every stage switch.** `gate-review.ts::decide()`
  sent `profileOptions(choice)` alone, and `IngestWorkflow._run` reads
  `approval.options` rather than the options the run was started with — so every
  stage a person unticked before pressing Import was silently turned back on at
  the gate, and every stage they ticked on was silently dropped. Nothing failed;
  the run simply did something other than what was asked. The desktop client has
  always merged them (`GateReview.tsx:65-66`), which is why this never showed up
  as a difference between the two planes. Fixed, with a test that fails before
  and passes after.
- **There was no parity spec for `StageOptions`, and the drift is measurable.**
  The desktop client's `StageOptions` has never carried `condense_descriptions`,
  which Python and the paid plane both have. Neither direction fails loudly: an
  *extra* property is refused by `forbidNonWhitelisted` with a 422, and a
  *missing* one silently takes its default, so a stage a person ticked does not
  run. `src/runs/ingest.parity.spec.ts` compares the DTO's declared properties
  against the dataclass's fields live, in both directions, and was verified by
  breaking it.

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

**Video indexing is built and deployed, and the production block is
environmental.** YouTube refuses `yt-dlp extract_info` from the EC2 egress IP —
measured 2026-09-05, along with the two facts that shape the fix: a caption URL
carries `ip=0.0.0.0` and the blocked host fetches it at 200, while a media URL
carries the resolving address and answers 403 anywhere else. So `resolve_video`
and `fetch_audio` can be routed to a laptop-side worker
(`worker/scripts/fetch_worker.py`, `BRAIN_FETCH_TASK_QUEUE`) and everything
else — the caption download included — stays where the workspace is. **Nothing
about that split has run outside the test suite.** `doc/VIDEO.md` has the table.

**There is a second route since 2026-09-10, and it is the one a paying customer
can use.** The worker split needs production Temporal, which is loopback-only —
the security group opens 443 from CloudFront and 80 for ACME and nothing else —
so serving `brain-fetch` costs AWS credentials, an SSM tunnel and a process
somebody keeps running. A desktop user has none of those. So the app makes the
refused call **itself**, with a bundled `yt-dlp`, and sends the answer:
`VideoRequest.resolved` carries the `VideoInfo` and the workflow skips
`resolve_video`; `VideoRequest.audio_path` carries audio the app downloaded and
uploaded through `POST /videos/audio`, and `stage_audio` puts it in S3 from the
host that holds the instance role. The narrowing that made `VideoInfo` fit a
task queue is what makes it fit a plane — 10,599 bytes on the video that
prompted this against 1,656,277 of raw info dict — and the caption download
still never moves, because `ip=0.0.0.0`.
Three things to know before touching it. **The record is not trusted**:
`videosource.check_resolved` refuses an id that does not match the URL (which
would index one video's words under another's identity, failing nowhere) and a
caption URL that is not YouTube's (the SSRF guard — `video_id`'s allowlist
governs which *video* may be named, not which *host* may be fetched), at the
route and again in `probe_video`. **Local mode is deliberately untouched**,
because the container's egress is the machine's egress. And **the app change
requires the plane change**: `forbidNonWhitelisted` means an un-updated paid
plane answers `422 request.property resolved should not exist`, seen in the
real window on 2026-09-10 against production. The two ship together.
The binary is a Tauri `externalBin` and is **not committed** — 40 MB per
platform, a release about monthly, against a whole `.git` of 104 MB. Run
`app/src-tauri/binaries/fetch.sh` before building; it pins the version and
checks the published sha256.

**Three other things about it have never run**, and the client-side fetch
above closes none of them. `doc/VIDEO.md` is the record; what belongs in *this*
list is what is missing. **Nobody has approved a video gate from the UI** — the panel and all
three gate states were screenshotted in the real window and clicks navigate, and
typing a URL and pressing Approve is still untested by anything but code.
**The reason recorded here was wrong, though, and it was the blocker: synthetic
keystrokes *do* reach this webview.** `xdotool type --window` does not — that is
`XSendEvent`, which WebKitGTK ignores — but plain `xdotool type`, which goes
through **XTEST**, works. Measured 2026-09-06 by typing six questions into the
Conversation screen's composer in the real window and sending them with `Return`.
So this verification is unblocked and merely undone; the recipe is
`GDK_BACKEND=x11` (so the window is an XWayland client `xdotool` and `import` can
see at all), `xdotool search --name "Company Brain"`, `mousemove --window` to
focus a field, then `xdotool type` with **no** `--window`.
**And `xdotool windowactivate <client id>` first, which the recipe was missing.**
Found 2026-09-10 typing a URL into the Import screen: a `mousemove … click 1`
lands on the field and the caret does not go there, so `xdotool type` sends
XTEST keystrokes to whatever has the keyboard focus and the box stays on its
placeholder. It is the same silent shape as the coordinate offset below — the
click reports success, the screenshot shows nothing — and the same wrong
conclusion is available: that keystrokes do not reach the webview. Activate,
then click, then type.
**And take the click coordinates from `xwininfo`, not from
`xdotool getwindowgeometry`.** The search returns two windows with this name —
a frame and the client inside it — and for the client the two tools disagree
about where it is: `xdotool` said `695,192` where `xwininfo` said `670,130`,
because it reports the position relative to the parent it resolved rather than
to the root. `import -window <id>` dumps the *client*, so a screenshot's
coordinates are the client's, and a 25x62 px offset is exactly enough to land a
sidebar click in the empty space below the last tab. The failure is silent —
`xdotool mousemove … click 1` reports success, the pointer really is over the
window, and the screenshot afterwards simply shows the screen unchanged — so it
reads as "clicks do not reach the webview", which is the wrong conclusion and
the one already recorded once above. **No video has been
indexed on the paid plane**: production serves `POST /videos` (verified — it
answers 401 where an unknown route answers 404) and no run has ever started
there. And **nothing longer than 19 seconds has been indexed at all**, on either
plane, so the grouping constants meet a real hour-long talk for the first time
whenever somebody tries one. Playlists and channels are refused by design, not
missing.

**Conversations are built and exercised; three things about them have not run.**
Six real turns went through the real window against the live free plane on
2026-09-06 — 3 answered, 1 `insufficient_evidence`, 2 `off_corpus`, $0.1044
total — and both `chat.parity.spec.ts` and the routes were verified on the paid
plane, which answers 401 on every chat path where an unknown route answers 404.
Stage events were added the same day and are verified live — see the timeline in
*Conversations* above.

**The paid plane's half was closed later that day**, and it is worth reading as
the argument for closing this kind of gap rather than recording it. Conversations
were held on `preprod`/`lib_teologia` — the real 73-book corpus — three ways: by
`curl` against 8788 with a Cognito token, in the Angular web client, and in the
desktop app switched to cloud mode. About a dozen paid turns, `standard` and
`brief`, at $0.023-$0.033 each. **It found three defects that no suite could
see, one of them making the feature unusable on a real corpus** — the silent
wire, the "could not be answered" over a still-running turn, and the dropped
refusal reason, all three written up under *Conversations*. A fourth is recorded
under *Known defects*: one answering call spent its whole 65,521-token output
ceiling on reasoning and billed $0.497 for no text.
It also found the naming bug in the paid plane's own `chat.service.ts`, where
`answered` was counted *after* `claimTurn`, so a conversation was never named —
and the existing test had been asserting the buggy value.

What still has not happened: **nobody has watched the draft→settled replacement
happen.** The turns that could have shown it either answered cleanly or refused
*before* `compose` was reached, and the case needs a turn that streams fluent
prose and then loses every citation to `_verify`. Covered by tests at three
layers and by nothing in a window.

**Reading a channel is built and nothing about it has touched YouTube, Vertex
or a window.** Built 2026-09-16 — `doc/CHANNEL.md` — and covered at every layer:
161 worker tests, 7 in Rust, 44 in TypeScript, each verified by reverting the
fix it stands on, plus the paid plane's parity spec verified by breaking the
fork. What none of that can do is sync a channel, because **no YouTube Data API
key exists on this machine** — checked in the environment and in both
`secrets.env` files, which hold only `SERPER_API_KEY`. One has to be created in
Google Cloud with YouTube Data API v3 enabled and entered on the Services
screen, which stores it in the OS keychain and writes it into the `secrets.env`
the worker mounts: the first provider credential this product has ever held,
and the pipe root `CLAUDE.md` described as empty is empty no longer. Until then
`POST /channels/sync` answers 503 `youtube_key_missing`, and that is the
correct answer. The first real sync is also the first measurement of whether
the topic pass reads a real auto-caption transcript well enough to be worth its
~$0.026 a video; the $0.42–$0.53 quoted for a hundred titles and ten
transcripts is from a synthetic channel of 76-minute talks. Videos without
captions are listed, quoted at Amazon's published rate and **start unticked**,
because they are both the worst informed row and about twelve times the most
expensive; `BRAIN_AWS_REGION` and `BRAIN_TRANSCRIBE_BUCKET` are unset here and
the audio half has still never run. And the first thing to measure once a
channel is indexed is the dense floor: a library made only of transcripts is
the worst case for `MIN_SCORE`, measured at 0.6001–0.614 against 0.60 on the
first real video.

**Nothing about the EPUB export has been seen in a window, and no real book has
been opened in a reader.** Built 2026-09-15 and covered at every layer — 33
tests on the writer alone, asserted against the archive rather than a return
value, plus route tests on both planes and the two clients' own. What none of
them can do is *look*: whether a 600-chunk book's table of contents matches the
chapters the Library shows, whether the accents survive two layers of escaping
into a real reader, whether `epubcheck` accepts the container, and whether the
one-chapter shape a transcript produces is readable at all. The recipe is in the
plan's verification section; the paid plane's half additionally needs the
migration applied. **`AuthorEdit` and the download button have not been
pressed** in the real window either, which is the same position every other
Library verb was in until somebody pressed it.

**Recasting a document into another genre is built, its free half has run
against the real corpus, and nothing has been composed.** Built 2026-09-18 —
`doc/TRANSFORM.md` — and covered at every layer: 163 worker tests on the pure
package, 16 workflow tests with typed doubles, 9 parity assertions on the paid
plane's fork verified by breaking it, and 16 in the two React components.

**The free half ran the same day, three times, for $0.000366 total**, against
`preprod`/`lib_teologia` through Temporal directly — the developer path the
design deliberately left open by putting the tenant guard at the route rather
than inside the workflow. Every run was refused at its first gate, so nothing
was composed. Two real books:
`4.-Doctrina-de-la-Regeneración` (19,105 characters, **1** detected chapter)
quoted at **$0.2922 – $0.6424**, and `01_RetoDeDios_INT-S` (468,714 characters,
43 chapters → 26 target ones) at **$6.0589 – $9.4133**.

**It found three defects, and none of them was reachable by any test.** All
three are written up in `doc/TRANSFORM.md`; the shapes are the ones this file
already records:

- **The probe sampled *chapters*, and 27 of this corpus's 52 documents have
  exactly one.** A 500-passage book was probed **once**, on its first 600
  characters, with the whole run's research budget derived from it. `supported`
  went **5 → 37** on one document when the probe moved to sampling passages.
  The cause is upstream and known: `build_chunks` consumes a heading paragraph,
  so a document whose headings were never detected really is one untitled
  chapter.
- **The budget's chapter cap was the *source's* count where it meant the
  work's.** A 400,000-character book with undetected headings has one source
  chapter and seventeen target ones, so it earned 3 queries instead of 51 — a
  17x under-budget on exactly the documents the first defect served worst.
- **Every charge was filed under `ask-embedding`**, which `ASK_COST_STAGES`
  deliberately maps to no workflow stage, so it rendered under the trailing
  `stage: null` heading — the `evidence` defect again — while the `COST_STAGES`
  entries naming `transform-probe` and `transform-research` were declared and
  written by nothing at all. Found by reading `cost_entry` after a real run,
  which is the only way any of this class is ever found.

**What the free half settled**: a real `chunks.jsonl` is read, the probe measures
the real library through the real dense floor, the estimate is computed and
persisted as an artifact so the quote is checkable afterwards, the run row is
opened before anything that can fail, and a refused gate is recorded as
`cancelled` in the stage it was in. The embedding cache also works: a second run
of one document wrote eight charge rows carrying **$0.000000** rather than no
rows at all, which is the rule that a stage that ran for nothing and a stage
that did not run are different facts.

**And then one was composed, in production.** A 1994 sermon transcript recast as
an essay: 3 chapters, 8 verified citations, **0 invented, 0 removed, 0
revisions**, 9 minutes, **$0.212318**. The work reads well. It found two more
defects, one in each half of what a gate is for, both now fixed and both written
up in `doc/TRANSFORM.md`:

- **The second gate showed three blank bullets.** The planner fell back to the
  source's own chapters — correct — but a transcript has one chapter with
  `title=""`, so `_split` produced three parts named nothing. The finished work
  was titled throughout, because a chapter is titled as it is *written*; the
  failure landed entirely on the one screen whose job is to show the outline
  **before** anybody pays. `_split` numbers an untitled part now, in the source's
  own language.
- **The quote was wrong in both directions at once**: `$1.0371 – $1.6494` against
  a bill of `$0.212318`, with `transform-genre`, `transform-plan` and
  `transform-research` all coming in **under** — the direction this product's
  rules forbid outright — while the total over-reported by five, which is the
  other harm the range exists to bound. All three were quoted from the wrong
  quantity: two from their *visible* answers while reasoning is on, one with
  `EVAL_QUERY_TOKENS` where a research query is a slice of the source. Re-quoted
  against the same run: **$0.3180 – $0.4313**, 1.5x at the low end and no stage
  under.

**What is still unmeasured**: one document and one genre. `Genre.expansion` has
never been measured for any of the eleven — a commentary expands where an essay
selects — and no mode but `faithful` has been composed. `doc/TRANSFORM.md`
lists what to read out of the first approved run, in order. One thing that run
should settle first: `QUERIES_PER_SUPPORTED = 8` is now visibly the binding
constant — the largest book in the library earned 6 research queries across 26
chapters, about one per four, and whether that is too thin is not answerable
without composing something.

**Nobody has pressed any of it in a window** — the screen, either gate panel, or
the download.

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

**The backend switch is witnessed as of 2026-09-06, and by more than the
picker.** It was recorded here as unwitnessed — `lib/backend.test.tsx`'s three
switching tests fail when the fix is removed, checked by removing it, and nobody
had watched a real window change planes. The app was then run in **cloud mode
against the paid plane** and the whole screen came from it: the picker read
`lib_teologia · 80 documents, 76 indexed` (which is `preprod`, reachable no
other way — the free plane's legacy tenant holds only `lib_pruebas`), the
Conversation tab listed the paid plane's two conversations with their generated
titles, a transcript rendered with its rewrite line and its citations panel, and
a new turn streamed and settled through the Rust proxy in 13.5 s for $0.0233
with 4 verified citations. Home renders its stack error in that mode, which is
correct and not a fault: containers are the local plane's business.
The session was **injected** rather than signed in, deliberately — the PKCE
loopback flow is separately tracked as never having run, and what was under test
here is the switch and the screen, not Cognito. So this closes the switch and
not the sign-in.

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
- **`staging` and `registering` preceded the `run` row, and that is what made a
  failed run invisible.** `_insert_event` derives its tenant from the run, so an
  event written before it is *silently dropped* — which is why the workflow
  buffered those two and flushed them once `register_document` returned. The
  buffering covered the **events**; nothing covered the row itself, and
  `Catalog.finish_run` is a bare `UPDATE … WHERE id = %s` that affects zero rows
  and raises nothing. Measured in production 2026-09-05: a video refused by
  YouTube failed 2.8 s in, `record_run_outcome` reported **Completed** having
  written nothing, and the import queue showed no row at all.
  Both workflows call `open_run` first now (`RunOpen`, modelled on
  `asking.start_question_run`), and the row carries **`library_id` and `label`**
  — the first because the queue is per-library and filtered through
  `d.library_id`, so a run with no document was filtered out of the screen that
  started it; the second because `title` is the *document's* and there is none
  yet, so the queue reads `title ?? label ?? workflow_id`. Both planes read
  `COALESCE(d.library_id, r.library_id)`.
  `_open` is behind **`workflow.patched`, the only patch in this codebase** —
  inserting a command at the head of a workflow breaks replay of anything in
  flight, and a gate parked for seven days is exactly that. The buffering stays
  for the same reason: it is still the live path for those runs. See
  `doc/VIDEO.md`.
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

### The import queue spans every library, and that was a defect for a while

Enqueuing *is* starting the workflow, so the queue is `run` rows and nothing
else — see the entry below. What it was **not** is project-wide: it took the
library from the top-bar picker and could not be asked for anything else. That
is fine for a screen somebody imports *into*, and wrong the moment a run is
started somewhere that owns no picker.

The Channel tab is exactly that: a channel *is* a library (`lib_yt_<channelId>`)
and that screen picks a channel, so the library picker is never on it. Reported
2026-09-16 as "a video requested from Channel does not appear in Import" — 11
probes parked correctly, seven days each, nothing spent, and invisible on the
one screen whose job is to show what is in flight, while the sidebar showed
them going because `/project-summary` is project-wide.

So the queue asks about every library and names each run's on its row, and the
library is an optional **narrowing**. Three things that are decisions:

- **The narrowing is a request parameter, never a filter over what arrived.**
  `QUEUE_LIMIT` is 30, so narrowing afterwards would show whichever of a
  library's runs the last 30 of *every* library happened to include — a
  different and much smaller list than "the last 30 of this one".
- **The library cell is conditional, so the row's grid needs a variant.** A
  conditional child in a grid of fixed tracks does not go missing: every later
  cell slides one track left, so the `1fr` that pushes the tail of the row to
  the right would land on the date the moment somebody filtered.
  `.queue-row.has-library` carries the eighth track; a screenshot is what said
  so, because jsdom lays out no grid — the same way the stretched kind badge
  was found.
- **The library rows reach `ImportQueue` as a prop, not as `useLibraries()`.**
  Everything else that component needs is one, and a context read there made
  three `ImportScreen` suites mount a provider to see a file chooser. It also
  exposed a double that had been lying: they faked the hook with a `libraries`
  key the real state has never had.

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

### A group's name goes beside its region, not across it

Added 2026-09-04. The name was written at the centroid of the cluster's own
drawn nodes, under them and behind a halo — a mitigation rather than a
placement, since the words stayed interleaved with the dots they describe.
`regionLabels.ts` places it outside instead, at the closest clear patch to the
cluster's own boundary, and is pure for the reason `force.ts` and `radial.ts`
are: jsdom lays out no SVG, so a rendered test can prove a `<text>` exists and
nothing about where it landed.

- **The type size decided whether this was possible at all.** A two-concept name
  at the old 44px is about **540 user units of a 1200-unit canvas — 45% of the
  width**, and ten of those cannot be placed outside ten clusters by any
  algorithm. 24px with one concept per line is 126-261 units, which is what
  makes the problem solvable. The size never depended on cluster size and still
  does not.
- **Two occupancy tests, because each is blind where the other sees.** A uniform
  grid over the drawn nodes answers "is this patch taken", which is also what
  intruding on another group amounts to — being among its concepts. The convex
  hulls answer the case the grid cannot: the sparse middle of a spread-out
  group, where a name sits in clear air and still reads as belonging to the
  wrong region. Measured, the hull test moves **one sample point of 450** on a
  library of dense blobs, and is the whole answer on a ring — 218 units of push
  with it, 26 and sitting in the hollow without. `hideIsolated` and `onlyBook`
  are what produce rings out of blobs.
- **`ATLAS_VIEW.MARGIN` is the gutter, and 80 is measured against 120 guessed.**
  `fit` preserves aspect, so a fitted cloud touches the margin on its binding
  axis and has slack on the other — and a name can always go to the slack side.
  The gutter therefore decides nothing except for a cloud already at 1200:820,
  where both axes are tight. At that aspect, on 73 books and 900 concepts:
  margin 40 crowds **4 of 10** names, 80 crowds none, and 120 crowds none while
  costing **21.6% of the drawn picture** against 40. The same library at its own
  roughly circular shape crowds nothing even at 40. So the constant covers the
  worst shape at the smallest price, and the crowded fallback — never a bigger
  number — is what covers a corpus worse than either.
- **A name is never dropped.** When no clear patch exists the least-bad
  candidate is taken and flagged `crowded`, which is the one case two names can
  still meet — and the only reason the paint order (largest last) still matters
  now that the solve order is largest first. Two orders, on purpose.
- **The footprint is measured, not counted.** `Roma` and `Tomás de Aquino` need
  different clearances, so `textMetrics.ts` asks a 2D context and falls back to
  `radial.ts`'s character ratio where there is none — jsdom, or a webview built
  without one. Measured against Chrome on twenty real concept names at
  `600 24px`: **mean real/estimate 0.983**, so the estimate over-reserves
  slightly, with a worst case of 1.27x on `Roma` — four wide glyphs against an
  average. Nothing in the product decides on the estimate; a window measures.
- **The solve is zoom-dependent and pan-independent.** A region label carries
  `scale(1 / zoom)`, so its footprint in layout units is `measured / zoom` and
  "no overlap" is a different problem at every zoom — which is also why this
  cannot live in the atlas worker beside the clustering it names. Pan is a pure
  translation and re-solves nothing. Measured on 10 labels: **6 ms at 467 nodes,
  2 ms at 3,067 and 5 ms at 12,867**, because each ray stops at its first clear
  distance and every grid query is local. What it scales with is the number of
  names, which is `CLUSTER_TARGET`.

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

## What the 2026-09-03 audit of the indexing path fixed

Fifteen defects of one class: each raised nothing, each left both suites green,
and each cost only quality. Every fix is pinned by a test **verified by removing
the fix and watching the test go red**, not by assumption. The measurements were
taken over the 45 files matching `docaget/libros/**/*.corrected.txt` with each
book's own learned profile applied, and over the 40 semantics artifacts and 9
runs with quote spans in `~/.local/share/io.sek.companybrain/workspace/runs`.
Nothing was re-indexed and no store was written.

- **A claim's quote span added a character index to a byte offset**, so it
  pointed at the wrong bytes for every chunk with an accent earlier in it — which
  in Spanish is nearly all of them. `_locate_quote` returned `offset +
  match.start()`, where `offset` is `char_from` in bytes and `match.start()` is
  an index into a `str`. Measured over the nine runs that carry spans: **295 of
  13,966 stored claim spans, 2.1%, resolved to the quote they were recorded
  for**, while the chunks' own `char_span`s verified 600/600, 631/631 and so on
  against the same stream. `claims_verified` counted every one as verified,
  because the quote *was* found — so the recorded "98.7% and 99.2% of claims
  carried a quote the code found" was true and the pointer stored beside it was
  not. **The test had pinned the bug**: it asserted
  `text.index("sobre el conocimiento")` over a string containing `á` and `ú`,
  which is 17 where the byte offset is 19.
- **A citation numbered like a heading became a chapter, and 428 of 4,239 chunks
  carried one as their breadcrumb.** `heading_level` had four guards, each added
  after a measured failure, and none for an endnote list. The rule now refuses
  the vocabulary of a citation (`ibid`, `idem`, `op. cit`, `pp. N`) and a
  ", <digit>" tail on a line that ends in a period, and it runs **before** the
  learned patterns because `heading_level` falls through to the built-in numbered
  detector when a profile's pattern does not match — which is why a profile never
  repaired it. Worst affected: `06-SexoEnLaBiblia` 233 of 359 chunks (64.9%),
  `CLASE 2` 53 of 76 (69.7%), `01_RetoDeDios` 126 of 600 (21.0%). Of the 34
  lines across the corpus whose level changed, **every one is a citation or a
  lowercase list item and no real heading moved**.
- **Its sibling: the same citations were classified `preguntas`**, which resets
  the section path while `nota` keeps it (invariant #11), so a bibliography wiped
  the breadcrumb of everything after it. 107 chunks moved out of `preguntas` and
  115 into `nota`. The fix recorded as tried-and-reverted required a ¿/?/an
  imperative to corroborate the dot and left every footnote with no section;
  keying on the citation vocabulary instead does not, and a numbered line that
  *asks* something is deliberately left alone (9 such citations carry a question
  in their title). **90 numbered paragraphs still read as `preguntas` while
  asking nothing** — citations with no comma-then-digit tail, and enumerations
  the author set in prose, which are not footnotes either.
- **A table of contents with spaced dot leaders escaped `TOC_LINE_RE`.** A
  typeset leader comes out of PyMuPDF as `". . . ."`, which `\.{5,}` never
  matched: 24 index and prologue lines across 4 books, 17 of them numbered and
  therefore tagged `preguntas`, each resetting the section path.
- **A paragraph too short to stand alone was thrown away rather than joined to a
  neighbour.** `_windowize` cut the pending run as soon as the next unit would
  overshoot `target_chars`, and `emit` then discarded anything under
  `min_chunk_chars` — which selects for exactly the document's short lines.
  **158 paragraphs reached no chunk at all**, and the shape of the loss is what
  settles it: 44 appear once in their document and are content ("Sobre el
  autor", "El verbo divino", the wrapped tails of sentences), and the other 114
  are running headers whose **49 of 55 distinct lines already appear inside a
  chunk elsewhere in the same book** — so the rule was never a header filter. A
  short run now travels forward into the chunk it opens, or merges backwards when
  nothing follows, and never past `hard_cap_chars`. 31 remain, the ones a merge
  would push past the cap. Chunk count over the corpus moved 4,239 to 4,246 and
  every span stayed byte-exact.
- **Two spellings of one concept were two `UNWIND` rows for one node**, so
  `SET k.type = row.type` let the last one win — and a row reached only as a
  claim's subject carries `type: None`. Measured over the 40 semantics
  artifacts: **1,169 groups of rows share a canonical key, 1,199 rows more than
  there are nodes**; `ingest-1788222510755-1ba85855` reports 2,612 concepts
  where the graph holds 2,484. The accumulator is keyed on
  `canonical_concept` now, which is what `project_concepts` derives the id from,
  and a known type is never overwritten by the absence of one.
- **The claim dedup dropped distinct claims that cite the same sentence.**
  `seen_spans` was per chunk rather than per pass, so two claims quoting one
  sentence in a single pass became one — and with `max_gleaning` at 0 there is
  only ever one pass, so that was the rule's only effect. It is exactly the pair
  `status` exists to keep apart: a text expounding the doctrine it is about to
  rebut cites the same words for `afirma` and for `niega`. Spans found by a pass
  are held back until it ends, which keeps the gleaning rule the comment
  describes. The drop rate cannot be recovered from the artifacts — the dropped
  claims are absent — but 128 pairs of distinct same-chunk claims share a quote
  *start* and 736 overlap, out of 40,019 pairs.
- **Rows sharing a baseline were ordered alphabetically.** `_page_rows` broke the
  tie with the row's own text, so table cells were assembled in dictionary order:
  9,302 of 109,444 lines share a baseline, 418 pages reorder, and **679
  paragraphs across 41 of the 84 PDFs come out different**, while the level-1
  heading count over the whole set moves only 148 to 147. The damage is shipped —
  `libros/done/Hermeneutica Capitulo 4.pdf.corrected.txt` reads "cada siete años
  se 15:2 perdona toda clase de deudas" — and correction cannot repair it,
  because reformulating is the one thing that pass may not do.
- **A correction could delete a sentence and the gate could not see it.** A
  ratio cannot express "a sentence was deleted": ±25% is three characters on a
  short paragraph and four hundred on a long one. Measured over the 2,684
  corrections in this repository's own cache, recovered by re-extracting each PDF
  and looking its paragraphs up by key: 2,145 gained or kept length, 525 lost 1-5
  characters, 5 lost 6-11, and **9 lost 12 or more — of which 7 had deleted real
  content**, including two section titles extraction had merged into their
  paragraph ("Generosidad en vez de avaricia.", "Se negó a recibir culto.") and
  two negations ("no se menciona, en este caso, la reproducción.", "Aunque los
  médicos decían no, yo sabía que"). None lost a proper noun, a two-digit number
  or a scripture reference. `MAX_LOST_CHARS = 20` refuses **7 of 2,684 (0.26%)
  and all 7 are content deletions** — zero false positives on this corpus. What
  it cannot see is a deletion offset by an addition; the measure is net.
- **And a cache hit skipped the gate entirely**, so a correction accepted under
  an older rule was reused for ever: those seven are in the cache this repository
  ships, and every re-index and every rebuild of those books would delete the
  same sentences again. `verify` is deterministic and free, so it now runs on the
  way out of the cache as well as on the way in.
- **Smaller, same class.** A numbered line whose title begins lowercase is a list
  item, not a chapter (4 in `05-CodigoJesus`). `_word_spans` returned each span
  starting *on* the space it had cut at, and `_split_oversized` strips the text
  without moving the offset — so every chunk ending on such a unit lost its last
  character, or half a multi-byte one; 0 of the corpus's 4,246 chunks reach that
  path, which needs a single sentence over `hard_cap_chars`, so it is a shape
  fixed on its own merits. `_SCRIPTURE_RE` capped a book name at 12 letters,
  which is shorter than "Tesalonicenses" and "Lamentaciones": 17 references
  indexed with their chapter and verse dropped, and widening the cap mints 13
  extra tokens, **all of them real references and no junk**.
- **The rule learner refused a proposal whose good half it was about to keep.**
  Both heading levels report under one rule name, and on a document that numbers
  nothing `validate` promotes that name to essential — so a level-1 pattern that
  had validated was blocked by an over-reaching level-2 one, the run earned a
  refine round it could not improve on, and after three attempts it fell back to
  the defaults with no table of contents. `Validation.passed` now ignores a
  failed `heading_patterns` when a level actually validated, which is what
  `adopt` already does. This became reachable on `01_RetoDeDios` only once its
  footnotes stopped counting as numbered headings — they were what made
  `_numbers_its_headings` true and skipped the promotion by luck.

**Two invariant tests stopped asserting anything on this corpus, and that is
information rather than an inconvenience.** With the false chapters gone,
`01_RetoDeDios_INT-S.pdf` — the document `tests/corpus.py` resolves to — produces
no breadcrumb and no section path at all under the built-in rules, because
`header_patterns` cannot strip a running header from a `.corrected.txt` and the
level-2 pattern is therefore rejected (recorded below). So
`test_inv04_breadcrumb_is_inside_the_embedded_content` was passing on a
breadcrumb that read `2. Ibídem.`, and
`test_inv11_footnotes_keep_their_section_path` had been **skipping** here for
want of any footnote chunk at all. Both now say so, and both assert the property
of the code on a document that has one: inv04 on a constructed chunk, inv11 on a
synthetic document carrying a heading, a footnote and a review question. Across
the corpus 4 books produce footnote chunks with a section, 13 chunks in total,
unchanged by any of this.

**And what could not be re-verified: `tests/test_port_fidelity.py` skips in this
checkout**, all ten of its tests, because `../sociologia/output_corrected_peluquiado.txt`
is absent. Its 328 chunks and 309/10/9 kinds are the cheapest strong test this
project has and the chunker changed underneath them. The corpus-level check that
stands in for it: chunk count 4,239 to 4,246 over 45 books, every span byte-exact,
and no chunk over `hard_cap_chars`.

## Known defects, not yet fixed

Distinct from the list above: this is shipped code that is wrong, not features
that are missing. Each was found by running the thing, and each is recorded
rather than fixed because the fix is somebody's decision or sits in another
session's files.

- **A cross-repo commit went half-done on 2026-09-18, and the parity spec caught
  it.** `stage_transcript` began raising `transcript_loops` — the refusal for a
  transcript that is a loop — and the paid plane's `BUCKET_KINDS` was not widened
  in the same commit, so `buckets.parity.spec.ts` was failing before any of the
  transform work touched that repository. Fixed in passing (422, the same status
  every other statement-about-the-bytes kind carries). Worth recording because it
  is the second time this shape has appeared: a kind is added on the Python side,
  the fork is not, and the only thing that notices is the spec that exists to.
  **Run the other checkout's suite before assuming its red is yours.**

- **A rejected correction is re-bought on every import, for ever, and re-rejected
  identically.** Measured when the first video was re-imported
  (`doc/AUDIT_VIDEO_20260910.md`, F7b): 85 of 107 paragraphs came back from the
  cache free, the **same 22** rejected ones were re-sent to the model, and the
  run produced `corrected.txt` and `chunks.jsonl` **byte-identical** to what was
  already on disk — `75c97c33…` and `ca7132f8…` both times. So `$0.031821` of a
  `$0.031821` bill bought nothing, and will be spent again next time. The
  embedding half shows the correct behaviour in the same run: every vector a
  cache hit, `$0.000000`. The whole asymmetry is `cache.put` being skipped on
  rejection. Same shape as the `$1.1965` of `$3.7572` the `trail` leg was built
  to find — 31.8% there, **100%** here. Caching the refusal is the fix and it is
  now possible, because `Rejection.proposed` records what was refused; what it
  needs first is a decision about re-verifying a cached *rejection* when the
  rules tighten, which is the same argument that made accepted entries
  verify-on-read.
- **The gate quotes a re-import as though it were a first import.** Visible only
  once `estimate.json` was persisted, which is how it was found within an hour of
  that fix shipping: quote `$0.2257767` against a bill of `$0.031821`, an
  over-report of **7.1x**, where a first import of the same video over-reported
  by 1.46x. `estimate_for` works from character counts and knows nothing about
  `cache/correct` or `docagent.embedcache`, so it quoted 107 paragraphs of
  correction when 85 were already paid for and 17,226 embedding tokens when the
  real figure was zero. Over-reporting misleads a user into declining affordable
  work exactly as much as under-reporting misleads them into approving expensive
  work, and a 7x over-quote on a re-import is the case most likely to be met
  with "that is too much for something I already have".

- **On an auto-caption transcript, `correct.verify`'s proper-noun rule protects
  the transcription error.** Measured on the first real video import
  (`doc/AUDIT_VIDEO_20260910.md`): **22 of 107 paragraphs — 20.6% — had their
  correction rejected, every one for `proper_noun` loss**, and the lost "names"
  are what the captioner misheard — `Tilich` for Tillich, plus `Beaida`,
  `Sawer`, `Bray`, `Chusa`, `Osana`, `Falangeja`, `Foxaque`, `Cuyama`. The rule
  is right on the corpus it was measured on, where a capitalised word is a real
  name and losing one is data loss. On speech a machine transcribed, refusing
  the repair **keeps the mistake**. The asymmetry is visible inside one run:
  `cartel` → `Gardel` was *accepted* because the captioner wrote the wrong name
  in lowercase, while the same repair capitalised would have been refused — so
  on a transcript the gate turns on whether the captioner happened to
  capitalise its error, which is not a property anybody chose. **Read in context,
  all 22 are the same thing**: the corpus now holds Fukuyama as *Cuyama*,
  Tillich as *Tilich*, Marx as *Mars*, Huntington as *Honding*, the FARC as
  *FARG* and Bethsaida as *Beaida*, in a Spanish theology library, because a
  rule written to protect proper nouns protected the transcription of them
  instead. Table in `doc/AUDIT_VIDEO_20260910.md`. Recorded rather than fixed
  because the shape a relaxation wants — *a capitalised token replaced by a near
  neighbour is a repair; one that simply vanished is still loss* — cannot be
  measured against this run, for the reason in the entry below.
- **A transcript is embedded with no breadcrumb, and it is measurably
  expensive.** `Chunk.embed_text()` prepends `breadcrumb()`; a transcript has no
  chapter and no section, so a video chunk reaches the embeddings API as bare
  speech while every book chunk carries `Chapter > Section` ahead of its text.
  Measured on the same import: the video's dense scores cluster at
  **0.6001–0.614 against `MIN_SCORE = 0.60`** — the whole document sits a
  hundredth of a cosine above the floor — and asked across the whole library,
  **0 of the top 5 hits are this video**, for a question phrased in the video's
  own title. `doc/VIDEO.md` left this explicitly open ("whether putting the
  video's *title* there would help retrieval is a real and measurable question,
  deliberately not answered by guessing"). **It has now been measured, and the
  obvious fix is not a clean win.** Embedding all 69 chunks a second time with
  the title prepended and comparing cosines against six queries: mean
  Δ **+0.0217**, range −0.0139 to +0.0946, and entry into the library's top five
  goes from 1 of 6 questions to 3 of 6. But the whole gain sits on the two
  questions phrased in the title's own words — the other four move by under a
  thousandth — which is the vocabulary leakage this file already measures on
  purpose elsewhere. And on those two questions the chunks clearing the dense
  floor go from 5 and 13 to **all 69**: a title strong enough to lift the
  document makes every fragment of it look equally relevant, and `diversify`
  cannot temper that here because a transcript has no sections for
  `PER_SECTION` to spread across. So the handicap is real and the one-line fix
  trades it for a monoculture risk; deciding needs an eval set, which is the
  stage a video has none of. Method and table in
  `doc/AUDIT_VIDEO_20260910.md` — nothing was written or re-indexed to get it,
  and the bare variant came free from the embedding cache, which is also what
  proves the stored vectors match `chunks.jsonl`.
- **An orphan `pending` version survives a track-selection change.**
  `ver_2c19d4975460f19198702f53` is still linked to
  `doc_34e7d656ba111ba08c930de8` and holds `captions:ab:auto` — **Abkhazian** —
  from the run that preceded the `_choose_track` fix by twelve minutes. Nothing
  indexes it, nothing charged for it, and nothing cleans it up either.

- **The second gate shows the *first* gate's report, and tells the reader
  "Nothing has been paid for yet" over a run that has spent $0.58.** Seen in the
  real window 2026-09-15 on `Conferencias Teologia Social`, parked at
  `awaiting_correction_review` with `$0.5834` of correction already billed — the
  figure is on the queue row directly above the panel that denies it. The panel
  went on to offer "No profile exists for this family of documents yet. One will
  be learned after approval", for a run whose profile had been learned an hour
  earlier, and a chunk count (299) measured *before* the correction that has
  since changed every offset.
  Two causes, and neither is the renderer's. `self._report` is assigned once, at
  `workflows/ingest.py:353`, before the first gate, and **never cleared** — so
  `GET /runs/{id}/gate` keeps answering with the pre-correction preview and
  estimate for the rest of the run. And the thing that should be shown instead
  does not exist on the wire at all: `IngestWorkflow.correction` is a
  `@workflow.query` with **zero callers** — no route on either plane, no screen
  in either client. `grep` finds the definition and nothing else.
  So the one gate whose whole purpose is "look at what correction did before you
  pay to embed it" cannot show what correction did, and fills the space with
  numbers that are not merely stale but false at the moment somebody decides.
  This file already recorded the weak version ("returns correction *counts*
  rather than a diff, so the UI cannot show what changed"); the counts are not
  reachable either. The fix is a route per plane over the existing query, and a
  panel that keys on the stage rather than reusing `GateReport`.

- **`deciding` is a one-way latch — and this entry is stale: it was fixed and
  the record was not updated.** Checked 2026-09-18 while adding a third gate to
  the same component: `ImportQueue.tsx` now clears it in a `finally` on both
  panels, with a comment naming the incident. The entry stays, moved nowhere,
  because the *shape* is the thing worth carrying — every screen that latches a
  button on a decision has to clear it on the way the decision can fail, and the
  new Recast screen carries the same `finally` and a test that asserts it.
  What follows is what the defect was.
  `ImportQueue.tsx` called `setDeciding(true)` in
  `onDecide` and **`setDeciding(false)` appeared nowhere in the file** — grep
  returned 0. On the happy path the row moves on and nobody notices; on a refusal
  the panel stays on screen with "Approve and index" and "Cancel" both disabled
  and no way to retry. Found 2026-09-15 by a person reporting that the gate had
  no answerable control: they had pressed Approve, taken a 422 from the paid
  plane, and been left with an inert panel over a run that was still waiting.
  A remount clears it, so relaunching the app is the workaround — which is
  exactly the kind of remedy nobody guesses.
  `ImportScreen.decide` compounds it slightly: `settled(workflowId)` is in the
  `finally`, so a *failed* approval is recorded as settled. That one is harmless
  today only because `settled` merely drops the cached gate and the next poll
  refetches it while `waiting(run)` still holds — the handler gets the right
  outcome for a reason that has nothing to do with the handler.

- **The desktop app forces a video's semantics off at the gate, so the stage
  `_recommended` deliberately stopped forcing off is unreachable — and the gate
  quotes it anyway.** `VideoGateReview.tsx:63` sends `extractSemantics: false`
  in every approval, under a comment saying "a video run has no profile,
  semantics, eval-set or tuning stage at all". That was true until
  `598a9a0` added the stage, and `_recommended` was changed in the same commit
  to pass the caller's choice through precisely so a person could tick it —
  its docstring spells out why forcing it off would be "worse than useless".
  Two consequences, and the second is the one that matters. A reader who wants
  this video on the Graph screen **cannot** get it from the app: the stage runs
  only if `approval.options.extract_semantics` is true and the client always
  says false. And because `_recommended` keeps the ingest-time value, a user who
  ticked semantics on the Import screen is shown an estimate that **quotes** it
  — measured at **$0.6532 against $0.2258** on a 76-minute talk — for a run that
  then does not do it. Over-reporting is the permitted direction for a *bill*,
  but this is not a cautious estimate; it is a quote for work the client has
  already decided to skip. Found 2026-09-15 while adding the EPUB switch, which
  travels through the same spread and is unaffected. The fix is a third checkbox
  in that component, or dropping the forced `false` and letting `...stages`
  carry it as every other switch does.

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
- **A 30-second retrieval turned out to be the query embedding, and the
  latency has nothing to do with the cost.** Found on 2026-09-06 by the stage
  events on their second live turn — which is the point of them — and settled
  the same day by timing each phase inside the worker container:

  | phase | |
  |---|---|
  | `provider.embed` | **0.36 s – 18.8 s**, one call |
  | Qdrant dense probe | 0.004 s |
  | Qdrant hybrid search | 0.003 s |
  | `_expand`, every graph read | 0.002 s |

  So the whole of `search` outside the embedding is **9 ms**, and the one
  network call in it swings by fifty times. Ten calls in a row produced 3.6,
  1.0, 9.2, 3.1, 1.1, 3.9, 0.8, 0.4, **18.8** and 0.4 seconds, one of them
  logging `embed failed (provider_quota), retrying in 2.1s [1/6]` — and the
  18.8-second call logged nothing at all, so raw latency reaches that on its own
  without the retry policy being involved. The quota is the recorded
  per-minute bucket (`online_prediction_requests_per_base_model`, measured at
  ~6 embeddings a minute sustained), and `RATE_LIMIT_ATTEMPTS` waits 2/8/32/60
  honouring `Retry-After`, so a turn that meets it twice spends forty seconds
  inside one call.
  **The useful part is the asymmetry.** `ask-embedding` costs about
  **$0.000004** — four orders of magnitude under the answering call, which is
  why it was invisible in the ledger for so long — and it is nonetheless the
  dominant latency risk in retrieval. Cost told nobody anything about it; a
  stage event did, immediately. Anything that reasons about where a turn's time
  goes has to measure, because the cheap call is the slow one.
  **The query embedding is cached since 2026-09-06, and the measurement is what
  prompted it.** `retrieve.search` fronts it with the same `CachedEmbedder`
  indexing uses, over the same `docagent.embedcache` keyed on
  (model, width, task, text), reading `Paths.embed_cache` — moved onto `Paths`
  precisely so the two callers cannot disagree about the directory, which would
  be a cache that silently never hits. Measured on the same question asked
  twice: `retrieving` → `evidence` went **0.40 s → 0.00 s**, and the charge went
  **12 tokens → 0**.
  Two things it introduced that are easy to get wrong. **A cache hit must not be
  billed**: the returned `Embedding` carries the token count the *original* call
  cost — right for saying what a vector was worth, wrong for billing — so the
  charge is read from `embedder.usage`, which accumulates misses only. Reading
  the other one books money nobody spent on every repeat question, and
  `test_a_repeated_question_books_no_embedding_charge` fails with exactly that
  message when it is. The row is still written on a hit, carrying zero, because
  a stage that ran for nothing and a stage that did not run are different facts.
  And **a cached vector is float32** where a fresh one is whatever the API
  returned, so two asks of one question are not bit-identical. Qdrant stores and
  compares float32 either way, so the ranking cannot move measurably — but
  anything comparing scores across asks should know.

- **The i18n dead-key scan is blind to a *read with no key*, and a dynamic key
  hides a whole family of them.** The scan reports a key nothing reads; it cannot
  report a `t()` call with nothing behind it. The sidebar renders
  ``t(`nav.${name}`)``, so the scan's dynamic-key fallback marks every `nav.*` as
  used — and an eighth tab added on 2026-09-06 rendered the literal string
  `nav.chat` in the real window, having passed the whole suite. Closed for the
  nav specifically by `i18n.test.ts::names every tab in the sidebar`, which reads
  `TABS` out of `App.tsx` and requires a label in both bundles. **Every other
  dynamic key in the app still has the hole**, and the general fix — asserting
  that each `t()` call site resolves — is not built.

- **A `useEffect` that persists state runs on mount, with the state empty.**
  `ChatScreen` saved the selected conversation on every change and restored it
  from `localStorage` on mount. Both effects run after mount; the save is
  synchronous and the restore awaits a fetch, so the save wrote `null` first and
  deleted the id before the restore could read it. **The feature never worked
  once**, and nothing in the suite could see it because no test remounted the
  screen with a populated `localStorage`. Fixed by persisting only a non-null
  selection and clearing explicitly in the one gesture that means "no
  conversation" rather than "not yet". Worth checking wherever else this shape
  appears: `askSession`'s `saveSession` is safe only because an empty session is
  the same thing as no session.

- **`control.rs`'s `Answer` struct drops `effort` and `style_effort`.** It
  declares only `state, text, citations, evidence, reason, spend`, and serde
  discards unknown fields on the way through — so both have never reached the
  webview, even though `api.ts` declares `effort` and the Python `Answer` carries
  them. A second, different hop from the `asdict` failure already recorded for
  `style_effort`. The conversation path is unaffected: `ConversationTurn`
  declares both and renders `styleEffort`.

- **An `ol` marker aligns to the *last* line of an inline-block child.** Every
  citation in `ol.citations` is a `button.link`, so a claim wrapping to four
  lines put its own number beside the fourth. Fixed 2026-09-06 with
  `vertical-align: top` after seeing it in the real window; the Ask screen shares
  the rule and had the same defect, hidden by shorter claims.

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
  **Measured again 2026-09-02 on `03-ElFrutoEterno_INT-S.pdf.corrected.txt`, and
  this time the damage shipped.** Its ten chapter titles appear 9-15 times each
  as running headers — `El poder de la paciencia` 15, `El misterio del amor` 14,
  plus `H h` 13 — so they matched the inherited level-2 pattern: **0 chapters and
  56 "sections" that alternate between the running header and the real
  sub-headings**, chunk 24 `EL GRANO DE TRIGO` and chunk 25 back to `El fruto
  espiritual`. The inherited exclusion list names *RetoDeDios's* 28 repeated
  lines, which do nothing here. That index was published anyway — measured
  recall@1 0.3125, recall@5 0.775, MRR@10 0.534, dense-only 0.85, noise floor
  0.5291 over 80 questions — so what the defect costs is not retrieval but every
  breadcrumb: a live citation on that book reads `El fruto espiritual` for a
  section name that is a page header.

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
  `/project-summary` already does with `available`. Seen again 2026-09-02 at a
  real gate: `03-ElFrutoEterno` inherited `01-retodedios`'s profile and the
  warning read "Solapamiento temático: 0%" for two books by the same author in
  the same series — a figure nobody could act on, printed at the moment somebody
  decides whether to spend $4.12.

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

- **The audit trail's timestamp for the transition after a long activity comes
  from before it, so the trail hangs the expensive stage's duration on the next
  one.** `_finish` and `_enter` pass `workflow.now()`, which is right in every
  argument for it — deterministic, replay-safe, unchanged under a retry — and on
  two real runs it did not advance across `extract_semantics`. Read straight out
  of the Temporal history rather than inferred: `record_run_outcome` was
  **scheduled at 15:26:57 carrying `at = 2026-09-02T14:41:48.067363`**, which is
  to the microsecond the `WorkflowTaskStarted` of the task that *scheduled*
  semantics (event 183), while the activity itself ran events 185-187, 14:41:48
  → 15:26:57. So the panel reads `semantics` as **40 ms** and gives its 45
  minutes to `blocked`. The same shape in the 2026-09-01 run that succeeded:
  semantics 36 ms, `activating` 22 minutes. `run.finished_at` is right in both,
  because `finish_run` takes it from SQL.
  **Not reproduced, and the probes are worth not repeating**: a 20 s activity, a
  25 s one that heartbeats with a `heartbeat_timeout`, and a worker with
  `max_cached_workflows=0` to force replay all advance `now()` correctly, on both
  the container's temporalio 1.32.0 and the host's 1.31.0. The 12.3-minute
  `evaluate_index` in the *same run* also advanced correctly, which rules out
  duration alone. So the trigger is unidentified and the trail is wrong in a
  specific, reproducible place: the row after the run's most expensive activity.
  Until it is found, read a stage's duration from `cost_entry.created_at` or from
  the raw history panel, which is exactly the second source that panel exists to
  be.

- **`tuning.json` is written before the paid candidate runs and never updated, so
  it permanently reports a verdict of "pending".** `propose_tuning` writes the
  artifact and `_tune_once` then chunks, embeds, evaluates and either promotes or
  reverts — and writes nothing back. Measured on `ver_f1d193c2f8995e09f65b3765`:
  the artifact says `{"candidate": "target=900", "accepted": null, "mrr_at_10":
  null, "why": "pending reindex"}` for a candidate that **did** run, produced 525
  chunks, scored MRR@10 0.5197 against the 0.534 baseline, was rejected and was
  correctly reverted — the collection holds the baseline's 444 points, verified.
  The numbers survive only in `scores.candidate.json`, and nothing joins the two,
  so the one artifact named after the decision is the one that does not record
  it. `promote_candidate_scores` already exists as the write on the accepting
  side; the revert has no counterpart.

- **The Library screen cannot offer the withheld-activation escape, because it
  has the version's state and not the run's reason.** Its promote button renders
  on `!v.active && v.state === "indexed"` (`LibraryScreen.tsx`), and a version
  whose activation was withheld is `pending` — so the one case the button's own
  docstring names first is the one case it does not appear for. Loosening it to
  `pending` would be wrong rather than lax: four versions in this catalog are
  `pending` because their run was cancelled and their indexes are partial. What
  the screen needs is the terminal run's `error_kind` on the version row, which
  is a payload change in both planes; the import queue does it today because a
  run row carries the reason already.

- **The BM25 length normalisation is computed per document, so the same chunk is
  weighted differently depending on which book it was indexed with.**
  `runner.index_chunks` builds `avgdl` from the batch it is handed
  (`docagent/runner.py`, `docs = [tokenize(c.text) for c in chunks]`), and that
  batch is one version — while `doc_sparse_vector` divides by it
  (`bm25.py:522`, `norm = k1 * (1 - b + b * dl / avgdl)`). Measured 2026-09-03
  over the 45 corrected texts: per-book `avgdl` runs from **54.6**
  (`ESCUELAS MORALISTAS`) to **103.0** (`8.-Doctrina-de-la-Resurrección`) against
  a corpus-wide 73.5, so a 70-token chunk carrying a term once is stored at
  **0.8962 in the first book and 1.1508 in the second — 28.4% higher for
  identical content**. Retrieval is scoped by tenant and library, not by
  document, so the sparse leg ranks every book of a shelf against every other
  through that skew. Nothing errors and no test can see it: within one document
  the ranking is self-consistent, which is the only thing the suite measures.
  **Not fixed, and the reason is the cost.** The fix is a constant the whole
  collection shares, and the moment it changes, every point already written is
  normalised against the old figure — so a half-migrated collection ranks worse
  than either end. Getting there means re-writing the sparse vector of all
  4,724 points, which is free of embedding cost but is a full re-index pass
  through the stores, and choosing the constant is a decision about whether it
  is frozen (and drifts as the corpus grows) or recomputed (and invalidates
  everything each time).

- **A concept's display name is pinned to the first spelling ever projected.**
  `_MERGE_CONCEPTS` (`graph/projection.py:432`) sets `k.name` under
  `ON CREATE SET` while updating `k.canonical` on every merge, so whichever
  document reached a shared concept first owns its label for the whole corpus,
  with no tie-break. Measured 2026-09-03 by replaying all 40 semantics
  artifacts in timestamp order: **207 of 12,196 concepts (1.7%) carry a display
  name that is not the majority spelling**, out of 1,404 with more than one.
  `SEÑOR` beat 19 later mentions of `Señor`; `espíritu` beat 12 of `Espíritu`
  against 9 lowercase; `cristianismo` beat 22 of `Cristianismo`; and
  `Darío Silva Silva` beat 11 mentions of the book's own `Darío Silva-Silva`.
  Both graph screens render those labels. Reported rather than fixed because
  "the majority spelling" is a rule somebody has to choose — the alternative,
  moving the write out of `ON CREATE`, only swaps the first arbitrary winner for
  the last — and because repairing the 207 already in the graph is a write.

- **`PAGE_NUMBER_RE` is applied to every row on the page, not to the band its
  own comment names.** `_filter_header_footer` (`extract/pdf_text.py:201`) drops
  any row whose whole text is one to four digits, wherever it sits, and the rule
  is documented as being for "bare page numbers that sit above the footer cutoff
  zone". Measured 2026-09-03 over the 84 PDFs in `libros/`: 2,948 of the 3,229
  matches sit in the top or bottom 15% of the page and are folios; **281 sit
  mid-page**, and ten of those are `03-ElFrutoEterno_INT-S.pdf`'s chapter
  numerals — set in 25.0pt type at 38% of the page height, directly above their
  titles, where the genuine folio on the same page is 10.0pt at 87%. So a
  256-page ten-chapter book carries no trace of its chapter numbering, and
  `HEADING_RE`, which needs a leading number, has nothing to match.
  **Measured the fix rather than assuming it, and that is why it is not
  applied**: restricting the rule to the top and bottom 15% recovers **3 of the
  10 chapters** (the numerals whose line pitch merges them with their title —
  "6 El tesoro de la amabilidad") and leaves the other 7 as bare-number
  paragraphs that no heading rule matches. The three arrive as `[6, 9, 10]`
  beside the document's existing `[1, 2, 3]`, which makes the level-1 sequence
  **non-contiguous — so `heading_guards` flips from pass to fail**, and since
  that rule is essential, `adopt` then discards the learned heading caps. Which
  index is better cannot be read off the extraction; it needs a paid learning
  run and a recall measurement.

- **The repeated-line gate is `pages // 4`, which makes a per-chapter running
  header arithmetically impossible to learn.** `Evidence.repeated_lines`
  (`extract/pdf_text.py:137`) offers the learner only lines seen on at least a
  quarter of the pages, and a chapter-title header appears on about
  `pages / chapters` of them — so no book with more than four chapters can have
  its chapter headers proposed as a `header_pattern`, and
  `_check_header_patterns` then reports "none proposed; nothing to strip" as a
  **pass**. Measured 2026-09-03: `03-ElFrutoEterno_INT-S.pdf` is 256 pages
  (threshold 64) and `El poder de la paciencia` appears in the header band 14
  times; header-band lines repeating three times or more that the threshold
  hides come to **490 instances across 6 of the 84 PDFs**, and counted in the
  *shipped* corrected texts as standalone paragraphs, **340** — 133 of
  RetoDeDios's 1,439 paragraphs (9.2%) and 118 of ElFrutoEterno's 1,092 (10.8%).
  This is the upstream half of the recorded `header_patterns` defect below,
  which is about the same headers surviving in a `.corrected.txt`; this one bites
  a fresh PDF import, where `header_res()` really is called. Not fixed because
  lowering the threshold changes the evidence a **paid** learning call reasons
  over, and whether the model then proposes a pattern that *validates* is not
  answerable without spending.

- **A page with a text layer can be discarded as having none.**
  `MIN_TEXT_PER_PAGE = 40` (`extract/pdf_text.py:259`) skips a page whose rows
  total fewer than 40 characters and appends it to `pages_without_text`, which
  the evidence then reports as "run with --ocr to transcribe them". Measured
  2026-09-03 across the 84 PDFs: **26 pages are discarded while holding real
  text**, among them `Cartilla ADN 2022.pdf`'s `PRESENTACIÓN` (page 2), its six
  `TALLER DE TRABAJO` pages and its three `NOTAS` pages. The threshold is a
  judgement about when a page is worth OCR and the fix is to separate that
  question from whether to keep the text already extracted; nobody has chosen a
  number.

- **Two ways an `.xlsx` loses rows, both measured on a named synthetic workbook
  and neither on real data, because there is no spreadsheet anywhere in this
  repository.** `_find_header` (`extract/excel.py:255`) extends a multi-row
  header downwards through any row that is at least 60% text, so a table of
  three text columns and one numeric one (`Concepto | Responsable | Nota |
  Monto`, five data rows) yields **one chunk instead of five rows**, with column
  names made of the swallowed data (`'Concepto / Concepto 1 / Concepto 2 / …'`)
  which are then re-rendered as the header line of every chunk and embedded; the
  evidence reports "1 data rows". And `_take_window` (`excel.py:75`) may end a
  window early on `MAX_WINDOW_CHARS` while the loop that calls it always strides
  `ROWS_PER_WINDOW`, so on a 40-row sheet whose rows render to ~420 characters
  the window fills at 4 and **23 of 40 rows appear in no chunk** — with
  `cell_ref` truthful about what is there, so nothing downstream can see the
  gap. `tests/test_excel.py` escapes the first because its fixture's data rows
  are half numeric. Left alone because the owner has deprioritised checking
  these formats and no measurement against real data is possible here.

- **`HYPHEN_BREAK_RE` glues two whole words when a dash closes a parenthetical.**
  `extract/pdf_text.py:247` joins a hyphen followed by whitespace and a
  lowercase letter, which is right for a soft hyphen at a line break and wrong
  for the closing dash of Spanish dialogue: `-Queda el más pequeño- respondió
  Isaí` becomes `pequeñorespondió` (`libros/01 Liderazgo.pdf`), and
  `erotismo- bendición` becomes `erotismobendición` (`06-SexoEnLaBiblia`).
  Measured 2026-09-03: **39 candidates corpus-wide** (the joined form absent
  from a 37,988-word vocabulary and both halves occurring 20 times or more),
  around 20 of the first 22 confirmed genuine by hand, against **5,291 correct
  soft-hyphen repairs**. Recorded rather than fixed because the two cases are
  indistinguishable at the point of the substitution — both are letter, hyphen,
  space, lowercase — and telling them apart needs the opening dash earlier in
  the line, which the paragraph assembly has already discarded.

- **`project_claims` and `project_semantic_edges` return `len(rows)`, not what
  the `MATCH` found.** `graph/projection.py:559,600` report the size of the list
  they were handed, so a claim whose chunk is absent from the graph is dropped
  by the `MATCH` and counted as projected anyway. No realised trigger, which is
  why this is a note rather than an entry: the path that would produce one is an
  **accepted** chunking tuning candidate, since `_tune_once` returns
  `candidate_chunked` with no second `project_structure` and `extract_semantics`
  then extracts against a cutting the graph does not hold. Every `target=`
  candidate in this workspace's seven `tuning.json` files reads
  `"accepted": null`, and no version's chunk count has ever shrunk.

- **The engine's ledger has no per-document attribution inside one invocation.**
  `costo.json` is a history of runs since 2026-09-03 rather than a single
  overwritten run, and each record names the paths it was given — but `Ledger`
  accumulates by stage, so `docagent index a.pdf b.pdf` leaves one record with
  two documents and one breakdown. Nothing needs threading through the call
  sites to fix it: `cmd_index` loops over paths with the same ledger, so a
  snapshot between documents gives the delta. Not done, because the ask was that
  the file stop overwriting itself and this is the next question, not that one.

## Defects that were recorded here and are now fixed

- **A transcript can be a valid document and not be a transcription of the
  recording, and nothing in this product could see it.** whisper.cpp's
  `--max-context` defaults to -1, feeding every word already produced into the
  next window as a prompt; once the model repeats a phrase it reads that back
  and emits it until the audio ends. The JSON parses, the size is ordinary, the
  process exits 0. Measured 2026-09-18 over the thirteen recordings this
  installation had indexed: **two of them** were loops, one replacing its
  closing 2.9 minutes — the altar call — with a single phrase said **167**
  times. Both had been chunked, embedded, answered from and archived into the
  customer's bucket first.
  The measurement is what settles the fix, because the obvious reading is
  wrong: the damaged transcript had *657 more words* than the clean one and
  **56 fewer distinct** ones. The loop does not add noise on top of speech, it
  takes speech away — so this was not a cosmetic defect in text nobody reads,
  it was missing content in an index people query. `-mc 0` collapses the
  longest run from 167 to 3 and recovers 126 distinct words across the corpus,
  all of them in the two damaged recordings.
  Two changes, and the second is the one that matters for the next engine:
  `whisper.rs::run_cli` passes the flag, and `stage_transcript` **refuses** a
  transcript whose longest identical run reaches `LOOP_RUN = 30` — the boundary
  every transcript crosses, whoever produced it. Refusing rather than warning
  is a cost argument: re-transcribing is free, accepting buys correction and
  semantics over fabricated text. The thresholds are where this corpus splits
  (healthy ≤ 7, damaged ≥ 51), not where taste put them, and `--vad` is
  deliberately unused — it fixes the loop too but merges 1,013 segments into
  748, and a segment's start is what a citation points at.
  **The general lesson is the one this file keeps relearning**: every check in
  the path was a check of *form* — `count_segments` counts, `transcript_engine`
  matches a shape — and a form check cannot see a document that is well-formed
  and false. See `doc/BUCKET.md`.

Kept because each fix carries a rule worth not relearning. The heading used to
count them and the count was already wrong — nine entries under "Eight" — which
is a small demonstration of the rule this file keeps applying to code: a number
maintained by hand drifts, and one that has drifted is worse than none.

- **A refused correction kept no record of what was refused, so `verify`'s
  false-positive rate had never been measured.** `correct_paragraphs` `continue`s
  before `cache.put`, so the model's text was discarded along with the
  rejection and all that survived was the reason plus the token that went
  missing. Two consequences, both real. The wide audit that set
  `MAX_LOST_CHARS` read **2,684 corrections out of the cache**, and the cache
  holds only corrections the gate *accepted* — so that sample could not contain
  a false positive by construction: the false-negative rate is measured on a
  real corpus and the false-positive rate is not measured at all. And on the
  first auto-caption transcript, 22 of 107 paragraphs were refused for
  `proper_noun` loss where every "name" was one the captioner had mangled, and
  the proposals were gone, so the table in `doc/AUDIT_VIDEO_20260910.md` had to
  be *inferred from the surrounding sentences* rather than read. `Rejection`
  carries `proposed` now and `correction-report.json` records it. It is model
  output the gate judged unsafe, so it goes in the report and **never into the
  corpus** — the corrected stream is byte-for-byte what it was. This is the
  precondition for relaxing the rule rather than guessing at it.

- **`chunk_transcript` built the warnings that matter most and dropped them on
  the way out.** It reports two conditions it cannot fix — a correction that
  moved the paragraph count, so the *uncorrected* transcript was indexed, and a
  paragraph that reached no chunk — and returned
  `Chunked(chunks=, count=, kinds=)`. `Chunked` had **no warnings field**,
  unlike `Transcribed` and `Preview`, which both do. So "the correction was paid
  for and not indexed" reached the worker's stderr and nothing else. Not
  hypothetical: the container that ran the first real video import was replaced
  three minutes after it finished, and by the time anyone looked the log held
  **zero** mentions of the run. `Chunked` carries them now and the workflow
  writes them as the `run_event.detail` on a second `chunking` row. The fix is
  replay-safe for free — a history from before the field decodes to no warnings,
  so the conditional command is never issued and the sequence is unchanged.
- **`auditversion.STREAMS` could not see a video's own stream.**
  `corrected.txt / extracted.txt / raw.txt`, and a video's uncorrected stream is
  `transcript.txt`. On the fallback path `choose_stream` would therefore crown
  `corrected` with near-zero verified spans: a byte-exact index reported as
  broken, by the tool whose only job is to be believed — the exact failure that
  function's docstring exists to prevent, reached from the video direction.
- **`evidence` was attributed to a stage the video path does not have, and
  `estimate` to one the rebuild path does not.** `ARTIFACT_STAGES` is keyed by
  artifact name alone, so it can name one writer per artifact; `evidence` has
  two (`extract` for a document, `group_transcript` for a video) and it named
  `extracting`, which is not in `VIDEO_STAGES`. `auditlog.build` then rendered
  it under the trailing `stage: null` heading beside charges that belong to
  nobody — the "no stage · evidence" row visible in every video's run detail.
  `ARTIFACT_STAGE_OVERRIDES`, keyed by `run.kind`, is where a second writer
  lives now; a property test asserts every override names a stage that path
  actually has, and the `stages.parity.spec.ts` fork compares the map with exact
  equality, so it was a cross-repo commit. The `estimate` half was found *by
  writing the artifact*: the mismatch could not show up while there was no file
  to misplace.
- **The `estimate` artifact kind was declared, mapped to a stage, and written by
  nothing.** Those two lines were its only references in the repository, so no
  run's quote was ever persisted and the only copy lived in the Temporal
  workflow — which retains for 72 hours, after which "did the gate over-report
  or under-report?" is permanently unanswerable for that run. That is the one
  question a gate exists to let somebody check. Both gates write it now,
  best-effort, because a gate that failed over its own receipt would be the
  worse trade. Same "declared and used by nothing" shape as `ChunkNode.sheet`
  and `SPEECH_RATE_SPREAD`, and found the same way: by auditing a real run.
- **`citation_title_prefixes` fired on every correct video.** It splits a
  locator on `" · "` and takes `[0]` to catch a re-projection under a changed
  title, but `_locator` returns early for a timed source and carries no title at
  all — the leading segment is a clock. A healthy 69-chunk video reported 69
  distinct "titles". `auditversion.title_prefixes` returns `None` there, because
  a check that fires on every correct run teaches a reader to skip the field.

- **An answering call spent its entire output ceiling on reasoning, returned no
  text, and was reported as "not enough evidence".** Measured on the real corpus
  2026-09-06, a `standard` chat turn over `preprod`/`lib_teologia`: the answering
  call billed **65,521 output tokens and $0.497373** — 20x the $0.0233 a normal
  turn of the same shape cost forty minutes later — took **6 m 42 s**, and
  produced not one character of prose. 65,521 is `gemini-3.6-flash`'s
  65,536-token output ceiling, so the model hit `MAX_TOKENS` while thinking; the
  envelope never closed, `json.loads` raised, and `answer.compose`'s exception
  branch returned `insufficient_evidence`. **Nothing failed anywhere** — the run
  is `succeeded` — and the turn read "Evidencia insuficiente", which is a claim
  about the corpus the run had no basis for.
  Two halves, and it is worth seeing why neither alone was enough. The reporting
  half threads `finish_reason` out of `generate` and `generate_stream` — read
  with **`.name`, never `str()`**, because `types.FinishReason` is a str-valued
  `enum.Enum` and `str()` yields `"FinishReason.MAX_TOKENS"`, the same trap
  `HistoryEvent.event_type` already records — and the JSON paths raise
  `TruncatedResponse` instead of a bare `ValueError` when the envelope failed to
  parse *and* the model had run out of room. It subclasses `ValueError` so every
  caller that handled the unparseable case is untouched; the one caller that can
  say something better does. The spending half is
  `answer.MAX_OUTPUT_TOKENS = 16384`, which is **4.5x the widest output ever
  measured here** (`thorough` at 3,582 tokens with no reasoning budget named) and
  turns a $0.50 loss into cents. Note what it deliberately does *not* do: it caps
  the bill, not the reasoning. Every effort level still names no
  `thinking_budget`, which the recorded A/B chose over 8192, and `_prepare`'s
  warning against a ceiling stands — it is about a number near the *answer's*
  length, which this is four times over.
  The truncation stayed `insufficient_evidence` rather than earning a state of
  its own, and the reason is the same one that gave `off_corpus` a state: what
  distinguishes a state is that a *reader* acts on it differently. Both refusals
  ask for the same thing — ask again. What had to change was the sentence, and
  the sentence reaches both clients for free, because `_settle` now carries a
  refusal's `reason` into `error.message`.

- **A re-index left the previous run's semantics in the graph, attached to
  chunks whose text had moved.** Every projection is a `MERGE`, so re-extracting
  a version *added*. The vector side has pruned since `QdrantWriter.prune_tail`;
  nothing pruned the graph. Measured 2026-09-01 on
  `ver_0b71d21eeb3228f54437d9cf`, re-indexed with a corrected profile that took
  it from 600 chunks to 631: **5,001 claims where the run extracted 3,055 —
  4,988 (62%) left behind**, and 4,057 of 7,554 `MENTIONS` (53.7%) likewise.
  Not inert debris, which is what made it worth fixing rather than sweeping:
  `claim_id` is `f(chunk_id, text)` and `chunk_id` is `f(version_id, index)`, so
  re-chunking keeps every id *alive* while the text underneath changes. A stale
  claim stays attached to a chunk that no longer contains the quote it carries
  and is indistinguishable from a good one at read time — the state
  `Semantics.claims_verified` exists to keep visible, reached from the one
  direction it does not cover: the quote *was* verified, against a chunk since
  re-cut.
  `projection.prune_semantics` is the fix and it mirrors `remove_version`
  exactly: candidates through all three routes in `_CANDIDATE_CONCEPTS` taken
  **before** the delete, the claims and `MENTIONS` this run did not produce
  deleted, then `_COLLECT_ORPHAN_CONCEPTS` over the candidates. `ABOUT` and
  `INVOLVES` need no equivalent — they hang off a `Claim` and `DETACH DELETE`
  takes them — but `MENTIONS` does, because it survives independently: a chunk
  keeps its id across a re-chunking while its text changes, so an edge naming a
  concept the new text never mentions stays perfectly well-formed.
  Three things about it that are decisions rather than details. **It runs after
  projecting, never before**: after, the keep-set is exactly what is now in the
  graph and the version is never observable without its semantics; before, there
  is a window holding neither. **The keep-set is derived inside**, from the same
  `claim_id` and the same `MENTIONS` edges the projection wrote, so it cannot
  drift from what was projected. And **a retry converges** — projection is a
  `MERGE` and this is a set difference against the same set — which incidentally
  fixes the recorded case where two attempts at one document *unioned*, leaving
  5,001 claims where `semantics.json` recorded 2,965. Paying twice no longer
  produces the document twice.
  One measurement, because the obvious worry was wrong: the whole-version
  membership test that counts what is stale runs in **0.01 s** against the
  biggest version this installation holds (3,055 claims, 3,497 `MENTIONS`). The
  per-chunk shape of the *deletes* is kept because a chunk is what a keep-set is
  naturally a set of, not because the alternative was measured slow.
  `replay_semantics` calls it too: replaying the artifact a version already
  holds is a no-op costing two reads, and replaying an *older* one is precisely
  the act of republishing it, where leaving the newer claims beside it would
  make the graph agree with neither.

- **A rebuild's semantics stage was visibly free, and it was invisible
  instead.** `replay_semantics` built a zero-token `Spend(stage=
  "semantics-replay", …)` under a comment claiming it "keeps the stage visible
  in the run's ledger" and **never called `_charge`**, so no `cost_entry` row was
  written. Invisible until 2026-09-01, when the audit ledger started rendering a
  per-stage cost and the row came back empty. The open question recorded beside
  it — "whether re-projecting an artifact spends at all" — is answerable by
  reading the stage: it is a file read and a graph write, no provider call and
  nothing to price. So it books a zero row, and that is the point. **A stage
  that ran for nothing and a stage that did not run are different facts**, the
  same distinction the cached query embedding already books a zero row for, and
  an empty cost cell reads as the second.

- **`_condense_descriptions` would have died the first time it ran, after the
  money was spent.** It called `row.get("raw")` on what
  `read_concept_descriptions` is *annotated* to return — `list[dict]` — while
  the function returned `graph.write(...)`, which is `list[Row]`, and `Row`
  defines `data` and `__getitem__` and no `.get`. The call would have raised
  `AttributeError` inside `extract_semantics`, **after every per-chunk
  extraction in the document had already been paid for**, which is the most
  expensive stage in the pipeline. Dormant only because
  `condense_descriptions` is off.
  Nothing could catch it: there is no type checker on the Python side, so the
  annotation was decoration — and the four tests over that function
  monkeypatched `read_concept_descriptions` to return the dicts it promised,
  which is the exact shape of a double that agrees with the assumption under
  test. The fix is to return dicts for real, making the annotation true, and it
  is also the shape `set_concept_descriptions` next door already takes. The
  test doubles for `Graph` in the activity suites are graph-shaped now rather
  than stubbed away, for the same reason.

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
  **That fix reached one activity, and on 2026-09-03 the same shape surfaced
  live in the other five.** A question asked 48 seconds after an import was
  started came back as "the control API did not answer", over an API that was
  replying in 0.0009s. What had actually happened: the import's
  `embed_and_index` was holding the worker's event loop, the question's
  workflow logged a `WORKFLOW_TASK_TIMED_OUT`, and it could not even be
  *queried* — the paid plane collects an answer with a Temporal query, which a
  blocked worker cannot serve. The signature to recognise is
  `last_heartbeat` unset on a pending activity that is demonstrably working.
  `learn_profile`, `correct_text`, `embed_and_index`, `build_evalset`,
  `evaluate_index` and `propose_tuning` were all `async def` around a
  synchronous, network-bound engine call; all six now `await asyncio.to_thread`,
  and three carry a test that measures the loop directly.
  **`embed_and_index` needed more than a `to_thread`, and the reason is worth
  keeping.** Its heartbeat callback could not simply move into the thread:
  temporalio installs its thread-safe heartbeat wrapper only for *sync*
  activities run in an executor, because "heartbeat calls internally use a data
  converter which is async so they need to be called on the event loop". For an
  `async def` activity `activity.heartbeat` is loop-bound. So the counting and
  the sending are split — the embedding thread records progress, a loop-side
  task sends it every `HEARTBEAT_INTERVAL`. Before this the stage recorded its
  heartbeats faithfully and never flushed one, because the call that recorded
  them was holding the loop that had to send them; the comment in
  `workflows/ingest.py` had adopted that as a fact, calling `extract_semantics`
  "the only activity that heartbeats". It is now true that both do, so a
  `heartbeat_timeout` could be set on `embed_and_index` — deliberately not done
  here, because arming a timeout on a stage that spends is a decision to make
  on purpose and measure.

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

### A verification trap: cargo can skip the file you just restored

Found 2026-09-16, the Rust twin of the CPython trap below and from the other
direction. Restoring a source file by `mv`-ing its backup back gives it the
backup's mtime — which is *older* than the last build — so cargo judges the
crate up to date and runs the tests against the reverted code. The restored
fix reported as still failing, which reads as "the fix does not work" and is
the wrong conclusion. `touch` the restored file before re-running. The Python
trap is the same mtime arithmetic with the inequality the other way round.

### A verification trap: CPython can serve you the code you just reverted

Found on 2026-09-03 while checking that each fix in this session was
load-bearing, by the standard method — revert the fix, run its test, expect red,
restore. One case came back green, and the reason was not the test.

A `.pyc` is invalidated by the source's **integer-second mtime and its size**.
The revert under test changed `{2,16}` to `{2,12}` — *the same number of bytes* —
and the revert and the restore happened inside the same second. So the restored
file matched the cached bytecode's (mtime, size) exactly, and every later run
imported the **reverted** module while `git diff` showed a clean tree and `grep`
showed the fixed source. The suite then failed a test whose fix was demonstrably
present in the file.

Two consequences worth carrying:

- A revert-and-run verification loop must run with `PYTHONDONTWRITEBYTECODE=1`
  (or clear `__pycache__` between cases), or a same-length edit can report a
  false result in either direction.
- When a test contradicts the source in front of you, check what Python actually
  loaded — `module.__file__` is not enough, because it names the right file
  while serving stale bytecode from beside it.

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
- **The ten screens are all mounted at once and only one is shown**, and the
  tab order is the order of the work: `home`, `stack`, `library`, `explore`,
  `graph`, `import`, `channel`, `bucket`, `ask`, `chat`, `transform`.
  `transform` is last because it is the most downstream: it consumes a document
  the rest of the product has already indexed, and produces a file rather than
  an index. Home is the default because
  "what is in here?" is the question a person arrives with — Services was the
  landing screen only for want of anything else. Home and Services own no
  library, so the picker is off both; Home's figures are project-wide, so a
  picker there would offer a choice that changes nothing. Channel is the third
  without one, for a different reason: a channel **is** a library
  (`lib_yt_<channelId>`), so that screen picks a channel and the library
  follows — two pickers over one choice could disagree. Its channel picker
  takes the *same slot* in the top bar, with the sync field beside it
  (`lib/channels.tsx`, the `LibrariesProvider` pattern with a channel in it),
  because the thing you do once per channel belongs in the bar and not in the
  first screenful. **Bucket is the fourth without a library picker and takes
  that same slot**, for the same reason again — a bucket *is* a library,
  `lib_s3_<sha1(bucket/prefix)[:12]>` — with one difference worth knowing:
  registering a bucket takes a name, a prefix, a role ARN and a manifest
  mapping, which is a form and not a field, so the screen holds it and only the
  re-sync lives in the bar. A tab id has to be lowercase letters only: `i18n.test.ts` reads `TABS` with `"([a-z]+)"` and
  would silently skip anything else.
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
