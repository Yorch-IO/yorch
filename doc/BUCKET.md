# Indexing a customer's own S3 bucket

Built 2026-09-16 and 2026-09-17. A second way into the pipeline, beside a
picked file, a YouTube video and a YouTube channel: **a bucket somebody already
has**, walked object by object, quoted per recording, and indexed through the
same gate as everything else.

Read this before touching `brainworker/s3source.py`, `audioprobe.py`,
`bucketstore.py`, `activities/bucket.py`, `workflows/bucket.py`,
`workflows/timed.py`, `app/src-tauri/src/whisper.rs`,
`app/src/lib/localTranscriber.tsx`, `BucketScreen.tsx`, or the paid plane's
`src/buckets/`. It holds the rules that are not visible from any one file.

**Paid plane only, by decision.** The free plane serves none of these routes:
the feature needs AWS credentials the free plane's `Aws` settings do not carry,
the transcription it quotes is billed to *this* deployment's account, and a
self-managed stack has no organisation to bill. `_aws()` refuses before a quote
if anybody starts the workflow by hand there. **Both clients get the screen** —
the count at the top of the root `CLAUDE.md` is why that sentence is written
out: "paid plane" is a statement about one of two backends and says nothing
about how many clients exist.

---

## The shape of it

```
POST /buckets            register + walk the listing          free
GET  /buckets            what this organisation registered    free
GET  /buckets/{id}       the catalogue, joined with Postgres  free
POST /buckets/{id}/sync  walk it again                        free
POST /buckets/{id}/probe one `audio` run per key, each parked free
DELETE /buckets/{id}     forget the catalogue                 free
GET  /runs/{id}/audio-gate          the quote for one object  free
POST /runs/{id}/approve             the existing gate verb    spends
POST /runs/{id}/transcript          a transcript this machine made
POST /runs/{id}/transcriber         give up on the local one
GET  /libraries/{l}/documents/{d}/media?t=  a presigned link, minted on click
```

Everything before `approve` is free, which is the whole point: a corpus of 147
recordings was quoted at **$232.91** and nothing was spent to learn that.

---

## A bucket is a library, and the catalogue is a file

`lib_s3_<sha1(bucket/prefix)[:12]>`, the same decision a channel records: a
retrieval narrows by equality on `library_id`, so "ask only this archive" *is*
"ask this library". One library per **(bucket, prefix)** — two prefixes of one
bucket are two archives, because that is how a person keeps sermons apart from
interviews.

`tenants/<t>/buckets/<id>/{bucket.json,objects.json}` holds what the listing
said; **Postgres holds what is indexed**, joined on every read of
`GET /buckets/{id}`. Two records of one fact can disagree in silence, and the
one that would go stale is the one a person makes decisions from. Same rule as
`channelstore.py`.

A sync is page by page and stops early only at a page it already knows, with
the same etag and size. `_describe_object` used to re-read the head of every
object whose container it had not recognised, on every sync; it skips on an
unchanged etag now regardless. And `sync_bucket` calls `_ensure_library`,
because a sync that catalogued 147 objects into a library row that did not
exist left the picker empty.

## Identity is the object, not the path

`content_sha256 = sha256("s3\n<bucket>\n<key>\n<etag>\n<size>")` — a proxy, like
the video path's, and for the same reason: hashing 5 GB to decide whether to
index it would cost more than the decision is worth. `source_key` is
`s3/<bucket>/<key>`, so `document_id(library, source_key)` is stable across
syncs and a re-listed object is the same document.

**The quote is bound to the bytes it was made for.** `fetch_object` re-heads the
object and refuses with `object_changed_since_quote` when the etag or the size
has moved: a recording replaced between the gate and the approval would
otherwise be transcribed at a price nobody was shown.

## Duration without ffmpeg

`audioprobe.py` reads the duration out of the container's own header, because
the quote is `seconds × the published rate` and a *wrong* duration is a wrong
price. MP3 (Xing/VBRI/CBR), MP4 `mvhd` (head or the last megabyte), FLAC
STREAMINFO, WAV. Measured against `ffprobe` on nine real files: **within
0.03%**.

