"""The stages that spend money. None of them runs before the gate is answered.

Each one is separately switchable there because they cost wildly different
amounts — from a real measured run, correction $0.0334 against embedding
$0.0033 — so "approve" is a set of switches rather than one yes.

Two properties are shared by everything here and are not optional. The activity
records what it actually spent, from the counts the API reported rather than
from the estimate; and it is idempotent, because Temporal retries and a retry
that re-embedded a book would double a real bill.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict

from temporalio import activity

from .. import config
from ..artifacts import ArtifactStore
from ..catalog import Catalog
from ..graph import Graph
from ..graph import projection as proj
from ..graph.schema import chunk_id as make_chunk_id
from ..graph.schema import concept_id as make_concept_id
from ..pipeline import (
    ChunkKindCount,
    Chunked,
    Correction,
    Extraction,
    Indexed,
    ProfileDecision,
    Registered,
    Semantics,
    Spend,
    StageOptions,
    Staged,
)
from ..providers import Provider, VertexAdapter
from ..providers.gemini import RETRIEVAL_DOCUMENT, Usage
from .ingest import (
    CHARS_PER_TOKEN,
    CONDENSE_SOURCE_CAP,
    CONDENSE_TOKENS,
    CONDENSE_WORDS,
    PROFILE_MAX_ATTEMPTS,
    RECORD_TIMEOUT,
    _Evidence,
    _record,
    _settings,
    kind_classifier,
    chunk_rules,
    price_for,
    rules_from_profile,
    section_tree,
)

log = logging.getLogger(__name__)

#: One Qdrant collection for every library, filtered by payload rather than one
#: collection per library. A collection's vector size is fixed at creation, so
#: many collections means many places a dimension change has to be migrated —
#: and cross-library search becomes a fan-out instead of a filter.
#:
#: The name comes from settings, not from here, so a test suite can write
#: somewhere disposable. This constant is only the default.
DEFAULT_COLLECTION = "brain"

#: Payload fields Qdrant must index for filtering to be usable at all.
#:
#: `tenant_id` is first because it is the one filter every search carries: a
#: library is a shelf inside an organisation, and every other key here narrows
#: within one.
PAYLOAD_INDEXES = ("tenant_id", "library_id", "version_id", "kind", "document_id")

#: Concurrent embedding requests. There is no batch size to choose: the model
#: returns one embedding for a request carrying four texts, with no error
#: (measured 2026-08-19), so `Provider.embed` issues one request per chunk and
#: this only bounds how many are in flight.
EMBED_WORKERS = 6


def _provider() -> Provider:
    return Provider(_settings().gemini)


def _charge(run_id: str, spend: Spend) -> Spend:
    """Record real spend in the catalog. Append-only.

    A retried activity spent its tokens whether or not the attempt succeeded, so
    this appends rather than overwriting — a total that could be revised
    downwards by a retry would under-report the bill that was actually incurred.
    """
    try:
        settings = _settings()
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.record_cost(
                run_id,
                stage=spend.stage,
                provider="vertex",
                model=spend.model,
                input_tokens=spend.input_tokens,
                output_tokens=spend.output_tokens,
                usd=spend.usd,
            )
    except Exception as e:
        # Losing the record is bad; failing the stage that already spent the
        # money is worse, because the retry spends it again.
        log.warning("could not record spend for %s: %s", run_id, e)
    return spend


# ---------------------------------------------------------------------------
# Rule learning — the first stage that spends, and the cheapest
# ---------------------------------------------------------------------------


@activity.defn(name="learn_profile")
async def learn_profile(
    run_id: str, extraction: Extraction, decision: ProfileDecision
) -> ProfileDecision:
    """Learn this document family's structural rules, once, and keep them.

    Runs the engine's own propose/validate loop directly rather than through its
    LangGraph. That graph also embeds into Qdrant, builds an eval set and runs
    the tuning loop — work this pipeline already owns differently, behind its own
    gate and with its own point ids — so invoking it would run a second pipeline
    inside this one. `propose`, `validate` and `adopt` are the three functions
    the graph's nodes call, and calling them is the same work without the rest.

    **Validation is adversarial and that is the point.** The model proposes; a
    deterministic checker applies the proposal to the *whole* document and
    demands an independent signal, then hands back feedback for a refine round.
    Exhausting `PROFILE_MAX_ATTEMPTS` is **not a failure**: the built-in rules
    were themselves measured on a real book, so falling back to them is a
    known-good configuration and the document indexes exactly as it would have
    before this stage existed.

    Adoption is partial — a rule that failed is dropped and the rest are kept.
    Requiring all of them once threw away a header pattern that had validated
    cleanly three times, leaving 175 running-header lines in the text.
    """
    from docagent import profiles as engine_profiles
    from docagent import rules as engine_rules

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    evidence = _Evidence(store.read_json(extraction.evidence))
    text = store.read_bytes(extraction.text)

    adapter = VertexAdapter(_provider())
    feedback: list[str] = []
    validation = None
    proposal = None

    for attempt in range(1, PROFILE_MAX_ATTEMPTS + 1):
        proposal = engine_rules.propose(adapter, evidence, feedback or None)
        validation = engine_rules.validate(proposal, text, evidence)
        _record(
            run_id,
            "proposal",
            store.write_json("proposal", {"attempt": attempt, **asdict(proposal)}),
        )
        _record(
            run_id,
            "validation",
            store.write_json(
                "validation",
                {
                    "attempt": attempt,
                    "passed": validation.passed,
                    "notes": validation.notes(),
                    "failed_rules": sorted(validation.failed_rules()),
                },
            ),
        )
        if validation.passed:
            break
        feedback = validation.feedback
        log.info(
            "profile attempt %d/%d failed: %s",
            attempt, PROFILE_MAX_ATTEMPTS, sorted(validation.failed_rules()),
        )

    spend = _charge(
        run_id,
        Spend(
            stage="profile",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    decision.spend = spend

    if validation is None or proposal is None:  # pragma: no cover - loop always runs
        _record(run_id, "profile", store.write_json("profile", asdict(decision)))
        return decision

    # Adopted even when an essential rule failed every attempt, because `adopt`
    # is per-rule: a failed rule is reset to its measured default and the ones
    # that validated are kept. Discarding the lot instead is the mistake this
    # project already paid for once — and paid for again on the first real run
    # here, where three attempts failed `heading_guards` on a non-contiguous
    # chapter sequence and took with them a header pattern that had matched on
    # 26 pages with no body hits, plus a footnote rule an independent signal
    # confirmed at 100%. Nothing about a bad length guard makes a good header
    # pattern less true.
    adopted = engine_rules.adopted_rules(validation)
    if not adopted:
        # Every rule failed, so the profile would be nothing but defaults —
        # and saving *that* is worse than saving none. The next document of this
        # family matches the same fingerprint, would "reuse" it, and would never
        # try to learn again: one bad run would freeze the family on generic
        # rules permanently. Checked before `save`, not after.
        log.info("profile learning adopted nothing; the measured defaults apply")
        _record(run_id, "profile", store.write_json("profile", asdict(decision)))
        return decision

    profile = engine_rules.adopt(
        proposal,
        validation,
        fingerprint=decision.fingerprint,
        slug=engine_profiles.slug_for(extraction.source_key or run_id, decision.fingerprint),
        extractor=extraction.extractor,
        learned_from=extraction.source_key or run_id,
    )
    profile.save()

    if not validation.passed:
        log.info(
            "profile learning kept %s after %s failed every attempt",
            adopted, sorted(validation.failed_rules()),
        )

    decision.source = "learned"
    decision.slug = profile.slug
    decision.learned_from = profile.learned_from
    decision.revisions = profile.revisions
    decision.rules = rules_from_profile(profile)
    decision.adopted = adopted
    _record(run_id, "profile", store.write_json("profile", asdict(decision)))
    log.info("learned profile %s, adopted %s", profile.slug, decision.adopted)
    return decision


# ---------------------------------------------------------------------------
# Correction — the dominant cost
# ---------------------------------------------------------------------------


@activity.defn(name="correct_text")
async def correct_text(run_id: str, extraction: Extraction) -> Correction:
    """Correct the prose, keeping the engine's verification gate intact.

    Structured sources never reach here: an LLM must not rewrite a cell value,
    and the workflow skips this stage for them.

    Note what this does *not* return: the corrected text inline. It is a whole
    book, and Temporal payloads cap around 2 MB.
    """
    from docagent import correct as engine_correct
    from docagent.chunk import split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    data = store.read_bytes(extraction.text)
    paragraphs = [p.text for p in split_paragraphs(data)]

    adapter = VertexAdapter(_provider())
    corrected, report = engine_correct.correct_paragraphs(
        adapter, paragraphs, stage="correct"
    )

    # Re-joined exactly the way `split_paragraphs` expects to find them, because
    # every char_span computed after this indexes *this* byte stream.
    body = "\n\n".join(corrected).encode("utf-8")
    text_ref = _record(run_id, "corrected_text", store.write_bytes("corrected_text", body))
    report_ref = _record(
        run_id,
        "correction_report",
        store.write_json(
            "correction_report",
            {
                "paragraphs": report.paragraphs,
                "changed": report.changed,
                "unchanged": report.unchanged,
                "missing": report.missing,
                "cache_hits": report.cache_hits,
                "calls": report.calls,
                "rejected": [
                    {"index": r.index, "reason": r.reason, "detail": r.detail}
                    for r in report.rejected
                ],
                "summary": report.summary(),
            },
        ),
    )

    spend = _charge(
        run_id,
        Spend(
            stage="correction",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    return Correction(
        text=text_ref,
        report=report_ref,
        paragraphs=report.paragraphs,
        changed=report.changed,
        rejected=len(report.rejected),
        missing=report.missing,
        cache_hits=report.cache_hits,
        spend=spend,
    )


@activity.defn(name="chunk_final")
async def chunk_final(
    run_id: str, text_kind: str, decision: ProfileDecision | None = None
) -> Chunked:
    """Chunk the text that will actually be indexed. Free, and always re-run.

    Re-run rather than reusing the preview because correction changed the byte
    stream: every `char_from`/`char_to` in the preview indexes text that no
    longer exists at those offsets.

    This is the chunking that decides the document's table of contents, so it is
    the one that must see the profile's rules. Passing `None` gives the engine's
    built-in defaults, which were themselves measured on a real book.
    """
    from docagent.chunk import build_chunks, split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    from ..artifacts import ArtifactRef, KINDS

    path = settings.workspace / "runs" / run_id / KINDS[text_kind]
    data = path.read_bytes()

    rules = decision.rules if decision and decision.source != "default" else None
    paragraphs = split_paragraphs(data)
    chunks = build_chunks(data, paragraphs, chunk_rules(rules), kind_classifier(rules))
    rows = [
        {
            "index": c.index,
            "kind": c.kind,
            "chapter": c.chapter,
            "section": c.section,
            "text": c.text,
            "context": c.context,
            "overlap": c.overlap,
            "embed_text": c.embed_text(),
            "char_from": c.char_from,
            "char_to": c.char_to,
            "cell_ref": c.cell_ref,
        }
        for c in chunks
    ]
    ref = _record(run_id, "chunks", store.write_jsonl("chunks", rows))
    kinds = Counter(c.kind for c in chunks)
    return Chunked(
        chunks=ref,
        count=len(rows),
        kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
    )


# ---------------------------------------------------------------------------
# Embedding and hybrid indexing
# ---------------------------------------------------------------------------


@activity.defn(name="embed_and_index")
async def embed_and_index(
    run_id: str,
    library_id: str,
    registered: Registered,
    staged: Staged,
    chunked: Chunked,
) -> Indexed:
    """Embed every chunk and upsert it into Qdrant with its BM25 sparse vector.

    **Point ids are derived from the version, not the path.** The engine's
    `doc_id_for()` hashes the filename, which is what lets byte-identical
    duplicates index twice and then compete in ranking; using the content-derived
    version id means a re-index of the same bytes overwrites the same points and
    a duplicate file adds none. That also makes this activity idempotent, which
    Temporal requires of it anyway.
    """
    from docagent.bm25 import avg_doc_len, doc_sparse_vector, tokenize
    from docagent.qdrant import Point, Qdrant, point_id

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunked.chunks))
    if not rows:
        return Indexed(
            collection=settings.qdrant_collection,
            points=0,
            dimensions=settings.gemini.embedding_dimensions,
            spend=Spend(stage="embedding", model=settings.gemini.embedding_model),
        )

    provider = _provider()
    embedded = provider.embed(
        [r.get("embed_text") or r["text"] for r in rows],
        task=RETRIEVAL_DOCUMENT,
        workers=EMBED_WORKERS,
    )
    usage = Usage()
    vectors: list[list[float]] = []
    for item in embedded:
        usage.add(item.usage)
        vectors.append(item.values)

    docs = [tokenize(r["text"]) for r in rows]
    avgdl = avg_doc_len(docs)

    collection = settings.qdrant_collection
    with Qdrant(settings.qdrant_url, collection) as q:
        q.wait_ready()
        q.create(settings.gemini.embedding_dimensions, PAYLOAD_INDEXES)
        points = [
            Point(
                id=point_id(registered.version_id, row["index"]),
                dense=vectors[i],
                sparse=doc_sparse_vector(docs[i], avgdl),
                payload={
                    # Written on every point and forced into every search. A
                    # point id is `uuid5(ns, f"{version_id}:{index}")` and the
                    # version id is now salted, so two customers holding the
                    # same file no longer collide — but a collision is not the
                    # same thing as authorization, and this is what a query
                    # actually filters on.
                    "tenant_id": registered.tenant_id,
                    "library_id": library_id,
                    "document_id": registered.document_id,
                    "version_id": registered.version_id,
                    # The graph's id for the same chunk, so a Qdrant hit can be
                    # expanded through the graph without a lookup table.
                    "chunk_id": make_chunk_id(registered.version_id, row["index"]),
                    "source_title": staged.title,
                    "chunk_index": row["index"],
                    "kind": row["kind"],
                    "chapter": row.get("chapter", ""),
                    "section": row.get("section", ""),
                    "breadcrumb": " > ".join(
                        p for p in (row.get("chapter"), row.get("section")) if p
                    ),
                    "text": row["text"],
                    "context": row.get("context", ""),
                    # Indexes the *corrected* byte stream, not the original file:
                    # correction changed the offsets.
                    "char_span": [row["char_from"], row["char_to"]],
                    "cell_ref": row.get("cell_ref", ""),
                },
            )
            for i, row in enumerate(rows)
        ]
        q.upsert(points)
        info = q.info()

    spend = _charge(
        run_id,
        Spend(
            stage="embedding",
            model=settings.gemini.embedding_model,
            input_tokens=usage.input_tokens,
            output_tokens=0,
            usd=price_for(settings.gemini.embedding_model, usage.input_tokens, 0),
        ),
    )
    log.info(
        "indexed %d points into %r (%d total in collection)",
        len(points), collection, info.points_count,
    )
    return Indexed(
        collection=collection,
        points=len(points),
        dimensions=settings.gemini.embedding_dimensions,
        spend=spend,
    )


# ---------------------------------------------------------------------------
# Semantic extraction
# ---------------------------------------------------------------------------

SEMANTIC_SYSTEM = """Eres un extractor de conocimiento sobre textos académicos en español.
Devuelves solo lo que el fragmento afirma explícitamente. No infieres, no completas
con conocimiento general y no inventas relaciones entre conceptos que el texto no
enuncia. Si el fragmento no contiene ninguna afirmación sustantiva, devuelves listas
vacías: eso es una respuesta correcta y frecuente.

