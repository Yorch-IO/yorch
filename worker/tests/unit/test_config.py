"""Config invariants that are cheap to state and expensive to discover broken."""

from __future__ import annotations

import pathlib

import pytest

from brainworker import config


def test_secret_values_never_appear_in_repr(tmp_path: pathlib.Path):
    """A Settings object lands in log lines and exception context. If a key
    rendered there it would outlive the process in whatever collected the log,
    which is exactly the exposure keeping keys out of Temporal is meant to
    prevent."""
    secrets = tmp_path / "providers.env"
    secrets.write_text("VERTEX_API_KEY=sk-do-not-leak-me\n", encoding="utf-8")

    settings = config.Settings(
        workspace=tmp_path,
        temporal_target="t:7233",
        temporal_namespace="default",
        task_queue="q",
        qdrant_url="http://q:6333",
        qdrant_collection="brain",
        memgraph_url="bolt://m:7687",
        database_url="postgresql://u:p@h/db",
        log_level="INFO",
        secrets_file=secrets,
        secrets=config._read_secrets(secrets),
    )

    assert settings.secret("VERTEX_API_KEY") == "sk-do-not-leak-me"
    assert "sk-do-not-leak-me" not in repr(settings)


def test_the_api_binds_loopback_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch):
    """The control API has no authentication — it is safe only because nothing
    off the machine can route to it. Binding every interface must be something
    you ask for, not what you get by forgetting. The container asks for it
    explicitly, because Docker forwards the host's 127.0.0.1 to the container's
    own address; the same value on a bare host would publish the control plane.
    """
    monkeypatch.delenv("BRAIN_API_HOST", raising=False)
    assert config.load().api_host == "127.0.0.1"

    monkeypatch.setenv("BRAIN_API_HOST", "0.0.0.0")
    assert config.load().api_host == "0.0.0.0"


def test_missing_secrets_file_is_not_an_error(tmp_path: pathlib.Path):
    """The stack has to start before any provider has been configured."""
    assert config._read_secrets(tmp_path / "absent.env") == {}


@pytest.mark.parametrize(
    "line,key,value",
    [
        ("KEY=plain", "KEY", "plain"),
        ('KEY="quoted"', "KEY", "quoted"),
        ("KEY='single'", "KEY", "single"),
        ("  KEY = spaced  ", "KEY", "spaced"),
        ("KEY=has=equals", "KEY", "has=equals"),
    ],
)
def test_secret_parsing(tmp_path: pathlib.Path, line: str, key: str, value: str):
    f = tmp_path / "s.env"
    f.write_text(f"# a comment\n\n{line}\n", encoding="utf-8")
    assert config._read_secrets(f) == {key: value}


@pytest.mark.parametrize("bad", ["", ".", "..", "../escape", "a/b", "a\\b", ".hidden"])
def test_run_dir_refuses_ids_that_escape_the_workspace(tmp_path: pathlib.Path, bad: str):
    """workflow_id becomes a path segment. This is the last cheap place to
    refuse one that would write outside the mounted volume."""
    with pytest.raises(ValueError):
        config.Paths(tmp_path).run_dir(bad)


def test_run_dir_stays_under_runs(tmp_path: pathlib.Path):
    paths = config.Paths(tmp_path)
    got = paths.run_dir("ingest-1730000000000-abc123")
    assert got.parent == paths.runs
    assert tmp_path in got.parents


def test_ensure_creates_every_directory_the_pipeline_writes(tmp_path: pathlib.Path):
    paths = config.Paths(tmp_path / "workspace")
    paths.ensure()
    for d in (paths.inbox, paths.runs, paths.profiles, paths.cache, paths.snapshots):
        assert d.is_dir(), d


# -- reasoning budget, per stage --------------------------------------------


def test_reasoning_is_off_where_the_task_is_not_a_judgement_call():
    """The defaults are a position, not an accident.

    Planning picks an id from a fixed list — classification, measured spending
    654 output tokens to do it. Correction is orthotypographic and separately
    verified by `docagent.correct.verify()`, so reasoning pays twice for a
    guarantee the deterministic gate already gives.
    """
    from brainworker.config import Gemini

    g = Gemini()
    assert g.thinking_for("planning") == 0
    assert g.thinking_for("correction") == 0


