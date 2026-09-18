# Recasting a document into another literary genre

Built 2026-09-18. A fifth thing the pipeline can be asked for, beside indexing a
file, a video, a channel and a bucket: take a document this product **already
holds** and write it again as another kind of work — a treatise, an essay, a
commentary, a study, a biography, a history, counsel, a novel, a sermon, a
lecture or a chronicle — consulting the rest of its library as supporting
material and citing everything that material contributes.

Read this before touching `brainworker/transform/`,
`brainworker/activities/transform.py`, `workflows/transform.py`,
`workflows/tracked.py`, `TransformScreen.tsx`, `TransformGateReview.tsx` or the
paid plane's `src/transform/`. It holds the rules that are not visible from any
one file.

**Paid plane and the desktop app only, by decision.** The free plane serves none
of these routes. Recasting a book is the most expensive thing this product can
be asked to do — one bounded generation call per chapter, and the chapter count
is the length of the book — and a self-managed stack has no organisation to
bill. Its download allowlist carries the artifact kind anyway, because that list
is forked and a kind added on one side is a download that works in one mode and
404s in the other. **There is no Angular screen**, and the count at the top of
the root `CLAUDE.md` is why that sentence is written out rather than left
implied: "paid plane" is a statement about one of two backends and says nothing
about how many clients exist.

---

## The shape of it

```
GET  /genres                             the eleven, two modes, four purposes   free
POST /transform                          start a run, park at the first gate    free
GET  /runs/{id}/transform-gate           the quote                              free
GET  /runs/{id}/transform-plan           the outline and the re-quote           free
POST /runs/{id}/transform-approve        answer either gate                     spends
GET  /runs/{id}/artifacts/transform      the finished work                      free
```

Stages, in order:

```
reading      chunks.jsonl → source chapters                   free
probing      the relevance probe that sets the research budget 8 query embeddings
previewing   the first quote                                  free
awaiting_approval                                             free, 7 days
planning     detect the genre, propose → validate → refine    ≤ 4 calls
awaiting_plan_review   the second gate, on by default         free, 7 days
composing    one activity per target chapter                  the bill
writing      assemble transformed.md                          free
done
```

`analysing` and `binding` were in that tuple and are not. Each named work
happening *inside* another stage's single activity — detecting the source genre
inside `planning`, rendering the bibliography inside `writing` — so no
transition could enter them, and a stage nothing can be in is a duration
attributed to its neighbour. `tests/unit/test_stages.py` asserts both directions
now.

---

## Two gates, because one quote could not be honest

The first quote is arithmetic over the source's own character and chapter
counts, which is everything knowable before a model has read it. The outline —
and with it the real chapter count, the real coverage and a quote computed from
them — exists only after `planning`. So `awaiting_approval` buys the four
planning calls and `awaiting_plan_review` buys the composition, which is where
essentially the whole bill is. It is `awaiting_correction_review`'s shape: look
at what the expensive-to-undo step produced before paying for the next one.

**`review_plan` defaults to True**, unlike `StageOptions.review_correction`, and
the difference is the point: the outline determines the entire work.

**Each gate has its own query, its own route, its own payload type and its own
component**, and none of them is the other reassigned. `IngestWorkflow._report`
is assigned once before the first gate and never cleared, so its second gate
serves the first one's pre-correction preview — seen in the real window telling
a reader "Nothing has been paid for yet" over a run that had spent $0.58, above
a chunk count measured before the correction that had changed every offset.
There is nothing here to reassign.

**The projected chapter count is not a prediction, it is a contract.**
`estimate.chapter_cap` is what the high end prices, and `outline.validate`
**enforces** it: a proposal with more chapters is refused and refined against
that as feedback. Same move `channel/estimate.py` makes when it quotes the
longest candidate rather than the likeliest — *"choosing the shortest would quote
a bill the run cannot come in under."*

---

## LangGraph inside an activity, Temporal around it

Two `StateGraph`s, each built and run **entirely inside one activity**.
`workflows/transform.py` imports neither, which is the separation `evaluate.py`
records about its own — *"so the Temporal worker can pick a candidate without
importing this module, which would pull in LangGraph and the whole node set."*

* **`transform/planning.py`** — `detect → propose → validate → (adopt | propose |
  fallback) → budget`, the engine's own propose/validate/refine shape from
  `docagent/graph.py`, down to the `_last` reducers and the `bind` that preserves
  `__name__`. `MAX_REFINE = 3`.
