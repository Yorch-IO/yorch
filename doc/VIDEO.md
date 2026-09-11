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

## YouTube refuses a datacentre, and only one call has to move

Measured 2026-09-05, after a real user pasted a URL against the paid plane on
EC2 and nothing appeared in the import queue at all. From the instance
(`i-0c7302467d8a702cc`, egress `34.218.169.144`) and from a residential address,
read-only:

| | laptop (`181.32.19.72`) | EC2 |
|---|---|---|
| `yt-dlp extract_info` | 200, 2.6 s | **refused** — "Sign in to confirm you're not a bot" |
| `youtube.com/watch` page | 200 | **200**, 1,170,129 bytes |
| signed caption URL, auto en | 200, 369 B | **200, 369 B** |
| signed caption URL, `yq6uVBsVkeQ` auto es | **429** ×6 over 25 min | **200, 582,176 B** |
| signed caption URL, manual en | **429** | **200, 440 B** |
| signed audio URL (`itag 140`) | 206 | **403** |

**It is the player API that is bot-checked, not the network.** The watch page
loads and every signed URL that is not IP-bound is served. So the split is one
call wide:

- **A caption URL carries `ip=0.0.0.0`** and an `expire` about seven hours out.
  Any host may fetch it — the blocked one fetched *every* URL this laptop was
  being throttled on. The caption download therefore **stays on the worker that
  owns the workspace**, which is what keeps the artifact store single-host. It
  also settles a question that would otherwise be open: a caption file measures
  **582,176 bytes for 4,573 s**, about 7.6 KB per minute, so a four-hour talk is
  ~1.8 MB and carrying captions in a Temporal payload was never going to work.
- **A media URL carries `ip=<the address that resolved it>`** and answers 403
  anywhere else. `fetch_audio` therefore cannot be split from the
  `extract_info` that produced the URL — it runs beside it. That is free: it
  writes no artifact, only a transient file it deletes in a `finally`, and
  returns an S3 URI.

So `resolve_video` was split out of `probe_video`, and `VideoRequest.fetch_queue`
routes it and `fetch_audio` to a worker on an address YouTube will answer.
Empty — the default — means "this queue", which is exactly the behaviour that
predates the field, so nothing changes until a deployment sets
`BRAIN_FETCH_TASK_QUEUE`.

**The narrowing is not tidiness.** `resolve_video` returns a `VideoInfo`, not the
info dict: on `yq6uVBsVkeQ` the raw dict is **1,656,277 bytes** of JSON, because
the video offers 161 automatic caption languages. `VideoInfo` is a few kilobytes.

**The queue name travels in the request, never read from settings inside the
workflow.** A workflow may only decide on what its own history holds; reading an
environment variable there would make replay depend on the machine replaying it.
Both planes stamp it at the route, beside where the paid plane stamps
`tenant_id`.

**`FETCH_START_TIMEOUT` is what stops this trading one silence for another.** Ten
minutes of schedule-to-start, which Temporal does not retry, so a queue nobody is
serving fails the run instead of parking it for seven days. `failure_of` names it
`fetch_worker_unavailable` rather than `activity_failed`, because the answer is
that a process is not running and not that an activity is broken. Note the trap
it walks past: `TimeoutType` is an `IntEnum`, so `str(cause.type)` is `"2"` and a
name match silently never fires — the same shape as the `event_type` defect the
raw-history panel already records.

**And `BRAIN_FETCH_TASK_QUEUE` is declared in three `environment:` blocks,
because `.env` is not container environment.** A value in `.env` is available to
*compose* for `${...}` interpolation and reaches no container by itself — the
same rule the Transcribe settings already record, arrived at the same way: the
first production deploy shipped a worker that could not name its own bucket
while the value sat correctly in `.env` two directories away, and nothing
failed. `api`, `worker` and `backend` all carry it now, empty by default.

