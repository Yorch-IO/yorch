"""Scripture references, normalised to one spelling so a filter can match them.

The engine's `bm25.scripture_tokens` already mints `juan3v16` beside `juan` for
the *ranking* leg, so a query naming a verse ranks the chunks that cite it
above the ones that only name the book. That is soft. What it cannot do is
**narrow**: "only fragments that cite Romans 8" is a payload filter, and a
filter matches strings exactly — so `Rom. 8:28`, `Romanos 8.28` and
`romanos 8:28` have to become one string before they are written.

This module is that one string: `Romanos 8:28` for a verse and `Romanos 8` for
its chapter, with the book spelled the way the Reina-Valera tradition prints
it. Two lists go into every point's payload — `scripture_refs` and
`scripture_chapters` — because a reader asking about "Romanos 8" means the
chapter, and a filter on verse strings would miss every citation of 8:1.

**Detection is the engine's regex, not a second one.** `_SCRIPTURE_RE` is
applied to folded lowercase text and has been measured against the corpus;
this module only decides what a match *means*. A book token it does not know
is not a reference — `a las 10:30` is a clock, and the regex alone cannot
tell, which is why the table is the gate.

The table is the sixty-six books and the abbreviations a Spanish Bible, a
concordance and a preacher's notes use for them. It is a list of strings, not
a library; a missing abbreviation is a missed citation, never a wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from docagent.bm25 import _FOLD, _SCRIPTURE_RE

#: Canonical book name → the folded, accent-free tokens that name it.
#: Folded because the regex runs on folded text: `génesis` is `genesis`.
_BOOKS: dict[str, tuple[str, ...]] = {
    "Génesis": ("genesis", "gen", "gn"),
    "Éxodo": ("exodo", "ex", "exo"),
    "Levítico": ("levitico", "lev", "lv"),
    "Números": ("numeros", "num", "nm"),
    "Deuteronomio": ("deuteronomio", "deut", "dt", "deu"),
    "Josué": ("josue", "jos"),
    "Jueces": ("jueces", "jue", "jc"),
    "Rut": ("rut", "rt"),
    "1 Samuel": ("1samuel", "1sam", "1sm", "1s"),
    "2 Samuel": ("2samuel", "2sam", "2sm", "2s"),
    "1 Reyes": ("1reyes", "1rey", "1re", "1r"),
    "2 Reyes": ("2reyes", "2rey", "2re", "2r"),
    "1 Crónicas": ("1cronicas", "1cron", "1cro", "1cr"),
    "2 Crónicas": ("2cronicas", "2cron", "2cro", "2cr"),
    "Esdras": ("esdras", "esd"),
    "Nehemías": ("nehemias", "neh", "ne"),
    "Ester": ("ester", "est"),
    "Job": ("job", "jb"),
    "Salmos": ("salmos", "salmo", "sal", "sl", "ps"),
    "Proverbios": ("proverbios", "prov", "pr", "pro"),
    "Eclesiastés": ("eclesiastes", "ecl", "ec", "qo"),
    "Cantares": ("cantares", "cantar", "cant", "cnt", "ct"),
    "Isaías": ("isaias", "isa", "is"),
    "Jeremías": ("jeremias", "jer", "jr"),
    "Lamentaciones": ("lamentaciones", "lam", "lm"),
    "Ezequiel": ("ezequiel", "ez", "eze", "ezq"),
    "Daniel": ("daniel", "dan", "dn"),
    "Oseas": ("oseas", "os"),
    "Joel": ("joel", "jl"),
    "Amós": ("amos", "am"),
    "Abdías": ("abdias", "abd", "ab"),
    "Jonás": ("jonas", "jon"),
    "Miqueas": ("miqueas", "miq", "mi"),
    "Nahúm": ("nahum", "nah", "na"),
    "Habacuc": ("habacuc", "hab", "ha"),
    "Sofonías": ("sofonias", "sof", "so"),
    "Hageo": ("hageo", "hag", "ag"),
    "Zacarías": ("zacarias", "zac", "za"),
    "Malaquías": ("malaquias", "mal", "ml"),
    "Mateo": ("mateo", "mat", "mt"),
    "Marcos": ("marcos", "mar", "mc", "mr"),
    "Lucas": ("lucas", "luc", "lc"),
    "Juan": ("juan", "jn", "jua"),
    "Hechos": ("hechos", "hch", "hech", "hec"),
    "Romanos": ("romanos", "rom", "ro", "rm"),
    "1 Corintios": ("1corintios", "1cor", "1co"),
    "2 Corintios": ("2corintios", "2cor", "2co"),
    "Gálatas": ("galatas", "gal", "ga"),
    "Efesios": ("efesios", "ef", "efe"),
    "Filipenses": ("filipenses", "fil", "flp", "fp"),
    "Colosenses": ("colosenses", "col"),
    "1 Tesalonicenses": ("1tesalonicenses", "1tes", "1ts", "1te"),
    "2 Tesalonicenses": ("2tesalonicenses", "2tes", "2ts", "2te"),
    "1 Timoteo": ("1timoteo", "1tim", "1ti", "1tm"),
    "2 Timoteo": ("2timoteo", "2tim", "2ti", "2tm"),
    "Tito": ("tito", "tit", "tt"),
    "Filemón": ("filemon", "flm", "fmn"),
    "Hebreos": ("hebreos", "heb", "he"),
    "Santiago": ("santiago", "stg", "sant", "st"),
    "1 Pedro": ("1pedro", "1ped", "1pe", "1p"),
    "2 Pedro": ("2pedro", "2ped", "2pe", "2p"),
    "1 Juan": ("1juan", "1jn", "1jua"),
    "2 Juan": ("2juan", "2jn", "2jua"),
    "3 Juan": ("3juan", "3jn", "3jua"),
    "Judas": ("judas", "jud"),
    "Apocalipsis": ("apocalipsis", "apoc", "ap", "apo"),
}

_BY_TOKEN: dict[str, str] = {
    tok: book for book, toks in _BOOKS.items() for tok in toks
}


@dataclass(frozen=True)
class Reference:
    book: str
    chapter: int
    verse: int

    @property
    def ref(self) -> str:
        return f"{self.book} {self.chapter}:{self.verse}"

    @property
    def chapter_ref(self) -> str:
        return f"{self.book} {self.chapter}"


def fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def references(text: str) -> list[Reference]:
    """Every reference in the text the table recognises, in order, deduplicated."""
    out: list[Reference] = []
    seen: set[str] = set()
    for num, book, chapter, verse in _SCRIPTURE_RE.findall(fold(text)):
        name = _BY_TOKEN.get(f"{num}{book}")
        if name is None:
            continue
        ref = Reference(name, int(chapter), int(verse))
        if ref.ref not in seen:
            seen.add(ref.ref)
            out.append(ref)
    return out


def payload_fields(text: str) -> tuple[list[str], list[str]]:
    """What a chunk's point carries: its verse strings and its chapter strings."""
    refs = references(text)
    chapters: list[str] = []
    for r in refs:
        if r.chapter_ref not in chapters:
            chapters.append(r.chapter_ref)
    return [r.ref for r in refs], chapters


_QUERY_RE = re.compile(r"^\s*(?:([123])\s*)?([a-zñ]{2,16})\.?\s*(\d{1,3})(?:[:.](\d{1,3}))?\s*$")


def normalise_query(value: str) -> tuple[str, str] | None:
    """A filter value as the payload spells it, or None for one this table
    cannot vouch for.

    Returns `("scripture_refs", "Juan 3:16")` for a verse and
    `("scripture_chapters", "Juan 3")` for a chapter, which is the payload
    field each one has to match. `Juan 3.16` and `Jn 3:16` are the same
    verse; `Juan` alone is not a reference this filter can narrow on — the
    ranking leg already handles a bare book name.
    """
    m = _QUERY_RE.match(fold(value or ""))
    if not m:
        return None
    num, book, chapter, verse = m.groups()
    name = _BY_TOKEN.get(f"{num or ''}{book}")
    if name is None:
        return None
    if verse:
        return "scripture_refs", Reference(name, int(chapter), int(verse)).ref
    return "scripture_chapters", f"{name} {int(chapter)}"
