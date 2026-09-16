# Reading a YouTube channel

Given a channel and a topic: review up to a hundred videos, **read what the
candidates actually say before paying to index them**, approve the bill with
the figure in front of you, and then ask the corpus for a comparative, neutral
reading in which every claim can be checked at the minute of the video.

Everything below is a decision plus the thing that forced it, the convention
`doc/CLAUDE.md` uses. Where a claim is unmeasured it says so.

Built 2026-09-16. Free plane (`worker/brainworker/api/main.py`, the `/channels`
routes) and the desktop app (`app/src/screens/ChannelScreen.tsx`) only — see
*What is not built* at the end. It replaces
`~/moodle/cursos/01-teologíaSocial/youtube_explorer.py`, which is left where it
is and untouched.

---

## What already existed, and what did not

Almost all of this is reuse, and the design rests on knowing exactly which
parts are not. `VideoIngestWorkflow` resolves a video, downloads its captions,
groups them into timed paragraphs, corrects, chunks, projects, embeds, and
**already produces citations with a timestamp**: a video chunk's locator is
`1:16:13 · https://youtu.be/<id>?t=4573` and `app/src/Locator.tsx` already
opens it. Nothing about the citation had to be built.

What did not exist:

- **A catalogue of a channel.** Nothing in the repository spoke to the YouTube
  Data API — every YouTube-facing call was yt-dlp or a signed caption URL.
- **A judgement from metadata**, which is a hypothesis about a title.
- **A reading of the uncorrected transcript**, which is the first pass that has
  read the words, and which is free to obtain wherever the video has captions.
- **A five-section answer** whose findings the code checks against citations
  and whose interpretation is kept apart from them.

Five modules, in `worker/brainworker/`: `youtube.py`, `channelstore.py`,
`channel/preselect.py`, `channel/topics.py`, `channel/synthesis.py`. One quote
in `channel/estimate.py`. Three workflows in `workflows/channel.py`.

---

## A channel is a library

Not an analogy. `retrieve.search` narrows by equality on `library_id`,
`document_id` or `version_id` — one value each, from `ALLOWED_FILTERS` — and
the graph expansion in `_hydrate` narrows by library **alone**. So "ask only
this channel" *is* "ask this channel's library", and any arrangement that put a
channel's videos into an existing library alongside books would make that
question unanswerable. `channelstore.library_id_for` is where the mapping
lives: `lib_yt_<channel_id>`, the id **verbatim and never lowercased**, because
a YouTube channel id is case-sensitive base64 and folding it would put two
different channels in one library — a merge nothing downstream could see, since
every document in it would be real.

Consequence: the Channel tab is the third screen with no library picker, beside
Home and Services. It picks a *channel* and the library follows. Two pickers
over one choice could disagree, and the one that would lose is the one showing
an id nobody can read.

## The catalogue is a file and the indexed state is Postgres

`<workspace>/channels/<channel_id>/channel.json` and `videos.json`, under
`Paths.for_tenant(...).channels` — **scoped to the organisation from the first
day**, unlike `runs/`, whose tenant blindness `config.Paths` records as a known
crossing. There is nothing here to migrate, so scoping it correctly costs
nothing.

It is not a table because it is *derived data*: refetchable for one quota unit
per fifty videos, changing whenever the channel does, joined against by nothing.
A table would have bought a migration and three models on two planes for a
thing a JSON file already does.

**Whether a video is indexed is deliberately not in it.** The original plan for
this feature proposed an `index-state.json` beside the catalogue. That is a
second record of a fact Postgres already holds — `document.source_key =
youtube/<id>` inside the channel's library, plus the document's active version
— and a second record of one fact is one that can disagree with the first in
silence. `GET /channels/{id}` joins the two on every read instead. The
consequence is the only migration this feature needed: `run.kind = 'channel'`,
in `../yorch-tauri-backend/prisma/migrations/20260916120000_run_kind_channel`.

## The Data API, and what the script it replaces got wrong

