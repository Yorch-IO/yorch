"""What one indexed version actually contains, checked against what produced it.

`auditlog.py` answers "what did this run do"; this module answers the question
underneath it — **is the index that run left behind still coherent with its own
artifacts**. The two are separate because a run can succeed, bill honestly, leave
a complete trail, and still leave a version whose Qdrant points, graph nodes and
`chunks.jsonl` disagree with each other. Every failure this module looks for has
happened here at least once and none of them failed anything at the time:

- a re-index that produced fewer chunks left the previous chunking's tail alive
  in Qdrant, carrying `char_span`s into a byte stream nothing holds;
- a second semantic extraction MERGE'd rather than converged, leaving claims
  attached to chunks whose text had moved under them;
- a node written without a tenant, or with the wrong one, which reads as a graph
  nobody has projected yet rather than as a leak.

Everything here is a **pure function over rows**, for the reason `radial.ts` and
`force.ts` are pure on the other side of the product: the comparisons are what
is worth asserting, and they are worth asserting without a Postgres, a Memgraph
and a Qdrant standing up first. `scripts/audit_version.py` is the thin half that
opens the stores and prints.

**Nothing here writes.** The Cypher literals below are server-owned reads in the
sense `graph/projection.py` establishes — they are literals in this repository,
never templates a planner can name by id — and :func:`assert_read_only` refuses
any of them that acquires a write clause, so the audit cannot become a writer by
somebody editing a string.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# The read-only guarantee
# ---------------------------------------------------------------------------

#: Clauses that would make a statement a write. Matched on word boundaries: a
#: substring test flags `OFFSET` for `SET` and `CREATED_AT` for `CREATE`, and a
#: guard that cries wolf is one somebody turns off.
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DETACH|DROP|LOAD\s+CSV|FOREACH)\b",
    re.IGNORECASE,
)


class NotReadOnly(RuntimeError):
    """A statement this module would run contains a write clause."""


def assert_read_only(cypher: str) -> str:
    """Return `cypher`, or refuse it.

    These statements run through `Graph.write`, which is how `projection.py`
    already runs *its* reads — `_COUNT_VERSION_SUBGRAPH` and
    `_CANDIDATE_CONCEPTS` are both reads on that path. That is deliberate on
    their part and load-bearing here: Memgraph does not enforce the read access
    mode, so `default_access_mode="READ"` would not have made this safe either.
    What makes it safe is that the statement is a literal in this file. This
    function is the check that keeps it one.
    """
    found = _WRITE_CLAUSE.search(cypher)
    if found:
        raise NotReadOnly(
            f"audit Cypher must not write; found {found.group(0)!r} in: "
            f"{' '.join(cypher.split())[:120]}"
        )
    return cypher


# ---------------------------------------------------------------------------
# The statements
# ---------------------------------------------------------------------------

VERSION_NODE = assert_read_only(
    """
MATCH (v:DocumentVersion {id: $version_id})
OPTIONAL MATCH (d:Document)-[:HAS_VERSION]->(v)
RETURN v.id AS id, v.tenant_id AS tenant_id, v.title AS title,
       v.content_sha256 AS content_sha256,
       collect(DISTINCT d.id) AS documents
"""
)

#: Every chunk the version holds, with the three things worth cross-checking:
#: the tenant it carries, the byte span it claims, and its text.
#:
#: The text is a book's worth of bytes and is fetched anyway, because it is what
#: a stale claim's quote has to be checked against. A claim left behind by an
#: earlier extraction carries a quote that *was* verified — against a cutting
#: that has since moved — and there is no way to see that without the text the
#: chunk holds now.
CHUNK_NODES = assert_read_only(
    """
MATCH (v:DocumentVersion {id: $version_id, tenant_id: $tenant_id})-[:HAS_CHUNK]->(c:Chunk)
RETURN c.id AS id, c.ordinal AS ordinal, c.tenant_id AS tenant_id,
       c.char_start AS char_start, c.char_end AS char_end, c.kind AS kind,
       c.text AS text, c.qdrant_point_id AS qdrant_point_id
"""
)

SECTION_NODES = assert_read_only(
    """