When the header cannot be found the duration is **estimated at 64 kbps and the
row says so** — never silently. `needs_body` exists because of one measured
case: an ID3 tag of 88 KB pushed the frame header past the 64 KB first read, so
a second ranged read was added and the corpus went from *one* estimated
duration to **0 of 147**.

## Access to the customer's bucket

`sts:AssumeRole` with **ExternalId = the tenant id**, into a role the customer
creates in *their* account. Nothing in the app or the catalogue is a secret:
the role ARN is public information and useless to anybody who is not the
principal its trust policy names. An empty `role_arn` means "the credentials
this process already holds", which is only right when this deployment's own
account owns the bucket — which is the case for the first corpus.

**Transcribe runs on our copy, in our account.** The zero-copy version — point
Transcribe at the customer's bucket — was rejected because it bills the
customer's account for a job this product started, and because the role
chaining would have to outlive an hour-long job. The worker streams the object
to `transcribe/<tenant>/<version>.<ext>` in this deployment's own bucket and
deletes it afterwards.

## One run per object, and the sum is on the screen

`POST /buckets/{id}/probe` starts an `AudioIngestWorkflow` per ticked key,
sequentially, collecting per-key failures — the `ImportScreen.start` loop, on
the server. Each run parks at **its own** seven-day gate with **its own**
estimate, and the screen adds them up.

That is deliberately not a batch gate. A batch gate would have to decide what
happens when one recording of 147 cannot be read, and the answer people
actually want — index the other 146 — is what a run per object gives for free.
The screen's total is a sum of quotes, not a second kind of promise.

`AudioIngestWorkflow` is `VideoIngestWorkflow` with `probe_object`/`fetch_object`
in place of `resolve_video`/`fetch_audio`; everything after the transcript is
shared verbatim in `workflows/timed.py::TimedIngest`. The extraction was proved
by a replay fixture: the commands before the gate are unchanged, so a run parked
when the old code shipped still replays.

## The transcript is written back to the customer's bucket

`transcripciones/<key>.json` beside a sidecar naming the engine, the model and
the `audio_sha256` it was made from. Two reasons, and the second is the one that
pays: the customer keeps the artefact they paid for, and `check_archive` finds
it on a re-index — **so re-indexing a recording costs $0 in transcription**.
The sidecar's `content_sha256` must match, or the archive is ignored rather
than trusted; and either engine's transcript is accepted, read through
`docagent.transcript.transcript_engine`.

`archive_prefix: ""` switches the write-back off.

## What retrieval gained

A sermon archive is the first corpus here where *when* and *about which
passage* are the questions people arrive with.

- **`recorded_day`** — days since epoch as an integer payload field
  (`EPOCH_ORDINAL = 719163`), because Qdrant ranges on numbers and not on ISO
  strings. `Question.recorded_from/recorded_to` are `IsoDay`-patterned, so a
  malformed date is FastAPI's own 422 rather than a hand-raised kind.
- **`scripture_refs` / `scripture_chapters`** — `brainworker/scripture.py`
  carries the 66-book Spanish table; a question naming "Juan 3:16" narrows on
  the reference and one naming "Romanos 8" narrows on the chapter.
- **`source_name`** — which feed the manifest said a recording came from.
- **The media link** is minted on click and never stored: `MediaLinkWorkflow`
  presigns through the customer's role and appends `#t=<seconds>`, so a citation
  opens the recording at the second it quotes. `source_url` — the public feed's
  own link, from the manifest — rides beside it, because that one never expires.

The locator reads `m:ss · s3://bucket/key`: `TIMED_FORMATS` in
`graph/projection.py` is `{"youtube", "audio"}` and `_heard_at` turns the
`source_key` back into an `s3://` URL.

---

## Transcribing on the person's own machine

Added 2026-09-17. Transcription is the one stage of a recording that costs real
money — **$0.024 a minute, $232.91 for the first corpus** — and the machine a
person is sitting at may hold a GPU that does the same work for nothing but
electricity and hours. The worker cannot use it: it runs in a container on a
host with no GPU. So the desktop app makes the call only it can make, exactly as
it already does for the YouTube player API.

**whisper.cpp, bundled, not Python.** `app/src-tauri/binaries/fetch-whisper.sh`
builds or downloads `whisper-cli` at a pinned commit, and the binary is
**not in git** — the same judgement `fetch.sh` records for yt-dlp.
`-DBUILD_SHARED_LIBS=OFF` is what keeps it one file: Tauri's `externalBin`
copies one file per entry, and a default build leaves `libggml.so` beside it.

