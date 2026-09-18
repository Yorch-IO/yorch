"""The closing chapter, rendered by a pure function with no model in the path.

That is the whole design, and it is worth stating as a property rather than as
an implementation detail: **the bibliography cannot be fabricated, because
nothing that could fabricate it is involved.** Every library entry is a
`CitedSource` built from an `Evidence` the retrieval returned and a `Citation`
that survived `answer._verify`; every original reference is a string
`reading.references_of` found in the source's own bytes. This module arranges
them. It is `epub.py`'s decision — a pure renderer, stdlib only — applied to the
one part of a generated work that a reader is most entitled to trust.

**Three lists, never merged**, and the first of them is the work this is a
recasting *of*. It was missing until somebody read a finished essay and noticed
it named every source except the one it was made from — the document is not
"consulted", it is the substance, so it is neither a reference the original
carries nor a library work quoted alongside. A reader who cannot tell what a
recasting recasts has been handed an orphan.

The other two stay apart for the reason they always did: a work the source cited
(which this work has never read) and a work this work actually quoted are
different things, and an alphabetical merge would put them under one heading
with nothing saying which is which. That is `synthesis.py`'s split applied to
provenance: *"Merging the two into a single prose answer would put the one text
a reader must treat sceptically in the same paragraph as the one they may rely
on."*

**An empty list renders its heading and a sentence.** An absent section and
"there were none" are different facts, which is `/project-summary`'s `available`
rule in prose: a reader who sees no library heading cannot tell whether the
library was consulted and gave nothing, or was never consulted at all.
"""

from __future__ import annotations

from .genres import BibliographyStyle
from .types import CitedSource

#: The wording, per language. Two languages because that is what this product's
#: bundles hold and what its corpus is in; anything else falls back to English,
#: which is visible in the work rather than silent — a Portuguese book with an
#: English bibliography heading is obviously wrong, where a mistranslated one
#: would not be.
_WORDS: dict[str, dict[str, str]] = {
    "es": {
        "source": "Obra de origen",
        "no_source": "No consta de qué obra procede este texto.",
        "original": "Referencias de la obra original",
        "library": "Obras de esta biblioteca consultadas",
        "no_original": "La obra original no recoge referencias.",
        "no_library": "No se consultó ninguna otra obra de esta biblioteca.",
        "cited_for": "sobre",
    },
    "en": {
        "source": "The work this is a recasting of",
        "no_source": "The work this was made from is not recorded.",
        "original": "References in the original work",
        "library": "Works from this library consulted",
        "no_original": "The original work carries no references.",
        "no_library": "No other work from this library was consulted.",
        "cited_for": "on",
    },
}

#: The chapter's own title, per tone and language. A treatise's reader expects
#: "Bibliografía"; a novel's expects a note.
_CHAPTER: dict[tuple[str, str], str] = {
    ("scholarly", "es"): "Bibliografía",
    ("scholarly", "en"): "Bibliography",
    ("formal", "es"): "Fuentes",
    ("formal", "en"): "Sources",
    ("plain", "es"): "Fuentes",
    ("plain", "en"): "Sources",
    ("note", "es"): "Nota sobre las fuentes",
    ("note", "en"): "A note on sources",
}

UNTITLED = {"es": "(sin título)", "en": "(untitled)"}


def language_of(language: str) -> str:
    """The language whose wording is used, falling back visibly to English."""
    code = (language or "").strip().lower()[:2]
    return code if code in _WORDS else "en"


def chapter_title(style: BibliographyStyle, language: str) -> str:
    lang = language_of(language)
    return _CHAPTER.get((style.tone, lang), _CHAPTER[("scholarly", lang)])


def source_entry(title: str, author: str, language: str) -> str:
    """The original work, named from the catalog and from nothing else.

    Title and author come from `document`, which is what a person edits and what
    `fill_document_metadata` may have corrected — so this is the same standard
    every library entry is held to: a catalog row, never a model's guess. A
    document with neither is named as untitled rather than invented, the rule
    `bookmeta._clean` applies when it refuses to print "desconocido" on a cover.
    """
    lang = language_of(language)
    name = " ".join(str(title or "").split()).strip() or UNTITLED[lang]
    who = " ".join(str(author or "").split()).strip()
    return f"{name} — {who}" if who else name


def render(
    *,
    style: BibliographyStyle,
    language: str,
    references: list[str],
    sources: list[CitedSource],
    source_title: str = "",
    source_author: str = "",
) -> str:
    """The closing chapter, as Markdown, headings included.

    `sources` may contain the same document many times — once per citation — and
    is grouped here rather than by the caller, because how a genre groups its
    sources is part of how the genre is set and the caller has no business
    knowing.
    """
    lang = language_of(language)
    words = _WORDS[lang]
    out: list[str] = [f"## {chapter_title(style, language)}", ""]

    # First, and on its own, because it is not one source among others: it is
    # the one the whole work is made of.
    out.append(f"### {words['source']}")
    out.append("")
    if str(source_title or "").strip() or str(source_author or "").strip():
        out.extend(_entries([source_entry(source_title, source_author, language)],
                            style.numbered))
    else:
        out.append(words["no_source"])
    out.append("")

    out.append(f"### {words['original']}")
    out.append("")
    if references:
        out.extend(_entries(references, style.numbered))
    else:
        out.append(words["no_original"])
    out.append("")

    out.append(f"### {words['library']}")
    out.append("")
    grouped = group(sources)
    if grouped:
        lines: list[str] = []
        for title, locators, claims in grouped:
            entry = title or UNTITLED[lang]
            if locators:
                entry = f"{entry} — {'; '.join(locators)}"
            if style.annotated and claims:
                entry = f"{entry} ({words['cited_for']}: {'; '.join(claims)})"
            lines.append(entry)
        out.extend(_entries(lines, style.numbered))
    else:
        out.append(words["no_library"])
    out.append("")

    return "\n".join(out).rstrip() + "\n"


def group(
    sources: list[CitedSource],
) -> list[tuple[str, list[str], list[str]]]:
    """One row per cited work: its title, its locators, and what it was cited for.

    Grouped on `version_id` and not on `title`, because a title is a display
    string two different versions can share — this corpus has measured books
    whose running header survived into the index as a chapter title — while a
    version id is what the catalog actually distinguishes. First appearance
    order, which is the order the work cited them in and therefore the order a
    reader met them.
    """
    order: list[str] = []
    rows: dict[str, tuple[str, list[str], list[str]]] = {}
    for source in sources:
        key = source.version_id or source.document_id or source.chunk_id
        if key not in rows:
            order.append(key)
            rows[key] = (source.title, [], [])
        _, locators, claims = rows[key]
        if source.locator and source.locator not in locators:
            locators.append(source.locator)
        claim = (source.claim or "").strip()
        if claim and claim not in claims:
            claims.append(claim)
    return [rows[key] for key in order]


def _entries(values: list[str], numbered: bool) -> list[str]:
    if numbered:
        return [f"{i}. {value}" for i, value in enumerate(values, start=1)]
    return [f"- {value}" for value in values]
