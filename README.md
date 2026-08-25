# Company Brain

Turn a pile of company documents into a searchable, cited knowledge base — and
see exactly what the pipeline is going to do, and what it will cost, **before**
it spends anything.

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

Correction dominates, not embedding — so the free gate sits before correction,
and every stage can be switched off individually there.

## Status

Early. The engine underneath is real and has indexed a ~75-document corpus; the
app around it is at its first working milestone.

| | |
|---|---|
| Indexing engine (`docaget/`) | Working, measured, 109 tests |
| Backing services (Qdrant, Postgres, Temporal) | Running, healthy |
| Workflow pipeline + control API | Walking skeleton verified end to end |
| Artifact store | Done |
| Desktop app | Builds and launches on Linux; deb and AppImage bundled |
| Ingestion workflow, preview UI, search, export | Not built yet |

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
COMPANY_BRAIN_REPO_ROOT=/path/to/this/checkout npm run tauri dev -- --release
```

`tauri dev` recompiles Rust on every change under `src-tauri/`, so it needs
`PKG_CONFIG_PATH` exactly as the bundle build does — the failure looks identical
and arrives minutes into what seemed like a working session. Pass `--release`:
the default debug profile builds a second full copy of the dependency graph
including a `staticlib`, which is the ~4 GB noted below. Vite serves the
frontend on 5173 and the window opens once the first Rust build finishes.

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
| `app` — `npx vitest run` | 44 passed, 6 files |
| `app` — `npm run build` | clean, 261 kB JS |
| `app/src-tauri` — `cargo test --release` | 45 passed |
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
- **`cargo test` in the debug profile needs ~4 GB of `target/`**, because
  `crate-type` includes `staticlib`. It can fill a disk and fail with `No space
  left on device` from `ar` rather than from rustc — an error easy to misread as
  a build defect. `cargo test --release` reuses the release artifacts.
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

Today: **PDF** (with OCR for pages that have no text layer), **DOCX**, **XLSX**,
**PPTX**, **CSV**, **TXT**, **MD**. HTML, email and standalone images are
planned.

Sources split into two shapes, and the difference is real rather than
incidental. Text-like documents become a linear byte stream, and every chunk's
anchor is a byte-exact slice of it. Spreadsheets have no such stream, so their
anchor is a cell range like `Ventas 2025!A41:D60`. Correction never touches
structured sources — "correcting" a spreadsheet corrupts data rather than
improving writing.

## How it works

```
┌─ Tauri desktop app ─────────────────────────────────────────────┐
│  React ──▶ Rust: Docker lifecycle · OS keychain · typed proxy   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ loopback only
┌──────────────────────────▼──────────────────────────────────────┐
│  api (FastAPI)     worker (Temporal + the docagent engine)       │
│  temporal ── postgres (workflow history + catalog)               │
│  qdrant (vectors, hybrid dense + BM25 with RRF fusion)           │
└──────────────────────────────────────────────────────────────────┘
```

Ingestion is a Temporal workflow, so a run that takes twenty minutes survives a
crash, a restart, or a laptop lid closing — and every step it took is
inspectable afterwards. The engine learns a *profile* per document family, so
the first document of a family pays for the exploration and every one after it
is cheaper and more consistent.

Retrieval is hybrid: a dense vector and a BM25 sparse vector, fused with
reciprocal rank. Results always report hybrid and dense-only side by side,
because synthetic eval questions leak vocabulary to the lexical leg and the gap
between the two modes is the only honest measure of it.

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
infra/     Docker Compose stack
doc/       design notes, engine internals, operational runbook
```

The directory is spelled `docaget` and the package inside it `docagent`. That is
a typo, and it is now load-bearing.

## Tests

```bash
cd docaget && uv sync && uv run pytest -q   # engine
cd worker  && uv sync && uv run pytest -q   # workflows, artifacts, config
cd app     && npm install && npm run typecheck && npx vitest run
cd app/src-tauri && cargo test --release     # see Building on Linux for the libraries
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
