/**
 * Typed wrapper over the Rust commands.
 *
 * These types mirror the `Serialize` structs in `src-tauri/src/`. They are hand
 * written rather than generated because the surface is small and the duplication
 * is visible at review time; if it grows past a couple of dozen shapes, generate
 * them from the Rust definitions instead of letting the two drift.
 */
import { Channel, invoke } from "@tauri-apps/api/core";

/** Machine-readable tags from `AppError::kind()` in src-tauri/src/error.rs. */
export type AppErrorKind =
  | "docker_missing"
  | "compose_failed"
  | "control_unreachable"
  | "control_timeout"
  // A stream that stopped delivering, which is not the same failure as a
  // request that never answered: the turn behind it is still running.
  | "control_stream_stalled"
  | "control_status"
  | "no_free_port"
  | "workspace_not_native"
  | "io"
  | "not_signed_in"
  | "config";

/**
 * Machine-readable tags the *control API* returns inside a 4xx/5xx body.
 *
 * Distinct from `AppErrorKind`, which is Rust's own. These arrive as JSON in
 * `AppError.message` when `kind` is `control_status`, so the UI needs to look
 * inside it to offer the right advice.
 */
export type ControlErrorKind =
  // The paid plane adds these. `tenant_scope_pending` had a phase-1 meaning —
  // the graph and the pipeline answered only for the legacy organisation — and
  // phase 2 left it exactly one use: that organisation has no inbox on this
  // plane, because its workspace root *is* the volume and this plane's mount is
  // read-only there. So it is a refusal on a *write*, not a narrower read, and
  // the guidance says which backend does own that corpus. Writing the file to
  // `tenants/<legacy>/inbox` instead returned 200 and a path `stage_source`
  // then refused, which is the dead end the refusal replaced.
  | "unauthenticated"
  | "unknown_user"
  | "user_inactive"
  | "no_membership"
  | "tenant_required"
  | "tenant_scope_pending"
  | "unsupported_format"
  | "not_a_video_url"
  | "run_not_found"
  | "gate_not_ready"
  | "provider_unconfigured"
  | "graph_unreachable"
  | "bad_identifier"
  | "document_not_found"
  | "version_not_found"
  | "source_path_unknown"
  | "rebuild_unavailable"
  | "question_not_found"
  /** A level name no effort table has. Only reachable by a hand-built request:
   *  the settings screen renders one box per level it was given. */
  | "effort_not_found";

export interface AppError {
  kind: AppErrorKind;
  message: string;
  /**
   * The *control API's* own `detail.kind`, read out by Rust.
   *
   * Rust does it because it holds the untruncated body: `message` is cut to 500
   * characters for a person to read, and digging the tag out of it here needed
   * a balanced `{…}` to survive — so a long body silently lost its guidance.
   * `tenant_required` provoked it, carrying the caller's tenant ids as a
   * sibling field.
   *
   * Absent when there is none, and absent from a shell older than this field —
   * which is why `controlErrorKind` still falls back to reading the message.
   */
  controlKind?: string;
}

/** Narrow an unknown thrown value to our error shape. */
export function isAppError(e: unknown): e is AppError {
  return (
    typeof e === "object" &&
    e !== null &&
    typeof (e as AppError).kind === "string" &&
    typeof (e as AppError).message === "string"
  );
}