**The engine is chosen per batch, before quoting** — a selector beside the
stage switches on the Bucket screen. It cannot be chosen at the gate, because
the gate *is* the quote: a run quoted at $1.90 and then transcribed for nothing
is a receipt for work nobody did, and the reverse is the failure this product
refuses outright. A local run's gate reads `transcription · whisper.cpp (local)
· $0`.

**A run waits, it does not hold anything open.** After the gate a local run
enters stage `transcribing` with the new state **`awaiting_transcript`**
(migration `20260917100000_run_state_transcript`) and waits up to **fourteen
days** for a `transcript_ready` signal. That is a seventh run state, and it
earned one: it is not `running` (nothing is in flight on the worker) and not
`awaiting_approval` (the queue would render a gate over a run already approved,
with buttons that spend). It is the first wait in this product that is neither a
gate nor work.

**`use_remote_transcriber` is the way out, and it is not an approval.** The
workflow re-quotes with Amazon's price, re-parks at its gate, and the person
approves *that* — which is why the route answers `state: awaiting_approval`
rather than `running`. The switch carries the run's **own** options through
`_recommended(options)`; an early version re-quoted with `StageOptions()`
defaults and silently turned semantics back on.

**What travels.** The app downloads the audio through the presigned link the
plane mints — the customer's bucket, the customer's role — so it holds no
credential, and it sends back a few hundred kilobytes of JSON through
`POST /runs/{id}/transcript`, which lands in the organisation's inbox exactly
as `POST /videos/audio` does. `stage_transcript` checks the path is inside the
tenant's tree before opening it and deletes it either way.

**Containers are forked, and the fork is measured.** `whisper-cli --help` at
v1.8.2 prints "supported audio formats: flac, mp3, ogg, wav", so
`whisper.rs::DECODABLE` is those four and the paid plane's `LOCAL_CONTAINERS`
is the same list. `transcriberFor` starts an object in any other container on
**Amazon whatever the batch asked**, and the probe's answer says so per key —
because the alternative is a run parked fourteen days for a transcript no
machine on that side can make. Four of the first corpus's 147 recordings are
M4A.

**Models are downloaded, verified, and never bundled.** The default,
`large-v3-turbo`, is 1.6 GB. It is fetched on first use into the app's data
directory and checked against a sha256 pinned from the publisher's own LFS
metadata; a mismatch **deletes the file** rather than keeping a model that would
run and transcribe nonsense. Verified 2026-09-17 by downloading it: the file is
1,624,555,275 bytes and hashes to `1fc70f77…e2bc69`, exactly the constants in
`whisper.rs`.

**The backend is read off the line whisper.cpp prints for that question**, not
off `system_info`. Verified against both builds on this machine: the CPU one
prints `whisper_backend_init_gpu: no GPU found`, the CUDA one
`whisper_backend_init_gpu: using CUDA0 backend`. `system_info` lists what the
*build* was compiled with — the CUDA build's reads
`CUDA : ARCHS = 890 | PEER_MAX_BATCH_SIZE = 128 | CPU : SSE3 = 1 | …` — so a
binary that has CUDA and found no device prints almost the same line as one
that is using it. Reporting that as a GPU run would make a CPU-speed
measurement look like a GPU one, which is the one number this feature is
judged on.

**The app says both things on the Services screen, and they are different
claims.** *Installed or not* is a badge over the binary's own path.
*GPU or CPU* is two different sentences depending on where the answer came
from: `backend` is read off the build's dependencies and is rendered as a
**guess**, while `device` is what whisper.cpp itself named and is rendered as a
fact with its evidence — `probe` (a model was loaded and nothing transcribed)
or `run` (a whole recording, which never loses to a probe). A person deciding
between $233 and a weekend needs the second, and the first is wrong on exactly
the machines where it matters: a laptop that parks its discrete card, a driver
older than the toolkit the binary was built with, a container with no
`/dev/nvidia*`. All three hint `cuda` and run on the processor, an order of
magnitude slower. The same word appears as a badge beside the engine selector
on the Bucket screen, where the choice is actually made.

**`whisper_probe` asks the binary and transcribes nothing.** whisper.cpp names
its device *while loading a model*, so the probe writes 1.5 s of silence as a
WAV (under a second is refused: `seek_end < seek_start + 100`), starts the
binary against the **smallest downloaded model**, reads stderr until the line
arrives and kills the child. With no model downloaded the question cannot be
answered at any price, and the button says so rather than being hidden.
`PROBE_TIMEOUT` is **180 s and was measured**: the first version was 60 s on
the reasoning that a model load is "about a second from the page cache", which
was true of the CPU build and wrong of the one people will ship — the CUDA
build takes **38.7 s** to reach that line, most of it a CUDA context and
1.6 GB into VRAM. The 60 s version failed against the real binary the first
time it was run.

**The decoder must not read its own transcript back to itself, and finding
that out cost two recordings.** whisper.cpp's `--max-context` defaults to -1:
every word already produced is fed into the next window as a prompt. Once the
model repeats a phrase it reads that repetition back and latches on, emitting
the same line until the audio ends. **Nothing fails** — the JSON is valid, the
size is ordinary, the process exits 0 — so it is invisible to every check this
product had.

Measured 2026-09-18 over the thirteen recordings already indexed, each
re-transcribed with and without `-mc 0`, `large-v3-turbo` on an RTX 4060:

| | longest identical run | damaged of thirteen | distinct words |
|---|---|---|---|
| default | up to **167** | **2** | 22,073 |
| `-mc 0` | at most **3** | **0** | **22,199** |

Read the last column first. On the worst recording the default produced *657
more words and 56 fewer distinct ones*: in the closing 2.9 minutes it emitted
one phrase 167 times where `-mc 0` finds 44 segments of 43 different lines —
the altar call. **The loop does not pile junk on top of speech, it replaces
it.** Both damaged recordings had been indexed, chunked, embedded, answered
from, and one had its corrected text archived into the customer's bucket before
anybody measured.

Two things follow, and both are in the code now. `run_cli` passes `-mc 0`, and
`stage_transcript` **refuses** a transcript whose longest identical run reaches
`LOOP_RUN` — every transcript passes through it, whoever made it. Refusing
rather than warning is a decision about cost: re-transcribing is free, while
accepting buys correction and semantics over fabricated text and then leaves
somebody to find it in an index. The thresholds are where this corpus splits —
healthy reach a run of 7, damaged start at 51 — not where taste put them.

`--vad` was measured too (longest run 1, coverage 98.3%) and is deliberately
**not** used: it merges segments, 1,013 to 748 on the same recording, and a
segment's start is what a citation points at. It would coarsen every locator in
the corpus to buy what `-mc 0` already bought.

**The speed is measured, never derived.** A desktop 4060 and a laptop's
integrated chip both report `cuda` and are an order of magnitude apart, so the
Services panel prints the ratio the last run produced and says plainly when
there is none. That figure is what turns "147 recordings" into a number of
hours, and hours is the currency this option is paid in.

**The queue is derived from the plane on every poll and holds nothing.** Same
decision as the import queue: the run is parked in the catalog from the moment
it is approved, so a relaunch, a crash or a second machine all see the same
list. One recording at a time, because a transcription saturates whatever it
runs on and two would each take twice as long. It lives above the screens and
outside their key, so switching tab does not stop it; a change of *plane* does,
because the parked runs belong to the organisation that was signed in.

### Measured 2026-09-17 on this machine

whisper.cpp v1.8.2, `large-v3-turbo`, against the same 12.12-minute recording
(727.1 s) that Amazon had already transcribed. The CUDA toolkit arrived
mid-session, so both builds were measured on the same audio:

| | Amazon Transcribe | whisper.cpp, CPU | whisper.cpp, RTX 4060 |
|---|---|---|---|
| wall time | ~2 min, on the server | 7 min 27 s | **38.5 s** |
| cost | **$0.291343** | $0 | $0 |
| speed | — | 1.63x real time | **18.9x real time** |
| encoder | — | 11,005 ms per 30 s window | **114.7 ms** — 96x |
| peak host RAM | — | 2.0 GB | 0.5 GB |
| output | 1,903 word cues | 398 segments | 411 segments |

**The GPU is 11.6x this CPU**, and the encoder — which is all of the work — is
96x. Extrapolated to the whole corpus, 9,704 minutes:

| | |
|---|---|
| Amazon | **$232.91**, ~an hour of wall time, nothing to keep running |
| this CPU | $0, **99 hours** |
| this GPU | $0, **8.6 hours** |

**Word agreement: 97.1% GPU against Amazon, 96.9% CPU against Amazon, and
98.4% between the two whisper runs.** The two whisper runs differ at all
because CUDA and CPU kernels are not bit-identical and the decoding diverges
from there, which is worth knowing before treating a transcript as reproducible.

What the differences are:

- Amazon writes digits (`4`, `10 mitos`); whisper spells them (`cuatro`,
  `diezmitos`). `correct.verify` protects numbers, so the two produce different
  corrections of the same speech.
- Amazon capitalises far more — `Iglesia`, `Católica`, `Asamblea`, `Leonardo` —
  where whisper's output is closer to sentence case. That interacts with the
  recorded proper-noun rule in `correct.verify`, already known to protect a
  captioner's *mistakes* on transcripts.
- **The hard proper noun, counted rather than sampled.** The sermon is about
  Campus Crusade, so `Bill Bright` is said nine times. Amazon: **8 right, one
  `Bill Bry`**. whisper on CPU: **4 right, five `Vilbrai`**. whisper on GPU:
  **4 right, five `Bill Bray`**. So Amazon is better on names here, and the GPU
  run fails more gracefully than the CPU one — `Bray` is a name, `Vilbrai` is
  not a word. An earlier note in this file said flatly that "whisper wrote
  Vilbrai"; it wrote the name correctly in four of nine places, and the
  distinction matters because `correct.verify` refuses a correction that drops
  a proper noun, so a *partly* right name is the case that survives into the
  index.
- whisper's cues are ~1.8 s segments rather than words, so a citation's second
  is the start of a segment. The product promises `m:ss`, which that satisfies.

The CUDA build is `fetch-whisper.sh --cuda` with `nvcc` 12.4 present: 109 MB,
dynamically linked against `libcudart.so.12`, `libcublas.so.12` and
`libcuda.so.1`. **Those three are why a CUDA build is not yet shippable as it
stands** — the driver library comes with the driver, the other two do not, and
bundling them is what makes the project's own Windows CUDA zip 457 MB against
3.8 MB for the CPU one. That is an open packaging decision, not a defect.

---

## What has run, and what has not

**Run, against the real corpus and the live stack:**

- A sync of 147 objects in **9.6 s**, 0 durations estimated, manifest joined.
- A quote of all 147: **$232.91**, free.
- One recording indexed end to end on Amazon: quoted $0.2913856, **billed
  $0.291343**, transcript written back to the customer bucket, four citations
  answered with `m:ss · s3://…` locators under a date narrowing.
