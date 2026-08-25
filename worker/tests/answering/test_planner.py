"""The planner, which is the security boundary for question answering.

Memgraph does not enforce read-only, so what stops a question writing is that a
model returns a *template id and typed parameters* and never Cypher. These tests
are about what happens when the model returns something else.
"""

from __future__ import annotations

import json

import pytest

from brainworker.answering import planner as mod
from brainworker.answering.types import Question
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.prompts: list[str] = []

        class _S:
            model = "gemini-3.6-flash"

        self.settings = _S()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.prompts.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        text = (
            self.payload if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return Generation(text=text, usage=Usage(300, 60, 20, 1))


def question(**kw) -> Question:
    kw.setdefault("library_id", "lib_1")
    return Question(text="¿Qué dice el libro sobre la gracia?", **kw)


VERSION = "ver_" + "a" * 24


# -- the boundary -----------------------------------------------------------


def test_cypher_in_the_template_field_is_refused():
    """The planner may name a template. It may not describe one."""
    provider = FakeProvider({
        "intencion": "busqueda",
        "template_id": "MATCH (n) DETACH DELETE n",
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question())
    assert plan.template_id is None


def test_an_unknown_template_degrades_to_vector_only():
    provider = FakeProvider({
        "intencion": "estructura", "template_id": "delete_everything",
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question())
    assert plan.template_id is None
    assert plan.intent == "estructura", "the intent survives; only the traversal is dropped"


def test_bad_arguments_drop_the_traversal_rather_than_failing_the_question():
    """A question answered from vector search alone is worse than one answered
    with graph context, and far better than an error."""
    provider = FakeProvider({
        "intencion": "estructura", "template_id": "document_outline",
        "parametros": {"version_id": "'; DROP DATABASE brain; --"},
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question())
    assert plan.template_id is None


def test_a_valid_plan_is_bound_and_kept():
    provider = FakeProvider({
        "intencion": "estructura", "template_id": "document_outline",
        "parametros": {"version_id": VERSION, "limit": 10},
        "conceptos": ["gracia común"], "motivo": "pide el índice",
    })
    plan = mod.plan(provider, question())
    assert plan.template_id == "document_outline"
    assert plan.params["version_id"] == VERSION
    assert plan.params["limit"] == 10
    assert plan.concepts == ["gracia común"]


def test_the_confidence_floor_is_the_callers_policy_not_the_planners():
    """A model asking for 0.0 would be asking to answer from relations nobody
    vetted."""
    provider = FakeProvider({
        "intencion": "relacion", "template_id": "concepts_in_version",
        "parametros": {"version_id": VERSION, "confidence_floor": 0.0},
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question(confidence_floor=0.75))
    assert plan.params["confidence_floor"] == 0.75


def test_the_library_is_the_callers_policy_not_the_planners():
    """A model naming a different library would be asking to answer this question
    out of somebody else's corpus.

    Overwritten rather than validated, exactly like the floor above: there is no
    value here worth reading, so none is read. Concepts are shared between
    documents by design, which is what made an unscoped concept traversal reach
    every library that had ever mentioned one.
    """
    provider = FakeProvider({
        "intencion": "concepto", "template_id": "chunks_for_concepts",
        "parametros": {
            "concept_ids": ["con_" + "a" * 24],
            "library_id": "lib_de_otra_persona",
        },
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question(library_id="lib_mia"))
    assert plan.template_id == "chunks_for_concepts"
    assert plan.params["library_id"] == "lib_mia"


def test_a_planner_that_omits_the_library_still_gets_the_callers():
    """The override fills the parameter in as well as replacing it, so a plan
    that simply forgot the scope is scoped rather than dropped."""
    provider = FakeProvider({
        "intencion": "concepto", "template_id": "chunks_for_concepts",
        "parametros": {"concept_ids": ["con_" + "a" * 24]},
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question(library_id="lib_mia"))
    assert plan.template_id == "chunks_for_concepts"
    assert plan.params["library_id"] == "lib_mia"


def test_the_limit_a_planner_asks_for_is_clamped():
    provider = FakeProvider({
        "intencion": "estructura", "template_id": "document_outline",
        "parametros": {"version_id": VERSION, "limit": 100_000},
        "conceptos": [], "motivo": "",
    })
    plan = mod.plan(provider, question())
    from brainworker.graph.queries import MAX_LIMIT

    assert plan.params["limit"] == MAX_LIMIT


# -- degrading gracefully ---------------------------------------------------


def test_no_template_is_a_valid_plan():
    """Most questions are answered by similarity alone; a planner that always
    picked a traversal would be guessing."""
    provider = FakeProvider({
        "intencion": "busqueda", "conceptos": ["felicidad"], "motivo": "basta buscar",
    })
    plan = mod.plan(provider, question())
    assert plan.template_id is None
    assert plan.concepts == ["felicidad"]


def test_a_planner_that_fails_does_not_fail_the_question():
    provider = FakeProvider(RuntimeError("429 RESOURCE_EXHAUSTED"))
    plan = mod.plan(provider, question())
    assert plan.template_id is None
    assert "no respondió" in plan.rationale


def test_an_unparseable_plan_falls_back_to_vector_only():
    plan = mod.plan(FakeProvider("no soy json"), question())
    assert plan.template_id is None


def test_empty_concepts_are_dropped():
    provider = FakeProvider({
        "intencion": "busqueda", "conceptos": ["gracia", "  ", ""], "motivo": "",
    })
    assert mod.plan(provider, question()).concepts == ["gracia"]


# -- what the planner is shown ----------------------------------------------


def test_the_planner_never_sees_any_cypher():
    """Showing the query text would invite the model to edit it, and every token
    of it is a token not spent on the question."""
    provider = FakeProvider({"intencion": "busqueda", "conceptos": [], "motivo": ""})
    mod.plan(provider, question())

    sent = provider.prompts[0].upper()
    for keyword in ("MATCH", "RETURN", "UNWIND", "MERGE", "DELETE"):
        assert keyword not in sent


def test_the_planner_is_shown_every_template_it_may_choose():
    """Every planner-visible template, and only those.

    The catalogue used to be the whole registry, and asserting that was the same
    assertion. It stopped being so when the overview reads arrived: those return
    a whole library for a canvas to draw, are named by the API alone, and raise
    their own row ceilings above `MAX_LIMIT` precisely because no model can reach
    them. Showing them here would put a 6000-row query one hallucinated id away
    from an answering context, and spend the planning prompt's budget describing
    queries that answer no question.
    """
    from brainworker.graph import queries

    provider = FakeProvider({"intencion": "busqueda", "conceptos": [], "motivo": ""})
    mod.plan(provider, question())

    sent = json.loads(provider.prompts[0])
    offered = {t["id"] for t in sent["catalogo"]}
    assert offered == {t.id for t in queries.TEMPLATES if t.planner_visible}
    assert offered, "an empty catalogue would pass the equality above too"


def test_the_planner_is_not_shown_a_template_it_may_not_choose():
    from brainworker.graph import queries

    provider = FakeProvider({"intencion": "busqueda", "conceptos": [], "motivo": ""})
    mod.plan(provider, question())

    sent = json.loads(provider.prompts[0])
    hidden = {t.id for t in queries.TEMPLATES if not t.planner_visible}
    assert hidden, "this test is vacuous if nothing is hidden"
    assert hidden.isdisjoint({t["id"] for t in sent["catalogo"]})


def test_planning_spend_is_attributed_to_its_own_stage():
    provider = FakeProvider({"intencion": "busqueda", "conceptos": [], "motivo": ""})
    plan = mod.plan(provider, question())
    assert plan.spend is not None
    assert plan.spend.stage == "planning"
    assert plan.spend.input_tokens == 300
