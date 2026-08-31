# Integrate `docaget` into Yorch ingestion

## Context

`docaget/` and `worker/` already share code — `extract`, `chunk`, `rules`,
`profiles`, `correct`, `bm25` and `qdrant` are all imported by the worker's
activities, lazily, inside activity bodies. What is **not** shared is the
engine's tail: `build_evalset`, `evaluate`, the tune loop and `persist`. The
worker's activity sequence is already the engine's node order with the gate
inserted and those four missing:

```
engine   detect → load_profile → propose/validate → adopt → extract → correct
         → chunk → build_evalset → index → evaluate → tune → persist
worker   stage → register → extract → resolve_profile →[GATE]→ learn_profile
         → correct → chunk_final → project → embed_and_index → semantics → activate
```

The consequence is measurable. `docaget/INFORME_INDEXACION.md` carries
recall@1 / recall@5 / MRR@10 for **30 documents**; the product has none, for any
of its 74. `CLAUDE.md` names that as the top unbuilt item and lists three
decisions that rest on argument because of it — gleaning's default,
`thinking_for("answering")`, and "profile quality is unmeasured".

The second cost is drift. Nine engine defects were fixed 2026-08-28/29, each with
a test; whether the product has a given one depends on when the image was last
built and on whether the worker calls that function at all. Three concrete
divergences are live today, all verified below against the running stack.

**Intended outcome:** an ingest run reports measured retrieval quality for the
document it just indexed, the two code paths stop diverging where they must not,
and a structural profile collision stops a bad index becoming the answerable one.

## Decisions taken (do not re-litigate)

| | Decision |
|---|---|
| Boundary | The engine owns the pipeline through embedding and **upserts through a writer Yorch injects**. It never names a tenant and never derives a point id. |
| First increment | **Measured quality** — eval set + scoring as real stages, scores persisted, recall visible in the API. |
| Tail scope | **Eval + bounded tuning**: tuning opt-in, off by default, ≤1 chunking candidate, its re-embed priced at the gate. |
| Blocking | **Profile collision only.** Other structural problems warn. |

## Three findings that change the plan, each verified on the running stack

**1. The engine and the product embed with different models, and both are 3072
dimensions.** `docagent/vertex.py:66` hard-codes `EMBED_MODEL =
"gemini-embedding-001"`; the live worker reports `gemini-embedding-2`, and
`cost_entry` has 86 billed embedding calls against it for 1.29 M tokens. Qdrant
accepts either without an error and the cosine between two models' vectors means
nothing — the runbook's "log sano, colección que acepta, ranking corrupto". So
**the embedding model is a property of the collection, not of the engine**: the
runner must take an embedder and must define no model constant of its own.

| Collection | Points | Model | Vectors |
|---|---|---|---|
| `brain` (6433) | 5,335 | `gemini-embedding-2` | dense 3072 Cosine + sparse `bm25` idf |
| `docagent_v2` (6333) | 4,064 | `gemini-embedding-001` | identical config |

**2. Nothing prunes the Qdrant tail on a re-index.** `grep prune_tail worker/`
is empty; `removal.py` deletes by filter, and that is the only delete. Point ids
are `point_id(version_id, chunk_index)`, so re-indexing a document whose chunk
count *shrank* leaves `[new_count, old_count)` alive carrying the old chunking's
`char_span`. This is a live defect independent of tuning, and tuning would make
it routine — it is exactly what the uncommitted `docagent/qdrant.py::prune_tail`
fixes for the CLI after a rejected 625-chunk candidate on
`07-LlavesDelPoder-INT.pdf`.

