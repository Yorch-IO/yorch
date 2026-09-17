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
  // The bundled yt-dlp's own failures, carried rather than mapped: these are
  // the names the *worker* already publishes for the same facts, so one
  // guidance map serves both and the two cannot drift into saying different
  // things about one situation.
  //
  // `youtube_refused_this_machine` is deliberately *not* the worker's
  // `youtube_refused_this_host`. That one means the server was refused and the
  // remedy is what this whole path is; being refused here means the remedy has
  // already been tried, and the advice has to be different.
  | "ytdlp_missing"
  | "youtube_refused_this_machine"
  | "youtube_timed_out"
  | "youtube_unreadable"
  | "video_unavailable"
  | "video_is_live"
  | "video_has_no_duration"
  | "audio_unavailable"
  // The bundled whisper.cpp's own failures, carried for the same reason as
  // yt-dlp's above: each names a different remedy, and a single
  // "transcription failed" would send every one of them to the same dead end.
  // `whisper_missing` is fixed by running a script, `whisper_model_missing` by
  // pressing download, `whisper_model_corrupt` by pressing it again, and
  // `whisper_container_unsupported` by transcribing that run on Amazon.
  | "whisper_missing"
  | "whisper_model_unknown"
  | "whisper_model_missing"
  | "whisper_model_corrupt"
  | "whisper_download_failed"
  | "whisper_audio_failed"
  | "whisper_container_unsupported"
  | "whisper_failed"
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
  | "effort_not_found"
  /** The record this machine resolved does not match the link it was sent
   *  with. Only reachable by a hand-built request from this app's own
   *  `video_start`, which builds both from one URL. */
  | "resolution_not_trusted"
  /** Audio in a container Amazon Transcribe cannot read. */
  | "audio_format_unsupported"
  /** A downloadable artifact this run does not have, or a kind that is not
   *  downloadable at all. The allowlist is one entry on both planes. */
  | "artifact_not_found"
  /** The file no longer hashes to what the catalog recorded. Refused rather
   *  than served: a file that changed under its reference is not the artifact
   *  the run produced. */
  | "artifact_changed"
  /** A version whose chunks are gone, so there is nothing to compose a book
   *  from. The remedy is a re-index, not a retry. */
  | "epub_source_missing"
  /** A link that is not a YouTube channel — including a link to one of its
   *  *videos*, which is a different request with a different screen. */
  | "not_a_channel_url"
  /** No YouTube Data API key on this installation. A 503 rather than an empty
   *  catalogue, because "this channel has no videos" and "nobody here can ask
   *  about this channel" are different facts. */
  | "youtube_key_missing"
  /** The day's Data API allowance is gone. Distinct from `youtube_refused`
   *  because the remedies are opposite: this one is fixed by waiting, that one
   *  by looking at the key. */
  | "youtube_quota_exceeded"
  | "youtube_refused"
  | "youtube_unreachable"
  /** A channel this installation has not catalogued yet. */
  | "channel_not_synced"
  /** A library id that belongs to another organisation. */
  | "library_owned_by_another"
  /** A topic pass asked for with no probed videos to read. */
  | "no_videos_to_read"
  // A customer's S3 bucket, paid plane only. Forked from the paid plane's
  // `ControlErrorKind`, which forks `brainworker.s3source._translate`.
  /** A bucket name, role ARN or manifest mapping the reader refuses. */
  | "bucket_source_invalid"
  /** STS refused to assume the customer's role: the trust policy or the ExternalId. */
  | "bucket_role_refused"
  /** The role was assumed and S3 refused: a permission the role lacks. */
  | "bucket_forbidden"
  | "bucket_not_found"
  | "bucket_wrong_region"
  | "bucket_session_expired"
  | "bucket_unavailable"
  | "bucket_not_registered"
  | "object_not_found"
  | "object_too_large"
  | "object_empty"
  | "object_changed_since_quote"
  | "manifest_unreadable"
  | "audio_container_unknown"
  | "audio_too_long"
  | "audio_too_large"
  | "no_disk_for_audio"
  /** No Transcribe region or bucket on this deployment; the quote cannot be made. */
  | "transcribe_not_configured"
  /** A document that is not a bucket recording, asked for a media link. */
  | "media_not_available";

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
  ytdlp_missing: "error.ytdlpMissing",
  youtube_refused_this_machine: "error.youtubeRefusedThisMachine",
  youtube_timed_out: "error.youtubeTimedOut",
  video_unavailable: "error.videoUnavailable",
  video_is_live: "error.videoIsLive",
  audio_unavailable: "error.audioUnavailable",
  whisper_missing: "error.whisperMissing",
  whisper_model_missing: "error.whisperModelMissing",
  whisper_model_corrupt: "error.whisperModelCorrupt",
  whisper_download_failed: "error.whisperDownloadFailed",
  whisper_audio_failed: "error.whisperAudioFailed",
  whisper_container_unsupported: "error.whisperContainerUnsupported",
  whisper_failed: "error.whisperFailed",
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
  resolution_not_trusted: "error.resolutionNotTrusted",
  audio_format_unsupported: "error.audioFormatUnsupported",
  artifact_not_found: "error.artifactNotFound",
  artifact_changed: "error.artifactChanged",
  epub_source_missing: "error.epubSourceMissing",
  not_a_channel_url: "error.notAChannelUrl",
  youtube_key_missing: "error.youtubeKeyMissing",
  youtube_quota_exceeded: "error.youtubeQuotaExceeded",
  youtube_refused: "error.youtubeRefused",
  youtube_unreachable: "error.youtubeUnreachable",
  channel_not_synced: "error.channelNotSynced",
  library_owned_by_another: "error.libraryOwnedByAnother",
  bucket_source_invalid: "error.bucketSourceInvalid",
  bucket_role_refused: "error.bucketRoleRefused",
  bucket_forbidden: "error.bucketForbidden",
  bucket_not_found: "error.bucketNotFound",
  bucket_wrong_region: "error.bucketWrongRegion",
  bucket_not_registered: "error.bucketNotRegistered",
  object_changed_since_quote: "error.objectChangedSinceQuote",
  audio_container_unknown: "error.audioContainerUnknown",
  audio_too_long: "error.audioTooLong",
  transcribe_not_configured: "error.transcribeNotConfigured",
  media_not_available: "error.mediaNotAvailable",
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