* **`transform/composing.py`** — `research → draft → verify → (revise | settle)`,
  once per chapter. `MAX_REVISIONS = 1`, against planning's 3, because a planning
  call is bounded and small and a composition call is the bill.

**No checkpointer on either.** The engine's CLI compiles with `SqliteSaver`
because it has no other durability; here Temporal is that, at a granularity that
already matches the bill. A second durable record of one run's progress that
Temporal does not know about is a resume from a step it thinks still has to run.

**Every node is `async` and every call goes through `asyncio.to_thread`,** with a
loop-side heartbeat task — the `embed_and_index` pattern, because
`activity.heartbeat` for an `async def` activity only *records* and the loop is
what sends it. The recorded cost of getting this wrong is a worker frozen for 97
minutes and **$9.4539 of a $10.017265 run** charged twice.

**Chapters are composed one at a time, never fanned out.** Each is written
against what the previous ones actually wrote, which a parallel fan-out cannot
see — and the drift that produces is measured in this repository from the other
direction: *207 of 12,196 concepts carry a display name that is not the majority
spelling*, because independent passes over one corpus disagree with themselves
about how to say a word. Sequential also makes the budget arithmetic correct by
construction: one activity, one return, one decrement.

### Four defects the workflow tests found, none of which any other test could

1. **`Failed decoding arguments / NameError: name 'StageEstimate' is not
   defined`.** Temporal decodes an activity's result against its return
   annotation, and resolving `Estimate`'s own `list[StageEstimate]` needs both
   names reachable **inside the workflow sandbox**. The workflow now imports them
   although it names neither in a signature. The failure landed *after* the
   activity had run and been paid for.
2. **`'dict' object has no attribute 'ordinal'`** — in the *test's own double*,
   which had no type annotation. The recorded `'dict' object has no attribute
   'source_path'` from the other direction, and the reason every double here is
   typed: Temporal maps payloads onto parameters **by arity**.
3. **Every refusal recorded as `activity_failed`.** `timed.failure_of` reads
   `cause.type`; `_refuse` was putting the kind only in `details`, the way
   `exporting._refuse` does — which gets away with it because nothing reads its
   run trail. The kind is in both now.
4. **Turning research off at the second gate did nothing.** `_queries_left` was
   derived from the options read *before* that gate and never again, so the one
   switch it exists to offer was inert.

---

## Coherence across a document longer than any context window

Three mechanisms, each doing one job.

**The outline is fixed before any prose is written**, and every chapter is
composed against the *whole* outline — all titles and intents travel in every
composition prompt, which is what stops chapter 12 re-introducing what chapter 2
established.

**A bounded continuity record travels between chapters**, and it travels as an
**artifact reference, not a payload**. `Continuity.tail` is the previous
chapter's last `TAIL_CHARS = 1200` **verbatim** — a summary cannot tell the next
chapter what sentence it is continuing from — and that is customer prose, which
`pipeline.py`'s own first rule forbids in a workflow history persisted for the
namespace's whole retention period. `trimmed()` enforces every cap and a test
asserts the serialised size of a record carried through a synthetic 200-chapter
run, because "it is probably small" is not a bound.

The glossary keeps its **first** rendering of a term rather than its latest,
which is the opposite of the usual newest-wins and is defensible here where the
graph's own first-writer-wins is not: chapters are written in order, so the first
writer of a term is where the term is introduced, while `_MERGE_CONCEPTS` takes
whichever document happened to project first.

**The prose travels as an `ArtifactRef` too.** Each `compose_chapter` reads the
draft, appends its chapter and rewrites it; `write_jsonl` replaces atomically and
`run_artifact` is `ON CONFLICT (run_id, name) DO UPDATE`, so a retried chapter
re-reads the reference describing the state *before* it ran and cannot
double-append. Only the latest reference may be held — each rewrite changes the
sha256, which is what makes a stale one detectable rather than silently wrong.

**Rejected: a whole-document seam pass.** It would re-read and partially rewrite
finished prose, which re-buys the document and can drop citations that already
verified — model output rewriting already-verified text, which is the failure
`answer.SYSTEM`'s fifth rule exists to prevent. Whether the `tail` is enough is
the first thing a real run should be read for.

---

## The research budget

The brief asks for a query limit calculated dynamically from relevance and
forbids a second relevance mechanism. There is no need for one:
`retrieve.search`'s `supported` out-parameter — the count of chunks clearing the
dense floor `MIN_SCORE = 0.60` — is the number, and it is the same signal
`effort.effective_style_level` already uses, measured on the real corpus at 48 of
48 for a broad question, 27 for a middling one and **3** for a narrow one.

