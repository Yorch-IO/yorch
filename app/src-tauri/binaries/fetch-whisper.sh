#!/usr/bin/env bash
# Fetch or build the `whisper-cli` sidecar this app bundles, for one target.
#
# **Why the app carries whisper.cpp at all.** Transcription is the one paid
# stage of a bucket run that dwarfs every other — $0.024 a minute, about $233
# for the first 147-recording corpus — and the machine the person is sitting
# at may hold a GPU that does the same work for nothing but electricity and
# hours. The worker cannot use it: it runs in a container on a host with no
# GPU. So the app makes the one call only it can make and sends the plane the
# few hundred kilobytes of transcript, exactly as it already does for the
# YouTube call yt-dlp makes next door.
#
# **Why it is not in git**, same judgement as `fetch.sh`: tens of megabytes per
# platform, regenerable from a pinned source revision. A build without it fails
# at the bundler, and a `tauri dev` without it answers `whisper_missing` with a
# sentence telling you to run this. Neither is silent.
#
# **Linux and macOS are built from source; Windows is downloaded.** The
# whisper.cpp project publishes prebuilt binaries for Windows only, so that is
# the one target with a checksum to verify; everywhere else the pin is the
# commit sha, which is stronger than a tarball hash and needs no second
# publisher to trust. `-DBUILD_SHARED_LIBS=OFF` is what keeps the result a
# single file: the default build leaves `libggml.so` and `libwhisper.so` beside
# it, and Tauri's `externalBin` copies one file per entry.
#
#   ./fetch-whisper.sh                      # this machine's triple
#   ./fetch-whisper.sh --cuda               # force the CUDA backend
#   ./fetch-whisper.sh --cpu                # force CPU, whatever nvcc says
#   ./fetch-whisper.sh x86_64-pc-windows-msvc
#
# **What CUDA costs, which is a decision and not a detail.** A CUDA build links
# cuBLAS, and cuBLAS is redistributed rather than assumed: the project's own
# `whisper-cublas-12.4.0-bin-x64.zip` is 457 MB against 3.8 MB for the CPU one.
# A build made here is dynamically linked against the toolkit that made it, so
# it runs on this machine and is **not** by itself shippable — the libraries it
# names are printed at the end, and packaging them is the open question. The
# CPU build is self-contained and ships as it stands.
set -euo pipefail
cd "$(dirname "$0")"

# Pinned, and bumped deliberately. The sha is the tag's commit: a tag can be
# moved and a sha cannot.
VERSION="v1.8.2"
COMMIT="4979e04f5dcaccb36057e059bbaed8a2f5288315"
REPO="https://github.com/ggml-org/whisper.cpp.git"
SRC="${WHISPER_SRC:-$HOME/.local/src/whisper.cpp}"

# The Windows assets of that release, with the checksums GitHub publishes.
WIN_CPU_ASSET="whisper-bin-x64.zip"
WIN_CPU_SUM="b1514ebc099765e39fa37eb780b92a140a94c86bb0b3b3d98226b38825979732"
WIN_CUDA_ASSET="whisper-cublas-12.4.0-bin-x64.zip"
WIN_CUDA_SUM="54a3c012e6c567e2e8bed874cdf49c55f018ec8d90bb5d5271568aa573291b2d"

backend=""
targets=()
for arg in "$@"; do
  case "$arg" in
    --cuda) backend="cuda" ;;
    --cpu)  backend="cpu" ;;
    --*) echo "unknown option '$arg'" >&2; exit 2 ;;
    *) targets+=("$arg") ;;
  esac