`youtube.py` is stdlib `urllib` over the Data API v3. Why that and not the
bundled yt-dlp: **the call that is bot-checked is the player API, not the
network** — measured from EC2 on 2026-09-05, `extract_info` is refused there
while every signed URL is served. A keyed REST endpoint is refused nowhere, so
a catalogue fetched through it works from the worker wherever the worker runs.
It is also the only source that carries a video's *description*, which is half
of what the preselection reads.

Three things this does that `youtube_explorer.py` did not:

- **No `search.list` fallback.** The script fell back to a search when a handle
  did not resolve. That call costs 100 quota units against this module's 1,
  and it answers with the channel Google thinks you meant. A channel indexed
  under the name of a different one fails nowhere: every video in it is real.
  It is the same shape of damage `videosource.check_resolved` refuses, so it is
  refused here too — an unresolvable handle is `channel_not_found`, never a
  guess. A legacy username is still tried, for one more unit.
- **`videos.list` for the duration.** `playlistItems` carries none, which is
  why the script's CSV has an empty duration column for this provider. The
  duration is what `videosource.projected_characters` turns into a character
  count, and therefore what the transcript pass is quoted from.
- **`contentDetails.videoPublishedAt`, not `snippet.publishedAt`.** On a
  playlist item the second is when the video entered the playlist.

`Client.units` reports what a sync spent. The default allowance is 10,000 a
day; a 2,000-video channel is about 81. It is the one resource here that runs
out, and saying what a sync cost beats discovering it at the end of the day.

Quota kinds are told apart by the reason in the body, not the status: a 403
`quotaExceeded` is fixed by waiting until midnight Pacific and a 403 `forbidden`
by looking at the key, and one status for both would send every reader to the
wrong remedy half the time.

## The key is the first provider credential this product has ever held

Vertex refuses API keys and uses ADC, so the keychain's provider half was
documented and **empty**: `secrets.env` was created `0600`, mounted read-only,
and written to by nothing. `app/src-tauri/src/secrets.rs` is the two
`keychain::Entry` calls root `CLAUDE.md` said it would take, and
`Stack::prepare` now *materialises* the file from the keychain on every launch
rather than merely creating it — rewritten whole, like `.env`, so a key the
user cleared leaves the file. `config.Settings.youtube_key` reads that file
first and `BRAIN_YOUTUBE_API_KEY` second, the environment being the fallback for
a stack brought up by hand.

The Services screen's field is **write-only**. What comes back is whether a key
is stored and which store holds it — `keychain` or `file`, a token rather than
prose, so the wording lives in the two bundles — never the key. The worker reads
the file once at startup, so a key set while the stack is up reaches it on the
next `up`, and the screen says so.

**No key exists on this machine as of 2026-09-16**: not in the environment, not
in `~/yorch-secrets.env`, not in `~/yorch-gcp-platform/secrets.env`. One has to
be created in Google Cloud with YouTube Data API v3 enabled. Until then
`POST /channels/sync` answers 503 `youtube_key_missing` — a 503 rather than an
empty catalogue, because "this channel has no videos" and "nobody here can ask
about this channel" are different facts and only one of them is about the
channel.

---

## Five acts, and where each decision is made

```
Sync        Data API → catalogue on the workspace          quota, never money
Filter      title keywords, computed on the client         free — narrows Discover too
Discover    one call per 25 titles (of the filtered set)   PAID — quoted first
   └─▶      probe: POST /videos per TICKED row             free
            resolves, downloads captions, groups, previews,
            quotes, and PARKS AT ITS OWN GATE
Read        one call per probed transcript, UNCORRECTED    PAID — quoted with Discover
Index       the same tick, one total                       PAID — each run's own gate
Ask         retrieval + five sections                      PAID — two calls
```

