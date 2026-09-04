"""How much evidence and reasoning one question is allowed to spend.

Everything that decides the size of an answering prompt used to be scattered
across two files and unreachable from anywhere a person could click:
``Question.top_k`` on the request, ``CANDIDATE_LIMIT``, ``PER_SECTION`` and
``CLAIMS_PER_CHUNK`` as module constants in ``retrieve.py``, and the reasoning
budget resolved by stage name in ``providers/gemini.py``. They move together or
not at all — a wider ``top_k`` with the per-section cap held down is a recall
ceiling rather than more evidence — so they are one table here, named by the
thing a person actually chooses.

**The wire carries the level, never the numbers.** A client sends ``brief``,
``standard`` or ``thorough``; this module is the only place that says what one
means. That is what keeps a request from asking for 5,000 chunks, and what keeps
the paid plane's fork down to a list of three strings instead of a table needing
a live parity dump to stay honest.

**``standard`` is exactly what this product did before this module existed.**
Every number in that row is the constant it replaced, and
``tests/answering/test_effort.py`` asserts it against the originals rather than
against literals, so merging this changed no behaviour until somebody moved the
control.

Three things effort deliberately does **not** touch:

* ``retrieve.MIN_SCORE``, the dense floor. It is the topicality gate, and a
  sweep on a real index measured ``min_score = 0.50`` scoring best of everything
  tried and being wrong — the noise floor for that index is 0.5153, which is
  what a *wrong* chunk scores. A dial that lowered it would win its own metric
  by admitting exactly what the floor was measured to exclude.
* ``Question.confidence_floor``, which is already its own request field with its
  own meaning.
* ``retrieve.PER_SECTION``, and this one was in the table until it was measured
  out of it. The reasoning that put it there was that a cap of 2 would starve a
  ``top_k`` of 16 — a document would need eight distinct sections to fill the
  slots. **That is not what happens**: ``diversify`` backfills in score order
  when the cap leaves it short, so ``diversify(hits, 2, 16)`` returns 16, not 6.
  The cap is a *diversity* policy, not a volume one, and "thorough" meaning
  "allow three near-identical chunks of one section" is the opposite of what it
  was measured for. It also already has a claimant: a profile's ``retrieval``
  block records a measured, per-version ``per_section``, and the recorded fix
  for that is to have ``retrieve.search`` read it. Two owners arriving at one
  line with no stated precedence is how the profile's measurement stays unread.

Mirrored, as a list of level names only, in
``../yorch-tauri-backend/src/ask/effort.ts``; that repository's
``ask.parity.spec.ts`` shells out to the dump at the bottom of this file.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The levels, widest last. Ordered, because the order is the ladder a UI draws
#: and the property `test_effort.py` asserts the budgets rise along.
EFFORT_LEVELS = ("brief", "standard", "thorough")

#: What a question with no stated level gets. Equal to the behaviour that
#: predates this module, which is what makes adopting it a no-op.
DEFAULT_EFFORT = "standard"


@dataclass(frozen=True)
class Budget:
    """One level's whole allowance, resolved once per question."""

    #: Chunks that reach the answering prompt. The model must read all of them,
    #: and a larger set buys recall at the cost of the attention that makes a
    #: citation accurate — which is why the ladder is short and starts low.
    top_k: int
    #: Candidates fused before diversifying down to `top_k`.
    candidate_limit: int
    #: Rows each leg of the hybrid search fetches before fusion. `SearchOpts`
    #: has carried this as a parameter for a while with no production caller;
    #: this is the first one.
    prefetch_limit: int
    #: Claims attached to any one chunk. They compete with the chunk's own text
    #: for the attention that keeps a citation accurate, so this rises with
    #: `top_k` but more slowly.
    claims_per_chunk: int
    #: Reasoning budget for the answering call, or None to leave whatever
    #: `Gemini.thinking_for("answering")` resolves.
    #:
    #: **None at every level today, and the field is still worth having.** The
    #: rule it enforces is that effort may only ever raise this: answering
    #: reasons by default against a neutral measurement — `config.py` calls it
    #: "a deliberate bias toward caution in the one stage where a wrong output
    #: is worst" — so no level may be less careful than the product was before
    #: the control existed.
    #:
    #: That rule is subtler than it looks, and `thorough` broke it by trying to
    #: obey it. `None` means no `ThinkingConfig` is sent and the model chooses
    #: per question, so it is not a floor a literal sits above — it is a
    #: *dynamic* value a literal can sit below. See the measurement on
    #: `thorough` in `BUDGETS`: any number named here has to be shown to beat
    #: the model's own choice, not merely to look generous.
    thinking_override: int | None = None
    #: How developed the answer should be, appended *below* the six answering
    #: rules — which are not editable from anywhere and win any disagreement
    #: with this. It is the one part of the prompt an organisation may rewrite,
    #: because how much to develop an answer is a house style rather than a fact
    #: about the corpus.
    #:
    #: It exists because the ladder measurably did not move the thing its name
    #: promises. Measured 2026-09-03: evidence 4/8/16 and citations
    #: 4.50/5.50/9.25, with the answer itself stuck between 705 and 1346
    #: characters and `brief` coming out *longer* than `standard` on one
    #: question. Nothing in the prompt mentioned length, so the prose was noise.
    #:
    #: Written as coverage rather than as a word count, deliberately. "Write
    #: more" enlarges the one surface nothing verifies — `answer._verify` checks
    #: that a citation names a retrieved chunk, never that a sentence is
    #: supported — while "develop each point the fragments support" grows only
    #: when the corpus has more to say.
    style: str = ""