/**
 * One step of getting a video ready, before the run exists.
 *
 * In cloud mode `videoStart` makes the two YouTube calls on this machine, and
 * the second of them — only for a video with no captions at all — downloads
 * the audio. Nothing is on screen for that yet: the run row is written by the
 * workflow, which has not started. So these events are the only thing between
 * pressing the button and the queue appearing.
 *
 * `total` is yt-dlp's own figure and is **0 when it did not offer one**, which
 * is a different fact from "nothing to download" and must render as no
 * percentage rather than as 0% — the rule `/project-summary` applies to a leg
 * it could not ask, at the scale of one number.
 */
export interface VideoFetchEvent {
  step: "resolving" | "downloading" | "uploading" | "starting";
  bytes?: number;
  total?: number;
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
  /** Package what was indexed as an EPUB a person can read on a device.
   *
   *  Nearly free: the packaging costs nothing and the one call it can make — a
   *  title and an author for a document the catalog has neither for — runs once
   *  per document ever, and never at all for a video, whose author is the
   *  channel. */
  buildEpub: boolean;
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
  buildEpub: false,
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

/** The four narrowings, as a screen holds them. Same spelling as `Question`. */
export interface AskNarrowings {
  recorded_from?: string;
  recorded_to?: string;
  scripture?: string;
  source_name?: string;
}

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
  /** Narrowings a recording corpus makes askable. All optional; empty is "no
   *  filter", and Rust omits the key. The dates are `YYYY-MM-DD`; the
   *  scripture value is a reference or a chapter (`Juan 3:16`, `Romanos 8`),
   *  normalised by the worker; `source_name` is the feed or folder a recording
   *  came from. Each is a hard cut over what the dense floor admits. */
  recorded_from?: string;
  recorded_to?: string;
  scripture?: string;
  source_name?: string;
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
  /** The document the chunk belongs to — what `mediaLink` is addressed by.
   *  Optional because a shell older than this field does not forward it. */
  documentId?: string;
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
  /** A recording's dates, feed link and source, as its manifest said. All
   *  null for a book. `recordedAt` is what the date filter narrows on. */
  recordedAt?: string | null;
  publishedAt?: string | null;
  sourceUrl?: string | null;
  sourceName?: string | null;
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
  /** The run to fetch this version's book from, or null when it has none yet.
   *  A run id rather than a flag, because the download is addressed by run. */
  epubRunId: string | null;
  /** Other documents holding these same bytes; non-empty means removing this
   *  document leaves the version standing. */
  alsoHeldBy: string[];
  /** What this version's index can be asked, from the newest run that measured
   *  it. null means nobody measured — which is every version indexed before the
   *  stage existed, and is not the same claim as a recall of zero. */
  scores: RunScores | null;
}

/** What a standalone book build produced. */
export interface BookBuilt {
  versionId: string;
  /** What the download is addressed by. */
  runId: string;
  artifact: string;
  bytes: number;
  title: string;
  author: string | null;
  /** What the metadata call cost, or null when it was not made — the ordinary
   *  case, because it runs once per document ever. */
  usd: number | null;
}

/** What a person may correct about a document. An omitted field is left
 *  alone; an empty author is how somebody says the document has none. */
export interface DocumentMetadata {
  title?: string;
  author?: string;
  /** `YYYY-MM-DD`, or an empty string to clear. Absent means "leave alone". */
  recordedAt?: string;
  publishedAt?: string;
  sourceUrl?: string;
  sourceName?: string;
}

export interface DocumentMetadataResult {
  id: string;
  title: string;
  author: string | null;
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
  /** Whether this version's chunks are still on disk to compose a book from.
   *  The same file `canRebuild` needs, named separately because the two verbs
   *  are not the same act. */
  canBuildEpub: boolean;
  versions: VersionRow[];
  recordedAt?: string | null;
  publishedAt?: string | null;
  sourceUrl?: string | null;
  sourceName?: string | null;
}