**The batch is a sum on a screen, not a gate of its own.** This is the decision
the whole design rests on. Each probed video is an ordinary `video` run with its
own `run` row, its own artifacts, its own `estimate.json` — computed by the
existing `estimate_video`, not by a second implementation — and its own
seven-day gate. The channel screen adds the figures up and approves the ticked
rows by signalling each gate through `POST /runs/{id}/approve`, unchanged, the
same sequential loop with per-item failure collection that `ImportScreen.start`
already runs over N files. What that buys: no child workflows, no aggregate
gate, no reimplemented quote; a half-finished batch leaves every remaining run
parked rather than lost; and re-running it costs nothing, because an
already-indexed video short-circuits at `registering` and spends $0 — verified
in `doc/VIDEO.md`.

**The semantics switch is ticked before the probe, never after.** `_recommended`
passes `extract_semantics` through and `estimate_video` quotes it, so a gate
quotes the options the run was *started* with. A switch at approval time would
be approving past the quote, which is the under-reporting failure this product
refuses outright. The screen sends its own options and does not copy
`VideoGateReview.tsx:63`'s forced `extractSemantics: false` — the recorded
defect that made the first real video indexed, citable and invisible on the
Graph screen.

## Filtering by title, and what the tick means

Added 2026-09-16, the same day, after a person sat in front of the screen and
found that the checkbox at the start of every video row did nothing. It was the
*approval* tick — `disabled` until that row's probe had parked at a gate — and
nothing on screen said so. Three changes, one decision.

- **One tick per video for the whole flow.** `selectable` (`lib/channel.ts`) is
  "not indexed and not an unaired premiere", before *and* after the probe. What
  is ticked is what *Sondear* probes (`toProbe`), and once the gates land the
  same tick is what *Indexar* approves. Discover's verdicts do not decide any
  more; they **seed** the tick once per discovery (`seedFromDiscovery`,
  relevante + dudoso in score order, up to the budget) and the person edits
  from there. A verdict is a hypothesis about a title, and a person may overrule
  it in either direction.
  The one judgement that survives is the no-captions one, inverted: a landed
  gate that reports no captions **takes the tick away** (`unpickOnGate`), once,
  the moment that row's own gate arrives. The row stays enabled — re-ticking it
  is a choice, and the totals print its price beside it.
- **The probe cap is a budget across rounds.** `deepLimit` is the number of
  transcripts the quote priced, the probes are what the transcript pass reads,
  and `probeBudget` is what is left of it after the runs already started. Two
  rounds of ten against a cap of ten would read twenty transcripts quoted as
  ten, which is spending beyond the figure somebody approved. Ticks beyond the
  budget are counted (`overCap`) and said out loud beside the button, never
  silently dropped.
- **The words a channel's titles repeat are offered as chips, free.**
  `lib/keywords.ts` ports `bm25.tokenize` — accent fold, ñ kept, stopwords,
  three-letter floor — and drops bare numbers on top, because a year is not a
  topic. Counted as *document frequency*: one title repeating a word three
  times is one video. A word on more than half the titles is not offered once
  there are ten or more of them: that is the channel's own name or its series
  label, and it joins nothing. The chip is labelled with the spelling the
  channel uses most, so it reads `Espíritu` and never `espiritu`. Forty at
  most, twenty shown, a fold for the rest. All of it unmeasured against a real
  channel — the first real sync will say whether 50% is the right ceiling.
- **A chip is a filter and a starting point at once.** Pressing one narrows the
  table to titles carrying any pressed word *and* writes the selection into the
  topic field; typing in the field afterwards is free and leaves the chips
  alone, because the chips decide what is on the table and the topic decides
  what the model is asked. A row that carries a run is shown whatever the
  filter says, labelled "fuera del filtro": a parked gate is a pending decision,
  and a filter that hid one would leave a person approving a batch with a row
  they could not see.
- **Discover judges the filtered set, and that is the one server change.**
  `ChannelQuery.video_ids` on both `/discovery-estimate` and `/discover`,
  `DiscoverRequest.video_ids` appended and defaulted so no `workflow.patched`
  is needed, and `estimate.plan_for` intersects before applying `limit` — after
  would let a filter over an old series judge nothing. An id the catalogue does
  not hold is ignored rather than refused: the catalogue can change between
  syncs, and the `preselection` artifact records the exact ids evaluated anyway.
  The Rust side builds both bodies through one `channel_query_body`, so the
  quote and the run cannot carry different sets. The paid plane serves no
  channel route, so nothing is forked.
