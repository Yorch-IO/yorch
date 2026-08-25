# docagent — self-tuning document indexer

A LangGraph agent that learns how to read a *family* of documents, indexes them
into Qdrant with Vertex AI embeddings, measures how good the retrieval actually
is, and remembers what it learned so the next document of the same family is
cheaper and more consistent.

It exists because the Go pipeline in `../sociologia/` works but is single-use:
every rule that made it work — the repeating header pattern, the footer zone, the
heading length guards, the questions-vs-footnotes discriminator — was found by
hand and does not transfer. This agent finds them itself, and **validates them by
measurement rather than by assertion**.

```bash
docker compose -f ../sociologia/docker-compose.yml up -d   # Qdrant on :6333
uv sync

uv run docagent index --dry-run libro.pdf     # learn rules, chunk, spend nothing
uv run docagent index libro.pdf               # + embed, evaluate, tune, persist
uv run docagent query "el cogito cartesiano"  # hybrid retrieval
uv run docagent profiles                      # what it has learned
uv run docagent diag                          # structural diagnostics
```

Reads `API_KEY` and `PROJECT_ID` from `.env`, same as `../sociologia`.

---

## Why validation is adversarial

This is the design's centre of gravity, and it comes from a specific mistake.

While building the Go pipeline, a rule for classifying end-of-chapter review
questions was written and checked with a script that reported *"11 paragraphs,
zero false positives"*. It was wrong. The checking script required a dot after the
leading number; the implementation made the dot optional. The real classifier was
tagging **nine footnotes as review questions**, and it only surfaced when the
classifier's output was printed over the whole document.

So `rules.py` obeys three non-negotiable rules:

1. A proposed rule is applied to the **whole document**, never only to the sample
   it was derived from.
2. Validation demands an **independent signal**, not a second pass of the same
   logic. A `preguntas` rule's matches must also read as questions by a different
   measure (¿ marks, imperative verbs); a `nota` rule's matches must read as
   bibliographic.
3. Degenerate rules are rejected outright — anything firing on <0.3% or >20% of
   paragraphs.

**It earns its keep.** On a lecture PDF the learner proposed heading guards that
produced level-1 chapters numbered `[1, 2, 4, 5, 6, 8, 9, 10, 11, 1991, 13, 15]`.
The numbering-sequence check caught it: `1991` is a year being read as a chapter
number. Nothing in the proposal itself looked wrong.

An earlier version of that same check compared the level-1 heading count against
the level-2 count. It was too weak — at `l1_max=400` the reference book yields 9
chapters and 59 subsections, which passes while being obviously wrong. The check
that works looks at the **numbering sequence**, because a document has exactly one
chapter 3, and duplicates are arithmetic rather than opinion.

## The graph

```
START
 └→ detect          extension + text-layer probe → extractor, fingerprint
     └→ load_profile
         ├ hit  ──────────────────────────────────────→ extract
         └ miss → propose → validate ─┬ pass ────────→ extract
                      ↑               └ fail → refine (≤3) ┘
                                      └ 3 failures → measured defaults ┘
     extract → correct → chunk → build_evalset → index → evaluate
          ├ meets recall target ────────────→ persist → END
          └ below → tune → evaluate (≤3 rounds) ┘
```

Two loops with very different budgets:

| Loop | Adjusts | Cost per iteration | Cap |
|---|---|---|---|
| Rules | header patterns, footer zone, heading guards, `kind` rules | one Flash call | 3 attempts |
| Parameters | `min_score`, `per_section`, chunk size, overlap | re-embedding, ~$0.017 | 3 rounds |

Failing rule learning is **not** a failure: it falls back to the built-in
defaults, which were themselves arrived at by measurement on a real book. A
known-good configuration, not a guess.

State is checkpointed to SQLite so an interrupted run can be resumed with
`--resume <thread-id>`. Each invocation gets a **fresh** thread id — keying the
thread on the filename alone makes every re-run silently replay the previous run's
state on top of the new input, which makes the graph appear to execute twice.

## Correction (`correct` node, on by default for prose)

Fixes accents, spelling, agreement and punctuation before chunking, via
`gemini-2.5-flash`. Skip it with `--no-correct`.

**It is deliberately conservative — it does not rewrite the author's prose.** That
choice rests on a measurement. The Go `fixer` existed to repair intra-word spacing
damage (`y s o`, `sea n uevo`, `se r ecomienda`), and PyMuPDF does not produce it:

