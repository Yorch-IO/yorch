"""The free ingest stages, against real files and the real engine.

No mocking of docagent here: the point of these stages is that they produce a
faithful preview for free, and a mocked extractor would test nothing about that.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from brainworker import config
from brainworker.activities import ingest as act
from brainworker.pipeline import IngestRequest, Preview, Registered, StageOptions

TEXTO = """LIBRO PRIMERO

DEL CONOCIMIENTO DE DIOS

Casi toda la suma de nuestra sabiduría, que de veras se deba tener por
verdadera y sólida sabiduría, consiste en dos puntos: el conocimiento que el
hombre debe tener de Dios, y el que debe tener de sí mismo.

Porque, en primer lugar, ninguno se puede considerar a sí mismo sin que
inmediatamente vuelva sus sentidos a la consideración de Dios.

PREGUNTAS

1. ¿En qué consiste la suma de nuestra sabiduría?

2. ¿Por qué no puede el hombre conocerse sin conocer a Dios?
"""


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A workspace with no catalog behind it.

    The database is pointed at a closed port on purpose. Artifact recording is
    best-effort bookkeeping, and these tests are about what the stages produce —
    so running them with the catalog down both keeps them fast (connection
    refused is instant, a timeout is not) and continuously proves the stages do
    not depend on it. `test_a_stage_still_produces_its_artifact_when_the_catalog_is_down`
    states that explicitly.
    """
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def libro(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "institucion.txt"
    path.write_text(TEXTO, encoding="utf-8")
    return path


def request_for(path: pathlib.Path, **kw) -> IngestRequest:
    return IngestRequest(
        library_id="lib_1",
        source_path=str(path),
        source_key=f"libros/{path.name}",
        **kw,
    )


# -- staging ----------------------------------------------------------------


async def test_staging_identifies_the_file_without_parsing_it(libro: pathlib.Path, workspace):
    staged = await act.stage_source(request_for(libro))
    assert staged.byte_size == libro.stat().st_size
    assert staged.fmt == "txt"
    assert staged.extractor == "plain"
    assert staged.title == "institucion"
    assert len(staged.content_sha256) == 64


async def test_the_same_bytes_hash_the_same_from_two_paths(tmp_path: pathlib.Path, workspace):
    """Version identity is the content, so this equality is load-bearing."""
    a, b = tmp_path / "a.txt", tmp_path / "copias" / "b.txt"
    b.parent.mkdir()
    a.write_text(TEXTO, encoding="utf-8")
    b.write_text(TEXTO, encoding="utf-8")

    first = await act.stage_source(request_for(a))
    second = await act.stage_source(request_for(b))
    assert first.content_sha256 == second.content_sha256


async def test_an_unsupported_format_is_refused_by_name(tmp_path: pathlib.Path, workspace):
    """Better here than three activities deep, where the error names an extractor."""
    path = tmp_path / "libro.epub"
    path.write_bytes(b"not supported")
    with pytest.raises(ValueError, match="unsupported format"):
        await act.stage_source(request_for(path))


async def test_a_missing_file_says_so(tmp_path: pathlib.Path, workspace):
    with pytest.raises(FileNotFoundError):
        await act.stage_source(request_for(tmp_path / "ausente.txt"))


async def test_the_title_comes_from_the_key_not_from_where_the_file_landed(
    tmp_path: pathlib.Path, workspace
):
    """The staged filename is chosen by the server, so it is never a name.

    In cloud mode `/uploads` stores the file as `randomUUID() + suffix`,
    deliberately: a name arriving over HTTP must not become a path segment. The
    title used to come from that path's stem, which labelled every document
    imported through the paid plane with a UUID — seen on screen on 2026-08-31 as
    "1f9c2599-2ffe-43f3-a386-eddd928c82c5" where the book's name belonged.
    """
    stored = tmp_path / "1f9c2599-2ffe-43f3-a386-eddd928c82c5.txt"
    stored.write_text(TEXTO, encoding="utf-8")

    request = IngestRequest(
        library_id="lib_1",
        source_path=str(stored),
        source_key="01_RetoDeDios_INT-S.pdf.corrected.txt",
    )
    staged = await act.stage_source(request)
    assert staged.title == "01_RetoDeDios_INT-S.pdf.corrected"

    # An explicit title still wins: this is a fallback, not an override.
    titled = await act.stage_source(
        IngestRequest(
            library_id="lib_1",
            source_path=str(stored),
            source_key="01_RetoDeDios_INT-S.pdf.corrected.txt",
            title="El reto de Dios",
        )
    )
    assert titled.title == "El reto de Dios"


# -- extraction and preview -------------------------------------------------


async def test_extraction_writes_both_the_text_and_its_evidence(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The evidence is needed by the collision check; re-extracting to recover it
    would cost minutes on a 900-page PDF."""
    extraction = await act.extract_text(request_for(libro), "run_1")
    assert extraction.text.kind == "raw_text"
    assert extraction.evidence.kind == "evidence"
    assert extraction.structured is False
    assert (workspace / extraction.text.path).is_file()
    assert (workspace / extraction.evidence.path).is_file()


async def test_the_preview_chunks_for_free_and_reports_its_kinds(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions(correct=False))

    assert preview.chunk_count > 0
    assert preview.characters == len(TEXTO.encode("utf-8"))
    kinds = {k.kind: k.count for k in preview.kinds}
    assert sum(kinds.values()) == preview.chunk_count
    # The engine keeps chunk kinds in Spanish on the wire; they are stored in
    # Qdrant payloads and used in filters, so renaming breaks every collection.
    assert set(kinds) <= {
        "cuerpo", "preguntas", "nota", "tabla_fila", "tabla_resumen", "diapositiva",
    }


async def test_the_preview_is_final_only_when_correction_is_off(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await act.extract_text(request_for(libro), "run_1")

    off = await act.preview_chunks("run_1", extraction, StageOptions(correct=False))
    assert off.chunks_are_final is True

    on = await act.preview_chunks("run_1", extraction, StageOptions(correct=True))
    assert on.chunks_are_final is False
    assert any("longitud" in w for w in on.warnings)


async def test_an_empty_extraction_warns_about_ocr_rather_than_succeeding_quietly(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """A scanned PDF with no text layer extracts to nothing. Reporting zero
    chunks with no explanation sends the user looking in the wrong place."""
    empty = tmp_path / "escaneado.txt"
    empty.write_text("", encoding="utf-8")
    extraction = await act.extract_text(request_for(empty), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions(correct=False))
    assert preview.chunk_count == 0
    assert any("OCR" in w for w in preview.warnings)


async def test_chunks_land_in_the_artifact_store_not_in_the_payload(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """Temporal payloads cap around 2 MB and history persists for the whole
    retention period; a book's chunks exceed that on the large documents and
    would permanently store a copy of every document on the small ones."""
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    assert preview.chunks.rows == preview.chunk_count
    assert (workspace / preview.chunks.path).is_file()


# -- estimation -------------------------------------------------------------


async def test_the_estimate_covers_exactly_the_stages_that_were_switched_on(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions())

    everything = await act.estimate_cost(
        preview,
        StageOptions(correct=True, embed=True, extract_semantics=True, generate_evalset=True),
    )
    assert [s.stage for s in everything.stages] == [
        "profile", "correction", "embedding", "semantics", "evalset", "evaluation",
    ]

    # `evaluation` is the query embeddings the measurement itself pays for. It
    # only appears when there is an index to measure: asking questions of a
    # collection this run never wrote would score somebody else's document.
    no_index = await act.estimate_cost(
        preview,
        StageOptions(
            correct=False, embed=False, extract_semantics=False,
            learn_profile=False, generate_evalset=True,
        ),
    )
    assert [s.stage for s in no_index.stages] == ["evalset"]

    embedding_only = await act.estimate_cost(
        preview,
        StageOptions(correct=False, embed=True, extract_semantics=False, learn_profile=False),
    )
    assert [s.stage for s in embedding_only.stages] == ["embedding"]

    # The book itself gets no line: it reads a file and writes one. What can
    # spend is the single call that reads a title and an author off the opening
    # pages, and a stage absent from `COST_STAGES` renders `cost: null` in the
    # audit — which is what distinguishes "this was free" from "the charge was
    # lost". A `$0.000000` row here would say the second.
    book = await act.estimate_cost(
        preview,
        StageOptions(
            correct=False, embed=False, extract_semantics=False,
            learn_profile=False, build_epub=True,
        ),
    )
    assert [s.stage for s in book.stages] == ["epub-metadata"]
    # Compared on tokens, not dollars: no price is configured for these models.
    assert sum(s.input_tokens + s.output_tokens for s in embedding_only.stages) < sum(
        s.input_tokens + s.output_tokens for s in everything.stages
    )


async def test_the_estimate_names_the_models_it_will_actually_use(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """Gemini Enterprise no longer serves `gemini-embedding-001`, and Gemini 3.x
    exists only on the global endpoint. An estimate naming a model the endpoint
    will refuse is worse than no estimate."""
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(preview, StageOptions())

    by_stage = {s.stage: s.model for s in estimate.stages}
    from brainworker.config import Gemini

    defaults = Gemini()
    assert by_stage["correction"] == defaults.model
    assert by_stage["embedding"] == defaults.embedding_model


async def test_correction_moves_the_most_tokens(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The gate sits before correction because correction dominates the bill.

    That ordering came from a measured run on `gemini-2.5-flash` — correction
    $0.0334 against embedding $0.0033. This test stays on the token side rather
    than the dollar side so it keeps meaning across a model change:
    correction is charged on input *and* output, embedding on input alone. The
    dollar dominance is currently an assumption inherited from the older model,
    and stating that is the point of this docstring.
    """
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(preview, StageOptions())

    by_stage = {s.stage: s for s in estimate.stages}
    assert by_stage["embedding"].output_tokens == 0, "embeddings have no output charge"
    assert by_stage["correction"].output_tokens > 0
    assert (
        by_stage["correction"].input_tokens + by_stage["correction"].output_tokens
        > by_stage["embedding"].input_tokens + by_stage["embedding"].output_tokens
    )


async def test_every_model_the_app_uses_has_a_price(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """A stage priced at None renders as "sin precio" — correct, but useless.

    The table was deliberately empty through the Gemini Enterprise migration
    rather than carrying `gemini-2.5-flash` rates that no longer applied. Prices
    were sourced on 2026-08-20, so every configured model should now resolve.
    """
    extraction = await act.extract_text(request_for(libro), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(preview, StageOptions())

    assert estimate.unpriced_stages == []
    assert estimate.total_usd is not None and estimate.total_usd > 0
    assert all(s.usd is not None for s in estimate.stages)
    assert "segunda mano" in estimate.price_source, (
        "the caveat travels with the figure: the counts are ours, the rates are not"
    )


def test_a_price_is_only_applied_to_the_model_it_was_recorded_for():
    """Keyed by exact id, never by family prefix.

    A prefix match would have applied `gemini-embedding-001`'s $0.15 to
    `gemini-embedding-2` at $0.20, and the 2.5-flash rates to every 3.x model —
    silently, which is the failure mode that matters.
    """
    assert act.price_for("gemini-3.5-flash", 1_000_000, 0) == pytest.approx(1.50)
    assert act.price_for("gemini-3.5-flash", 0, 1_000_000) == pytest.approx(9.00)
    assert act.price_for("gemini-embedding-2", 1_000_000, 0) == pytest.approx(0.20)
    # Embeddings have no output charge.
    assert act.price_for("gemini-embedding-2", 0, 1_000_000) == pytest.approx(0.0)
    # An unknown id is unpriced, not free.
    assert act.price_for("gemini-4-flash", 1_000_000, 1_000_000) is None
    assert act.price_for("gemini-3.5-flash-lite", 1_000_000, 0) is None


def test_prices_are_not_shared_between_neighbouring_model_ids():
    """The two flash models differ only on output, by 17%."""
    prices = act.PRICES_PER_MILLION
    assert prices["gemini-3.5-flash"] != prices["gemini-3.6-flash"]
    assert prices["gemini-embedding-2"] != prices["gemini-embedding-001"]


def test_every_configured_model_is_priced():
    """What the old emptiness guard became.

    It asserted `PRICES_PER_MILLION == {}` so that reusing the engine's
    2.5-flash constants had to be a deliberate act. Now that real prices are
    recorded, the useful guard is the inverse: changing a default model in
    `config.Gemini` without pricing it must fail here, rather than at a gate
    quietly showing "sin precio".
    """
    from brainworker.config import Gemini

    defaults = Gemini()
    for model in (defaults.model, defaults.embedding_model):
        assert model in act.PRICES_PER_MILLION, (
            f"{model} is the configured default but has no recorded price"
        )


async def test_the_estimate_errs_toward_over_reporting(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """A user who approved $0.03 and was billed $0.05 has been misled; the
    reverse has not. So the chars-per-token divisor takes the low end."""
    assert act.CHARS_PER_TOKEN <= 3.6


async def test_a_document_with_unnumbered_headings_says_it_will_have_no_outline(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """`heading_level` only recognises numbered headings without a learned
    profile, so "LIBRO PRIMERO" is prose to it. The document indexes and cites
    correctly but has no table of contents, and the gate is where the user
    should find that out — not an empty Explore screen."""
    path = tmp_path / "sin-numerar.txt"
    path.write_text(TEXTO, encoding="utf-8")
    extraction = await act.extract_text(request_for(path), "run_1")
    preview = await act.preview_chunks("run_1", extraction, StageOptions(correct=False))

    assert preview.chunk_count > 0
    assert any("sección" in w for w in preview.warnings)


async def test_a_numbered_outline_produces_no_such_warning(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    numbered = """1. Del conocimiento de Dios

Casi toda la suma de nuestra sabiduría consiste en dos puntos, el conocimiento
que el hombre debe tener de Dios y el que debe tener de sí mismo.

1.1 Qué cosa sea conocer a Dios

Entiendo por conocimiento de Dios aquel con el que no solamente concebimos que
hay algún Dios, sino también comprendemos lo que nos conviene saber de Él.
"""
    path = tmp_path / "numerado.txt"
    path.write_text(numbered, encoding="utf-8")
    extraction = await act.extract_text(request_for(path), "run_2")
    preview = await act.preview_chunks("run_2", extraction, StageOptions(correct=False))
    assert not any("sección" in w for w in preview.warnings)


# -- section hierarchy ------------------------------------------------------


def test_ordinals_are_counted_per_parent_not_globally():
    """Counting globally is the obvious shortcut and it is wrong twice over.

    The first sub-heading of chapter 1 comes out as `2.1` rather than `1.1`, and
    its implied parent `(2,)` names a section that does not exist — so the
    CONTAINS edge is never written and a document with headings still has no
    navigable outline.
    """
    rows = [
        {"chapter": "1. Del fin principal", "section": ""},
        {"chapter": "1. Del fin principal", "section": "1.1 De la regla"},
        {"chapter": "2. De lo que enseñan", "section": ""},
    ]
    _, paths = act.section_tree(rows)
    assert paths[("1. Del fin principal",)] == (1,)
    assert paths[("1. Del fin principal", "1.1 De la regla")] == (1, 1)
    assert paths[("2. De lo que enseñan",)] == (2,)


def test_every_section_has_an_existing_parent():
    """The precondition the CONTAINS edge depends on."""
    rows = [
        {"chapter": "A", "section": "A.1 > A.1.a"},
        {"chapter": "B", "section": "B.1"},
    ]
    _, paths = act.section_tree(rows)
    known = set(paths.values())
    for path in paths.values():
        if len(path) > 1:
            assert path[:-1] in known, f"{path} has no parent"


def test_deeper_levels_are_split_out_of_the_joined_section_string():
    """The engine joins levels below the chapter with " > " into one field."""
    nodes, paths = act.section_tree([{"chapter": "A", "section": "B > C"}])
    assert paths[("A", "B", "C")] == (1, 1, 1)
    assert [n.level for n in nodes.values()] == [1, 2, 3]


def test_repeated_titles_under_different_parents_stay_distinct():
    """"Introducción" appears once per part in most of this corpus."""
    rows = [
        {"chapter": "Parte I", "section": "Introducción"},
        {"chapter": "Parte II", "section": "Introducción"},
    ]
    _, paths = act.section_tree(rows)
    assert paths[("Parte I", "Introducción")] != paths[("Parte II", "Introducción")]


def test_a_chunk_before_any_heading_has_no_section():
    _, paths = act.section_tree([{"chapter": "", "section": ""}])
    assert paths == {}


async def test_a_stage_still_produces_its_artifact_when_the_catalog_is_down(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """Recording an artifact must never fail the stage that produced it.

    An artifact the catalog forgot is recoverable — the file is on disk and the
    reference is in the workflow's history. A re-extracted 900-page PDF is not
    free, so a bookkeeping failure must not cost one.
    """
    extraction = await act.extract_text(request_for(libro), "run_down")
    assert (workspace / extraction.text.path).is_file()

    preview = await act.preview_chunks(
        "run_down", extraction, StageOptions(correct=False)
    )
    assert preview.chunk_count > 0
    assert (workspace / preview.chunks.path).is_file()


# -- calibration against a real run -----------------------------------------
# -- calibration against real runs ------------------------------------------

#: What one real page actually consumed, measured 2026-08-20 against Gemini
#: Enterprise on `gemini-3.6-flash` / `gemini-embedding-2`:
#: `docaget/libros/Semana 2 Cómo puedo dejar el hábito…pdf`, ~1918 characters,
#: 3 chunks, correction + embedding + semantics.
#:
#: Recorded in both reasoning modes because they are different products
#: commercially. `output` includes thinking tokens, which are billed at the
#: output rate — counting only the visible half under-reported the first
#: measurement by roughly 4x.
CHARACTERS = 1918
CHUNKS = 3

MEASURED_THINKING = {
    "correction": {"input": 979, "output": 3184},
    "embedding": {"input": 433, "output": 0},
    "semantics": {"input": 1413, "output": 2682},
}

MEASURED_NO_THINKING = {
    "correction": {"input": 1019, "output": 560},
    "embedding": {"input": 433, "output": 0},
    "semantics": {"input": 1412, "output": 1015},
}


def _preview(characters: int = CHARACTERS, chunk_count: int = CHUNKS) -> Preview:
    from brainworker.artifacts import ArtifactRef

    ref = ArtifactRef(kind="raw_text", path="runs/r/raw.txt", sha256="a" * 64, bytes=1)
    return Preview(
        text=ref, chunks=ref, chunk_count=chunk_count, kinds=[],
        characters=characters, chunks_are_final=True,
    )


#: Any non-zero budget makes `thinking_for` report reasoning as enabled, which
#: is the only thing the estimator branches on.
ON_BUDGET = "512"


@pytest.fixture
def thinking(monkeypatch: pytest.MonkeyPatch):
    """Switch reasoning on or off for the estimator under test.

    Sets every stage, not just the global fallback. The estimator resolves the
    budget per stage, because the shipped configuration has the fallback on and
    three individual stages off — so setting only the fallback would leave those
    three unchanged and measure nothing.
    """

    def set_budget(value: str | None):
        # "On" has to be stated rather than left unset. Unsetting everything
        # gives the *shipped* configuration, which turns reasoning off for
        # correction, semantics and profile — so a test that compared "unset"
        # against "0" would be comparing off against off and would pass while
        # measuring nothing.
        budget = ON_BUDGET if value is None else value
        for name in ["BRAIN_THINKING_BUDGET"] + [
            f"BRAIN_THINKING_{s.upper()}" for s in config.THINKING_STAGES
        ]:
            monkeypatch.setenv(name, budget)

    return set_budget


@pytest.mark.parametrize(
    "budget,measured",
    [(None, MEASURED_THINKING), ("0", MEASURED_NO_THINKING)],
    ids=["reasoning-on", "reasoning-off"],
)
async def test_the_estimate_over_reports_every_measured_stage(
    workspace: pathlib.Path, thinking, budget, measured
):
    """The direction is the whole contract, and two real runs broke it in turn.

    First, estimating from character count alone ignored the per-*call* cost of
    the system instruction, schema and JSON envelope. Then, counting only
    `candidates_token_count` ignored reasoning tokens — billed at the output rate
    and roughly 85% of correction's output on a real page.

    A user who approved a smaller number than they were billed has been misled.
    The reverse has not, which is why this asserts `>=` and never equality.

    **Bound to the high end of the range**, which is the whole reason the range
    exists: this rule and `test_the_estimate_stays_within_reach_of_the_measurement`
    could not both hold on one number, so each now binds a different end.
    """
    thinking(budget)
    estimate = await act.estimate_cost(_preview(), StageOptions())
    by_stage = {s.stage: s for s in estimate.stages}

    for stage, m in measured.items():
        got = by_stage[stage]
        assert got.input_tokens >= m["input"], (
            f"{stage}: estimated {got.input_tokens} input tokens against a real "
            f"{m['input']} — the estimate must never undershoot"
        )
        assert got.output_tokens_high >= m["output"], (
            f"{stage}: estimated at most {got.output_tokens_high} output tokens "
            f"against a real {m['output']}"
        )


@pytest.mark.parametrize(
    "budget,measured",
    [(None, MEASURED_THINKING), ("0", MEASURED_NO_THINKING)],
    ids=["reasoning-on", "reasoning-off"],
)
async def test_the_estimate_stays_within_reach_of_the_measurement(
    workspace: pathlib.Path, thinking, budget, measured
):
    """Over-reporting is required; over-reporting wildly is its own failure.

    An estimate several times the real cost pushes a user to decline work that
    was affordable — the same harm mirrored.

    **Bound to the low end**, which is the figure that describes a typical
    document. The high end is allowed past 2x here: it is a ceiling, and the
    caller reads it as one.
    """
    thinking(budget)
    estimate = await act.estimate_cost(_preview(), StageOptions())
    by_stage = {s.stage: s for s in estimate.stages}

    for stage, m in measured.items():
        got = by_stage[stage]
        assert got.input_tokens <= m["input"] * 2
        if m["output"]:
            assert got.output_tokens <= m["output"] * 2


async def test_reasoning_is_the_largest_single_cost_lever(
    workspace: pathlib.Path, thinking
):
    """Measured on one page: $0.047584 with reasoning against $0.015459 without,
    a 68% saving — and correction found *more* to fix (3 paragraphs against 2)
    while extraction returned more concepts and claims.

    "More" is not verified to mean "better", so the default is left at the
    model's own. What is not in doubt is the price of the choice, and an estimate
    that ignored it would hide the biggest number on the gate.
    """
    thinking(None)
    with_reasoning = await act.estimate_cost(_preview(), StageOptions())
    thinking("0")
    without = await act.estimate_cost(_preview(), StageOptions())

    assert with_reasoning.total_usd > without.total_usd * 2, (
        "reasoning must visibly change the estimate, or the gate hides it"
    )
    # Embedding never reasons.
    assert (
        next(s for s in with_reasoning.stages if s.stage == "embedding").output_tokens
        == next(s for s in without.stages if s.stage == "embedding").output_tokens
        == 0
    )


async def test_prompt_overhead_is_counted_per_call_not_per_document(
    workspace: pathlib.Path,
):
    """Semantic extraction runs one call per chunk, so its overhead multiplies.

    A document with ten times the chunks pays ten times the system prompt, and an
    estimate charging it once would drift further the larger the document.
    """
    few = await act.estimate_cost(_preview(1514, 2), StageOptions(extract_semantics=True))
    many = await act.estimate_cost(_preview(1514, 20), StageOptions(extract_semantics=True))

    few_semantics = next(s for s in few.stages if s.stage == "semantics")
    many_semantics = next(s for s in many.stages if s.stage == "semantics")

    assert many_semantics.input_tokens - few_semantics.input_tokens == pytest.approx(
        18 * act.SEMANTICS_CALL_OVERHEAD
    )
    assert many_semantics.output_tokens > few_semantics.output_tokens


async def test_embedding_carries_no_prompt_overhead(workspace: pathlib.Path):
    """It has no system instruction, no schema and no reasoning — measured 433
    tokens for 1918 characters, i.e. the text alone."""
    estimate = await act.estimate_cost(_preview(), StageOptions())
    embedding = next(s for s in estimate.stages if s.stage == "embedding")
    assert embedding.input_tokens == int(CHARACTERS / act.CHARS_PER_TOKEN)


@pytest.mark.parametrize(
    "budget,measured",
    [(None, MEASURED_THINKING), ("0", MEASURED_NO_THINKING)],
    ids=["reasoning-on", "reasoning-off"],
)
async def test_the_estimated_bill_over_reports_the_measured_one(
    workspace: pathlib.Path, thinking, budget, measured
):
    """The same contract, in the unit the user actually reads."""
    thinking(budget)
    estimate = await act.estimate_cost(_preview(), StageOptions())
    by_stage = {s.stage: s for s in estimate.stages}

    real_total = 0.0
    for stage, m in measured.items():
        got = by_stage[stage]
        real = act.price_for(got.model, m["input"], m["output"])
        assert real is not None
        real_total += real
        assert got.usd >= real, f"{stage}: estimated {got.usd} against a real {real}"

    # Summed over the *measured* stages only. Rule learning is real spend with
    # no measurement behind it yet, so including it here would compare an
    # estimate of five stages against a bill for three and report the estimator
    # as wildly over when it is not.
    estimated_total = sum(by_stage[stage].usd for stage in measured)
    assert estimated_total >= real_total
    assert estimated_total <= real_total * 2, "over-reporting, but not wildly"


def test_correction_dominates_embedding_by_two_orders_of_magnitude():
    """The measurement the gate's position rests on, re-verified on the new models.

    On `gemini-2.5-flash` the ratio was ~10x (correction $0.0334 against
    embedding $0.0033). Against `gemini-embedding-2` it is now far wider:
    generation output got much more expensive while embeddings stayed cheap. The
    gate belongs before correction more firmly than when that call was made.

    Priced at the *configured* model, so switching flash models keeps this
    honest rather than pinning it to the one it was first measured on.
    """
    from brainworker.config import Gemini

    defaults = Gemini()
    m = MEASURED_NO_THINKING
    correction = act.price_for(
        defaults.model, m["correction"]["input"], m["correction"]["output"]
    )
    embedding = act.price_for(defaults.embedding_model, m["embedding"]["input"], 0)
    assert correction / embedding > 50


def test_which_stage_costs_most_depends_on_the_reasoning_mode():
    """A design premise that turns out to be conditional, which is worth pinning.

    The plan said correction dominates — measured on `gemini-2.5-flash`, before
    semantic extraction existed and before thinking tokens were being counted at
    all. With correct accounting on one real page:

    * reasoning **on**: correction $0.0253 > semantics $0.0222. Correction
      reasons far harder per call (5.7x its own output, against 2.6x for
      extraction), which more than offsets extraction's per-chunk calls.
    * reasoning **off**: semantics $0.0097 > correction $0.0057. Extraction pays
      a system prompt once per chunk and correction batches, so with the
      reasoning removed the call count decides.

    Either way the ordering is between the two *generation* stages, and either
    way both dwarf embedding — which is what the gate's position actually
    depends on. An earlier claim here that "semantics is now the largest single
    stage" was true only of the reasoning-off case.
    """
    from brainworker.config import Gemini

    model = Gemini().model

    def usd(measured: dict, stage: str) -> float:
        return act.price_for(model, measured[stage]["input"], measured[stage]["output"])

    assert usd(MEASURED_THINKING, "correction") > usd(MEASURED_THINKING, "semantics")
    assert usd(MEASURED_NO_THINKING, "semantics") > usd(MEASURED_NO_THINKING, "correction")


async def test_a_gleaning_pass_is_priced_before_it_is_spent(
    monkeypatch: pytest.MonkeyPatch, workspace: pathlib.Path
):
    """A stage that spends without appearing in the estimate breaks the rule the
    gate rests on. Gleaning re-sends the chunk *and* what was already extracted
    from it, so both legs scale with the passes — and the worst case is assumed,
    because an estimate at the gate must over-report."""
    options = StageOptions(correct=False, embed=False, extract_semantics=True,
                           learn_profile=False)

    monkeypatch.delenv("BRAIN_MAX_GLEANING", raising=False)
    off = await act.estimate_cost(_preview(), options)

    monkeypatch.setenv("BRAIN_MAX_GLEANING", "1")
    one = await act.estimate_cost(_preview(), options)

    [off_semantics] = [s for s in off.stages if s.stage == "semantics"]
    [one_semantics] = [s for s in one.stages if s.stage == "semantics"]

    assert one_semantics.output_tokens == 2 * off_semantics.output_tokens
    assert one_semantics.input_tokens > 2 * off_semantics.input_tokens, (
        "a second pass re-sends the first pass's output as input too"
    )


async def test_condensing_descriptions_is_priced_only_when_it_is_switched_on(
    workspace: pathlib.Path,
):
    """A stage that spends and does not appear in the estimate breaks the rule the
    approval gate rests on."""
    semantics_only = await act.estimate_cost(
        _preview(),
        StageOptions(correct=False, embed=False, extract_semantics=True,
                     learn_profile=False),
    )
    assert [s.stage for s in semantics_only.stages] == ["semantics"]

    with_condense = await act.estimate_cost(
        _preview(),
        StageOptions(correct=False, embed=False, extract_semantics=True,
                     learn_profile=False, condense_descriptions=True),
    )
    assert [s.stage for s in with_condense.stages] == ["semantics", "semantics-condense"]

    # Switched on without extraction there is nothing to condense, so it must not
    # appear — an estimate naming a stage that cannot run is a wrong estimate.
    neither = await act.estimate_cost(
        _preview(),
        StageOptions(correct=False, embed=False, extract_semantics=False,
                     learn_profile=False, condense_descriptions=True),
    )
    assert [s.stage for s in neither.stages] == []


async def test_the_range_covers_the_document_that_broke_the_point_estimate(
    workspace: pathlib.Path,
):
    """The 2026-08-21 run, pinned.

    `Avanzando hacia la madurez`: 17,819 characters over 16 chunks, correction
    off. The gate quoted $0.0919 and the run billed $0.1176 — 22% under, in the
    one direction the figure may not err in. Two things were wrong and only one
    was a mistake: `SEMANTICS_CALL_OVERHEAD` had not been re-measured after the
    schema grew three fields, and the output figure is a mean over a corpus whose
    documents vary by more than 2x, which no single number can cover without
    overshooting the small ones.

    So the low end may still sit under this document — it describes a typical one
    — but the high end may not.
    """
    options = StageOptions(correct=False, embed=True, extract_semantics=True,
                           learn_profile=False)
    estimate = await act.estimate_cost(_preview(characters=17819, chunk_count=16), options)
    [semantics] = [s for s in estimate.stages if s.stage == "semantics"]

    assert semantics.input_tokens >= 15388, "input is nearly deterministic; it must cover"
    assert semantics.output_tokens_high >= 12606
    assert semantics.usd_high >= 0.117627, (
        f"the high end quoted {semantics.usd_high} against a real $0.117627"
    )
    # And the low end still describes a typical document rather than the worst.
    assert semantics.usd <= 0.117627 * 2

    assert estimate.total_usd_high is not None
    assert estimate.total_usd_high >= estimate.total_usd


async def test_a_stage_with_no_measured_spread_reads_as_a_single_figure(
    workspace: pathlib.Path,
):
    """Only semantics has a measured spread. Widening a stage whose figure is
    already a ceiling — correction's 1.5 over a measured 1.29, or the borrowed
    6.0 on the unmeasured ones — would over-report twice over."""
    estimate = await act.estimate_cost(
        _preview(), StageOptions(correct=True, embed=True, extract_semantics=False,
                                 learn_profile=True),
    )
    for stage in estimate.stages:
        assert stage.output_tokens_high == stage.output_tokens, stage.stage
        assert stage.usd_high == stage.usd, stage.stage
    assert estimate.total_usd_high == estimate.total_usd


async def test_the_high_end_covers_the_worst_document_in_the_corpus(
    workspace: pathlib.Path,
):
    """1210 output tokens per chunk, measured 2026-08-22 on `CONFERENCIAS
    FYC-86-103.pdf` during a 37-document batch — the run that walked past the
    1175 ceiling measured the day before, and past the 1.86 spread set for it.

    The old single number could not reach this: at 1.85x the mean it would have
    been the "over-reporting wildly" failure. It is reachable now only because
    the reach test binds the low end, which is the point of having two.

    **The ceiling keeps moving**: 801, then 1175, then this. It has moved with
    every corpus growth, so this pins a measurement, not a law — re-run the query
    in `doc/COMPANY_BRAIN.md` after any large import.
    """
    worst_per_chunk = 1210
    ceiling = act.SEMANTICS_OUTPUT_PER_CHUNK * act.OUTPUT_SPREAD["semantics"]
    assert ceiling >= worst_per_chunk, (
        f"the high end tops out at {ceiling:.0f} output tokens per chunk against "
        f"a measured worst of {worst_per_chunk}"
    )

    chunks = 40
    estimate = await act.estimate_cost(
        _preview(characters=chunks * 1200, chunk_count=chunks),
        StageOptions(correct=False, embed=False, extract_semantics=True,
                     learn_profile=False),
    )
    [semantics] = estimate.stages
    assert semantics.output_tokens_high >= chunks * worst_per_chunk
    # And the low end still describes the middle of the corpus, not the tail.
    assert semantics.output_tokens <= chunks * 894, "p95 is the reach limit"


# -- the organisation's own tree ---------------------------------------------


async def test_a_path_outside_the_organisation_is_refused_before_it_is_read(
    tmp_path: pathlib.Path, workspace
):
    """The one guard on `source_path`, and there is nothing else.

    This activity does not stage a file; it is handed a path and hashes whatever
    is there. Without the check an organisation could name another's inbox — or
    the mounted provider secrets — and have the pipeline index it into their own
    corpus under their own tenant.
    """
    outsider = tmp_path.parent / "de-otro.txt"
    outsider.write_text(TEXTO, encoding="utf-8")
    with pytest.raises(PermissionError, match="outside"):
        await act.stage_source(request_for(outsider))


async def test_one_organisation_cannot_read_anothers_inbox(
    tmp_path: pathlib.Path, workspace
):
    """The case a plain "is it under the workspace?" check waves through.

    Both files are inside the volume. Only one is inside the asking
    organisation's tree, and that is the distinction that matters.
    """
    other = "tnt_" + "b" * 24
    theirs = tmp_path / "tenants" / other / "inbox"
    theirs.mkdir(parents=True)
    theirs_file = theirs / "suyo.txt"
    theirs_file.write_text(TEXTO, encoding="utf-8")

    # The owner reads it.
    staged = await act.stage_source(request_for(theirs_file, tenant_id=other))
    assert staged.content_sha256

    # The legacy tenant, whose root is the volume itself, must not — even though
    # the file is plainly under that root.
    with pytest.raises(PermissionError, match="outside"):
        await act.stage_source(request_for(theirs_file))


# -- whose graph a run writes into -------------------------------------------
#
# `VersionNode.tenant_id` used to carry a default, and all three activities that
# build one forgot to pass it. A paying organisation's document, sections,
# chunks and citations were written into the legacy tenant's graph under an
# *unsalted* version id — readable by the free plane, and absent from the
# organisation that paid for it. Nothing failed: retrieval still found the right
# text, with no locator and no claim, which reads exactly like a graph that has
# not been projected yet.
#
# The field is required now. These pin the three call sites, because "required"
# only stops a *new* one from being written without a tenant — it says nothing
# about one that passes the wrong tenant, and passing `LEGACY_TENANT_ID`
# explicitly would compile.

ACME = "tnt_" + "9" * 24


class _NoopCatalog:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def activate(self, *_a, **_k):
        pass


class _CapturingGraph:
    """Stands in for `Graph`, and for the projection functions it is passed to.

    The activities open a real Bolt session, so the double replaces the context
    manager; the projection call is intercepted separately, since what is being
    asserted is the *node handed over*, not anything Memgraph does with it.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ensure_schema(self):
        pass


@pytest.fixture
def acme_libro(workspace: pathlib.Path) -> pathlib.Path:
    """A source file inside ACME's own workspace subtree.

    Not in `tmp_path` beside the other fixtures' documents: phase 2 made
    `Paths.contains` refuse a source outside the organisation's own root, so a
    file elsewhere is rejected before any of this is reached. That refusal is
    the workspace half of the same boundary these tests are about.
    """
    path = workspace / "tenants" / ACME / "inbox" / "institucion.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEXTO, encoding="utf-8")
    return path


@pytest.fixture
def captured_versions(monkeypatch):
    seen: list = []
    monkeypatch.setattr(act, "Graph", lambda *_a, **_k: _CapturingGraph())
    monkeypatch.setattr(
        act.proj, "project_structure", lambda _g, version: seen.append(version) or {}
    )
    monkeypatch.setattr(
        act.proj, "activate", lambda _g, version: seen.append(version)
    )
    return seen


async def test_project_structure_writes_into_the_organisation_that_asked(
    acme_libro, workspace, captured_versions
):
    request = request_for(acme_libro, tenant_id=ACME)
    staged = await act.stage_source(request)
    # Built rather than registered: `register_document` writes to Postgres, and
    # what is being asserted here is which organisation the *graph* node names.
    registered = Registered(
        document_id="doc_x", version_id="ver_x", created=True,
        already_indexed=False, tenant_id=ACME,
    )

    # Chunking is not what is under test and needs the corrected artifact, so
    # the structure is projected from a hand-written one-row chunk file.
    store = act.ArtifactStore(workspace, "run_t")
    ref = store.write_jsonl(
        "chunks",
        [{"index": 0, "kind": "cuerpo", "text": "Uno.", "char_from": 0, "char_to": 4}],
    )
    await act.project_structure(request, staged, registered, "run_t", ref)

    assert [v.tenant_id for v in captured_versions] == [ACME]
    # The id the rest of the system will look this version up by. Unsalted is
    # what the defect produced, and it is a different string.
    assert captured_versions[0].version == act.make_version_id(
        staged.content_sha256, ACME
    )


async def test_activation_marks_the_asking_organisations_version(
    acme_libro, workspace, captured_versions, monkeypatch
):
    request = request_for(acme_libro, tenant_id=ACME)
    staged = await act.stage_source(request)
    registered = Registered(
        document_id="doc_x", version_id="ver_x", created=True,
        already_indexed=False, tenant_id=ACME,
    )
    # The catalog leg of activation is not what is under test.
    monkeypatch.setattr(act, "Catalog", lambda *_a, **_k: _NoopCatalog())
    await act.activate_version(request, staged, registered)
    assert [v.tenant_id for v in captured_versions] == [ACME]


async def test_the_eval_set_is_priced_per_call_not_per_document(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The miss this repository has now measured three times.

    A generation call pays for its system instruction, schema and JSON envelope
    *per call*, so an estimate that charges a document once is wrong by the
    overhead of every call after the first. The eval set makes one call per
    sampled chunk — `build_evalset` is stratified by `kind` — so the figure has
    to scale with the sample, and it stops scaling once the sample is capped.
    """
    extraction = await act.extract_text(request_for(libro), "run_e")
    preview = await act.preview_chunks("run_e", extraction, StageOptions())
    opts = StageOptions(
        correct=False, embed=False, extract_semantics=False,
        learn_profile=False, generate_evalset=True,
    )

    # Characters scale with the chunk count, so the *average chunk* stays the
    # same size across the three and only the call count moves. Holding
    # `characters` fixed instead would shrink the per-call input as chunks grew,
    # which is an artefact of the fixture rather than a property of the code.
    CHARS_PER_CHUNK = 1_100

    def at(chunks: int):
        return replace(preview, chunk_count=chunks, characters=chunks * CHARS_PER_CHUNK)

    small = await act.estimate_cost(at(3), opts)
    large = await act.estimate_cost(at(600), opts)
    capped = await act.estimate_cost(at(6000), opts)

    def stage(est, name):
        return next(s for s in est.stages if s.stage == name)

    # Three chunks, three calls; 600 chunks, forty — because the sample is
    # stratified and capped, not one question per chunk.
    assert stage(large, "evalset").input_tokens == pytest.approx(
        stage(small, "evalset").input_tokens * (act.EVAL_SAMPLE / 3), rel=0.01
    )
    assert stage(large, "evalset").input_tokens == stage(capped, "evalset").input_tokens
    # Reasoning is on for this stage — `evalset` is absent from
    # `Gemini.stage_thinking`, deliberately, so it inherits the budget rather
    # than being switched off — and reasoning tokens are billed as output.
    assert stage(large, "evalset").output_tokens == int(
        act.EVAL_SAMPLE
        * act.EVALSET_OUTPUT_PER_CALL
        * act.THINKING_OUTPUT_MULTIPLIER["evalset"]
    )


def test_the_noise_query_count_has_not_drifted():
    """`NOISE_QUERIES` is duplicated into the estimator to keep it free of engine
    imports. A duplicated constant needs a test or it is just a stale one."""
    from docagent.evaluate import NOISE_QUERIES

    assert act.NOISE_QUERIES == len(NOISE_QUERIES)


def test_the_estimator_and_the_stage_agree_on_the_sample():
    """The estimate is a promise about a bill; the stage is what settles it. Two
    constants would let the gate quote forty calls and the run make eighty."""
    from brainworker.activities import paid

    assert paid.EVAL_SAMPLE is act.EVAL_SAMPLE
    assert paid.EVAL_SEED is act.EVAL_SEED


async def test_the_eval_set_estimate_covers_what_the_first_real_run_billed(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The measurement that corrected a 3.6x under-report.

    Measured 2026-08-31 on `taller-de-tarsis.txt`: 8 chunks over 4,629
    characters, `generate_evalset` on, correction and semantics off. The run
    billed **4,408 input and 7,743 output tokens for the eval set, $0.064685**,
    against an estimate of 11,456 / 2,160 and $0.033384 — input over-reported by
    2.6x and output *under*-reported by 3.6x, which since output is priced at
    five times input left the whole quote at half the bill.

    Under-reporting is the one direction this must never fail in: a user who
    approved $0.03 and was billed $0.07 has been misled, and the reverse has not.
    """
    extraction = await act.extract_text(request_for(libro), "run_m")
    preview = await act.preview_chunks("run_m", extraction, StageOptions())
    measured = replace(preview, chunk_count=8, characters=4_629)

    estimate = await act.estimate_cost(
        measured,
        StageOptions(
            correct=False, embed=True, extract_semantics=False,
            learn_profile=False, generate_evalset=True,
        ),
    )
    stage = next(s for s in estimate.stages if s.stage == "evalset")

    assert stage.output_tokens >= 7_743, "it would quote less than the run cost"
    assert stage.input_tokens >= 4_408
    # And not wildly more, which is the other half of the rule the range exists
    # to hold: covering the worst must not mean doubling the typical.
    assert stage.output_tokens < 2 * 7_743
    assert stage.input_tokens < 2 * 4_408