BUDGETS: dict[str, Budget] = {
    #: A short factual lookup. Half the evidence, one fewer claim per chunk.
    "brief": Budget(
        top_k=4,
        candidate_limit=20,
        prefetch_limit=50,
        claims_per_chunk=2,
        style=(
            "Responde en pocas frases, directo a lo que se pregunta. Da lo que "
            "los fragmentos sostienen sobre esa pregunta concreta y nada más; "
            "no enumeres todo lo que los fragmentos contienen."
        ),
    ),
    #: Exactly what this product did before this module existed. Do not change a
    #: number here without changing the constant it mirrors; the test asserts
    #: them against each other, not against literals.
    "standard": Budget(
        top_k=8,
        candidate_limit=40,
        prefetch_limit=50,
        claims_per_chunk=3,
        style=(
            "Responde en uno o dos párrafos. Cubre los puntos principales que "
            "los fragmentos sostienen sobre la pregunta, cada uno con su cita."
        ),
    ),
    #: A question spanning several books.
    #:
    #: **`top_k` is 48 because the citation count was measured up the curve, and
    #: it turns.** It shipped at 16, which produced 12.3 verified citations on
    #: "¿Quién fue Jesucristo?" against the real corpus. Three runs per point,
    #: because one run per point had said this saturated at 24 and that was the
    #: outlier — the spread at a single setting is about ±3 citations, which is
    #: wide enough to invent a plateau that is not there:
    #:
    #:   16 → 12.3 citations, $0.044      32 → 17.0, $0.075
    #:   24 → 14.7, $0.060                48 → 19.5, $0.079
    #:                                    64 → 15.0, $0.085
    #:
    #: 64 is not noise — 14 and 16 against 48's 20 and 19 — and it is the
    #: attention dilution `Question.top_k`'s own docstring warned about
    #: arriving: past some width the model reads more and attributes less. So
    #: the ladder stops at the measured peak rather than at the largest number
    #: the clamp allows.
    #:
    #: The prose does *not* get longer with it (about 1,300-1,450 characters at
    #: 48, against 1,605 at 16). What the extra evidence buys is attribution
    #: density, not length, which is what was actually asked for.
    #:
    #: Worth knowing what the wider `top_k` actually buys, because it is not
    #: only "more of the same": `_expand` truncates to `top_k` *total* and
    #: appends graph hits after vector hits, so a full vector result leaves
    #: graph expansion contributing zero chunks — today it runs only to attach
    #: citations and claims. These are the first slots graph expansion has ever
    #: been able to fill.
    #:
    #: **`thinking_override` is None, and that is a measurement rather than a
    #: retreat.** It shipped as 8192 on the reasoning that "thorough" should
    #: think harder, and the A/B this comment used to merely propose was run on
    #: 2026-09-03 against the real corpus — four questions, `thorough`'s own 16
    #: chunks held constant, 8192 against leaving it unset:
    #:
    #:   named 8192     7.00 citations, 3322 output tokens, $0.0403
    #:   model default  7.75 citations, 3582 output tokens, $0.0423
    #:
    #: The default won 3 of 4 and tied the fourth, never losing, and it spent
    #: *more* output on 3 of 4. That is the mechanism the guess got backwards:
    #: leaving this unset sends no `ThinkingConfig` at all, so the model picks
    #: its own budget per question — a **dynamic** value. A literal does not
    #: raise that, it caps it, and 8192 was below what the model chose for a
    #: hard question. So the number meant to buy more care was buying less, and
    #: the only visible symptom was one citation fewer on an answer that still
    #: looked perfectly grounded.
    #:
    #: Four questions is a small sample and the effect is small, so what settles
    #: it is the direction plus the mechanism, not the margin. The field stays —
    #: the plumbing is threaded and inert, the way `SearchOpts.prefetch_limit`
    #: was before anything passed one — and a level that ever wants to name a
    #: budget can, with a number somebody measured.
    "thorough": Budget(
        top_k=48,
        candidate_limit=120,
        prefetch_limit=150,
        claims_per_chunk=4,
        style=(
            "Desarrolla la respuesta: recorre cada punto distinto que los "
            "fragmentos sostengan sobre la pregunta, uno por uno, explicando "
            "qué dice cada uno. Cita con amplitud: cada afirmación lleva la "
            "cita del fragmento del que sale, y un fragmento que sostenga dos "
            "afirmaciones distintas se cita dos veces, una por afirmación. "
            "Recorre todos los fragmentos que digan algo sobre la pregunta, no "
            "solo los primeros; el que no aporte nada lo omites, en vez de "
            "forzarlo. Si los fragmentos discrepan entre sí, dilo y atribuye "
            "cada postura a su fragmento. Extiéndete solo hasta donde los "
            "fragmentos den: si sostienen poco, responde poco."
        ),
    ),
}

