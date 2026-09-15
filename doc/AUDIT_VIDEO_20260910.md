# Audit — the first substantial video indexed

`La Transformacion de los Gobiernos y Naciones - Dario Silva`, audited 2026-09-11,
read-only, against production.

> **The run ids below no longer resolve.** The catalog was cleared and the video
> re-imported at 2026-09-11 11:20:58Z as `video-1789125657944-27203a97`. The
> version id, the content digest and every artifact hash are **unchanged** —
> identity is derived, so a re-import of the same captions lands on the same
> `ver_7005ed82817384c5dfa03062` — and `corrected.txt` and `chunks.jsonl` come
> back byte-identical, so every structural finding here still describes what is
> indexed today. What changed is the bill, and that turned out to be the most
> interesting thing in this document: see **F7b**.

The run is `video-1789084274929-508b1c52` on `tnt_f489b4a62220158ef6790c07` /
`lib_teologia`: `youtu.be/yq6uVBsVkeQ`, 1:16:13, indexed as
`ver_7005ed82817384c5dfa03062` between 23:51:15 and 23:52:50 UTC on 2026-09-10 for
**$0.154545**. It matters because `doc/VIDEO.md` recorded, the day before, that nothing
longer than 19 seconds had ever been indexed, that no video had gone through the
client-side split to an index, and that nobody had approved a video gate from the UI.

**The index is sound.** Every structural invariant holds, and the one question the
pipeline cannot answer about itself — whether the $0.1513 correction was indexed or paid
for and thrown away — is **indexed**. What the audit found instead is four defects in the
*reporting* around a correct index, and two measurements that change open questions in
`doc/VIDEO.md` into answered ones.

Method: the checks ran inside the deployed worker, piped over stdin through SSM, importing
`brainworker.auditversion`, `videosource`, `indexing`, `graph.schema` and
`docagent.transcript` rather than re-deriving any identity. Nothing was written.

---

## The free half is reproducible, and was reproduced

`sha256(identity_basis) == content_sha256` exactly, and the basis rebuilds line for line
from the probe — `youtube / yq6uVBsVkeQ / 4573 / 20141015 / captions:es-orig:auto /
<caption digest>`. On the caption path the sixth line is the digest of bytes that are free
to fetch, so this is a true content hash rather than the proxy the Transcribe path would
give.

The whole pre-correction pipeline then re-derives from `captions.vtt` alone:
`parse_vtt` → **3,616 cues** → `dedupe_rolling` → **1,842** → `group_cues` → **107
paragraphs**, every `(start_s, end_s)` equal to the stored table element-wise, and
`join_paragraphs(to_paragraphs(groups))` equal to `transcript.txt` **byte for byte**.

That was checked on the **local** run `video-1789043321464-3bae3683` — the same video,
rejected at the gate on this laptop at 07:28 — because five of its six free-half artifacts
are **byte-identical** to production's recorded `run_artifact` hashes:

| artifact | sha256 | identical |
|---|---|---|
| `captions.vtt` | `f871717f…` | yes |
| `transcript.txt` | `4c509716…` | yes |
| `transcript.json` | `1f2586e6…` | yes |
| `evidence.json` | `e2380aab…` | yes |
| `chunks.preview.jsonl` | `2411c67c…` | yes |
| `video-probe.json` | `52a42dad…` vs `4b52e13e…` | **no**, same 15,707 bytes |

The probe differing while `content_sha256` is identical is the identity design working:
the basis carries the video id, not the URL, so the `?si=` tracking parameter that differs
between two pastes cannot fork the identity. A rejected local run is therefore a free
reference fixture for a paid production one, which is worth knowing for the next audit.

## Every structural invariant holds

