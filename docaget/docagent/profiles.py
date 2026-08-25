"""Learned profiles: the agent's memory between runs.

A profile is everything the agent worked out about a *family* of documents — the
repeating header patterns, the footer zone, the heading length guards, the
questions-vs-footnotes rule, the chunking parameters, and the scores those
choices achieved. It is keyed by a **fingerprint** built from stable structural
signals rather than from the file's bytes, so a second lecture PDF from the same
course matches the first one's profile and skips rule learning entirely.

That is where the self-improvement compounds: the first document of a family pays
for the exploration, and every one after it is cheaper and more consistent.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import time
from dataclasses import asdict, dataclass, field, replace

from .chunk import ChunkRules, DocRules

PROFILE_DIR = pathlib.Path("profiles")


@dataclass
class RetrievalParams:
    """Retrieval-side knobs, tuned by the parameter loop."""

    min_score: float = 0.60
    per_section: int = 2
    dense_only: bool = False


@dataclass
class Scores:
    """What the profile's choices actually achieved, so a later run can tell
    whether it is doing better or worse."""

    recall_at_1: float = 0.0
    recall_at_5: float = 0.0
    mrr_at_10: float = 0.0
    # Same metrics with the lexical leg switched off. The gap between the two is
    # how the synthetic eval's vocabulary leakage is made visible rather than
    # hidden — see nodes/evaluate.py.
    recall_at_5_dense_only: float = 0.0
    noise_floor: float = 0.0
    chunks: int = 0
    eval_questions: int = 0

    def summary(self) -> str:
        return (
            f"recall@1={self.recall_at_1:.3f} recall@5={self.recall_at_5:.3f} "
            f"MRR@10={self.mrr_at_10:.3f} (dense-only recall@5="
            f"{self.recall_at_5_dense_only:.3f}, noise={self.noise_floor:.3f})"
        )


@dataclass
class EvalItem:
    """One synthetic question and the passage it was generated from.

    The passage is identified by ``char_mid`` — the **byte** offset of the midpoint
    of the chunk the question came from — and not by ``chunk_index``.

    That matters as soon as the tuning loop touches chunking: changing the target
    size or the overlap renumbers every chunk, so an eval set keyed by index would
    silently start scoring the wrong passages and the loop would optimise against
    noise. A byte offset survives re-chunking, and a retrieved chunk counts as a
    hit when its ``char_span`` contains that offset.

    ``chunk_index`` is kept for reporting only, since it is meaningless after a
    re-chunk. Structured sources (spreadsheets) have no byte stream, so they fall
    back to matching on ``cell_ref``.
    """

    question: str
    chunk_index: int
    char_mid: int = -1
    cell_ref: str = ""
    breadcrumb: str = ""

    def matches(self, payload: dict) -> bool:
        """Whether a retrieved chunk is the passage this question came from."""
        if self.char_mid >= 0:
            span = payload.get("char_span") or []
            if len(span) == 2 and span[0] <= self.char_mid < span[1]:
                return True
            # A chunk with no byte span (structured source) cannot match by offset.
            if len(span) == 2 and span != [0, 0]:
                return False
        if self.cell_ref:
            return payload.get("cell_ref") == self.cell_ref
        return int(payload.get("chunk_index", -1)) == self.chunk_index


@dataclass
class Profile:
    fingerprint: str
    slug: str
    extractor: str
    doc_rules: DocRules = field(default_factory=DocRules)
    chunk_rules: ChunkRules = field(default_factory=ChunkRules)
    # Learned kind discriminators. None means "use the measured built-in rules",
    # which is a known-good configuration rather than a gap.
    question_pattern: str | None = None
    footnote_pattern: str | None = None
    retrieval: RetrievalParams = field(default_factory=RetrievalParams)
    scores: Scores = field(default_factory=Scores)
    evalset: list[EvalItem] = field(default_factory=list)
    # Provenance, so a profile can be read as a record of how it was reached.
    learned_from: str = ""
    learned_at: float = 0.0
    revisions: int = 0
    validation_notes: list[str] = field(default_factory=list)
    tuning_history: list[dict] = field(default_factory=list)

    def path(self) -> pathlib.Path:
        return PROFILE_DIR / f"{self.slug}.json"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["doc_rules"]["header_patterns"] = list(self.doc_rules.header_patterns)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        doc = d.get("doc_rules", {})
        chunk = d.get("chunk_rules", {})
        return cls(
            fingerprint=d["fingerprint"],
            slug=d["slug"],
            extractor=d.get("extractor", ""),
            doc_rules=DocRules(
                header_patterns=tuple(doc.get("header_patterns", ())),
                footer_cutoff=doc.get("footer_cutoff", 0.06),
                line_gap_factor=doc.get("line_gap_factor", 1.5),
            ),
            chunk_rules=ChunkRules(**chunk) if chunk else ChunkRules(),
            question_pattern=d.get("question_pattern"),
            footnote_pattern=d.get("footnote_pattern"),
            retrieval=RetrievalParams(**d.get("retrieval", {})),
            scores=Scores(**d.get("scores", {})),
            evalset=[EvalItem(**e) for e in d.get("evalset", [])],
            learned_from=d.get("learned_from", ""),
            learned_at=d.get("learned_at", 0.0),
            revisions=d.get("revisions", 0),
            validation_notes=d.get("validation_notes", []),
            tuning_history=d.get("tuning_history", []),
        )

    def save(self) -> pathlib.Path:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        p = self.path()
        p.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return p

    def bumped(self, **changes) -> "Profile":
        return replace(self, revisions=self.revisions + 1, learned_at=time.time(), **changes)


# --- fingerprinting ----------------------------------------------------------


def fingerprint(evidence, extractor: str) -> str:
    """Stable id for a document *family*.

    Built from structural signals that repeat across documents of the same kind
    and ignore their content: the extractor, the repeating header lines, the page
    geometry rounded to a coarse bucket, and the shape of the heading numbering.
    Two lectures from the same course share all four; an unrelated PDF shares
    none.

    Deliberately NOT included: page count, file name, word counts — all of which
    differ between siblings and would defeat reuse.
    """
    parts: list[str] = [extractor]

    # The repeating headers are the strongest family signal, normalised so that a
    # different volume number or year does not fork the family.
    headers = sorted(_normalise_header(h) for h in getattr(evidence, "repeated_lines", {}))
    parts.append("|".join(headers))

    height = getattr(evidence, "page_height", 0.0) or 0.0
    parts.append(f"h{int(round(height / 25.0)) * 25}")

    # Heading numbering shape: how deep does it go, e.g. "1." vs "1.1.1.1.".
    depths = sorted(
        {ln.split()[0].count(".") for ln in getattr(evidence, "numbered_lines", []) if ln.split()}
    )
    parts.append("d" + ",".join(str(d) for d in depths))

    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _normalise_header(h: str) -> str:
    """Collapse whitespace and blank out digits, so "TOMO 3" and "TOMO 4" are the
    same family."""
    return _WS.sub(" ", _DIGITS.sub("#", h)).strip().upper()


def slug_for(path: str, fp: str) -> str:
    stem = pathlib.Path(path).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:48] or "doc"
    return f"{stem}-{fp[:8]}"


# --- store -------------------------------------------------------------------


def load(fp: str) -> Profile | None:
    """Find a saved profile by fingerprint. Returns None on a miss.

    More than one file can carry the same fingerprint: the slug is derived from
    the document that first learned the profile, so a run that skipped reuse
    leaves a second file for the family behind. Returning whichever sorted first
    made the answer depend on the book's title — the stale file from an aborted
    experiment won over a profile with eight measured revisions. The newest
    ``learned_at`` wins instead.
    """
    if not PROFILE_DIR.exists():
        return None
    best: Profile | None = None
    for p in sorted(PROFILE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("fingerprint") != fp:
            continue
        cand = Profile.from_dict(d)
        if best is None or cand.learned_at > best.learned_at:
            best = cand
    return best


def all_profiles() -> list[Profile]:
    if not PROFILE_DIR.exists():
        return []
    out: list[Profile] = []
    for p in sorted(PROFILE_DIR.glob("*.json")):
        try:
            out.append(Profile.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    return out