/**
 * One version's statistics, in five legs that fail independently.
 *
 * **Every figure is optional because `available: false` carries none of them.**
 * That is the contract, not defensive typing: a stopped Memgraph must render as
 * "could not ask" and never as a version with no concepts, and a run nobody
 * measured must never render as a recall of zero. A required field here would
 * force a default at the one layer that could invent one.
 */
export interface StatLeg {
  available: boolean;
  /** Why it could not answer — and for a store, the URL that was tried. */
  detail: string;
}

export interface VersionProfileWarning {
  profileId: string | null;
  collidesWith: string | null;
  similarity: number | null;
  detail: string | null;
  /** `_topical_overlap` returns 0 by construction for a plain-text document,
   *  and 0 is the *most dangerous* case — same structure, unrelated subject
   *  matter. false means the figure beside it is not a measurement. */
  comparable: boolean;
}

export interface VersionCatalogLeg extends StatLeg {
  contentSha256?: string | null;
  byteSize?: number | null;
  /** A column nothing writes: `register_version` runs before extraction. null
   *  is the measurement, and it starts working the day something fills it. */
  pageCount?: number | null;
  state?: string | null;
  active?: boolean | null;
  createdAt?: string | null;
  activatedAt?: string | null;
  failedReason?: string | null;
  runs?: number | null;
  rebuildRunId?: string | null;
  profileWarnings?: VersionProfileWarning[];
}

export interface VersionGraphLeg extends StatLeg {
  chunks?: number | null;
  sections?: number | null;
  citations?: number | null;
  claims?: number | null;
  /** Keyed by the chunk kind as stored — `cuerpo`, `preguntas`, `nota`. They
   *  stay Spanish on the wire because they are Qdrant payload values used in
   *  filters; the UI maps them to localised labels. */
  kinds?: Record<string, number>;
  sectionLevels?: Record<string, number>;
}

export interface VersionQdrantLeg extends StatLeg {
  points?: number | null;
}

export interface VersionStreamScore {
  bytes: number;
  spansVerified: number;
  spansMismatched: number;
}

export interface VersionSpans {
  chunks: number;
  spansVerified: number;
  spansMismatched: number;
  bytes: number;
}

export interface VersionSequence {
  indices: number;
  contiguous: boolean;
  missingIndices: number[];
  duplicateIndices: number[];
  bytesCovered: number;
  bytesTotal: number;
  coverage: number;
}

export interface VersionArtifactsLeg extends StatLeg {
  /** Which stream the `char_span`s index. Nothing records it, so it is chosen
   *  by scoring every stream present — on one real version `raw.txt` verified
   *  8 of 600 spans where `extracted.txt` verified 600. */
  stream?: string | null;
  streamVerifiesCompletely?: boolean | null;
  streamsConsidered?: Record<string, VersionStreamScore>;
  spans?: VersionSpans | null;
  sequence?: VersionSequence | null;
}

export interface VersionStructureLeg extends StatLeg {
  sourceRun?: string | null;
  graph: VersionGraphLeg;
  qdrant: VersionQdrantLeg;
  artifacts: VersionArtifactsLeg;
  /** null is "could not compare"; only false is the claim that they disagree. */
  countsAgree?: boolean | null;
}

export interface VersionSemanticsInStore {
  claims: number;
  /** Claims carrying a quote the code located in their own chunk. One nobody
   *  can check must not look like one that can. */
  withAQuote: number;
  /** `afirma` / `niega` / `atribuido` / `sin_estado`, and the last is never
   *  folded into the first: a text expounding the doctrine it is about to rebut
   *  enunciates it in the same words as one who holds it. */
  byStatus: Record<string, number>;
  concepts: number;
}

export interface VersionStaleDiff {
  produced: number;
  inStore: number;
  converged: number;
  /** What the graph holds and this run did not make. Not inert: a stale claim
   *  stays attached to a chunk whose text has moved. */
  leftBehind: number;
  /** Its mirror — a projection that did not finish. */
  missing: number;
  staleShare: number | null;
  /** Concepts only: the names extracted before `canonical_concept` folds them,
   *  so the fold does not read as a loss. */
  namesExtracted?: number | null;
}

export interface VersionSemanticsDiff extends StatLeg {
  claims?: VersionStaleDiff | null;
  concepts?: VersionStaleDiff | null;
  mentions?: VersionStaleDiff | null;
  staleClaimQuotes?: { withAQuote: number; quoteNoLongerLocates: number } | null;
}

export interface VersionSemanticsLeg extends StatLeg {
  sourceRun?: string | null;
  extractorModel?: string | null;
  inStore?: VersionSemanticsInStore | null;
  diff?: VersionSemanticsDiff | null;
}

export interface VersionFloor {
  minScore: number;
  noiseFloor: number;
  headroom: number;
  /** false means the floor admits exactly what it was measured to exclude. */
  honest: boolean;
}

export interface VersionRetrievalLeg extends StatLeg {
  sourceRun?: string | null;
  scores?: RunScores | null;
  floor?: VersionFloor | null;
}