| Check | Result |
|---|---|
| Which stream the offsets index | **`corrected.txt`, 69/69 spans verified, 0 mismatched** (`transcript.txt`: 0/69) |
| `assert_aligned` on that stream | OK — 107 paragraphs against 107 timed groups |
| `uncovered_paragraphs` | `[]` — no cue group indexed nowhere |
| Paragraph ranges unique per chunk | yes — no two chunks share a time span |
| `span_for` re-derivation | agrees for all 69 chunks |
| Times present, monotone, `start ≤ end` | yes, yes, yes |
| Kinds / sections / breadcrumb | 69 × `transcripcion`, 0 sections, empty breadcrumb |
| `document.format` | `youtube` — so `_locator` takes the timestamp branch |
| Citations | **69 == 69 chunks**, none absent, none with an empty locator |
| Every locator exact | **0 bad of 69** — each equals `hhmmss(start_s) · watch_url(vid, start_s)`, and `citation_id == digest(chunk, locator)` |
| Qdrant | 69 points == 69 chunks; 0 wrong ids or payload fields, 0 span drift, 0 stale tail, 0 absent indices; `source_title` correct |
| Tenancy | 0 wrong `tenant_id` on any node or point |
| Activation | `document_active_version` **and** the graph's `DocumentVersion.active` both agree |
| `byte_size` | `0`, as designed |
| Semantics | 0 `MENTIONS` edges — the designed absence, not a failure |

Locators, first three: `0:00 · …?t=0`, `1:14 · …?t=74`, `2:07 · …?t=127`. Floored, as the
rule requires.

## The bill, and the quote

Two `cost_entry` rows, no more: `correction / vertex / gemini-3.6-flash / 17,215 in /
16,733 out / $0.151320` and `embedding / vertex / gemini-embedding-2 / 16,125 in /
$0.003225`. Sum **$0.154545**. No `transcription` row, which on this path means *free*
rather than *lost*. No stage charged twice, in this run or across the version's other runs.

The gate quoted **$0.2257767** — correction $0.2223315, embedding $0.0034452, high equal to
low because with captions in hand `characters_high` is 0. So the quote **over-reported by
1.46×**, which is the permitted direction, and it named only `correction` and `embedding`:
the narrowing that fixed the 2,300× over-quote is holding. The over-quote is almost
entirely output tokens — 25,839 projected against 16,733 spent.

