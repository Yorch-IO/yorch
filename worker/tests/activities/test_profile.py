"""Resolving, applying and learning a document-family profile.

Until this existed, `check_profile_collision` computed a family fingerprint,
found the profile that matched it, wrote a warning saying its rules would be
applied — and discarded them. Every document in the product was chunked with
`ChunkRules()` defaults. These tests pin the three things that changed: reuse is
free and automatic, learning is paid and approved, and either way the rules
reach the chunker.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from brainworker.activities import ingest as act
from brainworker.activities import paid
from brainworker.pipeline import IngestRequest, ProfileDecision, ProfileRules, StageOptions
from brainworker.providers.gemini import Generation, Usage

#: Headings numbered in words, which `chunk.HEADING_RE` cannot see: it requires a
#: leading digit. Without a learned pattern this document indexes with no table
#: of contents at all — the defect the whole workstream exists for.
#: Three unnumbered headings among twenty-one paragraphs — about 14%, the shape
#: a real book has. An earlier version of this fixture put three headings in
#: nine paragraphs, and the degeneracy ceiling correctly rejected the pattern as
#: having stopped discriminating.
_PARTE = """LIBRO {ordinal}

Casi toda la suma de nuestra sabiduría, que de veras se deba tener por
verdadera y sólida sabiduría, consiste en dos puntos: el conocimiento que el
hombre debe tener de Dios y el que debe tener de sí mismo.

Porque ninguno se puede considerar a sí mismo sin que inmediatamente vuelva sus
sentidos a la consideración de Dios, en quien vive y se mueve y tiene su ser.

Con razón se dice que el hombre es un abismo para sí mismo, porque no puede
mirarse sin volver enseguida los ojos hacia aquel que lo hizo y lo sostiene.

La miseria del hombre es el espejo en que se contempla la bondad divina, y
quien no ha visto la primera difícilmente reconocerá la segunda como conviene.

De aquí nace aquel horror y espanto con que las Escrituras nos enseñan que los
santos eran heridos siempre que sentían de cerca la presencia de Dios.

Porque los hombres nunca reconocen bastante cuán viles son hasta que se han
comparado con la majestad de Dios, y no un momento antes de haberlo hecho.

