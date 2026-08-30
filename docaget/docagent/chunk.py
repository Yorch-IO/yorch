"""Document chunking: heading hierarchy, content kinds, and sentence-aligned
windows.

Port of ``sociologia/index/chunk.go``. Every rule here was arrived at by
measuring the Go pipeline's output on a real book, and several are load-bearing
in ways that are not obvious — see ``classify_kind`` and ``ChunkRules`` below.

Inherited invariants enforced here:

1. ``Chunk.text`` is a byte-exact slice of the source between ``char_from`` and
   ``char_to``, so a payload can be verified against the original file. Offsets
   are **byte** offsets, which is why this module works on ``bytes`` internally.
2. ``max_embed_chars > hard_cap_chars``, so chunks near the cap still get their
   overlap. Getting this wrong cost 63 of 323 chunks their context.
3. A chunk never spans a heading and never mixes ``kind``.
11. Review questions reset the section path; footnotes do not.
12. Questions vs footnotes is decided by **the dot after the leading number**.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# --- content kinds -----------------------------------------------------------

KIND_BODY = "cuerpo"
KIND_QUESTIONS = "preguntas"
KIND_FOOTNOTE = "nota"
KIND_TABLE_ROW = "tabla_fila"
KIND_TABLE_SUMMARY = "tabla_resumen"
KIND_SLIDE = "diapositiva"


@dataclass(frozen=True)
class ChunkRules:
    """Chunking parameters. Learned per document family and stored in a profile,
    which is why they are data rather than module constants."""

    target_chars: int = 1200
    hard_cap_chars: int = 2000
    overlap_chars: int = 150
    min_chunk_chars: int = 40
    # Caps breadcrumb + context + overlap + text, i.e. what actually goes to the
    # API. Deliberately larger than hard_cap_chars (invariant #2). ~780 tokens of
    # Spanish prose, well under gemini-embedding-001's 2048-token input limit.
    max_embed_chars: int = 2600
    # A paragraph averaging one ¿ per this many chars or denser is a question
    # block, not prose. See classify_kind.
    question_chars_per_mark: int = 300
    #: How many ¿ a paragraph must carry before the density rule may fire at all.
    #:
    #: The density rule exists for the one question block that does not begin
    #: with a number ("Freud\n27. ¿Hasta dónde…"), and the reference book put
    #: 17 marks in 2054 chars against prose at one per 800-4300. That gap is why
    #: one mark was enough there — its paragraphs are long. On a rhetorical
    #: essay split at the line pitch, a 280-char paragraph with a single
    #: rhetorical ¿ clears 300 chars/mark on its own: 62 paragraphs of prose
    #: were tagged `preguntas` on 01_RetoDeDios_INT-S.pdf. A block of review
    #: questions is recognisable by carrying *several* marks, so corroboration
    #: costs nothing on the documents the threshold was measured against.
    question_min_marks: int = 2
    # Heading guards, copied from html/main.go's headingLevel.
    heading_l1_max: int = 40
    heading_l2_max: int = 120
    # Learned patterns for headings that carry no number at all — "LIBRO
    # PRIMERO", "CAPITULO SEGUNDO". HEADING_RE requires a leading number, so
    # without these a book numbering its parts in words indexes with no table of
    # contents whatsoever. Observed, not theorised.
    #
    # Both default to None, so a document with no learned pattern chunks exactly
    # as it always has. That is deliberate: test_port_fidelity.py pins 328
    # chunks and kinds 309/10/9 against the Go implementation, and a heading rule
    # that fired by default would move those numbers.
    heading_l1_pattern: str | None = None
    heading_l2_pattern: str | None = None

    def __post_init__(self) -> None:
        if self.max_embed_chars <= self.hard_cap_chars:
            raise ValueError(
                "max_embed_chars must exceed hard_cap_chars (invariant #2), "
                f"got {self.max_embed_chars} <= {self.hard_cap_chars}"
            )
        if self.target_chars > self.hard_cap_chars:
            raise ValueError("target_chars must not exceed hard_cap_chars")


@dataclass(frozen=True)
class DocRules:
    """Document-structure rules. Learned by the propose/validate loop."""

    # Anchored regexes matching repeating page headers, stripped by content
    # rather than by position — position-based cutoffs clipped the first body
    # line of every page.
    header_patterns: tuple[str, ...] = ()
    # Bottom fraction of the page treated as footer.
    footer_cutoff: float = 0.06
    # Gap multiplier over the median line gap that marks a paragraph break.
    line_gap_factor: float = 1.5

    def header_res(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.header_patterns]


# --- regexes -----------------------------------------------------------------

# Numbered heading candidate. The dot after the number group is OPTIONAL here,
# matching html/main.go's headingRe.
HEADING_RE = re.compile(r"^(\d+(\.\d+)*)\.?\s+\w", re.UNICODE)
# 5+ dots means a table-of-contents entry with dot leaders, not a heading.
TOC_LINE_RE = re.compile(r"\.{5,}")
# A numbered list item — "1. Defina…", "26. Señale…". The dot is REQUIRED, which
# is what keeps footnotes out. See classify_kind.
NUMBERED_ITEM_RE = re.compile(r"^\d+\.\s")
# A footnote — a bare number then whitespace, no dot: "53 La palabra…".
FOOTNOTE_RE = re.compile(r"^\d+\s")
# Review questions are often imperative rather than interrogative ("11. Defina
# Arrianismo"), and a numbered imperative is indistinguishable from a numbered
# heading on shape alone. rules.py imports this same object — see the note there.
IMPERATIVES = re.compile(
    r"\b(defina|define|señale|senale|explique|mencione|indique|analice|compare|"
    r"justifique|describa|enumere|argumente|comente|responda|complete)\b",
    re.IGNORECASE,
)

# Sentence boundary candidate: terminal punctuation, optional closing
# quotes/brackets, then whitespace. Filtered by _accept_boundary.
SENTENCE_END_RE = re.compile(r"[.!?][\"»'”’)\]]*\s+")

# Abbreviations that end in a period without ending a sentence: scripture
# references (Gén. 2:15, Pr. 22:15), bibliographic and honorific forms.
ABBREVS = frozenset("""
gén gen ex lev núm dt sal pr ecl is jer ez mt mr mc lc jn ro co gá ef fil col
ts ti heb stg ap cf cfr etc p pp cap vol ed eds trad op cit ibid vs sr sra dr
dra st sta núms art fig no nro sig ss aa vv a d c
""".split())

_UPPER_EXTRA = "ÁÉÍÓÚÜÑ¿«—“\"'("


# --- data --------------------------------------------------------------------


@dataclass(frozen=True)
class Paragraph:
    """One blank-line-separated block of the source, with the **byte** offset
    where its stripped text begins."""

    idx: int
    offset: int
    text: str


@dataclass
class Chunk:
    """One unit of retrieval.

    ``text`` is always a byte-exact slice of the source between ``char_from``
    and ``char_to``, so the payload can be checked against the file.
    ``overlap`` and ``context`` are embedded — to carry meaning the chunk's own
    text lacks — but deliberately kept out of ``text`` so the span stays
    truthful.
    """

    index: int
    kind: str
    chapter: str
    section: str
    text: str
    char_from: int
    char_to: int
    para_from: int
    para_to: int
    overlap: str = ""
    context: str = ""
    # Set by non-text extractors instead of char_from/char_to, e.g.
    # "Ventas 2025!A41:D60" for a spreadsheet row window.
    cell_ref: str = ""
    extra: dict = field(default_factory=dict)

    def breadcrumb(self) -> str:
        if not self.chapter:
            return self.section
        if not self.section:
            return self.chapter
        return f"{self.chapter} > {self.section}"

    def embed_text(self) -> str:
        """What actually goes to the embeddings API.

        The breadcrumb goes inside the request's ``content`` field rather than a
        separate ``title`` field (invariant #4): ``title`` is only reliably
        supported by the older ``text-embedding-*`` models.
        """
        parts: list[str] = []
        if crumb := self.breadcrumb():
            parts.append(crumb)
        if self.context:
            parts.append(self.context)
        if self.overlap:
            parts.append(f"[…] {self.overlap}")
        parts.append(self.text)
        return "\n\n".join(parts)


# --- source reading ----------------------------------------------------------


def read_source(path: str) -> tuple[bytes, list[Paragraph]]:
    """Read a text file as bytes and split it into paragraphs with byte offsets.

    Bytes, not str: ``char_span`` is a byte offset (invariant #1), and indexing a
    Python ``str`` would give character offsets that silently disagree with the
    Go implementation and with anything reading the file in binary.
    """
    with open(path, "rb") as f:
        data = f.read()
    return data, split_paragraphs(data)


def split_paragraphs(data: bytes) -> list[Paragraph]:
    """Split on blank lines, tracking byte offsets.

    Mirrors ``fix/main.go``'s splitParagraphs plus offset tracking.
    """
    out: list[Paragraph] = []
    pos = 0
    for raw in data.split(b"\n\n"):
        start = pos
        pos += len(raw) + 2  # len(b"\n\n")
        stripped = raw.strip()
        if not stripped:
            continue
        # stripped begins with a non-space byte, so its first occurrence in raw
        # is exactly at the end of the leading whitespace strip() removed.
        start += raw.index(stripped)
        out.append(
            Paragraph(idx=len(out), offset=start, text=stripped.decode("utf-8"))
        )
    return out


# --- heading detection -------------------------------------------------------


def heading_level(s: str, rules: ChunkRules) -> int:
    """Return 1-4 for numbered headings ("2.1.1 Foo" -> 3), 0 for anything else.

    A profile may also supply ``heading_l1_pattern`` / ``heading_l2_pattern`` for
    families whose headings carry no number. Those are consulted only when
    learned, and still obey the same length guards — a "heading" longer than the
    cap is prose that happened to match.

    Copied from ``html/main.go``'s headingLevel. The guards are load-bearing:
    without the level-1 length cap, the review question "1. Defina qué es un
    metarrelato o metanarrativa" (46 chars) reads as a fifth chapter and poisons
    the breadcrumbs of every end-of-chapter question block.
    """
    if TOC_LINE_RE.search(s):
        return 0  # TOC line with dot leaders
    if "¿" in s or "?" in s or IMPERATIVES.search(s):
        # A numbered review question, not a heading. This check runs *before*
        # every heading rule below, learned ones included: "11. Defina
        # Arrianismo" is indistinguishable from a numbered heading on shape
        # alone, and letting it through put a year in the chapter sequence once.
        return 0
    if rules.heading_l1_pattern and re.match(rules.heading_l1_pattern, s):
        return 1 if len(s) <= rules.heading_l1_max else 0
    if rules.heading_l2_pattern and re.match(rules.heading_l2_pattern, s):
        return 2 if len(s) <= rules.heading_l2_max else 0
    if not HEADING_RE.match(s):
        return 0
    if sum(c.isdigit() for c in s) > sum(c.isalpha() for c in s):
        # A "numbered heading" with more digits than letters is a printer's
        # signature line or a run of page numbers, not a heading. Measured on
        # 02-PuertasEternas: "12 13 14 15 16 v6 5 4 3 2 1" (18 digits, 1 letter)
        # read as chapter 12 and became the breadcrumb of 13 chunks — the whole
        # epigraph and introduction, including four named sections. The length
        # cap above cannot catch it: at 27 chars it is well under heading_l1_max.
        # A real numbered heading always carries its title ("2.1.1 Foo" is 3 and
        # 3, which this lets through).
        return 0
    prefix = s.split()[0].rstrip(".")
    level = prefix.count(".") + 1
    if level == 1 and len(s) > rules.heading_l1_max:
        return 0
    if level >= 2 and len(s) > rules.heading_l2_max:
        return 0
    return level


def classify_kind(text: str, rules: ChunkRules) -> str:
    """Sort a non-heading paragraph into prose, review questions or a footnote.

    All three rules were validated against a real book:

    * **The dot after the leading number is the discriminator** between questions
      and footnotes. Every review question has it ("1. Defina qué es un
      metarrelato", "26. Señale algunos de los peligros"); no footnote does
      ("1\\nRecordemos que…", "53 La palabra “nihilismo”…"). Matching a merely
      *optional* dot — as ``HEADING_RE`` does — tags nine footnotes as questions.
      That was a real bug, and it only surfaced by printing the classifier's
      output over the whole document rather than trusting the rule.
    * The ¿-density rule catches the one question paragraph that does not begin
      with a number at all: "Freud\\n27. ¿Hasta dónde…".
    * Body prose asks rhetorical questions too — seven paragraphs carried 2 to 5
      ¿ — but at one per 800-4300 chars, an order of magnitude below the
      threshold. The longest had 2 ¿ in 8581 chars; a real question block has 17
      in 2054.
    """
    if heading_level(text, rules) > 0:
        return KIND_BODY  # callers normally handle headings; be safe if not
    if TOC_LINE_RE.search(text):
        # A table-of-contents line ("10. El «concordato evangélico»......105") is
        # numbered and dotted, so `NUMBERED_ITEM_RE` reads it as a review
        # question. `heading_level` already refuses it for the same reason and
        # returns 0; this classifier took that 0 and did not consult the guard.
        # Measured on 01_RetoDeDios_INT-S.pdf: 99 index lines tagged `preguntas`,
        # each one also resetting the section path (invariant #11).
        return KIND_BODY
    if NUMBERED_ITEM_RE.match(text):
        return KIND_QUESTIONS
    n = text.count("¿")
    if n and n * rules.question_chars_per_mark >= len(text.encode("utf-8")):
        # Density alone is not enough on a rhetorical essay. The rule exists for
        # the block that does not *begin* with a number but still is one
        # ("Freud\n27. ¿Hasta dónde…"), so a single mark must be corroborated by
        # a numbered item somewhere inside the paragraph; several marks
        # corroborate themselves. See `question_min_marks`.
        numbered_inside = any(
            NUMBERED_ITEM_RE.match(line) for line in text.splitlines()
        )
        if n >= rules.question_min_marks or numbered_inside:
            return KIND_QUESTIONS
    if FOOTNOTE_RE.match(text):
        return KIND_FOOTNOTE
    return KIND_BODY


# --- chunking ----------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """Smallest indivisible piece a chunk is built from: a whole paragraph, or
    one sentence-bounded slice of an oversized paragraph."""

    para_idx: int
    offset: int
    text: bytes


def build_chunks(
    src: bytes,
    paras: list[Paragraph],
    rules: ChunkRules | None = None,
    classify: "Callable[[str, ChunkRules], str] | None" = None,
) -> list[Chunk]:
    """Walk the paragraphs tracking the heading hierarchy, and pack each run of
    same-kind content into windows.

    Chunks never span a heading, and never mix body prose with review questions
    or footnotes.

    ``classify`` overrides the built-in kind rules, and it has to be injected
    here rather than applied to the finished chunks: **a change of kind is a
    chunk boundary**. Relabelling afterwards can only rename a chunk the default
    rules already cut, so a family whose review questions are marked "P1" rather
    than "1." got its questions and its prose merged into one chunk and then
    relabelled by whichever paragraph happened to come first. Defaults to
    `classify_kind`, so a caller passing nothing gets byte-identical output.
    """
    rules = rules or ChunkRules()
    classify = classify or classify_kind
    chunks: list[Chunk] = []
    chapter = ""
    section_path: list[str] = []
    pending: list[Paragraph] = []
    pending_kind = ""

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        chunks.extend(
            _windowize(
                src,
                pending,
                pending_kind,
                chapter,
                " > ".join(section_path),
                len(chunks),
                rules,
            )
        )
        pending = []

    for p in paras:
        level = heading_level(p.text, rules)
        if level > 0:
            # A heading closes the previous section before it changes the path.
            flush()
            if level == 1:
                chapter = p.text
                section_path = []
            else:
                depth = min(level - 2, len(section_path))
                section_path = section_path[:depth] + [p.text]
            continue

        kind = classify(p.text, rules)
        if kind != pending_kind:
            # A change of kind is a boundary, exactly like a heading.
            flush()
            pending_kind = kind
            # Review questions close the open section: they belong to the whole
            # chapter, not to whichever subsection happened to come last.
            # Without this reset they inherit that subsection's breadcrumb and
            # claim, for instance, that chapter 2's review questions are part of
            # "2.10. Freud y el libre ejercicio de la sexualidad".
            #
            # Footnotes are NOT reset: they annotate the section they sit in.
            if kind == KIND_QUESTIONS:
                section_path = []
        pending.append(p)

    flush()
    return chunks


def _windowize(
    src: bytes,
    paras: list[Paragraph],
    kind: str,
    chapter: str,
    section: str,
    start_index: int,
    rules: ChunkRules,
) -> list[Chunk]:
    """Pack a run of paragraphs into chunks of at most ``hard_cap_chars``, aiming
    for ``target_chars``, each carrying an overlap tail from its predecessor."""
    units: list[_Unit] = []
    for p in paras:
        units.extend(_split_oversized(p, rules))

    out: list[Chunk] = []
    cur: list[_Unit] = []
    size = 0

    def emit() -> None:
        nonlocal cur, size
        if not cur:
            return
        frm = cur[0].offset
        last = cur[-1]
        to = last.offset + len(last.text)
        text = src[frm:to].strip().decode("utf-8")

        if len(text.encode("utf-8")) >= rules.min_chunk_chars:
            c = Chunk(
                index=start_index + len(out),
                kind=kind,
                chapter=chapter,
                section=section,
                text=text,
                char_from=frm,
                char_to=to,
                para_from=cur[0].para_idx,
                para_to=last.para_idx,
            )
            # Overlap comes from the chunk just emitted, and only if the
            # resulting embedded text still fits max_embed_chars.
            if out:
                tail = _tail_context(out[-1].text, rules)
                budget = len(c.breadcrumb()) + len(tail) + len(text) + 10
                if budget <= rules.max_embed_chars:
                    c.overlap = tail
            out.append(c)
        cur = []
        size = 0

    for u in units:
        if cur and size + 2 + len(u.text) > rules.target_chars:
            emit()
        cur.append(u)
        size += len(u.text) + (2 if len(cur) > 1 else 0)
    emit()

    # Reindex: chunks dropped for being under min_chunk_chars would leave gaps.
    return [replace(c, index=start_index + i) for i, c in enumerate(out)]


def _split_oversized(p: Paragraph, rules: ChunkRules) -> list[_Unit]:
    """Break a paragraph longer than ``hard_cap_chars`` at sentence boundaries,
    falling back to word boundaries for runaway sentences."""
    body = p.text.encode("utf-8")
    if len(body) <= rules.hard_cap_chars:
        return [_Unit(p.idx, p.offset, body)]

    out: list[_Unit] = []
    start = end = 0
    for s0, s1 in _sentence_spans(p.text):
        if end > start and s1 - start > rules.hard_cap_chars:
            out.append(_Unit(p.idx, p.offset + start, body[start:end].strip()))
            start = s0
        if s1 - s0 > rules.hard_cap_chars:
            # A single sentence over the cap: chop it at word boundaries.
            for w0, w1 in _word_spans(body[s0:s1], rules):
                out.append(
                    _Unit(p.idx, p.offset + s0 + w0, body[s0 + w0 : s0 + w1].strip())
                )
            start = end = s1
            continue
        end = s1
    if end > start:
        out.append(_Unit(p.idx, p.offset + start, body[start:end].strip()))
    return out


def _sentence_spans(s: str) -> list[tuple[int, int]]:
    """[start, end) **byte** ranges of sentences in s, covering all of s.

    Boundaries are accepted only when they look like real sentence ends.
    """
    body = s.encode("utf-8")
    spans: list[tuple[int, int]] = []
    start = 0
    for m in SENTENCE_END_RE.finditer(s):
        # Convert char offsets to byte offsets: the rest of this module speaks
        # bytes, and mixing the two is exactly how char_span silently breaks.
        b_punct = len(s[: m.start()].encode("utf-8"))
        b_after = len(s[: m.end()].encode("utf-8"))
        if not _accept_boundary(s, m.start(), m.end()):
            continue
        spans.append((start, b_after))
        start = b_after
        del b_punct
    if start < len(body):
        spans.append((start, len(body)))
    return spans


def _accept_boundary(s: str, punct: int, after: int) -> bool:
    """Reject the two common false positives: abbreviations ("Gén. 2:15",
    "cfr.") and lowercase continuations."""
    if after >= len(s):
        return False
    word = _last_word(s[:punct]).lower()
    if word in ABBREVS:
        return False
    nxt = s[after]
    if "A" <= nxt <= "Z" or nxt in _UPPER_EXTRA:
        return True
    return "À" <= nxt <= "Þ"


def _last_word(s: str) -> str:
    for i in range(len(s) - 1, -1, -1):
        if s[i] in " \t\n\r":
            return s[i + 1 :]
    return s


def _word_spans(b: bytes, rules: ChunkRules) -> list[tuple[int, int]]:
    """Chop b into [start, end) ranges of at most ``hard_cap_chars``, breaking
    only at spaces so no multi-byte character is ever split."""
    spans: list[tuple[int, int]] = []
    start = 0
    while len(b) - start > rules.hard_cap_chars:
        cut = start + rules.hard_cap_chars
        i = b.rfind(b" ", start, cut)
        if i > 0:
            cut = i
        spans.append((start, cut))
        start = cut
    if start < len(b):
        spans.append((start, len(b)))
    return spans


def _tail_context(s: str, rules: ChunkRules) -> str:
    """The last complete sentences of s, up to ``overlap_chars`` bytes."""
    body = s.encode("utf-8")
    spans = _sentence_spans(s)
    for i in range(len(spans) - 1, -1, -1):
        if len(body) - spans[i][0] > rules.overlap_chars:
            if i == len(spans) - 1:
                return _tail_words(
                    body[spans[i][0] :].strip().decode("utf-8"), rules.overlap_chars
                )
            return body[spans[i + 1][0] :].strip().decode("utf-8")
    return s.strip()


def _tail_words(s: str, max_bytes: int) -> str:
    """The last <= max_bytes bytes of s, cut at a space so the result starts on a
    character boundary."""
    body = s.encode("utf-8")
    if len(body) <= max_bytes:
        return s
    cut = len(body) - max_bytes
    i = body.find(b" ", cut)
    if i >= 0:
        cut = i + 1
    return body[cut:].strip().decode("utf-8")
