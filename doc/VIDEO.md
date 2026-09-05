# Indexing video

A video is not a document, and the two places that shows are the two places this
subsystem could not reuse: it has no bytes until something transcribes it, and
the fact a reader wants back — *where in the video was this said* — is a time,
not a byte offset.

Everything below is a decision plus the thing that forced it, the convention
`doc/CLAUDE.md` uses. Where a claim is unmeasured it says so.

Built and verified 2026-09-05. `VideoIngestWorkflow` in
`worker/brainworker/workflows/video.py`; the free plane's routes are `POST
/videos` and `GET /runs/{id}/video-gate`, mirrored in
`../yorch-tauri-backend/src/videos/`.

---

## The idea the whole thing rests on

`docagent.chunk.Chunk` already carried **`para_from` / `para_to`** — the
paragraph index range a chunk covers — and `correct.correct_paragraphs` is
contracted to return *the same number of paragraphs in the same order*.

So: **emit one paragraph per timed group of caption cues, keep a table of
`(paragraph index → start_s, end_s)`, and a chunk's time span survives
correction with no byte arithmetic at all.** It is
`table[c.para_from].start_s .. table[c.para_to].end_s`.

That is the only reason correction can stay switchable on a video without
invalidating every timestamp, and it is asserted against the real chunker in
`docaget/tests/test_transcript.py::test_a_chunk_recovers_the_times_of_the_cues_inside_it`.

**The one way it breaks** is a correction that returns a paragraph containing a
blank line: the rejoin then yields more paragraphs than went in and every
timestamp after that point silently belongs to the wrong chunk.
`videosource.repair_paragraphs` collapses it *before* the join and
`assert_aligned` refuses afterwards — repair first, because a hard failure after
correction has been paid for is the worst outcome available. Video-only:
a book's paragraph legitimately contains single newlines (verse, numbered
question blocks) and collapsing those would damage the corpus the engine was
measured on.

**And one way it degrades rather than breaks.** `_split_oversized` gives every
piece of a split paragraph the *same* `para_idx`, so a cue group at
`hard_cap_chars` would make several chunks report one span. `GROUP_HARD_CAP` is
900 against the chunker's 2000 to keep that path unreachable, and a test asserts
no two chunks share a paragraph range.

---

## What the chunker does to a transcript, and why it needs telling not to

**Heading detection is switched off** by capping both heading lengths at zero.
Every branch of `heading_level` that can return non-zero is gated on
`len(s) <= max`, learned patterns included, so zero disables all of them with no
special case in the chunker.

It has to be off, and not for tidiness. Measured on the shapes a Spanish
transcript actually produces: **`"2 Corintios habla de esto"` and `"1975 Fue un
año decisivo"` are level-1 chapters under the built-in rules.** And
`build_chunks` `continue`s on a heading — the paragraph's *text* never reaches
a chunk — so a false heading does not mislabel a transcript paragraph, it
**deletes** it, and takes its cue group out of the time table with it.
`videosource.uncovered_paragraphs` is what says so out loud;
`test_uncovered_paragraphs_names_the_ones_a_heading_swallowed` demonstrates the
loss under the default rules and its absence under `transcript.chunk_rules()`.

**The kind is injected, not relabelled afterwards.** A change of kind is a chunk
boundary, so relabelling finished chunks can only rename what the default rules
already cut. Without the constant classifier, `FOOTNOTE_RE = ^\d+\s` makes every
cue group starting `"2019 fue…"` a `nota`, and the question-density rule makes an
interview `preguntas` — which additionally *resets the section path*.

**`transcripcion` is a kind of its own**, Spanish on the wire like the other six
because kinds live in Qdrant payloads and filters. A transcript is speech a
machine heard, not prose an editor set, and a reader deciding whether to trust a
fragment should be told which. Note the trap it walks into:
`ExploreScreen.tsx` calls `t(\`explore.kind.${c.kind}\`)` with **no
`defaultValue`**, so an unlisted kind renders as the raw key.

---

## Identity

**Document identity is the video id, never the title** — an uploader can rename
a video, and `document_id` is `digest(library, source_key)`. `source_key` is
`youtube/<id>`, and `videosource.video_id` normalises the six shapes a link
arrives in so a different-looking URL for the same video converges. Verified on
the running stack: `watch?v=…&list=PLxyz&t=5` for an already-indexed video
short-circuited at `registering` and **spent $0**.

**`content_sha256` is computed before the gate, and that is the point.** It is
`sha256` of an `identity_basis` — video id, duration, upload date, the chosen
source, and either the caption bytes' digest or the audio format id — rather
than of the finished transcript. Deriving it from the transcript would mean
discovering a duplicate *after* the bill. The cost is that on the Transcribe
path it is a proxy rather than a content hash; on the caption path the bytes are
free to fetch, so it is a true one. `identity_basis` travels in the artifact so
the digest can be re-derived by hand rather than taken on trust.