Y esto es lo que se ha de tener por cierto en toda esta materia, según la
extensión que ella pide y que el lector podrá comprobar más adelante.
"""

LIBRO = "\n".join(
    _PARTE.format(ordinal=o) for o in ("PRIMERO", "SEGUNDO", "TERCERO")
)

L1 = r"^LIBRO\s+\w+$"


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A workspace with the catalog pointed at a closed port.

    Same reasoning as the other activity suites: profile-warning recording is
    best-effort bookkeeping, and running with it down keeps the tests fast and
    proves the stage does not depend on it.
    """
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    # `docagent.profiles.PROFILE_DIR` is a relative path resolved against the
    # process CWD, which is exactly how `config.configure()` relocates it in
    # production. Reproducing that here rather than patching the constant is
    # what makes test_profiles_land_where_the_worker_looks_for_them meaningful.
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def libro(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "institucion.txt"
    path.write_text(LIBRO, encoding="utf-8")
    return path


def request_for(path: pathlib.Path, **kw) -> IngestRequest:
    return IngestRequest(
        library_id="lib_1", source_path=str(path), source_key=f"libros/{path.name}", **kw
    )


async def _extract(libro: pathlib.Path, run_id: str = "run_1"):
    return await act.extract_text(request_for(libro), run_id)


def save_profile(fingerprint: str, *, slug: str, learned_from: str, **rules):
    """Write a profile the way the engine does, so `load` finds it."""
    from docagent.chunk import ChunkRules, DocRules
    from docagent.profiles import Profile

    p = Profile(
        fingerprint=fingerprint,
        slug=slug,
        extractor="plain",
        doc_rules=DocRules(header_patterns=tuple(rules.pop("header_patterns", ()))),
        chunk_rules=ChunkRules(**rules),
        learned_from=learned_from,
        learned_at=1_700_000_000.0,
        revisions=3,
    )
    p.save()
    return p


# -- where the rules live ---------------------------------------------------


def test_profiles_land_where_the_worker_looks_for_them(workspace: pathlib.Path):
    """A profile written to the wrong directory is never found again, and
    nothing anywhere would say so — it would simply look like reuse never
    working. `config.configure()` chdirs to the workspace for exactly this."""
    from docagent import profiles as engine_profiles

    from brainworker import config

    settings = config.load()
    assert settings.paths.profiles.resolve() == (
        pathlib.Path.cwd() / engine_profiles.PROFILE_DIR
    ).resolve()


# -- resolving --------------------------------------------------------------


async def test_a_family_nobody_has_seen_gets_the_measured_defaults(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await _extract(libro)
    decision = await act.resolve_profile("ver_1", "run_1", extraction, StageOptions())
    assert decision.source == "default"
    assert decision.slug == ""
    assert decision.rules == ProfileRules()
    assert decision.fingerprint


async def test_a_matching_fingerprint_is_reused_and_costs_nothing(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The whole point of keying on a family: the first document pays, the rest
    inherit."""
    extraction = await _extract(libro)
    first = await act.resolve_profile("ver_1", "run_1", extraction, StageOptions())
    save_profile(
        first.fingerprint,
        slug="calvino-abc12345",
        learned_from="libros/institucion.txt",
        heading_l1_pattern=L1,
        heading_l1_max=44,
    )

    decision = await act.resolve_profile("ver_1", "run_1", extraction, StageOptions())
    assert decision.source == "reused"
    assert decision.slug == "calvino-abc12345"
    assert decision.revisions == 3
    assert decision.rules.heading_l1_pattern == L1
    assert decision.rules.heading_l1_max == 44


async def test_ignoring_a_profile_drops_its_rules_but_keeps_the_record(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """A person who saw a low topical overlap and declined the inherited rules
    still needs the run to record what was declined."""
    extraction = await _extract(libro)
    fp = (await act.resolve_profile("v", "run_1", extraction, StageOptions())).fingerprint
    save_profile(fp, slug="actas-1a2b3c4d", learned_from="libros/actas.txt",
                 heading_l1_pattern=L1)

    decision = await act.resolve_profile(
        "v", "run_1", extraction, StageOptions(ignore_profile=True)
    )
    assert decision.source == "default"
    assert decision.rules.heading_l1_pattern is None
    assert decision.warnings, "declining is not the same as never having collided"
    assert "descartado" in decision.warnings[0].detail


async def test_a_collision_is_reported_with_its_topical_overlap(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The fingerprint is structural and structure is not subject matter. A
    *low* overlap is the dangerous case: same layout, unrelated material."""
    extraction = await _extract(libro)
    fp = (await act.resolve_profile("v", "run_1", extraction, StageOptions())).fingerprint
    save_profile(fp, slug="actas-1a2b3c4d", learned_from="libros/actas-consistorio.pdf")

    decision = await act.resolve_profile("v", "run_1", extraction, StageOptions())
    warning = decision.warnings[0]
    assert warning.profile_id == "actas-1a2b3c4d"
    assert warning.collides_with == "libros/actas-consistorio.pdf"
    assert 0.0 <= warning.similarity <= 1.0
    assert fp in warning.detail


async def test_a_profile_that_only_adds_structure_raises_no_disagreement(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """Finding 3 chapters where the defaults find 0 is the profile *working*.

    A learned heading pattern turned 0 detected chapters into 9 on a real
    document, so warning on any difference would fire on every successful reuse
    — and a warning that fires on success is one an operator learns to click
    past, which is precisely how the collision it exists to catch gets missed.
    """
    extraction = await _extract(libro)
    fp = (await act.resolve_profile("v", "run_1", extraction, StageOptions())).fingerprint
    save_profile(fp, slug="otro-1a2b3c4d", learned_from="libros/otro.txt",
                 heading_l1_pattern=L1)

    decision = await act.resolve_profile("v", "run_1", extraction, StageOptions())
    assert decision.rules.heading_l1_pattern == L1, "the rules were inherited"
    assert not [w for w in decision.warnings if "capítulo" in w.detail]


async def test_rules_that_contradict_the_defaults_raise_a_warning(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """`doc/CLAUDE.md`'s recorded fix for a fingerprint collision: recall cannot
    see a wrong-family profile, but the two rule sets *contradicting* each other
    about the chapter count can. The report there read "4 chapters inherited vs
    1 read" — both non-zero, and disagreeing."""
    prose = (
        "Una exposición del capítulo con la extensión que la materia pide y "
        "que el lector podrá comprobar más adelante en el texto completo."
    )
    # Three numbered headings of deliberately different lengths, so a tighter
    # length guard admits some and drops others — a partial disagreement, which
    # is the shape the recorded case had ("4 chapters inherited vs 1 read").
    numbered = tmp_path / "numerado.txt"
    numbered.write_text(
        "\n\n".join(
            f"{title}\n\n{prose}"
            for title in (
                "1. Capítulo I",
                "2. Capítulo segundo de la obra completa",
                "3. Capítulo III",
            )
        ),
        encoding="utf-8",
    )
    extraction = await act.extract_text(request_for(numbered), "run_n")
    fp = (await act.resolve_profile("v", "run_n", extraction, StageOptions())).fingerprint
    save_profile(fp, slug="otro-1a2b3c4d", learned_from="libros/otro.txt",
                 heading_l1_max=16)

    decision = await act.resolve_profile("v", "run_n", extraction, StageOptions())
    disagreements = [w for w in decision.warnings if "capítulo" in w.detail]
    assert disagreements, "inherited and default rules contradict each other, unreported"
    assert "2 capítulo(s)" in disagreements[0].detail
    assert "3" in disagreements[0].detail


async def test_a_documents_own_profile_never_reads_as_a_collision(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """The check asks whether *inherited* rules read a different structure than
    the built-in ones. A profile reused on the document it was learned from is
    not inherited from anywhere, so a disagreement there is the profile doing
    exactly its job.

    Measured 2026-08-31 on `01_RetoDeDios_INT-S.pdf`, whose own learned pattern
    reads 38 chapters where the defaults read 8. Without this, activation was
    withheld from every re-index of a document that had learned its own profile
    — a `structural_mismatch` on a document that collides with nothing.
    """
    prose = (
        "Una exposición del capítulo con la extensión que la materia pide y "
        "que el lector podrá comprobar más adelante en el texto completo."
    )
    numbered = tmp_path / "propio.txt"
    numbered.write_text(
        "\n\n".join(
            f"{title}\n\n{prose}"
            for title in (
                "1. Capítulo I",
                "2. Capítulo segundo de la obra completa",
                "3. Capítulo III",
            )
        ),
        encoding="utf-8",
    )
    extraction = await act.extract_text(request_for(numbered), "run_own")
    fp = (await act.resolve_profile("v", "run_own", extraction, StageOptions())).fingerprint
    # The same rules that produce a contradiction above — but learned from *this*
    # document rather than from another one.
    save_profile(fp, slug="propio-1a2b3c4d",
                 learned_from=extraction.source_key, heading_l1_max=16)

    decision = await act.resolve_profile("v", "run_own", extraction, StageOptions())

    assert decision.source == "reused"
    assert not [w for w in decision.warnings if w.kind == "heading_disagreement"], (
        "a document's own profile was reported as a structural collision"
    )


async def test_no_disagreement_is_reported_when_the_rules_agree(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await _extract(libro)
    fp = (await act.resolve_profile("v", "run_1", extraction, StageOptions())).fingerprint
    save_profile(fp, slug="otro-1a2b3c4d", learned_from="libros/otro.txt")

    decision = await act.resolve_profile("v", "run_1", extraction, StageOptions())
    assert not [w for w in decision.warnings if "capítulo" in w.detail]


# -- applying ---------------------------------------------------------------


async def test_without_a_profile_the_document_has_no_table_of_contents(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The defect, stated so the fix has something to beat."""
    extraction = await _extract(libro)
    preview = await act.preview_chunks("run_1", extraction, StageOptions(correct=False))
    rows = _rows(workspace, preview.chunks)
    assert not any(r["chapter"] or r["section"] for r in rows)


async def test_a_reused_profile_gives_the_preview_a_table_of_contents(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The quality claim, asserted rather than argued."""
    extraction = await _extract(libro)
    decision = ProfileDecision(
        fingerprint="fp", source="reused", slug="calvino-abc12345",
        rules=ProfileRules(heading_l1_pattern=L1),
    )
    preview = await act.preview_chunks(
        "run_1", extraction, StageOptions(correct=False), decision
    )
    chapters = {r["chapter"] for r in _rows(workspace, preview.chunks) if r["chapter"]}
    assert chapters == {"LIBRO PRIMERO", "LIBRO SEGUNDO", "LIBRO TERCERO"}
    assert any("reglas heredadas" in w for w in preview.warnings)


async def test_the_final_chunking_applies_the_profile_too(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """`chunk_final` is the chunking that decides the indexed outline, so it is
    the one that must not fall back to defaults."""
    await _extract(libro)
    decision = ProfileDecision(
        fingerprint="fp", source="reused", rules=ProfileRules(heading_l1_pattern=L1)
    )
    chunked = await paid.chunk_final("run_1", "raw_text", decision)
    chapters = {r["chapter"] for r in _rows(workspace, chunked.chunks) if r["chapter"]}
    assert chapters == {"LIBRO PRIMERO", "LIBRO SEGUNDO", "LIBRO TERCERO"}


async def test_a_default_decision_leaves_the_engine_defaults_alone(
    workspace: pathlib.Path, libro: pathlib.Path
):
    await _extract(libro)
    plain = await paid.chunk_final("run_1", "raw_text")
    decided = await paid.chunk_final("run_1", "raw_text", ProfileDecision(fingerprint="fp"))
    assert plain.count == decided.count
    assert plain.kinds == decided.kinds


async def test_a_learned_kind_pattern_cuts_the_chunk_as_well_as_naming_it(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """A change of kind is a chunk *boundary*, so the learned pattern has to
    reach the chunker rather than relabel its output.

    This family marks review questions "P1" rather than "1.", which the built-in
    discriminator does not recognise — so by default the questions merge into the
    preceding prose and the whole thing indexes as body text. Relabelling
    finished chunks could not have fixed that: there was only ever one chunk."""
    path = tmp_path / "preguntas.txt"
    path.write_text(
        "\n\n".join(
            ["Una exposición del capítulo con la extensión que la materia pide y "
             "que el lector podrá comprobar más adelante en el texto."]
            + [f"P{n} Defina el término que aparece en el capítulo" for n in range(1, 6)]
        ),
        encoding="utf-8",
    )
    await act.extract_text(request_for(path), "run_q")
    decision = ProfileDecision(
        fingerprint="fp", source="reused",
        rules=ProfileRules(question_pattern=r"^P\d+\s"),
    )
    chunked = await paid.chunk_final("run_q", "raw_text", decision)
    assert any(k.kind == "preguntas" for k in chunked.kinds)

    plain = await paid.chunk_final("run_q", "raw_text")
    assert not any(k.kind == "preguntas" for k in plain.kinds)
    assert plain.count == 1, "the default rules merged everything into one chunk"
    assert chunked.count > plain.count, "the learned rule split prose from questions"


# -- the estimate -----------------------------------------------------------


async def test_learning_is_estimated_when_there_is_nothing_to_reuse(
    workspace: pathlib.Path, libro: pathlib.Path
):
    extraction = await _extract(libro)
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(
        preview, StageOptions(), ProfileDecision(fingerprint="fp", source="default")
    )
    profile = next(s for s in estimate.stages if s.stage == "profile")
    # Every refine attempt is priced. A proposal that validates first time is the
    # good case, and a gate must not quote the good case.
    assert profile.input_tokens == act.PROFILE_CALL_INPUT * act.PROFILE_MAX_ATTEMPTS


async def test_a_reused_profile_is_not_estimated_at_all(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The second document of a family is cheaper than the first, and the gate
    has to show that rather than quote for work it will not do."""
    extraction = await _extract(libro)
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(
        preview, StageOptions(), ProfileDecision(fingerprint="fp", source="reused")
    )
    assert "profile" not in [s.stage for s in estimate.stages]


async def test_the_preview_admits_that_learning_will_change_it(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """`chunks_are_final` is the field no UI may quietly drop."""
    extraction = await _extract(libro)
    preview = await act.preview_chunks(
        "run_1", extraction, StageOptions(correct=False),
        ProfileDecision(fingerprint="fp", source="default"),
    )
    assert preview.chunks_are_final is False
    assert any("perfil" in w for w in preview.warnings)


# -- learning ---------------------------------------------------------------


class FakeLearner:
    """Returns a canned rule proposal, and counts the calls it took."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.stages: list[str] = []

    def generate(self, prompt, *, system=None, temperature=0.0, max_output_tokens=None,
                 response_schema=None, stage=None):
        self.calls += 1
        self.stages.append(stage)
        payload = self.payloads[min(self.calls, len(self.payloads)) - 1]
        return Generation(
            text=json.dumps(payload), usage=Usage(input_tokens=2000, output_tokens=400)
        )


GOOD = {
    "header_patterns": [],
    "heading_l1_max": 44,
    "heading_l2_max": 120,
    "heading_l1_pattern": L1,
    "reasoning": "los libros se numeran con palabras",
}
#: Fails an **optional** rule. Partial adoption drops it and keeps everything
#: else, so this does not trigger a refine round — deliberately: discarding a
#: header pattern that validated cleanly because a footnote pattern beside it was
#: degenerate is the mistake `ESSENTIAL_RULES` exists to prevent.
DEGENERATE_OPTIONAL = {
    "header_patterns": [],
    "heading_l1_max": 44,
    "heading_l2_max": 120,
    "heading_l1_pattern": L1,
    "footnote_pattern": "^.",
    "reasoning": "todo es una nota",
}
#: Fails an **essential** rule, which is what a refine round is for. A header
#: pattern matching nothing changes what text comes out of the extractor.
DEGENERATE_ESSENTIAL = {
    "header_patterns": ["^ESTA LÍNEA NO EXISTE EN NINGUNA PÁGINA$"],
    "heading_l1_max": 44,
    "heading_l2_max": 120,
    "heading_l1_pattern": L1,
    "reasoning": "creo recordar un encabezado",
}


@pytest.fixture
def learner(monkeypatch: pytest.MonkeyPatch):
    def _install(*payloads: dict) -> FakeLearner:
        fake = FakeLearner(*payloads)
        monkeypatch.setattr(paid, "_provider", lambda: fake)
        return fake

    return _install


async def test_a_validated_proposal_is_adopted_and_saved(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    fake = learner(GOOD)
    extraction = await _extract(libro)
    decision = await act.resolve_profile("ver_1", "run_1", extraction, StageOptions())

    learned = await paid.learn_profile("run_1", extraction, decision)
    assert learned.source == "learned"
    assert learned.rules.heading_l1_pattern == L1
    assert "heading_patterns" in learned.adopted
    assert fake.calls == 1, "a proposal that validates needs no refine round"

    # Saved where the next document of this family will find it.
    saved = list((workspace / "profiles").glob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["fingerprint"] == decision.fingerprint


async def test_learning_reasons_are_off_for_this_stage(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """`docagent.rules.propose` passes stage="propose"; the config knows it as
    "profile". A silent miss there leaves the stage paying for reasoning while
    the config says it is off."""
    from brainworker import config

    fake = learner(GOOD)
    extraction = await _extract(libro)
    await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert fake.stages == ["propose"]
    assert config.load().gemini.thinking_for("propose") == 0


async def test_an_essential_rule_failing_earns_a_refine_round(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """The model proposes; a deterministic checker applied to the whole document
    decides, and hands its objection back for another attempt."""
    fake = learner(DEGENERATE_ESSENTIAL, GOOD)
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert fake.calls == 2
    assert learned.source == "learned"
    assert learned.rules.heading_l1_pattern == L1


async def test_an_optional_rule_failing_is_dropped_rather_than_retried(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """Partial adoption. Requiring every rule to pass once discarded a header
    pattern that had validated cleanly three times, alongside the bad footnote
    pattern it happened to travel with — 175 running-header lines left in the
    text as a result."""
    fake = learner(DEGENERATE_OPTIONAL)
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert fake.calls == 1, "an optional rule is not worth another paid call"
    assert learned.source == "learned"
    assert learned.rules.heading_l1_pattern == L1, "the good rule survived"
    assert learned.rules.footnote_pattern is None, "the bad one did not"
    assert "footnote_pattern" not in learned.adopted


async def test_exhausting_the_attempts_keeps_whatever_validated(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """Running out of attempts is not a reason to throw away the rules that
    passed.

    Observed on the first real learning run: three attempts failed
    `heading_guards` on a non-contiguous chapter sequence, and discarding the
    proposal took with them a header pattern that had matched on 26 pages with
    no body hits. Nothing about a bad length guard makes a good header pattern
    less true, and `adopt` already resets each failed rule to its measured
    default — so the partial result is both safe and strictly better than
    nothing."""
    fake = learner(DEGENERATE_ESSENTIAL)
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))

    assert fake.calls == act.PROFILE_MAX_ATTEMPTS, "an essential failure earns retries"
    assert learned.source == "learned"
    # The failed essential rule fell back to the measured default…
    assert learned.rules.header_patterns == []
    # …and the rule that validated survived it.
    assert learned.rules.heading_l1_pattern == L1
    assert "header_patterns" not in learned.adopted
    assert len(list((workspace / "profiles").glob("*.json"))) == 1


async def test_a_proposal_where_nothing_validated_saves_no_profile(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """The one case that must still fall back completely.

    Writing a profile of pure defaults would be worse than writing none: the
    next document of this family would match its fingerprint, "reuse" it, and
    never try to learn again — so one bad run would freeze the family on generic
    rules permanently.
    """
    learner({
        "header_patterns": ["^NO EXISTE$"],
        "heading_l1_max": 44,
        "heading_l2_max": 120,
        "heading_l1_pattern": "^.",
        "footnote_pattern": "^.",
        "question_pattern": "^.",
        "reasoning": "todo mal",
    })
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert learned.adopted == []
    assert learned.source == "default"
    assert learned.rules == ProfileRules()
    assert not list((workspace / "profiles").glob("*.json"))


async def test_a_failed_run_still_reports_what_it_spent(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """Three attempts is three billed calls whether or not a rule survived."""
    learner(DEGENERATE_ESSENTIAL)
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert learned.spend is not None
    assert learned.spend.stage == "profile"
    assert learned.spend.input_tokens == 2000 * act.PROFILE_MAX_ATTEMPTS


async def test_every_attempt_leaves_its_proposal_and_verdict_on_the_run(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    learner(DEGENERATE_ESSENTIAL, GOOD)
    extraction = await _extract(libro)
    await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    run = workspace / "runs" / "run_1"
    assert (run / "proposal.json").exists()
    verdict = json.loads((run / "validation.json").read_text())
    assert verdict["passed"] is True
    assert verdict["attempt"] == 2


async def test_the_learned_profile_records_the_library_relative_key(
    workspace: pathlib.Path, libro: pathlib.Path, learner
):
    """`learned_from` outlives the run in a file on disk. An absolute container
    path there would be meaningless on the host that later reads it."""
    learner(GOOD)
    extraction = await _extract(libro)
    learned = await paid.learn_profile("run_1", extraction, ProfileDecision(fingerprint="fp"))
    assert learned.learned_from == "libros/institucion.txt"
    assert not pathlib.Path(learned.learned_from).is_absolute()


def _rows(workspace: pathlib.Path, ref) -> list[dict]:
    path = workspace / ref.path
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def test_the_estimate_matches_the_reasoning_each_stage_will_actually_use(
    workspace: pathlib.Path, libro: pathlib.Path
):
    """The estimator resolves the budget **per stage**, not from the global one.

    Reading only the global fallback was wrong in the shipped configuration
    rather than in a corner of it: `thinking_budget` defaults to None while
    `stage_thinking` turns reasoning off for correction, semantics and profile.
    So the gate applied a 6x multiplier to three stages that were never going to
    reason, and quoted $0.076 for a document whose rule learning costs about a
    sixth of that.

    Over-reporting is the required direction, but not by 6x on the stages that
    dominate the bill — a user pushed into declining affordable work has been
    misled exactly as much as one billed more than they approved.
    """
    from brainworker import config

    settings = config.load()
    assert settings.gemini.thinking_budget is None, "the global fallback is on"
    assert settings.gemini.thinking_for("profile") == 0, "the stage itself is off"

    extraction = await _extract(libro)
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    estimate = await act.estimate_cost(preview, StageOptions(), ProfileDecision(fingerprint="fp"))

    by_stage = {s.stage: s for s in estimate.stages}
    # No reasoning multiplier, because this stage does not reason.
    assert by_stage["profile"].output_tokens == (
        act.PROFILE_CALL_OUTPUT * act.PROFILE_MAX_ATTEMPTS
    )
    assert by_stage["correction"].output_tokens < (
        preview.characters / act.CHARS_PER_TOKEN * act.THINKING_OUTPUT_MULTIPLIER["correction"]
    )


async def test_a_stage_left_reasoning_is_still_multiplied(
    workspace: pathlib.Path, libro: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The other direction: turning a stage back on must move its estimate, or
    the gate hides the largest cost lever in the product."""
    extraction = await _extract(libro)
    preview = await act.preview_chunks("run_1", extraction, StageOptions())
    off = await act.estimate_cost(preview, StageOptions(), ProfileDecision(fingerprint="fp"))

    monkeypatch.setenv("BRAIN_THINKING_PROFILE", "512")
    on = await act.estimate_cost(preview, StageOptions(), ProfileDecision(fingerprint="fp"))

    quiet = next(s for s in off.stages if s.stage == "profile")
    loud = next(s for s in on.stages if s.stage == "profile")
    assert loud.output_tokens > quiet.output_tokens * 5
