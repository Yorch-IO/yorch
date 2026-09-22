"""Extractor registry: file extension -> extractor.

Two shapes come out of here, and the difference is honest rather than
incidental:

* **Text-like** sources (PDF, DOCX, TXT) produce a linear ``bytes`` stream of
  blank-line-separated paragraphs. ``chunk.build_chunks`` then applies the
  learned heading/kind rules, and every chunk's ``char_span`` is a byte-exact
  slice of that stream (invariant #1).
* **Structured** sources (Excel, CSV, PPTX) produce chunks directly. A
  spreadsheet has no linear byte stream to be exact against, so the verifiable
  anchor is ``cell_ref`` ("Ventas 2025!A41:D60") instead of a byte span.

Callers branch on which field is populated.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, replace
from typing import Callable

from ..chunk import Chunk, DocRules


@dataclass
class Evidence:
    """Structural observations for the rule-learning loop.

    This is the raw material the ``propose`` node reasons over. It is
    deliberately made of counts and samples rather than whole pages: the model
    should be shown enough to spot a repeating header, not the entire document.
    """

    source: str = ""
    extractor: str = ""
    pages: int = 0
    # Lines that repeat on many pages — header/footer candidates. text -> count.
    repeated_lines: dict[str, int] = field(default_factory=dict)
    # Sample of first/last lines per page, where headers and folios live.
    first_lines: list[str] = field(default_factory=list)
    last_lines: list[str] = field(default_factory=list)
    # Candidate numbered-heading lines, for calibrating the length guards.
    numbered_lines: list[str] = field(default_factory=list)
    # Paragraphs beginning with a number, for the questions/footnotes rule.
    numbered_paragraphs: list[str] = field(default_factory=list)
    # Short standalone paragraphs carrying no leading number — the only evidence
    # from which an unnumbered heading pattern ("LIBRO PRIMERO") can be proposed.
    # Filled by `run_extract` for every text-like extractor rather than by each
    # one, so no format can quietly omit it.
    short_lines: list[str] = field(default_factory=list)
    page_height: float = 0.0
    median_line_gap: float = 0.0
    # Pages with no text layer, i.e. needing OCR.
    pages_without_text: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParagraphPosition:
    """Where one paragraph of the joined stream sits in the original document.

    **A sidecar, never a tag inside the text.** The obvious implementation —
    the one RAGFlow uses, an ``@@page\tx0\tx1\ttop\tbottom##`` sentinel
    appended to each line — survives arbitrary merge logic for free and is
    impossible here: invariant #1 says ``Chunk.text`` is a byte-exact slice of
    the source, the correction pass would be handed the tags as prose, and
    ``correct.verify`` would refuse the paragraphs that lost them.

    The join key is the **paragraph index**, and it is the one key that
    survives the whole pipeline: ``join_paragraphs`` writes ``\n\n`` between
    paragraphs, ``chunk.split_paragraphs`` reproduces ``0..N-1`` against that
    same separator, correction is keyed by paragraph index and structurally
    cannot merge two, and ``Chunk.para_from``/``para_to`` index exactly that
    sequence. A byte offset would not survive correction; this does.

    ``page`` is where the paragraph *starts*, which is the page a reader would
    be taken to. ``page_to`` differs only for a paragraph that runs across a
    page break, and it is carried rather than hidden because "this quote spans
    two pages" is a fact about the citation.

    The box is the union of the rows on the starting page, in PDF user units
    with **y growing downward** — PyMuPDF's convention, not the PDF-native one,
    the same inversion `_filter_header_footer` documents.
    """

    para: int
    page: int
    page_to: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class Extracted:
    """Result of extraction. Exactly one of ``text`` / ``chunks`` is populated."""

    text: bytes | None = None
    chunks: list[Chunk] | None = None
    evidence: Evidence = field(default_factory=Evidence)
    #: One row per paragraph of ``text``, or empty when the extractor cannot
    #: know — a plain ``.txt`` has no pages and a spreadsheet has no paragraphs.
    #: Empty is "not knowable", never "page 0": a citation that names a page
    #: the reader cannot find is worse than one that names none.
    positions: list[ParagraphPosition] = field(default_factory=list)

    @property
    def is_structured(self) -> bool:
        return self.chunks is not None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.chunks is None):
            raise ValueError("Extracted needs exactly one of text / chunks")


# An extractor takes a path plus the document rules learned so far.
Extractor = Callable[[str, DocRules], Extracted]

_REGISTRY: dict[str, str] = {
    ".pdf": "pdf_text",
    ".txt": "plain",
    ".md": "plain",
    ".docx": "docx_",
    ".pptx": "pptx_",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".csv": "csv_",
}


def extractor_name(path: str) -> str:
    suffix = pathlib.Path(path).suffix.lower()
    name = _REGISTRY.get(suffix)
    if name is None:
        raise ValueError(f"no extractor for {suffix!r}; supported: {sorted(_REGISTRY)}")
    return name


def get_extractor(name: str) -> Extractor:
    """Import lazily so a missing optional dependency only breaks its own format."""
    from importlib import import_module

    module = import_module(f".{name}", __package__)
    return module.extract  # type: ignore[no-any-return]


#: How long a standalone paragraph may be and still be a heading candidate.
#: Comfortably above `ChunkRules.heading_l1_max` (40) so the learner sees the
#: near misses it has to exclude, and far below a prose paragraph.
SHORT_LINE_CHARS = 70
#: Enough for the model to spot a repeating shape without pasting a chapter.
SHORT_LINE_SAMPLE = 40


def extract(
    path: str, rules: DocRules | None = None, name: str | None = None
) -> Extracted:
    extracted = get_extractor(name or extractor_name(path))(path, rules or DocRules())
    if extracted.text is not None and not extracted.evidence.short_lines:
        extracted.evidence.short_lines = short_line_candidates(extracted.text)
    return extracted


def short_line_candidates(text: bytes) -> list[str]:
    """Standalone short paragraphs that carry no leading number.

    These are what an unnumbered heading looks like structurally, and the
    numbered ones are excluded because `HEADING_RE` already covers those — a
    learner shown them proposes a pattern duplicating the built-in detector.

    Deduplicated in first-seen order: "LIBRO PRIMERO" and "LIBRO SEGUNDO" are two
    data points, while the same running header forty times is one.
    """
    from ..chunk import HEADING_RE, split_paragraphs

    seen: dict[str, None] = {}
    for para in split_paragraphs(text):
        line = para.text.strip()
        if not line or len(line) > SHORT_LINE_CHARS or "\n" in line:
            continue
        if HEADING_RE.match(line):
            continue
        seen.setdefault(line, None)
        if len(seen) >= SHORT_LINE_SAMPLE:
            break
    return list(seen)


def kept_paragraphs(
    pairs: "list[tuple[str, ParagraphPosition | None]]",
) -> "tuple[list[str], list[ParagraphPosition]]":
    """Strip, drop the empties, and renumber — **once**, for both lists.

    The hazard this exists for is silent and total: ``extract`` filtered its
    paragraphs twice, here and again inside ``join_paragraphs``, so a list of
    positions filtered by either rule alone would go one out of step at the
    first paragraph the *other* rule dropped — and every citation after it
    would name a confidently wrong page. A wrong page is worse than no page,
    and nothing downstream could tell the two apart.

    So there is one filter, and ``para`` is assigned here from the surviving
    order rather than carried in from before it.
    """
    texts: list[str] = []
    positions: list[ParagraphPosition] = []
    for text, pos in pairs:
        stripped = text.strip()
        if not stripped:
            continue
        if pos is not None:
            positions.append(replace(pos, para=len(texts)))
        texts.append(stripped)
    return texts, positions


def join_paragraphs(paragraphs: list[str]) -> bytes:
    """Assemble a linear byte stream the chunker can slice exactly.

    Paragraphs are separated by exactly one blank line, because
    ``chunk.split_paragraphs`` splits on ``b"\\n\\n"`` and tracks offsets against
    that assumption.
    """
    return "\n\n".join(p.strip() for p in paragraphs if p.strip()).encode("utf-8")
