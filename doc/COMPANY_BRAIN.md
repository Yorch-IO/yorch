# Company Brain

A desktop app that turns a pile of company documents into a searchable, cited
knowledge base — and shows you exactly what it is going to do, and what it will
cost, **before** it spends anything.

It wraps the `docagent` engine in `docaget/` (extraction, byte-exact chunking,
LLM correction, hybrid Qdrant indexing, profile learning, retrieval evaluation)
in a Temporal pipeline with an approval gate, a Postgres catalog for provenance
and cost, and a UI that makes the pre-index workflow inspectable.

Local-first: the vector database, the workflow engine, the catalog and every
artifact stay on the machine. Only embedding, text-fixing, OCR and answer
generation leave, to the provider you configure with your own API key.

Full design: `~/.claude/plans/write-a-plan-to-shiny-eclipse.md`.

## Layout

```
docaget/   the docagent engine — unchanged, still has its own CLI and tests
worker/    brainworker: Temporal workflows/activities + the FastAPI control API
app/       Tauri v2 desktop app (Rust shell + React/TypeScript UI)
infra/     Docker Compose stack definition
```

## Status — M0 (walking skeleton) + graph substrate

| Piece | State |
|---|---|
| Monorepo scaffolding | done |
| Compose stack (qdrant, memgraph, postgres, temporal, temporal-ui, api, worker) | **running** — five stock services healthy |
| `brainworker` package, PingWorkflow, control API | **verified end to end** — 39 tests pass |
| Artifact store (payload-by-reference) | done — first M1 piece, needs no running services |
| Worker Dockerfile | **built** — 873 MB, imports verified |
| Memgraph service, schema, templates, projection | **done** — 60 tests, 11 against a live graph |
| Postgres catalog schema + startup migrations | **done** — 8 tests against a live Postgres |
| Gemini provider on ADC (no API keys) | **done** — 22 tests, mocked client |
| Catalog repository (documents, versions, runs, cost) | **done** — 21 tests |
| Ingest workflow + free approval gate | **done** — 18 workflow, 22 activity tests, **run end to end** |
| Paid stages: correction, embedding+Qdrant, semantics | **done and run for real** — one page through all four stages against Gemini Enterprise |
| Gemini Enterprise migration (engine + app) | **done** — endpoint, models and auth verified against the live API |
| Question answering: planner, retrieval, cited answer | **done and run for real** — 34 tests, real Qdrant + Memgraph |
| Tauri commands + UI for ingest, gate, library, ask | **done** — 5 new commands, 3 new screens |
| Profile learning, reuse and application | **done and run for real** — two paid runs, $0.020 |
| Explore screen + 6 graph endpoints | **done** — walked live against the projection |
| Rust shell (ports, stack lifecycle, control client, errors) | done — 13 tests pass |
| React UI (Services, Library, Import + gate, Ask; en/es) | **done** — typechecks, builds, 4 tests pass |
| Tauri config, capabilities, icons | validated by `npx tauri info`; icons generated |
| Tauri app window | **launched** — deb and AppImage bundled; see Linux build below |

### What ran

The four stock services came up healthy, and the worker and control API were
run **on the host** against them — the dev mode the compose overlay is designed
for, which sidesteps needing the worker image built. `POST /ping` started a
workflow, Temporal scheduled it onto the worker, the activity probed Qdrant and
Postgres, and the result came back in 165 ms. Temporal's history for that run is
in Postgres with the full event chain (`WorkflowExecutionStarted` →
`ActivityTaskScheduled` → `ActivityTaskCompleted` → `WorkflowExecutionCompleted`),
which is the durability claim actually demonstrated rather than assumed.

Three defects surfaced only by running it, none of which any test would have
caught:

- `temporalio/ui:2` is not a published tag — that image only publishes full
  semver and `latest`. Both Temporal images are now pinned to exact patches.
- The Temporal healthcheck probed `127.0.0.1:7233` and always failed, even
  though the server was up and serving. The frontend binds the container's own
  IP, not loopback, so the check is now addressed by service name.
- The control API hardcoded `host="0.0.0.0"` with a comment reasoning that
  Docker publishes it to loopback. True in the container, false on a host — and
  running it in host dev mode did put an unauthenticated control plane on every
  interface. It now defaults to `127.0.0.1`, and the container opts into
  `0.0.0.0` explicitly via `BRAIN_API_HOST`.

### Three more found before the shell could launch

These predate the build below, when the Tauri shell still could not compile —
but not everything about it needed a build.
Resolving the dependency graph, reading the plugin's own permission manifests
and running `npx tauri info` are all possible without the system libraries, and
each caught something:

- The capability granted `opener:allow-open-url` alone. That permits *calling*
  the command; the URL scope it is checked against comes from
  `opener:allow-default-urls`, which was missing — so every link would have been
  denied at runtime with nothing visible to explain it. Both are now listed, and
  deliberately not `opener:default`, which would also grant `reveal-item-in-dir`
  that nothing uses.
- `bundle.icon` referenced five files in an empty directory, which fails the
  build. A 512×512 source is now generated and committed as `icons/icon.png`
  with the variants derived from it; the Android and iOS sets `tauri icon`
  also produces were deleted, since mobile is an explicit v1 non-goal.
- A test asserting no translation key goes unused found four. Three of them
  (`error.workspaceNotNative`, `error.composeFailed`,
  `error.controlUnreachable`) exposed a real gap: errors carry a machine-readable
  `kind` precisely so the UI can offer a fix, and the UI was printing the raw
  message instead. It now shows guidance keyed on the kind *and* the technical
  detail. The fourth, `nav.stack`, was speculative — there is no navigation yet
  — and was removed rather than left to rot.

## Prerequisites

| Tool | Notes |
|---|---|
| Docker + Compose v2 | Present: Docker 29.5.2, Compose v5.1.4 |
| `uv` | Installed at `~/.local/bin/uv` |
| Node 20+ | Present |
| Rust stable | Installed at `~/.cargo/bin` |
| Tauri Linux system libraries | runtime libs present; `-dev` half supplied by a local sysroot |

## Was blocked — both now done

**1. The worker image** is built. `docker build -f worker/Dockerfile -t
company-brain-worker:dev .` from the repo root succeeds; the result is 873 MB
and `import docagent, brainworker` resolves inside it on Python 3.13.14.

**2. Tauri's Linux build dependencies.** The recommended fix is still the
one-line `sudo` install, and it is what any other machine should do:

```bash
sudo apt-get update && sudo apt-get install -y \
  pkg-config build-essential curl wget file libssl-dev \
  libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
  libayatana-appindicator3-dev libxdo-dev
```

The local-prefix workaround was previously rejected on the grounds that GTK3 had
no headers and WebKit was absent entirely. That reasoning went stale:
`libgtk-3-dev` is now installed system-wide and `libwebkit2gtk-4.1-0`,
`librsvg2-2`, `libayatana-appindicator3-1` and `libxdo3` are all present as
runtime libraries. Only the `-dev` half was missing, which reduced the sysroot to
**eleven** packages rather than forty:

```bash
cd "$TMP/debs"
for p in libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libsoup-3.0-dev \
         librsvg2-dev libsqlite3-dev libpsl-dev libkrb5-dev krb5-multidev \
         libnghttp2-dev comerr-dev libverto-dev; do
  apt-get download "$p"        # no root required
done
for d in *.deb; do dpkg -x "$d" "$PREFIX"; done
```

Four details make the extracted tree usable, and none are guessable:

- The `.pc` files hardcode `prefix=/usr`, so `-I`/`-L` point at the system tree
  where the headers are not. Rewriting `prefix=`/`libdir=`/`includedir=` to the
  extracted path is correct where `PKG_CONFIG_SYSROOT_DIR` is not — sysroot
  applies to *every* `.pc` including the system ones that must keep resolving
  against `/usr`.
- The `.so` development symlinks in a `-dev` package are relative and dangle once
  extracted outside `/usr`. Repointing each at the real system `.so.N` is what
  lets the link succeed *and* is why the resulting binary needs nothing from the
  sysroot at runtime: `ldd` on it reports no unresolved libraries.
- `libsoup-3.0.pc` carries `Requires.private` on sqlite3, libpsl, krb5-gssapi
  and libnghttp2, and pkg-config resolves those even for a dynamic `--cflags`.
  `krb5-gssapi.pc` is itself a symlink into `mit-krb5/`, shipped by
  `krb5-multidev` and not by `libkrb5-dev` — the least obvious package in the set.
- `apt-get download` aborts the whole invocation if any one name has no
  candidate, so fetch package-by-package or one bad name silently gets you
  nothing.

Caveat that has not changed: the artifacts below were produced against this
sysroot, and the *build* reproduces only on a machine set up the same way. The
binary itself has no such tie — it links the ordinary system libraries, which is
verified rather than assumed.

## Linux build — verified artifacts

Every gate below was run to a real exit code.

| Step | Result |
|---|---|
| `docaget` — `uv sync && uv run pytest -q` | 109 passed, 13 skipped |
| `worker` — `uv sync && uv run pytest -q` | 39 passed |
| `app` — `npm run build` (`tsc --noEmit && vite build`) | clean, 205 kB JS |
| `app` — `npx vitest run` | 4 passed |
| `app/src-tauri` — `cargo test --release` | 13 passed |
| Worker image | `company-brain-worker:dev`, 873 MB, imports verified |
| `npx tauri build --bundles deb` | `Company Brain_0.1.0_amd64.deb`, 4.1 MB |
| `npx tauri build --bundles appimage` | `Company Brain_0.1.0_amd64.AppImage` |
| Binary launch | **first time the window has run** — stayed up 20 s, clean stderr |

Bundles land in `app/src-tauri/target/release/bundle/{deb,appimage}/`. The deb
declares `Depends: libwebkit2gtk-4.1-0, libgtk-3-0` and nothing else.

Two things worth knowing before repeating this:

- `cargo test` in the **debug** profile needs ~4 GB of `target/`, because
  `crate-type` includes `staticlib`. It filled the disk here and failed with
  `No space left on device` from `ar`, not from rustc — an error easy to
  misread as a build defect. `cargo test --release` reuses the release
  artifacts and avoids the second copy.
- The AppImage bundler downloads `linuxdeploy`, `AppRun` and two plugins from
  GitHub at bundle time. It is not an offline build.

## Running

### Host dev mode — no worker image needed

The fastest loop, and what has actually been verified. Only the four stock
services run in Docker; the worker and API run on the host with `uv`, so a code
change is a restart rather than an image rebuild.

```bash
cd infra
mkdir -p workspace/snapshots && touch secrets.env && chmod 600 secrets.env
export BRAIN_WORKSPACE=$PWD/workspace BRAIN_SECRETS_FILE=$PWD/secrets.env
docker compose -p company-brain -f docker-compose.yaml up -d --wait \
  qdrant memgraph postgres temporal temporal-ui

# In two more shells, from worker/. Ports are the host-published ones.
# The Postgres password is generated by the app on first launch and written to
# infra/.env — `brain:brain` is only ever right on a stack the app has never
# started. Read it rather than typing it.
export BRAIN_WORKSPACE_DIR=$PWD/../infra/workspace \
       BRAIN_TEMPORAL_TARGET=127.0.0.1:7333 \
       BRAIN_QDRANT_URL=http://127.0.0.1:6433 \
       BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7788 \
       BRAIN_DATABASE_URL="postgresql://brain:$(awk -F= '/^BRAIN_PG_PASSWORD=/{print $2}' ../infra/.env)@127.0.0.1:5532/brain" \
       BRAIN_SECRETS_FILE=$PWD/../infra/secrets.env
uv run python -m brainworker.entrypoint worker
uv run python -m brainworker.entrypoint api      # binds 127.0.0.1:8000

curl -s localhost:8000/health | python3 -m json.tool
curl -s -X POST localhost:8000/ping | python3 -m json.tool
```

### Full containerised stack

Needs the worker image, which is now built. `COMPANY_BRAIN_REPO_ROOT` makes the app
use `infra/` in place and build from local source instead of pulling.

```bash
cd infra
BRAIN_WORKSPACE=$PWD/workspace BRAIN_SECRETS_FILE=$PWD/secrets.env \
  docker compose -p company-brain -f docker-compose.yaml -f docker-compose.dev.yaml up -d --wait

curl -s localhost:8787/health | python3 -m json.tool   # every service, individually
curl -s -X POST localhost:8787/ping                    # full round trip through Temporal
xdg-open http://localhost:8380                         # Temporal UI

# The desktop app
cd app && npm install
COMPANY_BRAIN_REPO_ROOT=/home/jjimenez/yorch npm run tauri dev
```

Stopping keeps every volume, so vectors and catalog survive:
`docker compose -p company-brain down`

## Tests