export interface VersionStageCost {
  usd: number;
  /** Every run that charged this stage. More than one is the finding. */
  runs: string[];
  /** A missing price under-reports the bill rather than describing a free call,
   *  so it is counted and never totalled as zero. */
  unpricedEntries: number;
}

export interface VersionLedgerLeg extends StatLeg {
  byStage?: Record<string, VersionStageCost>;
  totalUsd?: number;
  chargedInMoreThanOneRun?: string[];
  /** Split by the terminal state of the run that incurred it. A split rather
   *  than one figure called "wasted": a cancelled run bought nothing durable,
   *  but a failed one can still have left a complete index behind. */
  usdByRunState?: Record<string, number>;
}

export interface VersionStatistics {
  libraryId: string;
  documentId: string;
  versionId: string;
  catalog: VersionCatalogLeg;
  structure: VersionStructureLeg;
  semantics: VersionSemanticsLeg;
  retrieval: VersionRetrievalLeg;
  ledger: VersionLedgerLeg;
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

/* -- buckets ---------------------------------------------------------------
 *
 * Audio out of a customer's own S3 bucket. **Paid plane only**: in local mode
 * every one of these calls answers with the 404 the proxy already turns into
 * `control_status`, and the tab says so rather than rendering an empty list.
 */

/** Where a customer's audio lives and how the plane may read it.
 *
 *  Spelled snake_case like `Question`, and for a stronger reason: it travels
 *  *in* on a register and *out* inside every `StoredBucket`, and the Rust
 *  struct can rename in one direction only. Nothing in it is a secret — the
 *  role is the customer's, and its trust policy is the gate. */
export interface BucketSource {
  bucket: string;
  prefix: string;
  role_arn: string;
  region: string;
  /** Where the raw transcript is written back into the customer's bucket.
   *  Empty switches the write-back off. */
  archive_prefix: string;
  manifest_key: string;
  /** Which manifest column means what: `file`, `title`, `author`, `recorded`,
   *  `published`, `source`, `url` → column name, or for `file` a template
   *  like `{folder}/{filename}`. */
  manifest_map: Record<string, string>;
  language: string;
}

export const MANIFEST_FIELDS = [
  "file",
  "title",
  "author",
  "recorded",
  "published",
  "source",
  "url",
] as const;

export interface StoredBucket {
  bucketId: string;
  source: BucketSource;
  libraryId: string;
  libraryName: string;
  syncedAt: string;
  objectCount: number;
  complete: boolean;
  /** Objects whose duration was estimated from size rather than read. */
  estimated: number;
  manifestRows: number;
  unmatchedRows: number;
  unmatchedObjects: number;
  warnings: string[];
}

/** One object as the catalogue holds it, joined with what the catalog says.
 *
 *  `state` comes from Postgres on every read — `indexed`, `pending` or
 *  `unindexed` — never from the catalogue file. `durationEstimated` is the
 *  one figure the quote is least sure of, and the row says so. */
export interface BucketObjectRow {
  key: string;
  etag: string;
  size: number;
  lastModified: string;
  /** What the bytes say: `mp3`, `mp4`, … or "" when they say nothing. */
  container: string;
  durationS: number;
  durationEstimated: boolean;
  title: string;
  author: string;
  recordedAt: string;
  publishedAt: string;
  source: string;
  url: string;
  available: boolean;
  warnings: string[];
  documentId: string | null;
  activeVersionId: string | null;
  runId: string | null;
  runState: string | null;
  state: "indexed" | "pending" | "unindexed";
}

export interface BucketTotals {
  objects: number;
  seconds: number;
  indexed: number;
  pending: number;
  unindexed: number;
}

export interface BucketDetail {
  bucket: StoredBucket;
  objects: BucketObjectRow[];
  totals: BucketTotals;
}

export interface BucketList {
  buckets: StoredBucket[];
}

export interface BucketSynced {
  bucketId: string;
  libraryId: string;
  objects: number;
  added: number;
  changed: number;
  absent: number;
  estimated: number;
  manifestRows: number;
  unmatchedRows: number;
  unmatchedObjects: number;
  warnings: string[];
}

export interface BucketRegistered {
  bucket: StoredBucket | null;
  synced: BucketSynced;
}

export interface BucketProbeResult {
  /** `transcriber` is what the run actually started on, which is not always
   *  what was ticked: an object in a container this machine cannot decode is
   *  started on Amazon whatever the batch asked for, and the answer says so
   *  per key rather than leaving a run parked for a transcript nobody can
   *  make. */
  started: { key: string; workflowId: string; transcriber: Transcriber }[];
  failed: { key: string; kind: string; message: string }[];
}

/** Who transcribes: Amazon, on the worker, or whisper.cpp on this machine. */
export type Transcriber = "transcribe" | "local";

/* -- transcribing here -----------------------------------------------------
 *
 * The same argument as the YouTube calls this app already makes, arriving from
 * the other direction: there the server cannot make a call this machine can,
 * and here the server has no GPU and this machine may. Transcription is the
 * one paid stage of a bucket run that dwarfs every other — about $233 for the
 * first 147-recording corpus — so a person with a GPU can trade hours for it.
 */

/** One Whisper model as the app can offer it, with whether it is downloaded. */
export interface WhisperModelState {
  name: string;
  file: string;
  bytes: number;
  /** What the size buys, in one line: the trade, not the architecture. */
  note: string;
  present: boolean;
  path: string;
}

/** What whisper.cpp itself said it runs on, and how that was learned.
 *
 *  Distinct from `WhisperStatus.backend`, which is what the *build* looks
 *  capable of. A binary that links cuBLAS and finds no device — an old driver,
 *  a laptop that parks its discrete card, a container with no `/dev/nvidia*` —
 *  hints `cuda` and runs on the processor at a fraction of the speed. */
export interface DeviceFact {
  /** `cuda`, `vulkan`, `metal`, `blas` or `cpu`. */
  backend: string;
  /** What whisper.cpp called it: `CUDA0`, `Metal`, `CPU`. */
  device: string;
  /** `probe` — a model loaded and nothing transcribed — or `run`, which is a
   *  whole recording and the stronger evidence. */
  how: "probe" | "run";
  model: string;
  at: string;
}

export interface WhisperStatus {
  /** Whether this build carries the transcriber at all. */
  installed: boolean;
  /** The bundled binary, or null with `error` saying why there is none. */
  binary: string | null;
  error: string | null;
  /** `cuda`, `vulkan`, `metal`, `cpu`, or `none` when there is no binary. A
   *  guess from the build; `device` is the answer to "GPU or CPU?". */
  backend: string;
  /** What the binary said it actually uses, or null until something asked. */
  device: DeviceFact | null;
  /** Whether the question can be settled at all: whisper.cpp names its device
   *  while loading a model, so with none downloaded there is nothing to ask. */
  canProbe: boolean;
  models: WhisperModelState[];
  defaultModel: string;
  modelsDir: string;
  /** Audio seconds per wall second on the last run, or null before one. This
   *  is what turns "147 recordings" into a number of hours on this machine. */
  measuredSpeed: number | null;
  measuredOn: string | null;
  decodable: string[];
}

/** One line of progress: bytes for `download`, `verify` and `audio`, percent
 *  for `transcribe`. */
export interface WhisperProgress {
  phase: "download" | "verify" | "audio" | "transcribe";
  done: number;
  total: number;
  message: string;
}

export interface Transcribed {
  path: string;
  engine: string;
  model: string;
  language: string;
  backend: string;
  audioSeconds: number;
  wallSeconds: number;
  segments: number;
}

export interface TranscriptUploaded {
  workflowId: string;
  state: string;
}

/** Giving up on a local transcript parks the run at a gate again, with
 *  Amazon's price on it — never "running", because nobody has approved it. */
export interface TranscriberSwitched {
  workflowId: string;
  state: string;
  transcriber: Transcriber;
}

/** A presigned link to a recording at a second. Minted on click and never
 *  stored — it dies with the session that signed it — so `sourceUrl`, the
 *  public feed's own link, rides beside it. */
export interface MediaLink {
  url: string;
  expiresAt: string;
  startS: number;
  sourceUrl: string;
}

/* -- channels --------------------------------------------------------------
 *
 * A YouTube channel, catalogued so a person can decide which of its videos are
 * worth paying to index. A channel **is** a library: retrieval narrows by
 * equality on `libraryId`, so "ask only this channel" is "ask this library",
 * and every other arrangement breaks that.
 */

export interface ChannelRef {
  channelId: string;
  title: string;
  handle: string;
  description: string;
  uploadsPlaylistId: string;
  url: string;
}

export interface ChannelSummary {
  channel: ChannelRef;
  libraryId: string;
  syncedAt: string;
  videoCount: number;
  /** Data API quota units the last sync spent. The daily allowance is the one
   *  resource here that runs out, so saying what a sync cost beats discovering
   *  it at the end of the day. */
  unitsSpent: number;
  /** Whether a sync has ever walked the uploads playlist to its end. "The most
   *  recent 500" and "all 2,000" are different catalogues, and the screen says
   *  which one it is showing. */
  complete: boolean;
  /** Only on a sync's own answer: what that sync did. */
  fetched?: number;
  added?: number;
  stoppedEarly?: boolean;
  unavailable?: number;
  /** Only on the listing, and from the *catalog* — not from the channel file.
   *  What a channel holds and what is indexed are different questions with
   *  different owners. */
  documents?: number;
  indexedVersions?: number;
}

export interface ChannelList {
  channels: ChannelSummary[];
}

export interface ChannelVideoRow {
  videoId: string;
  title: string;
  description: string;
  descriptionTruncated: boolean;
  publishedAt: string;
  durationS: number;
  /** `none`, `live` or `upcoming`. An upcoming premiere is refused by the video
   *  path anyway, so it is never offered. */
  liveState: string;
  thumbnail: string;
  url: string;
  /** null means this channel's library holds no document for the video. */
  documentId: string | null;
  /** null on a document that exists is a real state, not a gap: a run that was
   *  cancelled, or an activation withheld over a structural mismatch. */
  activeVersionId: string | null;
  /** false once a complete re-sync did not meet this id on the playlist —
   *  deleted, or made private. Kept in the list because it may be indexed;
   *  never offered to probe. */
  available: boolean;
}

export interface ChannelDetail {
  channel: ChannelRef;
  libraryId: string;
  syncedAt: string;
  videoCount: number;
  unitsSpent: number;
  complete: boolean;
  videos: ChannelVideoRow[];
}

export interface DiscoveryQuote {
  channelId: string;
  libraryId: string;
  topic: string;
  evaluated: number;
  read: number;
  /** Videos in the read set with no duration, which cannot be quoted. Reported
   *  rather than priced at nothing: a zero in a bill is a claim. */
  unmeasured: number;
  estimate: Estimate;
}

/** One video's verdict from the metadata pass.
 *
 *  `relevancia` is Spanish on the wire like the chunk kinds and a claim's
 *  status, and for the same reason: these strings are in artifacts already.
 *  `sin_evaluar` is the code's, not the model's — a video whose verdict did not
 *  come back or did not survive checking. Treating it as `descartado` would
 *  hide a model quietly answering about fewer videos than it was asked about. */
export interface Candidate {
  video_id: string;
  relevancia: "relevante" | "dudoso" | "descartado" | "sin_evaluar";
  puntaje: number;
  razon: string;
  incertidumbre: string;
}

export interface Preselection {
  topic: string;
  model: string;
  prompt_version: string;
  evaluated: string[];
  candidates: Candidate[];
  /** Verdicts naming a video that was never sent. Non-zero means the model
   *  invented identifiers, which is worth knowing before trusting its verdicts. */
  invented: number;
  malformed: number;
  unevaluated: number;
}

/** One topic the transcript pass found, with the sentence that says so.
 *
 *  `evidencia` is empty when the quotation the model gave is **not** in the
 *  transcript. That is a reading to treat as the model's impression rather than
 *  as something the talk says, and the two must not look the same on screen. */
export interface Topic {
  tema: string;
  evidencia: string;
  confianza: string;
}

export interface VideoTopics {
  video_id: string;
  topic: string;
  model: string;
  prompt_version: string;
  temas: Topic[];
  responde: boolean;
  motivo: string;
  characters: number;
  truncated: boolean;
  /** Of `temas.length`. A reading nobody can check must not look like one that
   *  can, so the count travels rather than being inferred. */
  verified: number;
  /** A call that failed: the video was never read, which is different from a
   *  video that was read and found irrelevant. */
  failed: boolean;
}

export interface TopicsRecord {
  topic: string;
  channel_id: string;
  prompt_version: string;
  videos: VideoTopics[];
}

/** What a discovery or a topic run concluded, read back from its artifacts.
 *
 *  `null` in any of the three is a state, not a gap: a run still preselecting
 *  has no topics, and one that failed before quoting has no estimate. */
export interface ChannelReading {
  channelId: string;
  workflowId: string;
  state: string | null;
  stage: string | null;
  usdSoFar: number | null;
  estimate: Estimate | null;
  preselection: Preselection | null;
  topics: TopicsRecord | null;
}

/** One descriptive statement about what was preached, and the citations that
 *  survived checking. A finding that named none was **moved to `limitaciones`**
 *  before this crossed the wire, never deleted. */
export interface Finding {
  afirmacion: string;
  chunkIds: string[];
}

/** One reading, labelled as a reading. Never merged with a finding: the thing a
 *  reader must treat sceptically may not share a paragraph with the thing they
 *  may rely on. */
export interface Inference {
  inferencia: string;
  alcance: string;
  limites: string;
}

export interface Comparison {
  convergencias: string[];
  diferencias: string[];
  matices: string[];
}

export interface Synthesis {
  /** `answered`, `insufficient_evidence` or `off_corpus`. The last two differ
   *  because their remedies do: one means the corpus was searched and came up
   *  short, the other that the topic belongs elsewhere. */
  state: string;
  topic: string;
  model: string;
  promptVersion: string;
  hallazgos: Finding[];
  comparacion: Comparison;
  interpretacionTeologica: Inference[];
  citas: Citation[];
  limitaciones: string[];
  evidence: EvidenceItem[];
  /** Findings moved to `limitaciones` for naming no citation that survived. A
   *  synthesis where half of them were demoted is one to distrust. */
  demoted: number;
  invented: number;
  /** Why, when `state` is not `answered`. The only thing that says *which*
   *  refusal this was. */
  reason: string;
  spend: Spend[];
}

export interface SynthesisResult {
  questionId: string;
  state: string;
  synthesis: Synthesis | null;
  error: Record<string, string> | null;
}

/** Whether a provider credential is stored, and where. **Never what it is.**
 *
 *  A value the UI could read back is a value a screenshot can leak, and nothing
 *  above Rust has a reason to hold a key. `store` is a token rather than prose
 *  so the wording lives in the two bundles, the way an error `kind` does. */
export interface ProviderSecret {
  name: string;
  env: string;
  stored: boolean;
  store: string;
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
  /**
   * Start indexing a video. No staging call precedes this: there is no file.
   *
   * **In cloud mode Rust asks YouTube from this machine first**, because the
   * server is refused — measured 2026-09-05, "Sign in to confirm you're not a
   * bot" from the EC2 egress address against 2.6 s from a residential one. So
   * this call is no longer instant: resolving is a few seconds and, for a
   * video with no captions at all, downloading its audio is minutes.
   * `onFetch` is how a window says so instead of sitting still; it is optional
   * and the channel is created either way, because Tauri cannot build an
   * `Option<Channel<_>>` argument.
   */
  videoStart: (
    request: VideoRequest,
    options: StageOptions = DEFAULT_STAGES,
    onFetch?: (event: VideoFetchEvent) => void,
  ) => {
    const channel = new Channel<VideoFetchEvent>();
    if (onFetch) channel.onmessage = onFetch;
    return invoke<StartedRun>("video_start", { request, options, onEvent: channel });
  },
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

