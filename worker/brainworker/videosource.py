"""The pure facts about a video: which one it is, and when things were said.

Two halves, both arithmetic, both with no I/O — the same testability decision
``auditversion.py`` embodies on the other side of the product. What is worth
asserting here is the *mapping*, and a test that needed a network to check a
timestamp would run rarely enough to be worth nothing.

**Identity.** A video's document identity is its id, never its title: a title is
something the uploader can change, and ``document_id`` is
``digest(library, source_key)``. :func:`video_id` is what turns any of the six
shapes a YouTube link comes in into that one stable string.

**Time.** ``docagent.transcript`` groups cues into paragraphs, one per timed
group. ``docagent.chunk.Chunk`` records ``para_from``/``para_to``. So a chunk's
time span is ``table[para_from].start_s`` to ``table[para_to].end_s``, and
because ``correct.correct_paragraphs`` returns the same number of paragraphs in
the same order, that survives correction with no byte arithmetic at all.

The single thing that can break it is a correction that returns a paragraph
containing a blank line: the rejoin then yields more paragraphs than went in and
every timestamp after that point silently belongs to the wrong chunk.
:func:`repair_paragraphs` collapses that before the join and
:func:`assert_aligned` refuses afterwards — repair first, because a hard failure
*after* correction has been paid for is the worst outcome available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

#: A YouTube id is eleven characters of URL-safe base64. Pinned rather than
#: matched loosely because this string becomes `source_key`, and therefore the
#: document's identity: a lax pattern would let a tracking parameter mint a
#: second document for a video already indexed.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")

_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
     "youtu.be", "www.youtu.be"}
)

#: Two blank lines are the paragraph separator `docagent.chunk.split_paragraphs`
#: splits on, so one appearing *inside* a paragraph is what shifts the index.
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


class NotAVideoUrl(ValueError):
    """A string that is not a link to a single YouTube video."""


class TimeAlignmentLost(RuntimeError):
    """The paragraph-to-time mapping no longer holds, so no timestamp is safe."""


@dataclass(frozen=True)
class ParagraphTime:
    """When paragraph ``idx`` of the byte stream was spoken."""

    idx: int
    start_s: float
    end_s: float


# --- identity ----------------------------------------------------------------


def video_id(url: str) -> str:
    """The eleven-character id in a YouTube link, whatever shape it arrives in.

    Accepts ``watch?v=``, ``youtu.be/``, ``/shorts/``, ``/embed/``, ``/live/``
    and ``/v/``, with any extra query parameters. Refuses a bare playlist or
    channel link, because those are not one video and this product does not
    expand them — a caller that silently indexed the first video of a playlist
    would be answering a question nobody asked.
    """
    raw = (url or "").strip()
    if not raw:
        raise NotAVideoUrl("empty")
    if "//" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":")[0]
    if host not in _HOSTS:
        raise NotAVideoUrl(f"not a YouTube host: {parsed.netloc!r}")

    candidate = ""
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path in ("/watch", "/watch/"):
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        for prefix in _PATH_PREFIXES:
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix) :].split("/")[0]
                break

    if not _ID_RE.match(candidate):
        raise NotAVideoUrl(f"no video id in {url!r}")
    return candidate


def source_key(vid: str) -> str:
    """The document's stable key. Namespaced, because a bare id is not obviously
    a video to anyone reading a catalog row."""
    return f"youtube/{vid}"


def watch_url(vid: str, start_s: float | None = None) -> str:
    """A link to the video, optionally at the second something was said.

    ``youtu.be`` rather than ``watch?v=`` because the deep link then carries one
    parameter instead of two, and the whole string ends up inside a citation's
    locator — which is what a person reads.

    The offset is floored, never rounded: rounding up can land after the word
    that is being cited.
    """
    if start_s is None:
        return f"https://youtu.be/{vid}"
    return f"https://youtu.be/{vid}?t={max(0, int(start_s))}"


def hhmmss(seconds: float) -> str:
    """``12:34`` under an hour, ``1:02:34`` over it. For a human, not a machine."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --- time --------------------------------------------------------------------