```bash
cd docaget && uv run pytest -q   # 142 passed, 13 skipped (engine — must stay green)
cd worker  && uv run pytest -q   # 347 passed, 59 skipped (config, artifacts, workflows,
                                 #                         graph, catalog, provider, ingest)
cd app     && npx vitest run     # 100 passed             (i18n parity + dead keys, the Ask
                                 #                         reducer, radial layout, three screens)
cd app     && npm run typecheck
cd app/src-tauri && cargo test --release   # 47 passed
```

`cargo` needs two environment variables that no shell sets for you. `~/.cargo/env`
is now sourced from `~/.bashrc`; the sysroot is not, and without it the build
fails at `javascriptcoregtk-4.1` with a pkg-config error that reads like a
missing crate:

```bash
export PKG_CONFIG_PATH="$HOME/.local/tauri-sysroot/prefix/usr/lib/x86_64-linux-gnu/pkgconfig"
```

Use `--release`: the debug profile needs ~4 GB of `target/` because `crate-type`
includes `staticlib`, and it has filled this disk before.

**With the stack up it is 406 passed and nothing skipped** — and getting there
needs the Bolt port read off `docker`, not off `infra/.env`:

```bash
BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789 uv run pytest -q
```

The conftest defaults to 7788, which is what `Ports::default()` asks for. A stack
that was already running when the app relaunched holds its ports, so the
allocator handed out 7789 and `.env` records 7788 — the `ports::revalidate`
defect below. Every integration test skips silently against the wrong port, which
is exactly the shape of a suite that looks green because it did not run.

The graph and catalog suites are integration tests. They skip — naming the URL
they tried — when Memgraph or Postgres is not up, and neither wipes anything:
the graph fixture works inside a random version id and tears down only its own
subgraph, and the catalog fixture migrates a throwaway `search_path` schema. A
`MATCH (n) DETACH DELETE n` in a fixture is one wrong environment variable away
from deleting the user's library.

### The engine test corpus

`doc/CLAUDE.md` claims 105 tests and `doc/RUNBOOK_INDEXACION.md` claims 118. On
a fresh checkout the real number was **90 passed, 31 skipped**: every skip gated
on `../sociologia/output_corrected_peluquiado.txt`, the Go pipeline's output,
which is not in this repo. Twenty of those skipped tests guard invariant #1 and
the eval anchoring — the safety net for the planned changes to `bm25.py` and
`correct.py`. They were silently doing nothing.

`tests/corpus.py` now resolves a corpus: the Go reference if present, otherwise
the largest `libros/**/*.corrected.txt` of at least 120 KB. The choice is
printed in the pytest header on every run, because a property failure is only
actionable if you know which document produced it. `DOCAGENT_TEST_CORPUS`
overrides it, and an override that is missing or too small fails the session
loudly rather than falling back.

That took the suite to **109 passed, 13 skipped**. The remaining skips are all
explained:

| Skipped | Why |
|---|---|
| 10 in `test_port_fidelity.py` | Asserts counts measured from the Go implementation (328 chunks, kinds 309/10/9). Meaningful against that one file and meaningless against any other — correctly left alone. |
| 2 in `test_adversarial_validation.py` | Marked `reference_corpus`: they assert a specific question pattern *validates cleanly*, which is a fact about a document, not a property of the code. |
| 1 in `test_invariants.py` | The footnote half of invariant #11. This corpus has no footnote chunks; raw PDF extraction often merges footnotes into body text, so there is no separable class to check. |

Three fixes were needed to make the property tests portable rather than
book-shaped. `test_eval_anchoring.py` indexed `a[200]`, which silently required
a document with 201+ chunks, and sampled `a[::8]`, which needed 161+ — both are
now proportional to the corpus. Invariant #11 was split in two, because its
halves have different preconditions: a document must contain question chunks to
check one and footnote chunks to check the other, and many contain neither.

### Invariant #11 checked corpus-wide

Splitting that test raised a real question — does any document produce question
chunks that keep a section path, which `graph.py` logs as "must be 0"? Rather
than leave it open, `build_chunks` was run with default rules over all 38
corrected texts in `libros/`:

**88 question chunks, zero carrying a section path.** The invariant holds
everywhere. The apparent failure that prompted the check was the test's own
precondition, not the chunker: `LOS APOLOGISTAS` yields 70 chunks with no
questions and no footnotes at all, so both halves now skip there rather than
assert against an empty set.

Worth noting from the same pass: `libros/` and `libros/done/` hold
byte-identical copies of nine documents under the same filenames. `doc_id_for()`
hashes the *path*, so indexing both would enter each twice and let them compete
in ranking — which is exactly the `DUPLICATE_CONTENT` case the catalog's
`content_sha256` column is designed to catch.

## Ports

All bound to `127.0.0.1`. Defaults avoid every upstream default so the stack
cannot collide with the `sociologia-qdrant` container in `docaget/`, and each is
bind-probed at first run.

| Service | Port |
|---|---|
| Qdrant REST / gRPC | 6433 / 6434 |
| Memgraph Bolt | 7788 |
| Postgres | 5532 |
| Temporal | 7333 |
| Temporal UI | 8380 |
| Control API (free plane, FastAPI) | 8787 |
| Control API (paid plane, NestJS) | 8788 |

The paid plane sits behind a compose **profile**, so a free, self-managed stack
never starts it:

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml \
  -f docker-compose.adc.yaml --profile paid up -d --build backend
```

All three `-f` matter. Without `docker-compose.dev.yaml` the `--build` is a
silent no-op; without `docker-compose.adc.yaml` anything reaching Vertex fails
with `provider_unavailable` / `DefaultCredentialsError` while `/health` still
shows the provider row green — that row is free and only reports whether a
project id is set, and `POST /provider/probe` is what actually spends and
therefore knows.

It reads its Cognito pool from `infra/cognito.env`, **not** `infra/.env`: the
desktop app rewrites `.env` whole on every launch (`app/src-tauri/src/stack.rs`
builds the body from scratch), so a pool id added there survives until somebody
next opens the app. The file is declared `required: false`, so a free-mode stack
does not need it; a paid start without it refuses to boot naming the variables.

## The `migrate` service

Schema ownership left the API. It used to apply numbered SQL files in FastAPI's
lifespan — which is why `worker` waited on `api` being *healthy*, a worker
waiting on an HTTP server to establish a database fact. Two control planes now
share this catalog, so exactly one thing may own its schema: a one-shot
`migrate` service running `prisma migrate deploy` from the backend image, which
`api` and `worker` both wait for with `service_completed_successfully`. Both
planes then only *check* the version and report it on `/health`.

`restart: "no"`, because a migration that failed must stay failed and visible.
Free-mode stacks run the same step; it costs one extra image pull and removes
the possibility of the two planes disagreeing about the schema.

## The graph substrate

Memgraph 3.12.0, Bolt on 7788, added to the compose stack with the same rules as
every other service: loopback-only, exact patch pin, healthcheck that proves the
protocol works rather than that a port accepts connections. The image ships
neither `bash` nor `nc`, so the `/dev/tcp` probe used for Qdrant is unavailable —
`mgconsole` runs a real query instead, which is the better check anyway.

### Document identity is the path; version identity is the content

One `DocumentVersion` exists per distinct sha256, no matter how many paths point
at it. A duplicate file arrives as a second `HAS_VERSION` edge, not a second set
of chunks, so correction, chunking, embedding and extraction happen once per
distinct content.

This is the fix for an inherited defect that this repository already has a
measurement for: `libros/` and `libros/done/` hold byte-identical copies of nine
documents under the same filenames, and `docagent.doc_id_for()` hashes the
*path*, so indexing both enters each twice and lets them compete in ranking.
`document_version.content_sha256` carries a `UNIQUE` constraint and
`test_two_paths_with_identical_bytes_share_one_version` checks the whole path
against a live graph.

### Memgraph does not enforce read-only, and this was measured

The plan called for "validated read-only Cypher", and the obvious implementation
is a `READ` session. That does not work: against Memgraph 3.12.0 a `CREATE`
inside a `default_access_mode="READ"` session **succeeds**. Bolt's access mode is
a routing hint for Neo4j clusters, not a permission, and Memgraph's role-based
access control is an Enterprise feature this stack does not have.

So the guarantee is structural instead of enforced at runtime, and it is worth
being precise about what holds it up:

- A planner never emits Cypher. It returns a template id from a fixed registry
  plus typed parameters, and those travel as Bolt parameters — no model output
  can become query syntax.
- `queries.validate_template` guards the other direction, the human writing a
  template that deletes or forgets a `LIMIT`. It runs at **import**, so a bad
  template stops the process from starting rather than failing a question.
- The `READ` flag is kept because it costs nothing and becomes real if the
  backend ever gains RBAC. It is belt, not braces, and the docstring says so.

`test_read_session_does_not_enforce_read_only` asserts the *permissive*
behaviour. If it ever fails, that is good news and the comment says what to do.

### What the validator refuses

Writes and reach-out (`CREATE`, `MERGE`, `DELETE`, `SET`, `LOAD CSV`, `CALL` —
Memgraph ships query modules that touch the filesystem), unknown labels, missing
`LIMIT`, unbounded variable-length patterns, parameter mismatches in either
direction, and semantic traversals that do not declare themselves. That last one
is not security: an edge a model proposed and an edge derived from the document's
own table of contents have different standing as evidence, and the UI cannot
label the difference unless the template declares it.

Two false-positive cases are tested explicitly, because a check that rejects
valid queries is one reviewers learn to override: a keyword inside a string
literal or comment (`s.title = 'How to create a graph'`), and `count(*)`, which a
naive scan for `*` reads as an unbounded traversal.

There is a hard `MAX_LIMIT` of 200 clamped over whatever a template asks for.
Unbounded traversal on a densely connected concept graph is the failure mode that
takes the desktop app down, and it needs no hostile input to happen.

### Provider: ADC, no API keys

`brainworker/providers/gemini.py` uses `google-genai` with
`Client(vertexai=True, …)` and Application Default Credentials. There is no key
to store, mount, rotate, or keep out of Temporal history — `BRAIN_GEMINI_PROJECT_ID`
and a model name are the only things a workflow carries, and both are safe in a
payload that persists to Postgres for the whole retention period.

Failures are classified by the fix they need rather than by status code, because
the fixes are unrelated: `provider_quota` is waited out, `provider_forbidden`
needs `roles/aiplatform.user` granted and says so in the message,
`provider_no_credentials` names the `gcloud auth application-default login` that
resolves it, and `provider_refused` is a defect here. Only the first, transport
errors, and 5xx are retried; retrying a missing role only wastes the user's time.

The client is built lazily so a worker on a machine with no ADC starts and
*reports* itself unhealthy instead of crashing before it can say why.

Two checks exist because their absence is silent rather than loud: a batch that
returns fewer vectors than texts is refused (it would pair chunk N with vector
N+1), and a vector of the wrong width is refused at the provider (a Qdrant
collection's vector size is fixed at creation, so the error would otherwise
surface at write time, far from its cause).

## The ingest pipeline and its gate

`IngestWorkflow` runs the free stages, publishes a gate report, and waits for a
person. Everything before the gate — staging, extraction, chunk preview, profile
comparison, cost estimation — touches no paid API, because a preview that cost
money to produce would defeat the point of having a gate.

Two constraints fix the shape, and neither is negotiable:

- **The gate sits before correction, not before embedding.** From
  `docaget/costo.json`: correction $0.0334, eval-set generation $0.0131,
  embedding $0.0033. Correction dominates, which is the opposite of the usual
  assumption about RAG pipelines. `test_correction_is_the_dominant_stage_in_the_estimate`
  fails if an estimate ever ranks them the other way.
- **Correction runs before chunking**, because it changes the text's length and
  every `char_span` is a byte offset into that text. So the previewed chunks are
  *not* the indexed chunks whenever correction is on, and `Preview.chunks_are_final`
  says so rather than leaving the UI to imply otherwise.

The gate is bounded at seven days rather than unbounded. A user may close the
lid on Friday and approve on Monday, but a workflow that waits forever never
reports an outcome and quietly accumulates in the namespace. Timing out is a
*rejection*, not a failure: it costs nothing and leaves the document importable.

Estimates deliberately over-report. `CHARS_PER_TOKEN` takes the low end of the
measured range, because a user who approved $0.03 and was billed $0.05 has been
misled and the reverse has not.

### What running it actually found

Five defects that no test written beforehand would have caught, in the order
they appeared:

- `register_document` created the `run` row naming a `version_id` that its own
  next statement had not written yet. `run.version_id` is a foreign key, so the
  first real ingest died with a `ForeignKeyViolation` pointing nowhere near
  ordering. The run is now created bare and the version attached after.
- The duplicate short-circuit returned before `project_structure`, so the
  catalog held two paths and the graph held one — and the Library screen reads
  the graph. `link_duplicate` now writes the second `Document` node and its
  `HAS_VERSION` edge without re-projecting a single chunk.
- Section ordinals were counted globally rather than per parent, so `1.1` came
  out as `2.1` and its implied parent `(2,)` named a section that did not exist.
  The `CONTAINS` edge was therefore never written and a document with headings
  still had no navigable outline. Visible only by reading a real projected
  outline, which is why the fix ships with `section_tree` as a pure function and
  five tests.
- `total_cost` returned `Decimal` from `SUM()` over bigint — not JSON
  serialisable, and not what the dataclasses downstream declare.
- Artifact recording opened a *pooled* connection per artifact. A pool retries a
  refused connection in the background, so a catalog that was simply down turned
  every best-effort bookkeeping write into a full-timeout stall: the activity
  test suite went from 2 seconds to 99. Best-effort writes are now unpooled with
  a 3-second ceiling, and the fixture points at a closed port on purpose so the
  suite keeps proving the stages do not depend on the catalog.

### Verified end to end

A real document through Temporal, on the running stack:

```
=== GATE (free) ===
chunks: 2   kinds: [('cuerpo', 1), ('preguntas', 1)]
chunks_are_final: False
warnings: ['la corrección cambia la longitud del texto, …']
estimate: [('correction', 0.000309), ('embedding', 3.3e-05), ('semantics', 7.4e-05)]
```

and, for a document with numbered headings, a real outline and real citations
carrying byte-exact spans:

```
   1      nivel 1  1. Del fin principal del hombre
   1.1    nivel 2  1.1 De la regla dada por Dios
   2      nivel 1  2. De lo que las Escrituras enseñan

   Catecismo Menor · 1. Del fin principal del hombre · [33:234]
   Catecismo Menor · 1.1 De la regla dada por Dios · [267:445]