  /* -- buckets ----------------------------------------------------------- */

  /** Register a customer's bucket and catalogue it. Free — S3 requests only —
   *  and awaited: seconds for a hundred objects, so the answer is the count. */
  bucketRegister: (source: BucketSource, libraryName = "", libraryId = "") =>
    invoke<BucketRegistered>("bucket_register", { source, libraryName, libraryId }),
  buckets: () => invoke<BucketList>("buckets"),
  bucketDetail: (bucketId: string, stateFilter = "all") =>
    invoke<BucketDetail>("bucket_detail", { bucketId, stateFilter }),
  /** Walk the listing again. Free; reads no known object twice. */
  bucketSync: (bucketId: string) => invoke<BucketRegistered>("bucket_sync", { bucketId }),
  /** Quote the ticked objects: one `audio` run per key, each parked at its own
   *  gate. Free. The screen sums the quotes and approves each through
   *  `ingestApprove`, unchanged. */
  bucketProbe: (
    bucketId: string,
    keys: string[],
    options: StageOptions = DEFAULT_STAGES,
    reindex = false,
    transcriber: Transcriber = "transcribe",
  ) =>
    invoke<BucketProbeResult>("bucket_probe", {
      bucketId,
      keys,
      options,
      reindex,
      transcriber,
    }),
  /** Forget a bucket's catalogue. What was indexed stays. */
  bucketForget: (bucketId: string) =>
    invoke<{ forgotten: boolean }>("bucket_forget", { bucketId }),
  /** An audio run's gate: the same report a video's, on its own route. null
   *  while the object is still being probed. */
  audioGate: (workflowId: string) =>
    invoke<VideoGateReport | null>("audio_gate", { workflowId }),
  /** A presigned link to the recording a document came from, at a second. */
  mediaLink: (libraryId: string, documentId: string, startS = 0) =>
    invoke<MediaLink>("media_link", { libraryId, documentId, startS: Math.floor(startS) }),