MATCH (v:DocumentVersion {id: $version_id})-[:HAS_SECTION]->(s:Section)
RETURN s.id AS id, s.level AS level, s.title AS title, s.tenant_id AS tenant_id
"""
)

#: The locator embeds the version's title, which can outlive the document that
#: supplied it — so the audit reads the locators rather than counting them.
CITATION_NODES = assert_read_only(
    """
MATCH (:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(c:Chunk)-[:CITES]->(cit:Citation)
RETURN cit.id AS id, cit.locator AS locator, cit.tenant_id AS tenant_id,
       c.id AS chunk_id
"""
)

#: Claims reachable the way `claims_about_concept` reaches them — through the
#: chunk. A claim this misses is one no read surface can return.
CLAIM_NODES = assert_read_only(
    """
MATCH (cl:Claim)-[:DERIVED_FROM]->(c:Chunk)
     <-[:HAS_CHUNK]-(:DocumentVersion {id: $version_id, tenant_id: $tenant_id})
RETURN cl.id AS id, cl.text AS text, cl.quote AS quote, cl.status AS status,
       cl.tenant_id AS tenant_id, cl.source_chunk_id AS source_chunk_id
"""
)

#: Claims whose `source_chunk_id` names one of this version's chunks but which
#: have no `DERIVED_FROM` edge to reach them by. Property-based rather than
#: traversal-based on purpose: these are exactly the ones a traversal cannot
#: find, which is why 214 of them sat in this graph unnoticed.
ORPHAN_CLAIMS = assert_read_only(
    """
MATCH (cl:Claim) WHERE cl.source_chunk_id IN $chunk_ids
  AND NOT (cl)-[:DERIVED_FROM]->(:Chunk)
RETURN cl.id AS id, cl.source_chunk_id AS source_chunk_id, cl.text AS text
"""
)

MENTION_EDGES = assert_read_only(
    """
MATCH (:DocumentVersion {id: $version_id, tenant_id: $tenant_id})
      -[:HAS_CHUNK]->(c:Chunk)-[m:MENTIONS]->(k:Concept)
RETURN c.id AS source_id, k.id AS target_id, m.confidence AS confidence
"""
)

#: Every concept this version reaches, by all three routes, with the degree the
#: database counted. Deliberately the same three paths `_CANDIDATE_CONCEPTS`
#: walks: a fourth route added there and not here would make this audit blind to
#: exactly the concepts that route exists to reach.
CONCEPTS_REACHED = assert_read_only(
    """
MATCH (:DocumentVersion {id: $version_id, tenant_id: $tenant_id})-[:HAS_CHUNK]->(c:Chunk)
OPTIONAL MATCH (c)-[:MENTIONS]->(mentioned:Concept)
OPTIONAL MATCH (c)<-[:DERIVED_FROM]-(:Claim)-[:ABOUT]->(claimed:Concept)
OPTIONAL MATCH (c)<-[:DERIVED_FROM]-(:Claim)-[:INVOLVES]->(involved:Concept)
WITH collect(DISTINCT mentioned) + collect(DISTINCT claimed)
     + collect(DISTINCT involved) AS nodes
UNWIND nodes AS k
WITH DISTINCT k WHERE k IS NOT NULL
RETURN k.id AS id, k.name AS name, k.tenant_id AS tenant_id
"""
)


# ---------------------------------------------------------------------------
# Reporting shapes
# ---------------------------------------------------------------------------


def leg(
    name: str | None, data: dict[str, Any], *, detail: str = ""
) -> dict[str, Any]:
    """A leg that answered.

    `name` is `None` for a caller that keys its legs **by name already** — the
    statistics route puts them under `structure`, `semantics` and so on, where a
    `leg` field would say a second time what the key says once. The audit script
    passes one because its legs travel in a *list*, and there the name is the
    only thing identifying a row.

    The distinction is not tidiness. A field no client declares is one every
    client silently drops, which is the shape this codebase already records as a
    defect on the other side of the same hop.
    """
    named = {"leg": name} if name is not None else {}
    return {**named, "available": True, "detail": detail, **data}


def unavailable(name: str | None, reason: str) -> dict[str, Any]:
    """A leg that could not answer, which is not the same as one that found zero.

    `/project-summary`'s rule, applied per leg: a stopped Memgraph must render as
    "could not ask", never as a version with no concepts. Every figure the leg
    would have carried is absent rather than zeroed, so a reader cannot quote one
    by accident.
    """
    named = {"leg": name} if name is not None else {}
    return {**named, "available": False, "detail": reason}


# ---------------------------------------------------------------------------
# Chunks against the byte stream
# ---------------------------------------------------------------------------


def verify_spans(chunks: Sequence[dict[str, Any]], raw: bytes) -> dict[str, Any]:
    """Does every chunk's `[char_from, char_to)` still hold its own text?

    The engine's first invariant, checked from the outside. The offsets index
    whichever stream the run chunked — the corrected one when correction ran, the
    extracted one when it did not — so the caller passes the bytes; guessing here
    would turn a correct index into a reported failure.

    A mismatch is reported with both ends visible, because the two ways this
    fails look nothing alike: a *shifted* span decodes to real text from the
    wrong place, and a *stale* one usually will not decode at all.
    """
    verified = 0
    mismatched: list[dict[str, Any]] = []
    for row in chunks:
        index = row.get("index")
        a, b = row.get("char_from"), row.get("char_to")
        text = row.get("text", "")
        if a is None or b is None:
            mismatched.append({"index": index, "why": "no span recorded"})
            continue
        try:
            found = raw[a:b].decode("utf-8")
        except UnicodeDecodeError as e:
            mismatched.append({"index": index, "why": f"span does not decode: {e}"})
            continue
        if found == text:
            verified += 1
        else:
            mismatched.append(
                {
                    "index": index,
                    "why": "span holds different text",
                    "span": [a, b],
                    "expected_head": text[:60],
                    "found_head": found[:60],
                }
            )
    return {
        "chunks": len(chunks),
        "spans_verified": verified,
        "spans_mismatched": len(mismatched),
        "mismatches": mismatched[:20],
        "bytes": len(raw),
    }


#: The streams a run can leave, newest transformation first. A chunk's
#: `char_span` indexes exactly one of them and nothing records which.
STREAMS: tuple[tuple[str, str], ...] = (
    # Correction rewrites the text, which is why it must run before chunking: it
    # changes every offset after it.
    ("corrected.txt", "corrected"),
    # Extraction with the learned profile's rules applied — a second pass the
    # pipeline makes whenever a profile is adopted. Missing this one is how a
    # perfectly byte-exact index reads as 592 broken spans out of 600.
    ("extracted.txt", "extracted"),
    # Extraction before any rule was applied.
    ("raw.txt", "raw"),
    # The video path's uncorrected stream. It is a stream like any other here,
    # and it has to be listed or the one case that most needs auditing is the
    # one this cannot see: `chunk_transcript` falls back to the uncorrected
    # transcript when correction moves the paragraph count, so the offsets index
    # this file and no other. Without it `choose_stream` crowns `corrected` with
    # near-zero verified spans — a byte-exact index reported as broken, which is
    # the exact failure the function's own docstring exists to prevent. It sorts
    # last because a video run holds only this and `corrected.txt`, so its
    # position relative to the document streams cannot matter, and on a tie the
    # corrected stream is the one the pipeline meant to index.
    ("transcript.txt", "transcript"),
)


def choose_stream(
    streams: dict[str, bytes], chunks: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Which stream the chunks actually index, decided by measuring them all.

    Which artifact the offsets belong to is a fact about the run that the run
    does not record: correction rewrites the text, and a learned profile makes a
    *second* extraction pass that rewrites it again, so one run can leave three
    streams side by side with only one of them chunked. Picking by precedence
    looks right and is a guess, and its failure is in the expensive direction —
    a correct index reported as broken, by a tool whose only job is to be
    believed. Measured here on a real run: `raw.txt` scored 8 of 600 where
    `extracted.txt` scored 600.

    When one stream verifies completely there is no judgement left to make. When
    none does, the comparison *is* the finding, and it says whether one stream is
    slightly off or every stream is unrelated — which are different defects.
    """
    scored = {
        label: verify_spans(chunks, data) for label, data in streams.items()
    }
    if not scored:
        return {"chosen": None, "streams": {}}
    chosen = max(scored, key=lambda label: scored[label]["spans_verified"])
    return {
        "chosen": chosen,
        "unanimous": scored[chosen]["spans_mismatched"] == 0,
        "streams": {
            label: {
                "bytes": report["bytes"],
                "spans_verified": report["spans_verified"],
                "spans_mismatched": report["spans_mismatched"],
            }
            for label, report in scored.items()
        },
        "report": scored[chosen],
    }


# ---------------------------------------------------------------------------
# A timed source: the chunk's span is a moment, not a byte offset
# ---------------------------------------------------------------------------


def para_ranges_unique(chunks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Does each chunk cover a paragraph range of its own?

    `_split_oversized` gives every piece of a split paragraph the *same*
    `para_idx`, so a cue group at the chunker's `hard_cap_chars` would make
    several chunks report one time span — confident timestamps that are wrong
    for all but one of them. `GROUP_HARD_CAP` is 900 against the chunker's 2000
    to keep that path unreachable; this is what says so about a finished run
    rather than about the constants.
    """
    ranges = [(c.get("para_from"), c.get("para_to")) for c in chunks]
    seen: dict[tuple[Any, Any], list[int]] = {}
    for c, r in zip(chunks, ranges):
        seen.setdefault(r, []).append(c.get("index"))
    shared = {str(r): idx for r, idx in seen.items() if len(idx) > 1}
    return {
        "unique": not shared,
        "chunks": len(ranges),
        "distinct_ranges": len(seen),
        "shared_by": dict(list(shared.items())[:10]),
    }


def time_report(
    chunks: Sequence[dict[str, Any]],
    table: Sequence[Any],
    duration_s: "float | None" = None,
) -> dict[str, Any]:
    """Every chunk's span, re-derived from the run's own cue table.

    `videosource.span_for` is the deriver and it **raises rather than clamping**
    on a range the table does not hold, so a clamped answer cannot reach here
    wearing the shape of a right one. What the caller gets is the disagreement
    list, which is empty for a healthy run.

    **`end_s` past the duration is reported, not failed.** Measured on a real
    76-minute talk: the cue table ends at 4574.699 s against a probed
    `duration_s` of 4573, because YouTube reports duration as a truncated
    integer while the caption track runs to the true end. An audit that
    asserted `end_s <= duration_s` would mark every auto-captioned video
    defective. No locator is affected — a locator uses `start_s`.
    """
    from . import videosource

    disagreements: list[dict[str, Any]] = []
    for c in chunks:
        try:
            want = videosource.span_for(
                int(c["para_from"]), int(c["para_to"]), list(table)
            )
        except Exception as e:  # noqa: BLE001 — the raise *is* the finding
            disagreements.append({"index": c.get("index"), "raised": str(e)})
            continue
        got = (c.get("start_s"), c.get("end_s"))
        if got != want:
            disagreements.append(
                {"index": c.get("index"), "want": list(want), "found": list(got)}
            )

    starts = [c.get("start_s") for c in chunks]
    ends = [c.get("end_s") for c in chunks]
    timed = [s for s in starts if s is not None]
    covered = max((e for e in ends if e is not None), default=None)
    return {
        "chunks": len(chunks),
        "all_timed": len(timed) == len(chunks),
        "untimed": [c.get("index") for c in chunks if c.get("start_s") is None][:10],
        "derivation_disagreements": disagreements[:10],
        "derivation_disagreement_total": len(disagreements),
        "starts_non_decreasing": all(b >= a for a, b in zip(timed, timed[1:])),
        "start_at_or_before_end": all(
            c["start_s"] <= c["end_s"] for c in chunks
            if c.get("start_s") is not None and c.get("end_s") is not None
        ),
        "covered_s": covered,
        "duration_s": duration_s,
        # Reported, never judged. See the docstring.
        "ends_past_duration": (
            [c.get("index") for c in chunks
             if duration_s is not None and (c.get("end_s") or 0) > duration_s][:10]
            if duration_s is not None else None
        ),
    }


#: A locator's leading segment when the source is timed: `0:00`, `1:14`,
#: `1:16:13`.
_CLOCK_RE = re.compile(r"^\d+:[0-5]\d(?::[0-5]\d)?$")


def title_prefixes(locators: Sequence[str]) -> list[str] | None:
    """The distinct titles a version's citations were built under, or ``None``.

    More than one distinct prefix means a re-projection under a changed title
    left the old citations beside the new, which is the defect this exists to
    catch on a document.

    ``None`` for a timed source, and that is the whole point of the function
    rather than an edge case. `projection._locator` returns early for a video
    and carries **no title at all** — the leading segment is `hhmmss(start_s)`,
    because a YouTube title is mutable by its uploader and putting it in a
    locator forked every citation on a real rebuild. So splitting on the
    separator yields one "title" per chunk, and a healthy 69-chunk video
    reported 69 of them. A check that fires on every correct video is worse
    than no check: it trains a reader to skip the field.
    """
    prefixes = sorted({(loc or "").split(" · ")[0] for loc in locators})
    if prefixes and all(_CLOCK_RE.match(x) for x in prefixes):
        return None
    return prefixes


def chunk_sequence(chunks: Sequence[dict[str, Any]], total_bytes: int) -> dict[str, Any]:
    """Indices, ordering and how much of the file no chunk covers.

    Coverage is reported rather than judged. Chunking legitimately drops material
    — a running header, a page number, whatever fell below `min_chunk_chars` —
    so a gap is a thing to look at, not a failure. What *is* a failure is a hole
    in the index sequence, because point ids are `point_id(version, index)` and a
    missing index is a point nothing will ever overwrite.
    """
    indices = sorted(int(c["index"]) for c in chunks if c.get("index") is not None)
    expected = list(range(len(indices)))
    ordered = sorted(chunks, key=lambda c: c.get("index", 0))

    inverted = [
        {"index": c.get("index"), "span": [c.get("char_from"), c.get("char_to")]}
        for c in ordered
        if c.get("char_from") is not None
        and c.get("char_to") is not None
        and c["char_to"] <= c["char_from"]
    ]
    backwards = [
        {"index": b.get("index"), "after": a.get("index")}
        for a, b in zip(ordered, ordered[1:])
        if (a.get("char_from") or 0) > (b.get("char_from") or 0)
    ]

    covered = 0
    edge = 0
    for c in sorted(
        (c for c in chunks if c.get("char_from") is not None),
        key=lambda c: c["char_from"],
    ):
        start, end = max(int(c["char_from"]), edge), int(c["char_to"])
        if end > start:
            covered += end - start
            edge = end
    return {
        "indices": len(indices),
        "contiguous": indices == expected,
        "missing_indices": sorted(set(expected) - set(indices))[:20],
        "duplicate_indices": sorted(
            {i for i in indices if indices.count(i) > 1}
        )[:20],
        "inverted_spans": inverted[:20],
        "spans_out_of_order": backwards[:20],
        "bytes_covered": covered,
        "bytes_total": total_bytes,
        "coverage": round(covered / total_bytes, 4) if total_bytes else None,
    }


# ---------------------------------------------------------------------------
# What a run produced against what the stores hold
# ---------------------------------------------------------------------------


def stale_diff(produced: Iterable[Any], present: Iterable[Any]) -> dict[str, Any]:
    """The three-way split between what a run made and what is there now.

    `project_claims` and `project_semantic_edges` MERGE, so a second extraction
    over a re-cut document *adds* rather than converges — and `claim_id` keys on
    the model's own paraphrase, so two passes union instead of agreeing. The
    difference is therefore computable exactly, with no estimation: the run's own
    `semantics.json` names precisely what it produced.

    `left_behind` is the defect. `missing` is its mirror and matters just as
    much: something the run produced and the graph does not hold means a
    projection that did not finish, which no count of the graph alone can see.
    """
    made, there = set(produced), set(present)
    converged = made & there
    left = there - made
    return {
        "produced": len(made),
        "in_store": len(there),
        "converged": len(converged),
        "left_behind": len(left),
        "missing": len(made - there),
        "stale_share": round(len(left) / len(there), 4) if there else None,
        "left_behind_sample": sorted(map(str, left))[:20],
        "missing_sample": sorted(map(str, made - there))[:20],
    }


def quote_still_locates(quote: str | None, chunk_text: str | None) -> bool | None:
    """Does a claim's verified quote still occur in the chunk it names?

    `None` for a claim that never carried one — a claim with no quote is a
    recorded state, not a failure, and must not be counted as a broken one.

    Whitespace is tolerated and nothing else, which is the rule the extractor's
    own verification uses: a fixed accent or a dropped word is a quote the
    document does not contain. That equivalence is the whole point here — a stale
    claim is one whose quote was verified against a chunk that has since been
    re-cut, and it is indistinguishable from a good one at read time.
    """
    if not quote:
        return None
    if not chunk_text:
        return False
    collapse = lambda s: " ".join(s.split())  # noqa: E731
    return collapse(quote) in collapse(chunk_text)


# ---------------------------------------------------------------------------
# The eval set
# ---------------------------------------------------------------------------

#: Bytes of UTF-8 that arrived as Latin-1 and were re-encoded. `Â¿` is `¿` and
#: `Ã©` is `é`; neither can occur in correct Spanish, so this is a positive
#: signal rather than a heuristic.
_MOJIBAKE = ("Â", "Ã", "â€")
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")


def suspect_question(question: str) -> str | None:
    """Why this question cannot be asked as stored, or `None` if it can.

    A corrupted question is not a hard failure anywhere — it embeds fine, it
    searches fine, and it reliably retrieves nothing, so it scores as an ordinary
    miss and lowers recall by exactly one question. That is the whole reason to
    look for it: the damage is invisible in the figure it damages.
    """
    if _PERCENT.search(question):
        try:
            decoded = urllib.parse.unquote(question, errors="strict")
        except UnicodeDecodeError:
            return "percent-encoded, and does not decode as UTF-8"
        if decoded != question:
            return "percent-encoded"
    if any(marker in question for marker in _MOJIBAKE):
        return "mojibake: UTF-8 read as Latin-1"
    return None


def scan_questions(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every question in an eval set, sorted into usable and not."""
    suspects = []
    for position, item in enumerate(items):
        why = suspect_question(str(item.get("question", "")))
        if why:
            suspects.append(
                {
                    "position": position,
                    "chunk_index": item.get("chunk_index"),
                    "why": why,
                    "question": str(item.get("question", ""))[:120],
                }
            )
    return {
        "questions": len(items),
        "suspect": len(suspects),
        "usable": len(items) - len(suspects),
        "suspects": suspects,
    }


def misses_explained(
    suspects: Sequence[dict[str, Any]], misses: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """How much of a recorded miss list is corrupted questions rather than index.

    Matched on `chunk_index`/`want`, which is what both artifacts carry. This
    bounds the damage without asserting it: a corrupted question that happens to
    name the same chunk as a genuine miss is counted here as explained, so the
    figure is an upper bound and is reported as one.
    """
    corrupted = {s.get("chunk_index") for s in suspects if s.get("chunk_index") is not None}
    hit = [m for m in misses if m.get("want") in corrupted]
    return {
        "misses": len(misses),
        "misses_from_suspect_questions": len(hit),
        "share": round(len(hit) / len(misses), 4) if misses else None,
        "note": "upper bound: a corrupted question naming a genuinely missed "
        "chunk is counted here as explained",
    }


def compare_scores(
    measured: dict[str, Any], recorded: dict[str, Any], margin: float
) -> dict[str, Any]:
    """Field-for-field, with the run's own bootstrap margin as the yardstick.

    Quoting a difference without the margin is what makes noise look like a
    finding; it is the same mistake `propose_tuning` exists to refuse, so the
    audit refuses it in the same terms.
    """
    fields = (
        "recall_at_1",
        "recall_at_5",
        "mrr_at_10",
        "recall_at_5_dense_only",
        "noise_floor",
        "chunks",
        "eval_questions",
    )
    rows = []
    for field in fields:
        was, now = recorded.get(field), measured.get(field)
        delta = None if was is None or now is None else round(now - was, 4)
        rows.append(
            {
                "field": field,
                "recorded": was,
                "measured": now,
                "delta": delta,
                "beyond_margin": (
                    None if delta is None else abs(delta) > margin
                ),
            }
        )
    return {"margin": margin, "fields": rows}


def floor_verdict(min_score: float, noise_floor: float) -> dict[str, Any]:
    """Whether the dense floor is above what a wrong chunk scores.

    A `min_score` at or below the noise floor wins the metric by admitting
    exactly what the floor was measured to exclude — and it looks like an
    improvement, because every eval question has a right answer to find and none
    of them is off-corpus. Stated as a verdict rather than two numbers because
    the relation is the finding.
    """
    return {
        "min_score": min_score,
        "noise_floor": noise_floor,
        "headroom": round(min_score - noise_floor, 4),
        "honest": min_score > noise_floor,
    }


# ---------------------------------------------------------------------------
# What a version cost, across every run that touched it
# ---------------------------------------------------------------------------


def cost_by_stage(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold a version's charges by stage across *all* the runs that made them.

    **A version's bill is not one run's bill**, and that is the whole reason
    this exists. `/runs/{id}/audit` answers "what did this run spend"; grouping
    across the version is what makes a stage charged in more than one run
    visible at all. On `ver_0cde0e3196d06e4259a32a52` the eval set was generated
    twice, for **$0.5753 + $0.5710**, the first of them inside a run that was
    cancelled — **$1.1965 of that document's $3.7572, 31.8%**. Nothing in the
    code was wrong about it; cancelling costs what it costs. There was simply no
    way to see it.

    `usd_by_run_state` is what reproduces that finding, and it is deliberately a
    *split* rather than a figure called "wasted". Which spend bought something
    is a judgement the rows cannot make: a cancelled run bought nothing durable,
    but a **failed** one can still have left a complete index behind — the
    2026-08-31 semantics failure billed $10.017265 and left 600 points and 5 001
    claims in the stores under a version the catalog still calls `pending`. So
    the states are reported and the reader draws the line.

    Rows are plain mappings rather than catalog dataclasses so that this stays
    assertable without a Postgres, and so the TypeScript fork can be handed the
    same shape:
    ``[{"run_id": str, "state": str | None, "costs": [{"stage": str, "usd": float | None}]}]``
    """
    totals: dict[str, dict[str, Any]] = {}
    by_state: dict[str, float] = {}
    for run in runs:
        run_id = str(run["run_id"])
        state = run.get("state") or "unknown"
        for charge in run.get("costs") or ():
            stage = str(charge["stage"])
            entry = totals.setdefault(stage, {"usd": 0.0, "runs": [], "unpriced": 0})
            usd = charge.get("usd")
            if usd is None:
                # "Not priced" is not zero. A missing price means the model id
                # is absent from the table, which under-reports the bill rather
                # than describing a free call, and it must never look like one.
                entry["unpriced"] += 1
            else:
                entry["usd"] += float(usd)
                by_state[state] = by_state.get(state, 0.0) + float(usd)
            if run_id not in entry["runs"]:
                entry["runs"].append(run_id)

    return {
        "by_stage": {
            stage: {
                "usd": round(data["usd"], 6),
                "runs": data["runs"],
                "unpriced_entries": data["unpriced"],
            }
            for stage, data in sorted(totals.items())
        },
        "total_usd": round(sum(d["usd"] for d in totals.values()), 6),
        "charged_in_more_than_one_run": sorted(
            stage for stage, data in totals.items() if len(data["runs"]) > 1
        ),
        "usd_by_run_state": {
            state: round(usd, 6) for state, usd in sorted(by_state.items())
        },
    }


def claim_shape(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold `version_claim_shape`'s rows into what the document *does* with its
    claims, and how many of them anybody can check.

    `sin_estado` is carried through as its own key rather than merged into
    `afirma`: a text expounding the doctrine it is about to rebut enunciates it
    in the same words as one who holds it, so "the extractor did not say" and
    "the document asserts this" are different facts and only one of them is a
    claim about the document. Measured on a real document, **14% of claims were
    not plain assertions** — 10 `atribuido` and 1 `niega` out of 79.

    `with_a_quote` is the count of claims carrying a span the code located in
    the chunk's own text. A claim nobody can check must not look like one that
    can, which is why the two travel together.
    """
    by_status: dict[str, int] = {}
    claims = 0
    with_a_quote = 0
    for row in rows:
        status = str(row.get("status") or "sin_estado")
        count = int(row.get("claims") or 0)
        by_status[status] = by_status.get(status, 0) + count
        claims += count
        with_a_quote += int(row.get("with_quote") or 0)
    return {
        "claims": claims,
        "with_a_quote": with_a_quote,
        "by_status": dict(sorted(by_status.items())),
    }


# ---------------------------------------------------------------------------
# What a run produced, against what the graph still holds
# ---------------------------------------------------------------------------
#
# **Keyed on the pre-images of the derived ids, never on the ids themselves**,
# and that is a decision rather than a convenience. `claim_id` is
# `digest(source_chunk_id, collapse_space(text))` and `concept_id` folds a name
# through `canonical_concept` and salts it with the tenant — so a second
# implementation of this comparison, in TypeScript for the paid plane, would
# have to fork the **tenant-salted id contract** into another language. That is
# the one fork this codebase must not take: a drift in `_salt()` is a silently
# wrong graph, not a wrong number.
#
# It is not needed. The graph stores both pre-images of a `claim_id`
# (`cl.source_chunk_id`, `cl.text`, written verbatim by `_MERGE_CLAIMS`), and
# `semantics.json` already carries derived `con_…` ids on its **edges** —
# measured on a real artifact: `MENTIONS` 3 497, `ABOUT` 3 055, `INVOLVES`
# 1 336, which are exactly the three routes `CONCEPTS_REACHED` walks. So the
# partition is identical and nothing is derived on either side.

_CONCEPT_EDGES: frozenset[str] = frozenset({"MENTIONS", "ABOUT", "INVOLVES"})


def collapse_space(text: str) -> str:
    """`schema._collapse_space`, which is what `claim_id` hashes.

    Applied to both sides of the claim key. Without it two claims whose texts
    differ only in whitespace are one node in the graph — they share a
    `claim_id` — and two keys here, which would report a claim as `missing` that
    is sitting right there.
    """
    return _SPACE_RUN.sub(" ", text or "").strip()


_SPACE_RUN = re.compile(r"\s+")


def claim_key(row: Mapping[str, Any]) -> str:
    """The pre-image of this claim's `claim_id`, from either side.

    A **string** rather than a tuple, and that is not cosmetic: `stale_diff`
    renders its samples with `str()`, so a tuple key reaches the wire as
    `"('chk_9', 'De un run anterior.')"` — Python's repr — while the paid
    plane's fork of this produces plain text for the same claim. One field, two
    shapes, depending on which plane answered. A chunk id contains no space, so
    the first one is an unambiguous separator.
    """
    chunk = str(row.get("source_chunk_id") or "")
    return f"{chunk} {collapse_space(str(row.get('text') or ''))}"


def concepts_produced(doc: Mapping[str, Any]) -> set[str]:
    """Every concept id the run's own artifact reaches, by all three routes.

    Read off the edges rather than recomputed from `concepts[].name`: the
    artifact's edges already carry the derived target id, so nothing here has to
    know what a tenant salt is. A concept the run listed but attached to nothing
    is deliberately not counted — `CONCEPTS_REACHED` could not see it either,
    and the two sides have to be asking the same question.
    """
    return {
        str(e["target_id"])
        for e in doc.get("edges") or ()
        if e.get("type") in _CONCEPT_EDGES and e.get("target_id")
    }


def stale_claims(
    doc: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """The graph's claims that this run did not produce.

    Not debris: `claim_id` keys on `(chunk_id, text)` and `chunk_id` keys on
    `(version_id, index)`, so re-chunking keeps every id *alive* while the text
    underneath moves. A stale claim stays attached to a chunk that no longer
    contains the quote it carries, and is indistinguishable from a good one at
    read time.
    """
    produced = {claim_key(c) for c in doc.get("claims") or ()}
    return [c for c in claims if claim_key(c) not in produced]


def semantic_diff(
    doc: Mapping[str, Any],
    *,
    claims: Sequence[Mapping[str, Any]],
    concepts: Sequence[Mapping[str, Any]],
    mentions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The three set differences between a run's semantics and the graph's."""
    return {
        "claims": stale_diff(
            (claim_key(c) for c in doc.get("claims") or ()),
            (claim_key(c) for c in claims),
        ),
        "concepts": {
            # The names the run extracted, before `concept_id` folds them.
            # Reported beside the diff because the two differ legitimately:
            # `canonical_concept` strips accents and punctuation, so "Espíritu
            # Santo" and "Espiritu santo" are one node and two names, and a
            # reader comparing `produced` against `semantics.json` would
            # otherwise read the fold as a loss.
            "names_extracted": len(doc.get("concepts") or ()),
            **stale_diff(concepts_produced(doc), (str(c["id"]) for c in concepts)),
        },
        # Strings here for the same reason `claim_key` is one: a tuple reaches
        # the wire as its own repr, and only from this plane.
        "mentions": stale_diff(
            (
                f"{e['source_id']} {e['target_id']}"
                for e in doc.get("edges") or ()
                if e.get("type") == "MENTIONS"
            ),
            (f"{m['source_id']} {m['target_id']}" for m in mentions),
        ),
    }
