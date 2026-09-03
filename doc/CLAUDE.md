# CLAUDE.md

## Project overview

LangGraph agent that learns how to read a *family* of documents, indexes them into
Qdrant via Vertex AI, measures retrieval quality, and persists what it learned.
Ports and generalises the single-purpose Go pipeline in `../sociologia/`.

```
docagent/
  chunk.py      windowing, kinds, byte-exact spans   <- port of sociologia/index/chunk.go
  correct.py    conservative orthographic fixing     <- successor to sociologia/fix/main.go
                + deterministic per-paragraph verification that can reject
  bm25.py       Spanish tokenizer + sparse vectors   <- port of sociologia/index/bm25.go
  vertex.py     embeddings, generation, OCR          <- port of index/embed.go + index/context.go
  qdrant.py     hybrid collection, RRF, gating       <- port of sociologia/index/qdrant.go
  rules.py      propose + adversarial validate       <- NEW: automates the manual iteration
  profiles.py   fingerprint -> learned rules         <- NEW: the memory between runs
  evaluate.py   synthetic eval, bootstrap margin     <- NEW: the improvement signal
  graph.py      StateGraph, nodes, conditional edges
  ledger.py     single measured-cost accumulator
  cli.py        index | query | profiles | diag
  extract/      pdf_text, pdf_ocr, excel, docx_, pptx_, csv_, plain
```

## Build and run

```bash
uv sync                                        # Python 3.13 pinned; ~150 MB venv
docker compose -f ../sociologia/docker-compose.yml up -d
uv run pytest -q                               # 105 tests, no network, ~0.5s
uv run docagent index --dry-run libro.pdf      # spends nothing
uv run docagent index libro.pdf
```

`.env` (working directory) supplies `API_KEY` and `PROJECT_ID`, same as
`../sociologia`. No extra credentials: the express-mode key works for
`gemini-embedding-001:predict`, `gemini-2.5-flash:generateContent`, **and** inline
image data for OCR — all three verified.

## Key design decisions

- **Validation is adversarial, and that is the point of the project.** The Go work
  produced a `kind` rule that a checking script pronounced clean while the real
  implementation mislabelled nine footnotes as review questions; the checking
  script's regex required a dot the implementation made optional. So: rules are
  applied to the whole document, validation demands an *independent* signal rather
  than a second pass of the same logic, and degenerate rules (<0.3% or >20% of
  paragraphs) are rejected. See `rules.py`.
- **The heading-guard check uses the numbering sequence, not counts.** An earlier
  version compared level-1 against level-2 counts and passed `l1_max=400`, which
  yields 9 chapters and 59 subsections on the reference book. Duplicate chapter
  numbers are arithmetic; "too many chapters" is opinion. This check caught a
  proposal producing chapters `[1,2,4,…,1991,13,15]` — a year read as a chapter.
- **Rule-learning failure is not run failure.** After 3 attempts the graph adopts
  the built-in defaults, which were themselves measured on a real book. In
  practice the header pattern and heading guards learn reliably; the `kind`
  patterns usually fall back, because raw PDF extraction merges footnotes into body
  paragraphs and there is often no separable footnote class to learn.
- **Correction is conservative, and that is a measured decision, not timidity.**
  The Go `fixer` existed to repair intra-word spacing (`y s o`, `sea n uevo`): the
  Go extractor left **1,464** stray single letters on the reference book, PyMuPDF
  leaves **0**. With extraction already clean, correction is editing the author
  rather than repairing a converter, so the prompt forbids reformulation, sentence
  splitting, and touching names, technical terms, scripture references or figures.
- **Correction runs before chunking and its output is persisted.** It changes the
  text's length, so `char_span` would be invalid otherwise. `<name>.corrected.txt`
  becomes what `char_span` indexes; the payload carries `corrected_file` alongside
  `source_file`, and invariant #1 is audited against the corrected file.
- **Structured output keyed by paragraph index, not free text with a count check.**
  The Go `fixer` compared paragraph counts after the fact and, when the model merged
  paragraphs, commit 54b8a61 settled for using the output anyway. Here a merge is
  structurally impossible; an omitted or hallucinated-index paragraph comes back
  unchanged.