  /* -- transcribing here ------------------------------------------------- */

  /** What this machine can do about a transcript, before it is asked to. Free
   *  and local: no control API call, so it answers with the stack down. */
  whisperStatus: () => invoke<WhisperStatus>("whisper_status"),
  /** Ask the binary which device it will use. Free and quick: it loads the
   *  smallest downloaded model, reads the line whisper.cpp prints while doing
   *  it, and stops — no audio is transcribed. */
  whisperProbe: () => invoke<DeviceFact>("whisper_probe"),
  /** Fetch a model, verified against the checksum its publisher's own metadata
   *  carries. Idempotent: a model already there costs two reads and no bytes. */
  whisperDownloadModel: (model: string, onProgress?: (p: WhisperProgress) => void) => {
    const channel = new Channel<WhisperProgress>();
    if (onProgress) channel.onmessage = onProgress;
    return invoke<string>("whisper_download_model", { model, onEvent: channel });
  },
  /** Transcribe one run's recording here. The audio comes down through the
   *  presigned link the plane minted, so this holds no credential. */
  whisperTranscribe: (
    args: {
      workflowId: string;
      audioUrl: string;
      container: string;
      model: string;
      language?: string;
      audioSeconds?: number;
    },
    onProgress?: (p: WhisperProgress) => void,
  ) => {
    const channel = new Channel<WhisperProgress>();
    if (onProgress) channel.onmessage = onProgress;
    return invoke<Transcribed>("whisper_transcribe", { ...args, onEvent: channel });
  },
  /** Hand the transcript to the plane, which signals the parked workflow. The
   *  file is deleted only once it is somewhere else. */
  transcriptUpload: (
    workflowId: string,
    path: string,
    meta: { engine?: string; model?: string; language?: string } = {},
  ) => invoke<TranscriptUploaded>("transcript_upload", { workflowId, path, ...meta }),
  /** Give up on transcribing a run here: it re-quotes on Amazon and parks at
   *  its gate again, so this spends nothing by itself. */
  runSwitchTranscriber: (workflowId: string) =>
    invoke<TranscriberSwitched>("run_switch_transcriber", { workflowId }),