It is deliberately **not** in `Stack::write_env`. That writer rewrites
`infra/.env` from a fixed list on every app launch, so anything it does not know
is erased — and a workstation has no reason to set this at all, since a
residential address is not the one YouTube refuses. It belongs to a hosted
deployment, whose `.env` is rendered by `apply.sh` in the `yorch-aws-platform`
checkout — which **does** carry it now, as `BRAIN_FETCH_TASK_QUEUE=$FETCH_TASK_QUEUE`
read from the `fetch_task_queue` key of the SSM config parameter. So turning the
split on in production is one key there and a process somebody keeps running;
this paragraph used to say the file did not carry it at all, and that was true
when it was written.

`worker/scripts/fetch_worker.py` is the process. Two activities, **no
workflows**, no stores, and AWS credentials only for the Transcribe path. It
reaches production Temporal through the SSM port-forward
(`deploy-brain.sh tunnel 7333 7333`). The cost is stated in its docstring and is
real: a video import only gets past `probing` while somebody is running it.

## The client can make the call instead, and for a paying customer it has to

Added 2026-09-10, after the split above met the case it cannot serve.

`fetch_worker.py` is the right answer for *this* deployment: a second worker on
an address YouTube answers, reached over the SSM port-forward. It is not an
answer for anybody else. Production Temporal is loopback-only — the security
group opens 443 from CloudFront and 80 for ACME, and nothing else — so serving
`brain-fetch` means AWS credentials, `session-manager-plugin`, a tunnel and a
process left running. A paying customer with a desktop app has none of those
and should need none of them.

So the second route is the one that needs nothing: **the desktop app makes the
refused call itself and sends the answer.**

```
app (residential IP)                      plane + worker (datacentre IP)
  yt-dlp extract_info  ──VideoInfo──▶  probe_video ─▶ caption download ─▶ …
  yt-dlp bestaudio     ──file────────▶  stage_audio ─▶ S3 ─▶ Transcribe
```

- **`VideoRequest.resolved` is why this is small.** `VideoInfo` was narrowed to
  cross a *task queue*, and it turns out to be exactly what is needed to cross a
  *plane*: measured on `yq6uVBsVkeQ`, the whole request body is **10,599 bytes**
  against a raw info dict of 1,656,277. Nothing else moves. The caption download
  stays on the worker that owns the workspace, because a caption URL carries
  `ip=0.0.0.0` and the 582,176 bytes of a 76-minute VTT have no business in a
  payload.
- **The workflow skips `resolve_video` when it is given one**, and calls it
  exactly as before when it is not — which is still right for local mode, where
  the container's egress *is* the user's egress, and for a hosted deployment
  serving `fetch_queue`. So this adds a route rather than replacing one.
- **It is not trusted, and the first check is not about safety.**
  `videosource.check_resolved` runs at the route and again inside `probe_video`,
  which is the frame that fetches `caption_url`. It refuses three things:
  - **An id that does not match the URL.** `source_key` comes from the URL and
    the transcript comes from the record, so a mismatch indexes one video's
    words under another video's identity, with a locator that deep-links to the
    wrong recording — and *nothing downstream fails*. It is the same shape of
    damage as the `VersionNode.tenant_id` default: a document that is quietly
    not what it says it is.
  - **A caption URL that is not YouTube's.** This is the SSRF guard, and it is
    needed only because the record now comes from outside. `video_id`'s
    allowlist does not cover it: that one governs which *video* may be named,
    this one which *host* may be read. Verified live on 2026-09-10 against the
    free plane — `evil.example`, `youtube.com.evil.example`, plain `http`, and
    the right host with `/watch` instead of `/api/timedtext` are all 422
    `resolution_not_trusted`.
  - **A `chosen` and a `caption_url` that disagree**, because `probe_video`
    branches on one and reads the other, and the incoherent pair is the branch
    that silently pays Amazon for a video that had captions.
