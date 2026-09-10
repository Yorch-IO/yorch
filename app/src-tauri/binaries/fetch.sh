#!/usr/bin/env bash
# Fetch the `yt-dlp` sidecar this app bundles, for one target triple.
#
# **Why the app carries yt-dlp at all.** Measured 2026-09-05 against
# production: `yt-dlp extract_info` succeeds from a residential address in
# 2.6 s and is refused from the EC2 egress address with "Sign in to confirm
# you're not a bot", while that same host fetches every signed caption URL at
# 200. It is the player API that is bot-checked, not the network — so in cloud
# mode the app makes that one call here, on the machine the person is sitting
# at, and sends the few kilobytes of `VideoInfo` to the plane.
#
# **Why it is not in git.** 40 MB per platform, and yt-dlp ships about monthly:
# committing it would add 40 MB to history every time it is bumped, on a
# repository whose whole `.git` is 104 MB. It is regenerable from a pinned
# version and a published checksum, which is the same judgement
# `docaget/cache/embed/` records for its 1538 float32 blobs.
#
# A build without it fails at the bundler, and a `tauri dev` without it fails
# at `binary()` with `ytdlp_missing`, which the app renders as a sentence
# telling you to run this. Neither is silent.
#
#   ./fetch.sh                       # this machine's triple
#   ./fetch.sh x86_64-pc-windows-msvc aarch64-apple-darwin
set -euo pipefail
cd "$(dirname "$0")"

# Pinned, and bumped deliberately. yt-dlp breaks when YouTube changes, so this
# will go stale — that is the cost of the choice and the reason `BRAIN_YTDLP`
# exists as an override. Checksums are the ones published beside the release.
VERSION="2026.08.19"
BASE="https://github.com/yt-dlp/yt-dlp/releases/download/${VERSION}"

asset_for() {
  case "$1" in
    *-linux-gnu|*-linux-musl) echo "yt-dlp_linux" ;;
    *-apple-darwin)           echo "yt-dlp_macos" ;;
    *-windows-msvc|*-windows-gnu) echo "yt-dlp.exe" ;;
    *) echo "" ;;
  esac
}

sum_for() {
  case "$1" in
    yt-dlp_linux) echo "58162f9bfdc27458ea47bfcb311cf47028f17d8154a8bf7d689861d46399230a" ;;
    yt-dlp_macos) echo "0f192b7ec147ab6288885d6351d9ab67367640029b4377576ef46dd79cf7b202" ;;
    yt-dlp.exe)   echo "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a" ;;
  esac
}

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  targets=("$(rustc -vV | sed -n 's/^host: //p')")
fi

for target in "${targets[@]}"; do
  asset="$(asset_for "$target")"
  if [ -z "$asset" ]; then
    echo "no yt-dlp build for '$target'" >&2
    exit 1
  fi
  # The name Tauri's bundler looks for: `externalBin` names `binaries/yt-dlp`
  # and the triple is appended. It strips the triple again when it copies the
  # file next to the app binary, which is where `ytdlp::binary()` looks first.
  out="yt-dlp-${target}"
  case "$target" in *windows*) out="${out}.exe" ;; esac

  echo "==> ${asset} ${VERSION} -> ${out}"
  curl -sSL --fail -o "$out.part" "${BASE}/${asset}"
  echo "$(sum_for "$asset")  $out.part" | sha256sum -c - >/dev/null
  chmod +x "$out.part"
  mv "$out.part" "$out"
done

echo "done. $(ls yt-dlp-* 2>/dev/null | tr '\n' ' ')"
