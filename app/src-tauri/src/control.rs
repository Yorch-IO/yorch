//! Typed client for the control API.
//!
//! Every call the UI makes goes through Rust rather than straight from the
//! webview. That costs a wrapper per endpoint and buys three things: the
//! webview's CSP stays `default-src 'self'` with no localhost exception, the
//! API port can move between launches without the frontend knowing, and there
//! is one place where a connection failure becomes a typed error the UI can
//! branch on.

use serde::{Deserialize, Serialize};
use std::time::Duration;

use crate::error::{AppError, Result};

/// Health is polled while the user watches a spinner, so it must fail fast
/// enough to keep the screen responsive.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(6);

/// A ping runs a real workflow round trip through Temporal and two service
/// probes, so it gets a longer budget than a plain health read.
const PING_TIMEOUT: Duration = Duration::from_secs(45);

/// Starting an ingest returns as soon as the workflow exists; it does not wait
/// for the free stages, which is what the gate poll is for.
const START_TIMEOUT: Duration = Duration::from_secs(30);

/// Asking now starts the work and returns an id; the answer is collected by
/// polling. So this budget covers handing the question over, not answering it.
///
/// It used to be 180s and cover the whole thing, which lost a real answer: a
/// question against the church-history library ran past it, reqwest reports an
/// expired timeout with the same "error sending request for url" text it uses
/// for a refused connection, and the app blamed an API that had logged
/// `POST /ask 200 OK`. The answer was computed, was paid for, and was discarded.
const ASK_TIMEOUT: Duration = Duration::from_secs(15);

/// Collecting is a dictionary lookup in the API's own memory.
const ASK_POLL_TIMEOUT: Duration = Duration::from_secs(10);

/// Browsing the graph is one bounded, indexed traversal with a hard `LIMIT`, so
/// anything slower than this is a stack that is not answering rather than a
/// query that is still working.
const EXPLORE_TIMEOUT: Duration = Duration::from_secs(15);

/// Removal touches three stores in sequence: a Qdrant delete-by-filter, a graph
/// transaction and a catalog transaction. All are local and fast, but a book
/// with thousands of chunks makes each of them non-trivial, and the operation
/// must not be reported as unreachable while it is still working.
const REMOVE_TIMEOUT: Duration = Duration::from_secs(120);

/// Cataloguing a channel is one Data API round trip per fifty videos plus one
/// per fifty *new* ones, and there is no cap on the channel any more: a
/// 10,000-video channel is four hundred calls on its first sync. Ten minutes
/// covers that at a second a call. Generous because the failure it guards
/// against is Google not answering rather than a channel being long — and
/// because the worker saves every page as it goes, so a timeout here loses
/// only the request, never the pages; the next sync walks on from what is on
/// disk. A second sync of an unchanged channel is one call.
const CHANNEL_SYNC_TIMEOUT: Duration = Duration::from_secs(600);

/// Reading a catalogue off the volume and pricing it. No network at all.
const CHANNEL_TIMEOUT: Duration = Duration::from_secs(20);
/// Cataloguing a customer's bucket is awaited by the plane — seconds for a
/// hundred objects, minutes for a hundred thousand — and each object costs two
/// ranged reads. An hour, because a large bucket on a slow day is a slow sync
/// and not a failed one; the worker heartbeats throughout.
const BUCKET_SYNC_TIMEOUT: Duration = Duration::from_secs(3600);
/// Starting one run per ticked object, sequentially on the server. A hundred
/// and forty-seven starts is a few seconds; the bound is generous.
const BUCKET_PROBE_TIMEOUT: Duration = Duration::from_secs(600);

/// Listing the queue is one indexed, keyset-paged read of the catalog.
const RUNS_TIMEOUT: Duration = Duration::from_secs(10);

/// The audit ledger is four catalog reads for one run — events, charges,
/// artifacts, warnings — and touches Temporal not at all, which is the whole
/// point of it: it answers for a run whose history has aged out.
const AUDIT_TIMEOUT: Duration = Duration::from_secs(15);

/// Three stores and two artifact reads, so it is given more room than the
/// catalog-only audit beside it — but not much more, because it is measured:
/// **0.52 s** on the biggest version here (631 chunks, 3 055 claims, a 4.2 MB
/// `semantics.json`), of which 0.29 s is the semantics diff. 30 s is the
/// allowance for a cold page cache and a contended Memgraph, not for a shape
/// anybody has seen.
const STATISTICS_TIMEOUT: Duration = Duration::from_secs(30);

/// The raw history is fetched from Temporal, page by page, and a run with many
/// retries has a long one. Longer than the ledger because this is the call that
/// can actually be slow, and it is made once, when a person expands the panel.
const EVENTS_TIMEOUT: Duration = Duration::from_secs(20);

/// How long a conversation's stream will wait between pieces of an answer.
///
/// **A different kind of number from every other constant here.** The rest are
/// total budgets: the whole request must finish inside them. A turn's stream
/// cannot have one — its body arrives over the life of an answer, and a total
/// budget generous enough for a `thorough` turn would be no budget at all for a
/// dead connection. So this bounds one *read*, and the stream lives as long as
/// the server keeps saying something.
///
/// Generous even so, because the gap that matters is not between tokens but
/// between the request and the first of them: planning, embedding, the
/// topicality probe and retrieval all happen before the model writes a word,
/// and on a large library that is tens of seconds during which the relay has
/// nothing to publish. The server's own poll loop is what keeps this honest —
/// it emits a terminal event rather than going quiet.
const CHAT_STREAM_IDLE: Duration = Duration::from_secs(120);

/// Uploading a source file. Generous because this is the one request whose
/// duration is set by the user's upstream bandwidth rather than by the server:
/// the cap is 200 MB, and a slow home connection can spend minutes on a book
/// that the whole rest of the pipeline then handles in seconds.
const UPLOAD_TIMEOUT: Duration = Duration::from_secs(900);

/// Uploading a video's audio, which is the same shape as above with a bigger
/// worst case. `MAX_AUDIO_BYTES` is a gigabyte and the audio of a four-hour
/// talk is a couple of hundred megabytes, so an hour is the budget rather than
/// the fifteen minutes a book gets. It bounds a stalled connection; it does not
/// bound the download, which happened before this call and reported its own
/// progress.
const AUDIO_UPLOAD_TIMEOUT: Duration = Duration::from_secs(3600);

/// Downloading a book. Bandwidth-bound like the uploads above rather than
/// server-bound: the file is already on disk by the time this is called, and
/// what sets the duration is the link between here and the plane. A 600-chunk
/// book is a few hundred kilobytes, so this bounds a stalled connection rather
/// than a slow one.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceHealth {
    pub ok: bool,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Health {
    pub ok: bool,
    pub services: std::collections::BTreeMap<String, ServiceHealth>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProbeResult {
    pub service: String,
    pub ok: bool,
    pub detail: String,
}

/// Deserialized from the Python API, which emits snake_case, and re-serialized
/// to the webview, which expects camelCase. Renaming only the serialize side
/// keeps both ends idiomatic without a translation struct.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct PingResult {
    pub workflow_id: String,
    pub probes: Vec<ProbeResult>,
}

// ---------------------------------------------------------------------------
// Ingest
// ---------------------------------------------------------------------------

/// Mirrors `brainworker.pipeline.IngestRequest`.
///
/// Renamed on the **deserialize** side, which is the mirror of the convention
/// the rest of this file follows and is correct for the same reason: this type
/// travels webview → Rust → Python, the opposite direction from a response. It
/// has to accept the webview's camelCase and emit Python's snake_case.
///
/// It was renamed the other way round, and the consequence was total rather than
/// subtle — `serde` rejected every request the Import screen sent with "missing
/// field `library_id`", so starting an ingest from the window had never worked.
/// Nothing caught it because nothing had opened the window.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct IngestRequest {
    pub library_id: String,
    pub source_path: String,
    pub source_key: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub author: Option<String>,
    #[serde(default)]
    pub folder_id: Option<String>,
    #[serde(default)]
    pub auto_approve: bool,
    /// What to call the library, when this import is the one that creates it.
    ///
    /// A library id is a value the client picks — `lib_teologia` — and is not a
    /// name. Empty means "leave whatever it is called alone", so a second
    /// import does not rename a library after its own id, which is what every
    /// import used to do.
    #[serde(default)]
    pub library_name: String,
}

/// One video to index.
///
/// Renamed on the **deserialize** side for the same reason `IngestRequest` is:
/// this travels webview → Rust → Python, so it accepts the webview's camelCase
/// and emits Python's snake_case.
///
/// It carries a URL where `IngestRequest` carries a path, and that is the whole
/// difference between the two pipelines at this end: there is nothing to stage,
/// so no `stage_source` call precedes this and no `sourcePath` is ever computed.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct VideoRequest {
    pub library_id: String,
    pub url: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub author: Option<String>,
    #[serde(default)]
    pub auto_approve: bool,
    #[serde(default)]
    pub reindex: bool,
    #[serde(default)]
    pub library_name: String,
    /// Caption languages to prefer, best first. Empty lets the worker choose.
    #[serde(default)]
    pub languages: Vec<String>,
    /// What this machine already learned about the video, or `None` to let the
    /// pipeline ask YouTube itself.
    ///
    /// Never sent by the webview — it is `#[serde(default)]` on the way in and
    /// filled by `video_start`, because the whole point is that the call is
    /// made here. It serializes under Python's own spelling; see
    /// [`crate::ytdlp::VideoInfo`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolved: Option<crate::ytdlp::VideoInfo>,
    /// Audio this machine already downloaded and uploaded, as the path the
    /// worker sees. Empty unless the video has no captions at all.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub audio_path: String,
}

/// Where audio the app uploaded landed, as the *worker* sees it.
///
/// Deliberately not `StagedSource`: that one carries a `source_key` because a
/// document's original filename becomes its title, and audio has no title of
/// its own — the video already supplied one.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct StagedAudio {
    pub audio_path: String,
    pub byte_size: u64,
}

/// Where a file the app uploaded landed, as the *worker* sees it.
///
/// `source_path` is a container path (`/workspace/tenants/<id>/inbox/…`) and is
/// meaningless on the user's own machine. That is the point: it is what
/// `IngestRequest.source_path` must carry, and in local mode the same field
/// carries the host path the worker shares through the volume. The two planes
/// disagree about what a path *is*, and this type is where that is resolved
/// once rather than at every call site.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct StagedSource {
    pub source_path: String,
    pub source_key: String,
    pub byte_size: u64,
}

/// Which paid stages the user approved. Individually switchable because they
/// cost wildly different amounts.
///
/// Renamed on the deserialize side — see `IngestRequest` for why, and for what
/// the other direction cost.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct StageOptions {
    pub correct: bool,
    pub embed: bool,
    pub extract_semantics: bool,
    pub generate_evalset: bool,
    /// Learn a family profile when none exists for this fingerprint. Paid, so
    /// it is a switch like every other stage that spends. Reuse costs nothing
    /// and has no switch.
    pub learn_profile: bool,
    /// Decline an inherited profile. The fingerprint is structural, so two
    /// unrelated families sharing a layout collide exactly — and the metrics
    /// cannot see it, because the eval questions come from the very chunks the
    /// wrong rules produced. This is the person's way out.
    pub ignore_profile: bool,
    pub review_correction: bool,
    /// Try to improve retrieval, and measure whether it worked.
    ///
    /// Off, and bounded to a single chunking candidate. The free half —
    /// `min_score`, `per_section`, dense-only — changes nothing in the index;
    /// the paid half re-cuts the document and embeds every chunk again, which is
    /// the largest single line a gate can show. Turning it on also raises the
    /// eval sample, because at 40 questions the bootstrap margin cannot resolve
    /// the effect the round is looking for.
    #[serde(default)]
    pub tune: bool,
    /// Package what was indexed as an EPUB a person can read on a device.
    ///
    /// `#[serde(default)]` for the reason `tune` has it: an older webview that
    /// does not send the key must not fail the whole call. Nearly free — the
    /// packaging costs nothing, and the one call it can make runs once per
    /// document ever and never at all for a video.
    #[serde(default)]
    pub build_epub: bool,
}

impl Default for StageOptions {
    fn default() -> Self {
        Self {
            correct: true,
            embed: true,
            extract_semantics: true,
            generate_evalset: false,
            learn_profile: true,
            ignore_profile: false,
            review_correction: false,
            tune: false,
            build_epub: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct StartedRun {
    pub workflow_id: String,
    pub state: String,
}

/// Where a run *is*, which is not the same question as which stage it last
/// recorded.
///
/// A `stage` query against a failed workflow hands back the last value it
/// reached, so a run whose activity retries were exhausted reported
/// `"stage": "learning"` indefinitely while Temporal already knew it had
/// failed. This is the field that tells them apart.
///
/// Only the two fields the app acts on are modelled; the response also carries
/// artifacts, cost and semantics counts, and no screen reads them. Both are
/// `#[serde(default)]` so a control plane that has not been upgraded loses the
/// distinction rather than the whole call — the same treatment the cost range's
/// new fields got.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunState {
    pub workflow_id: String,
    #[serde(default)]
    pub stage: Option<String>,
    /// `running`, or whatever terminal state it ended in. `None` means nobody
    /// could say — an old plane, or a run whose history has aged out — and a
    /// caller must read that as "keep waiting", never as "failed".
    #[serde(default)]
    pub state: Option<String>,
    /// How far the running activity has got, when it reports.
    ///
    /// `None` for every honest absence, and they are not told apart on purpose:
    /// nothing pending, an activity that does not heartbeat, a run older than
    /// the code that emits it. All of them mean "no progress to show", which is
    /// one thing for a screen to render rather than four.
    #[serde(default)]
    pub progress: Option<RunProgress>,
    /// What the index this run wrote can actually be asked.
    ///
    /// `None` means nobody measured — which is true of every version indexed
    /// before the stage existed, and of every run whose gate declined it. It is
    /// deliberately not zero: recall of 0.00 is a claim about the index, and it
    /// would send somebody to fix one that is fine.
    #[serde(default)]
    pub scores: Option<RunScores>,
}

/// One run as the queue lists it.
///
/// Everything here comes from the catalog, which is what makes the queue
/// survive a stopped Temporal and a closed window: what a run is *doing* belongs
/// to `RunState`, and what it *was* belongs here.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunListItem {
    pub id: String,
    pub workflow_id: String,
    pub kind: String,
    pub state: String,
    #[serde(default)]
    pub stage: Option<String>,
    pub started_at: String,
    #[serde(default)]
    pub finished_at: Option<String>,
    #[serde(default)]
    pub error_kind: Option<String>,
    #[serde(default)]
    pub error_detail: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub library_id: Option<String>,
    /// What the run knew about itself before it had a document: the URL for a
    /// video, the picked file's basename for an import. `default` is
    /// load-bearing rather than habit — a control plane older than the column
    /// sends no key, and the queue must still decode.
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub document_id: Option<String>,
    #[serde(default)]
    pub version_id: Option<String>,
    /// `None`, never zero, when no stage has recorded a price. A run still in
    /// its free stages legitimately has none, and zero would claim it spent.
    #[serde(default)]
    pub usd_so_far: Option<f64>,
}

/// A page of the queue, and where the next one starts.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunListPage {
    pub runs: Vec<RunListItem>,
    /// The cursor for the following page, or `None` at the end. Opaque: it is
    /// `{started_at}|{id}` because the sort is on both, but no caller parses it.
    #[serde(default)]
    pub next_before: Option<String>,
}

/// What one charge cost, as the ledger reports it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditCostEntry {
    pub stage: String,
    pub provider: String,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// `None` is "no price known for this model", rendered "sin precio". Never
    /// zero — that would be a claim that it was free.
    #[serde(default)]
    pub usd: Option<f64>,
}

/// What a stage cost, with the unpriced part reported rather than folded in.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditCost {
    pub input_tokens: u64,
    pub output_tokens: u64,
    #[serde(default)]
    pub usd: Option<f64>,
    #[serde(default)]
    pub unpriced_entries: u64,
    #[serde(default)]
    pub entries: Vec<AuditCostEntry>,
}

/// One artifact, as `run_artifact` recorded it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditArtifact {
    pub name: String,
    pub rel_path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

/// One row of the ledger: a stage, how long it took, and what it produced.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditStage {
    /// `None` on the trailing row that collects whatever no stage claimed —
    /// the charges a question makes, which belong to no pipeline stage.
    #[serde(default)]
    pub seq: Option<i64>,
    #[serde(default)]
    pub stage: Option<String>,
    #[serde(default)]
    pub at: Option<String>,
    /// `None` means one of two things `outcome` tells apart: the run is still in
    /// this stage, or this row *is* the outcome and is an instant.
    #[serde(default)]
    pub ended_at: Option<String>,
    #[serde(default)]
    pub seconds: Option<f64>,
    #[serde(default)]
    pub outcome: Option<String>,
    #[serde(default)]
    pub detail: Option<String>,
    /// `None` for a stage that does not spend — deliberately not a zeroed
    /// block, because "does not spend" and "the charge was not recorded" are
    /// different claims and a zero renders as the first.
    #[serde(default)]
    pub cost: Option<AuditCost>,
    #[serde(default)]
    pub artifacts: Vec<AuditArtifact>,
}

/// The run the ledger describes, read from the catalog rather than Temporal.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditRun {
    pub id: String,
    pub workflow_id: String,
    pub kind: String,
    pub state: String,
    #[serde(default)]
    pub stage: Option<String>,
    pub started_at: String,
    #[serde(default)]
    pub finished_at: Option<String>,
    #[serde(default)]
    pub error_kind: Option<String>,
    #[serde(default)]
    pub error_detail: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub library_id: Option<String>,
    /// What the run knew about itself before it had a document. See
    /// `RunListItem::label`.
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub document_id: Option<String>,
    #[serde(default)]
    pub version_id: Option<String>,
}

/// A profile warning raised against the version this run produced.
///
/// The row carries no `kind`, so a reader cannot tell a plain collision from the
/// heading disagreement that withholds activation — that distinction exists only
/// on the Temporal payload today, and the pane must not imply otherwise.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AuditWarning {
    #[serde(default)]
    pub profile_id: Option<String>,
    #[serde(default)]
    pub collides_with: Option<String>,
    #[serde(default)]
    pub similarity: Option<f64>,
    #[serde(default)]
    pub detail: Option<String>,
}

/// Everything a run did, from the catalog alone.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunAudit {
    pub run: AuditRun,
    pub stages: Vec<AuditStage>,
    pub totals: AuditCost,
    #[serde(default)]
    pub warnings: Vec<AuditWarning>,
}

/// One line of the raw workflow history.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunEvent {
    pub id: i64,
    pub at: String,
    #[serde(rename(deserialize = "type"))]
    pub kind: String,
    #[serde(default)]
    pub activity: Option<String>,
    /// Present from the second attempt onward, which is the whole reason this
    /// panel exists: a retry is invisible everywhere else.
    #[serde(default)]
    pub attempt: Option<u32>,
    #[serde(default)]
    pub detail: Option<String>,
}

/// The raw history, or an honest statement that there is none to be had.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunEventPage {
    /// `false` means Temporal has forgotten this run, which is ordinary past the
    /// retention period. It is deliberately not the same as an empty `events`:
    /// "the history aged out" and "this run did nothing" must not render alike.
    pub available: bool,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub events: Vec<RunEvent>,
}

/// Measured retrieval quality for one run's index.
///
/// `recall_at_5_dense_only` and `noise_floor` are not extras. The eval set's
/// questions are written *from* the chunks they must find, so they leak
/// vocabulary to the lexical leg and the hybrid figure alone flatters the index;
/// the gap between the two is that leakage. And recall says how often the right
/// chunk came back while the floor says what a *wrong* one scores — without it a
/// reader cannot tell an index that discriminates from one that returns
/// everything at a similar distance.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunScores {
    pub recall_at_1: f64,
    pub recall_at_5: f64,
    pub mrr_at_10: f64,
    pub recall_at_5_dense_only: f64,
    pub noise_floor: f64,
    pub chunks: u32,
    pub eval_questions: u32,
    /// Bootstrap margin on the objective. A difference smaller than this is
    /// noise, and reporting it as real is the failure it guards against.
    #[serde(default)]
    pub margin: f64,
    #[serde(default)]
    pub leakage: String,
    /// How many questions did not find their own chunk.
    #[serde(default)]
    pub misses: u32,
}