- **Every correction is verified deterministically and can be rejected**
  (`correct.verify`): lost proper noun, altered scripture reference, changed figure,
  ±25% length swing, or empty → keep the original. Names are compared accent-folded
  so `Nicolas` → `Nicolás` is a fix, not a loss; sentence-initial words are excluded
  because they are capitalised grammatically.
- **Correction never touches structured sources.** `PROSE_EXTRACTORS` gates the
  node: "correcting" a spreadsheet's cells would corrupt data, not improve writing.
- **The 174 PUA characters are fixed deterministically, not by the LLM.** `U+F02D`
  is an em dash opening and closing parenthetical asides in this book — read off its
  context in all 174 occurrences — so mapping it to a hyphen would have been wrong.
- **Paragraph breaks come from the line pitch (y0→y0), never the inter-bbox gap.**
  The bbox gap has a median of 2.48 pt and 5% jitter, so a 1.5× threshold fires
  constantly: 1064 paragraphs where the document has 330. The pitch has a median of
  15.84 pt and behaves. Fixed, extraction gives 377 paragraphs against Go's 330.
- **PyMuPDF y grows downward**, inverse of the PDF-native coordinates the Go code
  used, so the footer test is `y > height*(1-cutoff)`.
- **Text-like vs structured extractors is a real distinction, not a shortcut.** A
  spreadsheet has no byte stream to slice, so its verifiable anchor is `cell_ref`
  rather than `char_span`. `Extracted` enforces exactly one of `text` / `chunks`.
- **Excel is opened twice on purpose.** `data_only=True` gives cached values, but a
  workbook never opened by Excel has none, and those cells then silently vanish.
  The second parse finds formula cells with no value and reports them. Silent data
  loss is worth a second parse.
- **The eval set lives in the profile and is reused across tuning rounds.**
  Regenerating it each round would measure the questions, not the change.
- **Eval items are anchored to byte offsets, not `chunk_index`.** Re-chunking
  renumbers everything, so an index-keyed eval set starts scoring different
  passages the moment chunk tuning runs, and the loop then optimises against
  measurement noise. `EvalItem.char_mid` holds the source passage's byte midpoint
  and a hit is "the returned `char_span` contains it". Structured sources anchor on
  `cell_ref`. `tests/test_eval_anchoring.py` shows the failure, not just the fix.
- **Tuning is two-tier by cost.** Retrieval knobs change nothing in the index, so
  all of them are tried every round. A chunking candidate costs a full re-embed, so
  exactly one is proposed per round; the graph loops `tune → chunk → index →
  evaluate`, and `evaluate` judges it against the recorded baseline and **reverts it
  if it did not beat the margin**. Nothing is adopted for having been tried.
- **Partial rule adoption** (`n_adopt_rules`): `ESSENTIAL_RULES` (`header_patterns`,
  `heading_guards`) block adoption; the `kind` patterns are optional and a failure
  just drops that one. Requiring all four to pass threw away a header pattern that
  validated cleanly three times, leaving 175 running-header lines in the text — 552
  paragraphs instead of 377 and 135 chunks carrying the header as noise.
- **Tuning requires beating a bootstrap noise margin.** On the real run
  `min_score=0.65` offered +0.033 recall@5 (one question in thirty) against a
  ±0.077 margin and was correctly rejected.
- **Every report shows hybrid and dense-only side by side**, because the synthetic
  eval's questions are written from the chunks they must find and therefore leak
  vocabulary to the lexical leg. The gap between the two modes *is* the leakage
  measurement.
- **A fresh checkpointer thread per invocation.** Keying the thread on the filename
  makes every re-run replay the previous run's state over the new input; the graph
  then appears to execute twice, with contaminated numbers. `--resume <thread>` is
  the opt-in.
- **OCR costs $0.00068/page, measured** — ~8× the cost of embedding the whole book —
  so it is gated behind an estimate and `--ocr-confirm` above $0.05, with a
  page-hash cache.