| Text | Words | Stray single letters | PUA chars |
|---|---:|---:|---:|
| Go `pdf2txt` | 76,325 | **1,464** | 174 |
| PyMuPDF | 70,613 | **0** | 174 |

With the extraction already clean, correction is editing the author rather than
repairing a converter — and in a theology text, reformulating sentences changes
doctrinal shading and leaves an index that no longer reflects what was written. So
the prompt forbids reformulation, sentence splitting, and touching names, technical
terms, scripture references or figures.

The 174 Private Use Area characters are fixed **deterministically and for free**,
not by the LLM. `U+F02D` turned out to be an em dash opening and closing
parenthetical asides (`una evidente relación —como ha venido sucediendo…`), so
mapping it to a hyphen would have been wrong; the replacement was chosen by reading
its context in all 174 occurrences.

### It cannot lose structure

The Go version sent a chapter as free text and compared paragraph counts
afterwards; when the model merged paragraphs the counts disagreed and commit
54b8a61 settled for using the output anyway. Here the model returns JSON keyed by
paragraph index, so a merge is structurally impossible rather than detected late. A
paragraph the model omits, or returns under a hallucinated index, comes back
unchanged.

### Every correction is verified and can be rejected

`correct.verify()` is deterministic, and its job is to reject:

| Check | Rejects when |
|---|---|
| Proper nouns | a capitalised mid-sentence token of 4+ chars disappears (`Dooyeweerd`, `Kierkegaard`) |
| Scripture | a reference like `Gén. 2:15` is altered or lost |
| Figures | any run of 2+ digits changes (years, verse numbers, footnote markers) |
| Length | the paragraph moves by more than ±25% |
| Empty | the model returned nothing |

Names are compared accent-folded, so restoring `Nicolas` → `Nicolás` counts as a
fix rather than a lost name. Sentence-initial words are excluded, because they are
capitalised grammatically. A paragraph that fails any check **keeps its original
text** — the model proposes, it does not get the last word.

### `char_span` points at the corrected file

Correction changes the text's length, so it runs **before** chunking and the
corrected text is written to `<name>.corrected.txt`. That file, not the PDF, is
what `char_span` indexes, and the payload records both paths:

```json
"source_file":    "libro.pdf",
"corrected_file": "libro.pdf.corrected.txt",
"char_span":      [10434, 12286]
```

Invariant #1 stays auditable — open the corrected file in binary and the slice
matches — and there is a record of exactly what the model changed.

**Never applied to structured sources.** Excel, CSV and PPTX skip the node:
"correcting" a spreadsheet's cells would corrupt data, not improve writing.

Measured on 40 real paragraphs (54,040 chars): **30 corrected, 10 unchanged, 0
rejected, $0.0176** — scaling to roughly **$0.145** for the whole book, about 9×
the embedding cost, because output tokens are billed at $1.25/1M against input's
$0.15. Corrections are cached per paragraph, so re-indexing does not pay twice.

Representative changes, all conservative: `ésta.` → `esta.` (the RAE dropped that
accent), `aún` → `aun`, `intentando` → `intentado`, `su` → `sus`, plus missing
commas.

## CLI reference

```
docagent index <file>...    learn rules if needed, correct, chunk, embed, evaluate
docagent query "pregunta"   hybrid retrieval over the corpus
docagent profiles           what the agent has learned so far
docagent diag               structural diagnostics, no ground truth needed
```

### `index`

| Flag | Default | Effect |
|---|---|---|
| `--collection` | `docagent` | Qdrant collection |
| `--qdrant` | `http://localhost:6333` | Qdrant REST base URL |
| `--recreate` | off | Drop the collection first. Needed after a chunking or schema change |
| `--dry-run` | off | Extract, learn rules and chunk; spend nothing on embeddings. **Not free** if the document has no profile yet — rule learning is Flash calls (~$0.0006) |
| `--no-correct` | off | Skip the orthographic pass. Correction is on by default for prose |
| `--ocr-confirm` | off | Authorise paid OCR above $0.05 |
| `--eval-sample` | `40` | Synthetic questions. **Under ~80 the loop cannot detect small effects** — see below |
| `--force-tune` | off | Keep tuning even when the recall target is met. Without it the loop is unreachable on a document that already scores well |
| `--workers` | `6` | Concurrent embedding requests |
| `--resume` | — | Resume an interrupted run by its thread id, printed in the run header |
| `--no-checkpoint` | off | Disable the SQLite checkpointer |

