"""Reading a `chunks.jsonl` into everything the free stage can know.

Pure. No store, no provider, no Temporal — the `radial.ts` / `force.ts` /
`auditversion.py` decision applied again: what is worth asserting here is the
grouping and the sweep, and a test that needed three containers standing up to
check a set of spans would run rarely enough to be worth nothing.

**`Passage` is a whitelist, and that is the point of it.** A chunk row carries
`overlap` — the previous chunk's tail, non-empty on about four rows in five —
and `embed_text`, which is the breadcrumb and the overlap concatenated ahead of
the text. Neither may ever reach the page: a renderer that emitted `overlap`
would duplicate a paragraph on nearly every page, which reads as a corrupt book
rather than as a bug. The EPUB writer guards that with a test; this module
guards it by never putting the fields in a structure the rest of the package can
read. The test is still written, because a whitelist is a guarantee only as long
as nobody widens it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import SourceChapter, SourceSpan

#: How much of the opening the genre analysis reads. Bounded because it crosses
#: a Temporal payload, and generous because the question it answers — what kind
#: of document is this — is answered from the front matter and the first
#: chapters or not at all.
GENRE_SAMPLE_CHARS = 8_000

#: A ceiling on the references carried out of the source. A bibliography of a
#: thousand entries is a real thing; carrying all of it through a payload is
#: not, and the closing chapter reads better for a limit than for completeness
#: nobody checks.
MAX_REFERENCES = 400

#: Longest single reference kept. A "reference" longer than this is a paragraph
#: the sweep mistook for one.
MAX_REFERENCE_CHARS = 600

#: Chunk kinds that are notes, and therefore references by construction.
FOOTNOTE_KIND = "nota"

#: Headings under which a document keeps its own references, in the two
#: languages this product's corpus is in plus the ones a mixed corpus is most
#: likely to carry. A *heading* match, never a body match: a paragraph that
#: mentions the word "bibliography" is not a bibliography.
_BIBLIOGRAPHY_HEADINGS = re.compile(
    r"\b("
    r"bibliograf(?:y|ía|ia|ie)|"
    r"referencias?|references?|"
    r"obras\s+citadas|works\s+cited|"
    r"fuentes|sources|"
    r"bibliografia"
    r")\b",
    re.IGNORECASE,
)

_URL = re.compile(r"https?://[^\s<>\")\]]+", re.IGNORECASE)

#: Sentence punctuation a URL at the end of a sentence collects. Stripped,
#: because a link with a trailing full stop is a link that does not resolve, and
#: the bibliography is the one place in a generated work whose entries are meant
#: to be followed. A real trailing `)` is possible and rare; a real trailing `.`
#: is rarer still than the sentence that ends with one.
_URL_TAIL = ".,;:!?)]}\'\""


@dataclass(frozen=True)
class Passage:
    """One source chunk, projected onto the fields this feature may read.

    Nine fields, and the two that are missing are the whole reason the type
    exists. See the module docstring.
    """

    index: int
    kind: str
    chapter: str
    section: str
    text: str
    context: str
    char_from: int
    char_to: int
    cell_ref: str

    @classmethod
    def from_row(cls, row: dict) -> "Passage":
        return cls(
            index=int(row.get("index", 0)),
            kind=str(row.get("kind") or "cuerpo"),
            chapter=str(row.get("chapter") or ""),
            section=str(row.get("section") or ""),
            text=str(row.get("text") or ""),
            context=str(row.get("context") or ""),
            char_from=int(row.get("char_from", 0)),
            char_to=int(row.get("char_to", 0)),
            cell_ref=str(row.get("cell_ref") or ""),
        )


def passages_of(rows: list[dict]) -> list[Passage]:
    """Every row, projected and in the order the chunker produced them."""
    return [Passage.from_row(row) for row in rows]


def chapters_of(passages: list[Passage]) -> list[SourceChapter]:
    """The source's own chapters, as consecutive runs of one `chapter` value.

    Consecutive rather than grouped by title, deliberately. Two chapters may
    share a title — a running header that survived into the index does exactly
    that, and this corpus has measured books where it did — and merging them
    would produce a single span covering material from opposite ends of the
    document, which every ordering rule downstream would then be unable to
    satisfy.

    A document whose chapters were never detected yields one untitled chapter,
    because that is exactly what the index holds and what every breadcrumb on it
    already says. The EPUB export made that visible for the first time; this
    makes it plannable.
    """
    out: list[SourceChapter] = []
    for passage in passages:
        title = passage.chapter.strip()
        if out and out[-1].title == title and out[-1].last == passage.index - 1:
            out[-1].last = passage.index
            out[-1].chars += len(passage.text)
            continue
        out.append(
            SourceChapter(
                title=title,
                first=passage.index,
                last=passage.index,
                chars=len(passage.text),
            )
        )
    return out


def excerpt(passages: list[Passage], limit: int = GENRE_SAMPLE_CHARS) -> str:
    """The opening, with headings folded back in.

    Same reasoning `bookexport.excerpt` states and the same shape, reimplemented
    over `Passage` rather than reused over raw rows precisely so that this
    package never holds a raw row: *"a chunk row does not contain them — the
    chunker consumes a heading paragraph — and the title of a book is very often
    exactly the heading that was consumed."* Reading only `text` would hand the
    analysis the one part of the front matter that had the title removed.
    """
    out: list[str] = []
    size = 0
    chapter = ""
    section = ""
    for passage in passages:
        for value, seen, mark in (
            (passage.chapter, chapter, "#"),
            (passage.section, section, "##"),
        ):
            value = value.strip()
            if value and value != seen:
                line = f"{mark} {value}"
                out.append(line)
                size += len(line) + 1
                if mark == "#":
                    chapter = value
                else:
                    section = value
        out.append(passage.text)
        size += len(passage.text) + 1
        if size >= limit:
            break
    return "\n\n".join(out)[:limit]


def text_for(passages: list[Passage], spans: list[SourceSpan]) -> str:
    """The source material one chapter of the target work is made from.

    Headings folded in as above, so a chapter composed from the middle of a book
    still knows what part of it that was. Spans are read in the order given,
    which in Faithful mode is the source's own and in Adaptive is whatever the
    plan chose.
    """
    wanted = _index_set(spans)
    out: list[str] = []
    chapter = ""
    section = ""
    for passage in passages:
        if passage.index not in wanted:
            continue
        for value, seen, mark in (
            (passage.chapter, chapter, "#"),
            (passage.section, section, "##"),
        ):
            value = value.strip()
            if value and value != seen:
                out.append(f"{mark} {value}")
                if mark == "#":
                    chapter = value
                else:
                    section = value
        out.append(passage.text)
    return "\n\n".join(out)


def chars_for(passages: list[Passage], spans: list[SourceSpan]) -> int:
    wanted = _index_set(spans)
    return sum(len(p.text) for p in passages if p.index in wanted)


def _index_set(spans: list[SourceSpan]) -> set[int]:
    out: set[int] = set()
    for span in spans:
        first = int(span.first)
        last = int(span.last)
        if last < first:
            first, last = last, first
        out.update(range(first, last + 1))
    return out


def references_of(passages: list[Passage]) -> list[str]:
    """The source document's own references, carried verbatim.

    Three sweeps, in descending order of how sure each is:

    * every chunk whose kind is `nota` — a footnote *is* a reference, and the
      chunker already decided that;
    * every chunk under a heading that names a reference section — a heading
      match, never a body match, because a paragraph mentioning the word
      "bibliography" is not one;
    * every URL anywhere, because a link is a reference whatever surrounds it.

    Nothing is parsed into fields. A reference that could not be parsed and is
    carried as its literal string is honest; one parsed into a wrong author is
    not — the same rule `bookmeta._clean` applies when it refuses to print
    "desconocido" on a cover. Whatever the sweep collects reaches the closing
    chapter as it was found, and the sweep is what the run's report reports.
    """
    found: list[str] = []
    seen: set[str] = set()

    def keep(value: str) -> None:
        value = " ".join(value.split()).strip()
        if not value or len(value) > MAX_REFERENCE_CHARS:
            return
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        found.append(value)

    for passage in passages:
        if len(found) >= MAX_REFERENCES:
            break
        heading = f"{passage.chapter} {passage.section}"
        if passage.kind == FOOTNOTE_KIND or _BIBLIOGRAPHY_HEADINGS.search(heading):
            for line in passage.text.splitlines():
                keep(line)
            continue
        for url in _URL.findall(passage.text):
            keep(url.rstrip(_URL_TAIL))

    return found[:MAX_REFERENCES]
