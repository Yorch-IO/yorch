# Company Brain

A **retrieval-augmented generation (RAG)** system for your own documents. It
indexes a corpus into a **vector database** (Qdrant) and a **graph database**
(Memgraph), then answers questions out of both with citations you can check —
and it shows you exactly what the pipeline is going to do, and what it will
cost, **before** it spends anything.

RAG is usually the easy half of a product and the hard half of a result: the
retrieval works, the model writes a fluent paragraph, and nothing tells you
whether the paragraph is in the documents. Here the model proposes and the code
verifies. Every citation is checked against the passages actually retrieved, and
an answer left with none is returned as *insufficient evidence* rather than as an
answer with a caveat.

Everything runs on your machine: the vector database, the workflow engine, the
catalog, every intermediate artifact. Only embedding, text-fixing, OCR and
answer generation leave, to a provider you configure with your own API key.

## The idea

Indexing a document corpus with an LLM in the loop is easy to get wrong in ways
that look fine. Chunk boundaries land in the wrong place, a document gets read
with rules learned from an unrelated one, and the retrieval metrics report
success because the questions were generated from the same broken chunks.
Meanwhile the bill arrives.

So the pipeline stops and shows its work. Detection, extraction and chunking all
run for free; then you get a preview — the structure it found, the chunks it
would create, per-stage cost estimates, and explicit warnings for the failure
modes that measurement cannot catch. Nothing is paid for until you approve it.

That ordering is driven by a real measured run (`docaget/costo.json`):

| Stage | Cost | Share |
|---|---:|---:|
| Correction (`gemini-2.5-flash`) | $0.0334 | **65%** |
| Eval-set generation | $0.0131 | 26% |
| Embedding (`gemini-embedding-001`) | $0.0033 | 6% |
| Rule learning, eval queries | $0.0015 | 3% |
| **Total, 175-page book** | **$0.0513** | |

The model names in that table are the ones that run charged, and it is old: both
have since been replaced — `gemini-embedding-001` is no longer served at all, and
the current models are `gemini-3.6-flash` and `gemini-embedding-2`. The *shape*
is what the table is here for, and the shape has held.

Correction dominates, not embedding — so the free gate sits before correction,
and every stage can be switched off individually there.

**Which stage dominates depends on what you switch on**, which is the reason the
switches are per stage rather than one "spend money" button. That corpus was
later indexed with correction *off*, and then semantic extraction is essentially
the whole bill: a 37-document batch billed **$19.75**, of which correction was
zero. Both figures are real; they describe different runs.

## Status

Early, but past the walking-skeleton stage. The library it was built for holds
**72 documents** indexed through the app's own gate — 4 722 chunks, 11 348
concepts and 24 151 claims — and the answering and graph paths run against them.
What is missing is the piece that would let answer *quality* be measured rather
than argued about.

| | |
|---|---|
| Indexing engine (`docaget/`) | Working, measured, 142 tests |
| Backing services (Qdrant, Postgres, Temporal) | Running, healthy |
| Ingestion workflow, approval gate, control API | Working; a 37-document batch ran through it |
| Asking, with verified citations | Working |
| Home: project-wide totals and recent activity | Working |
| Concept graph: whole library, and one document | Working |
| Library verbs: reindex, rebuild, permanent removal | Working, each exercised end to end |
| Desktop app | Builds and launches on Linux; deb and AppImage bundled |
| Eval set — scoring answer quality | **Not built.** It is what three current defaults rest on |
| Folder watching, opening a citation in the source | Not built |

`doc/COMPANY_BRAIN.md` has the current detail, including which commands have
been run to a real exit code and which are still only intended.

## Requirements