done
if [ ${#targets[@]} -eq 0 ]; then
  targets=("$(rustc -vV | sed -n 's/^host: //p')")
fi

# Absent means "whatever this machine can do", which is the answer that makes
# a first run on a GPU box fast without being asked twice.
if [ -z "$backend" ]; then
  if command -v nvcc >/dev/null 2>&1; then backend="cuda"; else backend="cpu"; fi
fi

fetch_source() {
  if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --quiet --tags origin || true
  else
    mkdir -p "$(dirname "$SRC")"
    git clone --quiet "$REPO" "$SRC"
  fi
  # Refuses rather than building something else: a tag that has moved, or a
  # checkout somebody left on another revision, would produce a binary nobody
  # can reproduce from this script.
  git -C "$SRC" checkout --quiet "$COMMIT"
  local at
  at="$(git -C "$SRC" rev-parse HEAD)"
  if [ "$at" != "$COMMIT" ]; then
    echo "expected $COMMIT in $SRC, found $at" >&2
    exit 1
  fi
}

build_native() {
  local target="$1" out="$2"
  command -v cmake >/dev/null || { echo "cmake is needed to build whisper.cpp" >&2; exit 1; }
  fetch_source
  local build="$SRC/build-${backend}"
  local args=(
    -B "$build" -S "$SRC"
    -DCMAKE_BUILD_TYPE=Release
    -DBUILD_SHARED_LIBS=OFF
    -DWHISPER_BUILD_TESTS=OFF
    -DWHISPER_BUILD_SERVER=OFF
  )
  if [ "$backend" = "cuda" ]; then
    command -v nvcc >/dev/null || {
      echo "--cuda needs the CUDA toolkit (nvcc); install it or pass --cpu" >&2
      exit 1
    }
    args+=(-DGGML_CUDA=ON)
  fi
  echo "==> cmake ${VERSION} (${backend}) for ${target}"
  cmake "${args[@]}" >/dev/null
  cmake --build "$build" --config Release --target whisper-cli -j "$(nproc 2>/dev/null || echo 4)" >/dev/null
  cp "$build/bin/whisper-cli" "$out.part"
  chmod +x "$out.part"
  mv "$out.part" "$out"
}

fetch_windows() {
  local target="$1" out="$2" asset sum
  if [ "$backend" = "cuda" ]; then asset="$WIN_CUDA_ASSET"; sum="$WIN_CUDA_SUM";
  else asset="$WIN_CPU_ASSET"; sum="$WIN_CPU_SUM"; fi
  echo "==> ${asset} ${VERSION} -> ${out}"
  curl -sSL --fail -o "$asset.part" \
    "https://github.com/ggml-org/whisper.cpp/releases/download/${VERSION}/${asset}"
  echo "${sum}  ${asset}.part" | sha256sum -c - >/dev/null
  local dir="runtime-${target}"
  rm -rf "$dir" && mkdir -p "$dir"
  unzip -q -o -j "$asset.part" -d "$dir"
  rm -f "$asset.part"
  mv "$dir/whisper-cli.exe" "$out"
  # The DLLs beside it are the ones Windows loads from the executable's own
  # directory, so they have to be installed next to the app binary — which
  # `externalBin` cannot do, one file per entry. Printed rather than solved:
  # see the header.
  echo "    runtime libraries left in ${dir}/ — they must ship beside the app binary"
}

for target in "${targets[@]}"; do
  out="whisper-cli-${target}"
  case "$target" in
    *windows*)
      out="${out}.exe"
      fetch_windows "$target" "$out"
      ;;
    *)
      build_native "$target" "$out"
      ;;
  esac
done

echo "done. $(ls whisper-cli-* 2>/dev/null | tr '\n' ' ')"
for f in whisper-cli-*; do
  case "$f" in *.exe) continue ;; esac
  if command -v ldd >/dev/null 2>&1; then
    # What it needs at runtime, which is the whole shippability question for a
    # CUDA build: `libcuda.so.1` comes with the driver, `libcudart`/`libcublas`
    # do not.
    echo "--- $f"
    ldd "$f" | grep -Ei "cuda|cublas|vulkan|omp" || echo "    no accelerator libraries — self-contained"
  fi
done