### `query`

| Flag | Default | Effect |
|---|---|---|
| `-k` | `5` | Results to show |
| `--min-score` | `0.60` | Cosine floor on the dense leg; also gates off-topic queries |
| `--per-section` | `2` | Max results from one section |
| `--kind` | all | `cuerpo` / `preguntas` / `nota` / `tabla_fila` / `tabla_resumen` / `diapositiva` |
| `--doc` | all | Restrict to one `doc_id` |
| `--dense-only` | off | Skip the BM25 leg |

## Running against a flaky API

A book takes 19 sequential correction calls over tens of minutes, and that exposed
failure modes a short run never shows. All three were found by watching a real run
stall, not by reasoning about the code.

**Split timeouts, not one blanket value.** A single `timeout=300` applies to
connect, read, write *and* pool acquisition — 300s is absurd for a connect. Now:
connect 10s, read 240s, write 60s. The read budget is set from measurement, not
taste: a 22,946-character correction batch takes **55.8s**, so 240s is roughly 4×
headroom while still finite.

**A retry must not reuse the failed connection.** A run would complete 10-14 calls
and then hang on an `ESTABLISHED` socket the server had quietly stopped answering.
Retrying alone did not help: the retry came 2 seconds later, so the connection was
not idle long enough for `keepalive_expiry` to drop it, and httpx handed back the
same dead socket three times. `_reset_client()` now rebuilds the pool before every
transport retry.

**Silence is indistinguishable from work.** Three read timeouts at 150s each is
450 seconds during which the process sits at 0% CPU looking healthy. Retries now
print to stderr, and the correction pass reports progress per batch.

**The correction cache is written per batch, not at the end.** An interrupted run
used to throw away everything it had already paid for — observed after killing a
run at batch 14 of 19, with the cache still holding only the first 40 paragraphs.

Even with all of this, ReadTimeouts still happen: **3 retries across 19 batches**
on the reference book, all recovered. They are transient and server-side.

## Profiles: the memory that compounds

`profiles/<slug>.json` holds the learned rules, the chunking and retrieval
parameters, the scores they achieved, and the eval set. The key is a
**fingerprint** built from stable structural signals — extractor, normalised
repeating headers, page geometry bucketed coarsely, heading-numbering depth — and
deliberately *not* from page count, filename or word counts, which differ between
siblings and would defeat reuse.

Measured on this repo:

| Run | Learning calls | Cost |
|---|---|---|
| First pass over `Conferencia…pdf` | 2 propose + 2 validate | $0.0013 |
| Second pass over the same file | **0** | **$0.000000** |
| `La palabra.pdf` after `La comunicación.pdf` | **0** — same fingerprint | **$0.000000** |

The two Lingüística lectures share a fingerprint, so the second one inherited the
first's profile without a single LLM call.

## Inherited invariants

Fourteen rules, each a mistake already paid for in the Go implementation, each
with a test in `tests/test_invariants.py`.

| # | Invariant | Why |
|---|---|---|
| 1 | `chunk.text` is a byte-exact slice; `char_span` holds **byte** offsets | Makes the payload checkable against the source. A first audit read the file as text and reported 0/5 matches — the offsets were right, the audit was wrong |
| 2 | `max_embed_chars > hard_cap_chars` | Otherwise near-cap chunks lose their overlap: 63 of 323 did |
| 3 | A chunk never spans a heading nor mixes `kind` | Footnotes contaminating prose |
| 4 | The breadcrumb rides inside `content`, never a `title` field | `title` is only reliable on the older `text-embedding-*` models |
| 5 | `RETRIEVAL_DOCUMENT` indexing, `RETRIEVAL_QUERY` querying | The model embeds the two asymmetrically on purpose |
| 6 | One instance per embedding request | `gemini-embedding-001`'s hard limit; throughput comes from concurrency |
| 7 | Cost from `statistics.token_count` / `usageMetadata` | The chars/4 heuristic overshot the measured count by 11% |
| 8 | `min_score` only reaches the dense prefetch | RRF returns reciprocal ranks (1.0 / 0.333 / 0.25), not cosines |
| 9 | Off-topic queries gated by the dense leg | An off-topic query scored **5.58** on BM25, above `Gén. 2:15` at **3.82** — no lexical threshold separates them |
| 10 | Deterministic UUIDv5 point ids | Re-running overwrites instead of duplicating |
| 11 | Questions reset the section path; footnotes do not | Questions belong to the chapter; footnotes annotate their section |
| 12 | Questions vs footnotes: **the dot after the number** | The nine-false-positive bug |
| 13 | Named vectors are an incompatible schema change | Switching to hybrid needs `--recreate` |
| 14 | The IDF lives in Qdrant (`modifier: "idf"`) | Nothing to keep in sync with the collection |