**3. The worker has no embedding cache, and it copied the engine's retry policy
before the engine learned about quota.** `cache` appears nowhere in
`providers/gemini.py`, and `infra/workspace/cache/` has been empty since
2026-08-20 while `workspace/profiles/` holds 23 files — so the profile path is
exercised and the cache path never has been. `gemini.py:36` says "Three attempts,
matching the engine's `MAX_ATTEMPTS`"; the engine has since grown
`RATE_LIMIT_ATTEMPTS = 6` with 2/8/32/60/60 honouring `Retry-After`, recorded
against 127 × 429 stalling a run at `embedding 1/600`. The worker's total
patience is ~7 s against a 60 s quota window, and a paid Temporal retry re-pays
every vector.

## What this does *not* do: call `build_graph`

`docagent.graph`'s `IndexState` holds live dataclasses and the document's bytes,
so it is not a Temporal payload; its checkpointer is one shared
`state/checkpoints.sqlite` (**565 MB** today, because the state includes the
book); the tune loop re-embeds a whole book inside one invocation; every node is
synchronous with no cancellation hook; and all progress goes through `print()`.
The worker keeps orchestrating, because Temporal already gives per-stage retries,
the gate, and a durable history. What crosses is the **stage functions**, behind
a typed seam.

---

## The seam

New: **`docaget/docagent/runner.py`** — the typed boundary. Engine-side, so
`docaget/tests/` covers it and the CLI can adopt it later.

```python
class Embedder(Protocol):            # Yorch supplies; carries the collection's model
    model: str
    dimensions: int
    def embed_many(self, texts, *, task_type, on_done=None) -> list[EmbedResult]: ...

class PointWriter(Protocol):         # Yorch stamps identity; the engine never names a tenant
    model: str
    dimensions: int
    def upsert(self, rows: Sequence[IndexRow]) -> int: ...
    def prune_tail(self, keep: int) -> int: ...

@dataclass(frozen=True)
class IndexRow:
    chunk: Chunk; dense: list[float]; sparse: SparseVector

def index_chunks(chunks, *, embedder, writer, ledger, on_done=None) -> IndexOutcome
def build_evalset(generator, chunks, *, sample, seed) -> list[EvalItem]
def evaluate(embedder, searcher, evalset, *, params, chunks) -> EvalOutcome
def tune_once(baseline, *, evalset, searcher, embedder, params) -> TuneDecision
```

`index_chunks` raises `VectorSpaceMismatch` unless `embedder.model ==
writer.model` and the dimensions agree. That, plus a test asserting `runner.py`
names no model constant, is what stops finding 1 from recurring.

`Searcher` wraps Qdrant and **assigns `tenant_id` and `version_id` after any
caller-supplied filter**, mirroring `answering/retrieve.py:85`'s rule so a
smuggled key loses. `ALLOWED_FILTERS` stays without `tenant_id`.

### Making the engine's stateful directories injectable

New **`docaget/docagent/workspace.py`** holding a frozen `Workspace(profiles,
correct_cache, embed_cache)` with a `Workspace.cwd()` classmethod that reproduces
today's constants. Then an optional root parameter, defaulting to the module
constant, on:

- `profiles.load` / `all_profiles` / `Profile.save` / `Profile.path` (`profiles.py:25`)
- `correct.correct_paragraphs` (`correct.py:41`)
- `vertex`'s `_embed_cache_read/_embed_cache_write`, promoted to a public
  `docagent/embedcache.py` with an explicit root (`vertex.py:129`)

A parameter, not `os.chdir` and not a module setter, because the worker runs
activities concurrently and both alternatives are process-global. The CLI keeps
`Workspace.cwd()` and is unchanged.

**This closes a gap `CLAUDE.md` records as deliberately open**: "Profiles are the
open one … Closing it needs `docagent` to accept an explicit root instead of
resolving from the CWD." The worker then passes
`settings.paths.for_tenant(tenant).profiles`. `for_tenant(LEGACY)` returns the
root itself, so the 23 existing profiles are untouched — the same exemption, for
the same reason, as `_salt()`'s.