`probing` samples up to eight evenly spaced source chapters — evenly, because the
front matter of a book is its least characteristic part — embeds the leading 600
characters of each and runs the *same* dense-only probe, counting only hits from
**other** versions. Eight query embeddings, about $0.000032, and it is booked:
*a stage that spends without a row is exactly how the ledger came to be missing
every question ever asked.*

```
per_purpose = clamp(supported // 8, 2, 30)
budget      = min(per_purpose × purposes, 120, 3 × chapters)
```

**`supported == 0` disables research and the gate says so in words.** Not a
courtesy query: `OffCorpus` read at corpus scale rather than at question scale.

**The counter lives in the workflow.** An activity is *handed* an allowance and
*returns* what it used; only a successful return decrements. A counter read from
a file inside the activity is one two overlapping attempts can both read, which
is the exact shape of the recorded double-spend.

**The allowance is an even share and nothing else.** A slack term was written and
measured out again: with twenty queries over six chapters it left the last two
with **nothing**, because the slack compounds. The even share is already
self-balancing — the workflow subtracts what a chapter *spent*, so an
underspending chapter returns the rest to the pool.

**The source document is excluded by a post-filter, never by a filter
parameter.** `retrieve.ALLOWED_FILTERS` is an equality allowlist whose own
docstring says `tenant_id` *"is deliberately absent, and must stay absent… Two
guards for one property, because this is the property."* Teaching it negation to
buy one convenience is the wrong trade. The honest consequence is stated in the
run's report: `supported` includes the source's own chunks and therefore
**over-states** what other documents supply.

**Queries are deterministic** — sliced from the chapter's own plan and source
text, never generated. No query costs a generation call, and a retried activity
issues byte-identical strings, which `CachedEmbedder` then answers for nothing.

---

## References, citations, and the closing chapter

**The bibliography is rendered by a pure function with no model in the path.** It
is therefore unfabricatable by construction rather than by instruction: every
library entry is a locator a citation carried through `answer._verify`, and every
original reference is a string `reading.references_of` found in the source's own
bytes. `epub.py`'s decision applied to the one part of a generated work a reader
is most entitled to trust.

**Two lists, never merged.** A work the source cited (which this work has never
read) and a work this work actually quoted are different things, and an
alphabetical merge would put them under one heading with nothing saying which is
which. `synthesis.py`'s split applied to provenance.

**An empty list renders its heading and a sentence.** An absent section and
"there were none" are different facts — `/project-summary`'s `available` rule in
prose.

**A paragraph resting only on citations that did not verify is removed from the
prose and kept in the run's report.** Removed, because unlike a synthesis a novel
has nowhere to demote a claim to; kept, because a dropped claim that leaves the
work quietly shorter is its own failure. `invented` and `removed` accumulate
across drafts rather than being replaced by the last one: a chapter whose first
draft invented three citations and whose second did not still had a model invent
three.

**Only citations a surviving paragraph rests on reach the bibliography.** One
listed for a paragraph that was then removed describes nothing in the finished
work.

---

## The genre prompts

`brainworker/transform/genres/`, eleven modules, one per genre, each exporting
`NAME`, `PROMPT_VERSION`, `SYSTEM`, `OUTLINE_HINT`, `CHAPTER_RATIO`,
`EXPANSION`, `BIBLIOGRAPHY` and `validate`. **Independence is asserted, not
intended**: a test parses each module and fails if one imports a sibling.

**No genre carries an invariant.** The rules — never invent a source, mark what
came from the library, write in the source's own language, emit only the work —
live in `transform/rules.py` and are composed **above** every genre, with the
sentence that says they win. A rule repeated in eleven places is a rule that can
be weakened in one, and the weakening would read as a stylistic edit. A test
asserts no genre prompt restates one.

Three layers, `effort.compose_system`'s arrangement with one more:

```
TRANSFORM_RULES  →  "the six rules above govern everything below"
MODE             →  "the genre below shapes the prose and never the rules"
GENRE
```

The middle layer exists because a genre prompt is exactly the prose that would
otherwise read as permission to embellish — "render as lived experience",
"urgency without alarm" — and none of them says so on its own.

