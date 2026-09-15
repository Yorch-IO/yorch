"""Timed transcripts: parsing, and the grouping that decides what a paragraph is.

A video has no byte stream until something transcribes it, and the fact a reader
wants back is a *time*, not an offset. This module is the half of that which is
pure arithmetic: it normalises either transcript source into one list of
:class:`Cue`, and groups those cues into paragraphs that each carry a time span.

**The grouping is the whole design.** ``docagent.chunk.Chunk`` already records
``para_from``/``para_to`` — the paragraph index range a chunk covers — and
``correct.correct_paragraphs`` returns the same number of paragraphs in the same
order. So if one paragraph is one timed group, a chunk's time span is
``groups[para_from].start_s`` to ``groups[para_to].end_s``, and it survives
correction with no byte arithmetic at all. Everything here exists to make that
mapping true.

One constraint on the group size follows from it, and it is load-bearing:
``chunk._split_oversized`` cuts an over-long paragraph into several units that
all carry the *same* ``para_idx``, so a group that reaches
``ChunkRules.hard_cap_chars`` becomes several chunks reporting one span. That is
a loss of precision rather than a wrong answer, and it is avoided by never
getting near the cap — hence :data:`GROUP_HARD_CAP` at well under half of it.

The opposite bound is *not* needed: chunks aggregate groups, so a short group is
merged forward by the windower like any other short paragraph and ``para_to``
moves with it.

Nothing here does I/O, which is the same decision ``docagent.chunk`` embodies:
what is worth asserting is the arithmetic, and a test that needed a network to
check a timestamp would run rarely enough to be worth nothing.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace

#: Aim for a group about this many bytes before looking for a sentence end.
#:
#: At roughly 900 characters of Spanish per minute of speech this is about
#: twenty seconds. The number does **not** control how accurate a chunk's
#: timestamp is — chunks are built from whole paragraphs, so a chunk always
#: starts exactly at a group boundary — it controls how naturally the text reads
#: and how far a group is from the cap.
GROUP_TARGET_CHARS = 400

#: A group is closed here whatever the punctuation is doing.
#:
#: ``ChunkRules.hard_cap_chars`` defaults to 2000 and this is well under half of
#: it, so ``_split_oversized`` is never reached and no two chunks ever have to
#: share one time span. Anything approaching the cap would silently trade
#: precision for nothing.
GROUP_HARD_CAP = 900

#: A silence longer than this closes a group wherever it falls.
#:
#: Speakers pause between thoughts, and a pause is the one paragraph boundary a
#: transcript actually offers — punctuation in an ASR transcript is invented by
#: the model, and rolling captions carry none at all.
GROUP_MAX_GAP_S = 2.0

# WebVTT and SRT timestamps: [HH:]MM:SS.mmm, with a comma accepted for SRT.
_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"^{_TS}\s*-->\s*{_TS}")

# YouTube's auto-captions carry per-word karaoke markup: <00:00:01.234> between
# words and <c>…</c> around them. Both are presentation, not content.
_TAG_RE = re.compile(r"<[^>]*>")

_WS_RE = re.compile(r"\s+")

# Punctuation that closes a sentence. Deliberately narrower than
# `chunk.SENTENCE_END_RE`, which also requires trailing whitespace — here the
# candidate is always the end of the accumulated text, so there is none.
_SENTENCE_END = tuple(".!?…")


@dataclass(frozen=True)
class Cue:
    """One timed fragment, as either source produced it.

    A VTT cue is a line; a Transcribe cue is a word. The grouper does not care,
    which is what lets one rule serve both.
    """

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class TimedParagraph:
    """One paragraph of the stream that will be chunked, and when it was said.

    ``cues`` is kept because it is the cheapest evidence that grouping did
    something: a transcript whose paragraphs each hold one cue has not been
    grouped, and that is visible in the artifact without re-deriving anything.
    """

    start_s: float
    end_s: float
    text: str
    cues: int


class TranscriptError(ValueError):
    """A transcript that cannot be read as one. Never raised for an empty one."""


# --- parsing -----------------------------------------------------------------


def parse_vtt(data: bytes) -> list[Cue]:
    """Parse WebVTT (or SRT, which differs only in the decimal separator).

    Returns cues in file order with their markup stripped. Cues carrying no text
    after stripping are dropped rather than kept as empty: an empty cue would
    become an empty paragraph, and ``join_paragraphs`` drops those anyway — but
    it would drop them *after* the time table was built, which is exactly how an
    index shift is introduced.
    """
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    cues: list[Cue] = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        # A cue may be preceded by an identifier line, so find the timing line
        # rather than assuming it is first.
        for i, line in enumerate(lines):
            m = _CUE_RE.match(line.strip())
            if m:
                break
        else:
            continue  # header block, NOTE block, or a stray identifier
        body = _clean(" ".join(lines[i + 1 :]))
        if not body:
            continue
        cues.append(Cue(_seconds(m, 1), _seconds(m, 5), body))
    return cues


def parse_transcribe(payload: dict) -> list[Cue]:
    """Parse an Amazon Transcribe results document into one cue per word.

    Word granularity rather than the newer ``audio_segments``, because
    ``items`` is present in every response shape this could be handed and the
    grouper is what decides paragraph boundaries anyway. Punctuation items carry
    no timings and are appended to the word before them, which is where they
    belong and where a naive pass would instead emit a timeless cue.
    """
    try:
        items = payload["results"]["items"]
    except (KeyError, TypeError) as e:
        raise TranscriptError(f"no results.items in the transcript: {e}") from e

    cues: list[Cue] = []
    for item in items:
        alts = item.get("alternatives") or [{}]
        content = _clean(alts[0].get("content", ""))
        if not content:
            continue
        if item.get("type") == "punctuation":
            if cues:
                last = cues[-1]
                cues[-1] = Cue(last.start_s, last.end_s, last.text + content)
            continue
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
        except (KeyError, TypeError, ValueError):
            # A pronunciation item with no timing cannot be placed. Attaching it
            # to its predecessor keeps the words but would stretch that word's
            # span over silence it does not cover; dropping it would lose text
            # from the index. Text wins — the span is already a range.
            if cues:
                last = cues[-1]
                cues[-1] = Cue(last.start_s, last.end_s, f"{last.text} {content}")
            continue
        cues.append(Cue(start, end, content))
    return cues


def _seconds(m: re.Match[str], group: int) -> float:
    """Read one ``[HH:]MM:SS.mmm`` timestamp out of a ``-->`` match."""
    hours = int(m.group(group) or 0)
    minutes = int(m.group(group + 1))
    secs = int(m.group(group + 2))
    frac = m.group(group + 3).ljust(3, "0")
    return hours * 3600 + minutes * 60 + secs + int(frac) / 1000.0


def _clean(s: str) -> str:
    """Strip caption markup and collapse whitespace, in that order."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", s))).strip()