```

Importing the same bytes from a second path produced `already_indexed`, linked
the new path in both stores, and left the chunk count at 2 — the duplicate fix,
demonstrated rather than asserted.

**One honest limitation surfaced by that run.** `docagent.heading_level` only
recognises *numbered* headings without a learned profile, so an unnumbered
all-caps heading like `LIBRO PRIMERO` is prose to it and the document indexes
with no table of contents. It still cites correctly. The gate now says so, since
the alternative is the user discovering it from an empty Explore screen.

## The paid stages

"Paid" means precisely: makes a billed call to Vertex AI against the project in
`BRAIN_GEMINI_PROJECT_ID`. Everything else — extraction, chunking, profile
comparison, catalog, graph structure projection — is local CPU and free.

| Stage | Model | Measured |
|---|---|---|
| correction | `gemini-2.5-flash` | **$0.0334** |
| eval-set | `gemini-2.5-flash` | $0.0131 |
| embedding | `gemini-embedding-001` | $0.0033 |
| semantics | `gemini-2.5-flash` | not yet measured — stated rather than guessed |

**These have never been run against Vertex.** They are implemented and tested
with a mocked provider and a real Qdrant; the first real call will cost money and
should be a deliberate, watched run on one small document.

### Correction reuses the engine rather than reimplementing it

`docagent.correct.correct_paragraphs` takes a `Vertex` — a REST client with an
`x-goog-api-key` header the app deliberately does not have. `providers/adapter.py`
presents the one method that function calls and routes it through ADC.

Adapting matters because `correct.verify()` is a deterministic gate built from
measured rules: a correction that loses a scripture reference, a multi-digit
number or a proper noun, or changes length by more than 25%, is discarded and the
paragraph keeps its original text. So is the per-paragraph cache that survives an
interrupted run — added after one was killed at batch 14 of 19 and threw away
everything it had already paid for. A rewrite would quietly drop both.
`test_it_drives_the_engines_correction_end_to_end` runs the engine's real function
through the adapter with no network, and
`test_it_presents_the_signature_the_engine_calls` fails here rather than
mid-correction if the engine's signature moves.

The adapter deliberately has **no** `embed`: `Provider.embed` checks batch length
and vector width, and a second path would let both checks be bypassed. Image
input raises rather than being dropped, because silently discarding it would
correct a blank page and report success.

### Qdrant point ids come from the content, not the path

`docagent.doc_id_for()` hashes the filename. Point ids here are
`point_id(version_id, chunk_index)` instead, so the same bytes always overwrite
the same points: a re-index converges, a duplicate file adds nothing, and the
activity is idempotent — which Temporal requires of it anyway.
`test_indexing_writes_points_whose_ids_come_from_the_content` indexes twice
against a real Qdrant and asserts the count does not move.

One collection for every library, filtered by payload. A collection's vector size
is fixed at creation, so many collections means many places a dimension change
has to be migrated, and cross-library search becomes a fan-out instead of a
filter. Each payload also carries the graph's `chunk_id` for the same chunk, so a
vector hit can be expanded through the graph with no lookup table.

### Semantic extraction runs one chunk per call

Batching would be cheaper. It is not done, because every relation must carry the
`source_chunk_id` that lets a person check it, and a model given ten chunks
reliably attributes claims to the wrong one. **A relation nobody can verify is
worse than no relation, because it still looks like evidence.** One unparseable
chunk is logged and skipped rather than failing the document: structure and
citations are already projected, so it stays browsable and citable either way.

### Four patterns ported from graphrag and nano-graphrag, 2026-08-21

Both were read from local clones (`microsoft/graphrag` v3.1.2, `gusye1234/nano-graphrag`),
both MIT. Nothing was added as a dependency: both assume OpenAI-shaped clients and
this stack is Vertex/ADC only. What was taken is design, and in two places prompt
text.

**The claim's verbatim quote — `Claim Source Text` in graphrag's
`extract_claims.py`.** Their prompt asks for "all quotes from the original text
that are relevant to the claim" and nothing ever checks them. Here the quote is
matched back into the chunk it claims to come from, tolerating whitespace and
nothing else, and a hit yields `quote_char_start/end` against the corrected
stream. That is the same move `answer._verify` already made for chunk ids, one
level down: **the model supplies the pointer and the code decides whether it
exists.** A miss costs the claim its span and increments a counter that reaches
the run summary, so the degradation is visible rather than silent.

The half this does *not* solve: the span indexes the corrected byte stream, not
the original file, because correction changes the text's length. Opening the PDF
at the cited sentence still needs a mapping nothing computes.

**The claim's state — graphrag's `TRUE | FALSE | SUSPECTED`.** Renamed to what
this corpus needs: `afirma`, `niega`, `atribuido`. The interesting third case here
is not "unverified" but "reported as somebody else's", which is how a theology
text handles a position it is about to reject. A claim with no state is
`sin_estado`, never `afirma` — there is no safe default, because the whole point
is that a rebuttal and an assertion are written with the same words.

**Claims in the answering context — graphrag's local search, which feeds
`Sources`, `Entities`, `Relationships` and `Claims` as separate tables.** Before
this the graph leg only chose *which chunks* were read; a claim carrying
confidence and a source chunk was evidence nobody looked at. Now each fragment in
the prompt carries its claims as `lecturas`, capped at three, with the rule that
the chunk's own text wins any disagreement. Citations still name a `chunk_id`:
one namespace, and `_verify` unchanged.

**Gleaning — nano-graphrag's `_op.py:320`, with the prompt pair both projects
share.** Implemented, configurable, and **off**, which is the part worth
recording. Both projects default to one pass. This pipeline has no measurement of
its own, and the last measurement it took of this stage went against the
intuition: semantics came out *better* with reasoning off, 13 concepts against 8
at 40% of the cost. A pass also multiplies the call count of the stage whose
estimate already missed by 2.6x, so the estimator assumes the worst case (every
round used) and the default stays zero.

### The measurement, 2026-08-21

`Avanzando hacia la madurez.pdf` — 17,819 characters, 16 chunks, correction off,
profile learned on the first leg and reused on the second so both were chunked by
the same rules. The document was imported into a throwaway library and removed
between the legs, because byte-identical files are one `document_version` and the
second leg would otherwise have `MERGE`d onto the first's concepts.

| | 0 passes | 1 pass | |
|---|---|---|---|
| concepts | 57 | 80 | +40% |
| …with a description | 44 | 52 | +18% |
| claims | 79 | 120 | **+52%** |
| …with a verified quote | 78 | 119 | +53% |
| semantic edges | 141 | 212 | +50% |
| **semantics stage** | **$0.1176** | **$0.2001** | **+70%** |

**The default stays at zero, and now for a measured reason rather than an
inherited one.** One pass buys 52% more claims for 70% more money — a marginal
claim costs 12% more than an average one, which is not a bad rate. What is
missing is any evidence that *more claims are better claims*: nothing here can
score whether the extra 41 improve an answer, and the eval-set stage that would
is still a switch with no activity behind it. Turning gleaning on would buy
measured volume against unmeasured quality. That is the same reasoning
`thinking_for("answering")` was settled on, pointing the other way.

**Quote fidelity: 98.7% and 99.2%.** 78 of 79 claims, then 119 of 120, carried a
quote the code located in the chunk it came from — matching on whitespace alone.
This was the number the whole of the verified-quote work rested on and it had
never been measured; a poor rate would have meant rethinking the feature. The one
claim that failed had paraphrased ("Un optimista podría ser un pesimista bien
informado porque dispone de…"). Quote lengths ran 37–717 characters, median 134.

**The claim state earns its place too.** 11 of 79 claims (14%) were not plain
assertions: 10 `atribuido`, 1 `niega`. Before the field, those eleven read
exactly like things the document holds.

**The multi-turn path works against Vertex.** The gleaning leg is the first time
`contents` was sent as a list of `types.Content` rather than a string; every call
returned 200. The SDK warns "Direct use of automatic function calling (AFC) in
`Models.generate_content` is not recommended… use AFC in `Chat.send_message`" —
noted rather than acted on, since nothing here uses function calling.

**Also taken: the threshold, not the summary** — nano-graphrag's
`_handle_entity_relation_summary` only calls the model once accumulated
descriptions pass 500 tokens; below it the concatenation is the answer and is
free. Plus LightRAG's stop-updating rule: past `CONDENSE_SOURCE_CAP` contributing
chunks a concept is left alone, so one that fifty chunks mention is not
re-summarised on every import.

### What the measurement broke: the estimate under-reported again

The gate quoted **$0.0919** for a semantics stage that billed **$0.1176** — 22%
under, in the one direction the figure may not err in. The cause is squarely this
work's: `SEMANTICS_CALL_OVERHEAD` was measured at 350 before the schema grew
`cita`, `estado` and `descripcion` and the system prompt grew the paragraphs
explaining them. All of that is per-call input. Re-measured on this run:
(15,388 − 4,949) / 16 = **652 tokens per call**, and the constant is now 660.

`SEMANTICS_OUTPUT_PER_CHUNK` measured 788 against its 634, and was **left alone**.
Not because one document is one data point — the sharper problem is that the 634
is a weighted mean over 1787 chunks *all extracted before those three fields
existed*, so it describes a schema the pipeline no longer uses and is certainly
low. Raising it to 790 would put the estimate at 2.3x the 3-chunk page that
`test_the_estimate_stays_within_reach_of_the_measurement` is anchored to; trading
a 16% under-report for a 130% over-report is not an improvement. The fix that
file already names — a per-family figure, or a range at the gate instead of a
point — has moved from desirable to overdue.

The gleaning multiplier itself held up: leg B was quoted $0.1990 against $0.2001
billed, and with the corrected overhead it over-reports as it should.

### The fix: a range instead of a point

The overhead was a mistake and was corrected. The output figure was not — it is a
mean, and the paragraph above explains why no single number can cover a corpus
whose documents vary by more than 2x without overshooting the small ones. That
conflict was resolved by stopping the choice: `StageEstimate` carries
`output_tokens_high` and `usd_high`, `Estimate` carries `total_usd_high`, and the
two invariants that could not both hold on one number now bind different ends —
`test_the_estimate_over_reports_every_measured_stage` to the high,
`test_the_estimate_stays_within_reach_of_the_measurement` to the low.

### What the spread actually is, measured over the whole corpus

1.27 came from an earlier reading of 31 documents whose maximum was 801 output
tokens per chunk. Re-measured the same day over **every** semantics run in the
catalog — 37 runs, 1830 chunks, `cost_entry` joined to each run's own
`chunks.jsonl` for the divisor:

| | per chunk | × mean |
|---|---|---|
| weighted mean | 640 | 1.00 |
| p50 | 606 | 0.95 |
| p90 | 788 | 1.23 |
| p95 | 894 | 1.40 |
| **max** | **1175** | **1.84** |

So 1.27 left **3 of 37 runs (8%) above the high end** — the range still
under-reported them. `OUTPUT_SPREAD` is now **1.86**, taken against the 634 the
code multiplies rather than the 640 just measured: 1175/634 = 1.853. Computing it
against the measured mean gave 1.85 and a ceiling of 1173, *two tokens under* the
worst document, and
`test_the_high_end_covers_the_worst_document_in_the_corpus` caught it.

**The old design could not have done this.** A single number at 1.86x the mean is
precisely the "over-reporting wildly" failure the reach test exists to catch. It
is reachable only because that test now binds the low end. This is the payoff of
the range, and it arrived one step after the range did.

The cost: on the measured document the gate quotes **$0.1003 – $0.1658** against a
real $0.1185 — a 1.65x-wide band. That is the honest width of what this corpus
does, and the alternative is a narrower band that is wrong 8% of the time.

The query is worth keeping, because **the ceiling has already moved once** (801 →
1175 as the corpus grew from 31 runs to 37) and will move again:

```sql
SELECT c.run_id, c.output_tokens FROM cost_entry c
  JOIN run r ON r.id = c.run_id
 WHERE c.stage = 'semantics' AND r.state = 'succeeded';