**One concurrency fix comes with it.** `correct.py`'s cache is a
read-modify-write of a single `paragraphs.json`, so two concurrent corrections
lose each other's entries silently. Move to one file per key with the
write-then-rename-with-pid pattern `embedcache` already proves, reading the
legacy JSON once if present. The cache is what makes an interrupted correction
keep what it paid for; losing it silently is the failure it exists to prevent.

---

## Increment 1 — the seam and measured quality

### Engine (`docaget/`)

| File | Change |
|---|---|
| `docagent/workspace.py`, `docagent/embedcache.py` | new; see above |
| `docagent/profiles.py`, `correct.py`, `vertex.py` | optional root parameter |
| `docagent/runner.py` | new; the seam above |
| `docagent/evaluate.py` | `measure()` and `noise_floor()` take a `filters: dict[str,str]` instead of only `doc_id`, so the product can scope by `tenant_id`+`version_id`. `doc_id` kept for the CLI. |
| `docagent/qdrant.py` | keep the uncommitted `prune_tail` and its two tests; commit them |

### Worker (`worker/`)

**`brainworker/indexing.py`** (new) — `QdrantWriter`, lifted from
`paid.embed_and_index:444-481` unchanged in behaviour: `point_id(version_id,
chunk.index)` and the fifteen-key payload. Adds `prune_tail(keep)` deleting
`chunk_index >= keep` for this version — **finding 2's fix**, with a test that
indexes 10 chunks, re-indexes 6 and asserts the count is 6.

**`brainworker/providers/adapter.py`** — gains `embed_many`, delegating to
`Provider.embed` so its batch-length and vector-width checks still run, fronted
by `docagent.embedcache`. The current docstring says embed is absent "so a second
path cannot bypass the checks"; delegation preserves them, and the reason to
reverse the decision is finding 3. Update the docstring to say so.

**`brainworker/providers/gemini.py`** — port the engine's quota policy:
`RATE_LIMIT_ATTEMPTS = 6`, backoff 2/8/32/60/60, honour `Retry-After`.
`_classify` already separates `provider_quota`; only `_call` changes.

**`brainworker/activities/paid.py`**

- `embed_and_index` becomes: build `QdrantWriter` → `runner.index_chunks(...)`.
  Same points, same ids, same payload, plus the prune and the cache.
- `build_evalset` (new activity) → writes the **`evalset`** artifact — already in
  `artifacts.KINDS` and never yet written, so **no schema change** — returns
  counts and `Spend(stage="evalset")`.
- `evaluate_index` (new activity) → embeds each question as `RETRIEVAL_QUERY`,
  searches through the scoped `Searcher`, writes the **`scores`** artifact (also
  already in `KINDS`), returns `Scores`.
- `persist_profile_scores` (new, free) → writes `scores` + `evalset` back into
  the profile via the explicit root, bumping `revisions`/`learned_at`. The
  artifact is authoritative; the profile copy is what lets a *family* accumulate
  measurement. Guard the read-modify-write with a per-slug lock file —
  `n_persist` has none and two runs of one family would otherwise lose a
  measurement.

**The evalset drop rule must be re-applied here.** `n_load_profile` drops a
reused profile's `evalset` and `scores` when `learned_from` names a different
file; without the same rule in `build_evalset`, book B is scored against book A's
questions and reports a meaningless 0 — the exact failure `doc/CLAUDE.md`
records. One test.

**`brainworker/pipeline.py`** — new `EvalSet` and `Scores` dataclasses;
`IngestResult.scores: Scores | None = None`. Appended and defaulted, per the
arity rule: an absent field takes its default, a changed arity breaks every
history in flight.

**`brainworker/workflows/ingest.py`** — two activities after `embed_and_index`,
both gated on `approved.generate_evalset and approved.embed` (nothing to measure
without an index). Paid retry policy and timeout.