**`byte_size` is 0 and honest.** A video has no byte size at registration.
Putting the duration there would make it a term in every "corpus size" sum.

---

## The locator carries neither the title nor the byte range

```
0:01 · https://youtu.be/jNQXAC9IVRw?t=1
```

`citation_id` is `digest(chunk, locator)` — the locator **is** the citation's
identity — and `_PRUNE_CITATIONS` exists because a mutable title in a locator
forked citations on a real rebuild. A YouTube title is mutable by its uploader
without notice, so it is out. The byte range indexes a corrected transcript
nobody will ever open, so it is out too. What is left is two facts that cannot
move: when it was said, and where to hear it. The title is not lost — it is on
the `DocumentVersion`, on the evidence, and in every Qdrant payload as
`source_title`.

The offset is **floored, never rounded**: rounding up can land after the word
being cited.

**Four edits carry the time, and they must all land.** `ChunkNode` already
declared `sheet` and `slide`, written to Cypher and rendered by `_locator` and
**set by nothing**, because no row schema carried them. So: `chunk_transcript`
writes `start_s`/`end_s` into `chunks.jsonl` through `indexing.chunk_row` (one
row schema, two writers, sitting beside `StoredChunk.from_row`, the reader it
must agree with); `project_structure` reads them with `row.get`; `ChunkNode`
declares them; `_locator` branches on `version.fmt in TIMED_FORMATS`. The same
edit finally populated `sheet` and `slide`.

**A citation with no locator is invisible**, not merely unadorned:
`answer._verify` drops it, so the chunk is retrievable and permanently
uncitable.

---

## Waiting for Amazon is a workflow timer, never a polling activity

The EC2 host stops nightly at 23:00 America/Los_Angeles and **nothing starts it
again on a schedule**. A `workflow.sleep` is server-side durable state: the host
can go down mid-wait and the workflow resumes where it was while Transcribe
keeps working. A polling *activity* is worker-local — the stop kills it, Temporal
retries the whole attempt, and the attempt begins by re-downloading the audio.
`poll_transcription` is therefore single-shot and one minute long, so it can
never be the thing in flight when the host goes down. `TRANSCRIBE_DEADLINE` is
days for the same reason: a timeout in hours would abandon jobs that had already
succeeded and been paid for.

## The one place `_charge`'s premise is false

`_charge`'s docstring says a retried activity spent its tokens whether or not
the attempt succeeded. **That is true of a generation call and false here.** The
job name is `brain-<version_id>` — derived, never generated — so a Temporal
retry hits `ConflictException`, finds the job Amazon already has, and must not
bill again. `start_transcription` charges only on the branch that created the
job.

Verified against the real account, not a mock: re-running exactly as a retry
would, `fetch_audio` returned `reused=True` (the S3 object is keyed on the
version, so no second download and no second upload), `start_transcription`
returned the existing job, and **no charge was recorded**.

## No ffmpeg, and that is measured

`bestaudio` with **no postprocessor**. Transcribe reads m4a/mp4/webm/ogg
directly, and yt-dlp delivered `.m4a` untouched on a real video — so the worker
image keeps its "no compiler, no toolchain" property instead of growing ~80 MB
of apt onto a 60 GiB volume that also holds the corpus. If yt-dlp ever returns a
format Transcribe cannot read, that is a refusal with a message, not a silent
pull of a build toolchain into a shipped container.

Audio is written to disk and streamed to S3 — nothing is buffered in memory,
because the worker is capped at `mem_limit: 2g` with `oom_score_adj: 500` and is
offered to the OOM killer first. It is deleted in a `finally` either way.

## `.env` is not container environment

The Transcribe settings are written into `infra/.env` by `apply.sh` **and listed
in the worker service's own `environment:` block**, and both are needed. A
variable in `.env` is available to *compose* for `${...}` interpolation and
reaches no container by itself.

That is not a hypothetical. The first production deploy shipped a worker that
could not name its own bucket while the value sat correctly in `.env` two
directories away, and nothing failed — `Aws.configured` was simply `False`, so
the gate would have refused to quote a transcription and a reader would have
gone looking for a broken IAM policy. Found by running `printenv` inside the
deployed container rather than by reading the deploy log, which said `==>
rendering /srv/brain/infra/.env` and was telling the truth.

**Credentials genuinely need no plumbing, and that part is verified.** Inside the
deployed worker, boto3 resolves
`arn:aws:sts::…:assumed-role/vervux-prod-brain-host/i-…` — the instance role,
reached through IMDS because `http_put_response_hop_limit = 2` on the instance
lets a *container* make that hop. It was set so google-auth could; boto3 takes
the same route.