- **The audio half could not stay on the client, and could not leave it
  either.** A `googlevideo` media URL is bound to the address that resolved it,
  so only the app can download it; S3 needs the instance role, so only the host
  can store it. `POST /videos/audio` is the seam: the file streams to that
  organisation's inbox and `stage_audio` moves it to S3 from the host that holds
  the role. It is a route of its own rather than a longer `SUPPORTED_FORMATS` on
  `/uploads`, because that one buffers the whole body in memory at a cap chosen
  for a 900-page PDF, and an hour of audio would not fit in the worker's
  `mem_limit: 2g`.
- **`stage_audio` checks containment before it looks anything up**, and the
  order is load-bearing rather than tidy: the reuse branch **unlinks the path it
  was given**, so an unchecked path there is a cross-tenant delete. A test names
  the reason.
- **The audio is downloaded before the gate, and that is the one trade here.**
  The bytes cost nothing — no provider call — so they are not what the gate is
  guarding. The alternative is parking the workflow after approval until this
  machine sends the audio, which makes a run that stalls invisibly when the
  window is closed: the `ASK_TIMEOUT` failure in a third shape.
  What it does leave is up to a gigabyte in the inbox for a run that never
  transcribes, because `stage_audio`'s own `finally` is the only thing that
  deletes it and a **rejected gate** and an **already-indexed video** both
  return before reaching it. `discard_audio` covers those two.
  A run that *crashes* in between is not covered, and that is the existing
  property of the inbox rather than a new one: a file uploaded through
  `POST /uploads` whose ingest is never started stays there in the same way.
  Three orders of magnitude is what earns audio the explicit sweep.

**The app bundles yt-dlp as a Tauri `externalBin`**, resolved by
`ytdlp::binary()` as a sibling of the executable — which is where the bundler
puts it, verified by watching `tauri dev` stage
`binaries/yt-dlp-x86_64-unknown-linux-gnu` to `target/debug/yt-dlp` with the
triple stripped. It is **not committed**: 40 MB per platform and a release about
monthly, against a whole `.git` of 104 MB. `binaries/fetch.sh` pins the version
and checks the published sha256; a build without it fails at the bundler and a
`tauri dev` without it fails at `binary()` with `ytdlp_missing`, which the app
renders as a sentence telling you to run the script. Neither is silent.

`ytdlp.rs` forks `_tracks`, `_choose_track` and `_download_error_kind`, each
with a test naming the Python behaviour it reproduces — and `serde_json` is
built with `preserve_order` for one of them: Python walks the caption
dictionaries in insertion order and `choose_track`'s last pass returns the first
track matching nothing in particular, so sorted keys would pick a different
language from the server for the same video. It costs no new crate; `indexmap`
was already locked.

### The track a caller who said nothing gets

Found 2026-09-10, the moment the paid plane could reach a real video's gate for
the first time — which is the argument for closing a gap rather than recording
it, again.

`_choose_track` matched the language preference, then manual over automatic,
and with **no** preference took the first track it saw. YouTube offers an
automatic caption track in every language it can translate into, returns them
**in alphabetical order by code**, and marks the source one with an `-orig`
suffix. Measured on `yq6uVBsVkeQ`, a 76-minute talk in Spanish: 157 automatic
tracks — `ab`, `aa`, `af`, `ak`, `sq`, … — and the real one at `es-orig`.

So the gate offered to correct and index **Abkhazian**: a machine translation
of a machine transcription, named in the panel as a two-letter code in a line
of small print, under a heading that says nothing has been paid for yet. That
is precisely the shape of thing that gets approved.

The rule is now: **the original language before any translation of it**, as a
stable sort so it decides ties and reorders nothing else. It holds inside a
preference too, because `es` matches both `es` and `es-orig` by prefix and one
of those has been round tripped through a translator; it does not overrule a
preference that names another language; and manual still beats automatic,
which is a different ordering and a sound one.