## Extractors

Two shapes come out, and the difference is honest rather than incidental.

**Text-like** — PDF, DOCX, TXT, MD — produce a linear byte stream of
blank-line-separated paragraphs, so `char_span` is a byte-exact slice.

**Structured** — Excel, CSV, PPTX — produce chunks directly. A spreadsheet has no
byte stream worth slicing, so the verifiable anchor is `cell_ref`
(`Ventas 2025!A41:D60`), checkable by reopening the workbook.

| Extractor | Notes |
|---|---|
| `pdf_text` | PyMuPDF line bboxes, so no hand-grouping of glyphs by rounded Y |
| `pdf_ocr` | Renders at 200 DPI, transcribes via Flash, caches by page hash |
| `excel` | `data_only=True`, merged headers, multiple tables per sheet |
| `docx_` | Word heading styles, so no numbering heuristics needed |
| `pptx_` | Slide title as breadcrumb, speaker notes included |
| `csv_` | Dialect sniffed; semicolons are common in Spanish exports |

### The paragraph-detection trap

Paragraph breaks come from the **line pitch** (top-of-line to top-of-line), not
the gap between bounding boxes. Measured on the reference book, the inter-bbox gap
has a median of **2.48 points** and swings 5% between adjacent lines, so a 1.5×
threshold on it fires constantly: that produced **1064 paragraphs** where the
document has 330. The pitch has a median of **15.84 points**, and its 1.5×
threshold of 23.8 is crossed by exactly 2 of 39 gaps on a sample page — the page's
two real paragraph breaks. Fixed, the extractor produces **377 paragraphs against
the Go pipeline's 330**, with near-identical length distributions.

Also worth naming: PyMuPDF's origin is **top-left with y growing downward**, the
inverse of the PDF-native coordinates the Go code used. The footer zone is
`y > height × (1 − cutoff)` here, not `y < height × cutoff`.

### Excel: the three traps

* **Formulas.** `openpyxl` returns `"=SUM(A1:A9)"` unless opened with
  `data_only=True`. But a workbook never opened by Excel has no cached values
  either, and those cells then read as empty and vanish from the index. The
  extractor opens the file **twice** — once for formulas, once for values — and
  reports the difference, because silent data loss deserves a second parse.
* **Merged cells.** A header merged across columns holds its text only in the
  top-left cell. Merged ranges are propagated, so `Métricas` reaches both metric
  columns instead of one being named from a blank.
* **Several tables per sheet**, split on fully empty rows, each with its own
  header.

Every row window repeats the header. That is the whole point: a retrieved window
of numbers is unreadable without its column names, and the embedding needs them to
place the values semantically.

## Measured results

`../sociologia/Conferencia de Cultura, Sociedad y Cristianismo - Unificada.pdf`,
175 pages, full run with correction and a 90-question synthetic eval:

| | |
|---|---|
| Rules learned | `header_patterns`, `heading_guards`, `question_pattern` adopted; `footnote_pattern` dropped to defaults |
| Extraction | 444,702 bytes → 377 paragraphs → 380 chunks |
| Kinds | `cuerpo` 343 · `preguntas` 35 · `nota` 2 |
| Correction | 225 corrected · 152 unchanged · **5 rejected** · 0 undelivered |
| Rejections | `proper_noun` 2 · `numbers` 2 · `scripture` 1 |
| Question chunks with a wrong section path | **0** |
| recall@1 / recall@5 / MRR@10 | 0.689 / **0.922** / 0.791 |
| recall@5, dense-only | 0.922 — identical |
| Noise floor | 0.586 |
| **Total cost** | **$0.0543** |

Cost breakdown, all from measured token counts:

| Stage | Calls | Input | Output | USD |
|---|---:|---:|---:|---:|
| correct | 9 | 19,034 | 15,966 | 0.0228 |
| evalset | 90 | 53,986 | 4,825 | 0.0141 |
| embed | 380 | 108,939 | — | 0.0163 |
| eval queries + noise | 94 | 2,945 | — | 0.0004 |

**Correction cost $0.023, not the $0.145 estimated.** Only 9 of 19 batches needed
the API — 308 paragraphs came from the per-paragraph cache, because most body text
is byte-identical whether or not the running header was stripped alongside it.
The cache paid for itself across the re-runs it took to get the header handling
right.

**The 5 rejections are the safeguard earning its place**: two corrections dropped a
proper noun, two altered a figure, one touched a scripture reference. All five kept
their original text.

Three honest readings:

**recall@5 = 0.922 meets the 0.85 target**, but 0.85 was a chosen threshold with no
measured baseline behind it — the Go work never measured recall at all.

**Hybrid and dense-only scored identically.** On *these* questions the lexical leg
adds nothing. That does not contradict the Go finding that hybrid took exact-term
recall from 3/4 to 4/4: paraphrased synthetic questions are not exact-term queries.
The two suites measure different things, which is why both exist.

**An earlier run of the same book scored recall@5 0.800 at a third of the cost**,
and the difference is not the correction — it is that the earlier run failed to
strip the running header, leaving 552 paragraphs and 135 chunks carrying header
noise. See the partial-adoption bug below.

## The synthetic eval, and its bias

For a stratified sample of chunks, Flash writes a question that only that passage
answers; retrieval is then scored on whether it brings that chunk back. No human
labelling, cents of cost.

**The bias, stated rather than buried:** a question generated from a chunk inherits
that chunk's vocabulary, which flatters lexical retrieval — the eval would reward
hybrid search partly for a similarity the eval itself created. Mitigations, in
increasing order of usefulness:

1. The prompt demands paraphrase and forbids reusing distinctive terms.
2. Neighbouring chunks are named as distractors, and questions the model flags as
   ambiguous are dropped.
3. **Every report gives dense-only and hybrid side by side.** If hybrid's
   advantage here is much larger than in the structural diagnostics, that gap *is*
   the leakage — visible instead of hidden.

The eval set is stored in the profile and reused across tuning rounds. Comparing
rounds on freshly generated questions would measure the questions, not the change.

### Questions are anchored to byte offsets, not chunk indices

Each `EvalItem` records the **byte midpoint** of the passage it came from, and a
retrieved chunk counts as a hit when its `char_span` contains that offset.

This is load-bearing for chunk tuning. Changing the target size or the overlap
renumbers every chunk, so an eval set keyed by `chunk_index` would silently start
scoring *different passages* the moment the loop touched chunking — and the loop
would optimise against that noise, reporting improvements that are pure
measurement artefacts. `tests/test_eval_anchoring.py` demonstrates the failure
rather than only asserting the fix: at `target_chars=600` the chunk that inherits
index 200 is a different region of the file entirely.

Structured sources have no byte stream, so they anchor on `cell_ref` instead.

### What verifying the tuning loop actually found

The plan's last verification was "sabotage the overlap to 0 and prove the loop
recovers it". It never recovered it — and finding out why turned up three bugs and
a statistical limit, each of which had been invisible in logs that looked healthy.

| Attempt | What happened | Fix |
|---|---|---|
| 1 | The loop **never ran**: with `overlap=0` recall@5 was 0.875 against a 0.85 target, so the gate went straight to persist | `--force-tune`, without which the loop is unreachable on any document that scores well — i.e. untestable |
| 2 | Loop ran, 15 candidates, all rejected. `overlap=300` gave recall@5 **0.875 — identical** to overlap=0 | Objective changed from recall@5 to **MRR@10**: recall@5 is binary at a generous k and cannot see a passage moving from rank 4 to 2. MRR@10 moved 0.708 → 0.755 for the same change |
| 3 | Baselines drifted 0.729 → 0.762 → 0.700 across rounds that had each reverted | Reverting updated the profile but **not the collection**, so the next round measured a rejected configuration's index. A revert now routes back through `chunk` |
| 4 | Baselines still drifted, and the eval sets were 88, 90, 88, 86, 88 questions | The eval set was **regenerated every round**: the node consulted only the profile, whose copy is written at persist. It now reuses what is in state |

