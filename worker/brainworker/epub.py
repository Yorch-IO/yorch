"""An EPUB 3 archive, built from what a run already produced.

Pure and store-free, for the reason :mod:`brainworker.auditversion` and
``app/src/lib/radial.ts`` are: what is worth asserting here is the *rendering*
— which heading opens a chapter, what text reaches a page, what never does —
and a test that needed a workspace and a catalog standing up to check that
would run rarely enough to be worth nothing. Nothing in this module reads a
file, writes one, or knows what a tenant is.

**Stdlib only, deliberately.** ``worker/pyproject.toml`` keeps a "no compiler in
the image" property, so ``ebooklib`` and ``lxml`` are both out; the container,
OPF and navigation shapes are ported from ``scripts/generate_reto_de_dios_epub.py``,
which has built a real book from a Notion export since 2026-08-24. Its Markdown
parser is *not* ported — the input here is already structured.

**The input is the run's own chunks, not its text stream.** Three reasons, and
only the third is obvious. A chunk row carries ``chapter`` and ``section``,
which is the outline the chunker detected and the only place it survives —
:func:`docagent.chunk.build_chunks` *consumes* a heading paragraph rather than
emitting it, so re-walking ``corrected.txt`` would mean re-running heading
detection with rules this module would have to be handed. A DOCX, PPTX or XLSX
has no text stream at all, only ``structured_chunks``. And a video has neither,
but has cues. One reader covers all three.

What that costs is honesty about what the index holds: a document whose chapters
were never detected becomes one untitled chapter here, because that is exactly
what was indexed and what every breadcrumb on it says. The EPUB is the first
surface on which a reader can *see* that, which is a side benefit worth naming
rather than papering over.
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Iterable, Sequence

#: Namespace for the book's ``dc:identifier``. Derived from the version id, so
#: regenerating the same version twice produces the same book rather than two
#: books a reader's library would hold side by side.
_ID_NAMESPACE = uuid.NAMESPACE_URL
_ID_PREFIX = "urn:brain:version:"

#: What a chunk's ``kind`` becomes in the markup. Spanish on the wire like the
#: kinds themselves — they are stored in Qdrant payloads and renaming them
#: breaks every existing collection, so the class names follow rather than
#: translate. A kind this map has not been taught about styles as prose, which
#: is the safe default: it is what a body paragraph looks like.
_BLOCK_CLASS: dict[str, str] = {
    "cuerpo": "cuerpo",
    "preguntas": "preguntas",
    "nota": "nota",
    "tabla_fila": "tabla",
    "tabla_resumen": "tabla",
    "diapositiva": "diapositiva",
    "transcripcion": "transcripcion",
}

#: What a chapter is called when the document has no detected outline at all.
UNTITLED_CHAPTER = "Documento"


@dataclass(frozen=True)
class Block:
    """One chunk's worth of readable text, with the heading that opens it.

    ``headings`` is the *new* part of this chunk's breadcrumb — the chapter
    title when the chapter changed, then each section segment that changed —
    paired with its level. Empty for a chunk that continues the section before
    it, which is most of them.

    ``marker`` is the timestamp a transcript carries and a document does not.
    It is rendered beside the text rather than as a heading: an hour of speech
    cut into 69 fragments would otherwise produce 69 entries in the table of
    contents, which is a table nobody reads.
    """

    text: str
    kind: str = "cuerpo"
    headings: tuple[tuple[int, str], ...] = ()
    marker: str = ""


@dataclass(frozen=True)
class Chapter:
    title: str
    blocks: tuple[Block, ...] = ()


@dataclass
class Book:
    title: str
    author: str | None
    language: str
    identifier: str
    chapters: list[Chapter] = field(default_factory=list)


# ---------------------------------------------------------------------------
# From chunk rows to chapters
# ---------------------------------------------------------------------------


def identifier_for(version_id: str) -> str:
    """A stable ``dc:identifier`` for a version.

    ``uuid5`` rather than ``uuid4``: a reader's library deduplicates on this, so
    regenerating a book must not produce a second copy of it.
    """
    return f"urn:uuid:{uuid.uuid5(_ID_NAMESPACE, _ID_PREFIX + version_id)}"


def timestamp(seconds: float) -> str:
    """``h:mm:ss`` for an hour-long talk, ``mm:ss`` below that.

    The same shape a video player shows, so a reader can find the moment. Not
    ``timedelta``'s own formatting, which spells an hour as ``1:00:00`` and
    fractions as six decimal places.
    """
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _segments(section: str) -> list[str]:
    """A section path back into its parts.

    ``" > "`` is what :func:`docagent.chunk.build_chunks` joins with, and the
    separator is a property of that join rather than of any one document.
    """
    return [s for s in (part.strip() for part in section.split(" > ")) if s]


def chapters_from_rows(
    rows: Iterable[dict[str, Any]], *, timed: bool = False
) -> list[Chapter]:
    """Group chunk rows into chapters, emitting each heading exactly once.

    A heading is emitted when it *changes*, which is how an outline consumed at
    chunking time is reconstructed without storing it anywhere. The first row of
    a document with no chapter at all opens :data:`UNTITLED_CHAPTER`, because a
    book with zero chapters has no spine and no reader will open it.

    **``overlap``, ``context`` and ``embed_text`` are never read here.**
    ``overlap`` is the previous chunk's tail, carried so retrieval can show a
    fragment in context, and it is non-empty on about four rows in five — a
    renderer that emitted it would duplicate a paragraph on nearly every page.
    ``embed_text`` is the breadcrumb and the overlap concatenated ahead of the
    text, which is worse.
    """
    chapters: list[Chapter] = []
    blocks: list[Block] = []
    title = ""
    opened = False
    chapter_seen = ""
    section_seen: list[str] = []

    def close() -> None:
        nonlocal blocks
        if opened:
            chapters.append(Chapter(title=title or UNTITLED_CHAPTER, blocks=tuple(blocks)))
        blocks = []

    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        chapter = str(row.get("chapter") or "").strip()
        sections = [] if timed else _segments(str(row.get("section") or ""))

        headings: list[tuple[int, str]] = []
        if chapter != chapter_seen or not opened:
            close()
            title, chapter_seen, opened = chapter, chapter, True
            # The chapter's own title is the `<h1>` of its file and is written
            # by `_chapter_xhtml`, so it is not a heading *inside* the flow.
            section_seen = []
        for depth, name in enumerate(sections):
            if depth >= len(section_seen) or section_seen[depth] != name:
                # Level 2 is the first heading below the chapter, and a deeper
                # section nests from there. Capped at 6: XHTML has no `<h7>`.
                headings.append((min(depth + 2, 6), name))
        if sections != section_seen:
            section_seen = list(sections)

        marker = ""
        if timed and row.get("start_s") is not None:
            marker = timestamp(float(row["start_s"]))

        blocks.append(
            Block(
                text=text,
                kind=str(row.get("kind") or "cuerpo"),
                headings=tuple(headings),
                marker=marker,
            )
        )

    close()
    return chapters


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _paragraphs(text: str) -> list[str]:
    """A chunk's text into paragraphs.

    Split on a blank line, which is what ``join_paragraphs`` writes and what
    ``split_paragraphs`` reads. A single newline *inside* a paragraph is
    legitimate — verse, numbered lists — and is kept as a ``<br/>`` rather than
    silently joined, because a psalm reflowed into prose is a different text.
    """
    out: list[str] = []
    for part in re.split(r"\n\s*\n", text):
        part = part.strip()
        if part:
            out.append("<br/>".join(_escape(line) for line in part.split("\n")))
    return out


def _block_xhtml(block: Block) -> str:
    out: list[str] = []
    for level, name in block.headings:
        out.append(f"<h{level}>{_escape(name)}</h{level}>")
    css = _BLOCK_CLASS.get(block.kind, "cuerpo")
    marker = (
        f'<span class="marca">{_escape(block.marker)}</span> ' if block.marker else ""
    )
    paragraphs = _paragraphs(block.text) or [""]
    for i, paragraph in enumerate(paragraphs):
        lead = marker if i == 0 else ""
        out.append(f'<p class="{css}">{lead}{paragraph}</p>')
    return "\n".join(out)


def _document(title: str, body: str, stylesheet: str, language: str) -> str:
    lang = _attr(language)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">\n'
        f"<head><title>{_escape(title)}</title>"
        f'<link rel="stylesheet" type="text/css" href="{stylesheet}" /></head>\n'
        f"<body>{body}</body>\n"
        "</html>\n"
    )


def _chapter_xhtml(chapter: Chapter, language: str) -> str:
    body = [f"<h1>{_escape(chapter.title)}</h1>"]
    body.extend(_block_xhtml(b) for b in chapter.blocks)
    return _document(chapter.title, "\n".join(body), "../styles.css", language)


def _cover_xhtml(book: Book) -> str:
    author = (
        f'<p class="cover-author">{_escape(book.author)}</p>' if book.author else ""
    )
    body = (
        f'<section class="cover"><h1>{_escape(book.title)}</h1>{author}</section>'
    )
    return _document(book.title, body, "styles.css", book.language)


def _nav_xhtml(book: Book, hrefs: Sequence[tuple[str, str]]) -> str:
    items = "".join(
        f'<li><a href="{_attr(href)}">{_escape(title)}</a></li>' for href, title in hrefs
    )
    body = (
        '<nav epub:type="toc" id="toc"><h1>Índice</h1>'
        f"<ol>{items}</ol></nav>"
    )
    return _document("Índice", body, "styles.css", book.language)


STYLESHEET = """body { font-family: serif; line-height: 1.45; margin: 6%; }
h1, h2, h3, h4, h5, h6 { line-height: 1.2; margin-top: 1.5em; }
p { margin: 0 0 0.9em; text-align: justify; }
p.nota { font-size: 0.9em; }
p.preguntas { font-style: italic; }
p.tabla { font-family: sans-serif; font-size: 0.9em; }
p.transcripcion { text-align: left; }
span.marca { color: #555; font-family: sans-serif; font-size: 0.85em; }
.cover { text-align: center; margin-top: 30%; }
.cover h1 { font-size: 2.2em; margin: 0.2em; }
.cover-author { font-size: 1.15em; margin-top: 2em; }
"""


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def _opf(book: Book, manifest: Sequence[str], spine: Sequence[str], modified: str) -> str:
    creator = (
        f"    <dc:creator>{_attr(book.author)}</dc:creator>\n" if book.author else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="book-id" xml:lang="{_attr(book.language)}">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:identifier id=\"book-id\">{_attr(book.identifier)}</dc:identifier>\n"
        f"    <dc:title>{_attr(book.title)}</dc:title>\n"
        f"{creator}"
        f"    <dc:language>{_attr(book.language)}</dc:language>\n"
        f'    <meta property="dcterms:modified">{_attr(modified)}</meta>\n'
        "  </metadata>\n"
        f"  <manifest>{''.join(manifest)}</manifest>\n"
        f"  <spine>{''.join(spine)}</spine>\n"
        "</package>\n"
    )


CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" \
media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

#: A fixed timestamp, so the same book built twice is the same bytes.
#:
#: ``dcterms:modified`` is required by EPUB 3 and reading the clock would make
#: the archive's sha256 change on every build — which the catalog records and
#: `ArtifactStore.read_bytes` verifies, so a "regenerate" that changed nothing
#: would still look like a different book. The date a book was made is not a
#: fact this product has ever tracked; the version it was made from is, and that
#: is in the identifier.
EPOCH = "2026-01-01T00:00:00Z"


def build(book: Book, *, modified: str = EPOCH) -> bytes:
    """The whole archive, in memory.

    In memory rather than streamed to a path because the caller is an activity
    that hands bytes to :meth:`ArtifactStore.write_bytes`, which writes to a
    ``.partial`` and renames — the one place in this codebase that decides how a
    file appears on disk atomically.

    ``mimetype`` is written **first and uncompressed**, which is not decoration:
    it is how a reader identifies the container without unzipping it, and it is
    the check every validator makes before any other.
    """
    files: list[tuple[str, str]] = []
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="styles" href="styles.css" media-type="text/css"/>',
    ]
    spine = ['<itemref idref="cover"/>']
    nav: list[tuple[str, str]] = []

    chapters = book.chapters or [Chapter(title=book.title or UNTITLED_CHAPTER)]
    for n, chapter in enumerate(chapters, start=1):
        href = f"chapters/{n:04d}.xhtml"
        item_id = f"chapter-{n:04d}"
        files.append((f"OEBPS/{href}", _chapter_xhtml(chapter, book.language)))
        manifest.append(
            f'<item id="{item_id}" href="{_attr(href)}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{item_id}"/>')
        nav.append((href, chapter.title))

    files.append(("OEBPS/cover.xhtml", _cover_xhtml(book)))
    files.append(("OEBPS/nav.xhtml", _nav_xhtml(book, nav)))
    files.append(("OEBPS/styles.css", STYLESHEET))
    files.append(("OEBPS/content.opf", _opf(book, manifest, spine, modified)))
    files.append(("META-INF/container.xml", CONTAINER))

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, contents in files:
            archive.writestr(name, contents, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()