def table_from_groups(groups) -> list[ParagraphTime]:
    """The paragraph-index-to-time table, from ``transcript.group_cues`` output.

    Deliberately takes anything with ``start_s``/``end_s`` rather than importing
    the engine's dataclass, so this module stays free of the engine and the two
    can be tested apart.
    """
    return [ParagraphTime(i, g.start_s, g.end_s) for i, g in enumerate(groups)]


def repair_paragraphs(paragraphs: list[str]) -> list[str]:
    """Collapse any blank line a correction introduced *inside* a paragraph.

    Safe, because correction's own contract is accents, spelling, agreement and
    punctuation and it "explicitly does not rewrite the author's prose" — a
    blank line in its output is a model artefact, not content. Collapsing it to
    a single newline keeps every character and keeps the paragraph count, which
    is what the timestamps are indexed by.
    """
    return [_BLANK_LINE_RE.sub("\n", p).strip() for p in paragraphs]


def assert_aligned(paragraphs: list[str], table: list[ParagraphTime]) -> None:
    """Refuse to go on if the paragraph-to-time mapping no longer holds.

    Called after the repair and before anything derives a timestamp. A mismatch
    is unrecoverable rather than approximate: with the indices shifted, every
    citation after the shift would carry a confident link to the wrong moment,
    and nothing downstream could tell.
    """
    empty = [i for i, p in enumerate(paragraphs) if not p.strip()]
    if empty:
        raise TimeAlignmentLost(
            f"{len(empty)} paragraph(s) are empty and would be dropped by the "
            f"join, shifting every later timestamp; first at index {empty[0]}"
        )
    if len(paragraphs) != len(table):
        raise TimeAlignmentLost(
            f"{len(paragraphs)} paragraphs against {len(table)} timed groups; "
            "the paragraph index is what carries the time, so no timestamp "
            "derived from this is trustworthy"
        )


def span_for(
    para_from: int, para_to: int, table: list[ParagraphTime]
) -> tuple[float, float]:
    """The time span of a chunk covering paragraphs ``para_from..para_to``.

    Raises rather than clamping on an index the table does not hold: a clamped
    span is a wrong answer wearing the shape of a right one.
    """
    if not table:
        raise TimeAlignmentLost("no time table")
    if not 0 <= para_from < len(table) or not 0 <= para_to < len(table):
        raise TimeAlignmentLost(
            f"paragraphs {para_from}..{para_to} against a table of {len(table)}"
        )
    if para_to < para_from:
        raise TimeAlignmentLost(f"paragraphs run backwards: {para_from}..{para_to}")
    return table[para_from].start_s, table[para_to].end_s


def uncovered_paragraphs(chunks, table: list[ParagraphTime]) -> list[int]:
    """Paragraph indices that reached no chunk. Empty is the healthy answer.

    Two paths in ``docagent.chunk.build_chunks`` discard a paragraph in silence,
    and both are content loss rather than a labelling mistake:

    - **A heading is never chunked.** ``build_chunks`` ``continue``\\ s on one,
      so the paragraph's *text* is dropped and only its title survives, in the
      breadcrumb. Right for a book; for a transcript it means a cue group was
      indexed nowhere, and the sentence it held cannot be found or cited.
    - **A run shorter than ``min_chunk_chars`` with nothing before it to merge
      into is dropped** outright.

    ``chunk_rules`` is meant to make the first unreachable and the grouper's
    merge rule the second. This is what says so out loud instead of assuming it,
    and it costs one pass over chunks the caller already holds.
    """
    seen: set[int] = set()
    for c in chunks:
        seen.update(range(c.para_from, c.para_to + 1))
    return [t.idx for t in table if t.idx not in seen]


# --- what a video is going to cost -------------------------------------------

#: Amazon Transcribe bills in whole seconds with a floor per request.
#:
#: Rounding *up* to it is the safe direction: the rule the estimate has to obey
#: is that it may not undershoot, and a fifteen-second floor applied to a
#: ten-second clip is the difference between quoting the bill and quoting less
#: than it.
TRANSCRIBE_MINIMUM_S = 15.0

