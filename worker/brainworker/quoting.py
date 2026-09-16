"""Finding a model-supplied quote in the text it claims to come from.

One rule, in one place, because it now has two callers and they must not drift:
`activities/paid.py` checks the quote a *claim* carries, and
`channel/topics.py` checks the quote a *topic* carries. A second copy of this
matcher that tolerated one more thing than the other would make one of the two
features quietly accept quotes the other refuses, and neither would fail.

**Matching tolerates differences in whitespace and nothing else.** A model asked
for a verbatim span reliably reproduces the words and unreliably reproduces the
line breaks the extractor put between them, so a literal `in` test fails on
quotes that really are present. Anything beyond whitespace — a fixed accent, a
normalised quotation mark, a dropped clause — is a quote the text does not
contain, and it has to fail.

The idea of asking for the quote is ported from the `Claim Source Text` field of
microsoft/graphrag's claim-extraction prompt (MIT). **The checking is not**:
graphrag asks for the quote and nothing ever verifies it, which leaves exactly
the hole this module exists to close — a quotation nobody can check against the
source still looks precisely like evidence.

Note what is deliberately *not* here: the conversion from a character index to a
byte offset. That belongs to the caller that stores a span, because only that
caller knows which byte stream the span indexes — and getting it wrong is a
recorded defect, not a hypothetical one: adding `re.Match.start()` to a byte
offset left 295 of 13,966 stored claim spans resolving to the quote they were
recorded for, with nothing failing anywhere.
"""

from __future__ import annotations

import re


def find(quote: str, text: str) -> re.Match[str] | None:
    """Where `quote` appears in `text`, or None if it does not appear at all.

    The match's `group(0)` is the quote **as the text spells it**, which is what
    should be stored: the model's spacing is not the document's.
    """
    tokens = (quote or "").split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    return re.search(pattern, text)


def verbatim(quote: str, text: str) -> str | None:
    """The quote as the text spells it, or None when the text does not contain it."""
    match = find(quote, text)
    return None if match is None else match.group(0)