Cada afirmación lleva además `cita`: el tramo del fragmento del que sale, copiado
carácter por carácter. No lo parafrasees, no lo recortes a mitad de una palabra, no
corrijas su ortografía ni su puntuación, y no lo compongas juntando trozos separados
del texto. Tiene que ser un tramo contiguo que aparezca tal cual en el fragmento,
porque el código lo busca dentro de él: una cita que no se encuentra se descarta, y la
afirmación se queda sin nada con que comprobarse.

Cada concepto lleva `descripcion`: una frase sobre qué es ese concepto *según este
fragmento*, no según lo que tú sepas de él. Es lo único que hace que el grafo
devuelva algo más que un nombre. Si el fragmento no dice qué es, la dejas vacía.

Cada afirmación puede llevar `relaciona`: un SEGUNDO concepto que el propio
fragmento pone en relación con `concepto`. Solo lo rellenas si el texto enuncia esa
relación — no si los dos conceptos simplemente aparecen cerca. Que dos ideas salgan
en el mismo párrafo no es que el documento las relacione, y una relación que el
texto no afirma es exactamente lo que no debes inventar. Si no hay segunda idea,
lo dejas vacío, que es el caso más frecuente.

Cada afirmación lleva también `estado`, que dice qué hace el documento con ella:
`afirma` si el documento la sostiene, `niega` si la rechaza o la refuta, y `atribuido`
si la reporta como de otro — una posición que cita, la defienda o no. La distinción
importa: un texto que expone la doctrina que va a rebatir la enuncia con las mismas
palabras que quien la sostiene, y sin este campo las dos quedan idénticas."""

SEMANTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "conceptos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "tipo": {"type": "string"},
                    "confianza": {"type": "number"},
                    # Free: the same call, a few more output tokens. Without it a
                    # graph traversal returns a bare name, which is most of why
                    # the concept layer is hard to read.
                    "descripcion": {"type": "string"},
                },
                "required": ["nombre", "confianza"],
            },
        },
        "afirmaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "concepto": {"type": "string"},
                    # The second concept, when the fragment states a relation
                    # between the two. Not required: most claims are about one
                    # idea, and demanding this would invite the model to invent
                    # the relation the prompt just told it not to.
                    "relaciona": {"type": "string"},
                    "confianza": {"type": "number"},
                    # The span of the chunk the claim came from, copied
                    # verbatim. Required, because the alternative is a model
                    # deciding per claim whether to supply the one field that
                    # makes the claim checkable.
                    "cita": {"type": "string"},
                    # Spanish on the wire, like the chunk `kind` values and for
                    # the same reason: these are stored and filtered on, and the
                    # UI is what localises them.
                    "estado": {
                        "type": "string",
                        "enum": ["afirma", "niega", "atribuido"],
                    },
                },
                "required": ["texto", "concepto", "confianza", "cita", "estado"],
            },
        },
    },
    "required": ["conceptos", "afirmaciones"],
}

#: Chunks per extraction call. Larger batches cost fewer calls but make it
#: harder to attribute a concept to the chunk that produced it, and an
#: unattributable relation cannot be used as evidence for a cited answer.
SEMANTIC_BATCH = 1


#: Condense a concept's accumulated descriptions into one. A Spanish port of
#: graphrag's `SUMMARIZE_PROMPT` (MIT), keeping its "resolve the contradictions"
#: instruction: two documents describing one concept differently is the normal
#: case in a library, not an error to paper over.
CONDENSE_SYSTEM = """Recibes un concepto y varias descripciones suyas, extraídas de
fragmentos distintos. Devuelves UNA descripción que recoja lo que dicen todas.