(`doc/VIDEO.md`'s "$0.2223 quote" for this video is the **correction line**, not the total.)

## What $0.1513 bought

107 paragraphs, **41 changed**, 66 unchanged, 0 not returned, **22 rejected**, 3 calls, 0
cache hits. Net length change: **+22 bytes** on 63,288.

The 41 changes are overwhelmingly the deficit `correction_default` exists to close —
auto-captions arrive unpunctuated:

- sentence capitalisation (`sobre` → `Sobre`), comma/colon repair (`allá.` → `allá,`,
  `dijo,` → `dijo:` many times over), Spanish quote-then-period (`Amazon."` → `Amazon".`),
  em-dash parentheticals (`casa, tuve ahí dice,` → `casa —tuve ahí— dice:`);
- real spelling fixes: `postmodernidad` → `posmodernidad`, `Amnesia` → `amnesia`;
- one real name recovered: **`sería cartel` → `seguía Gardel`**.

And one regression worth naming: paragraph 3 turns `pasó lo` into `Pasuelo`, inventing a
word. One in 41, on a pass whose prompt forbids reformulation.

---

## Findings

### F1 — `chunk_transcript`'s warnings reach nothing durable, and the evidence was already gone

It builds a `warnings` list for the two conditions that most need saying — the correction
fallback, and paragraphs that reached no chunk — and returns
`Chunked(chunks=, count=, kinds=)`. `Chunked` has **no warnings field**, unlike
`Transcribed` and `Preview`, which both do. The facts reach only the container's stderr.

This was not hypothetical here. The worker container was replaced at ~23:55 on 2026-09-10,
three minutes after the run finished, so by the time anyone looked the log held **zero**
mentions of the run. The answer to "did the fallback fire" had to be re-derived by scoring
both streams — which is the only reason it is answerable at all.

### F2 — `auditversion.STREAMS` could not see a video's own stream

`corrected.txt / extracted.txt / raw.txt`. A video's uncorrected stream is
`transcript.txt`, so on the fallback path `choose_stream` would crown `corrected` with
near-zero verified spans: a byte-exact index reported as broken, by the tool whose only job
is to be believed. Fixed in this change.

### F3 — `evidence` is attributed to a stage the video path does not have

`ARTIFACT_STAGES["evidence"] = "extracting"`, and `extracting` is not in `VIDEO_STAGES`,
while `group_transcript` writes `evidence` on the video path. `auditlog.build` therefore
moves it into `loose_artifacts` and renders the trailing `stage: null` row — the
"no stage · evidence" line in the run detail. The map is keyed by artifact name alone, so
`evidence` is the one artifact written by two different stages on the two paths: precisely
the lie `grouping` was made a stage of its own to avoid.

### F4 — the quote is not persisted anywhere

The `estimate` artifact kind is declared and mapped to `previewing`, and **nothing writes
it**; those two lines are its only references in the repository. The quote above was
recovered from the live Temporal workflow, which retains for **72 hours** — so it expires
about 2026-09-13 18:52 and this reconciliation becomes unrepeatable. Same "declared and
used by nothing" shape already recorded for `ChunkNode.sheet` and `SPEECH_RATE_SPREAD`.

### F5 — `citation_title_prefixes` is noise for a timed source

It splits the locator on `" · "` and takes `[0]` to catch a re-projection under a changed
title. For a video that field is `hhmmss(start_s)`, so a healthy version reports as many
distinct "titles" as it has chunks.

### F6 — `covered_s` is unclamped, and `end_s ≤ duration_s` is not a sound invariant

`covered_s = max(g.end_s for g in groups)` = **4574.699** against a probed
`duration_s` of **4573**. The gate reported coverage *above* 100%, and chunk 68 ends
1.699 s after the video does. The cause is not a bug in grouping: YouTube reports duration
as a truncated integer while the caption track legitimately runs to the true end. So an
audit must **report** the overshoot rather than fail on it — asserting
`end_s ≤ duration_s` would mark every auto-captioned video defective. No locator is
affected, because a locator uses `start_s`.

### F7 — on an auto-caption transcript, `correct.verify`'s proper-noun rule protects the transcription error

**22 of 107 paragraphs — 20.6% — had their correction rejected, every one of them for
`proper_noun` loss**, and the lost "names" are auto-caption mis-hearings:
`Beaida`, `Sawer`, `Bray`, `Chusa`, `Tilich` (Tillich), `Osana`, `Falangeja`, `Foxaque`,
`Mars`, `Cuyama`, `Francisco` — 22 paragraphs at indices 1, 6, 11, 19, 20, 21, 23, 28, 32,
34, 38, 39, 84, 85, 87, 88, 92, 94, 95, 96, 98, 103.

The rule is right on the corpus it was measured on: a book's capitalised words are real
names and losing one is data loss. On speech a machine transcribed, a capitalised word is
as likely to be the machine's mistake, and refusing the repair **keeps the mistake**.

The asymmetry is visible in one run: `cartel` → `Gardel` was **accepted**, because the
captioner wrote the wrong name in lowercase; `Tilich` → `Tillich` would have been
**rejected**, because it wrote the wrong name capitalised. So on a transcript the gate's
behaviour turns on whether the captioner happened to capitalise its error, which is not a
property anybody chose.

**Read in context, all 22 are the same thing.** Every refused repair is a name the
captioner mangled, and most of them are famous:

| kept in the corpus | almost certainly | from the sentence around it |
|---|---|---|
| `Beaida` | Betsaida | *"como al ciego de Beaida por segunda vez para ver más claramente"* |
| `Sawer` | Sawyer | *"las aventuras de Tom Sawer, un clásico americano"* |
| `Bill Bray` | Bill Bright | *"el ministerio de campus Crusade, que fundó nuestro hermano Bill Bray"* |
| `Chusa` | Chuza | *"la esposa de Chusa, superintendente del rey Herodes"* |
| `Paul Tilich` | Paul Tillich | *"Tomando la premisa de Paul Tilich"* |
| `la Osana` | la Lausana | *"el movimiento de la Osana, que no hay una cultura cristiana"* |
| `La Falangeja`, `Carlos Mars` | La Falange, Carlos Marx | *"La Falangeja es una hija adulterina de Carlos Mars"* |
| `Agustín de Foxaque` | Agustín de Foxá | *"el Conde Agustín de Foxaque tenía un estilo muy pintoresco"* |
| `Francisco Cuyama` | Francis Fukuyama | *"en su famoso mexel fin de la historia"* |
| `Barán Lincoln` | Abraham Lincoln | *"ese decálogo de Barán Lincoln tan hermoso"* |
| `Honding` | Huntington | *"habla Honding donde la inmigración latinoamericana"* |
| `FARG` | FARC | *"condenó a los comandantes de las FARG por narcotráfico"* |
| `Madame Pompadur` | Madame Pompadour | *"ella es como Madame Pompadur"* |

The corpus now holds Fukuyama as *Cuyama*, Tillich as *Tilich*, Marx as *Mars* and
Bethsaida as *Beaida* — in a Spanish theology library — because a rule written to protect
proper nouns protected the transcription of them instead.

### F7a — and a rejection could not be reviewed at all, which is why nobody knew

`correct_paragraphs` `continue`s **before** `cache.put`, so a refused proposal is never
persisted. All that survived was the reason and the token that went missing; what the model
would have written was discarded with it. That is why the table above had to be inferred
from context rather than read.

It also undermines a measurement this repository relies on. The wide audit that set
`MAX_LOST_CHARS` read **2,684 corrections out of the cache** — and the cache holds only
corrections this gate *accepted*. So that sample could not contain a false positive by
construction: `verify`'s false-negative rate has been measured on a real corpus and its
**false-positive rate has never been measured at all**.

Fixed here: `Rejection` carries `proposed`, and `correction-report.json` records it. It is
model output the gate judged unsafe, so it goes in the report and never into the corpus —
the corrected stream is byte-for-byte what it was. That makes the next transcript's
rejections reviewable, which is the precondition for relaxing the rule rather than guessing
at it. The shape a relaxation probably wants is *"a capitalised token that was replaced by a
near neighbour is a repair; one that simply vanished is still loss"* — `Mars`→`Marx`,
`FARG`→`FARC`, `Tilich`→`Tillich` all pass that test and an outright deletion does not. It
is not implemented, because on this run there is no proposal to measure it against.

### F7b — and the rejected paragraphs are re-bought on every import, for ever

The re-import measured this by accident, and it is the sharpest form of the defect.

| | first import | re-import |
|---|---|---|
| correction | 17,215 in / 16,733 out / **$0.151320** | 4,324 in / 3,378 out / **$0.031821** |
| embedding | 16,125 in / $0.003225 | 0 in / **$0.000000** |
| `cache_hits` / `calls` | 0 / 3 | **85** / 2 |
| rejected | 22, all `proper_noun` | **the same 22, at the same indices** |
| `corrected.txt` | `75c97c33…` | **`75c97c33…`** |
| `chunks.jsonl` | `ca7132f8…` | **`ca7132f8…`** |

85 of 107 paragraphs came back free, because an *accepted* correction is cached. The
remaining 22 are exactly the rejected ones: re-sent to the model, re-corrected, and
**re-rejected for the same reason at the same indices**, producing output byte-identical
to what was already on disk.

So **$0.031821 of a $0.031821 bill bought nothing**, and it will be spent again on the
next re-import, and the one after that. The embedding half shows what the right behaviour
looks like — every vector a cache hit, **$0.000000**. The whole asymmetry is `cache.put`
being skipped on rejection.

Same shape as the `$1.1965` of `$3.7572` that the `trail` leg exists to find: 31.8%
there, **100% here**. `Rejection.proposed` does not fix it; caching the refusal would, and
that is now possible precisely because the refusal is recorded.

### F4a — the gate cannot see the cache, so it quotes a re-import as a first import

Visible only because `estimate.json` is now persisted. The fix landed six minutes before
this run and found a defect within the hour.

The quote was **$0.2257767** against a bill of **$0.031821** — an over-report of **7.1×**,
where a *first* import of the same video over-reported by 1.46×. `estimate_for` works from
character counts and knows nothing about `cache/correct` or `docagent.embedcache`, so it
quoted 107 paragraphs of correction when 85 were already paid for, and 17,226 tokens of
embedding when the real figure was zero.

Over-reporting misleads a user into declining affordable work exactly as much as
under-reporting misleads them into approving expensive work — this product's own rule —
and a 7× over-quote on a re-import is the case most likely to be met with *"that is too
much for something I already have."*

### F8 — a transcript is embedded with no breadcrumb, and it is measurably expensive

`Chunk.embed_text()` prepends `breadcrumb()`; a transcript has no chapter and no section,
so the breadcrumb is `""` and a video chunk goes to the embeddings API as bare speech while
every book chunk in `lib_teologia` carries `Chapter > Section` ahead of its text.

Measured, scoped to this version, retrieval is fine — `¿Qué dice sobre la transformación de
los gobiernos y las naciones?` puts chunk 51 (*"Frente a los gobiernos, ya terminando…"*)
first on the sparse leg at 5.2598, and the hybrid and dense legs both return sensible
passages. But:

- the video's **dense scores cluster at 0.6001–0.614** against `MIN_SCORE = 0.60`. The
  whole document sits a hundredth of a cosine above the floor;
- asked the same question across the **whole library**, the top five are all *books* —
  **0 of 5 hits are this video**, for a question phrased in the video's own title.

`doc/VIDEO.md` left this exactly open: *"Whether putting the video's title there would help
retrieval is a real and measurable question, deliberately not answered by guessing."*

**So it was measured, and the answer is not the clean yes two questions suggested.** Each
of the 69 chunks was embedded twice — as indexed, and with the title prepended as
`embed_text()`'s first part — and both were compared by cosine against the same six query
vectors. Nothing was written and nothing re-indexed. Control: the computed bare cosines
reproduce Qdrant's own dense scores to four decimals (0.6090, 0.6140), and all 69 bare
texts came back as embedding-cache *hits*, which independently confirms the stored vectors
correspond to the `embed_text` in `chunks.jsonl`.

| question | bare | titled | Δ | best book | 5th book | over floor, bare→titled |
|---|---|---|---|---|---|---|
| …transformación de los gobiernos y naciones? | 0.6090 | 0.7036 | **+0.0946** | 0.6470 | 0.6243 | 5 → **69** |
| …la posmodernidad? | 0.6140 | 0.6001 | −0.0139 | 0.7376 | 0.6773 | 1 → 1 |
| …los cristianos y el poder político? | 0.6789 | 0.6787 | −0.0003 | 0.7640 | 0.7096 | 24 → 38 |
| …la iglesia frente al Estado? | 0.6353 | 0.6351 | −0.0002 | 0.6864 | 0.6318 | 2 → 2 |
| …los reinos de este mundo? | 0.6259 | 0.6288 | +0.0029 | 0.6466 | — | 2 → 1 |
| …Darío Silva sobre las naciones? | 0.6764 | 0.7232 | **+0.0468** | 0.7305 | 0.7061 | 13 → **69** |

Mean Δ **+0.0217**, range −0.0139 to +0.0946. Entering the library's top five goes from
**1 of 6 to 3 of 6**, and the video outranks the best book on one question where before it
outranked none.

**But the gain is entirely where the question repeats the title's words.** The two large
deltas are the two questions I phrased from the title — "transformación de los gobiernos y
naciones" *is* the title — and the other four move by less than a thousandth. That is
vocabulary leakage, the same effect this repository already measures deliberately: the
synthetic eval's questions are written from the chunks they must find, and *"the gap
between the two modes is the leakage measurement"*. I wrote these questions, so a large
part of the two big numbers is mine.

**And there is a cost the naive version hides.** On both title-matched questions, prepending
the title takes the chunks clearing the dense floor from 5 and 13 to **all 69**. A title
strong enough to lift the document is strong enough to make every fragment of it look
equally relevant — and `diversify`/`PER_SECTION`, which is what would normally temper one
source flooding a result set, **cannot help here, because a transcript has no sections**.

So the honest conclusion is narrower than "add the title": a transcript is measurably
handicapped by its empty breadcrumb, and the obvious fix buys real ground on questions
shaped like the title while doing nothing for the rest and risking a 69-chunk monoculture
in the top-k. Worth doing deliberately, with a real eval set rather than six questions one
person wrote — which is the thing a video has no stage to pay for.

### F9 — the Abkhazian orphan, and the fix landing visible in the data

The document carries **two** versions. The indexed one is `ver_7005ed82817384c5dfa03062`
(`captions:es-orig:auto`). The other, `ver_2c19d4975460f19198702f53`, is `pending`, was
never activated, and its probe records `chosen: {"language": "ab", "kind": "auto"}` —
**Abkhazian**, a machine translation of a machine transcription, from a video offering 157
automatic tracks.

That is the exact failure `doc/VIDEO.md` describes under *"The track a caller who said
nothing gets"*. Its run started **17:27:34Z**; the image carrying the `_choose_track` fix
was built **17:39:29Z**; the next run, at **17:48:27Z**, chose `es-orig`. The fix is
confirmed working in production, and the deploy is legible in the data to the minute.

What remains is housekeeping rather than risk: an orphan `pending` `document_version` row
still linked to the document, plus that run's artifacts on disk. Nothing indexes it and
nothing charged for it.

---

## What `doc/VIDEO.md` can stop calling unverified

- **A video longer than 19 seconds, indexed end to end.** 1:16:13, 69 chunks, on production.
- **A video gate approved from the UI.** `awaiting_approval` 23:51:16 → `correcting`
  23:51:31: fifteen seconds, by a person, in the real window.
- **Correction on a transcript, indexed.** The stream the offsets index is the corrected one.

Still not run: the Transcribe/audio path — there is no `transcription` cost row, on this or
any other run of this video, and no video without captions has been indexed on either plane.

## What this audit cannot say

- **Whether the correction was worth $0.1513.** Its value on a transcript is unmeasured;
  the $0.0334 that positions the gate was measured on a book. The diff shows what changed,
  not that the corpus is better for it.
- **Whether 4,573 s versus 4,574.699 s of coverage is end-of-video silence or a lost cue.**
  Reported, not judged.
- **Whether the chunking is good.** There is no eval set and no recall figure for this
  version, and there cannot be one: `_recommended` switches `generate_evalset` off, so
  `audit_version.py --measure` returns `unavailable` before it constructs an embedder. Any
  recall number for a video would have to be paid for by a stage this workflow does not have.
- **Whether the chosen track is Spanish as spoken** rather than a round trip through a
  translator. The probe says `es-orig` and the rule now prefers the original before any
  translation of it; nothing in the artifacts proves the text itself.

---

## Verified after the fixes shipped

Production image `da6df10-9ebe0a1` — a tag naming the two commits it actually
contains, rather than the ones the trees became, because for once the build
followed the commit.

`worker/scripts` now ships in the image, so the audit above reproduces itself
from the installed script rather than from something piped over stdin:

```
docker exec company-brain-worker-1 \
  python /app/worker/scripts/audit_version.py ver_7005ed82817384c5dfa03062
```

Its `video` leg agrees with every figure in this document, independently:
`content_sha256_rederives: true`, `chunked: "corrected"` at 69/69 spans against
`transcript` at 0/69, `correction_fallback_fired: false`, `aligned: true`,
`uncovered_paragraphs: []`, `distinct_ranges: 69` of 69,
`derivation_disagreements: []`, and `rejected_by_reason: {"proper_noun": 22}`.

The two user-visible fixes, read back off the live trail:

```
stage=probing      artifacts=['captions', 'video_probe']
stage=grouping     artifacts=['evidence', 'transcript', 'transcript_text']
stage=previewing   artifacts=['preview_chunks']
stage=correcting   artifacts=['corrected_text', 'correction_report']
stage=chunking     artifacts=['chunks']
trailing "no stage" row present: False
total_cost: usd 0.154545, unpriced_entries 0
```

`evidence` sits on `grouping`, the stage that writes it; the trailing
`stage: null` heading is gone; and the ledger still totals to the cent, so
nothing was moved out of the bill to tidy the table. `citation_title_prefixes`
reads `null` rather than 69 clocks.

## One authorised check not made

A question asked end to end **from the desktop app** was in scope and was not
spent. The retrieval probe replaced it, and it is the better instrument for what
was actually in doubt: F8 says a library-wide question returns books rather than
this video, so an ask would most likely have cited the books and demonstrated
nothing about the video's locators — while B2 and B3, read straight off the
graph, already establish the condition under which `answer._verify` drops
nothing (every chunk carrying a locator equal to
`hhmmss(start_s) · watch_url(vid, start_s)`).

What remains genuinely unobserved is a human clicking a citation of *this* video
and landing at the right moment. That is one question away, and worth asking
once F8 is acted on rather than before — and F8, now that it has six questions
behind it rather than two, is a decision about eval sets rather than a one-line
change.