**And a limit that is not a bug.** With the objective fixed, the overlap effect is
real but small: **+0.040 MRR@10**, against a bootstrap margin of **±0.073** at 24
questions. The margin scales as σ/√n with σ ≈ 0.358:

| Questions | Margin | Detects +0.040? |
|---:|---:|---|
| 24 | 0.073 | no |
| 60 | 0.046 | no |
| 90 | 0.038 | yes |

**About 80 questions is the minimum** to detect it. Below that the loop *cannot*
recover the sabotage, and refusing to act on an effect indistinguishable from noise
is the design working rather than failing — the failure mode it prevents is
adopting noise as progress. The cost is that small real effects need a bigger eval.

`tests/test_tuning_loop.py` pins all four down without touching the API.

### The two-tier tuning loop

Retrieval knobs (`min_score`, `per_section`, dense vs hybrid) are **free** to
try — the index does not change — so they are exhausted first, all of them, every
round. Only when none of them beats the noise margin does the loop spend a
chunking candidate, because re-chunking means re-embedding every chunk. So exactly
one chunking candidate is proposed per round, the graph loops back through
`chunk → index → evaluate`, and `evaluate` then **judges it against the recorded
baseline and reverts it if it did not win**. A candidate is never adopted merely
for having been tried.

## OCR

Detected by a missing text layer, rendered at 200 DPI, transcribed by Flash
through the same `x-goog-api-key` path as everything else.

**Measured: $0.00068 per page** (2,597 input + 233 output tokens on a dense page).
That is roughly 8× the cost of embedding this project's entire 175-page book, so
it is gated: the estimate is printed and `--ocr-confirm` is required above
$0.05. Transcriptions cache by page-image hash, so a tuning loop never pays twice.

## Known limitations

- **Rule learning succeeds on the header pattern and heading guards, and usually
  fails on the `kind` patterns**, falling back to the measured defaults. On raw PDF
  extraction, footnotes get merged into body paragraphs by the gap detection, so
  there often is no clean separable footnote class to learn. The fallback is
  correct behaviour, but it means the `kind` taxonomy is currently carried by the
  built-in rules more often than by learned ones.
- The BM25 tokenizer drops tokens under 3 characters, so `"Gén. 2:15"` reduces to
  `gen`. Chapter-and-verse numbers are not lexically searchable.
- `recall@5 = 0.85` is a chosen target with no measured baseline behind it.
- **The default `--eval-sample 40` is underpowered for tuning.** The bootstrap
  margin scales as σ/√n, and with σ ≈ 0.358 it takes about **80 questions** to
  detect a +0.040 MRR effect. Below that the loop correctly refuses to act, but it
  also cannot find real small gains. Use `--eval-sample 90` when tuning matters.
- The prices in `ledger.py` come from third-party aggregators, not Google's own
  pricing page. **Token counts are measured; the multipliers are second-hand.**
  Verify against GCP billing before trusting an absolute figure.
- PyMuPDF is AGPL (dual-licensed). Fine for private use; the MIT alternative is
  `pdfplumber`, slower with the same bbox information.
- **The graph's log is printed only when the run ends**, because nodes return their
  lines in state. Live progress exists for the phases that take minutes —
  correction batches, eval-question generation, embedding, and retries — but the
  structural log (`adopt:`, `chunk:`, `evaluate:`) only appears at the end. A stall
  detector watching log growth fired a false positive on exactly this: the run was
  healthy and mid-embedding, with five worker sockets open, while the log had not
  moved in 703 seconds.
- Correction batches that exhaust their retries leave those paragraphs uncorrected
  and report it as `no devueltos`. On the reference book that was 56 of 552.

## Tests

```bash
uv run pytest -q      # 105 tests, ~0.5s, no network
```

Two are worth calling out, and both cost nothing to run:

**`test_port_fidelity.py`** chunks the already-corrected reference text and demands
**exactly 328 chunks, kinds 309/10/9, byte-exact spans, and the same size
distribution** as the Go implementation. If the port drifts, it says so before
anything is spent.

**`test_tuning_loop.py`** pins the four state bugs that only appeared when the
loop was run for real: a regenerated eval set, a revert that left the collection
out of step with the profile, an unreachable loop, and a saturated objective. Each
one had produced perfectly healthy-looking logs while invalidating every comparison
the loop made.
