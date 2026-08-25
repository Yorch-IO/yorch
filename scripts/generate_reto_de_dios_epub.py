#!/usr/bin/env python3
"""Build an EPUB 3 from the exported Notion Markdown pages.

The generated book intentionally omits every Notion URL. It uses only the
exported page metadata to establish the chapter and page order.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import uuid
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Iterable


BOOK_TITLE = "Reto de Dios"
BOOK_AUTHOR = "Dario Silva-Silva"
DEFAULT_SOURCE = Path("exports/notion/el-reto-de-dios")
DEFAULT_OUTPUT = Path("exports/epub/Reto-de-Dios.epub")
NOTION_URL = r"https?://(?:[a-z0-9-]+\.)?notion\.(?:so|com)/[^\s)\]>\"']+"


def parse_markdown(path: Path) -> tuple[dict[str, object], str]:
    """Return the small JSON-valued front matter and its Markdown body."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text

    _, front_matter, body = text.split("---\n", 2)
    metadata: dict[str, object] = {}
    for line in front_matter.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        try:
            metadata[key] = json.loads(value)
        except json.JSONDecodeError:
            metadata[key] = value
    return metadata, body


def remove_notion_links(markdown: str) -> str:
    """Keep link labels but remove their target when it is a Notion URL."""
    markdown = re.sub(
        rf"\[([^\]]+)\]\(\s*{NOTION_URL}\s*\)",
        r"\1",
        markdown,
        flags=re.IGNORECASE,
    )
    markdown = re.sub(
        rf"<({NOTION_URL})>",
        "",
        markdown,
        flags=re.IGNORECASE,
    )
    markdown = re.sub(NOTION_URL, "", markdown, flags=re.IGNORECASE)
    return markdown


def extract_exported_content(markdown: str) -> str:
    """Discard Notion's page wrapper, retaining only the original page body.

    The Notion fetch export represents a page as XML-like markup such as
    ``<page> ... <properties> ... <content>body</content> ... </page>``.
    That wrapper is export metadata, not book text, and must not be rendered.
    """
    content_match = re.search(r"<content\b[^>]*>(.*?)</content\s*>", markdown, flags=re.IGNORECASE | re.DOTALL)
    if content_match:
        markdown = content_match.group(1)

    # Preserve the text flow while removing any remaining HTML-style markup.
    markdown = re.sub(r"<br\s*/?>", "\n", markdown, flags=re.IGNORECASE)
    markdown = re.sub(r"</?(?:p|div|section|article|blockquote)\b[^>]*>", "\n", markdown, flags=re.IGNORECASE)
    markdown = re.sub(r"</?(?:li|tr)\b[^>]*>", "\n", markdown, flags=re.IGNORECASE)
    markdown = re.sub(r"<[^>]+>", "", markdown)
    return html.unescape(markdown)


def render_inline(text: str) -> str:
    """Render a deliberately small, dependable subset of Markdown to XHTML."""
    escaped = html.escape(text, quote=False)

    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), html.unescape(match.group(2))
        if re.fullmatch(NOTION_URL, url, flags=re.IGNORECASE):
            return label
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
    return escaped