**`brainworker/activities/ingest.py::estimate_cost`** — the existing `evalset`
line (`:794-800`) charges the whole document for one call and 20% of it as
output. The engine makes **one call per sampled chunk** (`DEFAULT_SAMPLE = 40`),
so the model is `sample × (chunk_tokens + SEMANTICS_CALL_OVERHEAD)` in and
`sample × ~30` out. Same class of miss as the one `CLAUDE.md` already records:
per-call overhead scales with call count. Add a second, small `evaluation` line
for the query embeddings (`sample` + the noise queries), because it is a real
charge with no row today.

### Surfacing

- `api/main.py::GET /runs/{id}` — a `scores` block read from `scores.json`,
  reusing the `_semantics_counts` pattern including its rule: **omitted when
  absent, never zeroed**, because zero would be a claim about the index.
- `api/main.py::GET /libraries/{lid}/documents/{did}` — per-version scores via
  the existing `catalog.latest_run_with_artifact(version_id, "scores")`.
- `app/src-tauri/src/control.rs` — `Scores` struct, `#[serde(default)]` on
  `RunState.scores`, `rename_all(serialize = "camelCase")` only.
- `app/src/lib/api.ts` + `ImportScreen` / document detail — render
  recall@1 / recall@5 / MRR@10, dense-only, noise floor, question count. Print
  the noise floor beside recall: a recall figure without it is not interpretable.
- i18n: `gate.stages.evalset` **already exists in both bundles**; new keys are
  needed for the scores block only. Both bundles, or `i18n.test.ts` fails on
  parity.

---

## Increment 2 — bounded tuning

- `StageOptions.tune: bool = False`, appended and defaulted.
- `estimate_cost` gains a `tuning` stage: a second full embedding pass at the
  candidate's chunk count. New i18n key `gate.stages.tuning` in both bundles.
- Workflow: after `evaluate_index`, if `approved.tune` and `scores.recall_at_5 <
  RECALL_TARGET`, one conditional block — `tune_once` → re-chunk → re-index
  through the same writer → re-evaluate → adopt or revert. **No loop**, so
  workflow history stays bounded.
- Revert re-chunks and re-indexes rather than only rewriting the profile: the
  engine's own measured bug was "revert updated the profile but not the
  collection", and `prune_tail` is what makes the revert leave no tail.

**One thing must be said at the gate rather than discovered.** With σ ≈ 0.358 the
bootstrap margin needs ~80 questions to see a +0.040 MRR effect, and
`DEFAULT_SAMPLE` is 40 — so at 40 questions tuning will nearly always, and
correctly, reject. Therefore: when `tune` is on, the eval sample defaults to 80
and the estimate shows the doubled evalset cost. Refusing to act on an effect
indistinguishable from noise is the design working; paying for a round that
*cannot* resolve the effect is not.

---

## Increment 3 — a profile collision blocks activation

`_heading_disagreement` (`activities/ingest.py:932`) already chunks twice —
inherited rules against defaults — and produces a `ProfileWarning` with a
similarity. Today it only warns.

- After `chunk_final`: if the decision was `reused` and the detected chapter
  count disagrees with the inherited guards, **skip `activate_version`**. Every
  projection still runs, artifacts and the reason are kept, and the prior active
  version stays active — absence from `document_active_version` already *is* how
  a superseded version reads, so `document_version.state` needs no new value.
- `IngestResult.state = "blocked_structural"` — a Temporal payload field, free.
- **`run.state` needs one new value.** `run_state_check` allows
  `running|awaiting_approval|succeeded|failed|cancelled`, and encoding a held
  outcome as `failed` is the same lie this repo keeps documenting — `stage` vs
  `state`, `cancelled` vs `failed`, `null` vs `0`. Add `blocked`: **a Prisma
  migration in `../yorch-tauri-backend/prisma/migrations/` plus a bump of
  `REQUIRED_MIGRATION` in `worker/brainworker/catalog/migrations.py:41`.** The
  no-migration fallback is `succeeded` + `error_kind='structural_mismatch'`; it
  is contradictory and is not recommended.