Reglas:
- No añades nada que no esté en las descripciones recibidas.
- Si se contradicen, lo dices en la descripción, en vez de elegir una y callar la
  otra.
- Escribes en tercera persona y nombras el concepto, para que la frase se entienda
  sola.
- Como máximo {words} palabras."""

#: The threshold in characters, which is the unit the text is actually in. This
#: worker has no tokenizer, so the conversion goes through the estimator's own
#: `CHARS_PER_TOKEN` rather than a second constant that could drift from it.
CONDENSE_CHARS = int(CONDENSE_TOKENS * CHARS_PER_TOKEN)


def _condense_descriptions(
    graph, adapter, concept_ids: list[str]
) -> tuple[int, int]:
    """Give each concept one description, paying only where it is needed.

    Returns `(written, paid_calls)`. Three outcomes per concept, in order of how
    often they happen: short enough to concatenate (free), long enough to condense
    (one call), and already rich enough to leave alone (free). The last one is
    what stops a concept mentioned by fifty chunks being re-summarised on every
    import.
    """
    rows = proj.read_concept_descriptions(graph, concept_ids)
    updates: list[dict] = []
    paid = 0

    for row in rows:
        raw = [str(d) for d in (row.get("raw") or []) if str(d).strip()]
        if not raw:
            continue
        joined = " ".join(raw)
        if len(joined) <= CONDENSE_CHARS:
            updates.append({"id": row["id"], "description": joined})
            continue
        if len(raw) > CONDENSE_SOURCE_CAP and row.get("description"):
            continue
        try:
            text = adapter.generate(
                json.dumps({"concepto": row["name"], "descripciones": raw},
                           ensure_ascii=False),
                system=CONDENSE_SYSTEM.format(words=CONDENSE_WORDS),
                stage="semantics",
            )
        except Exception as e:
            # The concatenation is a worse description, not a missing one, and
            # this step runs after everything else has already been projected.
            log.warning("could not condense %r: %s", row["name"], e)
            updates.append({"id": row["id"], "description": joined})
            continue
        paid += 1
        updates.append({"id": row["id"], "description": text.strip()})

    return proj.set_concept_descriptions(graph, updates), paid


#: Ask the model for what it missed, in the same conversation. Ported from
#: nano-graphrag's `entiti_continue_extraction` and graphrag's `CONTINUE_PROMPT`
#: (both MIT), which pair it with a second call asking "are there more? Y/N".
#:
#: That second call is folded into the schema here instead. Those projects need
#: it because their extraction returns delimited text; this one returns
#: structured output, so the model can be asked in the same breath — one call
#: per round rather than two, and nothing extra for the estimator to count.
GLEAN_PROMPT = """Se te escaparon conceptos y afirmaciones en la extracción anterior
del mismo fragmento.