```

divided per run by `wc -l < runs/<id>/chunks.jsonl`, looking in **both**
workspaces — see the defect above about why there are two.

### Why the per-family figure is not buildable yet

The other fix this file has been recommending — a per-document-family output
figure learned alongside the profile — was checked against the data and cannot be
evaluated, let alone built: **1 of 37 runs has a `profile.json` artifact**, and
the `run` table has no profile column. Nothing records which family a run
belonged to, so there is no way to ask whether families explain the 26% spread.

The cheap enabling step is to record it: a `profile_id` on `run`, written where
the profile is resolved. Until then "per-family would be tighter" is a hypothesis
with no data behind it, and the range is what the corpus supports.

On that document the gate now quotes **$0.1003 – $0.1209** where it used to quote
a flat $0.0919. The run billed **$0.1185** — inside the range, near the top, which
is what a range is for. `test_the_range_covers_the_document_that_broke_the_point_estimate`
pins it.

**Looked at before shipping, and it needed it.** Screenshotted at 520 and 760 in
both themes: `white-space: nowrap` on the range — the first instinct, and written
into the stylesheet with a confident comment — pushed the whole estimate table
past the viewport at 520px with the high figure clipped, while the token range
without it broke into three lines with the dash alone on the middle one. Both are
invisible to `vitest`. The fix holds the pair together in the markup instead: the
dash is glued to the low figure with a non-breaking space, so the only break
opportunity is after it. `never lets the dash break onto a line of its own` pins
that, because the property is a decision about a string and does not need a
browser to assert.

The UI renders a range only where there is one: `Cost` in `ImportScreen.tsx`
collapses to a single figure when the ends agree, so the embedding row still reads
as one number. It is exported and tested on its own, for the reason
`lib/radial.ts` gives — the property is a decision about numbers, and asserting it
directly beats rendering the approval screen to find out whether two figures got
joined by a dash. Rust defaults the new fields, so a half-upgraded install loses
the range rather than the approval screen.

### La primera indexación por lotes, y su auditoría — 2026-08-22

37 documentos nuevos en `lib_teologia`, conducidos por un script controlador
externo al repositorio. **$19.75 facturados, 10.941 claims extraídos, 10.870 con
cita verificada (99,4%).** Esa tasa es la tercera medición independiente de la
fidelidad de las citas — 98,7%, 99,2% y ahora 99,4% — y confirma que pedir un
tramo literal y comprobarlo funciona a escala de biblioteca.

La auditoría cruzó el manifiesto contra `run`, `cost_entry` y el `semantics.json`
de cada run. **Los costes cuadraron al céntimo y los claims al uno**: cero
discrepancias en 34 runs comprobados. Lo que no cuadró fue el resumen.

**El resumen afirmaba tres cosas falsas, todas contradichas por el archivo que
estaba leyendo:** que ningún documento superó el techo alto (fueron 8 de 34), que
la factura quedó por debajo del estimado bajo (ocurrió en 22 de 34, y sus propios
totales lo desmentían: $16.25 contra $14.54), y que ningún run cayó en falso
timeout (hubo uno). Además contaba $16.25 de gasto cuando el real fue **$19.75**,
porque ocho runs nunca llegaron al manifiesto — entre ellos uno de $1.81.

La causa no es aritmética: `generate_summary.py` sumaba el manifiesto y publicaba
el resultado como un hecho. **El manifiesto es lo que el script cree que pasó; el
catálogo es lo que pasó.** `infra/audit_indexacion.py` lo sustituye: los totales
salen de Postgres, los claims del artifact, y el manifiesto se contrasta contra
ambos imprimiendo cada discrepancia.

**El falso timeout enseñó algo del motor.** `CONFERENCIA RELACIÓN DEL HOMBRE CON
EL TIEMPO.pdf` y su copia `(1)` son byte-idénticos. La segunda tomó el
cortocircuito `already_indexed` y su run terminó en **90 milisegundos**, sin
compuerta y sin coste — que es exactamente el comportamiento correcto. El script
esperó veinte minutos una compuerta que nunca iba a existir y anotó un fallo. Un
controlador de lotes tiene que contemplar que un run termine sin pasar por la
compuerta.

### Lo que el techo del rango aprendió del lote

Los 8 documentos por encima del techo parecían decir que 1.86 se quedaba corto.
**La primera lectura era equivocada y conviene registrarla**: el contenedor corría
`OUTPUT_SPREAD = 1.27`. El 1.86 estaba en el repositorio y nunca se había
desplegado, porque el gate lee del *image*, no del checkout. Recalculado con 1.86,
los desbordes bajan de 8 a 1.

Ese 1 sí es real: `CONFERENCIAS FYC-86-103.pdf` produjo **1210 tokens de salida
por chunk**, por encima de los 1179 que 1.86 permite. El techo del corpus ha ido
801 → 1175 → 1210, y el último salto llegó dos días después del anterior. La
constante es ahora **1.91**.

La otra pata también se movió, y ésta era la que más pesaba: el overhead de
entrada por llamada, medido en 37 runs, da mediana 678 y máximo 701 contra la
constante de 660 — **32 de los 37 por encima**. Ahora es **710**, puesto sobre el
máximo observado y no sobre la media, porque esa pata es casi determinista (678 a
701 en todo el lote) y ponerle techo no cuesta casi nada en sobre-reporte.

La consulta que produce las dos cifras está más arriba en este documento. Después
de cada importación grande hay que volver a correrla: la constante no se mantiene
al día sola, y esta sección es la prueba.

### Two things the rebuild leg surfaced

**One catalog, two workspaces.** The rebuild of `1.-Doctrina-del-Hombre` failed
with `artifact is missing: runs/…/chunks.jsonl`. The file existed — in
`~/.local/share/io.sek.companybrain/workspace`, which is what `infra/.env` names,
while the running stack was mounted on `infra/workspace` (compose's `./workspace`
default). One Postgres holds rows for runs written under both. Every document
indexed under the other workspace therefore has a `run_artifact` row pointing at
nothing *from where the worker is looking*, and `rebuild` is the verb that finds
out. This is the same shape as the backup warning and the `ports::revalidate`
defect: the app rewrites `.env` on launch, and a stack started by hand does not
have to agree with it. Artifact paths are workspace-relative by design, so
copying the run directory across resolved it and the sha256 verification passed.

**Both were fixed on 2026-08-21, and only one of them was fixable.**

The workspace split is a configuration fact, not a bug in a function: two
workspaces under one catalog is a state the system can be *put into*, and code
cannot decide which of the two the user meant. What it can do is stop lying about
it. `ArtifactStore.resolve` now names the workspace it looked under — the message
used to carry only `runs/…/chunks.jsonl`, which sends the reader hunting for a
deleted file that is sitting in the other tree. And `can_rebuild` stats the file
instead of trusting the `run_artifact` row, so the button is disabled with a
reason rather than failing when pressed. That endpoint's own docstring already
said that was the point — "a button that fails when pressed is worse than one
that says why it is disabled" — and it trusted the row anyway. The same edit
replaced a literal `"chunks"` there with `REQUIRED_ARTIFACT`, the constant whose
comment says it exists so the probe and the loader cannot drift apart.

The orphan claims turned out to be worse than debris. `claims_about_concept`
matched on `ABOUT` alone, so **all 214 were visible, across 155 concepts** —
"Jesús", "Pablo", "Job" among them — each rendered with a `source_chunk_id` the
Explore screen offers as "check the source" and which resolves to nothing. That
is precisely the thing this product exists not to do. The template now requires
`DERIVED_FROM`, which is how `claims_for_chunks` always reached them, so the read
surface cannot show unverifiable claims however they got into the graph. On
"Jesús" that is 116 claims before and 115 after.

The nodes are still there. Deleting them is a separate decision about real data,
and it is no longer urgent now that nothing surfaces them. The removal path that
leaves them behind is not the current one: `_DELETE_VERSION` deletes claims
before chunks, and the two removals this session performed reported deleting all
79 and all 120 of theirs.

**The replay repaired drift nobody had noticed.** Before: 144 `Claim` nodes on
that version. After: 153 — exactly the count of distinct `(chunk, text)` pairs in
`semantics.json`, which holds 154 rows of which two collapse to one `claim_id`.
So the graph had been nine claims short of the artifact it came from, and the
replay closed the gap for $0.0015. Separately, the graph holds **214 `Claim` nodes
with no `DERIVED_FROM` edge to any chunk** — orphans predating this session's
removals, which reported deleting all 79 and 120 of theirs. Nobody has looked at
where they came from.

### The last ported idea: a claim relates two concepts

graphrag's claims carry a `subject` and an `object`. Ported on 2026-08-21 as an
`INVOLVES` edge from the `Claim` to a second `Concept`, and the adaptation matters
more than the transliteration: graphrag's frame is "an entity did something
affecting another entity", which is investigative journalism. A theology text
relates two ideas. So the field is `relaciona` and the prompt says what it is not
— *"solo lo rellenas si el texto enuncia esa relación, no si los dos conceptos
simplemente aparecen cerca"* — because the one thing this extractor must not do is
invent a relation.

**This is the graph's only concept-to-concept path.** Before it, two concepts were
connected only by a chunk that mentioned both, which is co-occurrence.
`claims_between_concepts` traverses it in either direction and returns the claim
with its verified quote, so a relation between two ideas arrives with the sentence
that supports it.

Three second-order consequences, all handled:

- **Orphan collection.** `_CANDIDATE_CONCEPTS` walked `MENTIONS` and `ABOUT`. A
  concept reached only through `INVOLVES` would have survived its last supporting
  chunk — the exact hole claim-only concepts fell through before `ABOUT` was added
  there. The test for it fails when the new clause is removed; that was checked by
  removing it.
- **Self-relations are dropped.** A claim relating a concept to itself is the
  model restating `concepto`, and it would put a loop in front of every traversal.
- **A claim template contributes its chunks.** `_by_template` read only `id`, and
  a claims template puts the *claim's* id there. The planner could have chosen
  `claims_between_concepts` and got nothing back; it now reads `source_chunk_id`
  as well.

The edge is additive: claims already in the graph simply have none, exactly like
`quote` and `status`, so nothing needs migrating.

### One defect this work surfaced

`answering/retrieve.py::_expand` returned early when the planner chose no
template and named no concepts — and that early return skipped
`_attach_citations`. Since `answer._verify` drops any citation whose chunk has no
locator, **a question the vector index had answered perfectly well came back as
"the model cited nothing verifiable".** The planner's decision is supposed to gate
the *expansion*, not the citation lookup.

Verified rather than reasoned about: the regression test was run against
`git show HEAD:...` of the module and fails there, passes on the fix.

### Retry policy differs for paid stages

Free activities get three attempts; paid ones get two. Every attempt spends real
money, and the provider already retries the transient failures internally — so a
Temporal retry means the *whole stage* runs again. Timeouts are hours rather than
minutes, because a measured correction batch of 22,946 chars took 55.8 s and a
book is many batches.

### The second gate

Optional, off by default, and it sits between correction and the remaining spend
— correction is the stage that rewrites the user's text, and its result is the
one a person may reasonably want to see before paying to embed it. Rejecting
there still reports the money correction already spent, because it was spent.

`total_usd` distinguishes `0.0` from `None`. Zero means the run was free, which
is true of a structure-only run; None means the models had no known price.
Conflating them would show "not priced" for a run that genuinely cost nothing.

## Gemini Enterprise

### `vertexai=True` was never the problem

`Client(vertexai=True, …)` addresses `aiplatform.googleapis.com`, which *is*
Gemini Enterprise Agent Platform. The flag keeps the SDK's older product name;
the service it reaches is the current one. `brainworker/providers/gemini.py` was
therefore already on the right endpoint and needed no migration.
(`yorchio-agent/src/providers/gemini.py` states the same thing in its own
docstring, and is the reference this followed.)

### What actually was legacy: the engine's REST client

`docagent/vertex.py` sent an `x-goog-api-key` header. Agent Platform **refuses
API keys outright** — `401 UNAUTHENTICATED / CREDENTIALS_MISSING, "API keys are
not supported by this API"`, verified by `yorchio` on 2026-08-11 with an
unrestricted key on the request Google's own docs print. The refusal comes from
the service, not from a key restriction, so there was nothing to configure
around: the engine could not talk to the endpoint at all.