It was not introduced by the client-side split — `_choose_track` is reached
identically from `resolve_video` — and the Rust fork reproduced it faithfully,
which is the parity guard working as designed. What the split changed is that
somebody could get far enough to see it.

**A refusal of *this machine* is a different kind from a refusal of the
server.** `youtube_refused_this_host` means the datacentre was refused and the
remedy is this whole path; `youtube_refused_this_machine` means the remedy has
already been tried, and the advice has to be "wait a few minutes" instead.

**Local mode is deliberately untouched.** The container's egress is the
machine's egress, so `resolve_video` is already answered there; routing it
through the bundled binary would only add a second copy of yt-dlp that goes
stale on its own schedule.

### What is verified, and the one thing that gates the rest

Verified 2026-09-10:

- The bundled binary resolves a real video from this machine — the exact video
  from the bug report, `yq6uVBsVkeQ`: 4,573 s, 157 caption tracks, caption URL
  on `www.youtube.com/api/timedtext` carrying `ip=0.0.0.0`.
- A client-supplied record drives the whole free half end to end. On that video
  through `POST /videos` on the free plane: 107 timed paragraphs, 62,016
  characters, 69 previewed chunks, a $0.2223 quote — and the Temporal history
  shows **12 activities scheduled and `resolve_video` not among them**.
- Every guard refuses live, with `resolution_not_trusted` and the message that
  says which rule fired.
- In the real window, in cloud mode: the button reads
  "Asking YouTube about this video, from this computer…" and the resolve runs
  here.

**Deployed to production the same day, and the deploy order is the thing to
know.** The paid plane's `ValidationPipe` runs `forbidNonWhitelisted`, so a
plane that has not been updated answers the new field with
`422 request.property resolved should not exist` — seen in the real window
before the deploy went out. The app change and the plane change ship together;
neither is useful alone, and the app is no worse in that window than before it,
since both ends in a failure and this one starts no run at all.

Verified in the real window against `https://brain-api.vervux.com` on
image `d1d51bf-8102f1c-dirty20260910T173929Z` — a tag that names the commits
the two trees were *on* when it was built rather than the ones they became,
because the build preceded the commit. It is the commit this paragraph arrives
in, and `89e45db` on the paid plane. That the image really holds that code is
checked rather than assumed, by the staleness guard's own test: no file under
`worker/`, `docaget/`, `src/` or `prisma/` is newer than the build stamp. The
next deploy from a clean tree names its commits itself. The video from the bug
report
reaches its gate reading *"YouTube's automatic captions (es-orig) — free, and
unpunctuated"*, 1:16:13, **69 chunks and 62,016 characters** — the same figures
the free plane produced for the same video, which is the cross-plane check that
comes for free here. Nothing has been paid for; the gate is where that is
decided.

One thing the deploy needed that is worth keeping: **the repository had no
`.dockerignore`, and `cmd_build` passes the repository root** as the worker's
build context because the image installs `docaget/` as a path dependency.
Measured 2026-09-10 that context was **12 GB**, 9.8 GB of it
`app/src-tauri/target`, on a disk with 11 GB free — a deploy that could fill
the volume. It is an allowlist now, matching the Dockerfile's four `COPY`
paths, so a corpus or a target directory added anywhere else cannot silently
re-enter it.

**And the audio half has never run.** No video with no captions has been indexed
on either plane, and nothing longer than 19 seconds has been indexed at all. It
is covered by tests at four layers — the activity, the workflow, the DTO and the
route — and by nothing that has spent a dollar at Amazon.

## A run that fails in its first activity is now visible

`POST /videos` answered 200, the URL box cleared, and **nothing ever appeared in
the import queue**. The workflow had failed 2.8 s in. Three things had to be true
at once for that to leave no trace:

1. `register_document` is what INSERTs the `run` row, and it is the **second**
   activity. `probe_video` runs before it.