Añade SOLO los que falten, con las mismas reglas: cita literal, estado, y nada que el
fragmento no enuncie. No repitas nada de lo que ya devolviste. Si no falta nada,
devuelves listas vacías, que es una respuesta correcta y frecuente.

`quedan` dice si después de esta respuesta todavía queda algo por extraer."""

SEMANTIC_GLEAN_SCHEMA = {
    "type": "object",
    "properties": {
        **SEMANTIC_SCHEMA["properties"],
        "quedan": {"type": "boolean"},
    },
    "required": [*SEMANTIC_SCHEMA["required"], "quedan"],
}


def _extract_passes(adapter, text: str, *, rounds: int) -> list[dict]:
    """One chunk's extraction, plus up to `rounds` gleaning passes over it.

    Each pass runs *in the same conversation*, which is what makes a second pass
    cheaper than a second independent extraction: the model can be told to add
    what it missed instead of being told not to repeat itself. It stops early
    when the model says nothing is left.

    Off by default (`Gemini.max_gleaning`). The two projects this is ported from
    both default to one pass; this pipeline has no measurement of its own yet,
    and its last measurement of this stage went against the intuition — semantics
    came out better with reasoning off, not on.
    """
    import json as _json

    first = adapter.generate_json(
        text, system=SEMANTIC_SYSTEM, schema=SEMANTIC_SCHEMA, stage="semantics"
    )
    passes = [first]
    if rounds <= 0:
        return passes

    history = [(text, _json.dumps(first, ensure_ascii=False))]
    for _ in range(rounds):
        more = adapter.generate_json(
            GLEAN_PROMPT, system=SEMANTIC_SYSTEM, schema=SEMANTIC_GLEAN_SCHEMA,
            stage="semantics", history=history,
        )
        passes.append(more)
        if not more.get("quedan"):
            break
        history.append((GLEAN_PROMPT, _json.dumps(more, ensure_ascii=False)))
    return passes


#: What the document does with a claim. Ported from graphrag's
#: `Claim Status: **TRUE**, **FALSE**, or **SUSPECTED**` (MIT), renamed for what
#: this corpus actually needs: the interesting third case here is not "unverified"
#: but "reported as somebody else's", which is how a theology text handles a
#: position it is about to reject.
CLAIM_STATES = frozenset({"afirma", "niega", "atribuido"})


def _status(value: object) -> str | None:
    """Keep a state the schema allows, and None for anything else.

    None rather than a default of `afirma`, because there is no safe default: a
    claim the document merely reports would be promoted to one it asserts, which
    is the exact confusion this field exists to remove. The read side renders the
    absence as `sin_estado`.
    """
    text = str(value or "").strip().lower()
    return text if text in CLAIM_STATES else None


def _locate_quote(quote: str, text: str, offset: int) -> tuple[str, int, int] | None:
    """Find a model-supplied quote inside the chunk it claims to come from.

    Returns the quote *as the document spells it* together with its absolute
    span, or None when the quote is not in the text at all.

    The idea of asking for the quote is ported from the `Claim Source Text`
    field of microsoft/graphrag's claim-extraction prompt (MIT). The checking is
    not: graphrag asks for the quote and nothing ever verifies it, which leaves
    the same hole this function exists to close — a claim nobody can check
    against the source still looks exactly like evidence.

    Matching tolerates differences in whitespace and nothing else. A model asked
    for a verbatim span reliably reproduces the words and unreliably reproduces
    the line breaks the extractor put between them, so a literal `in` test fails
    on quotes that are in fact present. Anything beyond whitespace — a fixed
    accent, a normalised quotation mark, a dropped clause — is a quote the
    document does not contain, and it has to fail.

    The span indexes the *corrected* byte stream, like every other offset this
    pipeline stores (see `char_span` in `index_chunks`), not the original file.
    Correction changes the text's length, so a byte-exact pointer into the PDF is
    not something this function can produce.
    """
    tokens = quote.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    match = re.search(pattern, text)
    if match is None:
        return None
    return match.group(0), offset + match.start(), offset + match.end()


@activity.defn(name="extract_semantics")
async def extract_semantics(
    run_id: str,
    registered: Registered,
    chunked: Chunked,
    options: StageOptions | None = None,
) -> Semantics:
    """Ask the model for concepts and claims, one chunk at a time.

    One chunk per call is deliberate. Batching would be cheaper, but every
    relation this produces must carry the `source_chunk_id` that lets a person
    check it — and a model given ten chunks reliably attributes claims to the
    wrong one. A relation nobody can verify is worse than no relation, because
    it still looks like evidence.
    """
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunked.chunks))

    # Concept ids are salted with it: two customers who both talk about "Dios"
    # must not share a node, because `description_raw` accumulates the text of
    # every chunk that mentions it.
    tenant = registered.tenant_id
    adapter = VertexAdapter(_provider())
    concepts: dict[str, dict] = {}
    claims: list[dict] = []
    edges: list[proj.SemanticEdge] = []
    #: Claims whose quote was not in the chunk. Counted rather than only logged:
    #: a claim with no verifiable quote is a *degraded* claim, and the number of
    #: them is the difference between a degradation the user can see and one that
    #: hides behind a count of claims that all look equally good.
    unverified = 0

    for row in rows:
        chunk = make_chunk_id(registered.version_id, row["index"])
        try:
            passes = _extract_passes(
                adapter, row["text"], rounds=settings.gemini.max_gleaning
            )
        except Exception as e:
            # One unparseable chunk must not lose the whole document's
            # semantics; structure and citations are already projected and the
            # document stays browsable either way.
            log.warning("semantic extraction failed for chunk %s: %s", chunk, e)
            continue

        # A gleaning pass is asked for what the previous ones *missed*, so a
        # claim it returns on ground already covered is by construction not new.
        # Deduplicating on the claim id — the same key the graph MERGEs on — and
        # on the verified span keeps a restatement from becoming a second node.
        seen_ids: set[str] = set()
        seen_spans: set[tuple[int, int]] = set()

        for parsed in passes:
            for item in parsed.get("conceptos", []) or []:
                name = (item.get("nombre") or "").strip()
                if not name:
                    continue
                confidence = float(item.get("confianza", 0.0))
                entry = concepts.setdefault(
                    name, {"name": name, "type": item.get("tipo"), "descriptions": []}
                )
                # Deduplicated as it accumulates. Two chunks often describe a
                # concept in the same words, and the condensation step is charged
                # by length.
                described = (item.get("descripcion") or "").strip()
                if described and described not in entry["descriptions"]:
                    entry["descriptions"].append(described)
                edges.append(
                    proj.SemanticEdge(
                        type="MENTIONS",
                        source_id=chunk,
                        target_id=make_concept_id(name, tenant),
                        confidence=confidence,
                        extractor_model=settings.gemini.model,
                        source_chunk_id=chunk,
                    )
                )

            for item in parsed.get("afirmaciones", []) or []:
                text = (item.get("texto") or "").strip()
                about = (item.get("concepto") or "").strip()
                if not text or not about:
                    continue
                confidence = float(item.get("confianza", 0.0))
                concepts.setdefault(
                    about, {"name": about, "type": None, "descriptions": []}
                )
                claim = {
                    "text": text,
                    "confidence": confidence,
                    "source_chunk_id": chunk,
                    "status": _status(item.get("estado")),
                }
                # A quote that is not in the chunk costs the claim its quote, not
                # its existence. The claim is still a reading of a chunk a person
                # can open; what it loses is the span that would have taken them
                # to the exact sentence.
                located = _locate_quote(
                    str(item.get("cita") or ""), row["text"], int(row["char_from"])
                )
                if located is not None:
                    verbatim, start, end = located
                    claim["quote"] = verbatim
                    claim["quote_char_start"] = start
                    claim["quote_char_end"] = end

                # `claim_id` is the key the graph MERGEs on, so deduplicating on
                # it here means a restatement from a later pass converges instead
                # of becoming a second node. The span is checked too: a gleaning
                # pass citing ground an earlier one already used is restating it,
                # since gleaning is asked only for what was missed.
                cid = proj.claim_id(chunk, text)
                span = (
                    (claim["quote_char_start"], claim["quote_char_end"])
                    if located is not None else None
                )
                if cid in seen_ids or (span is not None and span in seen_spans):
                    continue
                seen_ids.add(cid)
                if span is not None:
                    seen_spans.add(span)

                # Counted *after* the dedup, so the number refers to the claims
                # actually stored. Counting it above let a duplicate with an
                # unlocatable quote be tallied and then dropped, which is how a
                # verified count starts exceeding what it counts.
                if located is None:
                    unverified += 1
                claims.append(claim)
                edges.append(
                    proj.SemanticEdge(
                        type="ABOUT",
                        source_id=proj.claim_id(chunk, text),
                        target_id=make_concept_id(about, tenant),
                        confidence=confidence,
                        extractor_model=settings.gemini.model,
                        source_chunk_id=chunk,
                    )
                )

                # The second concept, when the fragment related two. This is the
                # graph's only concept-to-concept path: everything else joins two
                # concepts through a chunk that mentioned both, which is
                # co-occurrence rather than anything the document said.
                #
                # Self-relations are dropped — a claim relating a concept to
                # itself is the model restating `concepto`, and it would add a
                # loop the traversal has to filter out on every read.
                related = (item.get("relaciona") or "").strip()
                if related and make_concept_id(related, tenant) != make_concept_id(about, tenant):
                    concepts.setdefault(
                        related, {"name": related, "type": None, "descriptions": []}
                    )
                    edges.append(
                        proj.SemanticEdge(
                            type="INVOLVES",
                            source_id=proj.claim_id(chunk, text),
                            target_id=make_concept_id(related, tenant),
                            confidence=confidence,
                            extractor_model=settings.gemini.model,
                            source_chunk_id=chunk,
                        )
                    )

    condense_spend: Spend | None = None
    with Graph(settings.memgraph_url) as graph:
        graph.ensure_schema()
        proj.project_concepts(graph, list(concepts.values()), tenant=tenant)
        proj.project_claims(graph, claims, tenant=tenant)
        written = proj.project_semantic_edges(graph, edges)

        # After projection, so the descriptions being condensed include this
        # run's. Its own adapter, so its tokens land in their own ledger row
        # rather than inflating the per-chunk extraction they are not part of.
        if options is not None and options.condense_descriptions:
            condenser = VertexAdapter(_provider())
            names = [make_concept_id(c["name"], tenant) for c in concepts.values()]
            described, paid_calls = _condense_descriptions(graph, condenser, names)
            log.info(
                "condensed %d concept description(s), %d of them paid for",
                described, paid_calls,
            )
            condense_spend = _charge(
                run_id,
                Spend(
                    stage="semantics-condense",
                    model=settings.gemini.model,
                    input_tokens=condenser.usage.input_tokens,
                    output_tokens=condenser.usage.output_tokens,
                    usd=price_for(
                        settings.gemini.model,
                        condenser.usage.input_tokens,
                        condenser.usage.output_tokens,
                    ),
                ),
            )

    # Persisted *after* projecting, so the artifact only ever describes a graph
    # write that succeeded. This is the most expensive output of the pipeline to
    # reproduce — one generation call per chunk — and until now it existed
    # nowhere but in Memgraph, so dropping the graph meant paying for it again.
    _record(
        run_id,
        "semantics",
        store.write_json(
            "semantics",
            {
                "extractor_model": settings.gemini.model,
                "concepts": list(concepts.values()),
                "claims": claims,
                "edges": [asdict(e) for e in edges],
            },
        ),
    )

    if unverified:
        log.warning(
            "%d of %d claim(s) quoted text that is not in their chunk; they are "
            "stored without a span and cannot be opened at the sentence",
            unverified, len(claims),
        )

    spend = _charge(
        run_id,
        Spend(
            stage="semantics",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    return Semantics(
        concepts=len(concepts),
        claims=len(claims),
        claims_verified=len(claims) - unverified,
        edges=written,
        spend=spend,
        condense_spend=condense_spend,
    )