#: Ceiling on an explicitly requested `top_k`.
#:
#: The request field was an unbounded int reaching the prompt through no
#: validation at all, so `top_k = 5000` was a well-formed question. Clamped
#: rather than refused, because an over-large value is a caller asking for more
#: than the product offers rather than one asking for something meaningless —
#: and generous against the widest level, so the clamp is a backstop rather than
#: a second, quieter dial. It moved from 32 to 96 when `thorough` went to 48,
#: to keep it that: a clamp equal to the widest level is not a backstop, it is
#: the level's own value written twice.
MAX_TOP_K = 96


def budget_for(effort: str | None) -> Budget:
    """The budget for a level, or the default for anything unrecognised.

    Total on purpose. This is the second of two guards: the control planes
    refuse an unknown level at the edge, where a caller can be told what was
    wrong with the request. By the time a value reaches here it has already been
    through that, so the useful behaviour for a level that is somehow still
    unknown is a good answer rather than a crashed workflow.
    """
    return BUDGETS.get(effort or "", BUDGETS[DEFAULT_EFFORT])


def resolve_top_k(requested: int | None, budget: Budget) -> int:
    """The number of chunks this question actually gets.

    ``None`` means "the level decides", which is what every request from the
    desktop app says. An explicit value still wins — the API has accepted one
    since before levels existed and a test pins that it reaches the workflow —
    but it is clamped to `MAX_TOP_K` and floored at 1, because a zero or a
    negative would search successfully and deliver nothing.
    """
    if requested is None:
        return budget.top_k
    return max(1, min(int(requested), MAX_TOP_K))