2. `Catalog.finish_run` was a bare `UPDATE … WHERE id = %s`. Zero rows affected
   raises nothing, so `record_run_outcome` reported **Completed** having written
   nothing — event 13 of that history.
3. The import queue reads the catalog, by design, so that it answers with
   Temporal down.

`IngestWorkflow` has the same shape (`stage_source` before `register_document`),
so a file import that dies in staging was equally invisible; it had simply never
fired. Both workflows now call `open_run` first.

**Opening the row early is only half of it, and the other half is easy to miss.**
The import queue is **per-library**, and both planes filtered on `d.library_id`
through the document join — so a run with no document yet would exist and be
filtered straight back out of the screen that had just started it. `run` carries
its own `library_id` now, and both planes read
`COALESCE(d.library_id, r.library_id)`. It has no foreign key on purpose:
`ensure_library` runs inside `register_document`, so at run-open time the library
legitimately may not exist.

`run.label` is the second column: what the run knew about itself before it had a
document. For a video that is the URL somebody pasted; for an import, the file's
basename. `title` stays the *document's* title, and the queue reads
`title ?? label ?? workflow_id` — so a failed probe reads as a URL rather than as
`video-1788624193136-4fa22984`.

**`_open` is behind `workflow.patched`, the only patch in this codebase.**
Inserting a command at the head of a workflow's sequence is a non-determinism
error on replay, and an import parked at its gate for seven days is exactly the
history that would hit it. `_pending` and `_flush_pending` therefore stay: they
are still the live path for every execution that started before this shipped, and
they are why the patch is safe. Deprecate it once nothing older than that deploy
is open.

`register_document`'s own `start_run` is untouched — `ON CONFLICT (id) DO
NOTHING` makes it a no-op and `attach_version` fills in the ids immediately
after. The `kind` both calls pass goes through one `run_kind_of`, because the
first call is the one that survives the conflict and two copies of that
expression would eventually disagree.

## Two error kinds, because they call for opposite things

`_extract_info` collapsed every yt-dlp `DownloadError` into `video_unavailable`.
That is right for a private, deleted, age-gated or geo-blocked video — a decision
that will not change on a second attempt — and wrong for the one that actually
happened, where the video is fine and the caller is blocked.
`_download_error_kind` splits `youtube_refused_this_host` out of it, both kinds
still non-retryable, and the app renders `queue.errorKind.*` rather than printing
the identifier.

**And a 429 on captions must not quietly buy a transcription.**
`_download_caption` made one attempt and returned `b""` on any failure — which
sets `chosen = None`, which is the branch that pays Amazon. The measurement above
is what makes that concrete: this endpoint refused one address six times across
25 minutes while serving the same URLs elsewhere. Three attempts with backoff
now, and the fallback stays, because the gate still shows the bill before anybody
approves it.

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

**The split ran end to end on the local stack, 2026-09-05.** Image rebuilt with
all three overlays, migration applied, the container reporting
`schema 20260905180000_run_library_label`:

```
1. a run that fails in its first activity — the reported defect, reproduced
   POST /videos  https://youtu.be/aaaaaaaaaaa   ->  200, workflow started
   GET  /runs?library_id=lib_videos             ->  the row is there:
       state=failed  stage=probing  library_id=lib_videos
       label=https://youtu.be/aaaaaaaaaaa  title=null
       error_kind=video_unavailable
   run_event: (1, probing, -) (2, probing, failed)      <- previously: nothing

2. the routing, proved by withholding a worker rather than by adding one
   POST /videos  jNQXAC9IVRw  fetch_queue=brain-fetch, nobody serving it
   -> after 20 s: state=running stage=probing, and *already in the queue*
      with its label. The container worker has `resolve_video` registered on
      `brain-ingest` and did not take it.
   start scripts/fetch_worker.py on brain-fetch
   -> awaiting_approval, title "Me at the zoo", usd null
   reject the gate -> cancelled, error_kind=rejected, $0