**Novel and Sermon carry an extra clause each**, and the tension in Novel is real
and named rather than smoothed: the licence is over *presentation* — staging,
order, pacing, the rhythm of a recorded conversation — and never over *fact*. A
bridge the material does not supply is narrated across ("By the time the accounts
resume, three months had passed"), not filled.

**A genre's count preference is measured against the material.** `base.excess`
exists because an essay that prefers twelve parts cannot have that honoured on a
420,000-character book: twelve chapters would give each one 35,000 characters of
source, which `outline.validate` refuses as more than one call can be written
from. The two rules would then refuse every possible outline and every run of
that genre against a long document would silently fall back.

---

## Coverage is the one mechanical guarantee

"Preserve the source's ideas as strictly as possible" is a sentence in a prompt.
*Every source chunk is assigned to at least one target chapter* is a property,
and it is the only thing that can hold a model to the document it was given.

* **Faithful**: uncovered material is a refusal.
* **Adaptive**: bounded at `MAX_UNCOVERED_FRACTION = 0.25` and reported at the
  gate. Condensing is what the mode is for; discarding a quarter of a document is
  not, and the difference has to be a number or it is not a rule.

After `MAX_REFINE` refused proposals the source's own chapters are adopted, which
covers the document by construction. **Not a failure**: rule learning falls back
to built-in rules for the same reason — *"blocking there would refuse to publish
documents whose only fault is being ordinary."* The run records `fallback: true`
and the gate says so.

---

## The first real run, 2026-09-18

The free half has now run against the real corpus — `preprod`/`lib_teologia`, 91
indexed versions — three times, and it **found three defects nothing in the test
suite could have**. Total spend for the whole exercise: **$0.000366**. Every run
was refused at its first gate, so nothing was composed.

Two documents, both real:

| | `4.-Doctrina-de-la-Regeneración` | `01_RetoDeDios_INT-S` |
|---|---|---|
| characters | 19,105 | 468,714 |
| source chapters | **1** | 43 |
| projected chapters | 1 | 26 |
| supported fragments | 37 | 31 |
| research budget | 3 queries | 6 queries |
| quote | $0.2922 – $0.6424 | **$6.0589 – $9.4133** |

### 1. The probe sampled chapters, and half this corpus has one

`probe_passages` took the opening of up to eight evenly spaced *chapters*. That
is a good spread over eight of them and a terrible one over one — and measured
across the 52 documents whose `chunks.jsonl` is still on disk, **27 have exactly
one detected chapter and 37 are under-sampled**. The worst case was a
**500-passage book probed once, on its first 600 characters**, with the whole
run's research budget derived from that.

The cause is not the probe's: `build_chunks` *consumes* a heading paragraph, so
a document whose headings were never detected is genuinely indexed as one
untitled chapter, and this file's own sibling documents record the heading
defects that produce exactly that. A chapter start is only a passage anyway, so
sampling the finer unit loses nothing and stops depending on a field half the
corpus does not have.

Measured on the same document before and after: **`supported` 5 → 37.**

### 2. The budget's chapter cap was the *source's* count, not the work's

`QUERIES_PER_CHAPTER` caps the budget against "the work's own size", and the work
is the one being *written*. `probe_library` was passing `len(source.chapters)`.
A 400,000-character book whose headings were never detected has **one** source
chapter and **seventeen** target ones, so it got 3 queries where it had earned
51 — a 17x under-budget on precisely the documents the first defect already
served worst. It passes `estimate.projected_chapters` now.

### 3. Every charge was filed under `ask-embedding`

`retrieve.search` writes that stage into the `spend` it appends to, and this
feature passed it through. `ask-embedding` is in `ASK_COST_STAGES`, which
deliberately maps to **no workflow stage** because a question has no pipeline —
so a transformation's query embeddings rendered in the audit view under the
trailing `stage: null` heading, beside charges that belong to nobody. That is
the `evidence` defect exactly, which spent months doing the same thing. Worse,
the `COST_STAGES` entries naming `transform-probe` and `transform-research` were
declared and written by **nothing at all** — the "declared and used by nothing"
shape, found the same way it always is: by running a real one and reading
`cost_entry`.

`research.gather` takes the stage as a parameter now and re-labels, because the
same retrieval is made twice in a run for two different reasons and a ledger
that could not tell them apart would hide which half was expensive. Verified
live: the eight probe rows are filed under `probing`, and the three earlier runs'
rows are still under `ask-embedding` in the catalog as a before-and-after.

**And the zeros in those rows are the cache working.** The second run of the
same document embedded the same eight queries, `CachedEmbedder` answered them
all, and the rows were written carrying **$0.000000** rather than not written —
which is the standing rule that a stage that ran for nothing and a stage that
did not run are different facts.

### What the two runs settled, and what they did not

Settled: the free half works end to end against the real corpus — a real
`chunks.jsonl` is read, the probe measures the real library through the real
dense floor, the estimate is computed and **persisted as an `estimate` artifact**
so the quote is checkable afterwards, the run row is opened before anything that
can fail, and a refused gate is recorded as `cancelled` in the stage it was in.

Not settled: **nothing has been composed.** Every figure in the quotes above
rests on `OUTPUT_PER_CHAR` and `OUTPUT_TOKENS_PER_CHAR`, which are still guesses
— so `$6.06 – $9.41` is the shape of an answer, not one. The first *approved*
run is what turns it into a measurement.

One observation worth carrying into that run: **`QUERIES_PER_SUPPORTED = 8` is
now visibly the binding constant.** The largest book in the library earned 6
queries across 26 chapters — about one per four chapters. Whether that is too
thin is not answerable without composing something.

## What has never run

**Nothing about this has touched Vertex.** Every figure in
`transform/estimate.py` is a guess and each says so in its own docstring. The
whole feature is covered at every layer — 163 worker tests on the pure package,
16 workflow tests with typed doubles, 9 paid-plane parity assertions, 16 in the
two React components — and what none of them can do is spend a dollar and read
the result.

**The first *approved* run is the measurement.** The free half has run (above);
what follows is what only spending can answer. In order:

1. Start a transformation of one real book on `preprod`/`lib_teologia` as
   **Essay**, Faithful, all four purposes. Read the first gate: projected
   chapters, research budget, the range.
2. Approve. Read the **second** gate: the real outline, the coverage figure, what
   has been spent so far. This is the first quote anybody should act on.
3. Approve. Record the actual bill against both quotes and **state the
   direction** — the rule is that an estimate must over-report, and by how much
   is the number worth writing down. `estimate.json` is persisted for exactly
   this.
4. Open `transformed.md` and read it: does it read as one work; are the
   terminology and the argument consistent across chapters; do the seams read as
   transitions; does the bibliography keep the source's own references apart from
   the library's.
5. `grep` the artifact for a string planted in a source row's `overlap` — zero
   hits.
6. Read `transform-report.json`: paragraphs removed and their text, citations
   invented, queries used per chapter. A run where many paragraphs were removed
   is one to distrust, and the count is the only thing that says so.
7. Repeat once as **Novel**, Adaptive, with only Context Enrichment — the
   combination most likely to invent — and read it specifically for facts neither
   the source nor a citation supports.

The constants a real run should move, in the order they are most likely wrong:
`OUTPUT_PER_CHAR` and `OUTPUT_TOKENS_PER_CHAR` (the bill's dominant term),
`REVISION_RATE`, `QUERIES_PER_SUPPORTED`, and every genre's `EXPANSION` and
`CHAPTER_RATIO`.

**Nobody has pressed any of it in the real window.** The screen, both gate
panels and the download are covered by 16 component tests and by nothing that has
looked at them. The recipe is the recorded one: `GDK_BACKEND=x11`,
`xdotool search --name "Company Brain"`, **`xdotool windowactivate <client id>`
first**, click coordinates from `xwininfo` and not from
`xdotool getwindowgeometry` (they disagree by 25×62 px and the failure is
silent), then `mousemove --window` and `xdotool type` with **no** `--window`.

---

## Known limitations, on the record

- **The two gate panels live on the Recast screen, not in the import queue.** A
  transform run carries a `library_id`, so its *row* appears in the queue for
  free — which is the visibility the channel feature was reported for lacking.
  What the queue cannot carry is the panels: its `onDecide` is typed on
  `StageOptions`, and a transformation's two switches are not stage switches.
  `lib/importQueue.ts` therefore skips the gate fetch for this kind rather than
  making one 404 per poll for a panel it could not render.
- **The shared `POST /runs/{id}/approve` must not be used on a transform run.**
  It sends a `StageOptions`-shaped `{}`, which would decode into a
  `TransformOptions` carrying every default and turn research back on for a run
  started with it off. The dedicated route omits the key entirely when the caller
  sends none, so a client that forgets to merge loses nothing.
- **A re-transformation is a second full bill**, and the gate says so. There is
  no cache here, so the analogous failure to the recorded 7.1x re-import
  over-quote would be pretending otherwise.