- Docker with Compose v2
- [uv](https://docs.astral.sh/uv/) for the Python packages
- Node 20+ and a Rust toolchain for the desktop app
- On Linux, Tauri's system libraries (see [Building on Linux](#building-on-linux))
- On Windows, MSVC build tools and WebView2 (see [Building on Windows](#building-on-windows))
- An API key for an embedding provider — Vertex AI today

## Quick start

Shell commands below are bash, for Linux and macOS. To produce a Windows
application, see [Building on Windows](#building-on-windows).

The fastest loop runs the four stock services in Docker and the worker on your
host, so a code change is a restart rather than an image rebuild.

```bash
cd infra
mkdir -p workspace/snapshots && touch secrets.env && chmod 600 secrets.env
export BRAIN_WORKSPACE=$PWD/workspace BRAIN_SECRETS_FILE=$PWD/secrets.env
docker compose -p company-brain -f docker-compose.yaml up -d --wait \
  qdrant memgraph postgres temporal temporal-ui
```

Then, from `worker/`, in two shells:

```bash
export BRAIN_WORKSPACE_DIR=$PWD/../infra/workspace \
       BRAIN_TEMPORAL_TARGET=127.0.0.1:7333 \
       BRAIN_QDRANT_URL=http://127.0.0.1:6433 \
       BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7788 \
       BRAIN_SECRETS_FILE=$PWD/../infra/secrets.env \
       BRAIN_DATABASE_URL="postgresql://brain:$(awk -F= '/^BRAIN_PG_PASSWORD=/{print $2}' \
         ../infra/.env)@127.0.0.1:5532/brain"
```

The app generates the Postgres password on first launch and writes it to
`infra/.env`, which is why it is read rather than typed — `brain:brain` is only
ever right on a stack the app has never started.

```bash
uv run python -m brainworker.entrypoint worker
uv run python -m brainworker.entrypoint api
```

```bash
curl -s localhost:8000/health | python3 -m json.tool   # each service, separately
curl -s -X POST localhost:8000/ping                    # round trip through Temporal
```

Temporal's own UI is at <http://localhost:8380>. Stopping the stack keeps every
volume, so vectors and catalog survive:

```bash
docker compose -p company-brain down
```

Every port is bound to `127.0.0.1` and chosen to avoid the upstream defaults, so
the stack will not collide with a Qdrant or Postgres you already run.

## Building on Linux

Verified on Ubuntu 25.10 with Docker 29.7.2 (Compose v5.4.0), Node 24.10,
Rust 1.97.1 and uv 0.11.2. The build, test and launch commands here were run to
a real exit code; the one exception is called out where it appears.

### 1. System libraries

Tauri needs the GTK and WebKit *development* headers, not just the runtime
libraries your desktop already has. Where you have root, this is the whole
story:

```bash
sudo apt-get update && sudo apt-get install -y \
  pkg-config build-essential curl wget file libssl-dev \
  libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
  libayatana-appindicator3-dev libxdo-dev
```

If you cannot install packages, see [a local sysroot without
root](#a-local-sysroot-without-root) below — but note that a sysroot only works
if `PKG_CONFIG_PATH` names it in the shell you build from. Nothing sets that for
you, and forgetting it is the most common way this build fails:

```
The system library `libsoup-3.0` required by crate `soup3-sys` was not found.
The PKG_CONFIG_PATH environment variable is not set.
```

Which package it names depends on how far the build got — `webkit2gtk-4.1`,
`libsoup-3.0` and `javascriptcoregtk-4.1` all appear — but the cause and the fix
are the same one every time.

### 2. Build and launch

Skip the first line if you installed the `-dev` packages with `apt`. With the
local sysroot it is required in **every** new shell — `cargo` reads
`PKG_CONFIG_PATH` from the environment and there is no project-level fallback:

```bash
source ~/.local/tauri-sysroot/env.sh   # sysroot only: sets PKG_CONFIG_PATH and PATH
cd app
npm install
npx tauri build --bundles deb        # or --bundles appimage
./src-tauri/target/release/company-brain
```

To check that the Rust half compiles before paying for a full bundle, from the
repository root:

```bash
source ~/.local/tauri-sysroot/env.sh
cd app/src-tauri && cargo build --release   # ~2 min from cold on this machine
```

**That is a compile check, not a way to build the app.** `npx tauri build` runs
`beforeBuildCommand` — `npm run build` — first, and `generate_context!()` bakes
`../dist` into the binary at compile time. Bare `cargo build --release` skips the
frontend step and embeds whatever `dist/` already held, so the binary launches
showing an older UI with no error anywhere to say so. Run `npm run build`
yourself first if you use the shortcut.

Bundles land in `app/src-tauri/target/release/bundle/{deb,appimage}/`. The deb
declares `Depends: libwebkit2gtk-4.1-0, libgtk-3-0` and nothing else. The
AppImage is self-contained and is meant to run without installing anything,
though only the unbundled binary above was actually launched here:

```bash
"app/src-tauri/target/release/bundle/appimage/Company Brain_0.1.0_amd64.AppImage"
```

For the development loop, with hot reload on the frontend:

```bash
source ~/.local/tauri-sysroot/env.sh   # sysroot only
cd app
COMPANY_BRAIN_REPO_ROOT=/path/to/this/checkout npm run tauri dev
```

`tauri dev` recompiles Rust on every change under `src-tauri/`, so it needs
`PKG_CONFIG_PATH` exactly as the bundle build does — the failure looks identical
and arrives minutes into what seemed like a working session. **Do not pass
`--release` here.** It was the advice while the debug profile built a second
full copy of the dependency graph including a `staticlib`, and the fix for that
turned out to be tuning the profile rather than abandoning it: `[profile.dev]`
now sets `debug = "line-tables-only"`, which cut that `staticlib` from 845 MB to
352 MB and the `cdylib` from 244 MB to 82 MB, and `opt-level = 3` for
`[profile.dev.package."*"]`, so the dependency graph is optimized once, cached,
and the window still feels like the release build. What `--release` buys in the
dev loop is `codegen-units = 1` and fat LTO on *every* rebuild, and both are
serial — one core at 100% for minutes while the rest idle. Measured on a
16-thread machine: a full dev build is 51 s at 654% CPU with 13 `rustc`
processes at once. Vite serves the frontend on 5173 and the window opens once
the first Rust build finishes.

`COMPANY_BRAIN_REPO_ROOT` makes the app use `infra/` in place and build the
worker from local source instead of pulling an image that is not published.

The window opens either way, but every screen reports a connection error until
the control API answers. The app reaches it on `127.0.0.1:8787` — the
host-published port of the `api` container, not the `8000` uvicorn binds inside
it, and not the host-mode API from [Quick start](#quick-start). Build the worker
image once, then start the two services that use it:

```bash
docker build -f worker/Dockerfile -t company-brain-worker:dev .

cd infra
export BRAIN_WORKSPACE=$PWD/workspace BRAIN_SECRETS_FILE=$PWD/secrets.env
docker compose -p company-brain up -d --wait api worker
curl -s localhost:8787/health | python3 -m json.tool
```

`--wait` pulls the stock services in as dependencies, so this is enough on its
own. A healthy reply reports `api`, `qdrant`, `postgres` and `temporal`
separately. `Connection refused` from that `curl` means the `api` container is
down, and is the same error the window shows.

### Verified artifacts

Re-measured 2026-08-21.

| Step | Result |
|---|---|
| `docaget` — `uv sync && uv run pytest -q` | 142 passed, 13 skipped |
| `worker` — `uv sync && uv run pytest -q` | 310 passed, 48 skipped |
| `app` — `npm run typecheck` | clean |
| `app` — `npx vitest run` | 230 passed, 20 files (re-measured 2026-08-30) |
| `app` — `npm run build` | clean, 261 kB JS |
| `app/src-tauri` — `cargo test` | 91 passed (re-measured 2026-08-30, on the tuned dev profile) |
| `docker build -f worker/Dockerfile` | `company-brain-worker:dev`, 873 MB, imports verified on Python 3.13 |
| `npx tauri build --bundles deb` | `Company Brain_0.1.0_amd64.deb`, 4.1 MB |
| `npx tauri build --bundles appimage` | `Company Brain_0.1.0_amd64.AppImage`, 79 MB |
| Binary launch | window stays up, clean stderr |

### Backups

```bash
cd infra
./backup.sh                  # into ~/company-brain-backups, keeps the 7 newest
./backup.sh --verify         # catalog vs disk only; takes nothing
./restore.sh --from <dir> --check    # prove a backup restores, touching nothing live
./restore.sh --from <dir> --all --yes
```

Four legs, and they are not equal. The **workspace artifacts** are the only copy
of `chunks.jsonl` and `semantics.json`, which is what `rebuild` replays — bulk
data never enters a Temporal payload, so Postgres holds only a path, a sha256
and a size. The **catalog** is the source of truth. **Qdrant** and **Memgraph**
are projections, backed up because a file copy beats paying to re-embed.

`--verify` checks every `run_artifact` row against its file and its recorded
sha256, and runs before every backup: a backup of a workspace that has already
lost files is a backup of the loss.

`--check` is the one to trust. It replays the catalog dump into a scratch
database, loads the Qdrant snapshot into a scratch collection, extracts the tar
to a temp directory, and drops all three afterwards. The Memgraph leg is the
exception — community edition has one database, so proving it would mean
overwriting the live graph, and it is the leg a Rebuild replaces for free.

`secrets.env` and `.env` are deliberately excluded: a backup is a copy that
outlives the machine, and those hold provider keys and the Postgres password.

### Three things that will cost you time

- **Check real exit codes.** `npm run typecheck | tail -6 && echo CLEAN` reports
  the status of `tail`, not `tsc`, and will happily print CLEAN over a failure.
  Redirect to a file and test `$?`.
- **`cargo test` belongs in the debug profile, and that is now the fast path.**
  It used to need ~4 GB of `target/` because `crate-type` includes `staticlib`,
  and could fill a disk and fail with `No space left on device` from `ar` rather
  than from rustc — an error easy to misread as a build defect. Full DWARF, not
  the optimization level, was what made it that large; `[profile.dev]`'s
  `debug = "line-tables-only"` cut the `staticlib` to 352 MB. Plain `cargo test`
  runs the 91 tests in 60 s at 412% CPU, where `--release` pays fat LTO for the
  same result.
- **Rebuild both halves, or neither.** The app and the control API are two
  artifacts that ship one protocol between them, and nothing checks that they
  agree. Rebuilding the binary without recreating the stack — or the reverse —
  produces a failure that points at the network: `the control API is not
  reachable`, while `docker ps` shows it healthy and its log shows the request
  arriving. Rebuild the app with `npx tauri build`, and the API by stopping and
  starting the stack from the Services tab, which runs `up -d --build` in dev
  mode. This cannot happen in an installed copy, where both halves come from the
  same bundle.

  By hand, **`--build` is a silent no-op without `-f docker-compose.dev.yaml`**.
  The base file names an `image:` and nothing else; the `build:` section exists
  only in the dev overlay. Drop that `-f` and compose reuses whatever image is
  already tagged `company-brain-worker:dev`, recreates the containers, and
  reports success — leaving the old API serving under a stack that looks freshly
  built. `docker inspect <container> --format '{{index .Config.Labels
  "com.docker.compose.project.config_files"}}'` says which files really made it,
  and `curl -s localhost:${BRAIN_API_HOST_PORT}/openapi.json` says which routes
  the running API actually has.
- **The AppImage bundler is not offline.** It downloads `linuxdeploy`, `AppRun`
  and two plugins from GitHub at bundle time, into `~/.cache/tauri/`.

### A local sysroot without root

Only the `-dev` half of the dependencies is usually missing — on a normal
desktop `libwebkit2gtk-4.1-0`, `librsvg2-2`, `libayatana-appindicator3-1` and
`libxdo3` are already installed as runtime libraries. That reduces the problem
to eleven packages, which `apt-get download` fetches without any privilege:

```bash
SR=~/.local/tauri-sysroot
mkdir -p "$SR/debs" && cd "$SR/debs"
for p in libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libsoup-3.0-dev \
         librsvg2-dev libsqlite3-dev libpsl-dev libkrb5-dev krb5-multidev \
         libnghttp2-dev comerr-dev libverto-dev; do
  apt-get download "$p"
done
for d in *.deb; do dpkg -x "$d" "$SR/prefix"; done
```

Four details make the extracted tree usable, and none are guessable:

- **The `.pc` files hardcode `prefix=/usr`**, so `-I`/`-L` point at the system
  tree where the headers are not. Rewrite `prefix=`, `libdir=` and `includedir=`
  in each one to the extracted path. Do *not* reach for
  `PKG_CONFIG_SYSROOT_DIR`: it applies to every `.pc`, including the system ones
  that must keep resolving against `/usr`.
- **The `.so` development symlinks are relative and dangle** once extracted
  outside `/usr`. Repointing each at the real system `.so.N` is what lets the
  link succeed, and is also why the resulting binary needs nothing from the
  sysroot at runtime.
- **`libsoup-3.0.pc` carries `Requires.private`** on sqlite3, libpsl,
  krb5-gssapi and libnghttp2, and pkg-config resolves those even for a dynamic
  `--cflags`. `krb5-gssapi.pc` is itself a symlink into `mit-krb5/`, shipped by
  `krb5-multidev` and not by `libkrb5-dev` — the least obvious package in the set.
- **`apt-get download` aborts the whole invocation** if any one name has no
  candidate, so fetch package-by-package or one bad name silently gets you
  nothing.

A handful of krb5 admin symlinks (`libkadm5*`, `libkdb5`, `libgssrpc`,
`libverto*`) stay dangling because no runtime counterpart is installed. They are
harmless — nothing in the link graph references them. The check that matters is
`pkg-config --libs --static libsoup-3.0`, which pulls the whole
`Requires.private` chain.

**Keep the sysroot somewhere permanent and save its environment**, or you will
rebuild it the next time you touch the Rust half:

```bash
cat > ~/.local/tauri-sysroot/env.sh <<'EOF'
SR=~/.local/tauri-sysroot
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PKG_CONFIG_PATH="$SR/prefix/usr/lib/x86_64-linux-gnu/pkgconfig:$SR/prefix/usr/share/pkgconfig"
EOF
```

Then `source ~/.local/tauri-sysroot/env.sh` before `cargo` or `tauri` in each
new shell.

Two caveats. The dev packages must match the installed runtime versions — check
`dpkg -l libwebkit2gtk-4.1-0` against the version `apt-get download` fetched, or
you get headers that disagree with the library you link. And the *build*
reproduces only on a machine set up this way; the *binary* has no such tie, and
`ldd` on it confirms it resolves only ordinary system libraries.

## Building on Windows

**A build made inside WSL2 is a Linux binary.** It runs under WSLg and looks
like a window, but it is not a Windows application and cannot be installed on a
machine without WSL. A native `.exe` has to be produced by the Windows
toolchain — either on Windows itself, or on a Windows CI runner. There is no
supported way to cross-compile the installers from Linux, because the MSI and
NSIS bundlers are Windows tools.

### 1. Install the toolchain

Run in PowerShell. Everything here is per-user and needs no administrator rights
except the build tools installer.

```powershell
# Required to compile the executable
winget install --id Microsoft.VisualStudio.2022.BuildTools `
  --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
winget install --id Rustlang.Rustup
winget install --id OpenJS.NodeJS.LTS

# Required to run it, and to build the worker image
winget install --id Docker.DockerDesktop

# Only if you also want to run the Python engine or its tests on Windows
winget install --id astral-sh.uv
```

Then close and reopen PowerShell so the new `PATH` applies, and confirm:

```powershell
rustc --version; node --version; uv --version; docker compose version
```

Two notes. Rust's default target on Windows is already
`x86_64-pc-windows-msvc`, which is the one you want — the GNU toolchain will not
link against WebView2. And **WebView2** is preinstalled on Windows 11 and
current Windows 10; if `npx tauri info` reports it missing, install the
Evergreen Runtime from Microsoft.

### 2. Build the app

```powershell
cd app
npm install
npx tauri info          # verify: no missing prerequisites
npm run tauri build
```

The installers land in:

```
app\src-tauri\target\release\bundle\msi\Company Brain_0.1.0_x64_en-US.msi
app\src-tauri\target\release\bundle\nsis\Company Brain_0.1.0_x64-setup.exe
```

Either installs a working application. `bundle.targets` is `"all"`, so both are
produced; set it to `"nsis"` alone if you only want the smaller setup binary.
The first MSI build downloads WiX automatically.

### 3. Build the worker image — required, or the app half-starts

The installed app starts its stack from a Compose file it writes into your
app-data directory, and that file names `company-brain-worker:dev`. **No such
image is published yet**, so Qdrant, Postgres and Temporal will come up healthy
and the API and worker will fail to pull. Build it once on the same machine,
from a clone of this repository:

```powershell
docker build -f worker\Dockerfile -t company-brain-worker:dev .
```

Docker Desktop must be running, using the WSL2 backend, before you launch the
app.

### 4. Run it

Launch **Company Brain** from the Start menu, then press **Start stack** on the
first screen. Cold start pulls several images and waits for Temporal to create
its database schemas, which takes a few minutes; the window reports progress
rather than blocking silently.

### Signing, and the warning your users will see

The bundles are unsigned. SmartScreen will show "Windows protected your PC" on
first run, and the installer will look untrusted to anyone you send it to. For
distribution you need an Authenticode certificate and
`bundle.windows.certificateThumbprint` in `tauri.conf.json`. For your own use,
"More info → Run anyway" is enough.

### Building Windows artifacts from this Linux checkout

Since development happens in WSL2, CI is the practical way to produce a Windows
installer. A Windows runner needs no extra setup beyond the Node and Rust
actions:

```yaml
jobs:
  windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20 }
      - uses: dtolnay/rust-toolchain@stable
      - run: npm ci
        working-directory: app
      - run: npm run tauri build
        working-directory: app
      - uses: actions/upload-artifact@v4
        with:
          name: company-brain-windows
          path: app/src-tauri/target/release/bundle/*/*
```

### Windows-specific behaviour worth knowing

- The workspace lives under `%APPDATA%\io.sek.companybrain\`. Docker Desktop's
  WSL2 backend shares that automatically; no manual file-sharing setup.
- Paths written into the Compose `.env` are normalised to forward slashes
  (`C:/Users/...`), because whether a backslash in a `.env` value is treated as
  an escape has varied across Compose versions.
- The provider key file is not `chmod`ed — there is no mode bit. It inherits the
  ACL of your per-user app-data directory, which is already owner-only.
- The `/mnt/...` workspace check is Linux-only, and correctly does nothing here:
  it exists to stop a WSL user putting the workspace on a Windows drive mount,
  which is the reverse of this situation.

None of this section has been executed — this repository has only ever been
built on Linux. Treat it as the intended path rather than a verified one.

## What it can read

Today: **PDF**, **DOCX**, **XLSX**, **PPTX**, **CSV**, **TXT**, **MD**. HTML,
email and standalone images are planned.

**A PDF with no text layer is not readable yet.** OCR raises
`NotImplementedError` on purpose — it is a paid stage and it needs its own spend
gate before it can run — so a scan is skipped rather than silently indexed as a
handful of stray characters. Of 44 unindexed PDFs in one real corpus, three were
scans of this kind. Only PDF and TXT have been run end to end; the other formats
are implemented and unexercised.

Sources split into two shapes, and the difference is real rather than
incidental. Text-like documents become a linear byte stream, and every chunk's
anchor is a byte-exact slice of it. Spreadsheets have no such stream, so their
anchor is a cell range like `Ventas 2025!A41:D60`. Correction never touches
structured sources — "correcting" a spreadsheet corrupts data rather than
improving writing.

## How a document gets indexed

Ingestion is a Temporal workflow, so each step below is a durable activity: a run
that takes twenty minutes survives a crash, a restart or a laptop lid closing,
and every step it took is inspectable afterwards. **Everything up to the gate is
free**, and nothing after it runs until you approve it.

**1 · Stage and hash.** The file is copied into the workspace and hashed.
Identity is the sha256 of the bytes, not the path — so the same document imported
from two folders becomes one indexed version with two shelf entries, rather than
two copies competing in the ranking. A byte-identical re-import costs nothing.

**2 · Extract.** A format-specific extractor turns the file into either a linear
byte stream (PDF, DOCX, PPTX, TXT, MD) or a set of already-delimited cells
(XLSX, CSV). Paragraph breaks in a PDF come from the *line pitch*, not from the
gap between text boxes: the gap has a median of 2.5 pt with 5% jitter, so a
threshold on it fires constantly — 1 064 paragraphs on a book that has 330.

**3 · Resolve a profile.** Documents are fingerprinted structurally — extractor,
running headers, page geometry, heading numbering — and a *profile* holds the
chunking rules learned for that family. Looking one up is free, and a document
whose family is already known inherits its rules at no cost; learning a new one
is a paid stage that runs after the gate, so the first document of a family
pays for the exploration and every one after it does not. The fingerprint is
deliberately blind to subject matter, which means two unrelated families with the
same layout collide exactly — so the gate raises a structural warning and you can
decline the inherited profile.

**4 · Chunk, and preview.** Chunking is windowed with byte-exact spans: every
chunk records the byte offsets it came from, so a citation can be resolved back
to the exact slice of the source. Chunks are classified by kind — body, review
questions, footnote, table row, slide — because a numbered review question and a
numbered footnote look identical apart from a dot after the number, and treating
one as the other corrupts the section path for everything below it.

**5 · The gate.** You see the structure found, the chunks that would be created,
per-stage cost estimates as a range, and warnings for the failure modes no metric
can catch. Every paid stage below has its own switch here. Nothing has been
billed yet.

**6 · Correct** *(paid, optional)*. A conservative LLM pass repairs the
converter, not the author: no reformulation, no sentence splitting, no touching
names, technical terms, scripture references or figures. Each paragraph is
verified deterministically afterwards and **rejected if it lost a proper noun,
altered a reference or a figure, or changed length by more than 25%** — a
rejected correction keeps the original. It runs *before* chunking, because it
changes the text's length and every byte span would otherwise be wrong. That is
also why previewed chunks are not the final chunks when correction is on.

**7 · Project the structure.** Document, version, sections, chunks and citations
are written to the graph. This is derived from the document's own headings —
deterministic, nothing proposed by a model — which is why the UI can say "this
came from the table of contents" rather than "a model thought so".

**8 · Embed and index** *(paid)*. Each chunk is embedded and written to Qdrant
alongside a BM25 sparse vector. **One text per embedding request**: the model
returns a single embedding for a request carrying four, with no error — batching
does not fail, it silently drops, so throughput comes from concurrency instead.
Point ids are derived from the version and the chunk index, which is what makes
re-indexing converge rather than accumulate, and what makes the step safe to
retry.

**9 · Extract semantics** *(paid, optional)*. Concepts and claims, **one chunk
per call**. Batching is cheaper and wrong: every claim must carry the id of the
chunk
a person can check it against, and a model handed ten chunks attributes claims
to the wrong one. Concepts are merged across documents by canonical name, which is
what lets the graph answer "which books share this idea".

**10 · Activate.** The version becomes the one citations resolve to, and the run,
its artifacts and its costs are recorded. Costs are measured token counts with
third-party prices applied — the counts are ours, the multipliers are not.

### Answering, which is the other half

A question is embedded with the *query* task rather than the document task — the
model embeds questions and passages asymmetrically, and using one task for both
measurably degrades retrieval. Retrieval is hybrid: a dense vector and a BM25
sparse vector fused with reciprocal rank, filtered to the library being asked.
A dense-only pass first acts as a topicality gate, so a question about nothing in
the corpus comes back as *off corpus* instead of as the five least-bad passages.

The graph then expands that evidence, but only through *named templates*: a
planner returns a template id plus typed parameters, never Cypher. Claims reach
the answering prompt as a model's *reading* of a chunk, never as the chunk — the
prompt states that the document's own text wins any disagreement, and the model
still cites by chunk id, because otherwise model output re-enters the context
dressed as the source.

Then every citation is verified, and unverifiable ones are dropped.

## What it gives you back

A citation you can check, and a graph you can interrogate. Both are built the
same way: the model proposes, the code verifies, and anything that fails
verification is dropped rather than shown.

**Answers cite, or refuse.** Every answer names the chunks it rests on; each id
is checked against the passages actually retrieved, and one the model invented is
discarded. An answer left with no surviving citation is not returned as an answer
— it comes back as *insufficient evidence*, with the passages it did find. That
is a different state from *off corpus*, which means nothing cleared the
similarity floor at all: the two have different fixes.

**Claims carry the document's own words.** Extraction pulls out what a passage
claims, and asks for the sentence it came from. That quote is then located in the
chunk's own text — whitespace may differ, nothing else — and a claim whose quote
is not found keeps its text but loses its span. Measured across three runs of a
real corpus: **98.7%, 99.2% and 99.4%** of claims carried a quote the code could
find.

**A claim records what the document *does* with it** — asserts it, rejects it, or
attributes it to somebody else. On one real document 14% were not plain
assertions. Without that field a doctrine a text is about to rebut reads exactly
like one it holds.

**The concept graph answers "which", not only "how many".** It opens on the
whole library: every book, every concept that joins one book to another, and one
weighted line per pair. Selecting a book lights its concepts; selecting a concept
lights every book that mentions it. From there any book opens into the older
view — that book at the centre, its concepts on an inner ring, the documents it
shares them with on an outer one — where selecting one of those lists every
shared concept by name. No line is drawn between a concept and an outer document
there, because the data does not say which passage of that document mentions it,
and a line would be inventing one.

**What the library view draws is decided by degree, not by confidence.** Measured
on the real corpus: raising the confidence floor from 0.6 to 0.9 removes 3% of
the edges — the extractor is confident about almost everything it proposes —
while requiring a concept to appear in two books removes 84%, taking 10 835
concepts down to 1 719. Everything it removes is a leaf that cannot connect two
books. So the threshold that matters is "concepts in at least *N* books", set to
2 by default and adjustable down to 1; nothing is unreachable either way, since
the list beside the canvas holds every node in the response and is the keyboard
path to any of them.

**Home reports what the project holds, and says when it cannot.** Books, chunks,
concepts, claims, semantic relations, and the last ten runs with what each one
was and how it ended. Its two sources fail independently, so every figure carries
its own availability: with the graph database stopped, the counts that come from
it show an em dash — the reason is in the tooltip — and never a zero. "Could not
be read" and "is empty" are different facts with different fixes, and a zero
would be a claim about your corpus. Pages is in that state permanently for now:
nothing records a page count yet, so the card says how many versions do (none),
rather than reporting none as zero.

**The cost estimate is a range, not a number.** Semantic extraction varies by
more than 2x across documents, so a single figure could either cover the worst
document or stay in reach of a typical one, never both. The gate shows both ends;
the low one describes a typical document and the high one is a ceiling.

## The stack it runs on

```
┌─ Tauri desktop app ─────────────────────────────────────────────┐
│  React ──▶ Rust: Docker lifecycle · OS keychain · typed proxy   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ loopback only
┌──────────────────────────▼──────────────────────────────────────┐
│  api (FastAPI)     worker (Temporal + the docagent engine)       │
│  temporal ── postgres (workflow history + catalog)               │
│  qdrant (vectors) ── memgraph (concepts, claims, citations)      │
└──────────────────────────────────────────────────────────────────┘
```

The app never speaks Python. Temporal has no production Rust SDK, so everything
the UI needs goes through the control API, and Rust proxies each call as an
explicit typed command — which is what lets the webview keep a `default-src
'self'` policy with no localhost exception, and keeps the Rust surface to what
only Rust can do: Docker, the keychain, native dialogs, the filesystem.

Bulk data never enters a workflow payload. Activities write chunks and semantics
to files in the workspace and pass back a path, a sha256 and a size; Postgres
holds the reference. API keys never enter one either — they live in your OS
keychain and reach the worker as process state, and workflows carry only a
provider id.

One measurement worth repeating: retrieval always reports hybrid and dense-only
side by side, because synthetic eval questions leak vocabulary to the lexical leg
and the gap between the two modes is the only honest measure of that leak.

### Why three databases

They answer different questions, and each is the wrong shape for the others'.

| | Holds | Answers |
|---|---|---|
| **Qdrant** | An embedding and a sparse term vector per chunk | "Which passages are *about* this?" |
| **Memgraph** | Documents, sections, chunks, concepts, claims, citations | "Which books share this idea, and where exactly?" |
| **Postgres** | Catalog, run history, costs, workflow state | "What is on the shelf, and what did it cost?" |

#### Qdrant, for the vectors

**An embedding is a passage turned into coordinates.** You hand a chunk of text
to an embedding model and it returns a fixed-length list of numbers — here 3 072
of them, from `gemini-embedding-2`. That list is a point in a 3 072-dimensional
space, and the model has been trained so that passages *meaning* similar things
land near each other. Nothing about it is keyword matching: "el bautismo de los
niños" and "paedobaptism" end up close together without sharing a character,
and "banco" in a passage about rivers ends up far from "banco" in one about
loans.

**"Near" is measured as an angle, not as a distance.** That is cosine
similarity: take the two vectors as arrows from the origin and measure the angle
between them. Identical direction scores 1, unrelated scores about 0, opposite
scores −1. The formula is the dot product over the two lengths:

```
cos(a, b) = (a · b) / (|a| × |b|)
```

**Why the angle rather than the straight-line distance** is easiest to see in two
dimensions. Take `[2, 1]` and `[20, 10]`:

```
       ↑                  Euclidean distance:  √(18² + 9²) = 20.1  → "far apart"
   10  ┤        ● [20,10]  Cosine similarity:   1.0                → "identical"
       │      ⁄
       │    ⁄
    1  ┤  ● [2,1]
       └──┴─────────────→
          2           20
```

They point in exactly the same direction and differ only in magnitude. Straight-
line distance calls them far apart; the cosine calls them the same. For text that
is the behaviour you want, because magnitude tracks things like passage length
and word frequency rather than subject matter — a two-line note and a two-page
section about the same doctrine should not be pushed apart for the crime of
being different sizes.

**What the numbers actually look like** is worth seeing, because the two-
dimensional intuition above is misleading in one specific way. Measured on this
library — 1 753 chunks, every pair of a 260-chunk sample, 33 670 cosines:

| | Cosine |
|---|---:|
| Two chunks of the same document, on average | 0.695 |
| Two chunks of *different* documents, on average | 0.608 |
| The most similar pair found | 0.995 |
| The least similar pair found | 0.427 |

**Unrelated is not zero.** Textbook cosine similarity runs −1 to 1 and puts
"nothing to do with each other" near 0; in a real embedding space it sits around
0.6, and the whole corpus lives in a band roughly 0.43 to 0.99. Everything here
is Spanish prose about theology, so everything shares a great deal of direction,
and what carries the signal is the *spread* within that band rather than the
absolute value. This is the reason a similarity threshold has to be calibrated
against the model and the corpus and cannot be read off first principles.

The extremes are both instructive. The lowest pair, at 0.427, is a passage on
marriage against one on Rousseau and the humanist motive — genuinely unrelated,
and the model says so. The highest, at **0.995**, turned out to be the same text
in two files: `Hermeneutica Capitulo 1` and `Hermeneutica Capitulo 1 (1)`, a
duplicate nobody had noticed. A cosine that close is not similarity, it is
identity, and it found a copy in the corpus by accident.

**Here the cosine is just the dot product.** `gemini-embedding-2` returns
L2-normalised vectors at the full 3 072 dimensions — every vector already has
length 1 — so the denominator is `1 × 1` and `cos(a, b) = a · b`. Qdrant is
configured with `"distance": "Cosine"` and needs no renormalisation step.
(Truncating the vector to 768 or 1 536 dimensions, which the model supports,
would break that: the truncated vector is no longer unit length.)

That width is also why the collection survived a change of model. A collection's
vector size is fixed when it is created, and `gemini-embedding-2` kept the 3 072
dimensions of the `gemini-embedding-001` it replaced — otherwise every existing
collection would have had to be rebuilt from scratch.

That number is not only an internal ranking score — it is a **decision**
threshold. A question is embedded too, and a dense-only pass asks whether
anything in the corpus clears a cosine floor of **0.60**. If nothing does, the
question comes back as *off corpus* rather than as the five least-bad passages in
the library.

That floor is not comparable to the passage-to-passage figures in the table
above, and the difference matters. Questions and passages are embedded with
*different* task types — the model places a question and the passage answering it
asymmetrically on purpose, and using one task for both measurably degrades
retrieval — so question-to-passage cosines are their own distribution. Reading
the 0.60 floor against the 0.608 average of unrelated *passages* would be
comparing two different measurements that happen to print similar digits.

Qdrant earns its place by doing three things in the database that would otherwise
be done badly in the client:

- **Hybrid search and fusion, server-side.** One collection holds a *named* dense
  vector and a *named* sparse BM25 vector for the same chunk. A single query
  prefetches both legs and fuses them with reciprocal rank. There is no second
  store for lexical search and no client-side merge to get wrong.
- **The IDF lives in the database.** The sparse vector is declared with
  `modifier: "idf"`, so the corpus statistic is maintained by Qdrant as documents
  arrive. Stored vectors stay raw term frequencies, and nothing has to be
  rewritten when the corpus grows — which it does, constantly.
- **Payload filtering instead of collection-per-library.** A collection's vector
  size is fixed at creation, so many collections means many places a dimension
  change has to be migrated, and cross-library search becomes a fan-out rather
  than a filter. One collection, filtered by payload. Each payload also carries
  the *graph's* `chunk_id` for the same chunk, which is what lets a vector hit
  expand through the graph with no lookup table between them.

Point ids are derived from the version and the chunk index rather than assigned,
so re-indexing converges instead of accumulating and the indexing step is safe
for Temporal to retry.

One thing to know if you touch retrieval: **RRF scores are reciprocal ranks, not
cosines.** A similarity threshold is meaningless on the fused output, so it goes
on the dense prefetch and nowhere else.

#### And the discrete cosine transform?

Nothing in this system computes one. But "cosine similarity" and "the cosine
transform" are not merely a coincidence of names either, and the honest answer is
more interesting than dismissing it: **they are the same arithmetic asking
different questions.**

The DCT is what sits inside JPEG and MP3. It takes a signal — a row of pixel
values, a window of audio samples — and rewrites it as a sum of cosine waves of
increasing frequency, so that the components a human will not miss can be
quantised away. In its usual form (DCT-II), a signal `x` of `N` samples becomes
`N` coefficients:

```
        N-1
X[k]  =  Σ   x[n] · cos[ (π/N) · (n + ½) · k ]        k = 0 … N-1
        n=0
```

Look at what one coefficient is. Fix `k`, and the cosine term is a **fixed vector**
— call it `c_k`, with components `c_k[n] = cos[(π/N)(n + ½)k]`, a sampled cosine
wave at frequency `k`. Then the sum above is componentwise multiply-and-add, which
is exactly the dot product:

```
X[k]  =  x · c_k        "how much of frequency k is in this signal?"
```

And cosine similarity, for unit vectors, is:

```
cos(a, b)  =  a · b     "how much of b's direction is in a?"
```

**Both are projections — an inner product of a vector onto another vector.** That
is the relationship, and it is a real one. Where they part company is what the
second vector *is*, and it is a large difference:

| | Discrete cosine transform | Cosine similarity |
|---|---|---|
| Project onto | A **fixed, known** orthogonal basis of cosine waves | Another **learned** embedding |
| Where "cosine" is | The *basis functions* are cosines | The *result* is the cosine of an angle |
| How many outputs | `N` coefficients, one per frequency | One number |
| Reversible? | Yes — the DCT is invertible, which is what makes it a compression tool | No — you cannot recover the passage from its embedding |
| Chosen by | Mathematics, once, for all signals | Training, from data |

So the DCT is a change of coordinates: same information, rewritten in a basis
where discarding the small parts is cheap. An embedding is not a change of
coordinates at all — it is a lossy, learned map into a space where *direction
means meaning*, and there is no way back. The cosine appears in one as the basis
you project onto, and in the other as the measurement you get out. Same
operation, different question, and only the second one runs here.

#### Memgraph, for the relations

Nearest-neighbour search cannot answer "which of these books discuss the same
concept", "what does this document *claim* about it", or "which chunk do I open
to check that". Those are traversals, and a vector store has no edges to
traverse. Memgraph is where the structure lives, and it was chosen for reasons
that are mostly unglamorous:

- **It speaks Bolt and Cypher**, so the standard Neo4j driver works unchanged and
  the query language is one people already know.
- **It is in-memory**, so a traversal across the whole corpus is fast enough to
  sit behind an interactive canvas. Measured on the 72-document library: the
  unfiltered graph — 10 835 concepts and 15 367 edges, aggregated from 24 424
  mention relations — comes back over HTTP in **400 ms**, and the shared subgraph
  the canvas opens on in **177 ms**. It is memory-limited explicitly in the
  Compose file, because it shares a workstation with your editor and browser and
  its own default is 90% of physical memory.
- **It runs in a container on loopback**, like everything else here. Nothing about
  the graph leaves the machine.

Two things about it are worth knowing before you rely on them. **Memgraph does
not enforce read-only**: a `CREATE` inside a `default_access_mode="READ"` session
succeeds, because Bolt's access mode is a routing hint for Neo4j clusters and
Memgraph's role-based access control is an Enterprise feature. What actually
stops a question writing is that no query text ever reaches the database from a
model — a planner returns a *template id* plus typed parameters, and the template
registry is validated at import. And **`CREATE SNAPSHOT` evicts snapshot
history** as a side effect, so at the stock retention of three, three backups
destroy everything older than the first of them. The Compose file raises it to
ten; a backup must never be the thing that removes the state you wanted to go
back to.

#### Postgres, for the record

The catalog — what exists, where it came from, which version is active, what
every run cost — plus Temporal's own workflow history. It is the source of truth,
and the other two are derived from it: removal writes the projections first and
the catalog last, so a crash leaves the operation retryable rather than leaving
points and nodes that nothing can find again.

## Where your data goes

| | |
|---|---|
| Documents, extracted text, chunks, corrections | Your disk, under the workspace directory |
| Vectors | Qdrant, on `127.0.0.1`, no network exposure |
| Run history, costs, scores | Postgres, on `127.0.0.1` |
| API keys | Your OS keychain; mounted `0600`, never in workflow history |
| Sent to your provider | Text to embed, text to correct, page images to OCR, passages to answer over |

Nothing is uploaded anywhere else, and there is no telemetry.

## Layout

```
docaget/   the indexing engine (Python package `docagent`) — also a working CLI
worker/    Temporal workflows and activities, plus the control API
app/       Tauri v2 desktop app: Rust shell, React UI
infra/     Docker Compose stack, backup/restore, batch indexing and its audit
doc/       design notes, engine internals, operational runbook
```

`infra/audit_indexacion.py` is worth knowing about if you index in bulk. It
builds the run report from Postgres and from each run's own artifacts, and
treats the batch script's manifest as a claim to be checked rather than as the
record — printing every place the two disagree. The script it replaced summed
the manifest and published the total as fact, which is how a report came to
state three things the file it was reading contradicted.

The directory is spelled `docaget` and the package inside it `docagent`. That is
a typo, and it is now load-bearing.

## Tests

```bash
cd docaget && uv sync && uv run pytest -q   # engine — 142 passed, 13 skipped
cd worker  && uv sync && uv run pytest -q   # workflows, graph, catalog, API
cd app     && npm install && npm run typecheck && npx vitest run   # 170 passed
cd app/src-tauri && cargo test               # 91 — see Building on Linux for the libraries
```

The worker's graph and catalog suites are integration tests: they skip, naming
the URL they tried, when Memgraph or Postgres is down. With the stack up it is
**449 passed and nothing skipped** — and the Bolt port has to come from `docker`
rather than from `infra/.env`, which can name a different one:

```bash
cd worker && BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789 uv run pytest -q
```

The engine's property tests need a real document. They prefer the original Go
pipeline's reference output and otherwise fall back to the largest corrected
text in `docaget/libros/`, printing which one it chose in the pytest header.

## Documentation

| | |
|---|---|
| `doc/COMPANY_BRAIN.md` | Build status, verified commands, current blockers |
| `doc/CLAUDE.md` | Engine internals: design decisions with the measurements behind them, the 14 inherited invariants, known-unfixed defects |
| `doc/README.md` | The engine's own README and CLI reference |
| `doc/RUNBOOK_INDEXACION.md` | Operational runbook for supervised indexing (Spanish) |
| `doc/INFORME_INDEXACION.md` | Execution report from the real corpus run (Spanish) |
| `CLAUDE.md` | Orientation for coding agents |