## Cleanup is a lifecycle rule, not a delete call

The instance role is granted **no `s3:DeleteObject` anywhere**, deliberately —
"the host has no reason to be able to delete a backup". So a fourth S3 lifecycle
rule expires the `transcribe/` prefix after 7 days, which also survives a run
that died where a delete call in that run would not have. That rule is *not*
optional: the two existing prefix-filtered rules cover nothing else, and without
it every video's audio would sit at full Standard price for ever.

---

## What the gate quotes, and the two ways it got that wrong

**Only the stages that can actually run.** `estimate_video` is called with the
*narrowed* options, not the caller's. It was called with the raw ones, and a
19-second video was quoted **$0.027837** — profile learning and semantics, which
have no stage in this workflow — against a real bill of **$0.000010**. A 2,300x
over-quote. Over-reporting wildly misleads a user into declining affordable work
exactly as much as under-reporting misleads them into approving an expensive
one.

**A zero-token row renders as "—".** Transcribe bills seconds of audio, so zero
tokens is *true* there; a pair of zeroes reads as a stage about to do nothing.

**`preview` is `null` when there are no captions**, and that is why
`VideoGateReport` is its own type rather than `GateReport`. There is no text to
chunk until the money is spent, and a fabricated chunk count is precisely the
lie a gate exists to prevent. (`GateReport.preview` is a required `Preview`
holding two required `ArtifactRef`s, so it could not have said this.) Its own
route for the reason `/runs/{id}/rebuild-gate` already documents: querying
through the wrong typed handle decodes one report into the other and drops every
field they do not share, **without failing**. The import queue therefore branches
on `run.kind === "video"` and calls `videoGate`.

### The speech rate, measured

`CHARS_PER_SECOND_OF_SPEECH` projects a correction bill from a duration on the
one path where there is no text to count. **Measured 2026-09-05 over 11.21 hours
of real Spanish preaching and theology video** — 20 videos, 548,750 characters,
the domain this corpus indexes — by `worker/scripts/measure_speech_rate.py`,
which runs the captions through the same `group_cues` the pipeline uses:

```
pooled 13.59 c/s · median 12.40 · p10 11.46 · p90 14.69 · max 15.24
```

It measures **captions** and projects **Amazon's** output, which are not the same
text: on `jNQXAC9IVRw` the captions came to 217 characters and Transcribe's
transcript of the same audio to 225 — about 4% more, because Transcribe keeps
the fillers a caption writer drops. So the pooled figure is a floor.

The constant is **14.5**, and it was **16** when it was a guess. 16 sits above
the *fastest* video in an 11-hour sample, so it was not a low end at all — it
over-reported every quote. The guess happened to be safe and was still wrong.

`SPEECH_RATE_SPREAD` is 1.40, giving a high end of 20.3 c/s against a measured
maximum of 15.24 (15.85 with the uplift) — about 28% of headroom. **It was
declared and used by nothing** until the measurement went in, which is the same
defect as `ChunkNode.sheet`: it looked like it did something, a test asserted a
property of it, and no quote was ever any wider for it.

### Correction's default is per source, and it is a default because nothing is measured

`videosource.correction_default`: **on for automatic captions, off for a manual
track and off for Amazon's own output.** Auto-captions arrive with no
punctuation at all, which is the one deficit correction closes that
`docagent.correct.verify` will not reject — punctuation is neither a proper noun,
a figure nor a scripture reference. A manual track was written and punctuated by
a person, and Transcribe punctuates its own output (confirmed by the real run).

It is a *default* and not a rule because correction's value on a transcript is
**unmeasured** — the $0.0334 that sets the gate's position was measured on a
book — and the honest place for an unmeasured cost is behind a box somebody
chose. The gate says which case it is in, in words, and in the tense that case
deserves: at the gate an Amazon transcript does not exist yet, so it may not be
described as *already* punctuated.

---

## The stage vocabulary

`VIDEO_STAGES` is 13 stages. **A display order, not a schedule**, with the same
caveat `INGEST_STAGES` makes about `extracting` running twice: with captions,
`grouping` runs *before* `previewing` and neither `fetching` nor `transcribing`
happens at all; without them, `previewing` quotes from the duration and those two
run after approval with `grouping` after them.

`grouping` is a stage of its own rather than folded into whichever half produced
the cues, and that is what keeps `ARTIFACT_STAGES` honest: the transcript is
built the same way from either source, and an artifact attributed to two stages
would make that map a lie on one of the two paths.