# --- rolling-caption repair --------------------------------------------------


def dedupe_rolling(cues: list[Cue]) -> list[Cue]:
    """Remove the repetition YouTube's auto-captions produce by scrolling.

    An auto-caption track shows two lines at a time and re-emits the lower one
    as it scrolls up, so the raw cue list reads "A" / "A B" / "B C" / "C D".
    Concatenated as-is, every line is indexed twice — which doubles the
    correction bill, doubles the embedded text and gives the same sentence two
    different timestamps.

    The repair is to drop, from each cue, the longest word-aligned prefix that
    the previous cue already ended with. Word-aligned rather than character
    aligned because a partial word overlap is a coincidence, not a scroll.
    """
    out: list[Cue] = []
    for cue in cues:
        text = cue.text if not out else _drop_overlap(out[-1].text, cue.text)
        if not text:
            continue
        out.append(Cue(cue.start_s, cue.end_s, text))
    return out


def _drop_overlap(prev: str, new: str) -> str:
    """``new`` with the longest word-aligned prefix shared with ``prev``'s suffix
    removed. Returns ``""`` when ``new`` adds nothing at all."""
    p = prev.split()
    n = new.split()
    if not p or not n:
        return new
    for size in range(min(len(p), len(n)), 0, -1):
        if p[-size:] == n[:size]:
            return " ".join(n[size:])
    return new


# --- grouping ----------------------------------------------------------------


