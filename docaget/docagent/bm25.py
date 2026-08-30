"""BM25 sparse vectors for the lexical leg of hybrid search.

Port of ``sociologia/index/bm25.go``. Dense retrieval understands concepts but
not literal strings, which matters in documents full of proper names and
scripture references: querying those against a dense-only index scored barely
above the noise floor (0.664 vs a 0.55 floor, measured).

The IDF term is NOT baked into the stored values — Qdrant supplies it via the
collection's ``modifier: "idf"``. Documents carry only the BM25 term-frequency
normalisation and queries carry 1.0 per distinct term, which makes the dot
product Qdrant computes exactly::

    BM25(q, d) = sum over t in q of  idf(t) * tfnorm(t, d)

No vocabulary or document-frequency file has to be kept in sync with the
collection. (Inherited invariant #14.)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

BM25_K1 = 1.2
BM25_B = 0.75
MIN_TOKEN_LEN = 3

# Accented Latin letters folded to their base form, so "razón" and "razon" hit
# the same term. ñ is deliberately absent: folding it to n would conflate año
# with ano.
_FOLD = str.maketrans(
    "áàâäãåéèêëíìîïóòôöõúùûüçýÿÁÀÂÄÃÅÉÈÊËÍÌÎÏÓÒÔÖÕÚÙÛÜÇÝ",
    "aaaaaaeeeeiiiiooooouuuucyyaaaaaaeeeeiiiiooooouuuucy",
)

# Spanish function words with no retrieval signal. Tokens under MIN_TOKEN_LEN are
# dropped separately, so one- and two-letter words need no entry here.
STOPWORDS = frozenset("""
con por para que los las una uno unos unas del sus esta este esto estos estas
ese esa eso esos esas aquel aquella aquello ser son era eran fue fueron sido
siendo han has hay haber habia habian hace hacer hecho puede pueden podia
podian debe deben tiene tienen tenia tenian tener como cuando donde porque
pero sino aunque mientras segun sobre entre hasta desde sin tras ante bajo
cada todo toda todos todas otro otra otros otras mismo misma mismos mismas
tan tanto tambien solo solamente muy mas menos algo alguno alguna algunos
algunas nada nadie ningun ninguna cual cuales quien quienes cuyo cuya asi
aqui alli ahora luego entonces despues antes siempre nunca aun ademas decir
dice dicen dicho les nos ello ella ellos ellas usted ustedes ver vez veces
modo manera caso pues bien cierto cierta
""".split())

# FNV-1a 32-bit constants. Qdrant sparse indices are uint32; with a few hundred
# documents collisions are irrelevant, and hashing means there is no vocabulary
# to version alongside the collection.
_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619
_UINT32 = 0xFFFFFFFF


def tokenize(text: str) -> list[str]:
    """Lowercase, fold accents, split on non-alphanumerics, drop short tokens
    and stopwords.

    Walks character by character rather than using a regex character class, to
    mirror bm25.go's ``unicode.IsLetter(r) || unicode.IsDigit(r)`` exactly. A
    regex like ``[^0-9a-zñ]+`` would silently drop non-Latin letters that Go
    keeps — the corpus does contain transliterated Greek.
    """
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        tok = "".join(buf)
        buf.clear()
        if len(tok) >= MIN_TOKEN_LEN and tok not in STOPWORDS:
            out.append(tok)

    folded = text.translate(_FOLD).lower()
    for ch in folded:
        if ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    out.extend(scripture_tokens(folded))
    return out


# A scripture reference, matched against the *folded, lowercased* text so it sees
# "gen. 2:15" for "Gén. 2:15". The optional leading digit carries the numbered
# books ("1 sam. 10:24"); the book token must not be a stopword, which is what
# keeps a clock time ("a las 10:30") from minting a reference.
_SCRIPTURE_RE = re.compile(r"\b(?:([123])\s*)?([a-zñ]{2,12})\.?\s*(\d{1,3}):(\d{1,3})")


def scripture_tokens(folded: str) -> list[str]:
    """Compound tokens for the scripture references in already-folded text.

    ``tokenize`` splits on non-alphanumerics and drops tokens under
    ``MIN_TOKEN_LEN``, so "Juan 10:6" reduces to ``juan`` — the chapter and verse
    are gone and every citation of John is the same term. Measured on
    02-PuertasEternas: **42 of its 49 distinct references lost their
    chapter:verse entirely**, so a query for "Juan 10:9" retrieved every John
    reference in the book indifferently.

    This mints one extra token per reference, ``juan10v6``, alphanumeric so the
    character walk would never have split it. The plain ``juan`` is still emitted
    beside it, so a query naming only the book is unaffected. ``query_sparse_vector``
    tokenizes with the same function, so both legs agree with no configuration.
    """
    out: list[str] = []
    for num, book, chapter, verse in _SCRIPTURE_RE.findall(folded):
        if book in STOPWORDS or len(book) < 2:
            continue
        out.append(f"{num}{book}{chapter}v{verse}")
    return out


def term_id(token: str) -> int:
    """FNV-1a 32-bit hash of a token, matching bm25.go's termID exactly."""
    h = _FNV_OFFSET
    for byte in token.encode("utf-8"):
        h = ((h ^ byte) * _FNV_PRIME) & _UINT32
    return h


@dataclass
class SparseVector:
    """Qdrant's wire format for a sparse vector."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def as_payload(self) -> dict[str, list]:
        return {"indices": self.indices, "values": self.values}


def avg_doc_len(docs: list[list[str]]) -> float:
    """Mean token count over all documents, needed by BM25 length normalisation."""
    if not docs:
        return 0.0
    return sum(len(d) for d in docs) / len(docs)


def doc_sparse_vector(tokens: list[str], avgdl: float) -> SparseVector:
    """Document side of BM25: the term-frequency normalisation only.

    Qdrant multiplies in the IDF at query time.
    """
    if not tokens or avgdl <= 0:
        return SparseVector()

    tf = Counter(term_id(t) for t in tokens)
    dl = len(tokens)
    norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)

    v = SparseVector()
    for tid, n in tf.items():
        v.indices.append(tid)
        v.values.append(n * (BM25_K1 + 1) / (n + norm))
    return v


def query_sparse_vector(query: str) -> SparseVector:
    """1.0 on each distinct query term; Qdrant's idf modifier turns the dot
    product into a real BM25 score."""
    v = SparseVector()
    seen: set[int] = set()
    for tok in tokenize(query):
        tid = term_id(tok)
        if tid in seen:
            continue
        seen.add(tid)
        v.indices.append(tid)
        v.values.append(1.0)
    return v