It now uses ADC. The token is attached per request rather than baked into the
client's headers, because an OAuth token expires roughly hourly and an indexing
run outlives one. Passing `api_key=` now raises immediately with the fix in the
message, so a stale call site fails at construction instead of with a 401 three
stages into a paid run.

Note the constraint that shaped this: `brainworker` depends on `docagent` as a
path dependency, so the engine cannot import the worker's provider without a
cycle. The *mechanism* migrated, not the class.

### `LOCATION` moved from `us-central1` to `global`, and that is load-bearing

Gemini 3.x publishes **only** to the global endpoint. On `us-central1` every 3.x
id answers 404 while the 2.5 family answers 200 — so the old regional constant
silently pinned the engine to Gemini 2.5 no matter which model id it was given.

### Models, from listing the live endpoint

Listing on 2026-08-19 with the project's own credentials returned 23 ids.
`gemini-embedding-001` — what the engine used — **is not among them.** The only
embedding model served is `gemini-embedding-2`.

| | was | now |
|---|---|---|
| generation | `gemini-2.5-flash` | `gemini-3.5-flash` (matches `yorchio`) |
| embedding | `gemini-embedding-001` (not served) | `gemini-embedding-2` |
| dimensions | 3072 | **3072** — measured, not assumed |

The width is not exposed by `models.get`, so it was measured with one real
embed call. It being unchanged is the only reason existing Qdrant collections
survive the model change: a collection's vector size is fixed at creation.

### Two defects the live API exposed

**Embedding tokens are not on `usage_metadata`.** It is `None` for embeddings;
the counts live per embedding on `statistics.token_count`. This is the engine's
inherited invariant #7 naming two places instead of one, and the app was reading
only the first — reporting every embedding as free.

**Batching an embedding request silently drops texts.** A request carrying four
texts returns **one** embedding and no error. That is invariant #6, established
for `gemini-embedding-001` and still true for its replacement. The app's
`EMBED_BATCH = 16` would have failed every embedding stage — loudly, because of
the length check, but failed. `Provider.embed` now issues one request per text
and takes its throughput from a six-way thread pool, as the engine already did.

### Prices are gone rather than wrong

The gate multiplied measured tokens by 0.15/0.15/1.25 — `gemini-2.5-flash`
rates. Those are not the rates of the model now in use, and that figure is the
one a user approves spend against, so a plausible wrong number is strictly worse
than none. `PRICES_PER_MILLION` is now an empty table keyed by **exact model
id**; `price_for()` returns `None` for anything absent, and the gate shows
measured token counts with "sin precio". The engine's ledger works the same way
and still prices `gemini-2.5-flash` and `gemini-embedding-001`, which are the
models those numbers were actually checked against.

Keying by exact id rather than family prefix is what prevented the migration
from producing confident nonsense: `startswith("gemini-embedding")` would have
applied the old rates to `gemini-embedding-2` without a word.

To restore dollar figures, add a dated, sourced entry to `PRICES_PER_MILLION`
and update the estimate tests — `test_no_stale_price_table_is_shipped` exists to
make that a deliberate act.

## The first real run

2026-08-20. `docaget/libros/Semana 1 Qué hace feliz a la gente.pdf` — one page,
1514 characters of Spanish prose — through the whole pipeline with correction,
embedding and semantic extraction all enabled. This is the first time any paid
stage has touched the API.

It worked end to end: 2 chunks, 2 Qdrant points, 2 citations, 7 concepts, 10
claims, 19 semantic edges, version activated.

**Correction behaved conservatively, which is what it is meant to do.** Five
paragraphs, one changed, zero rejected by `verify()`, zero omitted by the model.
The single change moved a full stop outside a closing quotation mark — a real
Spanish orthotypographic fix and nothing more. No hallucination, no rewriting.

**Semantic extraction returned usable knowledge.** Concepts came back typed
("felicidad" / Estado, "Jesucristo" / Persona, "bienaventuranzas" / Concepto
teológico) at 0.90–0.95 confidence, and every claim carried the `source_chunk_id`
of the chunk that produced it.

### What the run broke: the estimate under-reported

| stage | estimated in | real in | estimated out | real out |
|---|---|---|---|---|
| correction | 420 | **924** | 420 | 443 |
| embedding | 420 | **342** | 0 | 0 |
| semantics | 420 | **990** | 63 | **526** |

Two of three under-reported, and the docstring on that function promised the
opposite: *"a user who approved $0.03 and was billed $0.05 has been misled; the
reverse has not."*

The cause is a whole cost category the estimator did not model. It projected
tokens from character count alone, and a generation call also pays for its
system instruction, its response schema and its JSON envelope — **per call**. So
the miss grows with the number of calls, and semantic extraction makes one call
per chunk by design. Its output model was worse still: 15% of input, against a
reality of ~263 tokens per chunk, an 8x miss.

Now modelled explicitly: `CORRECTION_CALL_OVERHEAD` (600) and
`SEMANTICS_CALL_OVERHEAD` (350) multiplied by the call count,
`SEMANTICS_OUTPUT_PER_CHUNK` (300), and correction output at 1.3x rather than
parity. All three stages now over-report by 8–23%, which is the required
direction. `test_the_estimate_over_reports_every_measured_stage` pins the real
figures so a regression fails loudly, and its companion asserts the estimate
stays within 2x — over-reporting wildly would push a user to decline affordable
work, which is the same harm mirrored.

**One document is one data point.** These constants are the best numbers
available, not a characterised model, and a second document may move them.

### Measured, incidentally: 4.43 characters per token

1514 characters reported 342 embedding tokens. `CHARS_PER_TOKEN` stays at 3.6 —
deliberately conservative, giving a 23% margin on the text component.

### Prices, sourced 2026-08-20

Google's own Agent Platform pricing page truncates rather than serving its
tables, so these come from third-party aggregators that agree with each other —
the same second-hand provenance the engine's ledger has always carried, and the
same warning: **the token counts are measured, the multipliers are not.**

| model | input /M | output /M |
|---|---|---|
| `gemini-3.5-flash` | $1.50 | $9.00 |
| `gemini-3.6-flash` | $1.50 | $7.50 |
| `gemini-embedding-2` | $0.20 | — |
| `gemini-2.5-flash` | $0.15 | $1.25 |
| `gemini-embedding-001` | $0.15 | — |

`gemini-embedding-2` is priced from listings naming `gemini-embedding-2-preview`;
the endpoint serves the non-preview id and no separate figure was published.
Flagged in the table rather than hidden.

**What that run actually cost:** correction $0.005373, semantics $0.006219,
embedding $0.000068 — **$0.011660** for a 1514-character page. The estimator now
over-reports it by 17% overall (14–23% per stage), which is the required
direction.

### Two premises the prices revised

**Correction against embedding went from ~10x to 79x.** On `gemini-2.5-flash` it
was $0.0334 against $0.0033. On the new models the same page gives $0.005373
against $0.000068: generation output got much more expensive while embeddings
stayed cheap. The gate belongs before correction more firmly than when that call
was made.

**Semantic extraction is now the largest single stage** — $0.006219 against
correction's $0.005373. The plan's "correction dominates" was measured before
semantic extraction existed. It still holds against embedding; it no longer holds
against semantics, which pays a system prompt once per chunk by design.

`test_correction_dominates_embedding_by_two_orders_of_magnitude` and
`test_semantic_extraction_is_now_the_largest_single_stage` pin both.

### PDF works

Incidentally the first PDF through the pipeline: PyMuPDF extraction, one page,
no OCR needed, byte-exact spans preserved into the citations. The remaining
formats — DOCX, PPTX, XLSX, CSV — are still untested end to end.

## gemini-3.6-flash, and the thinking tokens underneath it

The default moved from `gemini-3.5-flash` to `gemini-3.6-flash` for its output
price — $7.50 per million against $9.00, on a workload whose bill is dominated by
output. It is a deliberate divergence from `yorchio`, which stays on 3.5-flash,
so the two products no longer share a model and a regression seen in one may not
reproduce in the other.

Verifying that switch turned up something much larger.

### Reasoning tokens are billed as output and were not being counted

Gemini 3.x reasons before answering and reports it as `thoughts_token_count` —
which `candidates_token_count` **excludes** and which is billed at the output
rate. The provider read only the visible half.

Measured on a trivial prompt, 2026-08-20:

| model | in | visible out | thoughts | total |
|---|---|---|---|---|
| `gemini-3.6-flash` | 8 | 2 | **125** | 135 |
| `gemini-3.5-flash` | 8 | 2 | **110** | 120 |
| `gemini-3.6-flash`, `thinking_budget=0` | 8 | 2 | — | **10** |

So the first real run's reported $0.011660 was wrong by roughly 4x. `Usage` now
carries `thinking_tokens` as a *subset* of `output_tokens`, so the bill is right
and the reasoning share is still explainable.

A second hazard from the same source: **`max_output_tokens` is measured against
reasoning too.** At 16 tokens on `gemini-3.6-flash` the budget was consumed
entirely by thinking and the call returned `finish_reason=MAX_TOKENS` with *no
text at all*. Never set it near the expected answer length.

### The A/B, on one real page

`Semana 2 Cómo puedo dejar el hábito…pdf`, ~1918 characters, 3 chunks, same
input both ways (the per-paragraph correction cache was cleared between runs, or
the second pass would have been free and identical).

| | reasoning on | reasoning off |
|---|---|---|
| correction | 3184 out · $0.025349 · 2 paragraphs fixed | 560 out · $0.005728 · **3 fixed** |
| semantics | 2682 out · $0.022235 · 7 concepts, 6 claims | 1015 out · $0.009730 · **10 concepts, 11 claims** |
| **total** | **$0.047584** | **$0.015459** — 68% less |

Reasoning cost three times as much and extracted *less*. Zero corrections were
rejected by `verify()` either way.

**"More" is not verified to mean "better."** Ten concepts against seven could be
better recall or could be noise; nothing here measures extraction precision, and
saying so is the point. What is not in doubt is the price of the choice.

`BRAIN_THINKING_BUDGET` exposes it, and `Gemini.thinking_budget` defaults to
`None` — the model's own behaviour — so the switch changed nothing silently.

### A design premise turns out to be conditional

The plan says correction dominates. Measured on `gemini-2.5-flash`, before
semantic extraction existed and before thinking was counted. With correct
accounting the ordering **depends on the reasoning mode**:

* reasoning on — correction $0.0253 > semantics $0.0222. Correction reasons far
  harder per call (5.7x its own output against 2.6x for extraction), which more
  than offsets extraction's per-chunk calls.
* reasoning off — semantics $0.0097 > correction $0.0057. With reasoning gone,
  the call count decides, and extraction pays a system prompt per chunk while
  correction batches.

Either way both dwarf embedding, which is what the gate's position actually
rests on. An earlier note in this document claiming "semantic extraction is now
the largest single stage" was true only of the reasoning-off case, and is
corrected here.

### The estimator, recalibrated again

It now branches on the reasoning mode, with per-stage multipliers measured from
the A/B (correction 6x, extraction 3x — a single multiplier covering correction
would over-report extraction by more than double). Both modes over-report by
17–48%, inside the 2x ceiling the tests enforce.

## Answering a question

`POST /ask` → plan, retrieve, compose. Synchronous and deliberately *not* a
workflow: a question is interactive and short-lived, and durability buys nothing
when the remedy for a failure is to ask again. Ingestion is the opposite case,
which is why it is a workflow and this is not.

### Three outcomes, told apart on purpose

| state | means | what the user should do |
|---|---|---|
| `answered` | at least one **verified** citation | read it, click through |
| `insufficient_evidence` | the corpus was searched and does not support an answer | rephrase, or index more |
| `off_corpus` | nothing cleared the similarity floor at all | ask a different library |