- **`costo.json` is a history of runs, because it used to be one run.**
  `Ledger.dump` was `json.dump` over the whole file, so every invocation erased
  the last one's accounting. That is the mechanism behind
  `INFORME_INDEXACION.md`'s "per-document cost accounting does not exist and is
  not reconstructible": 28 documents had been indexed and the file could account
  for the most recent one. Read on 2026-09-03 it held **$1.335820, the total of
  book 07 alone**, while the corpus behind it had cost several times that.
  It appends now — `run_id`, timestamp, the paths given, and the run's stages —
  and each run stores the price table that produced its own figures, because
  the multipliers changed on 2026-08-20 and a figure that cannot be re-checked
  against the prices of its own day is not a measurement. Three properties are
  what the tests stand on: an old-shaped file *is* a run record and becomes the
  first entry rather than being dropped; a file this cannot parse is **moved
  aside**, never overwritten, because it is somebody's record of money already
  spent; and `run_id`/`at`/`documents` stay `null` for the migrated one, since
  "nobody recorded which documents" is not "this run touched none". The write
  goes through `os.replace`, which was not worth it while the file held one run
  and is worth it now that losing it loses everything.

## Bugs found by running the loop, not by reading it

Four state bugs in the tuning loop, all of which produced healthy-looking logs
while silently invalidating every comparison. Listed because the class of mistake
matters more than the instances: **a measurement loop can be confidently wrong**,
and only an end-to-end run surfaces it.

| Symptom | Cause | Guard now |
|---|---|---|
| Loop never entered | `recall@5 0.875 >= 0.85` target sent it straight to persist | `--force-tune`; `test_force_tune_makes_the_loop_reachable...` |
| Every candidate rejected, `overlap=300` scored identically to `overlap=0` | Objective was `recall@5`, saturated and binary at k=5 | Objective is `MRR@10`; `test_recall_at_5_would_have_been_blind...` |
| Baselines drifted 0.729 → 0.762 → 0.700 across reverting rounds | Revert updated the profile but not the collection | Revert routes back through `chunk`; `test_revert_routes_back_to_chunk...` |
| Baselines still drifted; eval sets were 88, 90, 88, 86, 88 | `n_build_evalset` consulted only `profile.evalset`, written at persist | Reuses `state["evalset"]`; `test_evalset_is_reused_from_state...` |

And a limit that is **not** a bug: with σ ≈ 0.358, the bootstrap margin needs about
**80 questions** to detect the +0.040 MRR effect of restoring the overlap. Below
that the loop cannot recover a sabotaged parameter — and refusing to act on an
effect indistinguishable from noise is the design working.

## Resilience against a flaky API

Long sequential runs (19 correction batches, tens of minutes) exposed failures a
short run never shows:

- **Split timeouts** (`connect=10, read=240, write=60`), not one blanket value that
  also applies to connect and pool acquisition. The read budget comes from
  measurement: a 22,946-char correction batch takes 55.8s.
- **`_reset_client()` before every transport retry.** A run would complete 10-14
  calls then hang on an `ESTABLISHED` socket; the retry 2s later reused the same
  dead connection three times, since `keepalive_expiry` had not elapsed.
- **Retries print to stderr** and correction reports per-batch progress. Three read
  timeouts at 150s each is 450s at 0% CPU that looks exactly like work.
- **The correction cache persists per batch**, so an interrupted run keeps what it
  paid for.

ReadTimeouts still occur — 3 retries across 19 batches on the reference book, all
recovered. They are transient and server-side.

## What a wide audit of the indexing path found, 2026-09-03

Root `CLAUDE.md` carries the whole list and its measurements. What belongs here
is the part that is about *this* engine, and the rules worth not relearning.

- **`heading_level`'s four guards had no fifth for an endnote list, and the
  learned pattern could not repair it.** `2. Ibídem.` is ten characters, more
  letters than digits, no question mark and no dot leaders. The fall-through is
  the load-bearing part: `heading_level` consults the profile's pattern and, when
  it does not match, drops to the built-in numbered detector — so the line read
  as a level-1 chapter both with `05-CodigoJesus`'s own profile and without it.
  The new guard therefore runs *before* the learned patterns, where the review-
  question check already does, and for the same reason.