def markdown_to_xhtml(markdown: str) -> str:
    """Convert common exported Markdown blocks without needing third-party packages."""
    markdown = extract_exported_content(remove_notion_links(markdown)).replace("\r\n", "\n")
    output: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append(f"<p>{render_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_kind, list_items
        if list_kind:
            output.append(f"<{list_kind}>" + "".join(f"<li>{render_inline(item)}</li>" for item in list_items) + f"</{list_kind}>")
        list_kind, list_items = None, []

    for line in markdown.splitlines():
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code:
                output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue
        unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            flush_paragraph()
            wanted_kind = "ul" if unordered else "ol"
            if list_kind and list_kind != wanted_kind:
                flush_list()
            list_kind = wanted_kind
            list_items.append((unordered or ordered).group(1))
            continue
        if line.startswith("> "):
            flush_paragraph()
            flush_list()
            output.append(f"<blockquote><p>{render_inline(line[2:])}</p></blockquote>")
            continue
        if re.fullmatch(r"\s{0,3}([-*_])(?:\s*\1){2,}\s*", line):
            flush_paragraph()
            flush_list()
            output.append("<hr />")
            continue
        flush_list()
        paragraph.append(line.strip())

    if in_code:
        output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(output) or "<p></p>"


def page_number(metadata: dict[str, object], path: Path) -> int:
    value = metadata.get("page")
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        match = re.match(r"(\d+)", path.name)
        return int(match.group(1)) if match else 0


def xhtml_document(title: str, body: str, stylesheet: str = "../styles.css") -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="es" lang="es">
<head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="{stylesheet}" /></head>
<body>{body}</body>
</html>
'''


def cover_xhtml() -> str:
    return xhtml_document(
        BOOK_TITLE,
        f'<section class="cover"><p class="cover-kicker">Libro</p><h1>{BOOK_TITLE}</h1><p class="cover-author">{BOOK_AUTHOR}</p></section>',
        "styles.css",
    )


def stylesheet() -> str:
    return '''body { font-family: serif; line-height: 1.45; margin: 6%; }
h1, h2, h3 { line-height: 1.2; margin-top: 1.5em; }
p { margin: 0 0 0.9em; }
blockquote { border-left: 0.2em solid #777; margin: 1em; padding-left: 1em; }
pre { white-space: pre-wrap; }
section.subsection + section.subsection { margin-top: 2em; }
.cover { align-items: center; display: flex; flex-direction: column; justify-content: center; min-height: 90vh; text-align: center; }
.cover h1 { font-size: 2.4em; margin: 0.2em; }
.cover-kicker { letter-spacing: 0.16em; text-transform: uppercase; }
.cover-author { font-size: 1.2em; margin-top: 2em; }
'''


def xml_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def comparable_title(value: object) -> str:
    """Normalize titles so ``1. Chapter`` and ``Chapter`` compare equally."""
    text = str(value).casefold()
    text = re.sub(r"^\s*\d+\.\s*", "", text)
    return re.sub(r"[^\w]+", "", text)


def write_epub(source: Path, output: Path) -> tuple[int, int]:
    documents: OrderedDict[str, list[tuple[dict[str, object], Path]]] = OrderedDict()
    for path in sorted(source.rglob("*.md")):
        if path.name == "INDICE.md":
            continue
        metadata, _ = parse_markdown(path)
        chapter = str(metadata.get("chapter") or "Sin capítulo")
        documents.setdefault(chapter, []).append((metadata, path))

    for pages in documents.values():
        pages.sort(key=lambda item: (page_number(item[0], item[1]), item[1].name))

    identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, str(source.resolve()))}"
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="styles" href="styles.css" media-type="text/css"/>',
    ]
    spine = ['<itemref idref="cover"/>']
    nav_sections: list[str] = []
    files: dict[str, str] = {
        "OEBPS/cover.xhtml": cover_xhtml(),
        "OEBPS/styles.css": stylesheet(),
    }

    chapter_index = 1
    source_document_count = 0
    for chapter, pages in documents.items():
        sections: list[str] = [f"<h1>{html.escape(chapter)}</h1>"]
        for metadata, path in pages:
            _, markdown = parse_markdown(path)
            title = str(metadata.get("title") or path.stem)
            section_heading = "" if comparable_title(title) == comparable_title(chapter) else f"<h2>{html.escape(title)}</h2>\n"
            sections.append(f'<section class="subsection">{section_heading}{markdown_to_xhtml(markdown)}</section>')
            source_document_count += 1

        filename = f"chapters/{chapter_index:04d}.xhtml"
        item_id = f"chapter-{chapter_index:04d}"
        files[f"OEBPS/{filename}"] = xhtml_document(chapter, "\n".join(sections))
        manifest.append(f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{item_id}"/>')
        nav_sections.append(f'<li><a href="{filename}">{html.escape(chapter)}</a></li>')
        chapter_index += 1

    files["OEBPS/nav.xhtml"] = xhtml_document(
        "Índice",
        f'<nav epub:type="toc" id="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Índice</h1><ol>{"".join(nav_sections)}</ol></nav>',
        "styles.css",
    )
    files["META-INF/container.xml"] = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
'''
    files["OEBPS/content.opf"] = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="es">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{xml_escape(identifier)}</dc:identifier>
    <dc:title>{xml_escape(BOOK_TITLE)}</dc:title>
    <dc:creator>{xml_escape(BOOK_AUTHOR)}</dc:creator>
    <dc:language>es</dc:language>
    <meta property="dcterms:modified">2026-08-24T00:00:00Z</meta>
  </metadata>
  <manifest>{''.join(manifest)}</manifest>
  <spine>{''.join(spine)}</spine>
</package>
'''

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, contents in files.items():
            epub.writestr(name, contents, compress_type=zipfile.ZIP_DEFLATED)
    return chapter_index - 1, source_document_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Reto de Dios EPUB from its Notion export.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Directory containing exported Markdown pages.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path of the EPUB to create.")
    args = parser.parse_args()
    if not args.source.is_dir():
        raise SystemExit(f"Source directory does not exist: {args.source}")
    chapter_count, source_count = write_epub(args.source, args.output)
    print(f"Created {args.output} with {chapter_count} chapters from {source_count} source documents.")


if __name__ == "__main__":
    main()