def test_reasoning_is_off_for_extraction_because_it_measured_worse():
    """Decided by A/B, not by argument.

    Same page: with reasoning, 8 concepts and 9 claims for $0.028549; without,
    13 and 13 for $0.011239. Turning it off extracted *more*, and the extras were
    real entities in the text. Every edge carries a confidence and the floor
    filters the weak ones, so the downside is bounded.
    """
    from brainworker.config import Gemini

    assert Gemini().thinking_for("semantics") == 0


def test_reasoning_stays_on_for_answering_by_choice_not_by_measurement():
    """The one stage kept expensive on purpose, and the record of why.

    Four attempts each way on the same question: same 3.5 citations on average,
    same substance, at 3.3x the price ($0.015561 against $0.004745). One attempt
    without reasoning claimed an answer it could not support and was caught by
    the citation guard — 1 failure in 5 against 0 in 5, which that sample cannot
    distinguish from noise.

    So the evidence is neutral and this is a deliberate bias toward caution in
    the stage where being wrong is worst: answering is where the product either
    cites the corpus or fabricates. Anyone revisiting it should know it was
    decided *despite* the numbers, not because of them.
    """
    from brainworker.config import Gemini

    assert Gemini().thinking_for("answering") is None


def test_a_global_budget_still_reaches_the_answering_stage():
    """Answering is a fallback, not a map entry, so someone turning off every
    reasoning cost gets what they asked for rather than a silent exception."""
    from brainworker.config import Gemini

    assert Gemini(thinking_budget=0).thinking_for("answering") == 0
    # The stages decided per stage are unaffected by the global value.
    assert Gemini(thinking_budget=4096).thinking_for("correction") == 0


def test_the_engines_own_stage_names_resolve_to_ours():
    """`docagent.correct` passes `stage="correct"`. A silent miss here would
    leave correction paying for reasoning while the config said it was off."""
    from brainworker.config import Gemini

    assert Gemini().thinking_for("correct") == 0


def test_an_unknown_stage_falls_back_to_the_global_budget():
    from brainworker.config import Gemini

    g = Gemini(thinking_budget=512)
    assert g.thinking_for("something_new") == 512
    assert g.thinking_for(None) == 512
    # A per-stage value still wins over the global one.
    assert g.thinking_for("planning") == 0


def test_a_per_stage_override_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_THINKING_ANSWERING", "0")
    monkeypatch.setenv("BRAIN_THINKING_CORRECTION", "1024")
    g = config.load().gemini
    assert g.thinking_for("answering") == 0
    assert g.thinking_for("correction") == 1024
    # Unset stages keep the measured default rather than being cleared.
    assert g.thinking_for("planning") == 0


def test_forgetting_to_export_anything_keeps_the_defaults(monkeypatch: pytest.MonkeyPatch):
    for name in ("PLANNING", "CORRECTION", "SEMANTICS", "ANSWERING", "EVALSET"):
        monkeypatch.delenv(f"BRAIN_THINKING_{name}", raising=False)
    g = config.load().gemini
    assert g.thinking_for("planning") == 0
    assert g.thinking_for("answering") is None


def test_the_loaded_stage_defaults_match_the_dataclass_ones(monkeypatch):
    """These were two literals in two files and they drifted on the first stage
    added to one of them: a directly-constructed `Gemini` said reasoning was off
    for it while the one `load()` builds said the model default. Nothing failed;
    the stage just quietly paid the output rate for reasoning."""
    for stage in config.THINKING_STAGES:
        monkeypatch.delenv(f"BRAIN_THINKING_{stage.upper()}", raising=False)
    assert config.load().gemini.stage_thinking == config.Gemini().stage_thinking


def test_every_named_stage_can_be_overridden_from_the_environment(monkeypatch):
    for stage in config.THINKING_STAGES:
        monkeypatch.setenv(f"BRAIN_THINKING_{stage.upper()}", "512")
    resolved = config.load().gemini
    assert all(resolved.thinking_for(s) == 512 for s in config.THINKING_STAGES)