- **The quote is dropped when the filter, the topic or either limit changes**,
  and both *Analizar* and *Leer* stay disabled until it is asked for again.
  Read gained that guard with this change: on the manual path — tick, probe,
  read, no Discover — nothing else has ever priced the transcript pass before
  it runs. Re-quoting is free.
- **The sync moved to the app's top bar.** `lib/channels.tsx` is
  `lib/libraries.tsx` with a channel in place of a library: a provider the
  shell mounts, a remembered choice scoped by plane, and a picker with the URL
  field beside it, in the slot the library picker uses on every other tab. A
  sync also reloads the libraries, because it creates one. The screen is two
  columns now — the chips and the table on the left, everything that decides
  on the right — collapsing at the same 76rem as the other split screens.

Verified the way everything above was: 47 TypeScript tests on the screen and
its arithmetic, 14 on the keywords, 5 on the provider, one on the Rust body and
five on the worker, each checked by reverting the fix it stands on; plus a
static-HTML screenshot pass at 1440 and 900 in both themes against a 40-title
synthetic catalogue with two landed gates and one reading. Nothing has touched
a real channel, for the reason recorded under *Verified, and not*.

## The quote widens the input, and every other quote widens only the output

`estimate_for` says of its own spread: *"Input is nearly deterministic — the
document's characters plus a per-call overhead, both known before the run."*
Here half of it is not. The preselection's input is **exact**:
`preselect.payload_for` builds the literal string that will be sent and the
estimator prices that string, the rule `bookmeta.METADATA_CHARS` states. The
transcript pass's input is a *projection from a duration* through
`CHARS_PER_SECOND_OF_SPEECH` — 14.5, pooled over 11.21 hours of real Spanish
preaching, with `SPEECH_RATE_SPREAD` of 1.40 against a measured maximum of
15.24 — so its range is the speech rate's own range, and one number would claim
a precision the measurement does not have. The output on both stages is already
a ceiling and does not widen.

Which videos the transcript pass would read is not known at quote time, because
the preselection has not run. The plan takes the **longest** of the candidates,
up to `deep_limit`: the worst case by input. Choosing the shortest would quote a
bill the run cannot come in under.

Measured on a synthetic channel of a hundred 76-minute talks, ten read:
**$0.42–$0.53**, of which the metadata half is $0.09. Indexing ten of those at
the measured $0.1545 is $1.55, or $6.53 with concepts. So reading costs about a
third of indexing and replaces a guess about a title with a quotation from the
talk. Unmeasured against a real channel; the first real sync will say.

`deep_limit` is capped at **25**, and the cap is the caption throttle rather
than the bill. Captions are free, but `timedtext` was measured refusing this
address six times across 25 minutes, and when `_download_caption` gives up
`chosen` becomes `None` — which is the branch that pays Amazon. A batch wide
enough to meet the throttle turns a free pass into a transcription nobody asked
for. The source the probe actually chose is a column on every row for the same
reason.

## Three checks the prompts cannot make

A prompt is a request. These are properties the code holds.

- **Every preselection verdict is matched back to the batch it was sent.** An
  id the model returned that was not sent is dropped and counted as `invented`;
  an id that was sent and came back with no usable verdict is `sin_evaluar`,
  never `descartado` — treating silence as rejection would hide a model quietly
  answering about fewer videos than it was asked about. A score outside 0–100
  loses its verdict, not its video. Together the result covers exactly the
  videos that were evaluated, which is the property the feature's honesty rests
  on: a candidate the reader is offered came from the list they were told was
  examined. `sin_evaluar` is kept **out of the schema's enum**, so the model
  cannot use it to decline.
