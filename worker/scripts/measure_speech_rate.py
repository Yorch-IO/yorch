#!/usr/bin/env python3
"""Measure how many characters of transcript a second of speech produces.

`videosource.CHARS_PER_SECOND_OF_SPEECH` projects a correction bill from a
video's duration, and it is used on exactly one path: a video with **no**
captions, where there is no text to count until Amazon has been paid. Until this
script ran it was reasoned from "about 160 words a minute at about six
characters a word" and nothing else — the same shape as `EVALSET_OUTPUT_PER_CALL`,
which was a guess at the length of a question and under-reported a real bill by
3.6x in the one direction the rule forbids.

**It measures captions, and the thing it projects is Amazon's output.** Those are
not the same text: measured on `jNQXAC9IVRw`, the manual captions came to 217
characters and Transcribe's own transcript of the same audio to 225 — about 4%
more, because Transcribe keeps the fillers a human caption writer drops. So the
figure this prints is a floor, and the constant should sit at or above it.

Free: captions and metadata are both free, and nothing here starts a
transcription job.

    uv run python scripts/measure_speech_rate.py --queries "predicación" -n 25
"""

from __future__ import annotations

import argparse
import statistics
import sys

from docagent import transcript as dt


def rate(info: dict, prefer: list[str]) -> tuple[float, int, int, str] | None:
    """Characters per second for one video, the way the pipeline would count.

    Through the real `group_cues`, not a naive character count of the caption
    file: what the estimate needs to project is the size of the paragraph stream
    that reaches correction, and grouping is what produces it.
    """
    from brainworker.activities import video as vid

    duration = int(info.get("duration") or 0)
    if duration <= 0 or info.get("is_live"):
        return None
    tracks = vid._tracks(info)
    chosen = vid._choose_track(tracks, prefer)
    if chosen is None:
        return None
    data = vid._download_caption(vid._caption_url(info, chosen), chosen.language)
    if not data:
        return None
    cues = dt.parse_vtt(data)
    if chosen.kind == "auto":
        cues = dt.dedupe_rolling(cues)
    if not cues:
        return None
    groups = dt.group_cues(cues)
    chars = sum(len(g.text) for g in groups) + 2 * max(0, len(groups) - 1)
    return chars / duration, chars, duration, chosen.kind


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", required=True)
    ap.add_argument("-n", type=int, default=10, help="videos per query")
    ap.add_argument("--lang", nargs="+", default=["es"])
    args = ap.parse_args()

    from brainworker.activities import video as vid

    rows: list[tuple[float, int, int, str, str]] = []
    for query in args.queries:
        try:
            found = vid._extract_info(f"ytsearch{args.n}:{query}")
        except Exception as e:  # noqa: BLE001 - a failed search is not fatal
            print(f"  ! search {query!r}: {e}", file=sys.stderr)
            continue
        for entry in found.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            try:
                info = vid._extract_info(f"https://youtu.be/{entry['id']}")
                got = rate(info, args.lang)
            except Exception:  # noqa: BLE001 - skip what will not load
                continue
            if got is None:
                continue
            r, chars, dur, kind = got
            rows.append((r, chars, dur, kind, (info.get("title") or "")[:48]))
            print(f"  {r:5.2f} c/s  {dur:>6}s  {chars:>7} chars  {kind:<7} {rows[-1][4]}")

    if not rows:
        print("nothing measurable", file=sys.stderr)
        return 1

    rates = sorted(r for r, *_ in rows)
    total_chars = sum(c for _, c, *_ in rows)
    total_secs = sum(d for _, _, d, *_ in rows)
    print(f"\nvideos            : {len(rows)}")
    print(f"total             : {total_secs/3600:.2f} h, {total_chars:,} chars")
    # The pooled rate, not the mean of per-video rates: the constant multiplies a
    # duration, so what matters is characters per second over the whole sample.
    print(f"pooled            : {total_chars/total_secs:.2f} c/s   <- the figure")
    print(f"median            : {statistics.median(rates):.2f} c/s")
    print(f"p10 / p90         : {rates[len(rates)//10]:.2f} / {rates[-max(1,len(rates)//10)]:.2f}")
    print(f"max               : {rates[-1]:.2f} c/s")
    print(
        "\nThe constant must cover the fast end, not the middle: under-reporting\n"
        "a bill is the failure the gate exists to prevent. Add ~4% for Amazon's\n"
        "fillers, which captions do not carry."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