`run.kind = 'video'` needed a migration (`20260905060000_run_kind_video`), and
the load-bearing reason is not the ledger: **it is how a client knows which gate
shape to expect.** `cost_entry.stage`, `cost_entry.provider` and
`document.format` carry no CHECK, so `transcription`, `aws` and `youtube` needed
none.

`stages.ts` in the paid plane mirrors all of it and `stages.parity.spec.ts`
compares `cost_stages` and `artifact_stages` with exact equality — so those
additions are a cross-repo commit, not an optional follow-up.

---

## Reuse, and the two seams that had to be new

Nine of the workflow's activities are existing code called unchanged —
`register_document`, `link_duplicate`, `correct_text`, `project_structure`,
`embed_and_index`, `activate_version`, and the bookkeeping three.
`IngestRequest.run_kind` is what lets `register_document` serve a run that is
not staging a file. `embed_and_index` needed nothing at all:
`StoredChunk.from_row` reads only fields a transcript row already carries.

**No `extract/transcript.py`.** The extractor contract exists to feed
`resolve_profile`'s fingerprint, and profile learning does not run for a video.
One fewer seam.

**No fabricated `ArtifactRef`, ever.** A reference carries a sha256 and
`ArtifactStore.read_bytes` verifies it, so a locator built from a path and an
empty hash fails the check it exists to pass. A `_ref()` helper that did exactly
that stopped the first real run dead in `grouping`, three retries deep.
Activities return their real refs instead (`VideoProbe.probe_ref`/`.captions`,
`Transcribed.evidence`): probing cannot *record* them before the `run` row
exists, which is a different thing from being unable to return them.

`chunk_transcript` reads **through** the refs, unlike `chunk_final` which opens
the corrected text by path. Here it matters: the text and the cue table have to
be a matched pair, and a stale one of either produces confident timestamps
pointing at the wrong moment. If correction moved the paragraph count despite
the repair, it falls back to the uncorrected stream and says so — a wrong
timestamp is worse than a missing correction, because it is an unverifiable
citation that looks verifiable and the only way a reader finds out is by
clicking it.

**`/reindex` needed a branch on both planes.** A video's `source_path` is its
URL, and `IngestWorkflow` would take it through `stage_source`, which checks
tenant containment on a filesystem path and dies three frames from the cause.

**The URL allowlist is the SSRF guard**, and it is applied at the route *and* in
the activity — the same doubling `retrieve.search` keeps for `tenant_id`. yt-dlp
ships ~1800 extractors and a `generic` one that will fetch an arbitrary host,
and unlike a file import there is no `Paths.contains` to inherit. Playlists and
channels are refused rather than expanded: a caller that silently indexed the
first video of a playlist would be answering a question nobody asked. The
TypeScript fork carries a live parity spec over 21 URL shapes.

---

## What a video does *not* get

**Semantics, profile learning, the eval set and tuning have no stage at all** —
absent rather than switched off, which is what a separate workflow buys. The
visible consequence: **a video does not appear on the Graph screen**, because
`library_mentions` derives its node list from `MENTIONS` edges and those come
from semantic extraction. A video is a `Document` and a `DocumentVersion` in the
graph, fully browsable and citable, and absent from the library canvas. That is
the chosen trade, not a projection that failed, and it is reversible later at the
cost that stage carries.

**A transcript has no sections**, so it carries no breadcrumb. Honest: a talk has
no table of contents. Whether putting the video's *title* there would help
retrieval is a real and measurable question, deliberately not answered by
guessing.

---

## Verified, and not

Indexed end to end on the local stack, 2026-09-05
(`https://youtu.be/jNQXAC9IVRw`, 19 s, manual English captions):

```
gate     1 paragraph · 217 chars · covers 18.881s of 19s · 1 chunk
         estimate: embedding $0.000012          (only what can run)
billed   embedding · 51 tokens · $0.000010      => over-reported 1.2x
run      kind=video state=succeeded, 11 audit events
locator  0:01 · https://youtu.be/jNQXAC9IVRw?t=1
```

The Transcribe path run against the real AWS account on the same audio: job
completed in ~10 s, 2 paragraphs, 225 chars, chunk `[1.4-18.6s]`; the retry
charged nothing; `abandon_transcription` deleted the job.

Deployed to production 2026-09-05, image
`7bc8cdb-c238668-dirty20260905T144734Z`. `POST /videos` answers **401
`unauthenticated`** where an unknown route answers 404 — the discriminator that
proves it is registered — and the worker reports 13 stages, the measured
constants and a working instance role.

**Not verified.** Nobody has approved a video gate *from the UI* — the panel and
all three gate states were screenshotted in the real window and in a static
render, and clicks worked, but synthetic keystrokes do not reach the WebKit
webview on this machine, so the typing-and-approving path is untested by
anything but code. Playlists, channels, and any video longer than 19 seconds are
also untried.