/// Chunks done out of chunks total, off the activity's heartbeat.
///
/// Only semantic extraction reports today, and it is the one worth reporting:
/// one generation call per chunk, so this is simultaneously a count of calls and
/// of spend — which is what a person stopping a run is actually weighing.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunProgress {
    pub activity: String,
    pub done: u32,
    pub total: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChunkKindCount {
    pub kind: String,
    pub count: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Preview {
    pub chunk_count: u32,
    pub kinds: Vec<ChunkKindCount>,
    pub characters: u64,
    /// False whenever correction is on: correction runs before chunking because
    /// it changes the text's length, so these are not the indexed chunks. The
    /// UI must not drop this.
    pub chunks_are_final: bool,
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct StageEstimate {
    pub stage: String,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// None means the model has no recorded price. Rendered "sin precio", never
    /// as zero — that figure is what a user approves spend against.
    pub usd: Option<f64>,
    /// The upper end, for a stage whose figure is a mean over a corpus that
    /// varies. Equal to the fields above where the point estimate is already a
    /// ceiling, which is what lets the table render one figure or two without a
    /// flag telling it which.
    #[serde(default)]
    pub output_tokens_high: u64,
    #[serde(default)]
    pub usd_high: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Estimate {
    pub stages: Vec<StageEstimate>,
    pub total_usd: Option<f64>,
    /// The upper end of the bill. Defaulted rather than required so an older API
    /// costs the gate a range, not the whole approval screen.
    #[serde(default)]
    pub total_usd_high: Option<f64>,
    pub price_source: String,
    #[serde(default)]
    pub unpriced_stages: Vec<String>,
}

// -- buckets ----------------------------------------------------------------
//
// Audio out of a customer's own S3 bucket. Paid plane only: the local plane
// serves none of these paths, and a call in local mode answers with the 404
// the proxy already turns into `control_status`.

/// Where a customer's audio lives and how the plane may read it.
///
/// Deliberately **not** renamed, like `Question`: it travels *in* from the
/// webview on a register and *out* inside `StoredBucket` on every read, and
/// one struct cannot rename in both directions. The TypeScript side spells it
/// snake_case, as it does `Question`, and says so at its declaration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BucketSource {
    pub bucket: String,
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub role_arn: String,
    #[serde(default)]
    pub region: String,
    #[serde(default = "default_archive_prefix")]
    pub archive_prefix: String,
    #[serde(default)]
    pub manifest_key: String,
    #[serde(default)]
    pub manifest_map: std::collections::BTreeMap<String, String>,
    #[serde(default = "default_language")]
    pub language: String,
}

fn default_archive_prefix() -> String {
    "transcripciones/".to_string()
}

fn default_language() -> String {
    "es-US".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct StoredBucket {
    pub bucket_id: String,
    pub source: BucketSource,
    pub library_id: String,
    pub library_name: String,
    #[serde(default)]
    pub synced_at: String,
    #[serde(default)]
    pub object_count: u32,
    #[serde(default)]
    pub complete: bool,
    #[serde(default)]
    pub estimated: u32,
    #[serde(default)]
    pub manifest_rows: u32,
    #[serde(default)]
    pub unmatched_rows: u32,
    #[serde(default)]
    pub unmatched_objects: u32,
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// One object as the catalogue holds it, joined with what the catalog says.
///
/// `state` is derived on the plane from Postgres — `indexed`, `pending` or
/// `unindexed` — never from the catalogue file, which deliberately does not
/// record it. `duration_estimated` is the flag the quote is least sure of.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketObjectRow {
    pub key: String,
    #[serde(default)]
    pub etag: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub last_modified: String,
    #[serde(default)]
    pub container: String,
    #[serde(default)]
    pub duration_s: u32,
    #[serde(default)]
    pub duration_estimated: bool,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub author: String,
    #[serde(default)]
    pub recorded_at: String,
    #[serde(default)]
    pub published_at: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub url: String,
    #[serde(default = "yes")]
    pub available: bool,
    #[serde(default)]
    pub warnings: Vec<String>,
    pub document_id: Option<String>,
    pub active_version_id: Option<String>,
    pub run_id: Option<String>,
    pub run_state: Option<String>,
    pub state: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketTotals {
    pub objects: u32,
    pub seconds: u64,
    pub indexed: u32,
    pub pending: u32,
    pub unindexed: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketDetail {
    pub bucket: StoredBucket,
    pub objects: Vec<BucketObjectRow>,
    pub totals: BucketTotals,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketList {
    pub buckets: Vec<StoredBucket>,
}

/// What one sync found.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketSynced {
    pub bucket_id: String,
    pub library_id: String,
    pub objects: u32,
    pub added: u32,
    pub changed: u32,
    pub absent: u32,
    pub estimated: u32,
    pub manifest_rows: u32,
    pub unmatched_rows: u32,
    pub unmatched_objects: u32,
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketRegistered {
    pub bucket: Option<StoredBucket>,
    pub synced: BucketSynced,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketProbeStarted {
    pub key: String,
    pub workflow_id: String,
    /// The engine this run actually started on, which is not always the one
    /// the batch asked for: an object in a container the app's decoder cannot
    /// read is started on Amazon whatever was ticked. Said per key here rather
    /// than discovered later as a run parked for a transcript no machine on
    /// this side can make.
    #[serde(default = "default_transcriber")]
    pub transcriber: String,
}

fn default_transcriber() -> String {
    "transcribe".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketProbeFailed {
    pub key: String,
    pub kind: String,
    pub message: String,
}

/// One run per key that could start, and why each of the others could not.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketProbeResult {
    pub started: Vec<BucketProbeStarted>,
    pub failed: Vec<BucketProbeFailed>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BucketForgotten {
    pub forgotten: bool,
}

/// A presigned link to a recording, minted on click. It dies with the
/// assumed-role session that signed it, which is why it is never stored and
/// why `source_url` — the public feed's own link — rides beside it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct MediaLink {
    pub url: String,
    #[serde(default)]
    pub expires_at: String,
    #[serde(default)]
    pub start_s: f64,
    #[serde(default)]
    pub source_url: String,
}

fn bucket_probe_body(
    keys: &[String],
    options: &StageOptions,
    reindex: bool,
    transcriber: &str,
) -> serde_json::Value {
    serde_json::json!({
        "keys": keys,
        "options": options,
        "reindex": reindex,
        "transcriber": transcriber,
    })
}

/// What the desktop app did with a run it transcribed itself.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TranscriptUploaded {
    pub workflow_id: String,
    pub state: String,
}

/// The answer to giving up on a local transcript. `state` is
/// `awaiting_approval` rather than `running`, because the run re-quotes on
/// Amazon and parks again — a client that expected it to proceed would wait on
/// a gate it did not know about.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TranscriberSwitched {
    pub workflow_id: String,
    pub state: String,
    #[serde(default = "default_transcriber")]
    pub transcriber: String,
}

// -- channels ---------------------------------------------------------------
//
// Responses, so the rename is on the **serialize** side: these decode from the
// Python API's snake_case and re-encode as the camelCase the webview reads.
// Getting that direction backwards is not subtle — it made every request the
// Import screen sent fail with "missing field `library_id`", and nothing caught
// it because nobody had opened the window.

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelRef {
    pub channel_id: String,
    pub title: String,
    pub handle: String,
    pub description: String,
    pub uploads_playlist_id: String,
    pub url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelSummary {
    pub channel: ChannelRef,
    /// A channel *is* a library: retrieval narrows by equality on `library_id`,
    /// so "ask only this channel" is "ask this library".
    pub library_id: String,
    pub synced_at: String,
    pub video_count: u32,
    /// Quota units the last sync spent. The Data API's daily allowance is the
    /// one resource here that runs out, and saying what a sync cost beats
    /// discovering it at the end of the day.
    #[serde(default)]
    pub units_spent: u32,
    /// Whether a sync has ever walked the uploads playlist to its end. "The
    /// most recent 500" and "all 2,000" are different catalogues, and the
    /// screen says which one it is showing.
    #[serde(default)]
    pub complete: bool,
    #[serde(default)]
    pub fetched: u32,
    /// Only on a sync's own answer: what that sync did, so the bar can say
    /// "12 new" rather than only "done".
    #[serde(default)]
    pub added: u32,
    #[serde(default)]
    pub stopped_early: bool,
    #[serde(default)]
    pub unavailable: u32,
    /// Filled by the listing only; a sync has no figures from the catalog yet.
    #[serde(default)]
    pub documents: u32,
    #[serde(default)]
    pub indexed_versions: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelList {
    pub channels: Vec<ChannelSummary>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelVideoRow {
    pub video_id: String,
    pub title: String,
    pub description: String,
    #[serde(default)]
    pub description_truncated: bool,
    pub published_at: String,
    pub duration_s: u32,
    pub live_state: String,
    pub thumbnail: String,
    pub url: String,
    /// `None` means this channel's library holds no document for the video.
    pub document_id: Option<String>,
    /// `None` on a document that exists is a real state and not a gap: a run
    /// that was cancelled, or an activation withheld over a structural
    /// mismatch. The screen has to tell the two apart.
    pub active_version_id: Option<String>,
    /// `false` once a complete re-sync did not meet this id on the playlist —
    /// deleted, or made private. Kept in the list, because it may be indexed.
    /// Defaults to `true` so a catalogue from before the flag offers everything.
    #[serde(default = "yes")]
    pub available: bool,
}

fn yes() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelDetail {
    pub channel: ChannelRef,
    pub library_id: String,
    pub synced_at: String,
    pub video_count: u32,
    #[serde(default)]
    pub units_spent: u32,
    #[serde(default)]
    pub complete: bool,
    pub videos: Vec<ChannelVideoRow>,
}

/// The body a sync takes. `limit` is **omitted** when there is none: the
/// Python dataclass reads an absent key as "the whole playlist", and sending
/// `null` says the same thing less clearly. `full` always travels, because a
/// full re-sync is the one thing that marks a video unavailable and the wire
/// must never leave that to a default.
fn channel_sync_body(url: &str, limit: Option<u32>, full: bool) -> serde_json::Value {
    let mut body = serde_json::json!({ "url": url, "full": full });
    if let Some(n) = limit {
        body["limit"] = serde_json::json!(n);
    }
    body
}

/// The body both channel query routes take.
///
/// One function for the quote and the run, because the quote is the figure the
/// person was shown and the run is what they approved: a body built twice could
/// price one set of videos and judge another. `video_ids` is the screen's
/// title-keyword filter and is **omitted** when there is none — the Python
/// dataclass reads an absent key as `None`, the whole catalogue, and sending
/// `null` would say the same thing less clearly.
fn channel_query_body(
    topic: &str,
    limit: u32,
    deep_limit: u32,
    video_ids: Option<&[String]>,
) -> serde_json::Value {
    let mut body = serde_json::json!({
        "topic": topic, "limit": limit, "deep_limit": deep_limit
    });
    if let Some(ids) = video_ids {
        body["video_ids"] = serde_json::json!(ids);
    }
    body
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct DiscoveryQuote {
    pub channel_id: String,
    pub library_id: String,
    pub topic: String,
    pub evaluated: u32,
    pub read: u32,
    /// Videos in the read set with no duration, which therefore cannot be
    /// quoted. Reported rather than priced at nothing: a zero in a bill is a
    /// claim about the work, not an absence of one.
    #[serde(default)]
    pub unmeasured: u32,
    pub estimate: Estimate,
}

/// What a discovery or a topic run concluded, read back from its artifacts.
///
/// The three payloads are untyped on purpose. They are the run's own records —
/// a quote, a table of verdicts, a table of readings — and every field of them
/// is rendered rather than acted on, so modelling them here would be a fourth
/// copy of a shape that already exists in Python, in the artifact and in
/// TypeScript. `None` is a state: a run still preselecting has no topics.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChannelReading {
    pub channel_id: String,
    pub workflow_id: String,
    pub state: Option<String>,
    pub stage: Option<String>,
    pub usd_so_far: Option<f64>,
    pub estimate: Option<serde_json::Value>,
    pub preselection: Option<serde_json::Value>,
    pub topics: Option<serde_json::Value>,
}

/// One descriptive statement about what was preached, and the citations that
/// survived checking. A finding that named none was **moved to `limitaciones`**
/// before this crossed the wire, never deleted: an answer that quietly lost a
/// claim looks complete and is shorter, and the reader would never know.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Finding {
    pub afirmacion: String,
    pub chunk_ids: Vec<String>,
}

/// One reading, labelled as a reading. Never merged with a finding: the thing a
/// reader must treat sceptically may not share a paragraph with the thing they
/// may rely on.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Inference {
    pub inferencia: String,
    #[serde(default)]
    pub alcance: String,
    #[serde(default)]
    pub limites: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Comparison {
    #[serde(default)]
    pub convergencias: Vec<String>,
    #[serde(default)]
    pub diferencias: Vec<String>,
    #[serde(default)]
    pub matices: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Synthesis {
    /// `answered`, `insufficient_evidence` or `off_corpus`. The last two are
    /// different states because their remedies differ: one means the corpus was
    /// searched and came up short, the other that the topic belongs elsewhere.
    pub state: String,
    pub topic: String,
    pub model: String,
    #[serde(default)]
    pub prompt_version: String,
    #[serde(default)]
    pub hallazgos: Vec<Finding>,
    #[serde(default)]
    pub comparacion: Comparison,
    #[serde(default)]
    pub interpretacion_teologica: Vec<Inference>,
    #[serde(default)]
    pub citas: Vec<Citation>,
    #[serde(default)]
    pub limitaciones: Vec<String>,
    #[serde(default)]
    pub evidence: Vec<EvidenceItem>,
    /// Findings moved to `limitaciones` for naming no citation that survived.
    /// A synthesis where half of them were demoted is one to distrust, and a
    /// count is the only thing that says so.
    #[serde(default)]
    pub demoted: u32,
    #[serde(default)]
    pub invented: u32,
    /// Why, when `state` is not `answered`. The only thing that says *which*
    /// refusal this was — four facts with four remedies used to render as one
    /// line reading "not enough evidence".
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub spend: Vec<Spend>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct SynthesisResult {
    pub question_id: String,
    /// `running` until it is not. Two calls, for the recorded reason: a real
    /// question outran a 180 s client timeout, was computed, was billed, and
    /// was discarded under a message blaming an unreachable API.
    pub state: String,
    pub synthesis: Option<Synthesis>,
    pub error: Option<std::collections::BTreeMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ProfileWarning {
    pub profile_id: String,
    pub collides_with: String,
    pub similarity: f64,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GateReport {
    pub run_id: String,
    pub document_id: String,
    pub version_id: String,
    pub preview: Preview,
    pub estimate: Estimate,
    #[serde(default)]
    pub profile_warnings: Vec<ProfileWarning>,
    /// Which rules will chunk this document, and where they came from.
    ///
    /// A field missing from this struct does not fail — serde ignores unknown
    /// keys — it silently arrives as `undefined` in TypeScript, so the screen
    /// would have reported "no profile for this family" for every document
    /// including ones that had one.
    #[serde(default)]
    pub profile: Option<ProfileDecision>,
}

/// One caption track a video offers.
///
/// `kind` is `manual` or `auto`, and the distinction is not cosmetic: an
/// automatic track is a machine transcript with no punctuation, which is why it
/// is the only one correction is suggested for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct CaptionTrack {
    pub language: String,
    pub kind: String,
    pub ext: String,
    #[serde(default)]
    pub name: String,
}

/// Everything free that could be learned about a video before the gate.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VideoProbe {
    pub video_id: String,
    pub canonical_url: String,
    pub source_key: String,
    pub title: String,
    #[serde(default)]
    pub channel: String,
    pub duration_s: i64,
    #[serde(default)]
    pub upload_date: String,
    #[serde(default)]
    pub tracks: Vec<CaptionTrack>,
    /// The track that will be read, or `null` when Amazon Transcribe must run —
    /// which is the difference between a free transcript and a paid one, and the
    /// only thing the gate needs to explain the transcription line.
    #[serde(default)]
    pub chosen: Option<CaptionTrack>,
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// The transcript, once it exists.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Transcribed {
    /// `captions:es:manual` or `transcribe`, so a reader can tell a human
    /// transcript from a machine one without a second call.
    pub source: String,
    pub paragraphs: i64,
    pub characters: i64,
    #[serde(default)]
    pub covered_s: f64,
    #[serde(default)]
    pub warnings: Vec<String>,
}

/// A video run's gate, which is a different shape from a document's.
///
/// `preview` is optional here and it is the whole reason this is its own type:
/// a video with no captions has no text to preview until the money has been
/// spent, and `null` says so rather than quoting a chunk count nobody measured.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VideoGateReport {
    pub run_id: String,
    pub document_id: String,
    pub version_id: String,
    pub probe: VideoProbe,
    pub estimate: Estimate,
    #[serde(default)]
    pub preview: Option<Preview>,
    #[serde(default)]
    pub transcript: Option<Transcribed>,
    #[serde(default)]
    pub warnings: Vec<String>,
    /// Which switches the gate should open with, given where this transcript
    /// came from. A suggestion, not a rule — the approval carries whatever the
    /// person actually ticked.
    #[serde(default)]
    pub recommended: Option<RecommendedStages>,
}

/// The two switches a video's gate actually offers.
///
/// A response-side twin of `StageOptions` rather than the type itself, and for
/// two reasons. `StageOptions` renames on the *deserialize* side, because it
/// travels webview → Python; this travels the other way and has to rename on
/// serialize, and one struct cannot do both. And a video run has no profile,
/// semantics, eval-set or tuning stage at all, so offering those switches would
/// quote work that cannot happen. Python sends the full nine fields and serde
/// drops the seven this does not name.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RecommendedStages {
    pub correct: bool,
    pub embed: bool,
}

/// Mirrors `brainworker.pipeline.ProfileRules` — the flattened rules, not the
/// whole profile. A `Profile` also carries an eval set, its scores and a tuning
/// history, none of which the gate shows or any activity applies.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ProfileRules {
    #[serde(default)]
    pub header_patterns: Vec<String>,
    pub heading_l1_max: i64,
    pub heading_l2_max: i64,
    pub heading_l1_pattern: Option<String>,
    pub heading_l2_pattern: Option<String>,
    pub question_pattern: Option<String>,
    pub footnote_pattern: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ProfileDecision {
    pub fingerprint: String,
    /// `reused` (free), `learned` (paid) or `default` (the measured built-ins).
    pub source: String,
    #[serde(default)]
    pub slug: String,
    #[serde(default)]
    pub learned_from: String,
    #[serde(default)]
    pub revisions: i64,
    pub rules: ProfileRules,
    #[serde(default)]
    pub warnings: Vec<ProfileWarning>,
    #[serde(default)]
    pub adopted: Vec<String>,
    #[serde(default)]
    pub spend: Option<Spend>,
}

/// The gate's answer. Travels webview → Python like the two above, and carries
/// `StageOptions`, so it needs the same rename direction.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct Approval {
    pub approved: bool,
    #[serde(default = "StageOptions::default")]
    pub options: StageOptions,
    #[serde(default)]
    pub reason: String,
}

// ---------------------------------------------------------------------------
// Questions
// ---------------------------------------------------------------------------

/// Deliberately **not** renamed: `Question` is the one request type the
/// TypeScript side spells snake_case, matching Python's field names directly.
/// It works, and `app/src/lib/api.ts` says so at its declaration — but it is the
/// exception, so the test below pins every request type's direction rather than
/// leaving the difference to be rediscovered.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Question {
    pub library_id: String,
    pub text: String,
    /// Absent means "the effort level decides", which is what the app always
    /// says. Omitted from the payload entirely rather than sent as `null`, so
    /// the two control planes see the same thing a `curl` that never mentioned
    /// it would send — and so neither has to decide what a null means.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub top_k: Option<u32>,
    #[serde(default)]
    pub filters: std::collections::BTreeMap<String, String>,
    #[serde(default = "default_floor")]
    pub confidence_floor: f64,
    /// How much evidence and reasoning the question may spend. The numbers
    /// behind each level live in the worker (`answering/effort.py`); nothing on
    /// this side knows or needs to know what they are.
    ///
    /// Defaulted rather than optional because a level is always in effect —
    /// there is no such thing as a question asked at no effort — and a webview
    /// older than this shell must keep asking at the level that behaves the way
    /// it always has.
    #[serde(default = "default_effort")]
    pub effort: String,
    /// The narrowings a recording corpus makes askable, all optional and all
    /// omitted from the payload when empty — the dataclass default is the one
    /// copy of "no filter", the same reasoning `effort` records for itself on
    /// the paid plane.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub recorded_from: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub recorded_to: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub scripture: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub source_name: String,
}

fn default_floor() -> f64 {
    0.6
}

/// One effort level's answer wording, as the settings screen needs it.
///
/// `body` is what would actually be used — the organisation's override where
/// there is one and the built-in default where there is not — and `custom` says
/// which of the two it is. Both are needed: the text goes in the box, the flag
/// decides whether "restore the default" would do anything.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AnswerStyle {
    pub effort: String,
    pub body: String,
    pub default_body: String,
    pub custom: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AnswerStyles {
    pub levels: Vec<AnswerStyle>,
    pub max_chars: usize,
}

/// The new wording for one level. Empty clears the override.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct AnswerStyleUpdate {
    #[serde(default)]
    pub body: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AnswerStyleSaved {
    pub effort: String,
    pub custom: bool,
}

/// Kept equal to `DEFAULT_EFFORT` in `worker/brainworker/answering/effort.py`.
/// Restated here only because a webview may omit the field; every other default
/// in this struct is there for the same reason.
fn default_effort() -> String {
    "standard".to_string()
}

// -- recasting a document into another genre --------------------------------
//
// Paid plane only. The local plane serves none of these paths, and a call in
// local mode answers with the 404 the proxy already turns into
// `control_status` — the same position the bucket paths are in, and recorded
// for the same reason in `doc/TRANSFORM.md`.

/// One document to recast, and into what.
///
/// Renamed on the deserialize side, like `StageOptions` and `Approval`: it
/// travels webview → plane, so the webview writes camelCase and this serialises
/// the snake_case both planes read. `tenantId` is deliberately absent — the
/// plane stamps it from the authenticated session, and a body carrying one is
/// refused outright by `forbidNonWhitelisted`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct TransformRequest {
    pub library_id: String,
    pub document_id: String,
    pub version_id: String,
    /// One of the eleven. Validated by the plane with its own `Literal`/`@IsIn`,
    /// so an unknown one comes back as a 422 with a list rather than as a
    /// hand-raised kind — the decision `Question.effort` already records.
    pub genre: String,
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub purposes: Vec<String>,
    #[serde(default)]
    pub auto_approve: bool,
    #[serde(default)]
    pub label: String,
}

/// Which of the two gates this run stops at, and whether it researches.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct TransformOptions {
    #[serde(default = "default_true")]
    pub review_plan: bool,
    #[serde(default = "default_true")]
    pub research: bool,
}

fn default_true() -> bool {
    true
}

/// The answer to either gate.
///
/// `options` is an `Option`, and `None` is not the same as a defaulted one: the
/// plane omits the key entirely when it is absent, and the workflow then keeps
/// the options the run was started with. Sending a defaulted object instead
/// would turn research back on for a run started with it off — which is the
/// recorded Angular gate defect, where every stage a person unticked before
/// pressing Import was silently turned back on.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct TransformApproval {
    pub approved: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub options: Option<TransformOptions>,
    #[serde(default)]
    pub reason: String,
}

/// The eleven genres, the two modes and the four research purposes.
///
/// Served rather than compiled in, so a genre added on the worker reaches the
/// picker without a client release.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TransformVocabulary {
    pub genres: Vec<String>,
    pub modes: Vec<String>,
    pub default_mode: String,
    pub purposes: Vec<String>,
}

/// A transformation's **first** gate: a quote from what is knowable for free.
///
/// `projection` is `true` and says so on the screen: the chapter count here is
/// arithmetic over the source's own chapters, not an outline. The second gate
/// carries the real one.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TransformGateReport {
    pub genre: String,
    pub mode: String,
    #[serde(default)]
    pub purposes: Vec<String>,
    #[serde(default)]
    pub source_title: String,
    pub source_chapters: usize,
    pub characters: usize,
    pub projected_chapters: usize,
    pub research_budget: usize,
    pub supported: usize,
    #[serde(default)]
    pub projection: bool,
    #[serde(default)]
    pub estimate: Option<Estimate>,
}

/// One chapter of the planned work, and the source material it is made from.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChapterPlan {
    pub ordinal: usize,
    pub title: String,
    #[serde(default)]
    pub intent: String,
    #[serde(default)]
    pub chars: usize,
}