- **Every topic carries a quotation, and the code looks it up in the
  transcript.** Through `quoting.find`, the matcher `activities/paid.py` uses
  for a claim's quote — extracted into one module precisely so the two cannot
  drift: it tolerates whitespace and nothing else, and a second copy that
  tolerated one more thing would let one feature accept quotes the other
  refuses without either failing. A topic whose quotation is not there loses
  the quotation, keeps its existence, and `verified` counts what survived. The
  lookup runs against the *excerpt the call was given*, not the whole file, so
  a model cannot be credited for quoting what it was not shown.
- **Every finding names a citation that survived `answer._verify`, or it is
  moved to `limitaciones`.** Moved, never deleted: an answer that quietly lost a
  claim looks complete and is shorter, and the reader would never know a claim
  had been made and dropped. `demoted` travels because a synthesis where half
  the findings were demoted is one to distrust. A refusal prints **no
  interpretation** — an inference resting on nothing checkable is the model's
  own reading of a corpus it may not have read.

## The transcript pass reads the uncorrected stream, and that costs something

`transcript.txt` — the `transcript_text` artifact, before `correct_text` has
run — is what exists at the moment somebody is deciding whether to pay for
correction, so it is what is read. The honest consequence: an auto-caption
transcript spells names the way the captioner heard them, and this corpus
already holds *Cuyama* for Fukuyama and *Tilich* for Tillich. A topic keyed on
a proper noun can be missed here and found after correction. Recorded rather
than worked around, because the alternative is paying for correction before the
gate that decides whether to pay for correction.

`TRANSCRIPT_CHARS` is 200,000 — about 3.8 hours at the measured rate — and it
is a **ceiling against a pathological input, not a sampling policy**. A
76-minute talk measures 62,016 characters and fits whole at about $0.026;
reading only its opening would save two cents and answer a different question,
since a preacher states the subject in the middle as often as at the start.
When the ceiling bites, `truncated` says so.

Both passes have reasoning off (`config.Gemini.stage_thinking`): they are
classification over text that was handed to them, the argument `planning` and
`epub-metadata` already record, and both sit between a button and a table.
**The synthesis keeps reasoning on**, by the same deliberate bias toward caution
as `answering`: it is the stage where the product either cites or fabricates.

## The synthesis reuses retrieval whole and changes only the envelope

`synthesise_channel` runs the planner, `retrieve.search` and `answer._prompt`
exactly as a question does, so the tenant and library scoping, the dense floor,
`diversify`, the concept expansion, the `lecturas` with their `estado`, and the
locator attachment all come with it — and every figure measured against the
one-shot path keeps holding. The system prompt is `answer.SYSTEM`'s six rules
first and a neutrality section below them, joined by `effort.compose_system`,
which writes the sentence that says the rules win: the same arrangement a
per-organisation answer style gets, and for the same reason.

`run.kind` for a channel question is **`ask`**, not `channel`. It has no gate
and no pipeline, so it has no stage vocabulary to belong to; reusing
`start_question_run` and `record_question_cost` is what makes that true rather
than stated, and it is why the question needed no migration. `channel-synthesis`
joins `ASK_COST_STAGES` beside `planning`, `ask-embedding` and `answering`.

`MAX_OUTPUT_TOKENS` is `answer.MAX_OUTPUT_TOKENS`, imported: it bounds the
bill, not the reasoning, and a call with dynamic thinking was once measured
spending its whole allowance for no text at $0.497373.

**Default effort is `thorough`**, where `/ask` defaults to `standard`. A
synthesis compares sermons, so it needs evidence from several of them, and the
measured curve puts the citation peak at 48 chunks. It is still a level name on
the wire and never numbers.

## Export

`app/src/lib/synthesisExport.ts`, pure. **One row per citation, not per
finding**: the unit somebody checks is a claim against a place in a video, and
a cell holding three links is a cell nobody clicks. `tipo` keeps the four kinds
of row apart in one file — an inference gets no link, because it rests on the
findings and pretending otherwise would give it a checkability it does not
have. The CSV carries a byte-order mark for one named consumer: this corpus is
Spanish and Excel reads a UTF-8 file without one as Latin-1. The JSON keeps
`demoted` and `invented`, which are facts about the synthesis rather than any
row of it.

