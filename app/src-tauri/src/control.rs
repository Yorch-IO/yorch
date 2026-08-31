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

/// Uploading a source file. Generous because this is the one request whose
/// duration is set by the user's upstream bandwidth rather than by the server:
/// the cap is 200 MB, and a slow home connection can spend minutes on a book
/// that the whole rest of the pipeline then handles in seconds.
const UPLOAD_TIMEOUT: Duration = Duration::from_secs(900);

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
    #[serde(default = "default_top_k")]
    pub top_k: u32,
    #[serde(default)]
    pub filters: std::collections::BTreeMap<String, String>,
    #[serde(default = "default_floor")]
    pub confidence_floor: f64,
}

fn default_top_k() -> u32 {
    8
}

fn default_floor() -> f64 {
    0.6
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
    /// Other documents holding these same bytes. Non-empty means removing this
    /// document leaves the version standing, and the confirm has to say so.
    #[serde(default)]
    pub also_held_by: Vec<String>,
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
    #[serde(default)]
    pub versions: Vec<VersionRow>,
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

    async fn send<T: serde::de::DeserializeOwned>(
        &self,
        req: reqwest::RequestBuilder,
        path: &str,
        timeout: Duration,
    ) -> Result<T> {
        let url = format!("{}{path}", self.base);
        // Credentials are attached here and nowhere else: every helper above
        // funnels through this method, so a new one cannot be added that
        // forgets them.
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
        // See `unreachable`: which of the two this becomes is decided by the
        // cause, not by the call site.
        let response = req
            .timeout(timeout)
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

        response
            .json::<T>()
            .await
            .map_err(|source| unreachable(url, source))
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
        let from_webview = r#"{"library_id": "lib_1", "text": "¿qué?"}"#;
        let parsed: Question = serde_json::from_str(from_webview).unwrap();
        assert_eq!(parsed.library_id, "lib_1");
        let out = serde_json::to_value(&parsed).unwrap();
        assert_eq!(out["library_id"], "lib_1");
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
}