/// A transformation's **second** gate: the outline, and the first quote anybody
/// should act on.
///
/// Its own type and its own route, never the first report reassigned. In
/// `IngestWorkflow` the one report is assigned before the first gate and never
/// cleared, so its second gate serves the first one's preview — a reader was
/// shown "Nothing has been paid for yet" over a run that had spent $0.58.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TransformPlanReport {
    pub genre: String,
    pub mode: String,
    #[serde(default)]
    pub chapters: Vec<ChapterPlan>,
    #[serde(default)]
    pub uncovered_fraction: f64,
    pub research_budget: usize,
    #[serde(default)]
    pub fallback: bool,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default)]
    pub spent_so_far: Option<f64>,
    #[serde(default)]
    pub estimate: Option<Estimate>,
}

// -- conversations ----------------------------------------------------------
//
// A conversation's transcript comes from the catalog rather than from Temporal,
// which is what lets it outlive the retention that bounds a session. These are
// response shapes, so they deserialize the snake_case the control planes emit
// and re-serialize camelCase for the webview.

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Conversation {
    pub id: String,
    pub library_id: String,
    pub title: String,
    pub title_generated: bool,
    pub turns: u32,
    pub created_at: String,
    pub last_message_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Conversations {
    pub conversations: Vec<Conversation>,
}

/// One question and its answer.
///
/// `searched` is the standalone question the rewrite produced, and it is shown
/// rather than kept for debugging: a follow-up is answered against a question
/// the person did not type, and an answer that quietly addresses something
/// adjacent is indistinguishable from a bad answer unless the substitution is
/// visible.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ConversationTurn {
    pub seq: u32,
    pub question: String,
    #[serde(default)]
    pub searched: Option<String>,
    #[serde(default)]
    pub answer: String,
    pub state: String,
    #[serde(default)]
    pub effort: String,
    #[serde(default)]
    pub style_effort: Option<String>,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub cited_evidence: Vec<EvidenceItem>,
    #[serde(default)]
    pub error: Option<AskFailure>,
    pub asked_at: String,
    #[serde(default)]
    pub answered_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ConversationDetail {
    pub id: String,
    pub library_id: String,
    pub title: String,
    pub title_generated: bool,
    pub turns: u32,
    pub created_at: String,
    pub last_message_at: String,
    #[serde(default)]
    pub turns_detail: Vec<ConversationTurn>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ConversationStarted {
    pub conversation_id: String,
    pub library_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct TurnStarted {
    pub conversation_id: String,
    pub turn_seq: u32,
    pub state: String,
}

/// The body of `POST /chat`. A request shape, so the rename goes the other way.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct NewConversation {
    pub library_id: String,
}

/// The body of `POST /chat/{id}/turn`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct NewTurn {
    pub text: String,
    /// Omitted when absent rather than defaulted here. A level spelled into
    /// this struct would be a copy of a value that already lives in the Python
    /// dataclass, and the copy that silently disagreed after somebody moved the
    /// other — the same reasoning `ask.service.ts` records for the same field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
    /// The same four narrowings `Question` carries, per turn. Omitted when
    /// absent, for the same reason as `effort`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recorded_from: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recorded_to: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scripture: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_name: Option<String>,
}

/// One server-sent event from a turn in flight.
///
/// Every field but the discriminator is optional because the three event types
/// share this one shape — `token` carries `seq` and `text`, `done` carries
/// `turn`, `error` carries `kind` and `message`. Modelling them as one struct
/// rather than an enum is deliberate: an event type this build has never heard
/// of arrives as data with an unknown `event` rather than failing to
/// deserialize, so a newer server cannot take the stream down.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChatEvent {
    /// `token`, `done` or `error`. Named `event` in Rust because `type` is a
    /// keyword; it is `type` on both wires.
    #[serde(rename = "type")]
    pub event: String,
    #[serde(default)]
    pub seq: Option<u32>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub turn: Option<ConversationTurn>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub message: Option<String>,
    /// Which stage a `stage` event is announcing.
    ///
    /// Declared, because serde drops what it does not declare — and a proxy that
    /// silently ate this would leave the window showing one unchanging line for
    /// the nine tenths of a turn these events exist to cover.
    #[serde(default)]
    pub stage: Option<String>,
    /// How many chunks reached the prompt, on the `evidence` stage.
    #[serde(default)]
    pub chunks: Option<u32>,
    /// How many of them cleared the dense floor. `None` is "not measured",
    /// which is different from zero — zero is a question the corpus does not
    /// support, and the two must not render the same.
    #[serde(default)]
    pub dense: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Citation {
    pub chunk_id: String,
    pub locator: String,
    pub claim: String,
    pub page: Option<u32>,
    pub section_title: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct EvidenceItem {
    pub chunk_id: String,
    pub title: String,
    pub breadcrumb: String,
    pub text: String,
    pub kind: String,
    pub score: f64,
    /// "vector" or "graph". Shown, because an answer resting on a
    /// model-proposed edge has different standing from one resting on the
    /// document's own structure.
    pub source: String,
    #[serde(default)]
    pub locator: String,
    /// The document the chunk belongs to. Python has always sent it; this
    /// struct dropped it on the way through — serde discards unknown fields —
    /// until the media link needed it: a citation on a recording opens the
    /// audio at its second through `media_link`, which is addressed by
    /// document. Defaulted, so a plane that omits it still parses.
    #[serde(default)]
    pub document_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Spend {
    pub stage: String,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub usd: Option<f64>,
}

/// What `ask` returns now: the question is in flight and this is how to find it
/// again. Deliberately not the answer — see `ASK_TIMEOUT`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AskStarted {
    pub question_id: String,
    pub state: String,
}

/// A poll. `answer` is present only once `state` is "done"; `error` only once it
/// is "failed". The API sets its state last for exactly this reason, so a poll
/// never sees a finished question with nothing in it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AskProgress {
    pub question_id: String,
    pub state: String,
    #[serde(default)]
    pub answer: Option<Answer>,
    #[serde(default)]
    pub error: Option<AskFailure>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct AskFailure {
    pub kind: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Answer {
    /// "answered" | "insufficient_evidence" | "off_corpus" — three states on
    /// purpose, because they have three different fixes.
    pub state: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub evidence: Vec<EvidenceItem>,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub spend: Vec<Spend>,
}

// ---------------------------------------------------------------------------
// Library
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct DocumentRow {
    pub id: String,
    pub title: String,
    pub author: Option<String>,
    pub format: String,
    pub source_key: String,
    pub present: bool,
    #[serde(default)]
    pub tags: Vec<String>,
    pub active_version_id: Option<String>,
    pub updated_at: Option<String>,
    /// When a recording was made and published, as its source's manifest said;
    /// the feed's own link; which feed or folder it came from. All absent for
    /// a book, and defaulted so a plane from before the columns still parses.
    #[serde(default)]
    pub recorded_at: Option<String>,
    #[serde(default)]
    pub published_at: Option<String>,
    #[serde(default)]
    pub source_url: Option<String>,
    #[serde(default)]
    pub source_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Library {
    pub library_id: String,
    pub documents: Vec<DocumentRow>,
}

/// One row of the library picker.
///
/// `indexed_versions` rather than `documents` is what decides whether a library
/// can answer a question: a registered document whose version never finished
/// indexing retrieves nothing, and the picker has to be able to say so before
/// the user spends a question finding out.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct LibraryRow {
    pub id: String,
    pub name: String,
    pub language: String,
    pub documents: i64,
    pub indexed_versions: i64,
}

/// What `GET /libraries` returns.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Libraries {
    pub libraries: Vec<LibraryRow>,
}

/// One version of a document, as the Library's detail row lists it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionRow {
    pub id: String,
    pub content_sha256: String,
    pub byte_size: i64,
    pub page_count: Option<i64>,
    pub state: String,
    pub active: bool,
    pub created_at: Option<String>,
    /// The run whose artifacts a rebuild would replay, or `None` when no run
    /// kept them — which is why the button can be disabled with a reason.
    pub rebuild_run_id: Option<String>,
    /// The run to fetch this version's book from, or `None` when it has none
    /// yet. A run id rather than a flag, because the download is addressed by
    /// run: a client that knew only *whether* one existed would have to ask
    /// again to find out where.
    #[serde(default)]
    pub epub_run_id: Option<String>,
    /// Other documents holding these same bytes. Non-empty means removing this
    /// document leaves the version standing, and the confirm has to say so.
    #[serde(default)]
    pub also_held_by: Vec<String>,
    /// What this version's index can be asked, from the newest run that measured
    /// it. `None` means nobody measured — which is every version on this
    /// installation today, and is not the same claim as a recall of zero.
    #[serde(default)]
    pub scores: Option<RunScores>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct DocumentDetail {
    pub id: String,
    pub library_id: String,
    pub title: String,
    pub author: Option<String>,
    pub format: String,
    pub source_key: String,
    /// `None` for a document imported before the catalog recorded it. That is
    /// exactly why `can_reindex` is computed server-side rather than inferred
    /// here from a missing field.
    pub source_path: Option<String>,
    pub present: bool,
    #[serde(default)]
    pub tags: Vec<String>,
    pub active_version_id: Option<String>,
    pub can_reindex: bool,
    pub can_rebuild: bool,
    /// Whether this version's chunks are still on disk to compose a book from.
    /// Defaulted, so a plane that predates the feature reads as "no" rather
    /// than failing the whole detail payload.
    #[serde(default)]
    pub can_build_epub: bool,
    #[serde(default)]
    pub versions: Vec<VersionRow>,
    /// See `DocumentRow`: a recording's dates, feed link and source.
    #[serde(default)]
    pub recorded_at: Option<String>,
    #[serde(default)]
    pub published_at: Option<String>,
    #[serde(default)]
    pub source_url: Option<String>,
    #[serde(default)]
    pub source_name: Option<String>,
}

/// What a standalone build produced.
///
/// Awaited rather than a run id to poll, because the book is a file read, a
/// render and a file write — and the caller's very next act is to download it.
/// `run_id` is what the download is addressed by.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct BookBuilt {
    pub version_id: String,
    pub run_id: String,
    pub artifact: String,
    pub bytes: u64,
    pub title: String,
    #[serde(default)]
    pub author: Option<String>,
    /// What the metadata call cost, or `None` when it was not made — which is
    /// the ordinary case, because it runs once per document ever.
    #[serde(default)]
    pub usd: Option<f64>,
}

/// What a person may correct about a document.
///
/// Both fields optional, and `None` means "leave this one alone" — which is
/// what lets a screen send only the field somebody edited. Renamed on the
/// deserialize side like every other request type, because the webview speaks
/// camelCase and Python speaks snake_case.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all(deserialize = "camelCase"))]
pub struct DocumentMetadata {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author: Option<String>,
    /// `YYYY-MM-DD`, or an empty string to clear. Absent means "leave alone".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recorded_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub published_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct DocumentMetadataResult {
    pub id: String,
    pub title: String,
    pub author: Option<String>,
}

// ---------------------------------------------------------------------------
// One version's statistics
// ---------------------------------------------------------------------------
//
// Five legs, and **every data field on every leg is optional**, because
// `available: false` carries none of them. That is not defensive typing: a leg
// that could not answer must be unable to supply a figure, and making the
// fields non-optional would force a default — which is how "the graph is
// stopped" becomes "this version has no concepts".

/// One leg's verdict: `available: false` plus the URL or reason it failed on.
///
/// The data fields live on each leg beside this rather than inside a nested
/// object, because that is the shape Python emits — `auditversion.leg` splats
/// its payload next to the three bookkeeping keys — and a translation struct
/// here would be a second opinion about the wire format.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionCatalogLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub content_sha256: Option<String>,
    #[serde(default)]
    pub byte_size: Option<u64>,
    /// A column nothing writes: `register_version` runs before extraction, so
    /// the page count is not knowable there. `None` is the measurement, not a
    /// zero, and the field starts working the day something fills it.
    #[serde(default)]
    pub page_count: Option<u32>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub active: Option<bool>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub activated_at: Option<String>,
    #[serde(default)]
    pub failed_reason: Option<String>,
    #[serde(default)]
    pub runs: Option<u32>,
    #[serde(default)]
    pub rebuild_run_id: Option<String>,
    #[serde(default)]
    pub profile_warnings: Vec<VersionProfileWarning>,
}

/// A profile collision raised at a gate, and whether its figure means anything.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionProfileWarning {
    #[serde(default)]
    pub profile_id: Option<String>,
    #[serde(default)]
    pub collides_with: Option<String>,
    #[serde(default)]
    pub similarity: Option<f64>,
    #[serde(default)]
    pub detail: Option<String>,
    /// `_topical_overlap` returns 0.0 by construction for a plain-text
    /// document, and 0.0 is documented as the *most dangerous* case — same
    /// structure, unrelated subject matter. `false` means the figure beside it
    /// is not a measurement and must not be rendered as one.
    #[serde(default)]
    pub comparable: bool,
}

/// The graph's own count of what this version projected.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionGraphLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub chunks: Option<u32>,
    #[serde(default)]
    pub sections: Option<u32>,
    #[serde(default)]
    pub citations: Option<u32>,
    #[serde(default)]
    pub claims: Option<u32>,
    /// Keyed by the chunk kind as stored — `cuerpo`, `preguntas`, `nota` —
    /// which stays Spanish on the wire because these are Qdrant payload values
    /// used in filters. `rename_all` renames struct fields and never map keys,
    /// so these survive the hop unchanged and the UI maps them.
    #[serde(default)]
    pub kinds: std::collections::BTreeMap<String, u32>,
    #[serde(default)]
    pub section_levels: std::collections::BTreeMap<String, u32>,
}

/// How many points Qdrant holds for this version, exactly.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionQdrantLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub points: Option<u32>,
}

/// The run's own `chunks.jsonl`, against the byte stream it indexed.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionArtifactsLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    /// Which stream the `char_span`s actually index. Nothing records it, so it
    /// is chosen by scoring every stream present — measured on one version,
    /// `raw.txt` verifies 8 of 600 spans where `extracted.txt` verifies 600.
    #[serde(default)]
    pub stream: Option<String>,
    #[serde(default)]
    pub stream_verifies_completely: Option<bool>,
    #[serde(default)]
    pub streams_considered: std::collections::BTreeMap<String, VersionStreamScore>,
    #[serde(default)]
    pub spans: Option<VersionSpans>,
    #[serde(default)]
    pub sequence: Option<VersionSequence>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStreamScore {
    pub bytes: u64,
    pub spans_verified: u32,
    pub spans_mismatched: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionSpans {
    pub chunks: u32,
    pub spans_verified: u32,
    pub spans_mismatched: u32,
    pub bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionSequence {
    pub indices: u32,
    pub contiguous: bool,
    #[serde(default)]
    pub missing_indices: Vec<u32>,
    #[serde(default)]
    pub duplicate_indices: Vec<u32>,
    pub bytes_covered: u64,
    pub bytes_total: u64,
    pub coverage: f64,
}

/// How big this version is, in all three stores at once.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStructureLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub source_run: Option<String>,
    pub graph: VersionGraphLeg,
    pub qdrant: VersionQdrantLeg,
    pub artifacts: VersionArtifactsLeg,
    /// `None` is "could not compare" and only `Some(false)` is the claim that
    /// the stores disagree. Always present on the wire so a reader never has to
    /// tell an absent key from a null one.
    #[serde(default)]
    pub counts_agree: Option<bool>,
}

/// What the graph holds of this version's semantics right now.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionSemanticsInStore {
    pub claims: u32,
    /// Claims carrying a quote the code located in their own chunk. A claim
    /// nobody can check must not look like one that can.
    pub with_a_quote: u32,
    /// `afirma`, `niega`, `atribuido`, `sin_estado` — Spanish on the wire like
    /// the chunk kinds, and `sin_estado` is never folded into `afirma`.
    #[serde(default)]
    pub by_status: std::collections::BTreeMap<String, u32>,
    pub concepts: u32,
}

/// One three-way split between what a run made and what is there now.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStaleDiff {
    pub produced: u32,
    pub in_store: u32,
    pub converged: u32,
    /// The defect: something the graph holds that this run did not make. Not
    /// inert — a stale claim stays attached to a chunk whose text has moved and
    /// reads exactly like a good one.
    pub left_behind: u32,
    /// Its mirror, and it matters as much: something the run produced and the
    /// graph lacks is a projection that did not finish.
    pub missing: u32,
    #[serde(default)]
    pub stale_share: Option<f64>,
    #[serde(default)]
    pub names_extracted: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStaleQuotes {
    pub with_a_quote: u32,
    pub quote_no_longer_locates: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionSemanticsDiff {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub claims: Option<VersionStaleDiff>,
    #[serde(default)]
    pub concepts: Option<VersionStaleDiff>,
    #[serde(default)]
    pub mentions: Option<VersionStaleDiff>,
    #[serde(default)]
    pub stale_claim_quotes: Option<VersionStaleQuotes>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionSemanticsLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub source_run: Option<String>,
    #[serde(default)]
    pub extractor_model: Option<String>,
    #[serde(default)]
    pub in_store: Option<VersionSemanticsInStore>,
    #[serde(default)]
    pub diff: Option<VersionSemanticsDiff>,
}

/// Whether the dense floor sits above what a wrong chunk scores.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionFloor {
    pub min_score: f64,
    pub noise_floor: f64,
    pub headroom: f64,
    /// `false` means the floor admits exactly what it was measured to exclude —
    /// and it looks like an improvement, because every eval question has a right
    /// answer to find and none of them is off-corpus.
    pub honest: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionRetrievalLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub source_run: Option<String>,
    /// Absent means **nobody measured**, which is not a recall of zero.
    #[serde(default)]
    pub scores: Option<RunScores>,
    #[serde(default)]
    pub floor: Option<VersionFloor>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStageCost {
    pub usd: f64,
    /// Every run that charged this stage. More than one is the finding.
    #[serde(default)]
    pub runs: Vec<String>,
    /// A missing price under-reports the bill rather than describing a free
    /// call, so it is counted and never totalled as zero.
    #[serde(default)]
    pub unpriced_entries: u32,
}

/// What this **version** cost, across every run that touched it.
///
/// Not one run's bill: on `ver_0cde…` the eval set was generated twice, the
/// first time inside a run that was cancelled. Grouping by run answers only
/// half of it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionLedgerLeg {
    pub available: bool,
    #[serde(default)]
    pub detail: String,
    /// Keyed by stage name as the ledger stores it — a map, so `rename_all`
    /// leaves the keys alone.
    #[serde(default)]
    pub by_stage: std::collections::BTreeMap<String, VersionStageCost>,
    #[serde(default)]
    pub total_usd: f64,
    #[serde(default)]
    pub charged_in_more_than_one_run: Vec<String>,
    /// Split by the terminal state of the run that incurred it. A *split*
    /// rather than a figure called "wasted": a cancelled run bought nothing
    /// durable, but a failed one can still have left a complete index behind.
    #[serde(default)]
    pub usd_by_run_state: std::collections::BTreeMap<String, f64>,
}

/// Everything one indexed version holds, cost, and still agrees with.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionStatistics {
    pub library_id: String,
    pub document_id: String,
    pub version_id: String,
    pub catalog: VersionCatalogLeg,
    pub structure: VersionStructureLeg,
    pub semantics: VersionSemanticsLeg,
    pub retrieval: VersionRetrievalLeg,
    pub ledger: VersionLedgerLeg,
}

/// What a removal destroyed, and what it deliberately did not.
///
/// Both halves cross the boundary because an irreversible act reported only as
/// "done" leaves the user to guess whether their cost history went with it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Removal {
    pub document_id: Option<String>,
    #[serde(default)]
    pub versions_removed: Vec<String>,
    #[serde(default)]
    pub versions_kept: Vec<String>,
    pub qdrant_points: i64,
    pub qdrant_repointed: i64,
    #[serde(default)]
    pub graph: std::collections::BTreeMap<String, i64>,
    #[serde(default)]
    pub catalog: std::collections::BTreeMap<String, i64>,
    #[serde(default)]
    pub kept: std::collections::BTreeMap<String, String>,
}

/// What promoting a withheld version touched.
///
/// Every document holding these bytes, not just the one asked about: a version
/// can be shared — byte-identical files at two paths are two documents and one
/// version — and promoting it for one while leaving the other on an older
/// version would make the same content answer differently depending on which
/// copy was asked about.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Activation {
    pub version_id: String,
    #[serde(default)]
    pub documents: Vec<String>,
}

/// The rebuild's one-question gate. One stage here can spend, not a table of
/// them, which is why this is not a `GateReport`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RebuildReport {
    pub document_id: String,
    pub version_id: String,
    pub title: String,
    pub chunk_count: i64,
    pub characters: i64,
    pub source_run_id: String,
    /// False for a document indexed before the `semantics` artifact existed.
    /// The screen says what will not be restored rather than quietly thinning
    /// the graph.
    pub semantics_available: bool,
    pub estimate: Option<Estimate>,
}

