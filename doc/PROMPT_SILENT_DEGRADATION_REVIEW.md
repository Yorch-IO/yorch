# Review: silent degradation in the indexing pipeline

Audit this repository's indexing pipeline for **defects that degrade quality
without failing**, and fix the ones the evidence supports.

## What counts as a finding

A defect of this class meets **all four**:

1. **It can fail without raising an exception.**
2. **The test suite passes with the defect present.**
3. **The only symptom is worse quality** — worse recall, wrong metadata, junk
   text paid for as an embedding — never a visible error.
4. **There is a concrete input that triggers it, and you can name it.**

**Point 4 is the filter, and it is not negotiable.** "This could be wrong" is not
a finding; "the line `2. Ibídem.` passes all four guards in `heading_level()` and
becomes a chapter" is. A finding with no named trigger does not get reported, however
plausible it sounds. Five findings with a named trigger beat thirty
speculations: the scope of this review is deliberately wide, and without this
filter wide turns into noise.

## Scope

The whole indexing path: `extract/` → correction → chunking → embedding →
Qdrant → graph projection. In `docaget/docagent/` (the engine; the package is
`docagent`, the directory is `docaget`, and that typo is load-bearing — every
path uses `docaget/`) and in `worker/brainworker/`.

## Method

**Discover by reading. Never claim without verifying.**

That distinction comes from a real investigation in this repo on 2026-09-03, and
it is why this prompt exists: the hypothesis arrived at by reading — "a long
enumeration dilutes the chunk's embedding" — turned out to be **false when
measured**. The enumeration-heavy chunk represented its own opening sentence
*better* than an ordinary chunk did (0.848 against 0.806). Reading produced a
convincing, wrong hypothesis; measuring found the real defect, which was a
different one.

So: read to find candidates, and before reporting any of them, **measure it
against the real data when measuring is cheap**. When it is not, write "no
measurement" explicitly — this repository's convention is that every claim
carries its measurement, and that a missing measurement is stated rather than
hidden.

## The worked example

One confirmed defect of this class, to calibrate what a good one looks like:

`docagent/chunk.py::heading_level()` decides whether a line is a heading. It has
four guards, each added after a measured failure (table-of-contents line,
numbered review question, more digits than letters, length cap). **It has no
guard for endnote lists.** The line `2. Ibídem.` — 10 characters, more letters
than digits, no question mark, not a TOC line — passes all four and reads as a
level-1 chapter.

Measured consequence: in `05-CodigoJesus-_int-S.pdf.corrected`, **250 of 277
chunks (90.3%) carry a false chapter**, from 10% of the document through to the
end, over real prose (Gospel narrative labelled `2. Ibídem.`). The book's actual
chapters are `Clave 1`…`Clave 24`. And because the breadcrumb is prepended to
what gets embedded (`Chunk.embed_text()`), removing it from the probe chunk
raised the cosine against its question from 0.5941 to 0.6242 — **+0.0301, for
free**, two thirds of what removing a 48-item enumeration bought.

Nothing failed. No test went red. The only symptom was worse retrieval.
**That defect is already diagnosed: do not report it again, fix it** — and note
that the learned profile does not repair it, because `heading_level` consults
the learned pattern and, when it does not match, *falls through* to the built-in
numbered detector (verified: `2. Ibídem.` is level 1 both with the profile and
without it).

## Before you start

1. **Read `CLAUDE.md` in full.** It has a long "Known defects, not yet fixed"
   section and another one for defects already fixed. **Do not report anything
   already listed there** — several are exactly this class and are recorded on
   purpose. If you find one that is already recorded, say so and move on.
2. **Read `docaget/doc/CLAUDE.md`**, the engine's own document: ~25 design
   decisions, each stated with the measurement that forced it, plus 14 inherited
   invariants. Several guards that look arbitrary are explained there.
3. **Run both suites and note the baseline** before touching anything:
   `cd docaget && uv run pytest -q` and `cd worker && uv run pytest -q`.

## What to deliver

For each finding that survives the filter:

- what fails, at `file:line`;
- the concrete input that triggers it;
- the measurement over real data, or "no measurement" said explicitly;
- how many chunks or books it affects, when that is measurable;
- **the fix applied, with a test that fails without it** (verify this by
  removing the fix and watching the test go red — do not assume it);
- both suites green at the end, compared against the baseline you noted.

Fix only what the evidence supports. A real finding whose fix is a product or
cost decision gets reported without being applied, with the reason.

## Permissions

**You may:** read Qdrant, Memgraph and Postgres; spend on embeddings to measure
(the diagnosis that produced this cost ~10,000 input tokens, fractions of a
cent); write to your scratchpad; modify code and tests.

**You may not:** write to any store; re-index any document (it costs real money
per book and carries consequences in the graph); touch the `brain` collection
with anything that writes.

## Constraints you must not break

- **No test writes to the `brain` collection.** Use a disposable
  `BRAIN_QDRANT_COLLECTION` and drop it. Leftover test points once outnumbered
  real ones 105 to 5 and silently turned eight retrieval tests into skips.
- **`char_span` is byte-exact** (invariant #1) and the offsets are bytes, not
  characters. Any change to extraction or chunking that moves offsets breaks the
  whole index.
- **Chunk `kind` values stay Spanish on the wire** (`cuerpo`, `preguntas`,
  `nota`, `tabla_fila`, `tabla_resumen`, `diapositiva`). Renaming them breaks
  every existing collection.
- **`min_score` is applied to the dense prefetch only**, never to the fused
  output and never to the sparse leg (invariant #8).
- **Never `git checkout`/`restore`/`reset` a file in this repo.** The tree holds
  uncommitted work from several sessions, and restoring that way reverts past
  the last edit; it has destroyed real changes before. If you need to undo
  something, copy it to your scratchpad first.
- **Another session may be working in the same tree.** Run `git status` before
  planning and again before editing.

## Two method traps, already paid for here

- **Do not optimize against an anecdote.** One case is a diagnostic probe, not
  an objective function. If you measure retrieval, measure against an eval set
  and accept a change only when it clears the bootstrap margin.
- **A heuristic detector that does not know it failed reports zero, and zero
  reads as "no problem".** When you review one, ask what it returns when it has
  no basis to judge, and whether that value is distinguishable from a real
  measurement. This repo already has a recorded defect of exactly that shape.