  /* -- channels ---------------------------------------------------------- */

  /** Catalogue a channel. Free: it costs Data API quota, never money.
   *
   *  No limit means the whole playlist, saved a page at a time on the worker
   *  and stopping at the first page it already knows once the catalogue is
   *  complete. `full` walks to the end whatever is known and marks what it
   *  does not meet as unavailable — the only thing that ever does. */
  channelSync: (url: string, options: { limit?: number; full?: boolean } = {}) =>
    invoke<ChannelSummary>("channel_sync", {
      url,
      limit: options.limit,
      full: options.full ?? false,
    }),
  channels: () => invoke<ChannelList>("channels"),
  channelDetail: (channelId: string) =>
    invoke<ChannelDetail>("channel_detail", { channelId }),
  /** What reading this channel would cost. **Free, and it reaches no model.**
   *  Its own call rather than a field on the discovery, because pressing the
   *  button is the decision: the figure has to be on screen before the call
   *  that starts the run, not inside it. */
  channelQuote: (
    channelId: string,
    topic: string,
    limit = 100,
    deepLimit = 10,
    videoIds?: string[],
  ) =>
    invoke<DiscoveryQuote>("channel_quote", { channelId, topic, limit, deepLimit, videoIds }),
  /** **This spends.** Judges the channel's metadata against the topic.
   *
   *  `videoIds` is the screen's title-keyword filter: judge only these. Absent
   *  means the whole catalogue. The quote above takes the same argument, so the
   *  figure shown is the figure approved. */
  channelDiscover: (
    channelId: string,
    topic: string,
    limit = 100,
    deepLimit = 10,
    videoIds?: string[],
  ) =>
    invoke<StartedRun>("channel_discover", { channelId, topic, limit, deepLimit, videoIds }),
  /** **This spends.** Reads the uncorrected transcripts of probed video runs. */
  channelTopics: (channelId: string, topic: string, videoRuns: string[]) =>
    invoke<StartedRun>("channel_topics", { channelId, topic, videoRuns }),
  channelReading: (channelId: string, workflowId: string) =>
    invoke<ChannelReading>("channel_reading", { channelId, workflowId }),