// -- Explore ---------------------------------------------------------------
//
// The graph half of the UI. Six shapes, and the split between them is the point
// rather than an accident of the schema: `Outline`, `SectionChunks` and
// `ChunkContext` are derived from the document's own structure, while
// `VersionConcepts`, `RelatedDocuments` and `ConceptClaims` were proposed by a
// model. Each of the latter carries `semantic` and a `confidence_floor` so the
// screen can label the difference — an edge a model suggested and an edge read
// off a table of contents have different standing as evidence.

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Section {
    pub id: String,
    pub title: String,
    /// A dotted ordinal path ("1.1"), not a number: the screen reads nesting
    /// depth from how many segments it has.
    pub path: String,
    pub level: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Outline {
    pub version_id: String,
    pub sections: Vec<Section>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChunkRow {
    pub id: String,
    pub kind: String,
    pub ordinal: i64,
    pub text: String,
    pub char_start: i64,
    pub char_end: i64,
    pub page: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct SectionChunks {
    pub section_id: String,
    pub chunks: Vec<ChunkRow>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Neighbours {
    pub id: String,
    pub text: String,
    pub kind: String,
    pub char_start: i64,
    pub char_end: i64,
    pub before_id: Option<String>,
    pub before_text: Option<String>,
    pub after_id: Option<String>,
    pub after_text: Option<String>,
}

/// A citation reached by browsing rather than by answering.
///
/// Deliberately not the `Citation` above, which carries the `claim` it supports:
/// browsing to a chunk asks "where is this in the original?", and there is no
/// assertion being supported. Reusing that type would have meant either an
/// always-empty `claim` field or a failed deserialization, and the first is the
/// worse of the two — an empty claim renders as a citation supporting nothing.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChunkCitation {
    pub id: String,
    pub chunk_id: String,
    pub locator: String,
    pub page: Option<u32>,
    pub section_title: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ChunkContext {
    pub chunk_id: String,
    pub context: Option<Neighbours>,
    /// The locator into the original. `None` means this chunk has none, which
    /// the screen must show as absence rather than silently omit — a citation
    /// the user cannot open is not a citation.
    pub citation: Option<ChunkCitation>,
}

/// Project-wide totals, each leg carrying its own availability.
///
/// Every count is an `Option` and none of them defaults to zero. The API
/// reports "could not be read" apart from "is empty" because the two have
/// different fixes, and a `#[serde(default)]` here would collapse the
/// distinction on the way through — a stopped Memgraph would render as a
/// project holding no concepts.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ProjectSummary {
    pub catalog: CatalogTotals,
    pub graph: GraphTotals,
    pub pages: PageTotals,
    /// `None` when the catalog could not be read at all; an empty list when it
    /// answered and nothing has run yet.
    pub recent_runs: Option<Vec<RunSummary>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct CatalogTotals {
    pub available: bool,
    /// Why it could not be read. `None` when it could.
    pub detail: Option<String>,
    pub libraries: Option<i64>,
    pub documents: Option<i64>,
    pub absent_documents: Option<i64>,
    pub active_versions: Option<i64>,
    pub indexed_versions: Option<i64>,
    pub indexed_bytes: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GraphTotals {
    pub available: bool,
    pub detail: Option<String>,
    /// Node counts by label.
    pub nodes: Option<std::collections::BTreeMap<String, i64>>,
    /// Edges the documents' own structure supplies.
    pub deterministic_edges: Option<std::collections::BTreeMap<String, i64>>,
    /// Edges a model proposed. Kept apart from the deterministic ones so a
    /// screen cannot report model output as something the corpus stated.
    pub semantic_edges: Option<std::collections::BTreeMap<String, i64>>,
}

/// How many versions record a page count, and their sum.
///
/// Nothing writes `page_count` today — the version is registered before the
/// document has been extracted, so the number is not knowable there. This
/// carries the measurement rather than a hardcoded absence, so the figure
/// appears by itself the day the column is filled in.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct PageTotals {
    pub available: bool,
    pub recorded: Option<i64>,
    pub of: Option<i64>,
    pub pages: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RunSummary {
    pub id: String,
    pub workflow_id: String,
    pub kind: String,
    pub state: String,
    pub stage: Option<String>,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub error_kind: Option<String>,
    /// `None` for a run whose document has been removed. Run history outlives
    /// the document it was spent on, deliberately.
    pub title: Option<String>,
    pub library_id: Option<String>,
    /// Billed so far, summed from the ledger. `None`, never zero: a run still in
    /// its free stages and a run whose model has no known price are both "no
    /// figure", and zero would claim the run was free.
    ///
    /// **It lags during the stage that costs most.** Semantic extraction records
    /// its spend when the activity finishes, not per chunk, so a run 260 calls
    /// into it still reports only what embedding cost. That is the reason
    /// `RunProgress` earns its place beside this rather than duplicating it.
    #[serde(default)]
    pub usd_so_far: Option<f64>,
}

/// A whole library as nodes and weighted edges.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct LibraryGraph {
    pub library_id: String,
    /// Always true: every edge here rests on a `MENTIONS` a model proposed.
    pub semantic: bool,
    pub confidence_floor: f64,
    /// The degree filter that produced this result. The volume control is this,
    /// not the confidence floor — measured on the real corpus, raising the floor
    /// from 0.6 to 0.9 removes 3% of edges while `min_documents = 2` removes 84%.
    pub min_documents: i64,
    pub documents: Vec<GraphDocument>,
    pub concepts: Vec<GraphConcept>,
    pub edges: Vec<GraphEdge>,
    pub truncated: GraphTruncation,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GraphDocument {
    pub document_id: String,
    pub version_id: String,
    pub title: Option<String>,
    pub format: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GraphConcept {
    pub id: String,
    pub name: String,
    #[serde(rename(serialize = "conceptType"))]
    pub concept_type: Option<String>,
    /// Mentions summed across every book that mentions it.
    pub mentions: i64,
    /// How many books mention it — the degree the database counted, not the
    /// number of edges that survived any cap.
    pub documents: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GraphEdge {
    pub version_id: String,
    pub concept_id: String,
    /// How many chunks of this book mention this concept.
    pub mentions: i64,
    pub confidence: f64,
}

/// "At least this many", not a true total: establishing the real count costs a
/// second traversal, and the caps sit well above what a library produces.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct GraphTruncation {
    pub documents: bool,
    pub edges: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Concept {
    pub id: String,
    pub name: String,
    #[serde(rename(serialize = "conceptType"))]
    pub r#type: Option<String>,
    /// One sentence about the concept, assembled from what the chunks mentioning
    /// it said about it. `None` until a run condenses them, which is a paid stage
    /// switched off by default — so a bare name stays the common case.
    #[serde(default)]
    pub description: Option<String>,
    pub mentions: i64,
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct VersionConcepts {
    pub version_id: String,
    pub semantic: bool,
    pub confidence_floor: f64,
    pub concepts: Vec<Concept>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RelatedDocument {
    pub id: String,
    pub title: Option<String>,
    /// The document that owns the version. A version id is not something a
    /// person can navigate to.
    pub document_id: Option<String>,
    pub shared_concepts: i64,
    /// *Which* concepts are shared, not only how many. Parallel arrays: the id
    /// at index i names the concept whose name is at index i, both taken from
    /// one `collect` so they cannot disagree.
    ///
    /// Capped at 200 by the template, and `shared_concepts` stays the true
    /// count — so a shorter list means truncated, not fewer.
    ///
    /// Defaulted: an older API returns neither, and the graph then falls back to
    /// showing the count alone rather than failing to render.
    #[serde(default)]
    pub shared_concept_ids: Vec<String>,
    #[serde(default)]
    pub shared_concept_names: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct RelatedDocuments {
    pub version_id: String,
    pub semantic: bool,
    pub confidence_floor: f64,
    pub documents: Vec<RelatedDocument>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct Claim {
    pub id: String,
    pub text: String,
    pub confidence: f64,
    /// The chunk a person can check this against. A relation nobody can verify
    /// is worse than no relation, because it still looks like evidence.
    pub source_chunk_id: String,
    /// The span of the chunk the claim was extracted from, when the worker could
    /// find the model's quote in the chunk's own text. `None` for a claim
    /// projected before quotes existed, or one whose quote did not check out.
    ///
    /// The API also returns the quote's character offsets. They are deliberately
    /// not carried here: serde ignores fields no struct names, and nothing opens
    /// a source at an offset yet. Add them when something does.
    #[serde(default)]
    pub quote: Option<String>,
    /// What the document does with the claim: `afirma`, `niega`, `atribuido`, or
    /// `sin_estado` when it was extracted before the field existed. Spanish on
    /// the wire like the chunk kinds, localised by the UI.
    ///
    /// Defaulted so an older API — one whose template does not return the field
    /// yet — costs the reader a badge rather than the whole panel.
    #[serde(default)]
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase"))]
pub struct ConceptClaims {
    pub concept_id: String,
    pub semantic: bool,
    pub confidence_floor: f64,
    pub claims: Vec<Claim>,
}

/// Split a transport failure by its cause rather than by where it happened.
///
/// reqwest reports a refused connection and an expired deadline through one
/// error type, and the UI's advice for them is opposite: a refused connection
/// may well be a stack that is still coming up, while an expired deadline means
/// the API is there and did not finish in time. Collapsing them cost two
/// investigations aimed at the wrong component — once at a port allocator that
/// was innocent, once at a control API that had logged `200 OK`.
/// Bytes in, whole events out.
///
/// Pulled out of `chat_stream` because the interesting part cannot be reached
/// through it: what is worth asserting is what happens at a *chunk boundary*,
/// and a boundary is decided by the network. As a struct it takes a table of
/// byte slices instead.
///
/// **Decoding waits for a newline.** A chunk can end in the middle of a
/// multi-byte character, and this corpus is Spanish — decoding each chunk as it
/// arrived would turn every accent unlucky enough to straddle a boundary into a
/// replacement character, in the one text the reader is actually reading. A
/// newline is ASCII and cannot appear inside a UTF-8 sequence, so a line that
/// ends with one is always safe to decode.
#[derive(Default)]
struct SseBuffer {
    buf: Vec<u8>,
}

impl SseBuffer {
    /// Feed a chunk; get the events it completed. Anything after the last
    /// newline is a partial line and is kept for the next call.
    fn push(&mut self, chunk: &[u8]) -> Vec<ChatEvent> {
        self.buf.extend_from_slice(chunk);
        let mut out = Vec::new();
        while let Some(at) = self.buf.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buf.drain(..=at).collect();
            let line = String::from_utf8_lossy(&line);
            let line = line.trim_end_matches(['\r', '\n']);
            let Some(payload) = line.strip_prefix("data: ") else {
                // Blank separators between events, and any SSE field this build
                // does not read (`event:`, `id:`, `retry:`, a `:` comment).
                continue;
            };
            // An event this build cannot parse is dropped rather than ending the
            // stream: the answer is still arriving, and the authoritative copy
            // is in the catalog either way.
            if let Ok(event) = serde_json::from_str::<ChatEvent>(payload) {
                out.push(event);
            }
        }
        out
    }
}

fn unreachable(url: String, source: reqwest::Error) -> AppError {
    if source.is_timeout() {
        AppError::ControlTimeout { url, source }
    } else {
        AppError::ControlUnreachable { url, source }
    }
}

/// The credentials the paid plane needs, and the local one refuses to have.
///
/// `Debug` is written by hand so a token cannot reach a log through a
/// `{:?}` on `Control` — which every derived `Debug` above this would have
/// done for free.
#[derive(Clone)]
pub struct Auth {
    pub token: String,
    /// `X-Tenant-Id`. Absent is correct for an account in exactly one
    /// organisation; the server refuses to guess for one in several.
    pub tenant: Option<String>,
}

impl std::fmt::Debug for Auth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Auth")
            .field("token", &"<redacted>")
            .field("tenant", &self.tenant)
            .finish()
    }
}

#[derive(Debug, Clone)]
pub struct Control {
    base: String,
    http: reqwest::Client,
    auth: Option<Auth>,
}

impl Control {
    /// The free plane: loopback, no credentials, and none possible.
    pub fn local(port: u16) -> Self {
        Self {
            base: format!("http://127.0.0.1:{port}"),
            http: reqwest::Client::new(),
            auth: None,
        }
    }

    /// The paid plane. `base` is validated where it is set, not here.
    pub fn cloud(base: String, auth: Auth) -> Self {
        Self {
            base,
            http: reqwest::Client::new(),
            auth: Some(auth),
        }
    }

    async fn get<T: serde::de::DeserializeOwned>(&self, path: &str, timeout: Duration) -> Result<T> {
        self.send(self.http.get(format!("{}{path}", self.base)), path, timeout)
            .await
    }

    async fn post<T: serde::de::DeserializeOwned>(&self, path: &str, timeout: Duration) -> Result<T> {
        self.send(self.http.post(format!("{}{path}", self.base)), path, timeout)
            .await
    }

    async fn delete<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        timeout: Duration,
    ) -> Result<T> {
        self.send(self.http.delete(format!("{}{path}", self.base)), path, timeout)
            .await
    }

    async fn post_json<B: Serialize, T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
        timeout: Duration,
    ) -> Result<T> {
        self.send(
            self.http.post(format!("{}{path}", self.base)).json(body),
            path,
            timeout,
        )
        .await
    }

    async fn put_json<B: Serialize, T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
        timeout: Duration,
    ) -> Result<T> {
        self.send(
            self.http.put(format!("{}{path}", self.base)).json(body),
            path,
            timeout,
        )
        .await
    }

    /// Attach credentials, send, and check the status. The body is untouched.
    ///
    /// Every helper funnels through here, which is what makes "credentials are
    /// attached in exactly one place" true rather than a convention — a new
    /// helper cannot be added that forgets them.
    ///
    /// `timeout` is a **total** budget and is therefore wrong for a response
    /// whose body arrives over the life of an answer. `None` leaves it off, and
    /// the one caller that passes `None` — `chat_stream` — bounds itself per
    /// read instead. See `CHAT_STREAM_IDLE`.
    async fn send_raw(
        &self,
        req: reqwest::RequestBuilder,
        path: &str,
        timeout: Option<Duration>,
    ) -> Result<reqwest::Response> {
        let url = format!("{}{path}", self.base);
        let req = match &self.auth {
            Some(auth) => {
                let req = req.bearer_auth(&auth.token);
                match &auth.tenant {
                    Some(tenant) => req.header("X-Tenant-Id", tenant),
                    None => req,
                }
            }
            None => req,
        };
        let req = match timeout {
            Some(t) => req.timeout(t),
            None => req,
        };
        // See `unreachable`: which of the two this becomes is decided by the
        // cause, not by the call site.
        let response = req
            .send()
            .await
            .map_err(|source| unreachable(url.clone(), source))?;

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            // The whole body, because the constructor reads the control
            // API's `detail.kind` out of it before cutting it down for a
            // person to read. Truncating here would sometimes take the tag.
            return Err(AppError::control_status(status.as_u16(), body));
        }
        Ok(response)
    }

    async fn send<T: serde::de::DeserializeOwned>(
        &self,
        req: reqwest::RequestBuilder,
        path: &str,
        timeout: Duration,
    ) -> Result<T> {
        let url = format!("{}{path}", self.base);
        self.send_raw(req, path, Some(timeout))
            .await?
            .json::<T>()
            .await
            .map_err(|source| unreachable(url, source))
    }

    /// A request whose success carries no body, such as a 204.
    ///
    /// Separate from `send` rather than decoding into `()`: serde cannot make
    /// `()` out of an empty body, so a 204 through the JSON path fails *after*
    /// the server has already done the thing — which reads as "the delete did
    /// not work" about a delete that did.
    async fn send_empty(
        &self,
        req: reqwest::RequestBuilder,
        path: &str,
        timeout: Duration,
    ) -> Result<()> {
        self.send_raw(req, path, Some(timeout)).await.map(|_| ())
    }

    pub async fn health(&self) -> Result<Health> {
        self.get("/health", HEALTH_TIMEOUT).await
    }

    pub async fn ping(&self) -> Result<PingResult> {
        self.post("/ping", PING_TIMEOUT).await
    }

    /// Hand a local file to the paid plane, and get back the path the worker
    /// will read it by.
    ///
    /// Only cloud mode has this. In local mode the worker and the app share a
    /// filesystem through the compose volume, so a copy over HTTP would be an
    /// upload to oneself; `stage_source` in `lib.rs` returns the path unchanged
    /// there and never calls this.
    ///
    /// **The file is read into memory.** The server caps a body at 200 MB and
    /// this is a desktop app, so the simplicity is worth more than the
    /// streaming; if that cap ever rises, this is what has to change first.
    ///
    /// The filename is sent because the server records it as `source_key` — the
    /// human-facing name of the document — but the server does **not** use it
    /// to name what it writes. That is its decision to make and it makes it;
    /// nothing here should depend on the two agreeing.
    pub async fn upload_source(&self, path: &std::path::Path) -> Result<StagedSource> {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "documento".to_string());
        let bytes = std::fs::read(path).map_err(|e| AppError::io(path.display(), e))?;
        let part = reqwest::multipart::Part::bytes(bytes).file_name(name);
        let form = reqwest::multipart::Form::new().part("file", part);
        self.send(
            self.http
                .post(format!("{}/uploads", self.base))
                .multipart(form),
            "/uploads",
            UPLOAD_TIMEOUT,
        )
        .await
    }

    /// Put audio this machine downloaded into the organisation's own inbox.
    ///
    /// The counterpart of `upload_source`, and a separate route rather than a
    /// second use of `/uploads` for two reasons: that one refuses anything
    /// outside `SUPPORTED_FORMATS`, which is a list of *document* extensions
    /// and rightly so; and it buffers the whole body in memory, which is a
    /// 200 MB cap chosen for a 900-page PDF and the wrong shape for an hour of
    /// audio. `/videos/audio` streams to disk and answers with the path
    /// `VideoRequest.audio_path` wants.
    ///
    /// Cloud mode only. In local mode the worker's own address is answered by
    /// YouTube, so `fetch_audio` runs where it always did and no file crosses
    /// anything.
    pub async fn upload_audio(&self, path: &std::path::Path) -> Result<StagedAudio> {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "audio.m4a".to_string());
        let bytes = std::fs::read(path).map_err(|e| AppError::io(path.display(), e))?;
        let part = reqwest::multipart::Part::bytes(bytes).file_name(name);
        let form = reqwest::multipart::Form::new().part("file", part);
        self.send(
            self.http
                .post(format!("{}/videos/audio", self.base))
                .multipart(form),
            "/videos/audio",
            AUDIO_UPLOAD_TIMEOUT,
        )
        .await
    }

    pub async fn start_ingest(
        &self,
        request: &IngestRequest,
        options: &StageOptions,
    ) -> Result<StartedRun> {
        // The control API takes the request and the switches as two top-level
        // arguments, so the body pairs them by name rather than nesting.
        self.post_json(
            "/ingest",
            &serde_json::json!({ "request": request, "options": options }),
            START_TIMEOUT,
        )
        .await
    }

    /// Start a video run. Free until the gate is answered.
    ///
    /// The body pairs the request and the switches by name, exactly as
    /// `/ingest` does — the embedded shape rather than the bare one, because
    /// that is what both planes serve for a two-body-parameter route.
    pub async fn start_video(
        &self,
        request: &VideoRequest,
        options: &StageOptions,
    ) -> Result<StartedRun> {
        self.post_json(
            "/videos",
            &serde_json::json!({ "request": request, "options": options }),
            START_TIMEOUT,
        )
        .await
    }

    /// A video run's gate, or `Ok(None)` while the probe is still running.
    ///
    /// Its own route, not `/runs/{id}/gate`: the two reports differ in shape,
    /// and decoding one into the other drops every field they do not share
    /// without failing — the same reason `rebuild-gate` is separate.
    pub async fn video_gate(&self, workflow_id: &str) -> Result<Option<VideoGateReport>> {
        match self
            .get::<VideoGateReport>(
                &format!("/runs/{workflow_id}/video-gate"),
                HEALTH_TIMEOUT,
            )
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// The gate report, or `Ok(None)` while the free stages are still running.
    ///
    /// A 409 is the normal answer for the first few seconds of every run, so it
    /// is modelled as absence rather than as an error the UI has to special-case
    /// by inspecting a status code.
    pub async fn gate(&self, workflow_id: &str) -> Result<Option<GateReport>> {
        match self
            .get::<GateReport>(&format!("/runs/{workflow_id}/gate"), HEALTH_TIMEOUT)
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Whether a run is still going, or what it ended as.
    ///
    /// Proxied because the gate alone cannot say. `gate` models a 409 as
    /// `Ok(None)`, and a run that died before publishing its report answers 409
    /// forever — so a screen polling the gate on an interval waits for
    /// something that is never coming unless it can ask this.
    pub async fn run_status(&self, workflow_id: &str) -> Result<RunState> {
        self.get(&format!("/runs/{workflow_id}"), HEALTH_TIMEOUT)
            .await
    }

    /// The persistent queue: every run this organisation has, newest first.
    ///
    /// Catalog only, so it answers with the stack's Temporal down — which is
    /// what lets the Import screen show a queue rather than an error panel while
    /// the worker is restarting.
    pub async fn runs(&self, query: &str) -> Result<RunListPage> {
        let path = if query.is_empty() {
            "/runs".to_string()
        } else {
            format!("/runs?{query}")
        };
        self.get(&path, RUNS_TIMEOUT).await
    }

    /// What the run did, stage by stage, and what each stage cost.
    pub async fn run_audit(&self, workflow_id: &str) -> Result<RunAudit> {
        self.get(&format!("/runs/{workflow_id}/audit"), AUDIT_TIMEOUT)
            .await
    }

    /// The raw workflow history — retries and timeouts no application code saw.
    pub async fn run_events(&self, workflow_id: &str) -> Result<RunEventPage> {
        self.get(&format!("/runs/{workflow_id}/events"), EVENTS_TIMEOUT)
            .await
    }

    pub async fn approve(&self, workflow_id: &str, approval: &Approval) -> Result<()> {
        let _: serde_json::Value = self
            .post_json(
                &format!("/runs/{workflow_id}/approve"),
                approval,
                START_TIMEOUT,
            )
            .await?;
        Ok(())
    }

    /// Stop a run that is already spending.
    ///
    /// The counterpart of `approve`. A gate that can only be opened is half a
    /// gate: before this the only way to stop an ingest 260 generation calls into
    /// semantic extraction was `temporal workflow cancel` from a shell.
    ///
    /// `POST` with no body, because the workflow id in the path is the whole
    /// request — there is nothing to decide, unlike an approval which carries the
    /// stage switches.
    pub async fn cancel_run(&self, workflow_id: &str) -> Result<()> {
        let _: serde_json::Value = self
            .post_json(
                &format!("/runs/{workflow_id}/cancel"),
                &serde_json::json!({}),
                START_TIMEOUT,
            )
            .await?;
        Ok(())
    }

    pub async fn ask(&self, question: &Question) -> Result<AskStarted> {
        self.post_json("/ask", question, ASK_TIMEOUT).await
    }

    pub async fn ask_result(&self, question_id: &str) -> Result<AskProgress> {
        self.get(&format!("/ask/{question_id}"), ASK_POLL_TIMEOUT)
            .await
    }

    /// Which libraries exist. Free, and the first call every screen needs: a
    /// library id is not something a person can be expected to type.
    // -- recasting a document -----------------------------------------------

    /// The genres, modes and purposes a picker draws. Paid plane only.
    pub async fn genres(&self) -> Result<TransformVocabulary> {
        self.get("/genres", HEALTH_TIMEOUT).await
    }

    pub async fn start_transform(
        &self,
        request: &TransformRequest,
        options: &TransformOptions,
    ) -> Result<StartedRun> {
        self.post_json(
            "/transform",
            &serde_json::json!({ "request": request, "options": options }),
            START_TIMEOUT,
        )
        .await
    }

    /// The first gate, or `Ok(None)` while the document is still being read.
    ///
    /// 409 is "not yet", the same convention every other gate here uses: the run
    /// exists and is simply not there, and a screen polling this must read it as
    /// keep-waiting rather than as failed.
    pub async fn transform_gate(&self, workflow_id: &str) -> Result<Option<TransformGateReport>> {
        match self
            .get::<TransformGateReport>(
                &format!("/runs/{workflow_id}/transform-gate"),
                HEALTH_TIMEOUT,
            )
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// The second gate, or `Ok(None)` while the outline is still being planned.
    pub async fn transform_plan(&self, workflow_id: &str) -> Result<Option<TransformPlanReport>> {
        match self
            .get::<TransformPlanReport>(
                &format!("/runs/{workflow_id}/transform-plan"),
                HEALTH_TIMEOUT,
            )
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Answer either gate.
    ///
    /// Its own route rather than `/approve`, because the payloads are different
    /// types: a transformation's options are not stage switches, and the shared
    /// route's DTO would refuse them outright.
    pub async fn approve_transform(
        &self,
        workflow_id: &str,
        approval: &TransformApproval,
    ) -> Result<()> {
        let _: serde_json::Value = self
            .post_json(
                &format!("/runs/{workflow_id}/transform-approve"),
                approval,
                START_TIMEOUT,
            )
            .await?;
        Ok(())
    }

    pub async fn answer_styles(&self) -> Result<AnswerStyles> {
        self.get("/answer-styles", HEALTH_TIMEOUT).await
    }

    pub async fn set_answer_style(
        &self,
        effort: &str,
        body: &AnswerStyleUpdate,
    ) -> Result<AnswerStyleSaved> {
        self.put_json(&format!("/answer-styles/{effort}"), body, HEALTH_TIMEOUT)
            .await
    }

    pub async fn libraries(&self) -> Result<Libraries> {
        self.get("/libraries", HEALTH_TIMEOUT).await
    }

    pub async fn library(&self, library_id: &str, include_absent: bool) -> Result<Library> {
        self.get(
            &format!("/libraries/{library_id}/documents?include_absent={include_absent}"),
            HEALTH_TIMEOUT,
        )
        .await
    }

    // -- Library verbs -----------------------------------------------------

    /// What one indexed version holds, what it cost, and what still agrees.
    ///
    /// Read-only across all three stores. Every leg degrades on its own, so a
    /// stopped Memgraph comes back as a leg saying so rather than as an error —
    /// which is why this returns a body where another read would return a
    /// status code.
    pub async fn version_statistics(
        &self,
        library_id: &str,
        version_id: &str,
    ) -> Result<VersionStatistics> {
        self.get(
            &format!("/libraries/{library_id}/versions/{version_id}/statistics"),
            STATISTICS_TIMEOUT,
        )
        .await
    }

    pub async fn document_detail(
        &self,
        library_id: &str,
        document_id: &str,
    ) -> Result<DocumentDetail> {
        self.get(
            &format!("/libraries/{library_id}/documents/{document_id}"),
            HEALTH_TIMEOUT,
        )
        .await
    }

    // -- conversations -----------------------------------------------------

    pub async fn chat_create(&self, body: &NewConversation) -> Result<ConversationStarted> {
        self.post_json("/chat", body, ASK_TIMEOUT).await
    }

    pub async fn chat_list(&self) -> Result<Conversations> {
        self.get("/chat", RUNS_TIMEOUT).await
    }

    pub async fn chat_read(&self, conversation_id: &str) -> Result<ConversationDetail> {
        self.get(&format!("/chat/{conversation_id}"), RUNS_TIMEOUT).await
    }

    pub async fn chat_delete(&self, conversation_id: &str) -> Result<()> {
        self.send_empty(
            self.http
                .delete(format!("{}/chat/{conversation_id}", self.base)),
            &format!("/chat/{conversation_id}"),
            REMOVE_TIMEOUT,
        )
        .await
    }

    pub async fn chat_turn(&self, conversation_id: &str, body: &NewTurn) -> Result<TurnStarted> {
        self.post_json(&format!("/chat/{conversation_id}/turn"), body, ASK_TIMEOUT)
            .await
    }

    /// Follow one turn's answer as it is written, handing each event to `on_event`.
    ///
    /// The only streaming call in this client, and the only one that passes no
    /// total timeout — see `CHAT_STREAM_IDLE` for why a total budget is the
    /// wrong shape here. It still goes through `send_raw`, so it cannot be the
    /// request that forgets its credentials.
    ///
    /// **Bytes are buffered and split on newlines before being decoded.** A
    /// chunk boundary can fall in the middle of a multi-byte character, and this
    /// corpus is Spanish — decoding each chunk as it arrives would turn every
    /// accent unlucky enough to straddle one into a replacement character. A
    /// newline is ASCII and cannot appear inside a UTF-8 sequence, so a complete
    /// line is always safe to decode.
    ///
    /// `since` is the resume point: pass the highest `seq` already seen and only
    /// what follows is sent. That is what makes reopening a window cost nothing.
    pub async fn chat_stream<F>(
        &self,
        conversation_id: &str,
        turn_seq: u32,
        since: u32,
        mut on_event: F,
    ) -> Result<()>
    where
        F: FnMut(ChatEvent),
    {
        let path = format!("/chat/{conversation_id}/turn/{turn_seq}/stream?since={since}");
        let url = format!("{}{path}", self.base);
        let mut response = self
            .send_raw(self.http.get(&url), &path, None)
            .await?;

        let mut lines = SseBuffer::default();
        loop {
            let chunk = match tokio::time::timeout(CHAT_STREAM_IDLE, response.chunk()).await {
                Err(_) => {
                    return Err(AppError::ControlStreamStalled {
                        url,
                        seconds: CHAT_STREAM_IDLE.as_secs(),
                    })
                }
                Ok(Err(source)) => return Err(unreachable(url, source)),
                Ok(Ok(None)) => break,
                Ok(Ok(Some(bytes))) => bytes,
            };
            for event in lines.push(&chunk) {
                let terminal = event.event == "done" || event.event == "error";
                on_event(event);
                if terminal {
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    pub async fn remove_document(
        &self,
        library_id: &str,
        document_id: &str,
    ) -> Result<Removal> {
        self.delete(
            &format!("/libraries/{library_id}/documents/{document_id}"),
            REMOVE_TIMEOUT,
        )
        .await
    }

    pub async fn remove_version(&self, library_id: &str, version_id: &str) -> Result<Removal> {
        self.delete(
            &format!("/libraries/{library_id}/versions/{version_id}"),
            REMOVE_TIMEOUT,
        )
        .await
    }

    /// Promote a version the pipeline deliberately withheld.
    ///
    /// Costs nothing: the index it activates is the one already paid for. The
    /// other way out of a structural block is re-importing with `ignore_profile`,
    /// which says the inherited rules were wrong and pays for a full run.
    pub async fn activate_version(
        &self,
        library_id: &str,
        version_id: &str,
    ) -> Result<Activation> {
        self.post(
            &format!("/libraries/{library_id}/versions/{version_id}/activate"),
            REMOVE_TIMEOUT,
        )
        .await
    }

    pub async fn reindex(
        &self,
        library_id: &str,
        document_id: &str,
        options: &StageOptions,
    ) -> Result<StartedRun> {
        self.post_json(
            &format!("/libraries/{library_id}/documents/{document_id}/reindex"),
            options,
            START_TIMEOUT,
        )
        .await
    }

    /// Build a book for a version that was indexed before books existed.
    ///
    /// Nearly free, and the argument is `activate_version`'s arriving from
    /// another direction: the expensive work is already paid for and what is
    /// missing is a last, cheap step.
    pub async fn build_epub(
        &self,
        library_id: &str,
        document_id: &str,
        version_id: &str,
    ) -> Result<BookBuilt> {
        self.post(
            &format!(
                "/libraries/{library_id}/documents/{document_id}/versions/{version_id}/epub"
            ),
            START_TIMEOUT,
        )
        .await
    }

    /// Correct what the catalog calls a document.
    pub async fn update_document(
        &self,
        library_id: &str,
        document_id: &str,
        metadata: &DocumentMetadata,
    ) -> Result<DocumentMetadataResult> {
        let path = format!("/libraries/{library_id}/documents/{document_id}");
        let url = format!("{}{path}", self.base);
        self.send(self.http.patch(&url).json(metadata), &path, START_TIMEOUT)
            .await
    }

    /// One artifact, as bytes.
    ///
    /// Through `send_raw` like every other request, which is what gives it the
    /// bearer token and the tenant header without a second place for those to
    /// be attached — and what makes a 404 arrive as an `AppError` carrying the
    /// plane's own `detail.kind` rather than as an empty file on disk.
    pub async fn download_artifact(
        &self,
        workflow_id: &str,
        name: &str,
    ) -> Result<Vec<u8>> {
        let path = format!("/runs/{workflow_id}/artifacts/{name}");
        let url = format!("{}{path}", self.base);
        let response = self
            .send_raw(self.http.get(&url), &path, Some(DOWNLOAD_TIMEOUT))
            .await?;
        response
            .bytes()
            .await
            .map(|b| b.to_vec())
            .map_err(|source| unreachable(url, source))
    }

    pub async fn rebuild(&self, library_id: &str, document_id: &str) -> Result<StartedRun> {
        self.post(
            &format!("/libraries/{library_id}/documents/{document_id}/rebuild"),
            START_TIMEOUT,
        )
        .await
    }

    /// The rebuild gate, or `Ok(None)` while the artifacts are still being read.
    ///
    /// Modelled as absence for the same reason `gate` is: a 409 is the normal
    /// answer for the first moment of every run, not something the UI should
    /// have to recognise by status code.
    pub async fn rebuild_gate(&self, workflow_id: &str) -> Result<Option<RebuildReport>> {
        match self
            .get::<RebuildReport>(
                &format!("/runs/{workflow_id}/rebuild-gate"),
                HEALTH_TIMEOUT,
            )
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    // -- Explore -----------------------------------------------------------
    //
    // One method per thing a person can look at. The template each one runs is
    // a literal on the Python side; nothing here can name a different one.

    pub async fn outline(&self, version_id: &str) -> Result<Outline> {
        self.get(&format!("/versions/{version_id}/outline"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn section_chunks(&self, section_id: &str) -> Result<SectionChunks> {
        self.get(&format!("/sections/{section_id}/chunks"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn chunk_context(&self, chunk_id: &str) -> Result<ChunkContext> {
        self.get(&format!("/chunks/{chunk_id}/context"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn version_concepts(&self, version_id: &str) -> Result<VersionConcepts> {
        self.get(&format!("/versions/{version_id}/concepts"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn related_documents(&self, version_id: &str) -> Result<RelatedDocuments> {
        self.get(&format!("/versions/{version_id}/related"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn concept_claims(&self, concept_id: &str) -> Result<ConceptClaims> {
        self.get(&format!("/concepts/{concept_id}/claims"), EXPLORE_TIMEOUT)
            .await
    }

    pub async fn project_summary(&self) -> Result<ProjectSummary> {
        self.get("/project-summary", EXPLORE_TIMEOUT).await
    }

    pub async fn library_graph(
        &self,
        library_id: &str,
        confidence_floor: f64,
        min_documents: i64,
    ) -> Result<LibraryGraph> {
        let path = format!(
            "/libraries/{library_id}/graph?confidence_floor={confidence_floor}&min_documents={min_documents}"
        );
        self.get(&path, EXPLORE_TIMEOUT).await
    }

    // -- buckets -----------------------------------------------------------

    /// Register a bucket and catalogue it, in one awaited call. Free.
    /// Register a bucket and walk its listing.
    ///
    /// `library_id` empty derives `lib_s3_<hash>` from the bucket and prefix,
    /// which is the default and right for a corpus that stands on its own.
    /// Naming a library that exists puts the recordings on that shelf: one
    /// graph and one retrieval scope with whatever is already there, and no
    /// way to ask the recordings by themselves.
    pub async fn bucket_register(
        &self,
        source: &BucketSource,
        library_name: &str,
        library_id: &str,
    ) -> Result<BucketRegistered> {
        self.post_json(
            "/buckets",
            &serde_json::json!({
                "source": source,
                "library_name": library_name,
                "library_id": library_id,
            }),
            BUCKET_SYNC_TIMEOUT,
        )
        .await
    }

    pub async fn buckets(&self) -> Result<BucketList> {
        self.get("/buckets", CHANNEL_TIMEOUT).await
    }

    pub async fn bucket_detail(&self, bucket_id: &str, state: &str) -> Result<BucketDetail> {
        let query = if state.is_empty() || state == "all" {
            String::new()
        } else {
            format!("?state={state}")
        };
        self.get(&format!("/buckets/{bucket_id}{query}"), CHANNEL_TIMEOUT)
            .await
    }

    pub async fn bucket_sync(&self, bucket_id: &str) -> Result<BucketRegistered> {
        self.post_json(
            &format!("/buckets/{bucket_id}/sync"),
            &serde_json::json!({}),
            BUCKET_SYNC_TIMEOUT,
        )
        .await
    }

    /// One `audio` run per key, each parked at its own gate. Free.
    pub async fn bucket_probe(
        &self,
        bucket_id: &str,
        keys: &[String],
        options: &StageOptions,
        reindex: bool,
        transcriber: &str,
    ) -> Result<BucketProbeResult> {
        self.post_json(
            &format!("/buckets/{bucket_id}/probe"),
            &bucket_probe_body(keys, options, reindex, transcriber),
            BUCKET_PROBE_TIMEOUT,
        )
        .await
    }

    /// Hand over a transcript this machine made.
    ///
    /// The counterpart of `upload_audio`, and the same shape for the same
    /// reason: the app cannot write a run artifact and the worker cannot run
    /// whisper on this GPU, so the file goes to the plane the app is already
    /// signed in to. The fields ride beside it because the sidecar the worker
    /// archives records which engine and which model made it — a transcript
    /// with no model named is one nobody can reproduce.
    pub async fn upload_transcript(
        &self,
        workflow_id: &str,
        path: &std::path::Path,
        engine: &str,
        model: &str,
        language: &str,
    ) -> Result<TranscriptUploaded> {
        let bytes = std::fs::read(path).map_err(|e| AppError::io(path.display(), e))?;
        let part = reqwest::multipart::Part::bytes(bytes)
            .file_name("transcript.json")
            .mime_str("application/json")
            .map_err(|e| AppError::Config(e.to_string()))?;
        let form = reqwest::multipart::Form::new()
            .part("file", part)
            .text("engine", engine.to_string())
            .text("model", model.to_string())
            .text("language", language.to_string());
        let path_for_error = format!("/runs/{workflow_id}/transcript");
        self.send(
            self.http
                .post(format!("{}{path_for_error}", self.base))
                .multipart(form),
            &path_for_error,
            AUDIO_UPLOAD_TIMEOUT,
        )
        .await
    }

    /// Give up on transcribing a run here and pay Amazon instead.
    pub async fn switch_transcriber(&self, workflow_id: &str) -> Result<TranscriberSwitched> {
        self.post_json(
            &format!("/runs/{workflow_id}/transcriber"),
            &serde_json::json!({ "engine": "transcribe" }),
            START_TIMEOUT,
        )
        .await
    }

    pub async fn bucket_forget(&self, bucket_id: &str) -> Result<BucketForgotten> {
        let path = format!("/buckets/{bucket_id}");
        let url = format!("{}{path}", self.base);
        self.send(self.http.delete(&url), &path, START_TIMEOUT).await
    }

    /// An audio run's gate: the same report a video publishes, on its own
    /// route. `Ok(None)` while the object is still being probed.
    pub async fn audio_gate(&self, workflow_id: &str) -> Result<Option<VideoGateReport>> {
        match self
            .get::<VideoGateReport>(
                &format!("/runs/{workflow_id}/audio-gate"),
                HEALTH_TIMEOUT,
            )
            .await
        {
            Ok(report) => Ok(Some(report)),
            Err(AppError::ControlStatus { status: 409, .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// A presigned link to the recording a document was indexed from, at a
    /// second. Minted on click and never stored.
    pub async fn media_link(
        &self,
        library_id: &str,
        document_id: &str,
        start_s: u32,
    ) -> Result<MediaLink> {
        self.get(
            &format!("/libraries/{library_id}/documents/{document_id}/media?t={start_s}"),
            START_TIMEOUT,
        )
        .await
    }

    // -- channels ----------------------------------------------------------
    //
    // Everything here is free except the last two, and those two are the only
    // reason the quote above them exists as a route of its own: pressing the
    // button is the decision, so the figure has to be on screen before the
    // request that starts the run rather than inside it.

    pub async fn channel_sync(
        &self,
        url: &str,
        limit: Option<u32>,
        full: bool,
    ) -> Result<ChannelSummary> {
        self.post_json(
            "/channels/sync",
            &channel_sync_body(url, limit, full),
            CHANNEL_SYNC_TIMEOUT,
        )
        .await
    }

    pub async fn channels(&self) -> Result<ChannelList> {
        self.get("/channels", CHANNEL_TIMEOUT).await
    }

    pub async fn channel_detail(&self, channel_id: &str) -> Result<ChannelDetail> {
        self.get(&format!("/channels/{channel_id}"), CHANNEL_TIMEOUT)
            .await
    }

    pub async fn channel_quote(
        &self,
        channel_id: &str,
        topic: &str,
        limit: u32,
        deep_limit: u32,
        video_ids: Option<&[String]>,
    ) -> Result<DiscoveryQuote> {
        self.post_json(
            &format!("/channels/{channel_id}/discovery-estimate"),
            &channel_query_body(topic, limit, deep_limit, video_ids),
            CHANNEL_TIMEOUT,
        )
        .await
    }

    pub async fn channel_discover(
        &self,
        channel_id: &str,
        topic: &str,
        limit: u32,
        deep_limit: u32,
        video_ids: Option<&[String]>,
    ) -> Result<StartedRun> {
        self.post_json(
            &format!("/channels/{channel_id}/discover"),
            &channel_query_body(topic, limit, deep_limit, video_ids),
            START_TIMEOUT,
        )
        .await
    }

    pub async fn channel_topics(
        &self,
        channel_id: &str,
        topic: &str,
        video_runs: &[String],
    ) -> Result<StartedRun> {
        self.post_json(
            &format!("/channels/{channel_id}/topics"),
            &serde_json::json!({ "topic": topic, "video_runs": video_runs }),
            START_TIMEOUT,
        )
        .await
    }

    pub async fn channel_ask(
        &self,
        channel_id: &str,
        topic: &str,
        effort: &str,
    ) -> Result<AskStarted> {
        self.post_json(
            &format!("/channels/{channel_id}/ask"),
            &serde_json::json!({ "topic": topic, "effort": effort }),
            ASK_TIMEOUT,
        )
        .await
    }

    pub async fn channel_ask_result(
        &self,
        channel_id: &str,
        question_id: &str,
    ) -> Result<SynthesisResult> {
        self.get(
            &format!("/channels/{channel_id}/ask/{question_id}"),
            ASK_POLL_TIMEOUT,
        )
        .await
    }

    pub async fn channel_reading(
        &self,
        channel_id: &str,
        workflow_id: &str,
    ) -> Result<ChannelReading> {
        self.get(
            &format!("/channels/{channel_id}/readings/{workflow_id}"),
            CHANNEL_TIMEOUT,
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    //! These types deserialize snake_case from Python and serialize camelCase to
    //! the webview, and nothing else checks that both directions still line up.
    //! A rename that only lands on one side fails silently: the field arrives as
    //! `undefined` in TypeScript, which renders as a blank rather than an error.

    use super::*;

    const CONTEXT: &str = r#"{
        "chunk_id": "chk_aaaaaaaaaaaaaaaaaaaaaaaa",
        "context": {
            "id": "chk_aaaaaaaaaaaaaaaaaaaaaaaa",
            "text": "La Palabra de Dios…",
            "kind": "cuerpo",
            "char_start": 267,
            "char_end": 445,
            "before_id": null,
            "before_text": null,
            "after_id": "chk_bbbbbbbbbbbbbbbbbbbbbbbb",
            "after_text": "Las Escrituras…"
        },
        "citation": {
            "id": "cit_cccccccccccccccccccccccc",
            "chunk_id": "chk_aaaaaaaaaaaaaaaaaaaaaaaa",
            "locator": "Catecismo Menor · 1.1 De la regla dada por Dios · [267:445]",
            "page": null,
            "section_title": "1.1 De la regla dada por Dios"
        }
    }"#;

    #[test]
    fn a_channel_sync_omits_the_limit_when_there_is_none_and_always_says_full() {
        // Absent means "the whole playlist" on the Python side; `full` is the
        // one switch that marks a video unavailable, so it never rides on a
        // default.
        let all = channel_sync_body("@canal", None, false);
        assert_eq!(all["url"], "@canal");
        assert_eq!(all["full"], false);
        assert!(all.get("limit").is_none());

        let some = channel_sync_body("@canal", Some(100), true);
        assert_eq!(some["limit"], 100);
        assert_eq!(some["full"], true);
    }

    #[test]
    fn a_video_row_from_before_the_availability_flag_reads_as_available() {
        // Every catalogue written under the 500-video cap has no such key,
        // and a missing field that deserialised as `false` would grey out a
        // whole channel with nothing failing anywhere.
        let row: ChannelVideoRow = serde_json::from_str(
            r#"{"video_id": "aaaaaaaaaaa", "title": "t", "description": "", "published_at": "",
                "duration_s": 60, "live_state": "none", "thumbnail": "", "url": "",
                "document_id": null, "active_version_id": null}"#,
        )
        .unwrap();
        assert!(row.available);
        let out = serde_json::to_value(&row).unwrap();
        assert_eq!(out["available"], true);
    }

    #[test]
    fn a_channel_query_carries_the_filter_only_when_there_is_one() {
        // The Python side reads an absent `video_ids` as the whole catalogue.
        // An empty list is *not* absent: it is a filter that matched nothing,
        // and the quote for it has to say "0 vídeos" rather than quote the
        // whole channel.
        let none = channel_query_body("justicia", 100, 10, None);
        assert_eq!(none["topic"], "justicia");
        assert_eq!(none["limit"], 100);
        assert_eq!(none["deep_limit"], 10);
        assert!(none.get("video_ids").is_none());

        let ids = vec!["aaaaaaaaaaa".to_string(), "bbbbbbbbbbb".to_string()];
        let some = channel_query_body("justicia", 100, 10, Some(&ids));
        assert_eq!(some["video_ids"], serde_json::json!(["aaaaaaaaaaa", "bbbbbbbbbbb"]));

        let empty: Vec<String> = vec![];
        let filtered_to_nothing = channel_query_body("justicia", 100, 10, Some(&empty));
        assert_eq!(filtered_to_nothing["video_ids"], serde_json::json!([]));
    }

    #[test]
    fn chunk_context_survives_the_round_trip_to_the_webview() {
        let parsed: ChunkContext = serde_json::from_str(CONTEXT).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert_eq!(out["chunkId"], "chk_aaaaaaaaaaaaaaaaaaaaaaaa");
        assert_eq!(out["context"]["charStart"], 267);
        assert_eq!(out["context"]["afterId"], "chk_bbbbbbbbbbbbbbbbbbbbbbbb");
        assert_eq!(out["citation"]["sectionTitle"], "1.1 De la regla dada por Dios");
        // Renamed on the serialize side only, so the Python field name still
        // deserializes and the webview still gets idiomatic JavaScript.
        assert!(out["context"].get("char_start").is_none());
    }

    #[test]
    fn a_first_chunk_keeps_its_absent_neighbour_as_null() {
        // Every document has one, and a missing key would read as "not loaded
        // yet" in the UI rather than "there is nothing before this".
        let parsed: ChunkContext = serde_json::from_str(CONTEXT).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert!(out["context"]["beforeId"].is_null());
        assert!(out["context"]["beforeText"].is_null());
    }

    #[test]
    fn a_chunk_without_a_citation_deserializes_rather_than_failing() {
        // A citation the user cannot open is not a citation, so the API returns
        // null instead of inventing a locator — and this must not be an error.
        let json = r#"{"chunk_id": "chk_a", "context": null, "citation": null}"#;
        let parsed: ChunkContext = serde_json::from_str(json).unwrap();
        assert!(parsed.citation.is_none());
        assert!(parsed.context.is_none());
    }

    #[test]
    fn a_concept_carries_the_confidence_that_qualifies_it() {
        let json = r#"{
            "version_id": "ver_aaaaaaaaaaaaaaaaaaaaaaaa",
            "semantic": true,
            "confidence_floor": 0.6,
            "concepts": [
                {"id": "con_a", "name": "felicidad", "type": "Estado",
                 "mentions": 2, "confidence": 0.95}
            ]
        }"#;
        let parsed: VersionConcepts = serde_json::from_str(json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        // `semantic` is not decoration: the screen renders a model's proposal
        // differently from the document's own table of contents, and this flag
        // is the only thing telling it which it has.
        assert_eq!(out["semantic"], true);
        assert_eq!(out["confidenceFloor"], 0.6);
        // `type` is a Rust keyword, so it travels as `conceptType` rather than
        // forcing every caller to write `concept["type"]`.
        assert_eq!(out["concepts"][0]["conceptType"], "Estado");
        assert_eq!(out["concepts"][0]["confidence"], 0.95);
    }

    #[test]
    fn a_concept_with_no_type_is_still_a_concept() {
        let json = r#"{"version_id": "v", "semantic": true, "confidence_floor": 0.6,
                       "concepts": [{"id": "con_a", "name": "x", "type": null,
                                     "mentions": 1, "confidence": 0.7}]}"#;
        let parsed: VersionConcepts = serde_json::from_str(json).unwrap();
        assert!(parsed.concepts[0].r#type.is_none());
    }

    // --- one version's statistics -----------------------------------------
    //
    // Both directions matter here more than anywhere else in this file: the
    // route's whole contract is that a leg which could not answer carries *no
    // figures*, and a `#[serde(default)]` that quietly supplies a zero would
    // turn "the graph is stopped" into "this version has no concepts" at
    // exactly the layer nothing else checks.

    /// Trimmed from a real response for `ver_0ebf4f0b50a27202db3fcca6`.
    const STATS: &str = r#"{
        "library_id": "lib_pruebas",
        "document_id": "doc_0b797166a685ce47ff7dc859",
        "version_id": "ver_0ebf4f0b50a27202db3fcca6",
        "catalog": {"available": true, "detail": "",
            "content_sha256": "0ebf4f0b", "byte_size": 3177305, "page_count": null,
            "state": "indexed", "active": true, "created_at": "2026-08-31T18:16:19Z",
            "activated_at": null, "failed_reason": null, "runs": 4,
            "rebuild_run_id": "ingest-1788215710194-a367fb3a",
            "profile_warnings": [{"id": 41, "profile_id": "01-retodedios",
                "collides_with": "libro", "similarity": 0.0, "detail": "…",
                "comparable": false}]},
        "structure": {"available": true, "detail": "",
            "source_run": "ingest-1788215710194-a367fb3a",
            "scope": {"tenant_id": "tnt_1", "version_id": "ver_x"},
            "graph": {"available": true, "detail": "",
                "chunks": 600, "sections": 38, "citations": 600, "claims": 0,
                "kinds": {"cuerpo": 502, "preguntas": 98},
                "section_levels": {"1": 38}},
            "qdrant": {"available": true, "detail": "", "points": 600},
            "artifacts": {"available": true, "detail": "",
                "stream": "extracted", "stream_verifies_completely": true,
                "streams_considered": {
                    "extracted": {"bytes": 484856, "spans_verified": 600, "spans_mismatched": 0},
                    "raw": {"bytes": 487032, "spans_verified": 8, "spans_mismatched": 592}},
                "spans": {"chunks": 600, "spans_verified": 600, "spans_mismatched": 0,
                          "bytes": 484856},
                "sequence": {"indices": 600, "contiguous": true, "missing_indices": [],
                             "duplicate_indices": [], "bytes_covered": 482321,
                             "bytes_total": 484856, "coverage": 0.9948}},
            "counts_agree": true},
        "semantics": {"available": true, "detail": "",
            "source_run": "reindex-1", "extractor_model": "gemini-3.6-flash",
            "in_store": {"claims": 3055, "with_a_quote": 3044,
                "by_status": {"afirma": 2635, "atribuido": 373, "niega": 47},
                "concepts": 2519},
            "diff": {"available": true, "detail": "",
                "claims": {"produced": 3055, "in_store": 3055, "converged": 3055,
                           "left_behind": 0, "missing": 0, "stale_share": 0.0},
                "concepts": {"names_extracted": 2634, "produced": 2517,
                             "in_store": 2519, "converged": 2517, "left_behind": 2,
                             "missing": 0, "stale_share": 0.0008},
                "mentions": {"produced": 3497, "in_store": 3497, "converged": 3497,
                             "left_behind": 0, "missing": 0, "stale_share": 0.0},
                "stale_claim_quotes": {"with_a_quote": 0, "quote_no_longer_locates": 0}}},
        "retrieval": {"available": true, "detail": "",
            "source_run": "ingest-1", "scores": {"recall_at_1": 0.5375,
                "recall_at_5": 0.825, "mrr_at_10": 0.6742,
                "recall_at_5_dense_only": 0.8625, "noise_floor": 0.5142,
                "chunks": 600, "eval_questions": 80, "margin": 0.041,
                "leakage": "no sign", "misses": 14},
            "floor": {"min_score": 0.6, "noise_floor": 0.5142, "headroom": 0.0858,
                      "honest": true}},
        "ledger": {"available": true, "detail": "",
            "by_stage": {"embedding": {"usd": 0.035484,
                "runs": ["ingest-a", "ingest-b"], "unpriced_entries": 0}},
            "total_usd": 14.927211,
            "charged_in_more_than_one_run": ["embedding", "semantics"],
            "usd_by_run_state": {"failed": 9.992302, "succeeded": 4.909946}}
    }"#;

    #[test]
    fn version_statistics_survive_the_round_trip_to_the_webview() {
        let parsed: VersionStatistics = serde_json::from_str(STATS).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert_eq!(out["libraryId"], "lib_pruebas");
        assert_eq!(out["structure"]["graph"]["sectionLevels"]["1"], 38);
        assert_eq!(out["structure"]["artifacts"]["streamVerifiesCompletely"], true);
        assert_eq!(out["semantics"]["inStore"]["withAQuote"], 3044);
        assert_eq!(out["ledger"]["usdByRunState"]["failed"], 9.992302);
        // The snake_case spellings must be gone, or the webview reads both and
        // one of them silently wins.
        assert!(out["structure"]["counts_agree"].is_null());
        assert!(out["semantics"]["in_store"].is_null());
        assert_eq!(out["structure"]["countsAgree"], true);
    }

    #[test]
    fn the_map_keys_are_never_renamed_with_the_fields() {
        // Chunk kinds and claim statuses are Qdrant payload values and graph
        // properties. `rename_all` renames struct fields and not map keys, and
        // this is what says so — a camelCased `sinEstado` would match nothing
        // the UI has a label for, and renaming them in the store would break
        // every existing collection.
        let parsed: VersionStatistics = serde_json::from_str(STATS).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["structure"]["graph"]["kinds"]["preguntas"], 98);
        assert_eq!(out["semantics"]["inStore"]["byStatus"]["atribuido"], 373);
    }

    #[test]
    fn a_leg_that_could_not_answer_carries_no_figures() {
        // The property the whole route is built on, checked at the hop that
        // would otherwise invent a default. A stopped Memgraph is not a version
        // with no concepts, and a run nobody measured is not a recall of zero.
        let json = r#"{
            "library_id": "l", "document_id": "d", "version_id": "v",
            "catalog": {"available": false, "detail": "sin fila"},
            "structure": {"available": true, "detail": "",
                "graph": {"available": false,
                          "detail": "bolt://127.0.0.1:7789: refused"},
                "qdrant": {"available": false, "detail": "6433: refused"},
                "artifacts": {"available": false,
                              "detail": "ningún run conserva chunks.jsonl"},
                "counts_agree": null},
            "semantics": {"available": false, "detail": "refused"},
            "retrieval": {"available": false,
                          "detail": "ningún run midió esta versión"},
            "ledger": {"available": true, "detail": ""}
        }"#;
        let parsed: VersionStatistics = serde_json::from_str(json).unwrap();

        assert!(parsed.structure.graph.chunks.is_none());
        assert!(parsed.structure.qdrant.points.is_none());
        assert!(parsed.structure.artifacts.spans.is_none());
        assert!(parsed.semantics.in_store.is_none());
        assert!(parsed.retrieval.scores.is_none());
        assert!(parsed.catalog.page_count.is_none());
        // "Could not compare" and "they disagree" are different claims.
        assert!(parsed.structure.counts_agree.is_none());
        // An empty ledger is a real answer: this version spent nothing.
        assert!(parsed.ledger.available && parsed.ledger.by_stage.is_empty());
        assert_eq!(parsed.structure.graph.detail, "bolt://127.0.0.1:7789: refused");
    }

    #[test]
    fn a_page_count_nothing_wrote_stays_absent_rather_than_zero() {
        // `register_version` runs before extraction, so the column is never
        // filled. A `u32` with a serde default would render "0 pages" on every
        // document in the catalog.
        let parsed: VersionStatistics = serde_json::from_str(STATS).unwrap();
        assert!(parsed.catalog.page_count.is_none());
        assert_eq!(parsed.catalog.byte_size, Some(3177305));
    }

    #[test]
    fn a_related_document_names_a_document_not_only_a_version() {
        let json = r#"{
            "version_id": "ver_a", "semantic": true, "confidence_floor": 0.6,
            "documents": [{"id": "ver_b", "title": "Otro tratado",
                           "document_id": "doc_b", "shared_concepts": 3}]
        }"#;
        let parsed: RelatedDocuments = serde_json::from_str(json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        // A version id is not something a person can navigate to.
        assert_eq!(out["documents"][0]["documentId"], "doc_b");
        assert_eq!(out["documents"][0]["sharedConcepts"], 3);
    }

    #[test]
    fn a_claim_keeps_the_chunk_it_can_be_checked_against() {
        let json = r#"{
            "concept_id": "con_a", "semantic": true, "confidence_floor": 0.6,
            "claims": [{"id": "clm_a", "text": "…", "confidence": 0.88,
                        "source_chunk_id": "chk_a"}]
        }"#;
        let parsed: ConceptClaims = serde_json::from_str(json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        // A relation nobody can verify is worse than no relation: it still looks
        // like evidence. Losing this field in the rename would produce exactly
        // that.
        assert_eq!(out["claims"][0]["sourceChunkId"], "chk_a");
    }

    #[test]
    fn an_outline_path_stays_a_dotted_string() {
        // The screen reads nesting depth from the number of segments. Parsing it
        // as a number would collapse "1.1" and "1.10" onto the same value.
        let json = r#"{"version_id": "ver_a",
                       "sections": [{"id": "sec_a", "title": "Libro I",
                                     "path": "1.1", "level": 2}]}"#;
        let parsed: Outline = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.sections[0].path, "1.1");
        assert_eq!(parsed.sections[0].path.split('.').count(), 2);
    }
}

#[cfg(test)]
mod request_direction {
    //! Request types travel webview → Rust → Python, which is the opposite
    //! direction from every response in this file, so they rename on the
    //! *deserialize* side instead of the serialize side.
    //!
    //! Getting it backwards is not a subtle failure and it is not a loud one
    //! either: serde rejects the whole payload with "missing field", the command
    //! returns an error, and the screen shows it as though the backend were
    //! down. Both `IngestRequest` and `StageOptions` were wrong this way, so
    //! neither starting an ingest nor approving one had ever worked from the
    //! window — and nothing caught it because nobody had opened the window.

    use super::*;

    #[test]
    fn an_ingest_request_accepts_what_the_screen_actually_sends() {
        let from_webview = r#"{
            "libraryId": "lib_1",
            "sourcePath": "/home/x/libro.pdf",
            "sourceKey": "libros/libro.pdf",
            "title": ""
        }"#;
        let parsed: IngestRequest = serde_json::from_str(from_webview).unwrap();
        assert_eq!(parsed.library_id, "lib_1");
        assert_eq!(parsed.source_key, "libros/libro.pdf");

        // …and hands Python the field names its dataclass declares.
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["library_id"], "lib_1");
        assert!(out.get("libraryId").is_none());
    }

    #[test]
    fn a_video_request_travels_webview_to_python_like_an_ingest_request() {
        let from_webview = r#"{
            "libraryId": "lib_videos",
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "autoApprove": false,
            "libraryName": ""
        }"#;
        let parsed: VideoRequest = serde_json::from_str(from_webview).unwrap();
        assert_eq!(parsed.library_id, "lib_videos");
        assert!(parsed.url.ends_with("dQw4w9WgXcQ"));

        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["library_id"], "lib_videos");
        assert!(out.get("libraryId").is_none());
        assert_eq!(out["auto_approve"], false);
        // Absent, not null, when this machine did not resolve the video — which
        // is every local-mode run. `VideoRequest.resolved` defaults to `None`
        // in Python, so either would decode; absent is what says "the webview
        // sent nothing" rather than "something decided there was nothing".
        assert!(out.get("resolved").is_none());
        assert!(out.get("audio_path").is_none());
    }

    #[test]
    fn a_resolved_video_reaches_python_under_pythons_own_spelling() {
        // The one type in this file that must **not** rename on serialize. It
        // is nested inside a request, so it travels Rust → Python only, and
        // `duration_s` arriving as `durationS` would be dropped by the
        // dataclass converter without failing — the exact shape of the
        // `style_effort` defect, which existed and was invisible because
        // `asdict` serialises declared fields and nothing else.
        let mut request: VideoRequest = serde_json::from_str(
            r#"{"libraryId": "lib_videos", "url": "https://youtu.be/jNQXAC9IVRw"}"#,
        )
        .unwrap();
        request.resolved = Some(crate::ytdlp::VideoInfo {
            video_id: "jNQXAC9IVRw".into(),
            title: "Me at the zoo".into(),
            channel: "jawed".into(),
            duration_s: 19,
            upload_date: "20050424".into(),
            format_id: "395+251".into(),
            tracks: vec![CaptionTrack {
                language: "en".into(),
                kind: "manual".into(),
                ext: "vtt".into(),
                name: String::new(),
            }],
            chosen: Some(CaptionTrack {
                language: "en".into(),
                kind: "manual".into(),
                ext: "vtt".into(),
                name: String::new(),
            }),
            caption_url: "https://www.youtube.com/api/timedtext?v=jNQXAC9IVRw".into(),
        });
        request.audio_path = "/workspace/tenants/t/inbox/audio.m4a".into();

        let out = serde_json::to_value(&request).unwrap();
        let resolved = &out["resolved"];
        assert_eq!(resolved["video_id"], "jNQXAC9IVRw");
        assert_eq!(resolved["duration_s"], 19);
        assert_eq!(resolved["upload_date"], "20050424");
        assert_eq!(resolved["format_id"], "395+251");
        assert_eq!(resolved["caption_url"], "https://www.youtube.com/api/timedtext?v=jNQXAC9IVRw");
        assert!(resolved.get("videoId").is_none());
        assert!(resolved.get("durationS").is_none());
        // `CaptionTrack` renames on serialize and is unaffected only because
        // every one of its fields is a single word. A field added to it would
        // have to be too, or this nesting starts lying.
        assert_eq!(resolved["chosen"]["language"], "en");
        assert_eq!(resolved["chosen"]["kind"], "manual");
        assert_eq!(out["audio_path"], "/workspace/tenants/t/inbox/audio.m4a");
    }

    #[test]
    fn a_video_gate_travels_python_to_webview_the_other_way() {
        // The opposite direction, and therefore the opposite rename. A report
        // that renamed on deserialize would arrive with every field undefined
        // in TypeScript and the gate would render a video with no title, no
        // duration and no transcript source — without failing anywhere.
        let from_python = r#"{
            "run_id": "video-1",
            "document_id": "doc_v",
            "version_id": "ver_v",
            "probe": {
                "video_id": "dQw4w9WgXcQ",
                "canonical_url": "https://youtu.be/dQw4w9WgXcQ",
                "source_key": "youtube/dQw4w9WgXcQ",
                "title": "Charla",
                "channel": "Canal",
                "duration_s": 1800,
                "upload_date": "20260101",
                "tracks": [],
                "chosen": null,
                "warnings": []
            },
            "estimate": {
                "stages": [], "total_usd": null, "price_source": "x",
                "unpriced_stages": [], "total_usd_high": null
            },
            "preview": null,
            "transcript": null,
            "warnings": [],
            "recommended": {
                "correct": true, "embed": true, "extract_semantics": false,
                "generate_evalset": false, "learn_profile": false,
                "ignore_profile": false, "review_correction": false,
                "tune": false, "condense_descriptions": false
            }
        }"#;
        let parsed: VideoGateReport = serde_json::from_str(from_python).unwrap();
        assert_eq!(parsed.probe.duration_s, 1800);
        // `null` and not a fabricated preview: without captions there is no
        // text to preview until the transcription is paid for.
        assert!(parsed.preview.is_none());
        assert!(parsed.recommended.as_ref().unwrap().correct);

        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["probe"]["durationS"], 1800);
        assert_eq!(out["probe"]["canonicalUrl"], "https://youtu.be/dQw4w9WgXcQ");
        assert!(out["probe"].get("duration_s").is_none());
        assert_eq!(out["recommended"]["correct"], true);
        // The seven switches a video run has no stage for are dropped rather
        // than offered.
        assert!(out["recommended"].get("extractSemantics").is_none());
    }

    #[test]
    fn stage_options_accept_what_the_gate_actually_sends() {
        let from_webview = r#"{
            "correct": true, "embed": true, "extractSemantics": false,
            "generateEvalset": false, "learnProfile": true,
            "ignoreProfile": false, "reviewCorrection": true
        }"#;
        let parsed: StageOptions = serde_json::from_str(from_webview).unwrap();
        assert!(!parsed.extract_semantics);
        assert!(parsed.learn_profile);
        assert!(parsed.review_correction);

        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["extract_semantics"], false);
        assert_eq!(out["learn_profile"], true);
    }

    #[test]
    fn a_book_switch_survives_both_renames_and_its_absence_is_off() {
        // Two properties in one, because they fail differently. A webview that
        // *sends* the switch must have it reach Python under Python's own
        // spelling — `buildEpub` arriving as-is would be dropped by the
        // dataclass converter with no error anywhere, which is the recorded
        // `style_effort` shape. And an older webview that sends nothing must
        // still decode, which is what `#[serde(default)]` is for: without it
        // the whole approval fails and the gate cannot be answered at all.
        let ticked: StageOptions = serde_json::from_str(
            r#"{"correct": true, "embed": true, "extractSemantics": true,
                "generateEvalset": false, "learnProfile": true,
                "ignoreProfile": false, "reviewCorrection": false,
                "buildEpub": true}"#,
        )
        .unwrap();
        assert!(ticked.build_epub);
        assert_eq!(serde_json::to_value(&ticked).unwrap()["build_epub"], true);

        let older: StageOptions = serde_json::from_str(
            r#"{"correct": true, "embed": true, "extractSemantics": true,
                "generateEvalset": false, "learnProfile": true,
                "ignoreProfile": false, "reviewCorrection": false}"#,
        )
        .unwrap();
        assert!(!older.build_epub, "an absent switch is off, not a failure");
    }

    #[test]
    fn a_version_row_survives_a_plane_that_has_never_heard_of_a_book() {
        // Every field the feature added is defaulted, so the *detail* payload
        // still decodes against a plane that predates it. Without that a user
        // whose desktop app updated before their cloud plane did would lose the
        // Library screen entirely rather than lose one button on it.
        let row: VersionRow = serde_json::from_str(
            r#"{"id": "ver_1", "content_sha256": "a", "byte_size": 10,
                "page_count": null, "state": "indexed", "active": true,
                "created_at": null, "rebuild_run_id": null}"#,
        )
        .unwrap();
        assert!(row.epub_run_id.is_none());
        assert_eq!(serde_json::to_value(&row).unwrap()["epubRunId"], serde_json::Value::Null);
    }

    #[test]
    fn a_metadata_edit_sends_only_the_field_somebody_changed() {
        // `None` means "leave this alone" on both planes, and it has to travel
        // as *absence* rather than as an explicit null: a null author is how a
        // person says the document has none, and the two must not be the same
        // request.
        let only_author: DocumentMetadata =
            serde_json::from_str(r#"{"author": "Darío Silva-Silva"}"#).unwrap();
        let out = serde_json::to_value(&only_author).unwrap();
        assert!(out.get("title").is_none());
        assert_eq!(out["author"], "Darío Silva-Silva");
    }

    #[test]
    fn an_approval_carries_its_switches_through_both_renames() {
        let from_webview = r#"{
            "approved": true,
            "options": {"correct": false, "embed": true, "extractSemantics": true,
                        "generateEvalset": false, "learnProfile": false,
                        "ignoreProfile": true, "reviewCorrection": false},
            "reason": ""
        }"#;
        let parsed: Approval = serde_json::from_str(from_webview).unwrap();
        assert!(!parsed.options.correct);
        assert!(parsed.options.ignore_profile);

        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["options"]["ignore_profile"], true);
        assert_eq!(out["options"]["correct"], false);
    }

    #[test]
    fn a_question_stays_snake_case_on_both_sides() {
        // The exception, pinned rather than left to be rediscovered: this is the
        // one request type the TypeScript side spells the Python way.
        let from_webview =
            r#"{"library_id": "lib_1", "text": "¿qué?", "effort": "thorough"}"#;
        let parsed: Question = serde_json::from_str(from_webview).unwrap();
        assert_eq!(parsed.library_id, "lib_1");
        assert_eq!(parsed.effort, "thorough");
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["library_id"], "lib_1");
        assert_eq!(out["effort"], "thorough");
    }

    #[test]
    fn a_question_omitting_the_effort_still_asks_at_the_default() {
        // A webview older than this shell sends no `effort`. Without the serde
        // default that is not a question asked at the default level, it is a
        // body serde refuses outright — so the whole Ask screen would stop
        // working rather than degrade.
        let parsed: Question =
            serde_json::from_str(r#"{"library_id": "lib_1", "text": "¿qué?"}"#).unwrap();
        assert_eq!(parsed.effort, "standard");
        assert_eq!(parsed.top_k, None);
    }

    #[test]
    fn a_question_that_names_no_top_k_omits_the_key_rather_than_nulling_it() {
        // `top_k` absent means "the level decides", and that decision belongs to
        // one place — the worker's effort table. Sending `null` would make both
        // control planes answer a question they should never be asked: what does
        // an explicitly-null top_k mean?
        let parsed: Question =
            serde_json::from_str(r#"{"library_id": "lib_1", "text": "¿qué?"}"#).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert!(out.get("top_k").is_none(), "got {out}");
    }

    #[test]
    fn a_question_omits_the_narrowings_it_does_not_set_and_carries_the_ones_it_does() {
        // Empty is the dataclass's own "no filter", and the key is left out so
        // both planes see what a curl that never mentioned it would send. A
        // set one travels verbatim: the normalising — `Rom 8:28` to
        // `Romanos 8:28` — is the worker's, and a copy here would drift.
        let plain: Question =
            serde_json::from_str(r#"{"library_id": "lib_1", "text": "¿qué?"}"#).unwrap();
        let out = serde_json::to_value(&plain).unwrap();
        for key in ["recorded_from", "recorded_to", "scripture", "source_name"] {
            assert!(out.get(key).is_none(), "{key} leaked: {out}");
        }
        let narrowed: Question = serde_json::from_str(
            r#"{"library_id": "lib_1", "text": "¿qué?", "recorded_from": "1993-01-01",
                "scripture": "Rom 8:28", "source_name": "iVoox"}"#,
        )
        .unwrap();
        let out = serde_json::to_value(&narrowed).unwrap();
        assert_eq!(out["recorded_from"], "1993-01-01");
        assert!(out.get("recorded_to").is_none());
        assert_eq!(out["scripture"], "Rom 8:28");
        assert_eq!(out["source_name"], "iVoox");
    }

    #[test]
    fn learning_is_on_and_ignoring_is_off_by_default() {
        // Reuse is free, so a family that already has a profile pays nothing;
        // learning one for a family that does not is the default because the
        // alternative is every document chunked with generic rules forever.
        let d = StageOptions::default();
        assert!(d.learn_profile);
        assert!(!d.ignore_profile);
    }
}

#[cfg(test)]
mod gate_report {
    //! The gate report gained a `profile` block, and a struct that omits a field
    //! does not fail — serde ignores unknown keys. It arrives as `undefined` in
    //! TypeScript, and the screen would then report "no profile exists for this
    //! family" for every document, including ones that had one and were about
    //! to reuse it for free.

    use super::*;

    const REPORT: &str = r#"{
        "run_id": "run_1", "document_id": "doc_1", "version_id": "ver_1",
        "preview": {"text": {"kind":"raw_text","path":"p","sha256":"a","bytes":1},
                    "chunks": {"kind":"preview_chunks","path":"p","sha256":"a","bytes":1},
                    "chunk_count": 2, "kinds": [], "characters": 100,
                    "chunks_are_final": false, "warnings": []},
        "estimate": {"stages": [], "total_usd": null, "price_source": "…",
                     "unpriced_stages": []},
        "profile_warnings": [],
        "profile": {
            "fingerprint": "abc123", "source": "reused", "slug": "calvino-abc12345",
            "learned_from": "libros/institucion.txt", "revisions": 3,
            "rules": {"header_patterns": [], "heading_l1_max": 44,
                      "heading_l2_max": 120, "heading_l1_pattern": "^LIBRO\\s+\\w+$",
                      "heading_l2_pattern": null, "question_pattern": null,
                      "footnote_pattern": null},
            "warnings": [], "adopted": [], "spend": null
        }
    }"#;

    #[test]
    fn the_profile_block_reaches_the_webview() {
        let parsed: GateReport = serde_json::from_str(REPORT).unwrap();
        let profile = parsed.profile.as_ref().unwrap();
        assert_eq!(profile.source, "reused");
        assert_eq!(profile.revisions, 3);

        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["profile"]["learnedFrom"], "libros/institucion.txt");
        assert_eq!(out["profile"]["rules"]["headingL1Pattern"], r"^LIBRO\s+\w+$");
    }

    #[test]
    fn a_report_from_a_run_with_no_profile_still_parses() {
        // Structured sources never learn one, and an older run has no such key
        // at all. Neither may fail the gate.
        let without = REPORT.replace(r#""profile": {"#, r#""unused": {"#);
        let parsed: GateReport = serde_json::from_str(&without).unwrap();
        assert!(parsed.profile.is_none());
    }

    // -- the Library's three verbs -----------------------------------------

    const DETAIL: &str = r#"{
        "id": "doc_1", "library_id": "lib_1", "title": "Institución",
        "author": "Juan Calvino", "format": "pdf",
        "source_key": "libros/calvino.pdf",
        "source_path": "/workspace/inbox/libros/calvino.pdf",
        "present": true, "tags": [], "active_version_id": "ver_1",
        "can_reindex": true, "can_rebuild": true,
        "versions": [{
            "id": "ver_1", "content_sha256": "aa", "byte_size": 100,
            "page_count": 12, "state": "indexed", "active": true,
            "created_at": "2026-08-20T00:00:00Z",
            "rebuild_run_id": "ingest-old", "also_held_by": ["doc_2"]
        }]
    }"#;

    #[test]
    fn document_detail_survives_the_round_trip_to_the_webview() {
        let parsed: DocumentDetail = serde_json::from_str(DETAIL).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert_eq!(out["sourceKey"], "libros/calvino.pdf");
        assert_eq!(out["canReindex"], true);
        assert_eq!(out["canRebuild"], true);
        assert_eq!(out["versions"][0]["rebuildRunId"], "ingest-old");
        assert_eq!(out["versions"][0]["alsoHeldBy"][0], "doc_2");
        assert!(out.get("can_reindex").is_none());
    }

    #[test]
    fn a_document_with_no_recorded_path_cannot_be_reindexed_and_says_so() {
        // Imported before the catalog recorded a source path. The flag is
        // computed server-side; the UI must not have to infer it from a null.
        let json = DETAIL
            .replace("\"/workspace/inbox/libros/calvino.pdf\"", "null")
            .replace("\"can_reindex\": true", "\"can_reindex\": false");
        let parsed: DocumentDetail = serde_json::from_str(&json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert!(out["sourcePath"].is_null());
        assert_eq!(out["canReindex"], false);
    }

    #[test]
    fn a_removal_reports_what_it_kept_as_well_as_what_it_destroyed() {
        let json = r#"{
            "document_id": "doc_1",
            "versions_removed": ["ver_1"], "versions_kept": [],
            "qdrant_points": 412, "qdrant_repointed": 0,
            "graph": {"chunks": 412, "concepts_collected": 3},
            "catalog": {"documents": 1, "versions": 1},
            "kept": {"runs": "conservados", "source_file": "intacto"}
        }"#;
        let parsed: Removal = serde_json::from_str(json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert_eq!(out["qdrantPoints"], 412);
        assert_eq!(out["versionsRemoved"][0], "ver_1");
        // Map *keys* are data, not field names: `rename_all` does not touch
        // them, and the UI reads them as Python spelled them.
        assert_eq!(out["graph"]["concepts_collected"], 3);
        assert_eq!(out["kept"]["source_file"], "intacto");
    }

    #[test]
    fn a_rebuild_report_says_whether_semantics_can_be_replayed() {
        // False for a document indexed before the semantics artifact existed.
        // Dropping this field would let the screen imply a full restore.
        let json = r#"{
            "document_id": "doc_1", "version_id": "ver_1",
            "title": "Institución", "chunk_count": 12, "characters": 4800,
            "source_run_id": "ingest-old", "semantics_available": false,
            "estimate": null
        }"#;
        let parsed: RebuildReport = serde_json::from_str(json).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();

        assert_eq!(out["semanticsAvailable"], false);
        assert_eq!(out["chunkCount"], 12);
        assert_eq!(out["sourceRunId"], "ingest-old");
        assert!(out["estimate"].is_null());
    }

    #[test]
    fn stage_options_still_serialize_snake_case_for_python() {
        // Reindex forwards these to the control API, which is the *opposite*
        // direction from a response: the rename is on deserialize only, so
        // serializing must keep Python's spelling.
        let out = serde_json::to_value(StageOptions::default()).unwrap();
        assert!(out.get("extract_semantics").is_some());
        assert!(out.get("extractSemantics").is_none());
    }

    /// A listener that accepts and then says nothing, so a request against it
    /// can only end in a read timeout. Local, so this needs no network.
    fn silent_server() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        std::thread::spawn(move || {
            // Hold the accepted sockets open. Dropping them would close the
            // connection and produce a transport error rather than a timeout.
            let mut held = Vec::new();
            while let Ok((socket, _)) = listener.accept() {
                held.push(socket);
            }
        });
        port
    }

    /// A port nothing is listening on, for the refused case.
    fn dead_port() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        drop(listener);
        port
    }

    #[tokio::test]
    async fn a_deadline_that_expires_is_not_reported_as_unreachable() {
        // The distinction this asserts is the whole point: "it may still be
        // starting" is a lie about an API that answered late, and it was
        // printed over a run the API had logged as 200 OK.
        let control = Control::local(silent_server());
        let error = control
            .send::<Health>(
                control.http.get(format!("{}/health", control.base)),
                "/health",
                Duration::from_millis(150),
            )
            .await
            .expect_err("a silent server cannot answer");

        assert_eq!(error.kind(), "control_timeout", "{error}");
    }

    #[tokio::test]
    async fn a_refused_connection_still_reads_as_unreachable() {
        let control = Control::local(dead_port());
        let error = control
            .send::<Health>(
                control.http.get(format!("{}/health", control.base)),
                "/health",
                Duration::from_secs(5),
            )
            .await
            .expect_err("nothing is listening");

        assert_eq!(error.kind(), "control_unreachable", "{error}");
    }

    /// A one-request server that records what it was sent.
    fn recording_server() -> (u16, std::sync::mpsc::Receiver<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            use std::io::{Read, Write};
            if let Ok((mut socket, _)) = listener.accept() {
                let mut buf = [0u8; 2048];
                let read = socket.read(&mut buf).unwrap_or(0);
                let _ = tx.send(String::from_utf8_lossy(&buf[..read]).to_string());
                let _ = socket.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
                );
            }
        });
        (port, rx)
    }

    #[tokio::test]
    async fn the_local_plane_is_sent_no_credentials() {
        // It has none and can have none: it is loopback-only and unauthenticated,
        // and a bearer sent there would be a token leaked to a process that
        // never asked for one.
        let (port, rx) = recording_server();
        let control = Control::local(port);
        let _: std::result::Result<serde_json::Value, _> = control
            .send(
                control.http.get(format!("{}/health", control.base)),
                "/health",
                Duration::from_secs(5),
            )
            .await;
        let request = rx.recv_timeout(Duration::from_secs(5)).expect("una petición");
        assert!(!request.to_lowercase().contains("authorization"), "{request}");
        assert!(!request.to_lowercase().contains("x-tenant-id"), "{request}");
    }

    #[tokio::test]
    async fn the_paid_plane_gets_the_bearer_and_the_tenant() {
        let (port, rx) = recording_server();
        let control = Control::cloud(
            format!("http://127.0.0.1:{port}"),
            Auth {
                token: "abc.def.ghi".into(),
                tenant: Some("tnt_x".into()),
            },
        );
        let _: std::result::Result<serde_json::Value, _> = control
            .send(
                control.http.get(format!("{}/health", control.base)),
                "/health",
                Duration::from_secs(5),
            )
            .await;
        let request = rx.recv_timeout(Duration::from_secs(5)).expect("una petición");
        assert!(request.contains("authorization: Bearer abc.def.ghi"), "{request}");
        assert!(request.contains("x-tenant-id: tnt_x"), "{request}");
    }

    #[tokio::test]
    async fn no_tenant_means_no_header_rather_than_an_empty_one() {
        // An account in exactly one organisation sends nothing and lets the
        // server use its sole membership. An empty header would be a value.
        let (port, rx) = recording_server();
        let control = Control::cloud(
            format!("http://127.0.0.1:{port}"),
            Auth { token: "t".into(), tenant: None },
        );
        let _: std::result::Result<serde_json::Value, _> = control
            .send(
                control.http.get(format!("{}/health", control.base)),
                "/health",
                Duration::from_secs(5),
            )
            .await;
        let request = rx.recv_timeout(Duration::from_secs(5)).expect("una petición");
        assert!(!request.to_lowercase().contains("x-tenant-id"), "{request}");
    }

    #[test]
    fn a_token_cannot_reach_a_log_through_debug() {
        // Every other type here derives `Debug`; this one is written by hand so
        // a `{:?}` on a `Control` cannot print a bearer.
        let auth = Auth { token: "muy-secreto".into(), tenant: Some("tnt_x".into()) };
        let rendered = format!("{auth:?}");
        assert!(!rendered.contains("muy-secreto"), "{rendered}");
        assert!(rendered.contains("tnt_x"), "{rendered}");
    }
}

#[cfg(test)]
mod estimate_range {
    //! The gate's cost went from one figure to two, and the older half of a
    //! half-upgraded install must not lose the approval screen over it.

    use super::*;

    #[test]
    fn an_api_without_the_range_still_deserialises() {
        // Exactly the payload the control API served before the range existed.
        // A missing field here would fail the whole `GateReport`, and the screen
        // would report it as though the backend were down — the same failure
        // mode `request_direction` above was written for.
        let older = r#"{
            "stages": [{
                "stage": "semantics", "model": "gemini-3.6-flash",
                "input_tokens": 15509, "output_tokens": 10144, "usd": 0.0993
            }],
            "total_usd": 0.0993,
            "price_source": "…",
            "unpriced_stages": []
        }"#;

        let parsed: Estimate = serde_json::from_str(older).unwrap();
        assert_eq!(parsed.total_usd_high, None);
        assert_eq!(parsed.stages[0].output_tokens_high, 0);
        assert_eq!(parsed.stages[0].usd_high, None);
    }

    #[test]
    fn the_range_reaches_the_webview_camel_cased() {
        // The webview reads `usdHigh`; the Python side writes `usd_high`. This
        // type renames on serialize only, so both ends stay idiomatic — and a
        // rename that silently did not apply would render an empty cost cell.
        let estimate = Estimate {
            stages: vec![StageEstimate {
                stage: "semantics".into(),
                model: "gemini-3.6-flash".into(),
                input_tokens: 15509,
                output_tokens: 10144,
                usd: Some(0.0993),
                output_tokens_high: 12882,
                usd_high: Some(0.1199),
            }],
            total_usd: Some(0.1003),
            total_usd_high: Some(0.1209),
            price_source: "…".into(),
            unpriced_stages: vec![],
        };

        let out = serde_json::to_value(&estimate).unwrap();
        assert_eq!(out["totalUsdHigh"], 0.1209);
        assert_eq!(out["stages"][0]["usdHigh"], 0.1199);
        assert_eq!(out["stages"][0]["outputTokensHigh"], 12882);
    }

    #[test]
    fn a_run_carries_its_spend_and_a_plane_without_it_still_parses() {
        // `usd_so_far` is `#[serde(default)]` for the reason `RunState`'s fields
        // are: a control plane that has not been upgraded must lose the figure,
        // not the whole call. Both directions are asserted because only one of
        // them is obvious.
        let with: RunSummary = serde_json::from_str(
            r#"{"id":"r1","workflow_id":"w1","kind":"index","state":"running",
                "stage":"semantics","started_at":"2026-08-31T02:50:14Z",
                "finished_at":null,"error_kind":null,"title":"Un libro",
                "library_id":"lib_teologia","usd_so_far":0.025}"#,
        )
        .unwrap();
        assert_eq!(with.usd_so_far, Some(0.025));

        let without: RunSummary = serde_json::from_str(
            r#"{"id":"r1","workflow_id":"w1","kind":"index","state":"running",
                "stage":"semantics","started_at":"2026-08-31T02:50:14Z",
                "finished_at":null,"error_kind":null,"title":null,
                "library_id":null}"#,
        )
        .unwrap();
        assert_eq!(without.usd_so_far, None);

        // Serialised camelCase, because the webview reads `usdSoFar`.
        let out = serde_json::to_value(&with).unwrap();
        assert_eq!(out["usdSoFar"], 0.025);
    }

    #[test]
    fn progress_is_read_when_the_activity_reports_and_absent_when_it_does_not() {
        // The absence is the common case and must not be an error: only
        // `extract_semantics` heartbeats, and no run older than that code does.
        let live: RunState = serde_json::from_str(
            r#"{"workflow_id":"w1","stage":"semantics","state":"running",
                "progress":{"activity":"extract_semantics","done":260,"total":598}}"#,
        )
        .unwrap();
        let progress = live.progress.as_ref().expect("a reported progress must survive");
        assert_eq!((progress.done, progress.total), (260, 598));

        let quiet: RunState = serde_json::from_str(
            r#"{"workflow_id":"w1","stage":"correcting","state":"running"}"#,
        )
        .unwrap();
        assert!(quiet.progress.is_none());

        let out = serde_json::to_value(&live).unwrap();
        assert_eq!(out["progress"]["done"], 260);
    }

    // -- the overview endpoints --------------------------------------------

    const SUMMARY: &str = r#"{
        "catalog": {
            "available": true, "detail": null,
            "libraries": 1, "documents": 72, "absent_documents": 0,
            "active_versions": 70, "indexed_versions": 69,
            "indexed_bytes": 16548853
        },
        "pages": {"available": false, "recorded": 0, "of": 69, "pages": null},
        "recent_runs": [{
            "id": "ingest-1", "workflow_id": "ingest-1", "kind": "index",
            "state": "succeeded", "stage": "semantics",
            "started_at": "2026-08-22T10:53:57.240966Z",
            "finished_at": "2026-08-22T11:00:53.734138Z",
            "error_kind": null, "title": "01 Liderazgo", "library_id": "lib_teologia"
        }],
        "graph": {
            "available": true, "detail": null,
            "nodes": {"Concept": 11348, "Chunk": 4722},
            "deterministic_edges": {"HAS_CHUNK": 8570},
            "semantic_edges": {"MENTIONS": 24424, "ABOUT": 24174}
        }
    }"#;

    #[test]
    fn the_summary_reaches_the_webview_camel_cased() {
        let parsed: ProjectSummary = serde_json::from_str(SUMMARY).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["catalog"]["indexedVersions"], 69);
        assert_eq!(out["catalog"]["absentDocuments"], 0);
        assert_eq!(out["graph"]["semanticEdges"]["MENTIONS"], 24424);
        assert_eq!(out["graph"]["deterministicEdges"]["HAS_CHUNK"], 8570);
        assert_eq!(out["recentRuns"][0]["workflowId"], "ingest-1");
        assert!(out["catalog"]["indexed_versions"].is_null());
        assert!(out["graph"]["semantic_edges"].is_null());
    }

    #[test]
    fn an_unavailable_leg_stays_null_and_never_becomes_zero() {
        // The property the endpoint exists for, carried through the proxy. A
        // `#[serde(default)]` on these counts would make a stopped Memgraph
        // render as a project holding no concepts — a different fact with a
        // different fix.
        let down = r#"{
            "catalog": {
                "available": true, "detail": null,
                "libraries": 1, "documents": 72, "absent_documents": 0,
                "active_versions": 70, "indexed_versions": 69,
                "indexed_bytes": 16548853
            },
            "pages": {"available": false, "recorded": 0, "of": 69, "pages": null},
            "recent_runs": [],
            "graph": {
                "available": false,
                "detail": "GraphError: Failed to DNS resolve address memgraph:7687",
                "nodes": null, "deterministic_edges": null, "semantic_edges": null
            }
        }"#;

        let parsed: ProjectSummary = serde_json::from_str(down).unwrap();
        assert!(!parsed.graph.available);
        assert!(parsed.graph.nodes.is_none());
        assert!(parsed.graph.semantic_edges.is_none());
        assert!(parsed.graph.detail.is_some());
        // and the half that was readable survives
        assert_eq!(parsed.catalog.documents, Some(72));

        let out = serde_json::to_value(&parsed).unwrap();
        assert!(out["graph"]["nodes"].is_null());
        assert_ne!(out["graph"]["nodes"], 0);
    }

    #[test]
    fn a_run_that_outlived_its_document_still_parses() {
        let orphan = r#"{
            "catalog": {
                "available": false, "detail": "OSError: connection refused",
                "libraries": null, "documents": null, "absent_documents": null,
                "active_versions": null, "indexed_versions": null,
                "indexed_bytes": null
            },
            "pages": {"available": false, "recorded": null, "of": null, "pages": null},
            "recent_runs": null,
            "graph": {
                "available": true, "detail": null, "nodes": {},
                "deterministic_edges": {}, "semantic_edges": {}
            }
        }"#;

        let parsed: ProjectSummary = serde_json::from_str(orphan).unwrap();
        // None, not an empty list: "could not be read" and "nothing has run yet"
        // are different states, and Inicio renders them differently.
        assert!(parsed.recent_runs.is_none());
        assert!(parsed.catalog.documents.is_none());
    }

    const GRAPH: &str = r#"{
        "library_id": "lib_teologia",
        "semantic": true,
        "confidence_floor": 0.6,
        "min_documents": 2,
        "documents": [{
            "document_id": "doc_411a063fb7c59a5ce0caf784",
            "version_id": "ver_614f638c1f995372b5d3b3d2",
            "title": "01 Liderazgo",
            "format": "pdf"
        }],
        "concepts": [{
            "id": "con_aaaaaaaaaaaaaaaaaaaaaaaa", "name": "Dios",
            "concept_type": "Doctrina", "mentions": 657, "documents": 51
        }],
        "edges": [{
            "version_id": "ver_614f638c1f995372b5d3b3d2",
            "concept_id": "con_aaaaaaaaaaaaaaaaaaaaaaaa",
            "mentions": 12, "confidence": 0.95
        }],
        "truncated": {"documents": false, "edges": false}
    }"#;

    #[test]
    fn the_library_graph_reaches_the_webview_camel_cased() {
        let parsed: LibraryGraph = serde_json::from_str(GRAPH).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["libraryId"], "lib_teologia");
        assert_eq!(out["confidenceFloor"], 0.6);
        assert_eq!(out["minDocuments"], 2);
        assert_eq!(out["documents"][0]["versionId"], "ver_614f638c1f995372b5d3b3d2");
        // `type` is a Rust keyword on the Python side's `concept_type`; the
        // webview reads `conceptType` here as it does on Explore's `Concept`.
        assert_eq!(out["concepts"][0]["conceptType"], "Doctrina");
        assert_eq!(out["edges"][0]["conceptId"], "con_aaaaaaaaaaaaaaaaaaaaaaaa");
        assert!(out["library_id"].is_null());
        assert!(out["concepts"][0]["concept_type"].is_null());
    }

    #[test]
    fn a_concepts_degree_survives_the_proxy_untouched() {
        // Not the number of edges in this payload: 51 books mention "Dios" and
        // this response carries one edge for it. A screen that recomputed the
        // degree from the edges it holds would understate every concept.
        let parsed: LibraryGraph = serde_json::from_str(GRAPH).unwrap();
        assert_eq!(parsed.concepts[0].documents, 51);
        assert_eq!(parsed.edges.len(), 1);
    }

    // -- the audit trail ----------------------------------------------------

    /// A real body from `GET /runs/{id}/audit`, snake_case as both planes emit it.
    const AUDIT: &str = r#"{
      "run": {"id": "ingest-1", "workflow_id": "ingest-1", "kind": "index",
              "state": "failed", "stage": "semantics",
              "started_at": "2026-09-01T14:00:00+00:00",
              "finished_at": "2026-09-01T14:10:00+00:00",
              "error_kind": "activity_failed", "error_detail": "503",
              "title": "Institución", "library_id": "lib_teologia",
              "document_id": "doc_1", "version_id": "ver_1"},
      "stages": [
        {"seq": 1, "stage": "correcting", "at": "2026-09-01T14:00:00+00:00",
         "ended_at": "2026-09-01T14:04:00+00:00", "seconds": 240.0,
         "outcome": null, "detail": null,
         "cost": {"input_tokens": 1000, "output_tokens": 500, "usd": 0.0334,
                  "unpriced_entries": 0,
                  "entries": [{"stage": "correction", "provider": "vertex",
                               "model": "gemini-3.6-flash", "input_tokens": 1000,
                               "output_tokens": 500, "usd": 0.0334}]},
         "artifacts": [{"name": "corrected_text", "rel_path": "runs/x/corrected.txt",
                        "sha256": "aa", "size_bytes": 10}]},
        {"seq": 2, "stage": "chunking", "at": "2026-09-01T14:04:00+00:00",
         "ended_at": null, "seconds": null, "outcome": null, "detail": null,
         "cost": null, "artifacts": []}
      ],
      "totals": {"input_tokens": 1000, "output_tokens": 500, "usd": 0.0334,
                 "unpriced_entries": 0},
      "warnings": [{"profile_id": "p1", "collides_with": "otro.pdf",
                    "similarity": 0.0, "detail": "colisión"}]
    }"#;

    #[test]
    fn the_ledger_reaches_the_webview_camel_cased() {
        // Renamed on the serialize side only, so the Python field names still
        // deserialize and the webview still gets idiomatic JavaScript.
        let parsed: RunAudit = serde_json::from_str(AUDIT).unwrap();
        let out = serde_json::to_value(&parsed).unwrap();
        assert!(out["run"].get("workflow_id").is_none());
        assert_eq!(out["run"]["workflowId"], "ingest-1");
        assert_eq!(out["stages"][0]["endedAt"], "2026-09-01T14:04:00+00:00");
        assert_eq!(out["totals"]["unpricedEntries"], 0);
    }

    #[test]
    fn a_free_stage_stays_null_rather_than_becoming_a_zero() {
        // The distinction the whole payload is built around: "this stage does
        // not spend" and "this stage's charge was not recorded" are different
        // claims, and a defaulted zero would render as the first.
        let parsed: RunAudit = serde_json::from_str(AUDIT).unwrap();
        assert!(parsed.stages[1].cost.is_none());
        assert_eq!(parsed.stages[0].cost.as_ref().unwrap().usd, Some(0.0334));
    }

    #[test]
    fn a_history_that_aged_out_is_unavailable_rather_than_empty() {
        let gone: RunEventPage =
            serde_json::from_str(r#"{"available": false, "truncated": false, "events": []}"#)
                .unwrap();
        assert!(!gone.available && gone.events.is_empty());

        let live: RunEventPage = serde_json::from_str(
            r#"{"available": true, "truncated": true, "events": [
                 {"id": 12, "at": "2026-09-01T14:07:25+00:00",
                  "type": "ActivityTaskStarted", "activity": "extract_semantics",
                  "attempt": 2, "detail": null}]}"#,
        )
        .unwrap();
        // `type` is a Rust keyword, so the field is `kind` and renamed on the
        // deserialize side only — the webview gets `kind`, which is what the
        // other tagged payloads in this file already use.
        assert_eq!(live.events[0].kind, "ActivityTaskStarted");
        assert_eq!(live.events[0].attempt, Some(2));
        assert!(live.truncated);
    }

    #[test]
    fn a_control_plane_that_predates_the_ledger_loses_a_field_not_the_call() {
        // Every optional is `#[serde(default)]` for the reason the cost range's
        // new fields were: an older plane must cost the caller one field, never
        // the whole response.
        let thin: RunListPage =
            serde_json::from_str(r#"{"runs": [{"id": "r1", "workflow_id": "r1",
                "kind": "index", "state": "running",
                "started_at": "2026-09-01T14:00:00+00:00"}]}"#)
                .unwrap();
        assert_eq!(thin.runs[0].id, "r1");
        assert!(thin.runs[0].usd_so_far.is_none());
        assert!(thin.next_before.is_none());
    }

    #[test]
    fn a_cursor_survives_the_query_string_it_travels_in() {
        // `+00:00` unencoded is decoded by the server as a space, so the cursor
        // parses as an invalid date, the route falls back to the first page, and
        // the queue pages forever over the same rows.
        let encoded = crate::percent_encode("2026-09-01T14:00:00+00:00|ingest-1");
        assert!(!encoded.contains('+'), "{encoded}");
        assert!(!encoded.contains('|'), "{encoded}");
        assert!(encoded.contains("%2B"), "{encoded}");
    }

    // -- conversations -----------------------------------------------------

    const TURN: &str = r#"{
        "seq": 2,
        "question": "¿y su muerte?",
        "searched": "¿Qué dice el corpus sobre la muerte de Jesucristo?",
        "answer": "Los fragmentos dicen…",
        "state": "answered",
        "effort": "standard",
        "style_effort": "brief",
        "citations": [{
            "chunk_id": "chk_aaaaaaaaaaaaaaaaaaaaaaaa",
            "locator": "Cap 1 · [1:2]",
            "claim": "algo",
            "page": null,
            "section_title": null
        }],
        "cited_evidence": [{
            "chunk_id": "chk_aaaaaaaaaaaaaaaaaaaaaaaa",
            "title": "El Reto de Dios",
            "breadcrumb": "Cap 1",
            "text": "…",
            "kind": "cuerpo",
            "score": 0.81,
            "source": "vector",
            "locator": "Cap 1 · [1:2]",
            "version_id": "ver_aaaaaaaaaaaaaaaaaaaaaaaa",
            "document_id": "doc_aaaaaaaaaaaaaaaaaaaaaaaa",
            "page": null,
            "claims": []
        }],
        "error": null,
        "asked_at": "2026-09-06T01:00:00+00:00",
        "answered_at": "2026-09-06T01:00:09+00:00"
    }"#;

    #[test]
    fn a_turn_arrives_snake_case_and_leaves_camel_case() {
        let turn: ConversationTurn = serde_json::from_str(TURN).unwrap();
        assert_eq!(turn.seq, 2);
        assert_eq!(turn.searched.as_deref(), Some("¿Qué dice el corpus sobre la muerte de Jesucristo?"));
        assert_eq!(turn.citations.len(), 1);
        // Evidence carries fields this struct does not declare — serde drops
        // them, which is what lets the server add one without breaking a build.
        assert_eq!(turn.cited_evidence[0].title, "El Reto de Dios");

        let out = serde_json::to_value(&turn).unwrap();
        assert_eq!(out["styleEffort"], "brief");
        assert_eq!(out["citedEvidence"][0]["chunkId"], "chk_aaaaaaaaaaaaaaaaaaaaaaaa");
        assert_eq!(out["askedAt"], "2026-09-06T01:00:00+00:00");
        // The snake_case names must not survive to the webview, or a component
        // reading either spelling would work by accident until somebody tidied.
        assert!(out.get("style_effort").is_none());
        assert!(out.get("cited_evidence").is_none());
    }

    #[test]
    fn a_new_turn_arrives_camel_case_and_leaves_snake_case() {
        let body: NewTurn = serde_json::from_str(r#"{"text":"¿y su muerte?","effort":"thorough"}"#)
            .unwrap();
        let out = serde_json::to_value(&body).unwrap();
        assert_eq!(out["text"], "¿y su muerte?");
        assert_eq!(out["effort"], "thorough");
    }

    #[test]
    fn a_turn_with_no_level_omits_the_key_rather_than_sending_null() {
        // The same decision `ask.service.ts` records: omitting the key is how
        // the payload says "whatever the server's default is", and a `null`
        // would be a value the server has to interpret.
        let body: NewTurn = serde_json::from_str(r#"{"text":"¿?"}"#).unwrap();
        let out = serde_json::to_value(&body).unwrap();
        assert!(out.get("effort").is_none());
    }

    #[test]
    fn a_conversation_summary_survives_the_round_trip() {
        let row: Conversation = serde_json::from_str(
            r#"{"id":"cnv_1","library_id":"lib_a","title":"La fe","title_generated":true,
                "turns":2,"created_at":"t0","last_message_at":"t1"}"#,
        )
        .unwrap();
        let out = serde_json::to_value(&row).unwrap();
        assert_eq!(out["libraryId"], "lib_a");
        assert_eq!(out["titleGenerated"], true);
        assert_eq!(out["lastMessageAt"], "t1");
        assert!(out.get("library_id").is_none());
    }

    // -- the stream ---------------------------------------------------------

    fn events(chunks: &[&[u8]]) -> Vec<ChatEvent> {
        let mut buf = SseBuffer::default();
        chunks.iter().flat_map(|c| buf.push(c)).collect()
    }

    #[test]
    fn a_whole_event_in_one_chunk() {
        let got = events(&[b"data: {\"type\":\"token\",\"seq\":1,\"text\":\"hola\"}\n\n"]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].event, "token");
        assert_eq!(got[0].text.as_deref(), Some("hola"));
        assert_eq!(got[0].seq, Some(1));
    }

    #[test]
    fn an_event_split_across_chunks_is_held_until_it_is_whole() {
        let got = events(&[
            b"data: {\"type\":\"tok",
            b"en\",\"seq\":1,\"text\":\"hola\"}",
            b"\n\n",
        ]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].text.as_deref(), Some("hola"));
    }

    #[test]
    fn an_accent_split_across_a_chunk_boundary_survives() {
        // The reason decoding waits for a newline. `ó` is two bytes, and a chunk
        // boundary can fall between them — this corpus is Spanish, so that is
        // not a rare case. Decoding per chunk would put a replacement character
        // in the text the reader is reading.
        let line = "data: {\"type\":\"token\",\"text\":\"predicación\"}\n";
        let whole = line.as_bytes();
        // One byte into the two-byte `ó`, computed rather than guessed.
        let cut = line.find('ó').unwrap() + 1;
        assert!(std::str::from_utf8(&whole[..cut]).is_err(), "the split must be mid-character");
        let got = events(&[&whole[..cut], &whole[cut..]]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].text.as_deref(), Some("predicación"));
    }

    #[test]
    fn several_events_in_one_chunk_all_come_out_in_order() {
        let got = events(&[
            b"data: {\"type\":\"token\",\"seq\":1,\"text\":\"a\"}\n\ndata: {\"type\":\"token\",\"seq\":2,\"text\":\"b\"}\n\n",
        ]);
        assert_eq!(
            got.iter().map(|e| e.text.clone().unwrap()).collect::<Vec<_>>(),
            vec!["a", "b"]
        );
    }

    #[test]
    fn the_done_event_carries_the_settled_turn() {
        // Compacted first: `TURN` is pretty-printed, and an SSE `data:` line
        // ends at the first newline — embedding it raw would frame one event as
        // a dozen broken ones. Worth knowing before writing another of these.
        let turn: serde_json::Value = serde_json::from_str(TURN).unwrap();
        let line = format!(
            "data: {}\n\n",
            serde_json::json!({"type": "done", "turn": turn})
        );
        let got = events(&[line.as_bytes()]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].event, "done");
        let turn = got[0].turn.as_ref().expect("done carried no turn");
        assert_eq!(turn.state, "answered");
        assert_eq!(turn.citations.len(), 1);
    }

    #[test]
    fn an_error_event_keeps_the_kind_the_ui_keys_on() {
        let got = events(&[
            b"data: {\"type\":\"error\",\"kind\":\"catalog_unreachable\",\"message\":\"no\"}\n\n",
        ]);
        assert_eq!(got[0].kind.as_deref(), Some("catalog_unreachable"));
    }

    #[test]
    fn lines_that_are_not_data_are_ignored_rather_than_breaking_the_stream() {
        // Blank separators, comments and any SSE field this build does not read.
        let got = events(&[
            b": keep-alive\n\nevent: token\nid: 7\ndata: {\"type\":\"token\",\"text\":\"x\"}\n\n",
        ]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].text.as_deref(), Some("x"));
    }

    #[test]
    fn an_event_this_build_cannot_parse_is_dropped_not_fatal() {
        // A newer server sending something unknown must not take the stream
        // down: the answer is still arriving and the authoritative copy is in
        // the catalog either way.
        let got = events(&[
            b"data: not json at all\n\ndata: {\"type\":\"token\",\"text\":\"sigue\"}\n\n",
        ]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].text.as_deref(), Some("sigue"));
    }

    #[test]
    fn an_unknown_event_type_arrives_as_data_rather_than_failing() {
        let got = events(&[b"data: {\"type\":\"stage\",\"seq\":3}\n\n"]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].event, "stage");
    }

    #[test]
    fn the_event_type_is_spelled_type_on_the_way_out_too() {
        // Rust calls it `event` because `type` is a keyword; the webview must
        // still see the same discriminator the server sent.
        let got = events(&[b"data: {\"type\":\"token\",\"text\":\"x\"}\n\n"]);
        let out = serde_json::to_value(&got[0]).unwrap();
        assert_eq!(out["type"], "token");
        assert!(out.get("event").is_none());
    }

    /// One turn's stream, captured from the running control API on 2026-09-05.
    ///
    /// A real sample rather than a handwritten one, for the reason
    /// `test_the_raw_history_translates_against_a_real_temporal` gives on the
    /// Python side: a double built from the same assumptions as the code agrees
    /// with them. This is Spanish prose with real accents, a real settled turn
    /// and real citations, framed exactly as the server frames it.
    const REAL: &[u8] = include_bytes!("../fixtures/chat-turn.sse");

    #[test]
    fn the_real_stream_decodes_however_it_is_cut_up() {
        // The property that matters, asserted at *every* byte boundary rather
        // than at boundaries somebody thought to pick. A chunk can end anywhere,
        // and this corpus is Spanish: 6.7 kB of it holds hundreds of two-byte
        // characters, so a decoder that split them would be caught here even if
        // no handwritten case happened to land on one.
        let whole = events(&[REAL]);
        assert_eq!(whole.len(), 1, "the capture should hold exactly one event");
        assert_eq!(whole[0].event, "done");
        let expected = whole[0].turn.as_ref().expect("no turn in the capture");

        for cut in 1..REAL.len() {
            let got = events(&[&REAL[..cut], &REAL[cut..]]);
            assert_eq!(got.len(), 1, "cut at {cut} produced {} events", got.len());
            let turn = got[0].turn.as_ref().expect("no turn");
            assert_eq!(turn.answer, expected.answer, "answer differs when cut at {cut}");
            assert_eq!(turn.question, expected.question, "question differs when cut at {cut}");
            assert_eq!(turn.searched, expected.searched, "searched differs when cut at {cut}");
        }
    }

    #[test]
    fn the_real_stream_carries_what_the_screen_needs() {
        let got = events(&[REAL]);
        let turn = got[0].turn.as_ref().unwrap();
        assert_eq!(turn.state, "answered");
        // The substitution the whole feature exists for: what was asked is not
        // what was searched.
        assert_ne!(turn.searched.as_deref(), Some(turn.question.as_str()));
        assert!(turn.searched.as_deref().unwrap().contains("justificación"));
        // Every citation the screen offers must carry a locator, because a
        // citation nobody can open is one `answer._verify` would have dropped.
        assert!(!turn.citations.is_empty());
        for c in &turn.citations {
            assert!(!c.locator.is_empty(), "a citation reached the client with no locator");
        }
        assert_eq!(turn.cited_evidence.len(), turn.citations.len());
    }

    #[test]
    fn a_stage_event_survives_the_proxy_with_its_counts() {
        // serde drops what it does not declare, so a proxy missing these fields
        // would leave the window on one unchanging line for the nine tenths of a
        // turn they exist to cover — and nothing would error.
        let got = events(&[
            b"data: {\"type\":\"stage\",\"seq\":4,\"stage\":\"evidence\",\"chunks\":48,\"dense\":27}\n\n",
        ]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].event, "stage");
        assert_eq!(got[0].stage.as_deref(), Some("evidence"));
        assert_eq!(got[0].chunks, Some(48));
        assert_eq!(got[0].dense, Some(27));

        let out = serde_json::to_value(&got[0]).unwrap();
        assert_eq!(out["stage"], "evidence");
        assert_eq!(out["chunks"], 48);
    }

    #[test]
    fn a_stage_with_no_counts_reports_absence_rather_than_zero() {
        // `dense: 0` is a question the corpus does not support; `None` is a
        // stage that never measured it. The two must not render the same.
        let got = events(&[b"data: {\"type\":\"stage\",\"stage\":\"planning\"}\n\n"]);
        assert_eq!(got[0].stage.as_deref(), Some("planning"));
        assert_eq!(got[0].chunks, None);
        assert_eq!(got[0].dense, None);
    }

    #[test]
    fn a_stage_carries_no_text_so_it_cannot_be_appended_to_an_answer() {
        let got = events(&[
            b"data: {\"type\":\"token\",\"text\":\"a\"}\n\ndata: {\"type\":\"stage\",\"stage\":\"generating\"}\n\ndata: {\"type\":\"token\",\"text\":\"b\"}\n\n",
        ]);
        let prose: String = got
            .iter()
            .filter(|e| e.event == "token")
            .filter_map(|e| e.text.clone())
            .collect();
        assert_eq!(prose, "ab");
        assert!(got.iter().any(|e| e.event == "stage" && e.text.is_none()));
    }
}