export function errorMessage(e: unknown): string {
  if (isAppError(e)) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

/**
 * Translation key carrying advice for a failure the user can act on.
 *
 * The point of tagging errors with a `kind` is that some failures have a fix
 * the raw message does not state. "workspace must live on the native
 * filesystem" says what is wrong; only the guidance says to pick a path inside
 * the Linux home directory and why. Kinds absent here have nothing useful to
 * add beyond their message.
 */
const GUIDANCE: Partial<Record<AppErrorKind, string>> = {
  workspace_not_native: "error.workspaceNotNative",
  compose_failed: "error.composeFailed",
  control_unreachable: "error.controlUnreachable",
  control_timeout: "error.controlTimeout",
  control_stream_stalled: "error.controlStreamStalled",
  not_signed_in: "error.notSignedIn",
};

/** Advice keyed on the control API's own `kind`, read out of the error body. */
const CONTROL_GUIDANCE: Partial<Record<ControlErrorKind, string>> = {
  unsupported_format: "error.unsupportedFormat",
  not_a_video_url: "error.notAVideoUrl",
  provider_unconfigured: "error.providerUnconfigured",
  run_not_found: "error.runNotFound",
  graph_unreachable: "error.graphUnreachable",
  bad_identifier: "error.badIdentifier",
  source_path_unknown: "error.sourcePathUnknown",
  rebuild_unavailable: "error.rebuildUnavailable",
  document_not_found: "error.documentNotFound",
  question_not_found: "error.questionNotFound",
  unauthenticated: "error.unauthenticated",
  unknown_user: "error.unknownUser",
  user_inactive: "error.userInactive",
  no_membership: "error.noMembership",
  tenant_required: "error.tenantRequired",
  tenant_scope_pending: "error.tenantScopePending",
};

/**
 * The control API's `kind`, when this error carries one.
 *
 * Rust reads it off the untruncated body and sends it as `controlKind`; that is
 * the path that always works. The fallback below re-reads it out of the
 * message, which is what this did on its own until the truncation was found to
 * be eating tags — kept so a webview running against an older shell still gets
 * its guidance rather than none.
 *
 * Best-effort either way: a body that is not JSON, or one cut mid-object,
 * simply has no tag rather than throwing while an error screen renders.
 */
export function controlErrorKind(e: unknown): ControlErrorKind | undefined {
  if (!isAppError(e) || e.kind !== "control_status") return undefined;
  if (e.controlKind) return e.controlKind as ControlErrorKind;
  const match = e.message.match(/\{[\s\S]*\}/);
  if (!match) return undefined;
  try {
    const body = JSON.parse(match[0]) as { detail?: { kind?: string } };
    return body.detail?.kind as ControlErrorKind | undefined;
  } catch {
    return undefined;
  }
}

export function errorGuidanceKey(e: unknown): string | undefined {
  const control = controlErrorKind(e);
  if (control && CONTROL_GUIDANCE[control]) return CONTROL_GUIDANCE[control];
  return isAppError(e) ? GUIDANCE[e.kind] : undefined;
}

export interface DockerInfo {
  dockerVersion: string;
  composeVersion: string;
}

export interface Ports {
  qdrantHttp: number;
  qdrantGrpc: number;
  postgres: number;
  temporal: number;
  temporalUi: number;
  api: number;
}

export interface ServiceState {
  name: string;
  state: string;
  health: string;
  publishers: string[];
}

export interface StackStatus {
  project: string;
  composeDir: string;
  workspace: string;
  ports: Ports;
  devMode: boolean;
  services: ServiceState[];
}

export interface StackEvent {
  phase: "starting" | "healthy";
  detail: string;
}

export interface ServiceHealth {
  ok: boolean;
  detail: string;
}

export interface Health {
  ok: boolean;
  services: Record<string, ServiceHealth>;
}

export interface ProbeResult {
  service: string;
  ok: boolean;
  detail: string;
}

export interface PingResult {
  workflowId: string;
  probes: ProbeResult[];
}

// -- ingest -----------------------------------------------------------------

export interface IngestRequest {
  libraryId: string;
  sourcePath: string;
  sourceKey: string;
  title?: string;
  author?: string | null;
  folderId?: string | null;
  autoApprove?: boolean;
  /** What to call the library, when this import is the one that creates it.
   *  Empty leaves an existing name alone rather than replacing it with the id. */
  libraryName?: string;
}

// -- video --------------------------------------------------------------------

/** One video to index.
 *
 * A URL where `IngestRequest` carries a path, and that is the whole difference
 * at this end: nothing is staged, so no `stageSource` call precedes this. A
 * path typed by hand cannot work in either plane — the app and the container
 * see two different namespaces — but a URL has no such problem, which is why
 * this screen has a text box and the file one does not.
 */
export interface VideoRequest {
  libraryId: string;
  url: string;
  title?: string;
  author?: string | null;
  autoApprove?: boolean;
  reindex?: boolean;
  libraryName?: string;
  /** Caption languages to prefer, best first. Empty lets the worker choose. */
  languages?: string[];
}

export interface CaptionTrack {
  language: string;
  /** `manual` or `auto`. An automatic track is a machine transcript with no
   *  punctuation, which is the one case correction is suggested for. */
  kind: string;
  ext: string;
  name: string;
}

export interface VideoProbe {
  videoId: string;
  canonicalUrl: string;
  sourceKey: string;
  title: string;
  channel: string;
  durationS: number;
  uploadDate: string;
  tracks: CaptionTrack[];
  /** null when Amazon Transcribe has to run, which is what turns a free
   *  transcript into a paid one. */
  chosen: CaptionTrack | null;
  warnings: string[];
}

export interface Transcribed {
  source: string;
  paragraphs: number;
  characters: number;
  coveredS: number;
  warnings: string[];
}

/** The two switches a video's gate offers. A video run has no profile,
 *  semantics, eval-set or tuning stage, so those are not shown. */
export interface RecommendedStages {
  correct: boolean;
  embed: boolean;
}

/** A video run's gate.
 *
 * `preview` is null when the video has no captions: there is no text to preview
 * until the money has been spent, and saying so is the point of a gate. Do not
 * render a zero there.
 */
export interface VideoGateReport {
  runId: string;
  documentId: string;
  versionId: string;
  probe: VideoProbe;
  estimate: Estimate;
  preview: Preview | null;
  transcript: Transcribed | null;
  warnings: string[];
  recommended: RecommendedStages | null;
}

/** Where a staged file landed, as the *worker* sees it.
 *
 * `sourcePath` is meaningless on the user's own machine in cloud mode — it is a
 * path inside the worker's container. It goes straight into `IngestRequest`
 * and is never shown or opened.
 */
export interface StagedSource {
  sourcePath: string;
  sourceKey: string;
  byteSize: number;
}

export interface StageOptions {
  correct: boolean;
  embed: boolean;
  extractSemantics: boolean;
  generateEvalset: boolean;
  /** Learn a document-family profile when none exists yet. Paid, so it is a
   *  switch; reusing one that exists is free and has none. */
  learnProfile: boolean;
  /** Decline an inherited profile and use the engine's measured defaults. */
  ignoreProfile: boolean;
  reviewCorrection: boolean;
  /** Try to improve retrieval, and measure whether it worked.
   *
   *  Off, and bounded to a single chunking candidate. The free half changes
   *  nothing in the index; the paid half re-cuts the document and embeds every
   *  chunk again — the largest single line the gate can show. It also raises the
   *  eval sample, because at 40 questions the bootstrap margin cannot resolve
   *  the effect the round is looking for. */
  tune: boolean;
}

export const DEFAULT_STAGES: StageOptions = {
  correct: true,
  embed: true,
  extractSemantics: true,
  generateEvalset: false,
  learnProfile: true,
  ignoreProfile: false,
  reviewCorrection: false,
  tune: false,
};

/**
 * Whether a run is still going, or what it ended as.
 *
 * Distinct from `stage`, and that distinction is the whole point: a stage query
 * against a failed workflow returns the last one it recorded, so a run whose
 * activity retries were exhausted reported `stage: "learning"` for as long as
 * anyone kept asking. Meanwhile the gate answers 409 — "not ready" — forever,
 * and a screen polling it waits for something that is never coming.
 *
 * `state` is `null` when nobody could say: a control plane older than this
 * field, or a run whose history has aged out of Temporal. Both mean *keep
 * waiting*, never *it failed*.
 */
export interface RunState {
  workflowId: string;
  stage: string | null;
  state: string | null;
  /** How far the running activity has got, when it reports one.
   *
   *  Null for every honest absence and they are deliberately not told apart:
   *  nothing pending, an activity that does not heartbeat, a run older than the
   *  code that emits it. All of them mean "no progress to show". */
  progress: RunProgress | null;
  /** What the index this run wrote can actually be asked.
   *
   *  Null means nobody measured — true of every version indexed before the
   *  stage existed, and of every run whose gate declined it. Never render it as
   *  zero: a recall of 0.00 is a claim about the index, and it sends somebody to
   *  fix one that is fine. */
  scores: RunScores | null;
}

/** Measured retrieval quality for one index.
 *
 *  `recallAt5DenseOnly` and `noiseFloor` are not extras. The eval set's
 *  questions are written *from* the chunks they must find, so they leak
 *  vocabulary to the lexical leg and the hybrid figure alone flatters the index;
 *  the gap between the two is that leakage. And recall says how often the right
 *  chunk came back while the floor says what a *wrong* one scores — without it
 *  a reader cannot tell an index that discriminates from one that returns
 *  everything at a similar distance. Render them together or not at all. */
export interface RunScores {
  recallAt1: number;
  recallAt5: number;
  mrrAt10: number;
  recallAt5DenseOnly: number;
  noiseFloor: number;
  chunks: number;
  evalQuestions: number;
  /** Below this a difference is noise. With σ ≈ 0.358 it takes about 80
   *  questions to resolve a +0.040 MRR effect. */
  margin: number;
  leakage: string;
  /** How many questions did not find their own chunk. */
  misses: number;
}

/** Chunks done out of chunks total, off the activity's heartbeat.
 *
 *  Only semantic extraction reports, and it is the one worth reporting: one
 *  generation call per chunk, so this counts calls and spend at the same time —
 *  which is what somebody deciding whether to stop a run is weighing. */
export interface RunProgress {
  activity: string;
  done: number;
  total: number;
}

export interface StartedRun {
  workflowId: string;
  state: string;
}

/** One run as the queue lists it.
 *
 *  Every field comes from the catalog, which is what makes the queue survive a
 *  stopped Temporal and a closed window: what a run is *doing* is `RunState`,
 *  what it *was* is this. `usdSoFar` is null and never zero when no stage has
 *  recorded a price — a run still in its free stages legitimately has none. */
export interface RunListItem {
  id: string;
  workflowId: string;
  kind: string;
  state: string;
  stage: string | null;
  startedAt: string;
  /** Null while it is still going. The queue filters on *this*, not on `state`,
   *  because a run that died without recording an outcome keeps a stale state. */
  finishedAt: string | null;
  errorKind: string | null;
  errorDetail: string | null;
  title: string | null;
  libraryId: string | null;
  /** What the run knew about itself before it had a document: the URL for a
   *  video, the picked file's basename for an import. `title` is the
   *  *document's* title and a run that failed before registering one has none,
   *  so the queue reads `title ?? label ?? workflowId`. */
  label: string | null;
  documentId: string | null;
  versionId: string | null;
  usdSoFar: number | null;
}

export interface RunListPage {
  runs: RunListItem[];
  /** Opaque cursor for the next page, or null at the end. Never parse it. */
  nextBefore: string | null;
}

export interface AuditCostEntry {
  stage: string;
  provider: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  /** Null is "no price known for this model", rendered "sin precio". */
  usd: number | null;
}

export interface AuditCost {
  inputTokens: number;
  outputTokens: number;
  usd: number | null;
  unpricedEntries: number;
  entries: AuditCostEntry[];
}

export interface AuditArtifact {
  name: string;
  relPath: string;
  sha256: string;
  sizeBytes: number;
}

/** One row of the ledger.
 *
 *  `stage` is null on the trailing row that collects whatever no stage claimed —
 *  the charges a question makes belong to no pipeline stage, and dropping them
 *  would make the ledger a bill that does not add up.
 *
 *  `endedAt` null means one of two things `outcome` tells apart: the run is
 *  still in this stage, or this row *is* the outcome and is an instant.
 *
 *  `cost` null means the stage does not spend. Deliberately not a zeroed block:
 *  "does not spend" and "the charge was not recorded" are different claims. */
export interface AuditStage {
  seq: number | null;
  stage: string | null;
  at: string | null;
  endedAt: string | null;
  seconds: number | null;
  outcome: string | null;
  detail: string | null;
  cost: AuditCost | null;
  artifacts: AuditArtifact[];
}

export interface AuditRun {
  id: string;
  workflowId: string;
  kind: string;
  state: string;
  stage: string | null;
  startedAt: string;
  finishedAt: string | null;
  errorKind: string | null;
  errorDetail: string | null;
  title: string | null;
  libraryId: string | null;
  /** What the run knew about itself before it had a document: the URL for a
   *  video, the picked file's basename for an import. `title` is the
   *  *document's* title and a run that failed before registering one has none,
   *  so the queue reads `title ?? label ?? workflowId`. */
  label: string | null;
  documentId: string | null;
  versionId: string | null;
}

/** A profile warning raised against the version this run produced.
 *
 *  The row carries no kind, so a reader cannot tell a plain collision from the
 *  heading disagreement that withholds activation — that distinction lives only
 *  on the Temporal payload today, and the pane must not imply otherwise. */
export interface AuditWarning {
  profileId: string | null;
  collidesWith: string | null;
  similarity: number | null;
  detail: string | null;
}

export interface RunAudit {
  run: AuditRun;
  stages: AuditStage[];
  totals: Omit<AuditCost, "entries">;
  warnings: AuditWarning[];
}

/** One line of the raw workflow history.
 *
 *  `attempt` appears from the second try onward, which is the whole reason this
 *  panel exists: a retry is invisible in every other view. */
export interface RunEvent {
  id: number;
  at: string;
  kind: string;
  activity: string | null;
  attempt: number | null;
  detail: string | null;
}

/** The raw history, or an honest statement that there is none to be had.
 *
 *  `available: false` means Temporal has forgotten this run, which is ordinary
 *  past the retention period. Deliberately not the same as an empty `events`:
 *  "the history aged out" and "this run did nothing" must not render alike. */
export interface RunEventPage {
  available: boolean;
  truncated: boolean;
  events: RunEvent[];
}

/** Which filters the queue accepts. Every one optional; all are strings on the
 *  wire because the Rust side assembles the query, so the webview cannot invent
 *  a parameter the control plane will silently ignore. */
export interface RunsQuery extends Record<string, unknown> {
  limit?: number;
  before?: string;
  kinds?: string;
  states?: string;
  libraryId?: string;
  documentId?: string;
  versionId?: string;
}

export interface ChunkKindCount {
  kind: string;
  count: number;
}

export interface Preview {
  chunkCount: number;
  kinds: ChunkKindCount[];
  characters: number;
  /**
   * False whenever correction is on. Correction runs before chunking because it
   * changes the text's length, which would invalidate every char_span — so the
   * previewed chunks are not the ones that get indexed. Never hide this.
   */
  chunksAreFinal: boolean;
  warnings: string[];
}

export interface StageEstimate {
  stage: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  /** null means the model has no recorded price. Never render it as zero. */
  usd: number | null;
  /** The upper end of the range. Equal to the fields above wherever the point
   *  estimate is already a ceiling, so a stage with no measured spread renders
   *  as a single figure without needing a flag to say so. */
  outputTokensHigh: number;
  usdHigh: number | null;
}

export interface Estimate {
  stages: StageEstimate[];
  totalUsd: number | null;
  totalUsdHigh: number | null;
  priceSource: string;
  unpricedStages: string[];
}

export interface ProfileWarning {
  profileId: string;
  collidesWith: string;
  similarity: number;
  detail: string;
}

export interface GateReport {
  runId: string;
  documentId: string;
  versionId: string;
  preview: Preview;
  estimate: Estimate;
  profileWarnings: ProfileWarning[];
  profile: ProfileDecision | null;
}

/** Where the rules that will chunk this document came from.
 *
 * `reused` is free and automatic — a family already learned these rules.
 * `learned` means one was proposed and validated for this run, which costs a
 * generation call. `default` is the engine's measured built-ins, which is a
 * known-good configuration rather than a gap.
 */
export type ProfileSource = "reused" | "learned" | "default";

export interface ProfileRules {
  headerPatterns: string[];
  headingL1Max: number;
  headingL2Max: number;
  headingL1Pattern: string | null;
  headingL2Pattern: string | null;
  questionPattern: string | null;
  footnotePattern: string | null;
}

export interface ProfileDecision {
  fingerprint: string;
  source: ProfileSource;
  slug: string;
  learnedFrom: string;
  revisions: number;
  rules: ProfileRules;
  warnings: ProfileWarning[];
  adopted: string[];
  spend: Spend | null;
}

export interface Approval {
  approved: boolean;
  options: StageOptions;
  reason: string;
}

// -- questions ---------------------------------------------------------------

export type { AskEffort } from "./askEffort";
import type { AskEffort } from "./askEffort";

export interface Question {
  library_id: string;
  text: string;
  /** Absent means "the effort level decides", which is what this app sends. */
  top_k?: number;
  filters?: Record<string, string>;
  confidence_floor?: number;
  /**
   * How much evidence and reasoning the question may spend.
   *
   * A level name, never a set of numbers: what each one means lives in the
   * worker, so a client cannot ask for two hundred chunks and neither control
   * plane has to police a number. Absent means the server's default.
   */
  effort?: AskEffort;
}

/**
 * One effort level's answer wording, as the settings screen needs it.
 *
 * `body` is what would actually be used; `custom` says whether that is the
 * organisation's override or the built-in default. Both matter: the text fills
 * the box, the flag decides whether "restore the default" would do anything,
 * and `defaultBody` is what it would restore to — carried here so restoring
 * needs no second request.
 */
export interface AnswerStyle {
  effort: AskEffort;
  body: string;
  defaultBody: string;
  custom: boolean;
}

export interface AnswerStyles {
  levels: AnswerStyle[];
  maxChars: number;
}

export interface AnswerStyleSaved {
  effort: AskEffort;
  custom: boolean;
}

export interface Citation {
  chunkId: string;
  locator: string;
  claim: string;
  page: number | null;
  sectionTitle: string | null;
}

export interface EvidenceItem {
  chunkId: string;
  title: string;
  breadcrumb: string;
  text: string;
  kind: string;
  score: number;
  source: string;
  locator: string;
}

export interface Spend {
  stage: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  usd: number | null;
}

export type AnswerState = "answered" | "insufficient_evidence" | "off_corpus";

/**
 * Asking hands the question over; the answer is collected by polling.
 *
 * The two were one call, and a real question outran the client's 180s budget:
 * the API logged `POST /ask 200 OK` while the app reported that the control API
 * had not answered, and an answer that had been computed and billed was thrown
 * away. Now the timeout covers the handover, and the answer waits in the API
 * until it is asked for — which also means closing the window no longer loses
 * one.
 */
export interface AskStarted {
  questionId: string;
  state: string;
}

export interface AskFailure {
  kind: string;
  message: string;
}

/** `answer` only once `state` is "done"; `error` only once it is "failed". */
export interface AskProgress {
  questionId: string;
  state: "running" | "done" | "failed";
  answer: Answer | null;
  error: AskFailure | null;
}

// -- conversations ----------------------------------------------------------
//
// A conversation is multi-turn asking over the same retrieval path a one-shot
// question uses. Two things about the shape are worth knowing before reading
// the screen:
//
// The transcript comes from the **catalog**, not from Temporal — which is what
// lets a conversation outlive the retention that bounds a session. So there is
// no client-side history to reconcile and nothing that can drift; the server is
// the only copy.
//
// And a turn's answer arrives twice: as `token` events while it is written, and
// as the settled turn on `done`. The second is authoritative and **replaces**
// the first — see `ChatEvent`.

/** One conversation, as a list row renders it. */
export interface Conversation {
  id: string;
  libraryId: string;
  /** Never empty: the first question truncated until `chat-title` names it. */
  title: string;
  titleGenerated: boolean;
  turns: number;
  createdAt: string;
  lastMessageAt: string;
}

export interface Conversations {
  conversations: Conversation[];
}

/**
 * A turn's state. Four of the five are an answer's own; `running` is the turn
 * still being worked on and is the reason this is not `AnswerState`.
 */
export type TurnState = "running" | AnswerState | "failed";

/** One question and its answer. */
export interface ConversationTurn {
  seq: number;
  /** What the person typed. */
  question: string;
  /**
   * The standalone question the rewrite produced, which is what was actually
   * embedded and searched for.
   *
   * Shown, not kept for debugging. A follow-up is answered against a question
   * the person did not type — that is the whole mechanism — and an answer that
   * quietly addresses something adjacent to what was asked is
   * indistinguishable from a bad answer unless the substitution is visible.
   * Equal to `question` on a first turn, which makes no rewrite call.
   */
  searched: string | null;
  answer: string;
  state: TurnState;
  effort: string;
  /** The level the *style* used, which steps down when the corpus supplied too
   *  little to justify the one asked for. */
  styleEffort: string | null;
  citations: Citation[];
  /** Only the chunks a verified citation names — never the whole retrieval. */
  citedEvidence: EvidenceItem[];
  error: AskFailure | null;
  askedAt: string;
  answeredAt: string | null;
}

export interface ConversationDetail extends Conversation {
  turnsDetail: ConversationTurn[];
}

export interface ConversationStarted {
  conversationId: string;
  libraryId: string;
}

export interface TurnStarted {
  conversationId: string;
  turnSeq: number;
  state: string;
}

/**
 * One server-sent event from a turn in flight.
 *
 * One shape for all three types rather than a discriminated union, mirroring
 * the Rust struct: an event type this build has never heard of arrives as data
 * with an unknown `type` rather than failing to decode, so a newer server
 * cannot take the stream down.
 *
 * - `stage` — `stage`, and on `evidence` the counts `chunks` and `dense`. What
 *   covers the wait: streamed prose was measured at about 8% of a turn, and the
 *   rest used to be one unchanging line over the rewrite call, the planning
 *   call, retrieval and the model's reasoning.
 * - `token` — `seq` and `text`. Prose as the model writes it, and a **draft**.
 * - `done` — `turn`, the settled turn. Authoritative, and it *replaces* the
 *   draft rather than completing it: `citas` is the last field in the answering
 *   schema, so verification cannot run until the envelope closes, and a turn can
 *   stream fluently and still come back `insufficient_evidence` because no
 *   citation survived.
 * - `error` — `kind` and `message`. The stream could not continue; the turn
 *   itself may still be landing in the catalog.
 */
export interface ChatEvent {
  type: string;
  seq?: number | null;
  text?: string | null;
  turn?: ConversationTurn | null;
  kind?: string | null;
  message?: string | null;
  /** Which stage a `stage` event announces. */
  stage?: string | null;
  /** On `evidence`: how many chunks reached the prompt. */
  chunks?: number | null;
  /**
   * On `evidence`: how many cleared the dense floor. `null` means "not
   * measured", which is not zero — zero is a question the corpus does not
   * support, and the two must not render the same.
   */
  dense?: number | null;
}

export interface Answer {
  state: AnswerState;
  text: string;
  citations: Citation[];
  evidence: EvidenceItem[];
  reason: string;
  spend: Spend[];
  /**
   * The level this answer was produced at, echoed by the server.
   *
   * Optional because an answer collected from a worker older than the level
   * carries none, and because it must not become a required field that every
   * test factory has to learn about. Read it rather than what the client
   * remembers sending: an answer can be collected on another machine.
   */
  effort?: AskEffort;
}

// -- provider ----------------------------------------------------------------

/** What the Services screen shows about Vertex AI, and what it can change.
 *
 * Two independent conditions, and both must hold before anything paid can run.
 * `configured` is a billing project having been named; `adcFound` is credentials
 * being on the machine. Gemini Enterprise refuses API keys outright, so there is
 * no third option and no fallback — without ADC the project id alone buys
 * nothing.
 */
/**
 * Which control plane the app talks to.
 *
 * The two speak the same 26 paths with the same payloads — parity the server
 * side tests — so nothing above this type knows which one answered. `local` is
 * the free stack on this machine; `cloud` is the paid, multi-tenant service.
 *
 * `signedIn` rather than a token: the webview has no reason to hold a bearer,
 * and it only needs to know whether the paid mode is usable.
 */
export type BackendMode = "local" | "cloud";

export interface BackendInfo {
  mode: BackendMode;
  baseUrl: string;
  /** Sent as `X-Tenant-Id`. Empty is correct for an account in one organisation. */
  tenantId: string;
  /**
   * A stored session exists. Still true while the ID token has expired: the
   * refresh token is good for thirty days and the next request renews it, so
   * reporting "signed out" for that would send a user to sign in once an hour.
   */
  signedIn: boolean;
  /** Whose session, for the screen. Never used to authorize anything. */
  email: string;
  /**
   * Where the refresh token is kept: `"keychain"` or `"file"`.
   *
   * A token rather than prose, so the wording lives in the i18n bundles the way
   * an error `kind` does. Rust has reported it since the keychain landed and
   * nothing read it, which made its docstring's claim that "the UI can tell the
   * user" false.
   */
  secretStore: "keychain" | "file";
}

export interface ProviderSettings {
  projectId: string;
  configured: boolean;
  adcFound: boolean;
  adcPath: string | null;
}

// -- explore -----------------------------------------------------------------
//
// Two kinds of result, and the screen must not render them alike. The outline,
// its chunks and their citations are derived from the document's own structure.
// Concepts, claims and related documents were *proposed by a model*, which is
// what `semantic` and `confidence` are for — an edge a model suggested and an
// edge read off a table of contents have different standing as evidence.

export interface Section {
  id: string;
  title: string;
  /** A dotted ordinal path ("1.1"). Nesting depth is its number of segments —
   *  never parse it as a number, which would collapse "1.1" and "1.10". */
  path: string;
  level: number;
}

export interface Outline {
  versionId: string;
  sections: Section[];
}

export interface ChunkRow {
  id: string;
  kind: string;
  ordinal: number;
  text: string;
  charStart: number;
  charEnd: number;
  page: number | null;
}

export interface SectionChunks {
  sectionId: string;
  chunks: ChunkRow[];
}

export interface Neighbours {
  id: string;
  text: string;
  kind: string;
  charStart: number;
  charEnd: number;
  beforeId: string | null;
  beforeText: string | null;
  afterId: string | null;
  afterText: string | null;
}

export interface ChunkCitation {
  id: string;
  chunkId: string;
  locator: string;
  page: number | null;
  sectionTitle: string | null;
}

export interface ChunkContext {
  chunkId: string;
  context: Neighbours | null;
  citation: ChunkCitation | null;
}

export interface Concept {
  id: string;
  name: string;
  conceptType: string | null;
  /** One sentence about the concept, assembled from the chunks that mention it.
   *  Null until a run condenses them — a paid stage, off by default — so a bare
   *  name is still the common case and the UI must read well without this. */
  description?: string | null;
  mentions: number;
  confidence: number;
}

export interface VersionConcepts {
  versionId: string;
  semantic: boolean;
  confidenceFloor: number;
  concepts: Concept[];
}

export interface RelatedDocument {
  id: string;
  title: string | null;
  /** The document owning the version. A version id is not navigable on its own. */
  documentId: string | null;
  sharedConcepts: number;
  /** *Which* concepts are shared, not only how many — parallel arrays, so the
   *  id at index i belongs to the name at index i.
   *
   *  Capped at 200 by the template while `sharedConcepts` stays the true count,
   *  so `sharedConceptIds.length < sharedConcepts` means the list is truncated.
   *  Empty from an API that predates them, which the graph must survive. */
  sharedConceptIds: string[];
  sharedConceptNames: string[];
}

export interface RelatedDocuments {
  versionId: string;
  semantic: boolean;
  confidenceFloor: number;
  documents: RelatedDocument[];
}

/** What the document does with a claim.
 *
 *  Spanish on the wire, like the chunk kinds and for the same reason: these are
 *  stored and filtered on, and the UI is what localises them. `sin_estado` is
 *  not a fourth judgement — it is a claim extracted before the field existed,
 *  and it must not be read as `afirma`. */
export type ClaimStatus = "afirma" | "niega" | "atribuido" | "sin_estado";

export interface Claim {
  id: string;
  text: string;
  confidence: number;
  /** The chunk this can be checked against. A relation nobody can verify is
   *  worse than no relation, because it still looks like evidence. */
  sourceChunkId: string;
  /** The document's own words, when the worker found the model's quote inside
   *  the chunk it claimed to come from. Absent when it did not check out. */
  quote?: string | null;
  status: ClaimStatus;
}

export interface ConceptClaims {
  conceptId: string;
  semantic: boolean;
  confidenceFloor: number;
  claims: Claim[];
}

// -- overview ----------------------------------------------------------------

/** Project-wide totals, each leg carrying its own availability.
 *
 *  Every figure is nullable and none of them defaults to zero. "Could not be
 *  read" and "is empty" are different states with different fixes, and a screen
 *  that rendered a stopped Memgraph as `0 conceptos` would be making a claim
 *  about the corpus. */
export interface ProjectSummary {
  catalog: CatalogTotals;
  graph: GraphTotals;
  pages: PageTotals;
  /** null when the catalog could not be read at all; an empty list when it
   *  answered and nothing has run yet. */
  recentRuns: RunSummary[] | null;
}

export interface CatalogTotals {
  available: boolean;
  /** Why it could not be read, or null when it could. */
  detail: string | null;
  libraries: number | null;
  documents: number | null;
  absentDocuments: number | null;
  activeVersions: number | null;
  indexedVersions: number | null;
  indexedBytes: number | null;
}

export interface GraphTotals {
  available: boolean;
  detail: string | null;
  nodes: Record<string, number> | null;
  /** Edges the documents' own structure supplies. */
  deterministicEdges: Record<string, number> | null;
  /** Edges a model proposed. Kept apart from the deterministic ones so nothing
   *  can report model output as something the corpus stated. */
  semanticEdges: Record<string, number> | null;
}

/** How many versions record a page count, and their sum.
 *
 *  Nothing writes it today — the version is registered before the document has
 *  been extracted, so the number is not knowable there. This carries the
 *  measurement rather than a hardcoded absence, so the figure appears by itself
 *  the day the column is filled in. */
export interface PageTotals {
  available: boolean;
  recorded: number | null;
  of: number | null;
  pages: number | null;
}

export interface RunSummary {
  id: string;
  workflowId: string;
  kind: string;
  state: string;
  stage: string | null;
  startedAt: string;
  finishedAt: string | null;
  errorKind: string | null;
  /** null for a run whose document has been removed. Run history outlives the
   *  document it was spent on, deliberately. */
  title: string | null;
  libraryId: string | null;
  /** Billed so far. Null, never zero: a run in its free stages and one whose
   *  model has no known price are both "no figure", and zero would claim free.
   *
   *  **It lags through the stage that costs most.** Semantic extraction records
   *  its spend when the activity ends, not per chunk, so a run 260 calls in
   *  still reports only what embedding cost — which is why progress is shown
   *  beside it rather than instead of it. */
  usdSoFar: number | null;
}

// -- the library graph -------------------------------------------------------

export interface LibraryGraph {
  libraryId: string;
  /** Always true: every edge rests on a `MENTIONS` a model proposed. */
  semantic: boolean;
  confidenceFloor: number;
  /** The degree filter that produced this result — the volume control, and the
   *  confidence floor is not. Measured on the real corpus: raising the floor
   *  from 0.6 to 0.9 removes 3% of the edges, while requiring a concept to
   *  appear in two books removes 84% — and what goes is every concept that
   *  cannot join one book to another. */
  minDocuments: number;
  documents: GraphDocument[];
  concepts: GraphConcept[];
  edges: GraphEdge[];
  truncated: { documents: boolean; edges: boolean };
}

export interface GraphDocument {
  documentId: string;
  versionId: string;
  title: string | null;
  format: string | null;
}

export interface GraphConcept {
  id: string;
  name: string;
  conceptType: string | null;
  /** Mentions summed across every book that mentions it. */
  mentions: number;
  /** How many books mention it — the degree the database counted, not the
   *  number of edges in this payload. */
  documents: number;
}

export interface GraphEdge {
  versionId: string;
  conceptId: string;
  /** How many chunks of this book mention this concept. */
  mentions: number;
  confidence: number;
}

// -- library -----------------------------------------------------------------

export interface DocumentRow {
  id: string;
  title: string;
  author: string | null;
  format: string;
  sourceKey: string;
  present: boolean;
  tags: string[];
  activeVersionId: string | null;
  updatedAt: string | null;
}

export interface Library {
  libraryId: string;
  documents: DocumentRow[];
}

/**
 * One row of the library picker.
 *
 * `indexedVersions` is the field that matters, not `documents`: a registered
 * document whose version never finished indexing retrieves nothing, so a
 * library can hold documents and still answer every question `off_corpus`.
 */
export interface LibraryRow {
  id: string;
  name: string;
  language: string;
  documents: number;
  indexedVersions: number;
}

export interface Libraries {
  libraries: LibraryRow[];
}

export interface VersionRow {
  id: string;
  contentSha256: string;
  byteSize: number;
  pageCount: number | null;
  state: string;
  active: boolean;
  createdAt: string | null;
  /** The run a rebuild would replay. null means there is nothing to replay. */
  rebuildRunId: string | null;
  /** Other documents holding these same bytes; non-empty means removing this
   *  document leaves the version standing. */
  alsoHeldBy: string[];
  /** What this version's index can be asked, from the newest run that measured
   *  it. null means nobody measured — which is every version indexed before the
   *  stage existed, and is not the same claim as a recall of zero. */
  scores: RunScores | null;
}

export interface DocumentDetail {
  id: string;
  libraryId: string;
  title: string;
  author: string | null;
  format: string;
  sourceKey: string;
  sourcePath: string | null;
  present: boolean;
  tags: string[];
  activeVersionId: string | null;
  canReindex: boolean;
  canRebuild: boolean;
  versions: VersionRow[];
}

/**
 * What a removal destroyed, and what it deliberately kept.
 *
 * The map keys inside `graph`, `catalog` and `kept` come straight from Python
 * and stay snake_case: `rename_all` renames struct fields, never map keys.
 */
export interface Removal {
  documentId: string | null;
  versionsRemoved: string[];
  versionsKept: string[];
  qdrantPoints: number;
  qdrantRepointed: number;
  graph: Record<string, number>;
  catalog: Record<string, number>;
  kept: Record<string, string>;
}

/** What promoting a version touched.
 *
 *  Every document holding these bytes, not just the one asked about: a version
 *  can be shared, and promoting it for one copy while leaving another on an
 *  older version would make the same content answer differently depending on
 *  which copy was asked about. */
export interface Activation {
  versionId: string;
  documents: string[];
}

export interface RebuildReport {
  documentId: string;
  versionId: string;
  title: string;
  chunkCount: number;
  characters: number;
  sourceRunId: string;
  /** False for a document indexed before the semantics artifact existed. */
  semanticsAvailable: boolean;
  estimate: Estimate | null;
}

export const api = {
  dockerProbe: () => invoke<DockerInfo>("docker_probe"),
  stackStatus: () => invoke<StackStatus>("stack_status"),
  stackDown: () => invoke<StackStatus>("stack_down"),
  stackLogs: (service: string, tail = 200) =>
    invoke<string>("stack_logs", { service, tail }),
  controlHealth: () => invoke<Health>("control_health"),
  controlPing: () => invoke<PingResult>("control_ping"),

  /** Put a file where the worker can read it, whichever plane that is.
   *
   *  Local mode returns the same path back and copies nothing — the app and the
   *  worker share a filesystem. Cloud mode uploads it and returns the container
   *  path the worker will open. The screen calls this in both cases and uses
   *  what it gets, so nothing in the UI has to know which plane is in use.
   */
  /** Open the OS file chooser. `null` means the person dismissed it, which is
   *  the ordinary way to leave a dialog and must not read as a failure. */
  pickSource: () => invoke<string | null>("pick_source"),

  stageSource: (path: string) => invoke<StagedSource>("stage_source", { path }),
  ingestStart: (request: IngestRequest, options: StageOptions = DEFAULT_STAGES) =>
    invoke<StartedRun>("ingest_start", { request, options }),
  /** null while the free stages are still running — a normal first answer. */
  ingestGate: (workflowId: string) =>
    invoke<GateReport | null>("ingest_gate", { workflowId }),
  /** Start indexing a video. No staging call precedes this: there is no file. */
  videoStart: (request: VideoRequest, options: StageOptions = DEFAULT_STAGES) =>
    invoke<StartedRun>("video_start", { request, options }),
  /** A video run's gate. Its own route, because the report is a different
   *  shape — `preview` is null when there are no captions to preview. null here
   *  means the probe is still running, which is the ordinary first answer. */
  videoGate: (workflowId: string) =>
    invoke<VideoGateReport | null>("video_gate", { workflowId }),
  /** Where a run *is*, which `ingestGate` alone cannot say. See `RunState`. */
  runStatus: (workflowId: string) => invoke<RunState>("run_status", { workflowId }),
  ingestApprove: (workflowId: string, approval: Approval) =>
    invoke<void>("ingest_approve", { workflowId, approval }),
  /** Stop a run that is already spending. The counterpart of `ingestApprove`:
   *  a spend gate that can only be opened is half a gate. */
  cancelRun: (workflowId: string) => invoke<void>("cancel_run", { workflowId }),
  /** The persistent queue. Catalog only, so it answers with Temporal down —
   *  which is what lets the Import screen show a queue rather than an error
   *  panel while the worker restarts. */
  runsList: (query: RunsQuery = {}) => invoke<RunListPage>("runs_list", query),
  /** What a run did, stage by stage. Answers for a run Temporal has forgotten,
   *  which is every run past the retention period — and which is exactly when
   *  somebody goes looking. */
  runAudit: (workflowId: string) => invoke<RunAudit>("run_audit", { workflowId }),
  /** The raw workflow history. Fetched only when somebody expands the panel:
   *  it is the one call here that can genuinely be slow. */
  runEvents: (workflowId: string) => invoke<RunEventPage>("run_events", { workflowId }),
  /** Which libraries exist. Free, and what every screen needs before it can
   *  ask anything: a library id is not something a person can be expected to
   *  type from memory. */
  libraries: () => invoke<Libraries>("libraries"),
  libraryDocuments: (libraryId: string, includeAbsent = false) =>
    invoke<Library>("library_documents", { libraryId, includeAbsent }),
  answerStyles: () => invoke<AnswerStyles>("answer_styles"),
  setAnswerStyle: (effort: AskEffort, body: string) =>
    invoke<AnswerStyleSaved>("set_answer_style", { effort, update: { body } }),
  ask: (question: Question) => invoke<AskStarted>("ask", { question }),

  /** Collect a question started earlier. Safe to call repeatedly; the API keeps
   *  the answer rather than consuming it on the first read. */
  askResult: (questionId: string) =>
    invoke<AskProgress>("ask_result", { questionId }),

  // -- conversations --------------------------------------------------------
  /** Open a conversation. Must precede any turn: the turn and relay tables
   *  derive their tenant from this row, so a turn naming a conversation that
   *  does not exist writes nothing at all. */
  chatCreate: (libraryId: string) =>
    invoke<ConversationStarted>("chat_create", { request: { libraryId } }),
  chatList: () => invoke<Conversations>("chat_list"),
  /** The whole transcript. Read from the catalog rather than from Temporal,
   *  which is what makes a conversation outlive a session's retention. */
  chatRead: (conversationId: string) =>
    invoke<ConversationDetail>("chat_read", { conversationId }),
  /** Irreversible, and free. The screen confirms before calling this. */
  chatDelete: (conversationId: string) =>
    invoke<void>("chat_delete", { conversationId }),
  /** Ask the next question. Returns as soon as the turn has a number, not when
   *  it has an answer — `chatStream` follows that. */
  chatTurn: (conversationId: string, text: string, effort?: AskEffort) =>
    invoke<TurnStarted>("chat_turn", {
      conversationId,
      // `effort` omitted rather than sent as null when absent: omitting is how
      // the payload says "whatever the server's default is", and a null would
      // be a value the server has to interpret.
      request: effort ? { text, effort } : { text },
    }),
  /**
   * Follow a turn's answer as it is written.
   *
   * `since` is the resume point — pass the highest `seq` already seen and only
   * what follows is sent, so reopening a conversation mid-answer costs nothing
   * and never shows the same text twice.
   *
   * Resolves when the stream ends. The `done` event has already been delivered
   * to `onEvent` by then, so a caller does not need the return value for
   * anything but knowing it is over.
   */
  chatStream: (
    conversationId: string,
    turnSeq: number,
    since: number,
    onEvent: (e: ChatEvent) => void,
  ) => {
    const channel = new Channel<ChatEvent>();
    channel.onmessage = onEvent;
    return invoke<void>("chat_stream", {
      conversationId,
      turnSeq,
      since,
      onEvent: channel,
    });
  },

  // -- the Library's three verbs --------------------------------------------
  documentDetail: (libraryId: string, documentId: string) =>
    invoke<DocumentDetail>("document_detail", { libraryId, documentId }),
  /** Irreversible, and free. The screen confirms before calling this. */
  documentRemove: (libraryId: string, documentId: string) =>
    invoke<Removal>("document_remove", { libraryId, documentId }),
  versionRemove: (libraryId: string, versionId: string) =>
    invoke<Removal>("version_remove", { libraryId, versionId }),
  /** Make an already-indexed version the one questions see. Free.
   *
   *  Two things reach this. An ingest that found a structural collision indexed
   *  everything and withheld only the promotion, because the fingerprint that
   *  picks a family profile is structural and structure is not subject matter —
   *  and the retrieval metrics cannot see the difference, since the eval
   *  questions come from the very chunks the wrong rules produced. And a bad
   *  re-index can be rolled back by flipping the flag rather than paying for the
   *  pipeline again. */
  versionActivate: (libraryId: string, versionId: string) =>
    invoke<Activation>("version_activate", { libraryId, versionId }),
  /** Re-runs the whole pipeline; arrives at the normal gate, which re-quotes. */
  documentReindex: (libraryId: string, documentId: string,
                    options: StageOptions = DEFAULT_STAGES) =>
    invoke<StartedRun>("document_reindex", { libraryId, documentId, options }),
  /** Replays artifacts already paid for; only embedding can spend. */
  documentRebuild: (libraryId: string, documentId: string) =>
    invoke<StartedRun>("document_rebuild", { libraryId, documentId }),
  /** null while the artifacts are still being read — a normal first answer. */
  rebuildGate: (workflowId: string) =>
    invoke<RebuildReport | null>("rebuild_gate", { workflowId }),

  backendSettings: () => invoke<BackendInfo>("backend_settings"),
  /**
   * Opens the hosted sign-in in the system browser and waits for it. The whole
   * exchange happens in Rust: the PKCE verifier must not reach the webview,
   * which is the point of the flow.
   */
  signIn: () => invoke<BackendInfo>("sign_in"),
  signOut: () => invoke<BackendInfo>("sign_out"),
  setBackendMode: (mode: BackendMode, baseUrl: string, tenantId: string) =>
    invoke<BackendInfo>("set_backend_mode", { mode, baseUrl, tenantId }),
  providerSettings: () => invoke<ProviderSettings>("provider_settings"),
  /** Compose interpolates `.env` at `up` time, so this reaches the containers on
   *  the next `stackUp` and not before. The screen has to say so. */
  setProviderProject: (projectId: string) =>
    invoke<ProviderSettings>("set_provider_project", { projectId }),

  // -- explore --------------------------------------------------------------
  exploreOutline: (versionId: string) =>
    invoke<Outline>("explore_outline", { versionId }),
  exploreSectionChunks: (sectionId: string) =>
    invoke<SectionChunks>("explore_section_chunks", { sectionId }),
  exploreChunkContext: (chunkId: string) =>
    invoke<ChunkContext>("explore_chunk_context", { chunkId }),
  exploreConcepts: (versionId: string) =>
    invoke<VersionConcepts>("explore_concepts", { versionId }),
  exploreRelated: (versionId: string) =>
    invoke<RelatedDocuments>("explore_related", { versionId }),
  exploreClaims: (conceptId: string) =>
    invoke<ConceptClaims>("explore_claims", { conceptId }),

  /** What the whole installation holds. Not library-scoped: its figures are
   *  project-wide, which is why Inicio owns no library picker. */
  projectSummary: () => invoke<ProjectSummary>("project_summary"),
  /** A whole library as nodes and weighted edges.
   *
   *  `minDocuments` is what controls the volume — see `LibraryGraph`. 1 returns
   *  every concept the library mentions; 2 returns the subgraph that has edges
   *  between books at all, which measured 2,034 concepts and was still an
   *  unreadable canvas; 3 is the default and measured 858. */
  libraryGraph: (libraryId: string, confidenceFloor = 0.6, minDocuments = 3) =>
    invoke<LibraryGraph>("library_graph", {
      libraryId,
      confidenceFloor,
      minDocuments,
    }),

  /**
   * Start the stack, reporting progress as it goes.
   *
   * A cold start pulls images and waits on Postgres and Temporal schema
   * creation, which can run for minutes with nothing on screen — hence the
   * channel rather than a bare promise.
   */
  stackUp: (onEvent: (e: StackEvent) => void) => {
    const channel = new Channel<StackEvent>();
    channel.onmessage = onEvent;
    return invoke<StackStatus>("stack_up", { onEvent: channel });
  },
};