#: Characters of transcript per second of speech, for the low end of a
#: correction quote when there is no text to count yet.
#:
#: **Measured 2026-09-05 over 11.21 hours of real Spanish preaching and theology
#: video** — 20 videos, 548,750 characters, the domain this corpus actually
#: indexes — by `scripts/measure_speech_rate.py`, which runs the captions through
#: the same `group_cues` the pipeline uses:
#:
#:     pooled 13.59 c/s · median 12.40 · p10 11.46 · p90 14.69 · max 15.24
#:
#: The measurement is of **captions**, and what this projects is **Amazon's**
#: output. They are not the same text: on `jNQXAC9IVRw` the manual captions came
#: to 217 characters and Transcribe's transcript of the same audio to 225, about
#: 4% more, because Transcribe keeps the fillers a caption writer drops. So the
#: pooled figure is a floor, and 13.59 x 1.04 = 14.13 is the typical video.
#:
#: 14.5 rather than 14.13 for a little room, and **rather than the 16 this was
#: guessed at**: 16 sits above the *fastest* video in an 11-hour sample, so it
#: was not a low end at all — it over-reported every quote, which misleads a user
#: into declining affordable work exactly as much as under-reporting misleads
#: them into approving an expensive one. The guess happened to be safe and was
#: still wrong.
CHARS_PER_SECOND_OF_SPEECH = 14.5

#: How far above that the high end of the range is drawn.
#:
#: The pair binds the two ends the way `OUTPUT_SPREAD` does for semantics: the
#: low figure stays within reach of a typical video, the high one covers the
#: worst. 14.5 x 1.40 = **20.3 c/s**, against a measured maximum of 15.24 and
#: 15.85 with Transcribe's uplift — about 28% of headroom for content the sample
#: did not contain, a faster register or a denser language.
#:
#: **This constant was declared and used by nothing until 2026-09-05**, which is
#: the same defect as `ChunkNode.sheet`: it looked like it did something, a test
#: asserted a property of it, and no quote was ever any wider for it. The
#: Transcribe path's character count is the single most uncertain input in the
#: product, so a range is worth more there than anywhere else it already exists.
SPEECH_RATE_SPREAD = 1.40


def transcribe_usd(duration_s: float, usd_per_minute: float) -> float | None:
    """What Amazon Transcribe will bill for this video, or None if unpriced.

    None rather than 0.0 when no rate is configured, for the reason
    `price_for()` gives: "we cannot price this" and "this is free" are different
    answers, and a gate that rendered one as the other would invite somebody to
    approve an unknown amount believing it was nothing.
    """
    if usd_per_minute <= 0:
        return None
    return max(TRANSCRIBE_MINIMUM_S, float(duration_s)) / 60.0 * usd_per_minute


def projected_characters(duration_s: float, *, high: bool = False) -> int:
    """How much text a video of this length is likely to hold.

    Only ever reached when there are no captions to count — with them the text is
    in hand before the gate and the quote is exact. ``high`` gives the top of the
    range, which is what makes the Transcribe path's estimate say how uncertain
    it is rather than quoting one number it cannot stand behind.
    """
    rate = CHARS_PER_SECOND_OF_SPEECH * (SPEECH_RATE_SPREAD if high else 1.0)
    return int(max(0.0, float(duration_s)) * rate)


def correction_default(caption_kind: str | None) -> bool:
    """Whether correction should start ticked for a transcript from this source.

    Not one rule for every video, because the three sources arrive in different
    shape and correction is the stage that dominates the bill:

    - **Automatic captions: on.** YouTube's auto-captions carry no punctuation
      at all, which is the deficit correction is good at closing and the one
      `docagent.correct.verify` will not block — punctuation is neither a proper
      noun, a figure nor a scripture reference, so a repunctuated paragraph
      passes the gate that a re-spelled name would fail.
    - **Manual captions: off.** Someone already wrote and punctuated them.
    - **Amazon Transcribe: off.** It punctuates its own output.

    This is a *default*, not a rule: it decides which box is ticked when the gate
    opens, and a person can tick or untick it before approving. It is a default
    at all because correction's value on a transcript is **unmeasured** — the
    $0.0334 that sets the gate's position was measured on a book — and the
    honest place to put an unmeasured cost is behind a box somebody chose.
    """
    return caption_kind == "auto"