def as_json() -> str:
    """Dump the level vocabulary and the request shape, for the TypeScript port.

    Live at test time rather than into a committed fixture, for the reason
    `queries.parity.spec.ts` records about its own: a snapshot only detects
    drift if something forces it to be refreshed, and nothing does.

    The `question_fields` half is not about effort at all. The paid plane
    hand-builds a `Question`-shaped object literal with no named type, and
    Temporal's converter *silently ignores* a key whose name does not match a
    dataclass field — for `tenant_id` that would be a cross-tenant read with no
    error anywhere. Adding a field to that dataclass is exactly when the shape
    is most likely to drift, so the dump carries it.
    """
    import dataclasses
    import json

    from .types import Question

    return json.dumps(
        {
            "levels": list(EFFORT_LEVELS),
            "default": DEFAULT_EFFORT,
            "max_top_k": MAX_TOP_K,
            "max_style_chars": MAX_STYLE_CHARS,
            # The default wording per level. Forked into the paid plane because
            # that plane serves the same settings screen and cannot call Python
            # to fill a text box — and prose is a string, which is exactly what
            # the fork convention here permits and this dump then guards.
            "styles": {name: BUDGETS[name].style for name in EFFORT_LEVELS},
            "question_fields": [
                {
                    "name": f.name,
                    "default": (
                        None
                        if f.default is dataclasses.MISSING
                        else f.default
                    ),
                    "required": (
                        f.default is dataclasses.MISSING
                        and f.default_factory is dataclasses.MISSING
                    ),
                }
                for f in dataclasses.fields(Question)
            ],
        },
        indent=2,
        sort_keys=True,
    )


if __name__ == "__main__":  # pragma: no cover - a dump, not behaviour
    print(as_json())


def effective_style_level(requested: str | None, evidence_count: int) -> str:
    """Which level's *style* an answer with this much evidence should use.

    The search has already run and been paid for by the time this is asked, so
    what steps down is how developed the answer is, never how hard it looked.

    A `thorough` question that retrieved 5 chunks is the case this exists for:
    the level asked for a paragraph per distinct point and the corpus supplied
    barely more than `brief` would have, so answering in `thorough`'s voice
    means padding — and padding is prose no citation backs, which is the one
    surface `answer._verify` cannot check. Stepping down removes the occasion
    instead of forbidding it in wording the model may or may not honour.

    Chosen by the widest level whose own `top_k` the evidence actually reached,
    and **never above the level asked for**: this only ever narrows. Asking for
    `brief` and receiving sixteen chunks is not a reason to write an essay.
    """
    budget = budget_for(requested)
    fits = [
        name
        for name in EFFORT_LEVELS
        if BUDGETS[name].top_k <= evidence_count
        and BUDGETS[name].top_k <= budget.top_k
    ]
    # Nothing reached even the narrowest level's `top_k`, which is a question
    # answered on a handful of chunks however it was asked.
    return fits[-1] if fits else EFFORT_LEVELS[0]


def compose_system(base: str, style: str) -> str:
    """The answering rules with a style appended, and the precedence stated.

    Order and wording are the safety here. The rules go first and the style
    second, and the sentence between them says which wins — because `style` is
    the one part of this prompt an organisation can rewrite, and an edit that
    weakened "do not use general knowledge" would produce a fuller answer that
    is worse grounded, which is the failure that reads as success.
    """
    if not style.strip():
        return base
    return (
        f"{base}\n\n"
        "Sobre la forma de la respuesta. Lo que sigue describe cómo quiere esta "
        "organización que redactes, y no altera ninguna de las reglas de arriba: "
        "ante cualquier discrepancia, mandan las reglas.\n\n"
        f"{style.strip()}"
    )


#: Longest style an organisation may store. Generous for a house style and far
#: short of anything that could bury the rules above it by sheer length.
MAX_STYLE_CHARS = 2000
