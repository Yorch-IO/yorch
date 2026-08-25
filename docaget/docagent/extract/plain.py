"""Plain text and Markdown.

The simplest extractor, and the one the port-fidelity test uses: it must hand the
chunker the file's bytes essentially untouched, so that indexing an already-clean
``.txt`` reproduces the Go pipeline exactly.
"""

from __future__ import annotations

import re

from ..chunk import ChunkRules, DocRules, heading_level, split_paragraphs
from . import Evidence, Extracted

_NUMBERED = re.compile(r"^\d+[.)]?\s")


def extract(path: str, rules: DocRules) -> Extracted:
    with open(path, "rb") as f:
        data = f.read()

    # Normalise line endings but do not otherwise touch the bytes: the chunker
    # slices this stream and any rewrite would invalidate char_span.
    if b"\r\n" in data:
        data = data.replace(b"\r\n", b"\n")

    paras = split_paragraphs(data)
    chunk_rules = ChunkRules()
    ev = Evidence(
        source=path,
        extractor="plain",
        pages=1,
        numbered_lines=[
            p.text for p in paras if heading_level(p.text, chunk_rules) > 0
        ][:40],
        numbered_paragraphs=[
            p.text[:200] for p in paras if _NUMBERED.match(p.text)
        ][:40],
    )
    return Extracted(text=data, evidence=ev)