- **The dot after the number decides questions from footnotes (invariant #12),
  and this corpus has publishers who dot their footnotes too.** That was recorded
  in `classify_kind`'s docstring as reported-and-not-fixed, with a note that the
  obvious fix — requiring ¿/?/an imperative to corroborate — was reverted because
  it left every footnote with no section. Keying on the *vocabulary of a
  citation* instead does not move invariant #11: a citation becomes a `nota`, and
  a `nota` keeps its path. A numbered line that asks something is still left
  alone, because a cited title can carry a question mark.
- **A run under `min_chunk_chars` was discarded, not merged.** The rule looked
  like a junk filter and was not one: of the 158 paragraphs it dropped across the
  corpus, 44 are content appearing once, and of the 114 running headers, 49 of
  the 55 distinct lines already sit inside a chunk elsewhere in the same book. A
  short run travels forward into the chunk it opens — a title belongs to the
  section it opens, not to the one before it — and merges backwards only when
  nothing follows.
- **`_word_spans` returned each span starting on the space it cut at**, and
  `_split_oversized` strips the unit's text without moving its offset, so
  `emit`'s `to = last.offset + len(last.text)` landed one byte short and the
  chunk lost its final character. The invariant #1 test could not see it: it
  compares `src[from:to].strip()`, and the truncated slice still strips to the
  truncated text. Measured: **0 of the corpus's 4,246 chunks reach that path**,
  which needs one sentence over `hard_cap_chars` with no accepted boundary — so
  this was fixed on the shape rather than on a measurement, and stated as such.
  After the fix `src[char_from:char_to]` equals `text` **without stripping** for
  all 4,246, which is a stronger property than the invariant asks for.
- **`_page_rows` broke a baseline tie with the row's own text.** Alphabetical
  order, not reading order: 679 paragraphs across 41 of the 84 PDFs, and
  `Hermeneutica Capitulo 4.pdf.corrected.txt` has shipped with "cada siete años
  se 15:2 perdona toda clase de deudas" ever since. Correction cannot repair
  that, because reformulating is precisely what its prompt forbids — which is a
  general property worth holding on to: **anything extraction scrambles is
  permanent.**
- **`verify` guarded what a correction may not lose and not how much.** ±25% of a
  1,600-character paragraph is four hundred characters. Measured over the 2,684
  corrections in `cache/correct/paragraphs.json` — recovered for free by
  re-extracting each PDF and looking its paragraphs up by cache key, a 98.8% hit
  rate — seven had deleted real content, four of them a negation or a section
  title, and **not one was rejected by the gate as it stood**. `MAX_LOST_CHARS`
  is an absolute floor beside the ratio because the ratio is the wrong shape:
  on "pág 33." → "pág. 33." a single character is 14%.
- **A cache hit skipped `verify` entirely**, so the gate's rules could be
  tightened and the entries written under the old ones would still be applied for
  ever. The cache is the output of a gate that changes; it is verified on read
  now, which costs nothing.
- **`_SCRIPTURE_RE`'s book name was capped at 12 letters**, shorter than
  "Tesalonicenses" (14) and "Lamentaciones" (13) — so the longest book names in
  the canon were the ones the rule could not reach, which is the failure it
  exists to prevent. 17 references, and the wider cap mints 13 extra tokens with
  no junk among them.
- **`Validation.passed` blocked a proposal whose good half `adopt` was keeping.**
  The two heading levels report under one rule name; `heading_levels_ok` exists
  precisely because `failed_rules()` cannot tell them apart, and `adopt` already
  consults it. `passed` did not.

**A hypothesis this refuted, worth not re-running.** `verify` checks that
*capitalised* words survive a correction, so the obvious next question is whether
lowercase content words are being lost. They are — 332 of the 2,684 corrections
lose at least one 5-letter-or-longer word that is not a substring of the result —
and **a guard on it would be wrong**: nearly every one is a genuine spelling
repair (`compartimientos` → `compartimentos`, `eclessia` → `ecclesia`,
`latinamericano` → `latinoamericano`, `ideosincracia` → `idiosincrasia`), which
is what the pass is for. The 19 corrections that changed a number are the same
story: every one repairs an extraction artifact, `p. l6` → `p. 16` and
`Romanos l0:9` → `Romanos 10:9`, so `NUMBER_RE`'s two-digit floor is right rather
than lax.

## Inherited invariants

Fourteen, each a mistake already paid for in `../sociologia`, each with a test in
`tests/test_invariants.py`. The load-bearing ones:

| # | Invariant |
|---|---|
| 1 | `char_span` holds **byte** offsets; slicing the source as text gives character indices and fails for the wrong reason |
| 2 | `max_embed_chars > hard_cap_chars`, enforced in `ChunkRules.__post_init__` |
| 8 | RRF scores are reciprocal ranks, not cosines → `min_score` only on the dense prefetch |
| 9 | Off-topic gating uses the dense leg; an off-topic query scored 5.58 on BM25 vs `Gén. 2:15` at 3.82, so no lexical threshold separates them |
| 11 | Review questions reset the section path; footnotes do not |
| 12 | Questions vs footnotes: the dot after the leading number |
| 14 | The IDF lives in Qdrant via `modifier: "idf"`, not in the stored vectors |

## The cheapest strong test

`tests/test_port_fidelity.py` chunks `../sociologia/output_corrected_peluquiado.txt`
and demands **328 chunks, kinds 309/10/9, 0 mislabelled, byte-exact spans, and the
same size distribution** as the Go implementation. Free to run; it fails the moment
the port drifts.

## Measured baseline

175-page reference PDF, 30-question synthetic eval: 373 chunks, recall@1 0.433,
recall@5 **0.800**, MRR@10 0.596, dense-only recall@5 0.800, noise floor 0.579,
total **$0.0224**. recall@5 is **below** the 0.85 target, which is a chosen
threshold with no measured baseline behind it — the Go work never measured recall.

Profile reuse: second pass over the same file makes **0** LLM calls; the two
Lingüística lectures share a fingerprint, so the second inherits the first's
profile for free.

## Caveats to keep in mind

- Prices in `ledger.py` come from third-party aggregators, not Google's pricing
  page. Token counts are measured; multipliers are second-hand.
- The BM25 tokenizer drops tokens under 3 chars, so `"Gén. 2:15"` reduces to `gen`.
- The parameter loop only explores retrieval knobs; `CHUNK_CANDIDATES` exists in
  `evaluate.py` but is not wired, because each costs a full re-embed.

## Investigation into Heading Detection on a new corpus

When indexing a new book ("1. DESDE AGUSTÍN DE HIPONA HASTA LOS SIETE CONCILIOS ECUMÉNICOS.pdf"), the `heading_guards` validation failed. The detected chapter sequence was `[1, 2, 3, 4, 11, 12, 13, 14, 15, 16, 17]`, which is not a valid run from 1.

### Analysis

The investigation revealed that the review questions at the end of the book (e.g., "11. Defina Arrianismo") were being misclassified as level-1 headings. The existing logic in `chunk.py`'s `heading_level` function checked for question marks (`?` or `¿`) but not for imperative verbs (like "Defina") which are common in review questions. The function was "stealing" these lines and classifying them as headings before the more specific `classify_kind` function had a chance to identify them as `KIND_QUESTIONS`.

### The Fix

To solve this, the `_IMPERATIVES` regex from `rules.py` was added to `chunk.py`, and the `heading_level` function was modified to exclude lines containing these imperative verbs from being considered headings.

```python
# chunk.py: heading_level function
...
    if "¿" in s or "?" in s or _IMPERATIVES.search(s):
        return 0  # numbered review question, not a heading
...
```

### The Result: A Trade-off

With the fix in place, the `heading_guards` validation passed correctly, identifying the 4 chapters in the book. This demonstrates a more robust structural understanding of the document.

However, this structural improvement came at a small, but measurable, cost to the retrieval metrics on the synthetic evaluation set:

*   **recall@5:** 0.950 -> 0.925 (-0.025)
*   **MRR@10:** 0.778 -> 0.747 (-0.031)

This change was within the statistical noise margin (`±0.050` for recall@5). While the principles of the project would suggest reverting a change that doesn't produce a clear win, the user decided to **keep the change**. The reasoning is that the improved structural correctness of the index is a valuable asset, and the slight performance dip on the synthetic questions is an acceptable trade-off. This case serves as a valuable data point on the trade-offs between structural purity and metric-driven optimization.

## Processing a second book and the profile fingerprinting issue

When processing a second book from the same corpus (`2. PAPADO, MONAQUISMO...`), a new problem emerged: the `docagent` reused the profile from the first book. This happened because the `fingerprint` for both books was the same. The fingerprint is based on structural characteristics (extractor, headers, page geometry, heading numbering style), which were identical for both books.

This reuse of the profile led to the evaluation of the second book with the `evalset` (synthetic questions) from the first book, resulting in a meaningless score of 0.

To address this, a temporary "hack" was introduced in `graph.py` to disable profile
loading entirely. That was an experiment, and it has been reverted: it also threw
away the rule reuse, which is the feature the fingerprint exists to provide.

**The fix separates what travels between documents from what does not.** Rules are
a property of the family and are still reused; the eval set and the scores it
produced are properties of one document. `n_load_profile` now keeps the reused
profile's rules and drops its `evalset` and `scores` whenever `learned_from` names a
different file, so a fresh eval set is generated and measured for the new book.
`tests/test_second_book.py` pins both halves — the drop *and* the fact that
re-indexing the same file still pays nothing.

Remaining limitation, not yet addressed: a profile stores only one document's
`evalset` and `scores`, so alternating between two books of the same family
regenerates the eval set each time. Storing eval sets keyed by `doc_id` inside the
profile would fix it, at the cost of a schema change.

**A reused profile now re-derives its slug, because keeping it destroyed data.**
The slug names the file, and a reused profile kept the slug of the document that
had learned it — so indexing a hermeneutics chapter saved its eval set and scores
*over* `1-desde-agustín-…json`, and the measured numbers of both church-history
books were gone. The profile then also lied about its provenance: the filename said
Agustín, `learned_from` said Hermenéutica. Reuse is by fingerprint, not by
filename, so one file per document costs nothing and keeps the record honest.

**`profiles.load` picks the newest of several files sharing a fingerprint.** The
same corpus left two files on `110b1333`: one with eight measured revisions and one
with all scores at 0.0, left over from the reverted profile-loading hack. `load`
returned whichever sorted first, so the family's rules depended on the title of the
book that learned them. It now returns the highest `learned_at`.

**The "second book" `heading_guards` failure has been reproduced.** The original note reported a sequence `[2, 5, 6, 7]`. In the current run, when indexing the second book of the "Historia de la Iglesia" family with `--force-tune`, the `heading_guards` validation failed with the exact sequence `[2, 5, 6, 7]`. This confirms the latent bug is real. The agent fell back to default rules, which seem to work for this document, but the failure in rule learning for this specific structure needs to be addressed.

**The fingerprint groups by structure, and structure is not subject matter.** In the
same run, a Hermenéutica chapter and the church-history books landed on
`110b1333` — same extractor, same normalised headers, same page geometry, same
numbering depth. So a hermeneutics chapter was chunked with heading guards learned
from a church-history book, and the informe records the symptom: "4 chapters
inherited vs 1 read", and 2 vs 1 for chapter 4. The eval metrics do not see it,
because the synthetic questions are generated from the very chunks the wrong guards
produced. **Structural diagnostics, not recall, are what detect this.** Unaddressed:
the fingerprint would need a content signal, which is exactly what it was designed
not to have — so the honest fix is probably to report a suspected collision when
the detected chapter count disagrees with the inherited guards, and let the operator
decide.

### Related cleanups from the same run

- **`_IMPERATIVES` lives in `chunk.py` and `rules.py` imports it.** It had been
  copy-pasted into both, which is the founding bug of this project waiting to
  happen again (`rules.py` imports `chunk.py`, so the dependency can only run that
  way).
- **Diagnostic suites are per-document sidecars** (`<document>.pdf.diag.json`, or
  `--suite`), not a dict in `cli.py` keyed by literal filename. The key stopped
  matching the moment a file was renamed, and `diag` then silently asked a
  church-history book about Dooyeweerd and hilemorfismo. A missing sidecar is now
  reported in the output rather than hidden.
- **`--doc` hashes its argument** through `doc_id_for` before filtering. It was
  comparing a path against a stored `doc_id`, so the filter matched nothing.
- **`--dry-run` ends without persisting.** It measures nothing; routing it to
  `persist` wrote empty scores over a profile that had paid for its numbers.
- **`logs/` is not tracked; `costo.json` and `*.corrected.txt` are.** This entry
  used to name all three as untracked "because they are rewritten on every run",
  and that was wrong about two of them: `.gitignore` ignores `logs` and
  `docaget/cache/embed/`, and nothing else here. 170 files under `libros/` and
  the ledger are in git, which is what lets a corrected text be diffed against
  the run that produced it. `costo.json` is also no longer rewritten — see the
  ledger entry above. `profiles/` and `cache/correct/` stay tracked
  deliberately: a correction costs generation tokens and its cache is what
  survives an interrupted run.

