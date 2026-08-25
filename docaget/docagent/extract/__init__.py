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
from dataclasses import dataclass, field
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


@dataclass
class Extracted:
    """Result of extraction. Exactly one of ``text`` / ``chunks`` is populated."""

    text: bytes | None = None
    chunks: list[Chunk] | None = None
    evidence: Evidence = field(default_factory=Evidence)

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


def join_paragraphs(paragraphs: list[str]) -> bytes:
    """Assemble a linear byte stream the chunker can slice exactly.

    Paragraphs are separated by exactly one blank line, because
    ``chunk.split_paragraphs`` splits on ``b"\\n\\n"`` and tracks offsets against
    that assumption.
    """
    return "\n\n".join(p.strip() for p in paragraphs if p.strip()).encode("utf-8")