```

`resolve_video`, `fetch_worker.py` and the two new `run` columns are also covered
by 722 worker tests, 121 on the paid plane and 428 in the app — the routing by a
test that *withholds* `resolve_video` from the main worker, so it cannot pass
unless the workflow really routes it.

**That second run fell back to Transcribe, which is the caption throttle
appearing in the wild.** The probe recorded four advertised tracks, `chosen:
null`, and `transcribe` in its identity basis: the manual `en` track would not
download after three attempts. That is not a flaw in the design — it is this IP.
The local container shares the developer's residential address, measured
refusing that endpoint six times across 25 minutes, while EC2 fetched the same
URLs at 200. In production the caption download sits on the host that *can*
fetch them, which is exactly why it was left there. What the run does show is
that the fallback is real and reachable, and that reaching it costs the whole
Transcribe bill — so `CAPTION_ATTEMPTS` is a floor under a measured risk rather
than a theoretical one.

**Deployed to production 2026-09-08**, image `be5061d-8102f1c`, with the three
pending migrations applied by the `migrate` service. The reported failure was
then reproduced from the host itself, against the exact URL from the report:

```
POST /videos  https://youtu.be/yq6uVBsVkeQ   -> 200, video-1788869591212-5bc8e240
GET  /runs?library_id=lib_videos
  state        failed          stage    probing
  library_id   lib_videos      label    https://youtu.be/yq6uVBsVkeQ
  title        null            usd      null
  error_kind   youtube_refused_this_host
  detail       ERROR: [youtube] yq6uVBsVkeQ: Sign in to confirm you're not a bot.
GET  /runs/{id}/audit
  probing  outcome=-
  probing  outcome=failed