Collapsing the last two would send the user to the wrong fix. The floor is
`Qdrant.topicality_gate`, inherited from the engine: `min_score` may only ever be
applied to the dense prefetch (invariant #8), so without a separate gate a
nonsense question still returns purely lexical BM25 matches and the answer step
would dutifully cite them.

### The planner never writes Cypher

It is shown the template *catalogue* — id, summary, parameter names and types —
and returns one id plus typed parameters, which `queries.bind` validates before
anything reaches the database. That is the entire security model for question
answering, because Memgraph does not enforce read-only.

Every way a planner can go wrong degrades to vector-only retrieval rather than
failing the question: an unknown template, Cypher in the id field, unbindable
arguments, an unparseable reply, or a quota error. A question answered from
similarity alone is worse than one answered with graph context, and far better
than an error. The `confidence_floor` is overridden by the caller's policy
whatever the planner asked for — a model requesting 0.0 is requesting to answer
from relations nobody vetted.

### No answer is returned without a citation the code verified

The model cites by chunk id, and **every id it returns is checked against the
evidence it was actually given**. An id that was not in the prompt is a
fabrication — the most dangerous failure this product can have, because it looks
exactly like a grounded answer — so it is dropped, and an answer left with no
surviving citations is downgraded to `insufficient_evidence` rather than
returned with a warning. A citation whose chunk has no locator is dropped too: a
citation the user cannot open is not a citation.

Retrieval order follows from the same concern. Vector search runs first and
decides whether the question is about this corpus at all; the graph then adds
context around what was found. Running the graph first would let a confidently
wrong concept match pull in chunks for a subject the corpus never discusses, and
the answer would cite real documents for it.

### Verified against the real stores

Asked of the two indexed pages, on the running stack:

```
PREGUNTA: ¿Qué hace feliz a la gente según estos documentos?
  estado: answered   plan: busqueda, sin plantilla, conceptos=['felicidad']
  · La mayor felicidad procede de servir a Dios, conocerlo…
      → Semana 1: ¿Qué hace feliz a la gente? · [525:1515]
  · Las personas se sienten felices cuando se entregan a Dios…
      → Semana 1: ¿Qué hace feliz a la gente? · [41:523]

PREGUNTA: ¿Cuál es la capital de Mongolia?
  estado: off_corpus   plan: fuera_de_alcance
  motivo: ningún fragmento supera el umbral de similitud
```

The off-corpus question never reached the answering model, so it cost the
planning call and nothing more.

**One defect that run exposed:** the model wrote chunk ids into the prose it
returned — `"…entregarse a Dios [chk_2bf13bd98fd6091aeb6e9ce2, chk_affd…]."` The
prompt now forbids it *and* `_clean()` strips any that survive, because "the
model mostly complies" is not a property a user-facing string can rest on.

### What a question costs

$0.022946 for the answered one: planning $0.006665 (1031 in / 654 out) and
answering $0.016281 (816 in / 2036 out). Reasoning dominates both — the planner
spent 654 output tokens choosing a template id from a fixed list. That is the
single strongest argument yet for `BRAIN_THINKING_BUDGET=0`, and it is still at
the model default because changing it is the user's call, not a silent one.

### Retrieval tests use the real index without spending

The question's vector is taken from a point already in Qdrant rather than
embedded, which makes the search genuinely end to end — real index, real RRF
fusion, real filters — while costing nothing and staying deterministic. A seeded
random vector stands in for an off-corpus question.

The fixture insists on a point that exists in **both** stores. The collection
also holds points written by activity tests that never projected a graph, and a
fixture choosing one of those made the citation test fail for a reason that had
nothing to do with retrieval.

## The app can finally drive the pipeline

Until now Rust proxied only `health` and `ping`, so nothing built since M0 was
reachable from the window. Five commands close that: `ingest_start`,
`ingest_gate`, `ingest_approve`, `library_documents`, `ask`. Each is an explicit
`#[tauri::command]` rather than a generic pass-through — that is what lets the
webview keep `default-src 'self'` with no localhost exception.

New endpoint behind it: `GET /libraries/{id}/documents`, with `include_absent`
off by default. A file that disappeared keeps its history but should not clutter
the shelf; turning it on is how a user finds a document whose folder was
unmounted, which is otherwise indistinguishable from one never imported.

### Three screens, and what each refuses to hide

**Import** runs the free stages and stops at the gate. It renders
`chunksAreFinal` as a warning rather than a footnote — with correction on, the
previewed chunks are *not* the indexed ones — and shows profile-collision
warnings in full. The price caveat travels with the estimate on screen, not in a
tooltip.

**Ask** renders the three answer states differently, with three different border
colours, because they have three different fixes. An `answered` shows its
verified citations as a numbered list with the locator under each claim.

**Library** strikes through documents whose file is missing rather than dropping
them, and distinguishes "indexed" from "not yet active".

`Money` is a component rather than a format call: `usd === null` renders "not
priced" in italics, and there is no code path that formats a null as `$0.00`.
That figure is what a user approves spend against.

### A defect the Library listing exposed

A duplicate document showed as "not yet active" forever. The ingest short-circuit
returns before `activate_version`, so a second path to already-indexed content
was linked to a fully indexed version and never activated — a second copy on the
shelf that could never be answered from. `link_duplicate` now activates it in the
catalog as well as writing the graph edge.

### The i18n dead-key test earns its keep again

Both bundles gained ~60 keys. The suite enforces parity, matching interpolation
placeholders, and that no key goes unused — the last one via a prefix heuristic
for keys reached dynamically (`t(\`gate.stages.${s.stage}\`)`). Worth knowing
before adding a key that is only ever built at runtime.

**Not verified:** the window itself was not launched. `tsc`, `vitest`, `vite
build` and `cargo test --release` all pass, and the endpoints were exercised
directly against the running stack, but nobody has clicked these screens.

## Reasoning, per stage, decided by A/B

`thinking_budget` is now per stage, because the right answer differs by stage
rather than by product. Reasoning tokens are billed at the output rate, so this
is the largest cost lever here.

| stage | budget | why |
|---|---|---|
| planning | **0** | picks an id from a fixed list. Classification, not judgement — and it was measured spending 654 output tokens to do it |
| correction | **0** | orthotypographic fixes, already gated by `docagent.correct.verify()`. Reasoning pays twice for a guarantee the deterministic check gives |
| semantics | **0** | decided by measurement — see below |
| answering | **on** | decided against a neutral measurement — see below |

`config.Gemini.thinking_for()` resolves the engine's own stage names too
(`stage="correct"` → `correction`), because a silent miss there would leave
correction paying for reasoning while the config said it was off.

### Extraction measured *better* without reasoning

Same page, same input, both modes:

| | with reasoning | without |
|---|---|---|
| cost | $0.028549 | **$0.011239** |
| concepts | 8 | **13** |
| claims | 9 | **13** |

The extra concepts were real entities in the text — "templo de Dios", "malos
hábitos", "deseo ansioso" — and the claims came back more granular, which suits
a citation better than one compound assertion. Every edge carries a confidence
and the floor filters the weak ones, so the downside is bounded. Turning it off
is not a cost compromise here; it measured better on both axes.

### Answering: no measurable benefit, at 3.3x the price

Same question, four attempts each:

| | answered | citations (mean) | cost per answer |
|---|---|---|---|
| with reasoning | 4/4 | 3.5 | $0.015561 |
| without | 4/4 | 3.5 | ~$0.004745 |

One earlier attempt without reasoning did fail — the model claimed an answer and
supported it with nothing verifiable, so the citation guard downgraded it to
`insufficient_evidence`. **That is 1 failure in 5 against 0 in 5, which is not a
difference this sample can support**, and it is recorded as an observation rather
than a finding. It did demonstrate the guard working on a real failure.

**Kept on, decided against the measurement.** The numbers are neutral; the choice
is a deliberate bias toward caution in the one stage where being wrong is worst —
answering is where the product either cites the corpus or fabricates. Anyone
revisiting this should know it was settled *despite* the figures, not because of
them, and `test_reasoning_stays_on_for_answering_by_choice_not_by_measurement`
carries that reasoning where it will be read.

It stays a fallback rather than an entry in `stage_thinking`, so
`BRAIN_THINKING_BUDGET=0` still reaches it: someone turning off every reasoning
cost means it. `BRAIN_THINKING_ANSWERING` overrides either way.

### What this is worth on the real corpus

Extrapolated with the measured constants over the 22 documents that have text
(~829,000 characters, ~691 chunks); the full corpus is 58 PDFs, so roughly 2.5x
these figures.

| | reasoning everywhere | as configured now |
|---|---|---|
| correction | $10.63 | $2.13 |
| semantics | $9.34 | $3.93 |
| embedding | $0.04 | $0.04 |
| **total** | **$20.01** | **$6.10** |

Answering is not in that table — it is per question, not per document. At the
configured setting a question costs about $0.023 against roughly $0.008 with
reasoning off. That recurring difference is the price of the caution above, and
it is worth re-examining once there is an eval set to judge answer quality with.

### A hygiene defect the skip counter caught

After this work the suite reported "251 passed, **8 skipped**" where it had
reported 252 passed. The eight were the retrieval tests, skipping with a message
about a missing citation in the graph.

The real cause was three files away: `test_paid.py` wrote its points into the
**real** `brain` collection and never removed them. After enough runs the
answering fixture's 64-point scroll window was entirely test data, and no point
in it had a graph projection. 105 test points against 5 real ones.

The collection name is now a setting (`BRAIN_QDRANT_COLLECTION`), the activity
tests write to a disposable one and drop it, and the leftovers were purged. Worth
noting how it surfaced: not as a failure, but as a *skip count* that had to be
read.

## Profiles: learned, applied, and measured

2026-08-20. Until this ran, `check_profile_collision` computed a family
fingerprint, found the profile matching it, warned that its rules would be
applied — and discarded them. Every document in the product was chunked with
`ChunkRules()` defaults and extracted with no header stripping.

**Reuse is free and automatic; learning is paid and approved.** `resolve_profile`
runs before the gate and never calls the model, so it does not break the rule
that a preview costs nothing. `paid.learn_profile` sits behind the gate with
every other stage that spends.

### The estimator disagreed with the configuration, by 3.8x

The first gate quoted **$0.0765** to learn a profile that then cost **$0.0140**.
`estimate_cost` read only `thinking_budget`, the global fallback, which defaults
to `None` — while `stage_thinking` turns reasoning *off* for correction,
semantics and profile. So the gate applied a 6x reasoning multiplier to three
stages that were never going to reason, in the shipped configuration rather than
in some corner of it.

Resolved per stage now, and the same gate quotes **$0.0203**. Over-reporting is
the required direction, but not by 6x on the stages that dominate the bill: a
user pushed into declining affordable work has been misled exactly as much as one
billed more than they approved.

### Two real runs, and what the first one cost

`CLASE 3 Holiletica a traves de la historia biblíca.pdf` — 26 pages, 285
paragraphs, **no numbered headings at all**, so no table of contents.

Three attempts, all failing `heading_guards` on a chapter sequence of
`[1, 3, 4, 5]`, then a fallback that threw away **everything** — including a
header pattern the validator had confirmed on 26 pages with no body hits, and a
footnote rule an independent signal confirmed at 100%. $0.014031 spent, nothing
kept.

That is the mistake this repository has already paid for once: *"Requiring all
four to pass threw away a header pattern that validated cleanly three times,
leaving 175 running-header lines in the text."* `rules.adopt` is per-rule and
resets each failed rule to its measured default, so the fix is to adopt whatever
survived rather than to discard the lot. **Nothing about a bad length guard makes
a good header pattern less true.** Only a proposal where *nothing* validated
saves no profile — writing one of pure defaults would make the next document of
the family "reuse" it and never try again, freezing it on generic rules.

The model had also declined to propose an unnumbered-heading pattern, reasoning
*"No numbered headings were detected in the document, so heading patterns remain
null"* — reading the absence of numbering as grounds to skip the rule that exists
for exactly that case. The prompt now says so explicitly.

`CLASE 2.pdf` with both fixes: **one attempt, $0.005994**, and four rules
adopted — `header_patterns` (22 pages, no body hits), `footnote_pattern`
(11/257 paragraphs, 91% confirmed), `heading_guards`, `question_pattern`.

Its `heading_l1_pattern` was **rejected**, and correctly: the proposal
`^[A-ZÁÉÍÓÚÑ][a-zA-Z0-9\s¿?().,:;-]{3,70}$` admitted `¿?` among its allowed
characters and so took two review questions along with the titles. A pattern
that swallows prose is worse than no pattern, and the adversarial check is what
refused it.

### The refine round that never happened

But the checker had written precise feedback — *"se está quedando con preguntas
de repaso… excluye las preguntas"* — and **it was never sent back.**
`heading_patterns` sits in `OPTIONAL_RULES`, so failing it does not make
`Validation.passed` false, so no refine round was earned. One attempt, pattern
dropped, document indexed with no table of contents.

That table is right for `question_pattern` and `footnote_pattern`, where the
built-ins were themselves measured and a refine costs real money. It is wrong for
`heading_patterns` on a document that numbers nothing, because there the pattern
is the **only** rule that can produce an outline: failing it is not a minor loss,
it is the whole structural outcome.

So essentiality is now **per document** rather than per rule name.
`Validation.essential` defaults to `ESSENTIAL_RULES`, and `validate` adds
`heading_patterns` when `_numbers_its_headings` finds no numbered level-1 heading
— load-bearing exactly where it is load-bearing, optional where the numeric
detector already covers it. Both halves are pinned by tests.

The prompt was sharpened with the concrete failure too: key on the **absence** of
sentence punctuation, use a *negated* character class, and never admit `.`, `?`
or `¿` among the allowed characters — quoting the rejected proposal so the
guidance is specific rather than plausible.

### The rule learned, and the outcome it was built for

`CLASE 4. Clases de sermones.pdf` — 121 paragraphs, **zero** detected chapters.
First attempt, **$0.004280**, five rules adopted:

| rule | learned | validated against |
|---|---|---|
| `header_patterns` | `^TIPOS DE SERMON$` | 12 pages, no body hits |
| `heading_l1_pattern` | `^(Sermón [A-ZÁÉÍÓÚÑa-záréíóúñ]+\|Introducción)$` | 6 headings |
| `heading_l2_pattern` | `^Sermón Textual Basado en [^.?¿!]{3,60}$` | 3 headings |

The level-2 pattern uses the negated class the prompt recommends, which is the
guidance landing rather than a coincidence. `heading_guards` passed as
*"unexercised; structure comes from the learned pattern"*.

And the outcome, read back off the projection through the Explore endpoints:

```
1      nivel 1  Introducción
2      nivel 1  Sermón Textual
2.1      nivel 2  Sermón Textual Basado en Palabras Claves
2.2      nivel 2  Sermón Textual Basado en Frases Principales
2.3      nivel 2  Sermón Textual Basado en Ideas Fundamentales
3      nivel 1  Sermón Temático
4      nivel 1  Sermón Biográfico
5      nivel 1  Sermón Narrativo
6      nivel 1  Sermón Expositivo
```

Nine sections with correct two-level nesting, where there were none. A sibling
copy then inherited both patterns with **no estimate stages at all**.

**What this does not establish.** Nothing here measures retrieval. The claim is
structural — the outline exists and its chunks carry byte-exact spans — and the
eval-set stage that would score answer quality is still a switch with no activity
behind it.

### A warning that fired on success

Reuse exposed a defect in the collision check added alongside it. It warned
whenever the inherited and default rules disagreed about the chapter count — and
on a successful reuse that is *always*, because the profile finds 6 chapters
where the defaults find 0. **A warning that fires on success is one an operator
learns to click past**, which is exactly how the collision it exists to catch
gets missed.

It now fires only on a contradiction: the defaults must have found structure of
their own and the two must differ. Where the defaults find nothing the profile is
*adding* an outline, which is the entire reason for learning one. That matches
the recorded case, which was "4 chapters inherited vs 1 read" — both non-zero,
and disagreeing.

### Reuse, demonstrated

A byte-modified sibling of `CLASE 2.pdf` at a second path:

```
PROFILE source : reused        slug: clase-2-91d23efb
  header_pats  : ['^INTRODUCCION A LA HOMILETICA$']
ESTIMATE stages: none — nothing to pay for      total: None
chunks         : 85 (against 86 unstripped)     final: True
  collision: comparte huella estructural (91d23efb…) con «clase-2-91d23efb»
  collision: las reglas heredadas detectan 1 capítulo(s) donde las por defecto detectan 0
```

The estimate has **no stages at all** — `total_usd` is `None`, meaning "nothing
to price", not `0.0`. The running header is gone. And the second warning is
`doc/CLAUDE.md`'s recorded fix for a fingerprint collision working: recall cannot
see a wrong-family profile, but the two rule sets disagreeing about the chapter
count can, and a person decides.

**Total spend for the whole verification: $0.024305** across three learning runs.

### What running it found that no test had

- **Temporal maps payloads to parameters by arity.** `extract_text` gained a
  third parameter, the workflow still passed two, and the converter gave up and
  passed raw dicts — `'dict' object has no attribute 'source_path'`, three frames
  from anything naming the cause. Every workflow double took `*_args`, which
  accepts anything; the extraction double is now typed so the call site fails in
  the suite instead.
- **`_Evidence` carried four fields**, the ones `profiles.fingerprint` reads.
  `rules.propose` reads four more, so learning would have died on the first real
  call.
- **Profile-warning recording used a pooled connection** for a best-effort write.
  The same stall already documented for artifacts: the suite went from 40s to
  0.6s once it was unpooled.
- **`heading_guards` is essential and can never pass for a document that numbers
  nothing**, so it blocked adoption of the one rule that would give such a
  document an outline. It now passes unexercised — but only while the learned
  pattern that justifies it survives its own check.

## The Explore screen

Six of the nine graph templates had no caller but the answering planner, which
reaches one only when it happens to pick it. Six endpoints now name them, each as
a literal: **no route takes a template id or Cypher from its caller**, because
Memgraph does not enforce read-only and the guarantee is structural.

Walked live, outline → section → chunk → neighbours → locator:

```
1      nivel 1  1. Del fin principal del hombre
2.1    nivel 2  1.1 De la regla dada por Dios
  before: "El fin principal del hombre es glorificar a Dios…"
  chunk : "La Palabra de Dios, contenida en las Escrituras…"  [267:445]
  after : "Las Escrituras principalmente enseñan…"
  cita  : Catecismo Menor · 1.1 De la regla dada por Dios · [267:445]
```

The screen marks model-proposed results as such. `uses_semantic_edges` exists in
the template registry precisely because an edge a model suggested and an edge
read off a table of contents have different standing as evidence, and until now
nothing acted on the declaration.

Three empty states are stated rather than left blank: a document with no outline
says what causes it, a version with no concepts says extraction was declined
rather than rendering "0", and a chunk with no locator says so — a citation the
user cannot open is not a citation.

### Three defects it exposed

- **`IngestRequest` and `StageOptions` renamed on the wrong side.** They travel
  webview → Rust → Python, the opposite direction from a response, so they must
  rename on *deserialize*. Serde rejected every payload the Import screen sent
  with "missing field": starting an ingest from the window had never worked, and
  nothing caught it because nobody had opened the window.
- **`GateReport` had no `profile` field.** A missing field does not fail — serde
  drops it — so the gate would have reported "no profile for this family" for
  every document, including ones about to reuse one for free.
- **`Graph.query` validates after opening a session**, so a malformed id sent
  while Memgraph was down came back as `graph_unreachable`, pointing the user at
  the stack instead of at their stale link. The endpoints bind first.

## The graph leg was not scoped to a library

Found while verifying `related_documents` for the Explore screen, not by a test —
nothing called `chunks_for_concepts` at all, which is why it survived.

The two retrieval legs disagreed. The vector leg has always filtered
(`retrieve.search` sets `filters["library_id"] = question.library_id`). The graph
leg had no library parameter anywhere: `chunks_for_concepts` took concept ids, a
floor and a limit, and `_hydrate` matched the owning `Document` with
`OPTIONAL MATCH` and read nothing off it.

**Concepts are shared between documents on purpose.** `project_concepts` merges
by canonical name, so two books discussing "felicidad" reach one `Concept` node —
that is what makes `related_documents` work at all. It also means an unscoped
traversal from a concept reaches every library that has ever mentioned it. And
the citation guard would not have caught it: that guard checks a cited chunk was
among the evidence *given*, not that the evidence was in scope. So a question
asked of one library could be answered citing a document from another.

Latent rather than live when found — only one library had had semantics extracted
— but the path was open.

### Fixed in two places, deliberately

- `chunks_for_concepts` gains a required `library_id` and traverses
  `Chunk ← HAS_CHUNK ← DocumentVersion ← HAS_VERSION ← Document`. It still enters
  from the concept ids, which are indexed and selective; the library is an added
  constraint, not the entry point. **Required, not defaulted**: a default would
  mean a caller who forgot the scope silently got a different one.
- `_hydrate` filters too, and that is the half worth keeping. Every path that
  turns a graph result into `Evidence` goes through it — including `_by_template`,
  which harvests chunk ids from whatever template the planner chose — so a
  template added later cannot reopen the hole by forgetting to scope itself. Its
  `OPTIONAL MATCH` became a required one: a chunk with no owning document cannot
  be attributed to a library or to a source, and an answer may not cite something
  whose origin it cannot name.

The library is the **caller's policy, never the planner's**. `planner._validate`
overwrites it exactly as it already overwrote `confidence_floor` — a model naming
a different library is asking to answer out of somebody else's corpus, and that is
not a request worth reading.

### What the tests found

Six live-graph tests, and one earned its keep immediately. The first draft used
the literal concept name "Felicidad" and its **precondition** assertion failed
with `{'lib_real', 'lib_scope_mine', 'lib_scope_theirs'}`: the name already
existed in this machine's real library from an earlier extraction run. Concept
identity is `concept_id(canonical_name)` with no library in it, so a test name
collides with production data — the leak's own mechanism, demonstrated by
accident. The fixture randomises the name now.

Reverted to the leaking version, 4 of the 6 fail. Restored, all 6 pass.

Verified end to end against the running stack, **$0.015793**:

```
estado: answered      3 citas verificadas
  · La mayor felicidad procede de servir a Dios…
      -> Semana 1: ¿Qué hace feliz a la gente? · [525:1515]
  => every cited chunk checked against the graph: {'lib_real'} — in scope
```

and an off-corpus question still costs the planning call and nothing more
(**$0.002302**), so the filter did not turn a working answer into a refusal.

## The provider reaches the containers

Until this, **no paid stage worked from the app window at all.** Compose passed no
`BRAIN_GEMINI_PROJECT_ID` to `api` or `worker` and mounted no credentials, and
`stack.rs` wrote only `BRAIN_PROJECT=company-brain` — the *Compose* project name,
not the GCP one. `POST /ask` from the window answered `provider_unconfigured` and
an ingest would have died at its first paid stage. Every real run had gone through
host dev mode with the environment exported by hand, which is exactly why nobody
had noticed: nobody had opened the window.

### Three pieces, and one of them is a separate file on purpose

**The project id travels in `.env`, not in the secrets mount.** It is not a
credential — this document already says a workflow carries only a project id and
a model name, and that both are safe in a payload persisted for the whole
retention period. Written empty when unset rather than omitted, because compose
warns once per service on an undefined variable and a warning on every launch is
one the user learns to ignore.

**No other Gemini setting is passed.** `brainworker/config.py` owns those
defaults, and repeating them here as `${X:-default}` would put one constant in two
files — which has already drifted once in this repository, in `_stage_thinking`.
A test asserts their absence.

**ADC is a separate overlay, applied only when a credentials file was found.**
Compose creates a *directory* at a bind mount's source when it does not exist, so
an unconditional mount on a machine without ADC would put an empty directory
where the credentials belong and fail at authentication with nothing pointing at
the cause. `compose_args()` appends `-f docker-compose.adc.yaml` the same way it
already appends the dev overlay. Mounted read-only, like the provider secrets
beside it.

`find_adc` consults `GOOGLE_APPLICATION_CREDENTIALS` first and the well-known
gcloud path second — the order the Google libraries themselves use, so a user who
had already pointed the standard variable at a service-account key is not ignored.

### Two things the setter has to get right

The project id is **read back** from `.env` on every launch, exactly as the
Postgres password is: `write_env` rewrites the whole file, so without that a
value set once would be erased by the next launch. And it is **validated**
before it is written — 6-30 characters, lowercase-leading, letters, digits and
hyphens. A typo would otherwise surface as a 403 three stages into a paid run,
and the value goes into `.env` verbatim, where a newline would forge a second
line. `"yorch\nBRAIN_PG_PASSWORD=hunter2"` is in the test table for that reason.

`AppState` **replaces** the cached stack rather than clearing it. Clearing would
send the next caller through `ports::allocate`, whose ports are already taken by
the running stack — so it would allocate a different set and every subsequent
command would address services that are not there.

### What the screen says, and what it refuses to conflate

Two conditions with two different fixes, shown separately: naming a project is a
text field, and obtaining credentials is a command the user runs outside the app.
Gemini Enterprise refuses API keys, so a project id without ADC buys nothing —
and reporting one "not configured" state for both would hide which one is
missing. When ADC is absent the screen names the `gcloud` command.

Saving also says the setting **has not taken effect yet**: compose interpolates
`.env` at `up` time, so a project named while the stack is running reaches the
API on the next `stack_up` and not before. Silently accepting a setting that does
nothing is how a user concludes a feature is broken.

### Verified through the app's own stack

Recreated with the ADC overlay, then inside the container:

```
BRAIN_GEMINI_PROJECT_ID=yorch-platform-prod
GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/adc.json
-rw------- 1 1000 1000 417 /run/secrets/adc.json
```

and the question that used to be refused:

```
POST /ask -> 200   estado: answered   4 citas
  · Semana 1: ¿Qué hace feliz a la gente? · [525:1515]
  $0.013450
```

## Two things the design turns on

**Correction is the expensive stage, not embedding.** From `docaget/costo.json`
(a real measured run): correction $0.0334, eval-set generation $0.0131,
embedding $0.0033. So the free approval gate sits *before* correction, and every
stage is individually switchable at that gate.

**Some defects are invisible to the metrics.** `doc/CLAUDE.md` documents a
profile-fingerprint collision that chunks one document with another family's
heading rules. Recall cannot detect it, because the synthetic eval questions are
generated from the very chunks the wrong rules produced. Defects of that class
become explicit warnings at the gate rather than something the numbers quietly
absorb.

## The Library's three verbs

`doc/PLAN_GRAFO_DOCUMENTAL.md` §6 asks for "permanent removal of versions and
indexes" as an explicit user act, and §UI for "re-index/delete actions". None of
it existed: `Catalog.mark_absent` was there and nothing called it, and there was
no removal anywhere — no endpoint, no Qdrant deletion, no graph cleanup. The
product could only accumulate.

It is three verbs, not one, because they cost three different things:

| Verb | What it does | Measured |
|---|---|---|
| **Eliminar** | Qdrant points, graph subgraph, catalog rows | free |
| **Reindexar** | Re-runs the pipeline through the normal gate | $0.000032 (correction cache was warm) |
| **Reconstruir** | Replays `chunks.jsonl` and `semantics.json` | $0.000032, embedding only |

**Reconstruir is not "Reindexar with correction off."** Correction runs before
chunking because it changes the text's length, so re-running without it yields
different `char_span`s and different chunk boundaries — a silently different
index wearing the same version id. Reproducing what was paid for means replaying
the artifacts, which is why `extract_semantics` now writes a `semantics`
artifact: until this, the most expensive output in the pipeline — one generation
call per chunk — existed nowhere but in Memgraph.

### Projections first, catalog last

Postgres is the source of truth and the other two stores are projections of it.
Delete the catalog row first and crash, and the projections hold points and nodes
that nothing references and nothing can find again to retry — an unrecoverable
leak, and an invisible one, because the shelf looks right. Delete the projections
first and crash, and the catalog still names the document, so pressing the button
again finishes the job.

That ordering is what makes removal idempotent, and idempotence is why it is a
request handler rather than a workflow — the same reasoning `/ask` already uses.
`tests/unit/test_removal.py` proves it by failing between the stores and
asserting the catalog row survived, rather than asserting the order in a comment.

Destructive Cypher is a server-owned literal in `graph/projection.py`, never a
template in `graph/queries.py`. A template id is a string a model can return.

### What running it found

Four defects, none of which a test had caught, all found by measuring the stores
before and after rather than by reading the code.

- **`count()` over an empty match still returns a row.** `remove_version` used
  "did the query return anything" as its existence test and reported removing 1
  version for an id that was never in the graph. Retry after a partial failure is
  the *normal* second pass, so this was the common path.
- **Re-projection under a changed title duplicated every citation.** Found by
  rebuilding a document whose surviving duplicate had a different title: three
  citations became six, and the stale three still answered
  `citations_for_chunks`, which is where an answer's evidence comes from.
  `citation_id` is keyed on `(chunk, locator)` and the locator embeds the title.
  `project_structure` now prunes citations of the chunks it just projected that
  it did not produce. Reverted, the regression test fails 4 against 2.
- **Orphan concepts reachable only through a Claim were never collected.**
  `extract_semantics` names a concept two ways — `conceptos` gets a `MENTIONS`
  edge from the chunk, but a claim's subject gets only `ABOUT` from the Claim.
  Collecting on `MENTIONS` alone left four of them sitting in the live graph.
  The candidate query now follows both, and the two lists are deduplicated in
  Python: a concept reached both ways was counted twice, doubling the number the
  removal reported.
- **The rebuild gate offered "0 fragmentos" for a document with three.**
  `run_artifact` stores a path, a digest and a size but no row count, so the
  `ArtifactRef` rebuilt from it had `rows=None`.

And one that is test hygiene rather than product behaviour: this file's own
suite leaked 55 randomly-named `Concept` nodes into the developer's live graph,
because the leak happens exactly when a test *fails* between creating a concept
and removing it. `conftest._purge` deliberately leaves concepts alone — right for
a real one, which is shared by design — but these carry a random hex suffix and
nothing can ever reach them. They are drained by an autouse fixture now.

### Verified end to end, against the running stack

Removal was exercised **inside a library holding real documents**, which is a
stronger test than an empty one. Removing a 3-chunk document from a library of
seven left the other six byte-identical — 216 chunks across three projected
documents — deleted exactly the 3 chunks, 3 sections, 3 citations and 6 claims
that were its own, and collected exactly the 6 of its 8 concepts that nothing
else mentioned. `Dios` (mentioned by two other versions) and `felicidad`
(mentioned by one, in a *different* library) both survived. The run row and all
three cost entries survived with `document_id` set to NULL.

The duplicate path was exercised too: a byte-identical copy at a second path
produced one version, two documents and **no new points, at zero cost**.
Removing the document that had *indexed* the points deleted nothing and
re-pointed all three payloads to the survivor — the stale `document_id` defect —
and the surviving document still answered with a verified citation. Removing the
last holder took everything: 0 points, 0 nodes, 0 rows, and the same question
then returned `off_corpus` rather than a citation into a removed document.

Rebuild converged exactly: the same three point ids, the same 3/3/3/7/6 graph
counts, for $0.000032.

### A name that looked disposable and was not

The verification first ran in a library called `lib_verify`, chosen because it
read like scratch. It already existed, with six real CLASE documents in it from
earlier profile-learning runs. Nothing was damaged, but it is the same hazard
`test_library_scope` hit with the literal concept name "Felicidad": a plausible
test name collides with production data. The rest of the verification used a
randomised library id.

### Two operational notes

- **The containerised worker creates the workspace subdirectories as root.**
  `inbox/`, `runs/`, `cache/` and `profiles/` under the app's workspace end up
  owned by root, so the host user cannot write to them — which means host dev
  mode cannot share a workspace with a stack that has already run.
- **`ports::revalidate` has no caller.** `AppState::stack()` always calls
  `ports::allocate(Ports::default())`, so relaunching the app while its own
  stack is up allocates a *different* set and writes it to `.env`, orphaning the
  running containers. Observed: `.env` naming 7788/6433/5532/8788 while the
  containers were published on 7789/6435/5533/8787. The compiler already reports
  `revalidate` as dead code.


## The graph screen, redrawn

The picture was already correct and nearly unreadable. Same three tiers, same
three endpoints, same refusal to draw a concept-to-outer-document edge; what
changed is everything about how it reads.

### Nine tokens, because one was missing

`.graph-canvas .node.concept circle` filled with `var(--accent-soft, #dfe7f2)`
and **`--accent-soft` was defined nowhere**, so every concept node painted that
hard-coded light blue in both themes — on `--panel: #1b1e24` in the dark one.
Eight other graph rules carried the same shape of fallback, six of them holding
values from a palette that no longer exists.

The block now defines `--graph-bg`, `--graph-edge`, `--graph-concept-fill`,
`--graph-concept-stroke`, `--graph-doc-fill`, `--graph-doc-stroke`,
`--graph-centre-fill`, `--graph-centre-fg` and `--graph-halo` in both `:root`
blocks, and **there is not one `var(--x, #hex)` fallback left in it**. That is
the actual lesson: a fallback that looks plausible is how a missing token
survives a year of code review. An undefined token should paint wrong.

### The labels were all at y = -14

Every concept label was placed above its node, centred, wherever on the ring
that node sat — so near twelve o'clock they stacked and at three and nine
o'clock they ran back over the node. Labels are now placed by quadrant
(`labelAnchor` in `app/src/lib/radial.ts`): out to the right on the right of the
circle, out to the left on the left, above or below within ~20° of vertical. The
0.35 cosine threshold rather than 0 is what stops a label near the top being
flung sideways by a barely-positive cosine.

Two rings that both started at twelve o'clock put document 0 directly behind
concept 0. The outer ring is now offset by half its own step, which at the shipped
12-and-8 counts leaves a 7.5° minimum gap between any concept ray and any
document ray. Concepts above eight also alternate ±14px in radius, so
neighbouring labels sit on different circles instead of in one horizontal band.

### What the geometry cannot promise, paint order does

A 140×40 axis-aligned card on an arbitrary ray will sometimes reach further than
the angular gap allows, so "documents must not cover concepts" is not a spacing
property. It is enforced by drawing concepts last, and by giving every label a
`paint-order: stroke fill` halo in `--graph-halo` so it stays legible over
whatever passes behind it. `radial.test.ts` still asserts the box arithmetic at
the real counts — no concept label meets a document card, another label, or the
centre card — because an assertion is cheaper than trusting the halo.

### Three defects only a screenshot found

`tsc`, `vitest` and `vite build` all passed over these.

- The legend swatches painted **SVG-default black**: they reuse the canvas's own
  `.node` / `.edge` classes precisely so a colour change moves both, but the
  marks lacked the `shape` class the fill rules are keyed on.
- Edges at `opacity: 0.35` **washed out entirely on a white background**. Now
  0.45, with legend swatches exempt at 0.9 — one edge being pointed at is not
  twenty edges competing.
- `.graph-layout`'s two-column rule sits in the graph block, **below** the 76rem
  media query, so at equal specificity it won and the layout stayed two-column
  down to a 96px canvas beside a 320px panel. A media query is not a stronger
  rule, only a conditional one; `.explore-panes` and `.ask-columns` work solely
  because they are declared above theirs. The collapse is now its own query,
  same breakpoint, positioned after the rule it overrides.

A fourth came out of the hover state: probing any node took the *open* concept's
ring down to 0.2 with everything else, so which concept the panel was describing
stopped being visible on the canvas the moment the pointer moved. Selection now
holds at 0.7 under the dim.

### How it was looked at

No stack, no window. A throwaway vitest file rendered the screen with mocked
projections, wrote `document.body.innerHTML` into a page with `styles.css`
inlined, and `google-chrome-stable --headless --screenshot` captured it at 1600,
1250, 1100 and 700px in both themes. Two details make the difference between a
useful screenshot and a misleading one:

- **`prefers-color-scheme` answers the OS**, and headless Chrome will not fake
  it — it reported dark for every run. The theme's own `:root` token block has to
  be re-applied directly to see the other one.
- **React sets a `<select>`'s value as a DOM property**, which `innerHTML` does
  not serialise. The first screenshots showed "Elige un documento…" over a drawn
  canvas — the exact bug being fixed — until the `selected` attribute was
  restored in the captured HTML.

### One measurement, not acted on

`rem` in a media query resolves against the *initial* 16px root font size, not
the stylesheet's `:root { font-size: 15px }`, so 76rem is 1216px. At that width
— the narrowest the two-column layout is allowed — the canvas is about 630px
wide and its 11px labels render at roughly 7.9px. Legible, but only just.
Collapsing the graph at 90rem would end that band. 76rem was chosen deliberately
to match `.explore-panes` and `.ask-columns`, and is left alone.

### Four things the screen was already fetching and throwing away

- `semantic` and `confidenceFloor` arrive on all three responses. Graph was the
  only screen rendering wholly model-proposed data without the `.semantic`
  badge that `styles.css` calls the load-bearing class on Explore.
- `Claim.sourceChunkId` — the id whose type comment exists to make a claim
  checkable — was fetched from the first version of this screen and never shown.
  It is in the detail panel now. Opening the fragment behind it is still not
  built.
- `claims.length === 0` meant both "still fetching" and "nothing clears the
  floor", so the empty answer flashed before the real one at every click.
- Clicking an outer-ring document re-centred on a *version* id that is generally
  not any shelf document's `activeVersionId`, so the picker found no matching
  option and silently reverted to "pick a document" while the canvas showed one.
  The centred version is now added to the option list when the shelf lacks it.

### Still true

The canvas is `role="group"` rather than `role="img"` — SVG contents under
`role="img"` are presentational, which silenced twenty `role="button"` children
that kept keyboard focus and lost their accessible names. Nothing here is
tooltip-only: a fixed line under the canvas carries the hovered *or focused*
node's full name and metrics, because a name available only on hover is not
available to anyone who cannot hover.