def group_cues(
    cues: list[Cue],
    *,
    target_chars: int = GROUP_TARGET_CHARS,
    hard_cap_chars: int = GROUP_HARD_CAP,
    max_gap_s: float = GROUP_MAX_GAP_S,
) -> list[TimedParagraph]:
    """Pack cues into paragraphs, each carrying the span of the cues inside it.

    A group closes on the first of: a silence longer than ``max_gap_s``; a
    sentence end at or past ``target_chars``; or ``hard_cap_chars``, whatever the
    text is doing. The three are ordered by how much they mean — a pause is the
    speaker's own boundary, punctuation in an ASR transcript is the model's
    guess, and the cap is ours.

    Every cue's text appears in exactly one group, in order. That property is
    what makes a timestamp checkable, and it is asserted rather than assumed.

    A group exceeds ``hard_cap_chars`` only when a *single* cue does, which
    nothing can split without inventing a time for the halves.
    """
    if hard_cap_chars < target_chars:
        raise ValueError("hard_cap_chars must not be below target_chars")

    groups: list[TimedParagraph] = []
    held: list[Cue] = []

    def flush() -> None:
        nonlocal held
        if not held:
            return
        groups.append(
            TimedParagraph(
                start_s=held[0].start_s,
                end_s=max(c.end_s for c in held),
                text=" ".join(c.text for c in held),
                cues=len(held),
            )
        )
        held = []

    def size(held: list[Cue]) -> int:
        return sum(len(c.text) + 1 for c in held) - 1 if held else 0

    for cue in cues:
        if held and cue.start_s - held[-1].end_s > max_gap_s:
            flush()
        # Closed *before* appending, so the cap is genuinely a cap. Checking
        # after would let a group overshoot by the length of whatever cue
        # happened to cross it, which is the difference between a bound and a
        # suggestion — and the bound is what keeps `_split_oversized`
        # unreachable.
        if held and size(held) + 1 + len(cue.text) > hard_cap_chars:
            flush()
        held.append(cue)
        if size(held) >= target_chars and cue.text.rstrip().endswith(_SENTENCE_END):
            flush()
    flush()
    return groups


def to_paragraphs(groups: list[TimedParagraph]) -> list[str]:
    """The text of each group, in order — what ``join_paragraphs`` is handed.

    Trivial on purpose, and named, because the pairing of this list with the
    groups it came from *is* the time table: index i of the byte stream's
    paragraphs is group i. Every guard downstream is a restatement of that.
    """
    return [g.text for g in groups]


# --- how a transcript is chunked ---------------------------------------------


def chunk_rules(base: "ChunkRules | None" = None) -> "ChunkRules":
    """The chunking rules a transcript needs, which differ in exactly one way.

    **Heading detection is switched off**, by capping both heading lengths at
    zero. ``chunk.heading_level`` returns 0 whenever a candidate is longer than
    the cap for its level, and every guard — the learned patterns included —
    goes through those same two caps, so zero disables all of them without a
    special case anywhere in the chunker.

    It has to be off. ``HEADING_RE`` matches any paragraph opening with a number
    and a word, and a transcript is full of them: "1975 fue el año" is a level-1
    heading by shape, and would become the chapter of every chunk after it. That
    is the failure this repository already measured on citations numbered like
    headings — 428 of 4,239 chunks carrying a footnote as their breadcrumb — and
    a video has no headings at all for a real rule to find.

    So a transcript chunk carries no chapter and no section, and therefore no
    breadcrumb. That is honest: a talk has no table of contents. Whether putting
    the video's *title* there would help retrieval is a real question and a
    measurable one, and it is deliberately not answered by guessing here.
    """
    from .chunk import ChunkRules

    base = base or ChunkRules()
    return replace(base, heading_l1_max=0, heading_l2_max=0)


def classify(_text: str, _rules: "ChunkRules") -> str:
    """Every chunk of a transcript is a transcript.

    Injected into ``build_chunks`` rather than applied afterwards, because a
    change of kind is a chunk boundary: relabelling finished chunks can only
    rename what the default rules already cut. Here it also suppresses the
    question and footnote rules, which would otherwise fire on a speaker asking
    something rhetorically.
    """
    from .chunk import KIND_TRANSCRIPT

    return KIND_TRANSCRIPT