  /** **This spends.** Two calls, like `ask`: the second collects. */
  channelAsk: (channelId: string, topic: string, effort = "thorough") =>
    invoke<AskStarted>("channel_ask", { channelId, topic, effort }),
  channelAskResult: (channelId: string, questionId: string) =>
    invoke<SynthesisResult>("channel_ask_result", { channelId, questionId }),
  /** Write text the page already holds wherever the person says. `null` means
   *  they dismissed the dialog, which is not a failure. */
  saveText: (suggestedName: string, contents: string) =>
    invoke<string | null>("save_text", { suggestedName, contents }),

  providerSecrets: () => invoke<ProviderSecret[]>("provider_secrets"),
  /** Store a provider credential, or clear it with an empty value.
   *
   *  The worker reads the file this writes once at startup, so a key set while
   *  the stack is up reaches it on the next `up`. The screen says so. */
  setProviderSecret: (name: string, value: string) =>
    invoke<ProviderSecret[]>("set_provider_secret", { name, value }),
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
  chatTurn: (
    conversationId: string,
    text: string,
    effort?: AskEffort,
    narrowings: AskNarrowings = {},
  ) =>
    invoke<TurnStarted>("chat_turn", {
      conversationId,
      // `effort` omitted rather than sent as null when absent: omitting is how
      // the payload says "whatever the server's default is", and a null would
      // be a value the server has to interpret. The narrowings follow the
      // same rule: only the ones set travel.
      request: {
        text,
        ...(effort ? { effort } : {}),
        ...(narrowings.recorded_from ? { recordedFrom: narrowings.recorded_from } : {}),
        ...(narrowings.recorded_to ? { recordedTo: narrowings.recorded_to } : {}),
        ...(narrowings.scripture ? { scripture: narrowings.scripture } : {}),
        ...(narrowings.source_name ? { sourceName: narrowings.source_name } : {}),
      },
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
  /** What one version holds, cost, and still agrees with. Reads three stores
   *  and two artifacts, so it is fetched when a person asks rather than with
   *  the detail. Free: nothing behind it writes and nothing behind it spends. */
  versionStatistics: (libraryId: string, versionId: string) =>
    invoke<VersionStatistics>("version_statistics", { libraryId, versionId }),
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
  /** Builds a book for a version already indexed, and waits: it is a file read,
   *  a render and a file write, and the caller's next act is to download it. */
  versionBuildEpub: (libraryId: string, documentId: string, versionId: string) =>
    invoke<BookBuilt>("version_build_epub", { libraryId, documentId, versionId }),
  /** Corrects what the catalog calls a document. An omitted field is left
   *  alone, which is what lets a screen send only what somebody edited. */
  documentUpdate: (libraryId: string, documentId: string,
                   metadata: DocumentMetadata) =>
    invoke<DocumentMetadataResult>("document_update",
                                   { libraryId, documentId, metadata }),
  /** Opens the save dialog, fetches the file and writes it. Resolves to the
   *  path, or to null when the person dismissed the chooser — which is not an
   *  error and must not paint one. */
  artifactSave: (workflowId: string, name: string, suggestedName: string) =>
    invoke<string | null>("artifact_save", { workflowId, name, suggestedName }),
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