---

## What the tests found while being written

- **Seeding the selection once left every later row unticked.** The probes
  park one at a time and the poll records each gate as it lands, so ticking the
  defaults when the *first* gate arrived left every subsequent row clear — with
  no way to tell that from a row somebody had deliberately unticked. A row is
  read exactly once, the moment its own gate arrives, and after that the
  selection is the person's. Found by
  `ChannelScreen.test.tsx::approves what is ticked and rejects what is not`,
  which is the one test in that file that waits for both gates. (Since the
  tick became one choice for the whole flow, what a gate does once is *untick*
  a row with no captions; the once-per-row rule is the same.)
- **The first checkbox in the document is no longer the semantics switch.**
  With the table ahead of the controls in the DOM, a test that reached for
  `input[type="checkbox"]` toggled a video's tick instead. The switch has an
  id now and the test asks for it by name.
- **`activity.heartbeat` raises outside an activity**, so every test in
  `tests/activities/` that calls an activity as a plain function needs the
  `activity.in_activity()` guard the existing paid stages already carry.
- **The i18n dead-key scan needs the dynamic segment last.** Its fallback looks
  for `prefix.${`, so `provider.secret.${name}.title` read as unused and
  `provider.secretTitle.${name}` does not. Every other dynamic group in the
  bundles already has that shape.
- **A revert-and-run loop in Rust needs a `touch`.** Restoring a file by
  `mv`-ing its backup back gives it an mtime *older* than the last build, and
  cargo skips the rebuild — so the restored fix reported as still failing. The
  same shape as the recorded `.pyc` trap for Python, from the other direction.

## Verified, and not

**Verified 2026-09-16, with nothing spent.** 161 worker tests, 7 Rust, 44
TypeScript, each one checked by reverting the fix it stands on and watching it
go red; the parity spec on the paid plane, checked by breaking the fork. The
free plane's nine routes are registered and answer their refusals with a kind.

**Not verified.** No channel has been synced, because no key exists. Nothing
here has run against Vertex, against YouTube, or in the real window. The
quote's figures above are from a synthetic channel. Whether the topic pass
reads a real auto-caption transcript well enough to be worth its $0.026 a video
is exactly the measurement the first real sync exists to produce.

## What is not built

- **The paid plane serves no channel route and the Angular client has no
  screen.** The vocabulary is forked into `src/runs/stages.ts` and the
  migration is in that repository, because the parity spec compares with exact
  equality and a `channel` run can appear in the queue that plane serves — but
  nothing there can start one. Root `CLAUDE.md` opens by counting four things
  so that this gap is a decision on the record rather than an omission.
- **Videos without captions reach the gate worse informed and far more
  expensive, at once.** There is no transcript, so the topic pass could not
  read them and their only evidence is a title; and at the published batch rate
  (~$0.024/min — published, not measured, and what
  `BRAIN_TRANSCRIBE_USD_PER_MINUTE` must be set to) a 76-minute talk is about
  $1.82 against the $0.1545 that same talk cost with captions. They are listed,
  quoted, flagged, and **start unticked**; the audio half itself has still
  never run on either plane, and `BRAIN_AWS_REGION`/`BRAIN_TRANSCRIBE_BUCKET`
  are unset on this machine.
- **A library made only of transcripts is the worst case for the dense floor.**
  Measured on the first real video: its scores cluster at 0.6001–0.614 against
  `MIN_SCORE = 0.60`, and `off_corpus` fires when nothing clears it. Measure
  before believing the asking half; and do not fix it the two obvious ways —
  prepending the title is measured and not a clean win, and a floor below the
  noise floor wins the metric by admitting what the floor excludes.
- **The quote does not know about caches.** `estimate_for` cannot see
  `cache/correct` or `docagent.embedcache`, and over-quoted a re-import by
  7.1x; a batch of re-imports will exhibit that multiplied by N.
- The Moodle HTML export in the old script has no successor here, by decision.
