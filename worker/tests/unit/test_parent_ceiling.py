"""The one decision `scripts/parent_ceiling.py` holds: both sides or neither.

That script decides whether parent-child retrieval is worth a re-chunking, by
comparing a parent's recall against what the same characters buy when spent on
more chunks. Everything else in it is a store read. The comparison itself is
the decision, and it has exactly one way to be silently wrong: reranking one
leg and not the other, which credits the reranker's own gain — measured at
+0.094 at `brief` — to the parent.

That was live. On `ver_13d4bee9f75a6bda50e40568` the broken version turned
every row of the verdict from "plain wins or ties" into "parent wins by +0.05
to +0.075", four times the pooled bootstrap margin, printed with nothing to say
it was wrong. The same data through the fixed version is ties and plain wins.

So `reordered` is the only place either leg may rerank, and this pins both
halves of that: the function behaves, and no caller reaches past it.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "parent_ceiling.py"
sys.path.insert(0, str(SCRIPT.parent))


@dataclass
class _Hit:
    payload: dict


class _Ranked:
    def __init__(self, scores): self.scores, self.queries, self.usd = scores, 1, 0.001


class _Ranker:
    """Scores by the text's own length, so the expected order is obvious."""

    def __init__(self): self.calls = 0

    def rank(self, question, texts):
        self.calls += 1
        return _Ranked([float(len(t)) for t in texts])


def _hits(*texts):
    return [_Hit({"text": t}) for t in texts]


def _spent():
    return {"rank_queries": 0, "rank_usd": 0.0}


# --- the function ------------------------------------------------------------


def test_without_a_ranker_the_list_is_handed_back_untouched():
    """The no-rerank run must be the fused order exactly, or the free
    measurement is not measuring what production serves without a reranker."""
    import parent_ceiling as pc

    hits = _hits("a", "bb", "ccc")
    spent = _spent()
    assert pc.reordered(None, "q", hits, spent) is hits
    assert spent == {"rank_queries": 0, "rank_usd": 0.0}


def test_an_empty_list_is_not_a_billed_call():
    import parent_ceiling as pc

    ranker = _Ranker()
    spent = _spent()
    assert pc.reordered(ranker, "q", [], spent) == []
    assert ranker.calls == 0, "a refused question must not pay to reorder nothing"
    assert spent["rank_usd"] == 0.0


def test_with_a_ranker_the_order_follows_the_scores_and_the_cost_is_booked():
    import parent_ceiling as pc

    spent = _spent()
    out = pc.reordered(_Ranker(), "q", _hits("bb", "a", "cccc"), spent)
    assert [h.payload["text"] for h in out] == ["cccc", "bb", "a"]
    assert spent["rank_queries"] == 1 and spent["rank_usd"] > 0


# --- and that nothing reaches past it ----------------------------------------


def _calls_to(tree: ast.AST, attr: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
    ]


def test_reordered_is_the_only_place_the_script_ranks_anything():
    """A second hand-written rerank is how the legs came apart the first time."""
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "reordered"
    )
    inside = set(_calls_to(fn, "rank"))
    everywhere = set(_calls_to(tree, "rank"))
    assert everywhere == inside and len(inside) == 1, (
        f"ranking happens at lines {sorted(everywhere - inside)} outside "
        f"`reordered`; both legs must rerank or neither may"
    )


def test_both_legs_go_through_it():
    """One call site is not enough: the defect was one leg reranked and one not,
    so the plain-chunk curve and the ceiling must each reach `reordered`."""
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    measure = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "measure"
    )
    uses = [
        n for n in ast.walk(measure)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "reordered"
    ]
    assert len(uses) == 2, (
        f"`measure` reorders at {len(uses)} places; it has two legs — the "
        f"plain-chunk sweep and the per-level ceiling — and comparing a "
        f"reranked one against an unreranked one is the defect this pins"
    )