#[cfg(test)]
mod buckets {
    //! The bucket types cross in both directions, and one of them crosses both
    //! ways: `BucketSource` arrives from the webview on a register and leaves
    //! inside every `StoredBucket`. It is therefore snake_case on both sides,
    //! like `Question`, and this pins that beside the other exception.
    use super::*;

    #[test]
    fn a_source_stays_snake_case_on_both_sides_and_defaults_its_archive() {
        let from_webview = r#"{"bucket": "tenant-bucket", "prefix": "audios/",
            "role_arn": "arn:aws:iam::123456789012:role/reader",
            "manifest_key": "metadatos/m.csv", "manifest_map": {"file": "archivo"}}"#;
        let parsed: BucketSource = serde_json::from_str(from_webview).unwrap();
        assert_eq!(parsed.archive_prefix, "transcripciones/");
        assert_eq!(parsed.language, "es-US");
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["role_arn"], "arn:aws:iam::123456789012:role/reader");
        assert_eq!(out["manifest_map"]["file"], "archivo");
        assert_eq!(out["archive_prefix"], "transcripciones/");
    }

    #[test]
    fn an_object_row_arrives_snake_case_and_leaves_camel_case() {
        let from_plane = r#"{"key": "audios/a.mp3", "etag": "e1", "size": 1000,
            "last_modified": "", "container": "mp3", "duration_s": 3600,
            "duration_estimated": false, "title": "A", "author": "", "recorded_at": "1995-04-02",
            "published_at": "", "source": "iVoox", "url": "https://feed/1", "available": true,
            "warnings": [], "document_id": "doc_a", "active_version_id": null,
            "run_id": "audio-1", "run_state": "awaiting_approval", "state": "pending"}"#;
        let parsed: BucketObjectRow = serde_json::from_str(from_plane).unwrap();
        assert_eq!(parsed.duration_s, 3600);
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["durationS"], 3600);
        assert_eq!(out["recordedAt"], "1995-04-02");
        assert_eq!(out["runState"], "awaiting_approval");
        assert!(out["activeVersionId"].is_null());
        assert!(out.get("duration_s").is_none());
    }

    #[test]
    fn a_stored_bucket_from_before_a_count_existed_still_parses() {
        // The counts are defaulted: a `bucket.json` written by an older worker
        // must not take the picker down.
        let from_plane = r#"{"bucket_id": "ce2d1a0695b0",
            "source": {"bucket": "b"}, "library_id": "lib_s3_ce2d1a0695b0",
            "library_name": "s3://b/"}"#;
        let parsed: StoredBucket = serde_json::from_str(from_plane).unwrap();
        assert_eq!(parsed.object_count, 0);
        assert!(!parsed.complete);
        assert_eq!(parsed.source.prefix, "");
    }

    #[test]
    fn the_probe_body_carries_the_switches_python_spells() {
        let body =
            bucket_probe_body(&["audios/a.mp3".to_string()], &StageOptions::default(), true, "local");
        assert_eq!(body["keys"][0], "audios/a.mp3");
        assert_eq!(body["reindex"], true);
        assert_eq!(body["options"]["extract_semantics"], true);
        assert!(body["options"].get("extractSemantics").is_none());
        // The engine is decided per batch, before quoting, so it travels with
        // the probe and not with the approval: every gate quotes exactly the
        // engine its own run is on.
        assert_eq!(body["transcriber"], "local");
    }

    /// A probe answer from a plane that predates the local transcriber still
    /// parses, and reads as Amazon.
    #[test]
    fn a_probe_answer_with_no_engine_named_reads_as_amazon() {
        let parsed: BucketProbeStarted =
            serde_json::from_str(r#"{"key": "audios/a.mp3", "workflow_id": "audio-1"}"#).unwrap();
        assert_eq!(parsed.transcriber, "transcribe");
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["workflowId"], "audio-1");
    }
}