- A quote of one recording with `transcriber: local`: **$0 for transcription,
  model `whisper.cpp (local)`, $0.0009778 total** against the $0.4856 Amazon
  would have charged for the same 20 minutes. Cancelled afterwards; $0 spent.
- whisper.cpp against a recording Amazon had already transcribed, on both a CPU
  and a CUDA build — the table above.
- `whisper_probe` against the real binary, through the `#[ignore]`d live test in
  `whisper.rs`: `{"backend": "cuda", "device": "CUDA0", "how": "probe",
  "model": "large-v3-turbo"}`. It found the 60 s timeout, which is the whole
  reason a live test exists for a thing the unit tests already cover.

**Not run:**

- **No transcript has gone through `POST /runs/{id}/transcript` on a live run.**
  Every layer is covered by tests — the workflow's park-and-signal, the plane's
  multipart landing, the app's queue — and nothing has driven the whole chain,
  because the next step after it spends (embedding, and the owner's standing
  rule is that no further spend happens without their word).
- **The CUDA build has not been packaged**, only built and measured here. The
  three CUDA libraries it links are present on this machine because the toolkit
  is; an installer that ships them is the open decision above.
- **Nobody has pressed any of this in the window.** The Bucket screen, the
  engine selector, the Services panel with its two indicators, and the queue
  rows are covered by 668 vitest tests and have not been looked at.
- **The other 145 recordings are uncoted and unindexed**, on purpose.