- Two ways out, neither a second seven-day gate: `POST
  /libraries/{lid}/versions/{vid}/activate` — a request handler like removal,
  because the ordering makes it retryable — and the existing reindex with
  `ignore_profile`, which is what `StageOptions.ignore_profile` was added for.
- New error kind `structural_mismatch` in `CONTROL_GUIDANCE`
  (`app/src/lib/api.ts:110`) with its key in both bundles.

---

## Cross-checkout cost

The contract surface is quadruplicated. Anything touching `StageOptions`,
`Estimate`, `GateReport` or `RunState` lands on, in lockstep:

`worker/brainworker/pipeline.py` → `api/main.py` → `app/src-tauri/src/control.rs`
(both serde directions) → `app/src/lib/api.ts` → `app/src/screens/*.tsx` + both
i18n bundles → **`../yorch-tauri-backend/src/runs/dto.ts` and `runs.service.ts`**.

The NestJS plane reimplements `GET /runs/:id` in raw SQL and re-reads
`semantics.json` itself, so the `scores` block needs mirroring there. Its
`StageOptionsDto` already carries all 8 fields while the TypeScript client
carries 5 — so `tune` must be added to both. **That work is in the other checkout
and is out of scope here**; it is listed so it is not discovered late.

## What this deliberately leaves alone

- No corpus-wide reindex. The 70 `preprod` versions keep the index they have.
- The `brain` collection and `gemini-embedding-2` are unchanged. Changing the
  embedding model is re-embedding the collection, not editing a constant.
- The CLI keeps writing `docagent_*` on 6333 and must never be pointed at 6433.
- **The diagnostic sidecar does not become an ingest-time stage.** A `.diag.json`
  is only worth anything when a person has read the document — "un sidecar
  inventado convierte el diagnóstico en un generador de ceros"
  (`doc/RUNBOOK_INDEXACION.md`, 1.2) — so it stays a supervised CLI activity. The
  eval set is what gives the product its own measurement, and it is generated
  from the chunks rather than asserted by a person.
- OCR stays unreachable (`pdf_ocr` is not in `extract/_REGISTRY`) and unfunded.
- Checkpoints and engine logs are not persisted, because the LangGraph is not
  run; Temporal history and the artifact store are the durable record.
- `docagent.graph` and `docagent.cli` stay as they are; the CLI may adopt
  `runner.py` later, and nothing here requires it to.

---

## Verification

Run these in order; each is a real exit code, not a piped `tail`.

```bash
# 1. Engine suite — the seam must not move the port fidelity numbers.
cd docaget && uv sync && uv run pytest -q            # baseline 142 passed, 13 skipped
uv run pytest tests/test_invariants.py::test_inv01_char_span_is_byte_exact -q
uv run pytest tests/test_second_book.py -q           # the evalset drop rule
uv run pytest tests/test_qdrant_removal.py -q        # prune_tail

# 2. Worker suite. 102 tests in tests/activities/ call docagent for real.
cd ../worker && uv sync && uv run pytest -q          # baseline 379 passed, 70 skipped
BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789 uv run pytest -q   # 449 with the stack up

# 3. The image, or none of it reaches a real gate. All three overlays: `dev` or
#    the --build is a no-op, `adc` or every paid stage dies on
#    DefaultCredentialsError. The app applies `adc` conditionally, so a stack
#    rebuilt by hand does not get it.
cd ../infra && docker compose -f docker-compose.yaml -f docker-compose.dev.yaml \
  -f docker-compose.adc.yaml up -d --build api worker
docker exec company-brain-api-1 python -c \
  "from docagent import runner; print(runner.__file__)"

# 4. App gates.
cd ../app && npm run typecheck > /tmp/tsc.log; echo "tsc=$?"
npx vitest run && npm run build
cd src-tauri && export PKG_CONFIG_PATH=~/.local/tauri-sysroot/prefix/usr/lib/x86_64-linux-gnu/pkgconfig
cargo test                                            # no --release
```

**New tests that must fail before the change and pass after** — each written
first and checked by removing the fix:

| Test | Property |
|---|---|
| `test_the_engine_never_chooses_the_embedding_model` | `runner.py` names no model constant; `index_chunks` raises on a model/dimension mismatch |
| `test_a_shrinking_reindex_leaves_no_orphan_points` | 10 chunks then 6 → count is 6, against a real Qdrant |
| `test_a_retry_does_not_re_embed_what_it_already_paid_for` | second `embed_many` over the same texts makes zero API calls |
| `test_a_reused_profile_does_not_score_against_another_book` | evalset dropped when `learned_from` differs |
| `test_the_searcher_cannot_be_asked_outside_its_version` | a smuggled `tenant_id`/`version_id` filter loses |
| `test_evaluation_is_absent_unless_it_was_approved` | no `evalset`/`scores` artifact and no spend without the switch |
| `test_a_collision_leaves_the_prior_version_active` | `document_active_version` unchanged, artifacts kept |
| `test_tuning_reverts_through_the_collection_not_only_the_profile` | after a revert the point count matches the reverted chunking |

**End to end, on the running stack, watched.** One small document through the
gate with `generate_evalset` on, against `acme` — the isolation reference, one
document, deliberately small. Confirm: `evalset.json` and `scores.json` exist
under `runs/<id>/`, `GET /runs/{id}` returns the scores block, the point count
equals the chunk count, `cost_entry` gains an `evalset` row, and the profile on
disk carries the scores. Then re-index the same document with a profile that
yields fewer chunks and confirm the collection shrinks.

Report the measured recall beside the noise floor, and repeat the price caveat:
prices in `ledger.py` and `PRICES_PER_MILLION` are third-party multipliers over
token counts this repository measured.

---

## The original brief, and where this plan departs from it

Kept because the departures are decisions, not omissions.

The brief asked for: a typed docaget runner API replacing the CLI-only
integration boundary; docaget writing version-scoped, tenant-carrying points into
the product collection; a workflow refactor putting the free gate before
docaget's paid stages with opt-in evaluation and tuning; profiles, caches,
checkpoints and logs under a managed tenant-scoped workspace; model work routed
through Yorch's provider/ADC handling; docaget's evidence, ledger, chunks,
profile decisions, evaluation results and diagnostics converted into Yorch
artifacts and run records; an optional diagnostic attachment; and structural
validation failures treated as activation blockers.

| Brief | This plan | Why |
|---|---|---|
| "replacing the CLI-only integration boundary" | The runner **adds the missing tail** | The boundary was never CLI-only: the worker already imports seven engine modules. What is missing is `build_evalset`, `evaluate`, `tune`, `persist`. |
| "docaget must write … points carrying the required tenant/library metadata" | The engine upserts through an **injected writer**; it never names a tenant or derives a point id | The engine has no tenancy concept and must not gain one; the point-id and payload contract stays where `answering/` and `removal.py` already read it. |
| One change | **Three increments**, measured quality first | Each increment is independently shippable and the first is the one that unblocks three deferred decisions. |
| Evaluation *and* tuning as peers | Tuning is opt-in, ≤1 candidate, and raises the sample to 80 when on | At 40 questions the bootstrap margin cannot resolve the effect tuning is looking for. |
| "checkpoints, logs" persisted | Not persisted | The LangGraph is not run; see *What this does not do*. |
| "docaget's … stage ledger … into Yorch artifacts" | The ledger maps onto `cost_entry`, which already exists | The `ledger` artifact kind stays unwritten; Postgres is the durable ledger, and the engine's `costo.json` holds only the last run. |
| Optional diagnostic attachment, sidecar or UI-entered | **Deferred**, with a reason | A sidecar is only worth anything when a person has read the document; the eval set is the product's own measurement. |
| "Treat structural validation failures as activation blockers" | **Profile collision only** blocks; the rest warn | Rule-learning falling back to defaults is the engine's designed behaviour, not a failure — the defaults were themselves measured on a real book. |