```

Three days earlier the same request produced no row, no error and no trace.
`BRAIN_FETCH_TASK_QUEUE` is empty on that host and reaches all three containers
as such, so the split is deployed and **off**: the failure is legible, and no
video gets past `probing` from EC2 until somebody turns it on.

**And the deploy found one more dropped field.** `/runs` reported the URL for
that run and `/runs/{id}/audit` reported `label: null` for the same row:
`auditlog.build` hand-names its columns, while the paid plane's `auditRun` is
`runSummary` minus one key and therefore picked the new field up by itself.
Nothing failed — the client type declares `label` and null is legal for it. It
is now compared field by field against `RunSummary` in a test, so the next
column added to the queue fails until it is threaded through.

**What has still not run.** The fetcher has never run against production
Temporal, and no video has been indexed through the split all the way to an
index — both local runs stopped before the bill, deliberately, and the
production run above was the refusal itself. `fetch_task_queue` is wired
through terraform and `apply.sh` on a branch of its own in the
`yorch-aws-platform` checkout and **has not been applied**. Nor has any of this
been driven from the desktop window: every queue row above was read from
`GET /runs`, not looked at.

---

## What the first substantial video actually did

Indexed on production 2026-09-10, audited read-only 2026-09-11:
`La Transformacion de los Gobiernos y Naciones - Dario Silva`, `yq6uVBsVkeQ`,
1:16:13, 107 timed paragraphs, 62,016 characters, **69 chunks**, `$0.154545`.
Full account in `doc/AUDIT_VIDEO_20260910.md`; what belongs here is the part
that changes what this document claims.

**Three of the "not verified" items above are now verified.** A video longer
than 19 seconds went end to end; a gate was approved from the UI (15 seconds
between `awaiting_approval` and `correcting`, by a person, in the real window);
and correction ran on a transcript and *is* the stream the index holds —
`corrected.txt` verifies 69 of 69 spans and `transcript.txt` 0 of 69, so the
fallback did not fire. The audio half still has not run and still has no
`transcription` cost row anywhere.

**The gate over-quoted by 1.46×**, which is the permitted direction: $0.2257767
against $0.154545, almost entirely output tokens (25,839 projected, 16,733
spent). It named only `correction` and `embedding`, so the narrowing that fixed
the 2,300× over-quote is holding. Note that the `$0.2223` recorded above for
this video is the **correction line**, not the total.

**`_choose_track` is confirmed against the case that motivated it, from the
data.** The document carries two versions. The indexed one is `es-orig`; the
other holds `captions:ab:auto` — **Abkhazian** — and its run started 17:27:34Z
against a fixed image built 17:39:29Z, with the next run at 17:48:27Z choosing
`es-orig`. The deploy is legible in the catalog to the minute. What it leaves is
an orphan `pending` version row, recorded in root `CLAUDE.md`.

### `covered_s` is unclamped, and `end_s ≤ duration_s` is not an invariant

The cue table ends at **4574.699 s** against a probed `duration_s` of **4573**,
so the gate reported coverage *above* 100% and the last chunk ends 1.699 s after
the video does. This is not a grouping bug: YouTube reports duration as a
truncated integer while the caption track legitimately runs to the true end. An
audit that asserted `end_s <= duration_s` would mark every auto-captioned video
defective, so `auditversion.time_report` **reports** the overshoot instead. No
locator is affected, because a locator uses `start_s`.

### Two things this measured that were left open above

- **Correction's proper-noun gate inverts on auto-captions.** 22 of 107
  paragraphs rejected, every one for a name the captioner got wrong. Recorded in
  root `CLAUDE.md` under known defects, with the `cartel` → `Gardel` asymmetry
  that shows the mechanism.
- **"Whether putting the video's *title* in the breadcrumb would help retrieval"
  now has a number.** A transcript has no chapter and no section, so
  `embed_text()` sends bare speech while every book chunk carries
  `Chapter > Section`. The video's dense scores cluster at 0.6001–0.614 against
  `MIN_SCORE = 0.60`, and across the whole library **none of the top five hits
  are this video** for a question phrased in its own title. Scoped to the
  version, retrieval is fine.

  **The obvious fix was then measured, and it is not a clean win.** Re-embedding
  all 69 chunks with the title prepended lifts the mean best-chunk cosine by
  **+0.0217** over six questions and takes top-five entry from 1 of 6 to 3 of 6
  — but the entire gain is on the two questions phrased in the title's own
  words, and on those two the chunks clearing the dense floor go from 5 and 13
  to **all 69**. A title strong enough to lift the document makes every fragment
  of it look equally relevant, and `diversify` cannot temper that here precisely
  because a transcript has no sections for `PER_SECTION` to spread across. The
  handicap is real; the one-line fix trades it for a monoculture in the top-k.
  Table and method in `doc/AUDIT_VIDEO_20260910.md`.

### Auditing the next one

`worker/scripts/audit_version.py` has a sixth leg, `video`, gated on
`run.kind == 'video'`, and `worker/scripts` now ships in the worker image — so
the audit runs where the artifacts are:

```bash
# on the host, over SSM; the container already has all three store URLs
docker exec company-brain-worker-1 \
  python /app/worker/scripts/audit_version.py ver_… --json /workspace/audit.json
```

The **retrieval** leg is unreachable for a video and says so rather than
reporting zeros: `_recommended` switches `generate_evalset` off, so there is no
eval set, and `--measure` returns `unavailable` before it constructs an
embedder. A video has no recall figure and cannot be given one without paying
for a stage this workflow does not have.

**A rejected local run is a free reference fixture for a paid remote one.** The
same video rejected at the gate on a laptop left `captions.vtt`,
`transcript.txt`, `transcript.json`, `evidence.json` and `chunks.preview.jsonl`
**byte-identical** to production's recorded hashes — the identity basis carries
the video id, not the URL, so the `?si=` parameter that differs between two
pastes cannot fork it. Every free-half check was made against those bytes for
nothing, which is worth remembering before tunnelling anything.
