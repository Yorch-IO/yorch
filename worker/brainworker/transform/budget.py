"""How many library queries this transformation may make, and who may spend them.

The brief requires a query limit calculated dynamically from relevance, and
forbids introducing a second relevance mechanism. There is no need for one: the
existing mechanism already produces the number. `retrieve.search` takes a
`supported` out-parameter carrying how many chunks cleared the dense floor
`MIN_SCORE = 0.60`, and `effort.effective_style_level` already uses exactly that
signal to tell a narrow question from a broad one — measured on the real corpus
at 48 of 48 for a broad question, 27 for a middling one and **3** for a narrow
one, where the evidence count was inert because *"the fused RRF output carries
no score floor, so hybrid search returns as many rows as it is asked for."*

So the budget is a function of `supported`, and of nothing else. This module is
that function plus the arithmetic of spending it, and it is pure: no store, no
provider, no Temporal. Everything it decides is a number a test can assert.
"""

from __future__ import annotations

import math

from .reading import Passage

#: How many places in the source the probe samples, evenly spaced. Eight rather
#: than more because the probe is a measurement of the *library*, not of the
#: document: a ninth sample of a book the library has nothing on tells nobody
#: anything, and eight query embeddings is about $0.000032.
PROBE_SAMPLE = 8

#: How much of a sampled chapter is embedded. Long enough to be about something,
#: short enough that the vector is about the chapter rather than about prose in
#: general.
PROBE_CHARS = 600

#: Supporting chunks a query has to earn. One query per eight chunks that
#: cleared the floor — a guess, and stated as one: what it encodes is "a library
#: that has a lot to say deserves to be asked more", and the constant that
#: converts "a lot" into "how many" has never been measured. The first real run
#: is what should move it, and the recipe is in `doc/TRANSFORM.md`.
QUERIES_PER_SUPPORTED = 8

#: The floor, per enabled purpose, when the library supports anything at all.
#: Two rather than one, because a single query has no way to disagree with
#: itself and `contradiction` needs at least the chance.
MIN_QUERIES = 2

#: The ceiling per purpose, so one purpose cannot consume a whole run's budget.
MAX_PER_PURPOSE = 30

#: The absolute ceiling, whatever the library holds.
MAX_QUERIES = 120

#: And a second ceiling, tied to the work's own size rather than to the
#: library's. Without it a short document against a 73-book library buys a large
#: research bill for a small work, which is the shape of over-spending nobody
#: notices because every individual query is defensible.
QUERIES_PER_CHAPTER = 3


def probe_passages(
    passages: list[Passage],
    sample: int = PROBE_SAMPLE,
) -> list[str]:
    """The texts the probe embeds: evenly spaced places in the source.

    Evenly spaced rather than the first *n*, because the front matter of a book
    is the least characteristic part of it — a preface is about the author, an
    index is about nothing — and a probe that read only the opening would
    measure the library's coverage of prefaces.

    **Over passages, not over chapters**, and that is a fix rather than a
    preference. It sampled chapter openings, which is a good spread when a
    document has eight of them and a terrible one when it has one — and on this
    corpus, measured on the first real run, **27 of 52 documents have exactly
    one detected chapter** and 37 of 52 were under-sampled. The worst case was a
    **500-passage book probed once, on its first 600 characters**, with the
    whole run's research budget derived from that.

    The cause is not this function's: `build_chunks` *consumes* a heading
    paragraph, so a document whose headings were never detected is genuinely
    indexed as one untitled chapter, and this corpus has recorded heading
    defects that produce exactly that. A chapter start is only a passage anyway,
    so sampling the finer unit loses nothing and stops depending on a field half
    the corpus does not have.
    """
    if not passages or sample <= 0:
        return []
    out: list[str] = []
    for i in _spread(len(passages), sample):
        text = ""
        for passage in passages[i:]:
            text = f"{text}\n\n{passage.text}".strip() if text else passage.text
            if len(text) >= PROBE_CHARS:
                break
        text = text[:PROBE_CHARS].strip()
        if text:
            out.append(text)
    return out


def _spread(total: int, sample: int) -> list[int]:
    """`sample` indices spread across `range(total)`, first and last included."""
    if total <= sample:
        return list(range(total))
    if sample == 1:
        return [0]
    step = (total - 1) / (sample - 1)
    return sorted({int(round(i * step)) for i in range(sample)})


def budget_for(supported: int, purposes: list[str] | tuple[str, ...],
               chapters: int) -> int:
    """The whole run's research allowance, from what the probe measured.

    **Zero is a real answer and is not rounded up to a courtesy query.** A
    library where nothing clears the floor for any sampled chapter of this
    document genuinely has nothing to add, and asking it anyway would spend
    money to be told so once per chapter. That is `OffCorpus` read at corpus
    scale rather than at question scale, and the gate says so in words.
    """
    if not purposes or supported <= 0 or chapters <= 0:
        return 0
    per_purpose = min(
        MAX_PER_PURPOSE,
        max(MIN_QUERIES, supported // QUERIES_PER_SUPPORTED),
    )
    return max(
        0,
        min(
            per_purpose * len(purposes),
            MAX_QUERIES,
            QUERIES_PER_CHAPTER * chapters,
        ),
    )


def allowance_for(remaining: int, chapters_left: int) -> int:
    """What one chapter may spend out of what is left: an even share, and that is all.

    **The even share is already self-balancing, which is why there is no slack
    term.** The workflow subtracts what a chapter *spent*, never what it was
    allowed, so a chapter that asks two questions of a five-query allowance
    returns three to the pool and every later chapter's share goes up. A slack
    term on top of that was tried and measured against its own arithmetic: with
    twenty queries over six chapters it left the last two with **nothing**,
    because the slack compounds — each chapter takes its share plus two, out of
    a pool the previous chapter has already taken its slack from.

    This is the shape `doc/CHANNEL.md` records for the probe cap: a budget
    across rounds rather than per round. What it buys is that a chapter early in
    a work cannot spend the whole run's research, and what it costs is nothing,
    because unspent allowance is not lost.

    The counter itself lives in the **workflow**, and this returns an *input* to
    the activity: a retried attempt replays with the identical number and cannot
    overrun. A counter read from a file inside the activity would be exactly the
    shape of the recorded double-spend — two overlapping attempts, both reading
    the same figure, both spending it.
    """
    if remaining <= 0 or chapters_left <= 0:
        return 0
    return max(0, min(remaining, math.ceil(remaining / chapters_left)))
