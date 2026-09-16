//! Company Brain — desktop shell.
//!
//! All application logic lives here rather than in `main.rs`: Tauri replaces
//! `main()` on mobile targets, so anything defined there is unreachable in
//! those builds.

mod auth;
mod backend;
mod control;
mod error;
mod keychain;
mod ports;
mod secrets;
mod stack;
mod ytdlp;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;
use tauri::{Manager, State};
use tokio::sync::Mutex;

use auth::{AuthConfig, Pkce, Session};
use backend::{BackendMode, BackendSettings};
use control::{
    AnswerStyleSaved, AnswerStyleUpdate, AnswerStyles,
    Activation, Approval, Auth, AskProgress, AskStarted, BookBuilt,
    ChannelDetail, ChannelList, ChannelReading, ChannelSummary, ChatEvent, ChunkContext,
    DiscoveryQuote, SynthesisResult,
    ConceptClaims,
    Control, ConversationDetail, ConversationStarted, Conversations,
    DocumentDetail, DocumentMetadata, DocumentMetadataResult,
    NewConversation, NewTurn, TurnStarted,
    GateReport, Health, IngestRequest, Libraries, Library, LibraryGraph, Outline, PingResult,
    ProjectSummary, Question, RebuildReport, RelatedDocuments, Removal, RunAudit,
    RunEventPage, RunListPage, SectionChunks,
    RunState, StageOptions, StagedSource, StartedRun, VersionConcepts, VersionStatistics,
    VideoGateReport, VideoRequest,
};
use error::{AppError, Result};
use ports::Ports;
use stack::{DockerInfo, Stack, StackStatus};

/// Shared state. A `tokio::sync::Mutex` rather than `std::sync::Mutex` because
/// every command that touches it awaits a subprocess or an HTTP call while
/// holding it, and a std guard is not `Send` across an await point.
struct AppState {
    stack: Mutex<Option<Arc<Stack>>>,
    data_dir: PathBuf,
    workspace: PathBuf,
}

impl AppState {
    /// The local stack, refusing when this app is not the one that owns it.
    ///
    /// Every caller of `stack()` below is a Docker operation, and Docker is a
    /// requirement of the local backend and of nothing else. They used to call
    /// it unconditionally, so in cloud mode `stack_status` and
    /// `provider_settings` still tried to allocate ports and find a container
    /// runtime — the Services screen swallowed both failures, which is why
    /// nobody noticed, and a user of the paid service on a machine with no
    /// Docker got an error from a subsystem they do not use.
    async fn local_stack(&self) -> Result<Arc<Stack>> {
        if BackendSettings::load(&self.data_dir).mode != BackendMode::Local {
            return Err(AppError::Config(
                "esta operación es del backend local; el servicio de pago no \
                 gestiona contenedores desde la aplicación"
                    .into(),
            ));
        }
        self.stack().await
    }

    /// The prepared stack, laying it out on first use.
    ///
    /// Preparation is deferred rather than done at startup so a machine without
    /// Docker still opens to a window that can explain why, instead of failing
    /// before the webview exists.
    async fn stack(&self) -> Result<Arc<Stack>> {
        let mut guard = self.stack.lock().await;
        if let Some(existing) = guard.as_ref() {
            return Ok(existing.clone());
        }
        let allocated = match Stack::saved_ports(&self.data_dir) {
            Some(saved) => saved,
            None => ports::allocate(Ports::default())?,
        };
        let prepared = Arc::new(Stack::prepare(
            &self.data_dir,
            self.workspace.clone(),
            allocated,
        )?);
        *guard = Some(prepared.clone());
        Ok(prepared)
    }

    /// A client pointed at whichever plane the user chose.
    ///
    /// Read from disk on every call rather than cached. The cached-`Stack`
    /// mistake next door is the reason: a cache that outlives the thing it
    /// describes hands back stale values, and there a stale set of ports made
    /// every command address services that were not there. This file changes
    /// rarely and costs one small read.
    ///
    /// **Local mode is the only one that lays out the stack.** A user of the
    /// paid service never has the app allocate ports or look for Docker.
    async fn control(&self) -> Result<Control> {
        let settings = BackendSettings::load(&self.data_dir);
        match settings.mode {
            BackendMode::Local => Ok(Control::local(self.stack().await?.ports.api)),
            BackendMode::Cloud => {
                let token = self.id_token(&settings).await?;
                Ok(Control::cloud(
                    settings.base_url,
                    Auth {
                        token,
                        // Empty means "let the server use my sole membership",
                        // which is different from sending an empty header.
                        tenant: (!settings.tenant_id.is_empty()).then_some(settings.tenant_id),
                    },
                ))
            }
        }
    }

    /// A usable ID token, refreshing it first if it is about to expire.
    ///
    /// Refreshing here rather than on a timer means it happens when it matters
    /// and never when the app is idle — and it is why an app left open
    /// overnight does not greet the user with a 401 they have to interpret.
    /// A refresh that fails is reported as being signed out, because that is
    /// what it means: the thirty-day refresh token is gone or revoked.
    async fn id_token(&self, settings: &BackendSettings) -> Result<String> {
        let session = auth::load_session(&self.data_dir).ok_or(AppError::NotSignedIn)?;
        if session.fresh() {
            return Ok(session.id_token);
        }
        let config = self.auth_config(settings).await?;
        let refreshed = auth::refresh(&reqwest::Client::new(), &config, &session)
            .await
            .map_err(|_| AppError::NotSignedIn)?;
        auth::save_session(&self.data_dir, &refreshed)?;
        Ok(refreshed.id_token)
    }

    /// Ask the paid plane where its sign-in lives.
    ///
    /// Fetched rather than configured: three values typed by hand are three
    /// chances to produce a login that fails with something unhelpful, and the
    /// app is already pointed at the service that knows them.
    async fn auth_config(&self, settings: &BackendSettings) -> Result<AuthConfig> {
        let url = format!("{}/auth/config", settings.base_url);
        let response = reqwest::Client::new()
            .get(&url)
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await
            .map_err(|source| AppError::ControlUnreachable { url: url.clone(), source })?;
        if !response.status().is_success() {
            let status = response.status().as_u16();
            return Err(AppError::control_status(
                status,
                response.text().await.unwrap_or_default(),
            ));
        }
        response
            .json()
            .await
            .map_err(|source| AppError::ControlUnreachable { url, source })
    }

    /// Swap the cached stack for one carrying a different billing project.
    ///
    /// The cache is replaced rather than cleared. Clearing would make the next
    /// caller re-run `ports::allocate`, and a running stack's ports are already
    /// taken — so it would allocate a *different* set and every subsequent
    /// command would address services that are not there.
    async fn set_gemini_project(&self, project_id: &str) -> Result<Arc<Stack>> {
        let current = self.local_stack().await?;
        let updated = Arc::new(current.with_gemini_project(project_id)?);
        *self.stack.lock().await = Some(updated.clone());
        Ok(updated)
    }
}

/// Which plane the app talks to, and whether it could authenticate to it.
///
/// `signedIn` is reported rather than the token: the UI needs to know that the
/// paid mode is usable, and nothing above Rust has a reason to hold a bearer.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendInfo {
    mode: BackendMode,
    base_url: String,
    tenant_id: String,
    signed_in: bool,
    /// Whose session it is, for the screen. Read from the token's `email`
    /// claim, never used to authorize anything.
    email: String,
    /// Where the refresh token is kept: `"keychain"` or `"file"`.
    ///
    /// Reported rather than assumed: on a Linux box with no Secret Service the
    /// keychain is simply not there, and the app falls back to a `0600` file.
    /// That is a real difference in how well a thirty-day credential is
    /// protected, and a user is entitled to know which one they got. A token
    /// rather than prose, because the wording belongs in the i18n bundles.
    secret_store: &'static str,
}

fn describe_backend(state: &AppState, settings: BackendSettings) -> BackendInfo {
    let (session, store) = auth::load_session_with_backend(&state.data_dir);
    BackendInfo {
        mode: settings.mode,
        base_url: settings.base_url,
        tenant_id: settings.tenant_id,
        // A session whose ID token has expired still counts: the refresh token
        // is good for thirty days and the next request renews it. Reporting
        // "signed out" for that would send a user to sign in again once an hour.
        signed_in: session.is_some(),
        email: session.map(|s| s.email).unwrap_or_default(),
        secret_store: store.tag(),
    }
}

#[tauri::command]
async fn backend_settings(state: State<'_, AppState>) -> Result<BackendInfo> {
    let settings = BackendSettings::load(&state.data_dir);
    Ok(describe_backend(&state, settings))
}

/// Sign in, end to end: authorization code with PKCE against the hosted UI.
///
/// The listener is bound **before** the browser is opened. Opening first would
/// leave a window in which the redirect arrives at a port nothing is listening
/// on, which reads to the user as a login that silently did nothing.
///
/// The whole flow is one command rather than two because the verifier must not
/// leave this process, and a `start`/`finish` pair would either have to hand it
/// to the webview or keep cross-call state for something that is over in
/// seconds.
#[tauri::command]
async fn sign_in(app: tauri::AppHandle, state: State<'_, AppState>) -> Result<BackendInfo> {
    use tauri_plugin_opener::OpenerExt;

    let settings = BackendSettings::load(&state.data_dir);
    if settings.mode != BackendMode::Cloud {
        return Err(AppError::Config(
            "el backend local no tiene cuentas: no hay dónde iniciar sesión".into(),
        ));
    }
    let config = state.auth_config(&settings).await?;
    let pkce = Pkce::new();

    let listener = std::net::TcpListener::bind(("127.0.0.1", auth::CALLBACK_PORT))
        .map_err(|e| AppError::io(format!("127.0.0.1:{}", auth::CALLBACK_PORT), e))?;

    app.opener()
        .open_url(auth::authorize_url(&config, &pkce), None::<&str>)
        .map_err(|e| AppError::Config(format!("no se pudo abrir el navegador: {e}")))?;

    // Blocking accept, on a thread that is allowed to block.
    let expected = pkce.state.clone();
    let code = tokio::task::spawn_blocking(move || auth::await_callback(listener, &expected))
        .await
        .map_err(|e| AppError::Config(format!("la espera del navegador falló: {e}")))??;

    let session: Session = auth::exchange_code(&reqwest::Client::new(), &config, &pkce, &code).await?;
    auth::save_session(&state.data_dir, &session)?;
    Ok(describe_backend(&state, settings))
}

/// Forget the session. Local only: it does not revoke anything upstream, and
/// saying otherwise would be a claim this app cannot make.
#[tauri::command]
async fn sign_out(state: State<'_, AppState>) -> Result<BackendInfo> {
    auth::clear_session(&state.data_dir)?;
    Ok(describe_backend(&state, BackendSettings::load(&state.data_dir)))
}

/// Choose a plane. Validated here so a bad address is refused by the request
/// that set it, rather than by every screen afterwards.
#[tauri::command]
async fn set_backend_mode(
    state: State<'_, AppState>,
    mode: BackendMode,
    base_url: String,
    tenant_id: String,
) -> Result<BackendInfo> {
    let settings = BackendSettings::validated(mode, &base_url, &tenant_id)?;

    // Pointing the app somewhere else ends the session, because the token
    // belongs to the service that issued it. Without this a bearer minted
    // against one deployment survived a change of address and was sent to
    // another, which fails as a 401 that reads like a signing problem rather
    // than like "you are not signed in to *this* one". Switching to local and
    // back counts: nothing there refreshes it, so the token quietly ages out.
    if BackendSettings::load(&state.data_dir).repoints_to(&settings) {
        auth::clear_session(&state.data_dir)?;
    }

    settings.save(&state.data_dir)?;
    Ok(describe_backend(&state, settings))
}

/// What the Services screen shows about the provider, and what it can change.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ProviderSettings {
    /// The Vertex AI billing project, empty when unset.
    project_id: String,
    /// `Gemini.configured` on the Python side is exactly `bool(project_id)`.
    configured: bool,
    /// Whether Application Default Credentials were found. Gemini Enterprise
    /// refuses API keys, so without these no paid stage can run whatever the
    /// project id says.
    adc_found: bool,
    /// The credentials file, so the screen can show *which* one is mounted
    /// rather than only that one is.
    adc_path: Option<String>,
}

impl ProviderSettings {
    fn of(stack: &Stack) -> Self {
        Self {
            project_id: stack.gemini_project.clone(),
            configured: !stack.gemini_project.is_empty(),
            adc_found: stack.adc_file.is_some(),
            adc_path: stack
                .adc_file
                .as_ref()
                .map(|p| p.display().to_string()),
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct StackEvent {
    phase: String,
    detail: String,
}

#[tauri::command]
async fn docker_probe() -> Result<DockerInfo> {
    stack::probe_docker().await
}

#[tauri::command]
async fn stack_status(state: State<'_, AppState>) -> Result<StackStatus> {
    state.local_stack().await?.status().await
}

#[tauri::command]
async fn stack_up(
    state: State<'_, AppState>,
    on_event: tauri::ipc::Channel<StackEvent>,
) -> Result<StackStatus> {
    let stack = state.local_stack().await?;

    // `up --wait` can sit silently for minutes on a cold start while Postgres
    // initialises and Temporal creates its schemas. Without these the window
    // looks hung, which is the false positive that makes people kill it
    // half-way through a database migration.
    let _ = on_event.send(StackEvent {
        phase: "starting".into(),
        detail: if stack.dev_mode {
            "building the worker image and starting services".into()
        } else {
            "pulling images and starting services".into()
        },
    });

    stack.up().await?;

    let _ = on_event.send(StackEvent {
        phase: "healthy".into(),
        detail: "all services report healthy".into(),
    });

    stack.status().await
}

#[tauri::command]
async fn stack_down(state: State<'_, AppState>) -> Result<StackStatus> {
    let stack = state.local_stack().await?;
    stack.down().await?;
    stack.status().await
}

#[tauri::command]
async fn stack_logs(state: State<'_, AppState>, service: String, tail: u32) -> Result<String> {
    state.local_stack().await?.logs(&service, tail.min(2000)).await
}

#[tauri::command]
async fn control_health(state: State<'_, AppState>) -> Result<Health> {
    let control = state.control().await?;
    control.health().await
}

#[tauri::command]
async fn control_ping(state: State<'_, AppState>) -> Result<PingResult> {
    let control = state.control().await?;
    control.ping().await
}

// Each of these is an explicit command rather than a generic pass-through. That
// costs a wrapper per endpoint and buys the webview keeping a `default-src
// 'self'` CSP with no localhost exception, and one place where a connection
// failure becomes a typed error the UI can branch on.

/// Put a file where the worker can read it, and say what to call it.
///
/// **The two planes disagree about what a path is, and this is where that is
/// settled.** Both modes hand back a path *the worker* can open, and in neither
/// mode is that the path the user picked.
///
/// Local mode used to return it unchanged, on the reasoning that the volume
/// makes one filesystem out of two. It does not make one *namespace*: the app
/// sees the workspace at its app-data directory and the container sees the same
/// bytes at `/workspace`, so a host path fails the worker's containment check
/// and a container path fails the `metadata` read here. Nothing translated
/// between them — `IngestRequest` claims the API does and it does not — so the
/// import screen could not succeed with any string a person typed. It copies
/// into the workspace inbox now and returns the container path, which is the
/// same shape cloud mode has always returned.
///
/// In cloud mode the worker is on somebody else's machine and the file has to
/// be sent; the server decides where it lands and hands back a container path.
///
/// The frontend calls this before `ingest_start` in both modes and uses what
/// comes back. That is the point of doing it here rather than branching in the
/// UI: the import screen has no business knowing which plane it is talking to,
/// and a branch there is one somebody adds a second, inconsistent copy of.
///
/// `source_key` is the library-relative name a person reads, and local mode
/// still derives it from the path the user *picked*, not from where the copy
/// landed. Every staged file lands in the same inbox, so keying on the copy
/// would make `inbox/` the first half of every document's name and would give
/// two unrelated books the same key. It also matters more than a label: the
/// graph derives a document's id from `document_id(library, source_key)`, so a
/// second import of the same file must produce the same key or it becomes a
/// second document. Cloud mode takes what the server recorded, which is the
/// original filename. Neither is the stored filename — in cloud mode the
/// server names the file itself, because a name arriving over HTTP is a name
/// an attacker chose.
#[tauri::command]
async fn stage_source(state: State<'_, AppState>, path: String) -> Result<StagedSource> {
    let settings = BackendSettings::load(&state.data_dir);
    let p = std::path::Path::new(&path);
    match settings.mode {
        BackendMode::Local => {
            let (source_path, byte_size) = stage_into_inbox(&state.workspace, p)?;
            Ok(StagedSource {
                source_path,
                source_key: local_source_key(&path),
                byte_size,
            })
        }
        BackendMode::Cloud => state.control().await?.upload_source(p).await,
    }
}

/// Where the containers see the workspace. The compose file mounts
/// `BRAIN_WORKSPACE` here (`BRAIN_WORKSPACE_DIR: /workspace`), and the worker
/// refuses a path outside its organisation's tree, so this prefix is not
/// cosmetic — it is the half of the path that makes the file reachable at all.
const CONTAINER_WORKSPACE: &str = "/workspace";

/// Formats the pipeline accepts, mirroring `SUPPORTED_FORMATS` in
/// `brainworker/pipeline.py`. Duplicated rather than fetched: this only
/// pre-filters a file dialog, and the server refuses an unsupported suffix
/// anyway with an error naming what it does support. A stale entry here costs a
/// worse dialog, never a wrong import.
const PICKABLE: [&str; 8] = ["pdf", "txt", "md", "docx", "pptx", "xlsx", "xlsm", "csv"];

/// Copy a chosen file into the workspace inbox and return the path *the worker*
/// will open, with the copy's size.
///
/// The copy is the point. A person picks a file from wherever it lives —
/// Downloads, a memory stick, a synced folder — and none of those are inside
/// the volume the container can read. Copying makes the pick work from
/// anywhere, and leaves the original where the person left it.
///
/// A name that is already taken is resolved rather than overwritten, and
/// identical content is reused rather than duplicated: staging the same book
/// twice is an ordinary thing to do (a failed run, a second attempt) and it
/// should not grow the inbox each time. Content, not the name, decides — two
/// different books can honestly be called `capitulo 1.pdf`.
fn stage_into_inbox(workspace: &Path, source: &Path) -> Result<(String, u64)> {
    let meta = std::fs::metadata(source).map_err(|e| AppError::io(source.display(), e))?;
    let inbox = workspace.join("inbox");
    std::fs::create_dir_all(&inbox).map_err(|e| AppError::io(inbox.display(), e))?;

    let name = source
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "documento".to_string());
    let target = free_inbox_name(&inbox, source, &name)?;

    // Only when it is not already there, byte for byte.
    if !target.exists() {
        std::fs::copy(source, &target).map_err(|e| AppError::io(target.display(), e))?;
    }

    let file_name = target
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or(name);
    Ok((
        format!("{CONTAINER_WORKSPACE}/inbox/{file_name}"),
        meta.len(),
    ))
}

/// The name to stage under: the file's own, unless something different already
/// holds it.
///
/// Returns an existing path when its content matches, so the caller can skip
/// the copy. Comparison is by length first because that settles almost every
/// case without reading either file.
fn free_inbox_name(inbox: &Path, source: &Path, name: &str) -> Result<PathBuf> {
    let (stem, suffix) = match name.rsplit_once('.') {
        // A leading dot is the whole name of a dotfile, not an extension.
        Some((s, ext)) if !s.is_empty() => (s.to_string(), format!(".{ext}")),
        _ => (name.to_string(), String::new()),
    };

    for attempt in 1..=99u32 {
        let candidate = if attempt == 1 {
            inbox.join(name)
        } else {
            inbox.join(format!("{stem} ({attempt}){suffix}"))
        };
        if !candidate.exists() || same_bytes(source, &candidate)? {
            return Ok(candidate);
        }
    }
    Err(AppError::io(
        inbox.join(name).display(),
        std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "no free name in the inbox after 99 attempts",
        ),
    ))
}

fn same_bytes(a: &Path, b: &Path) -> Result<bool> {
    let (ma, mb) = (
        std::fs::metadata(a).map_err(|e| AppError::io(a.display(), e))?,
        std::fs::metadata(b).map_err(|e| AppError::io(b.display(), e))?,
    );
    if ma.len() != mb.len() {
        return Ok(false);
    }
    let (da, db) = (
        std::fs::read(a).map_err(|e| AppError::io(a.display(), e))?,
        std::fs::read(b).map_err(|e| AppError::io(b.display(), e))?,
    );
    Ok(da == db)
}

/// Open the OS file chooser and return what was picked, or `None` if the person
/// dismissed it.
///
/// The dialog is opened *here* rather than from the webview, which is why no
/// new capability appears in `capabilities/default.json`: a capability grants
/// the webview the right to invoke a plugin command, and the webview never
/// invokes one. It invokes this, an explicit `#[tauri::command]` like every
/// other thing Rust does on its behalf.
///
/// Cancelling is `None`, not an error. It is the ordinary way to leave a file
/// dialog and the screen must not paint a red panel over it.
#[tauri::command]
async fn pick_source(app: tauri::AppHandle) -> Result<Option<String>> {
    use tauri_plugin_dialog::DialogExt;

    let (tx, mut rx) = tauri::async_runtime::channel(1);
    app.dialog()
        .file()
        .add_filter("Documentos", &PICKABLE)
        .pick_file(move |picked| {
            // Capacity is 1 and this fires once; a failure here means the
            // receiver is gone, which is nothing to report.
            let _ = tx.try_send(picked);
        });

    let picked = match rx.recv().await {
        Some(p) => p,
        None => return Ok(None),
    };
    Ok(picked.and_then(|p| p.into_path().ok()).map(|p| p.display().to_string()))
}

/// The last two components of a path, which is what a library-relative key has
/// always been here. Separate so the property can be tested without a
/// filesystem, and so cloud mode's differing answer is visibly a different
/// decision rather than a forgotten branch.
fn local_source_key(path: &str) -> String {
    let parts: Vec<&str> = path.split(['/', '\\']).filter(|p| !p.is_empty()).collect();
    let tail = parts[parts.len().saturating_sub(2)..].join("/");
    // A path that is nothing but separators leaves no components, and this is
    // the document's whole visible name — an empty one would render as a blank
    // row. In practice `stage_source` fails on the metadata read long before a
    // path like that gets here; the fallback is so the property holds for the
    // function rather than for its one caller.
    match tail.as_str() {
        "" if path.trim().is_empty() => "documento".to_string(),
        "" => path.to_string(),
        _ => tail,
    }
}

#[tauri::command]
async fn ingest_start(
    state: State<'_, AppState>,
    request: IngestRequest,
    options: Option<StageOptions>,
) -> Result<StartedRun> {
    let control = state.control().await?;
    control
        .start_ingest(&request, &options.unwrap_or_default())
        .await
}

#[tauri::command]
async fn ingest_gate(state: State<'_, AppState>, workflow_id: String) -> Result<Option<GateReport>> {
    let control = state.control().await?;
    control.gate(&workflow_id).await
}

/// One step of getting a video ready, for a window that would otherwise sit
/// still for minutes.
///
/// A Channel rather than a return value because the interesting part is the
/// middle: resolving is three seconds and downloading an hour of audio is not.
/// `bytes`/`total` are only ever set on `downloading`, and a `total` of zero
/// means yt-dlp offered no estimate — rendered as no percentage rather than as
/// 0%, the rule `/project-summary` applies to a leg it could not ask.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VideoFetchEvent {
    /// `resolving`, `downloading`, `uploading`, or `starting`.
    pub step: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub total: Option<u64>,
}

impl VideoFetchEvent {
    fn step(step: &'static str) -> Self {
        Self { step, bytes: None, total: None }
    }
}

/// Start indexing a video. No staging call precedes this: there is no file.
///
/// **In cloud mode the two YouTube calls are made here, on this machine.**
/// Measured 2026-09-05: `extract_info` is answered from a residential address
/// in 2.6 s and refused from the EC2 egress address with "Sign in to confirm
/// you're not a bot" — it is the player API that is bot-checked, not the
/// network — so a paid run that asks the server to make that call fails with
/// `youtube_refused_this_host` and the video is never indexed. Doing it here
/// and sending the small `VideoInfo` is what makes paid mode work at all, and
/// it needs nothing of the user: no tunnel, no second worker, no credentials.
///
/// **Local mode is deliberately untouched.** The container's egress is this
/// machine's egress, so `resolve_video` is already answered there; routing it
/// through the bundled binary instead would only add a second copy of yt-dlp
/// that can go stale on its own schedule. The narrowest change the measurement
/// justifies.
///
/// The audio half only runs for a video with **no captions at all**, and it has
/// to run here for a different measured reason: a `googlevideo` media URL
/// carries the address that resolved it and answers 403 anywhere else, so the
/// download cannot be separated from the resolution. It happens before the
/// gate, which is the one thing about this that is a trade rather than a fact:
/// the bytes cost nothing, only time, and the alternative — parking the
/// workflow after approval until this machine sends them — makes a run that
/// stalls silently when the window is closed.
#[tauri::command]
async fn video_start(
    state: State<'_, AppState>,
    request: VideoRequest,
    options: Option<StageOptions>,
    on_event: tauri::ipc::Channel<VideoFetchEvent>,
) -> Result<StartedRun> {
    let mut request = request;
    // Required rather than optional, because `Option<Channel<_>>` is not a
    // command argument Tauri can build: `Channel` implements `CommandArg` and
    // not `Deserialize`, so wrapping it asks serde for something it has no way
    // to make. The caller always creates one; a send into a channel nobody is
    // listening to is dropped, which is the behaviour an optional one would
    // have had anyway.
    let say = |event: VideoFetchEvent| {
        // A closed channel means the window moved on. Dropped rather than
        // treated as a failure: the run is what matters and it has not started
        // yet.
        let _ = on_event.send(event);
    };

    if BackendSettings::load(&state.data_dir).mode == BackendMode::Cloud {
        say(VideoFetchEvent::step("resolving"));
        let resolved = ytdlp::resolve(&request.url, &request.languages).await?;
        let needs_audio = resolved.chosen.is_none();
        let video_id = resolved.video_id.clone();
        request.resolved = Some(resolved);

        if needs_audio {
            say(VideoFetchEvent::step("downloading"));
            let into = state.data_dir.join("video-audio").join(&video_id);
            // Whatever a previous attempt left. The directory is read back to
            // find the finished file, so a stale one from an abandoned run
            // would be uploaded instead of the new download.
            let _ = std::fs::remove_dir_all(&into);
            let audio = ytdlp::download_audio(&request.url, &into, |p| {
                say(VideoFetchEvent {
                    step: "downloading",
                    bytes: Some(p.bytes),
                    total: Some(p.total),
                });
            })
            .await?;

            say(VideoFetchEvent::step("uploading"));
            let staged = state.control().await?.upload_audio(&audio).await;
            // Deleted whether or not the upload worked, exactly as the worker
            // deletes its own download in a `finally`: this is a transient on
            // the user's own disk and can be a couple of hundred megabytes.
            let _ = std::fs::remove_dir_all(&into);
            request.audio_path = staged?.audio_path;
        }
    }

    say(VideoFetchEvent::step("starting"));
    let control = state.control().await?;
    control
        .start_video(&request, &options.unwrap_or_default())
        .await
}

/// A video run's gate. `None` while the probe is still running, which is the
/// ordinary first answer and not a failure.
#[tauri::command]
async fn video_gate(
    state: State<'_, AppState>,
    workflow_id: String,
) -> Result<Option<VideoGateReport>> {
    let control = state.control().await?;
    control.video_gate(&workflow_id).await
}

#[tauri::command]
async fn run_status(state: State<'_, AppState>, workflow_id: String) -> Result<RunState> {
    let control = state.control().await?;
    control.run_status(&workflow_id).await
}

/// The persistent queue.
///
/// Every filter is optional and assembled here rather than in the webview, so
/// the query string has one owner and a screen cannot invent a parameter the
/// control plane will silently ignore.
#[tauri::command]
async fn runs_list(
    state: State<'_, AppState>,
    limit: Option<u32>,
    before: Option<String>,
    kinds: Option<String>,
    states: Option<String>,
    library_id: Option<String>,
    document_id: Option<String>,
    version_id: Option<String>,
) -> Result<RunListPage> {
    let mut query: Vec<String> = Vec::new();
    if let Some(n) = limit {
        query.push(format!("limit={n}"));
    }
    for (key, value) in [
        ("before", before),
        ("kinds", kinds),
        ("states", states),
        ("library_id", library_id),
        ("document_id", document_id),
        ("version_id", version_id),
    ] {
        if let Some(v) = value.filter(|v| !v.is_empty()) {
            query.push(format!("{key}={}", percent_encode(&v)));
        }
    }
    let control = state.control().await?;
    control.runs(&query.join("&")).await
}

/// Percent-encode a query value, hand-written rather than pulled in.
///
/// Twenty characters of logic with no way to be subtly wrong, which is the same
/// test `sha2` failed and base64url passed elsewhere in this crate. It matters
/// for exactly one value and would be easy to skip: the queue's cursor is
/// `{ISO timestamp}|{run id}`, and an unencoded `+` in `…+00:00` is decoded by
/// the server as a **space** — so the cursor parses as an invalid date, the
/// route falls back to the first page, and the queue silently pages forever
/// over the same rows.
fn percent_encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// What a run did, stage by stage. Answers for a run Temporal has forgotten.
#[tauri::command]
async fn run_audit(state: State<'_, AppState>, workflow_id: String) -> Result<RunAudit> {
    let control = state.control().await?;
    control.run_audit(&workflow_id).await
}

/// The raw workflow history, fetched only when somebody expands the panel.
#[tauri::command]
async fn run_events(state: State<'_, AppState>, workflow_id: String) -> Result<RunEventPage> {
    let control = state.control().await?;
    control.run_events(&workflow_id).await
}

#[tauri::command]
async fn ingest_approve(
    state: State<'_, AppState>,
    workflow_id: String,
    approval: Approval,
) -> Result<()> {
    let control = state.control().await?;
    control
        .approve(&workflow_id, &approval)
        .await
}

/// Stop a run that is already spending.
///
/// Explicit like every other proxy — no generic pass-through — which is what
/// keeps the webview on a `default-src 'self'` CSP with no localhost exception.
#[tauri::command]
async fn cancel_run(state: State<'_, AppState>, workflow_id: String) -> Result<()> {
    let control = state.control().await?;
    control.cancel_run(&workflow_id).await
}

#[tauri::command]
async fn libraries(state: State<'_, AppState>) -> Result<Libraries> {
    let control = state.control().await?;
    control.libraries().await
}

#[tauri::command]
async fn library_documents(
    state: State<'_, AppState>,
    library_id: String,
    include_absent: Option<bool>,
) -> Result<Library> {
    let control = state.control().await?;
    control
        .library(&library_id, include_absent.unwrap_or(false))
        .await
}

#[tauri::command]
async fn provider_settings(state: State<'_, AppState>) -> Result<ProviderSettings> {
    let stack = state.local_stack().await?;
    Ok(ProviderSettings::of(&stack))
}

/// Name the Vertex AI billing project.
///
/// Written to `.env`, which compose interpolates at `up` time — so the running
/// containers keep the old value until the stack is recreated. The screen says
/// so; silently accepting a setting that does not take effect is how a user
/// concludes the feature is broken.
#[tauri::command]
async fn set_provider_project(
    state: State<'_, AppState>,
    project_id: String,
) -> Result<ProviderSettings> {
    let stack = state.set_gemini_project(&project_id).await?;
    Ok(ProviderSettings::of(&stack))
}

// -- Channels --------------------------------------------------------------
//
// Reading a YouTube channel before paying to index any of it. Five commands,
// and only two of them can spend — which is why the quote is a command of its
// own rather than a field on the discovery: pressing the button is the
// decision, so the figure has to be on screen before the call that starts it.

/// Whether a provider credential is stored, and where. **Never what it is.**
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ProviderSecret {
    name: String,
    /// The environment name the worker reads it under, so the Services screen
    /// can name the thing a person would put in a `.env` by hand.
    env: String,
    stored: bool,
    /// `"keychain"` or `"file"` — a token rather than prose, so the wording
    /// lives in the two i18n bundles the way an error `kind` does. An install
    /// that fell back to a file is less protected than one that did not, and
    /// saying so is the difference between a documented trade-off and a quiet
    /// downgrade.
    store: &'static str,
}

#[tauri::command]
async fn provider_secrets(state: State<'_, AppState>) -> Result<Vec<ProviderSecret>> {
    Ok(secrets::PROVIDER_SECRETS
        .iter()
        .map(|(name, env)| {
            let (stored, backend) = secrets::status(&state.data_dir, name);
            ProviderSecret {
                name: (*name).to_string(),
                env: (*env).to_string(),
                stored,
                store: backend.tag(),
            }
        })
        .collect())
}

/// Store a provider credential, or clear it with an empty value.
///
/// Written to the OS keychain and materialised into the `0600` file the worker
/// mounts read-only. The worker reads that file once at startup, so a key set
/// while the stack is up reaches it on the next `up` — the screen says so, for
/// the reason `set_provider_project` says it about `.env`.
#[tauri::command]
async fn set_provider_secret(
    state: State<'_, AppState>,
    name: String,
    value: String,
) -> Result<Vec<ProviderSecret>> {
    let stack = state.local_stack().await?;
    secrets::set(&state.data_dir, &stack.secrets_file, &name, &value)?;
    provider_secrets(state).await
}

#[tauri::command]
async fn channel_sync(
    state: State<'_, AppState>,
    url: String,
    limit: Option<u32>,
) -> Result<ChannelSummary> {
    let control = state.control().await?;
    control.channel_sync(&url, limit.unwrap_or(100)).await
}

#[tauri::command]
async fn channels(state: State<'_, AppState>) -> Result<ChannelList> {
    let control = state.control().await?;
    control.channels().await
}

#[tauri::command]
async fn channel_detail(
    state: State<'_, AppState>,
    channel_id: String,
) -> Result<ChannelDetail> {
    let control = state.control().await?;
    control.channel_detail(&channel_id).await
}

/// What reading this channel would cost. Free, and it reaches no provider.
#[tauri::command]
async fn channel_quote(
    state: State<'_, AppState>,
    channel_id: String,
    topic: String,
    limit: Option<u32>,
    deep_limit: Option<u32>,
) -> Result<DiscoveryQuote> {
    let control = state.control().await?;
    control
        .channel_quote(
            &channel_id,
            &topic,
            limit.unwrap_or(100),
            deep_limit.unwrap_or(10),
        )
        .await
}

#[tauri::command]
async fn channel_discover(
    state: State<'_, AppState>,
    channel_id: String,
    topic: String,
    limit: Option<u32>,
    deep_limit: Option<u32>,
) -> Result<StartedRun> {
    let control = state.control().await?;
    control
        .channel_discover(
            &channel_id,
            &topic,
            limit.unwrap_or(100),
            deep_limit.unwrap_or(10),
        )
        .await
}

#[tauri::command]
async fn channel_topics(
    state: State<'_, AppState>,
    channel_id: String,
    topic: String,
    video_runs: Vec<String>,
) -> Result<StartedRun> {
    let control = state.control().await?;
    control
        .channel_topics(&channel_id, &topic, &video_runs)
        .await
}

/// Ask this channel's indexed sermons. **This spends.**
///
/// Two calls, like `ask`: the second collects. A real question once outran the
/// app's own 180 s timeout — was computed, was billed, and was discarded under
/// a message blaming an unreachable API.
#[tauri::command]
async fn channel_ask(
    state: State<'_, AppState>,
    channel_id: String,
    topic: String,
    effort: Option<String>,
) -> Result<AskStarted> {
    let control = state.control().await?;
    control
        .channel_ask(&channel_id, &topic, effort.as_deref().unwrap_or("thorough"))
        .await
}

#[tauri::command]
async fn channel_ask_result(
    state: State<'_, AppState>,
    channel_id: String,
    question_id: String,
) -> Result<SynthesisResult> {
    let control = state.control().await?;
    control.channel_ask_result(&channel_id, &question_id).await
}

#[tauri::command]
async fn channel_reading(
    state: State<'_, AppState>,
    channel_id: String,
    workflow_id: String,
) -> Result<ChannelReading> {
    let control = state.control().await?;
    control.channel_reading(&channel_id, &workflow_id).await
}

// -- Library verbs ---------------------------------------------------------
//
// Three verbs, and they cost three different things. `document_remove` is free
// and irreversible; `document_reindex` re-runs the whole pipeline and arrives at
// the normal approval gate, which re-quotes before spending; `document_rebuild`
// replays artifacts already paid for and can only spend on embedding.

#[tauri::command]
async fn document_detail(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
) -> Result<DocumentDetail> {
    let control = state.control().await?;
    control
        .document_detail(&library_id, &document_id)
        .await
}

/// What one indexed version holds, what it cost, and what still agrees.
///
/// Proxied like everything else rather than fetched by the webview, which is
/// what lets the CSP stay `default-src 'self'` with no localhost exception.
/// Read-only: nothing behind this writes and nothing behind it spends.
#[tauri::command]
async fn version_statistics(
    state: State<'_, AppState>,
    library_id: String,
    version_id: String,
) -> Result<VersionStatistics> {
    let control = state.control().await?;
    control.version_statistics(&library_id, &version_id).await
}

/// Permanent removal, across Qdrant, Memgraph and the catalog.
///
/// No confirmation happens here. The screen confirms, because only the screen
/// can name what is about to be destroyed — and a dialog raised from Rust would
/// need a plugin permission the capability file deliberately does not grant.
#[tauri::command]
async fn document_remove(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
) -> Result<Removal> {
    let control = state.control().await?;
    control
        .remove_document(&library_id, &document_id)
        .await
}

#[tauri::command]
async fn version_remove(
    state: State<'_, AppState>,
    library_id: String,
    version_id: String,
) -> Result<Removal> {
    let control = state.control().await?;
    control
        .remove_version(&library_id, &version_id)
        .await
}

/// Promote a version an ingest deliberately withheld.
///
/// The pipeline blocks activation when the inherited heading rules and the
/// built-in ones disagree about how many chapters a document has — the one
/// automatic signal a structural fingerprint collision gives, and one the
/// retrieval metrics cannot see. This is the judgement that the rules were right
/// after all, and it costs nothing: the index is already written and paid for.
#[tauri::command]
async fn version_activate(
    state: State<'_, AppState>,
    library_id: String,
    version_id: String,
) -> Result<Activation> {
    let control = state.control().await?;
    control.activate_version(&library_id, &version_id).await
}

/// Build a book for a version already indexed, and say where it landed.
#[tauri::command]
async fn version_build_epub(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
    version_id: String,
) -> Result<BookBuilt> {
    let control = state.control().await?;
    control
        .build_epub(&library_id, &document_id, &version_id)
        .await
}

/// Correct what the catalog calls a document.
#[tauri::command]
async fn document_update(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
    metadata: DocumentMetadata,
) -> Result<DocumentMetadataResult> {
    let control = state.control().await?;
    control
        .update_document(&library_id, &document_id, &metadata)
        .await
}

/// Fetch a book and write it wherever the person says.
///
/// The dialog is opened *here*, for the reason `pick_source` records at length:
/// a capability grants the **webview** the right to invoke a plugin command, and
/// the webview invokes only this. So no entry appears in
/// `capabilities/default.json` and the webview keeps its `default-src 'self'`
/// CSP with no localhost exception — which is also why the bytes cannot simply
/// be fetched by an `<a download>` in the page.
///
/// Cancelling is `Ok(None)`, not an error: it is the ordinary way to leave a
/// file dialog and the screen must not paint a red panel over it.
///
/// The fetch happens **after** the dialog rather than before. A person who
/// dismisses the chooser should not have paid for a transfer, and a failure
/// then has somewhere to land — an error raised before a path exists cannot say
/// which file it was about.
#[tauri::command]
async fn artifact_save(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    workflow_id: String,
    name: String,
    suggested_name: String,
) -> Result<Option<String>> {
    use tauri_plugin_dialog::DialogExt;

    let (tx, mut rx) = tauri::async_runtime::channel(1);
    app.dialog()
        .file()
        .set_file_name(&suggested_name)
        .add_filter("EPUB", &["epub"])
        .save_file(move |chosen| {
            let _ = tx.try_send(chosen);
        });

    let chosen = match rx.recv().await {
        Some(c) => c,
        None => return Ok(None),
    };
    let Some(path) = chosen.and_then(|p| p.into_path().ok()) else {
        return Ok(None);
    };

    let control = state.control().await?;
    let bytes = control.download_artifact(&workflow_id, &name).await?;
    std::fs::write(&path, &bytes).map_err(|e| AppError::io(path.display().to_string(), e))?;
    Ok(Some(path.display().to_string()))
}

/// Write text the webview already holds wherever the person says.
///
/// The dialog is opened *here* for the reason `artifact_save` records: a
/// capability grants the **webview** the right to invoke a plugin command, and
/// the webview invokes only this. So no entry appears in
/// `capabilities/default.json` and the page keeps its `default-src 'self'` CSP.
///
/// Unlike `artifact_save` there is nothing to fetch — the synthesis is already
/// on screen — so this writes what it is given. Cancelling is `Ok(None)`, not
/// an error: it is the ordinary way to leave a file dialog.
#[tauri::command]
async fn save_text(
    app: tauri::AppHandle,
    suggested_name: String,
    contents: String,
) -> Result<Option<String>> {
    use tauri_plugin_dialog::DialogExt;

    let (tx, mut rx) = tauri::async_runtime::channel(1);
    app.dialog()
        .file()
        .set_file_name(&suggested_name)
        .add_filter("JSON", &["json"])
        .add_filter("CSV", &["csv"])
        .save_file(move |chosen| {
            let _ = tx.try_send(chosen);
        });

    let chosen = match rx.recv().await {
        Some(c) => c,
        None => return Ok(None),
    };
    let Some(path) = chosen.and_then(|p| p.into_path().ok()) else {
        return Ok(None);
    };
    std::fs::write(&path, contents.as_bytes())
        .map_err(|e| AppError::io(path.display().to_string(), e))?;
    Ok(Some(path.display().to_string()))
}

#[tauri::command]
async fn document_reindex(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
    options: Option<StageOptions>,
) -> Result<StartedRun> {
    let control = state.control().await?;
    control
        .reindex(&library_id, &document_id, &options.unwrap_or_default())
        .await
}

#[tauri::command]
async fn document_rebuild(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
) -> Result<StartedRun> {
    let control = state.control().await?;
    control
        .rebuild(&library_id, &document_id)
        .await
}

#[tauri::command]
async fn rebuild_gate(
    state: State<'_, AppState>,
    workflow_id: String,
) -> Result<Option<RebuildReport>> {
    let control = state.control().await?;
    control.rebuild_gate(&workflow_id).await
}

// -- Explore ---------------------------------------------------------------
//
// Six commands, one per pane the screen offers. Each is explicit rather than a
// generic pass-through for the same reason every other command here is: that is
// what lets the webview keep `default-src 'self'` with no localhost exception,
// and it means the set of graph queries the UI can reach is a list somebody
// wrote rather than whatever the API happens to expose.

#[tauri::command]
async fn explore_outline(state: State<'_, AppState>, version_id: String) -> Result<Outline> {
    let control = state.control().await?;
    control.outline(&version_id).await
}

#[tauri::command]
async fn explore_section_chunks(
    state: State<'_, AppState>,
    section_id: String,
) -> Result<SectionChunks> {
    let control = state.control().await?;
    control.section_chunks(&section_id).await
}

#[tauri::command]
async fn explore_chunk_context(
    state: State<'_, AppState>,
    chunk_id: String,
) -> Result<ChunkContext> {
    let control = state.control().await?;
    control.chunk_context(&chunk_id).await
}

#[tauri::command]
async fn explore_concepts(
    state: State<'_, AppState>,
    version_id: String,
) -> Result<VersionConcepts> {
    let control = state.control().await?;
    control.version_concepts(&version_id).await
}

#[tauri::command]
async fn explore_related(
    state: State<'_, AppState>,
    version_id: String,
) -> Result<RelatedDocuments> {
    let control = state.control().await?;
    control.related_documents(&version_id).await
}

/// What the whole installation holds. The landing screen's only request.
///
/// Not library-scoped: its figures are project-wide, which is why Inicio owns no
/// library picker.
#[tauri::command]
async fn project_summary(state: State<'_, AppState>) -> Result<ProjectSummary> {
    let control = state.control().await?;
    control.project_summary().await
}

/// Every projected book in one library and every concept it mentions.
///
/// `min_documents` is the volume control and the confidence floor is not: on the
/// real corpus, raising the floor from 0.6 to 0.9 drops 3% of edges while
/// requiring a concept to appear in two books drops 84% — and what it drops is
/// every concept that cannot join one book to another.
#[tauri::command]
async fn library_graph(
    state: State<'_, AppState>,
    library_id: String,
    confidence_floor: f64,
    min_documents: i64,
) -> Result<LibraryGraph> {
    let control = state.control().await?;
    control
        .library_graph(&library_id, confidence_floor, min_documents)
        .await
}

#[tauri::command]
async fn explore_claims(
    state: State<'_, AppState>,
    concept_id: String,
) -> Result<ConceptClaims> {
    let control = state.control().await?;
    control.concept_claims(&concept_id).await
}

/// How this organisation words an answer at each effort level.
#[tauri::command]
async fn answer_styles(state: State<'_, AppState>) -> Result<AnswerStyles> {
    let control = state.control().await?;
    control.answer_styles().await
}

/// Override one level's wording, or clear it with an empty body.
#[tauri::command]
async fn set_answer_style(
    state: State<'_, AppState>,
    effort: String,
    update: AnswerStyleUpdate,
) -> Result<AnswerStyleSaved> {
    let control = state.control().await?;
    control.set_answer_style(&effort, &update).await
}

/// Hand a question over. Returns as soon as the API has taken it, not when it
/// has an answer — `ask_result` collects that.
#[tauri::command]
async fn ask(state: State<'_, AppState>, question: Question) -> Result<AskStarted> {
    let control = state.control().await?;
    control.ask(&question).await
}

#[tauri::command]
async fn ask_result(state: State<'_, AppState>, question_id: String) -> Result<AskProgress> {
    let control = state.control().await?;
    control.ask_result(&question_id).await
}

// -- conversations ----------------------------------------------------------

/// Open a conversation. Must precede any turn: the turn and relay tables derive
/// their tenant from this row, so a turn naming a conversation that does not
/// exist writes nothing at all.
#[tauri::command]
async fn chat_create(
    state: State<'_, AppState>,
    request: NewConversation,
) -> Result<ConversationStarted> {
    let control = state.control().await?;
    control.chat_create(&request).await
}

#[tauri::command]
async fn chat_list(state: State<'_, AppState>) -> Result<Conversations> {
    let control = state.control().await?;
    control.chat_list().await
}

/// The whole transcript, read from the catalog rather than from Temporal —
/// which is what makes a conversation outlive a session's retention.
#[tauri::command]
async fn chat_read(
    state: State<'_, AppState>,
    conversation_id: String,
) -> Result<ConversationDetail> {
    let control = state.control().await?;
    control.chat_read(&conversation_id).await
}

#[tauri::command]
async fn chat_delete(state: State<'_, AppState>, conversation_id: String) -> Result<()> {
    let control = state.control().await?;
    control.chat_delete(&conversation_id).await
}

/// Ask the next question. Returns as soon as the turn has a number, not when it
/// has an answer — `chat_stream` follows that.
#[tauri::command]
async fn chat_turn(
    state: State<'_, AppState>,
    conversation_id: String,
    request: NewTurn,
) -> Result<TurnStarted> {
    let control = state.control().await?;
    control.chat_turn(&conversation_id, &request).await
}

/// Follow a turn's answer as it is written.
///
/// The only streaming command in this app, and it keeps the webview's
/// `default-src 'self'` CSP intact the same way every other one does: the
/// webview invokes a `#[tauri::command]`, Rust makes the HTTP request, and the
/// events come back over a Channel rather than over a connection the page
/// opened itself.
///
/// **What streams is a draft.** The `done` event carries the settled turn and is
/// authoritative: `citas` is the last field in the answering schema, so
/// verification cannot run until the envelope closes, and a turn can stream
/// fluent prose and still come back `insufficient_evidence` because no citation
/// survived. The caller must replace what it showed rather than append to it.
///
/// `since` is the resume point. A window reopened mid-answer passes the highest
/// `seq` it saw and pays nothing for the part it already has.
#[tauri::command]
async fn chat_stream(
    state: State<'_, AppState>,
    conversation_id: String,
    turn_seq: u32,
    since: u32,
    on_event: tauri::ipc::Channel<ChatEvent>,
) -> Result<()> {
    let control = state.control().await?;
    control
        .chat_stream(&conversation_id, turn_seq, since, |event| {
            // A closed channel means the window moved on. The turn is
            // unaffected — it lands in the catalog either way — so this is
            // dropped rather than treated as a failure.
            let _ = on_event.send(event);
        })
        .await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?;
            std::fs::create_dir_all(&data_dir)?;
            let workspace = data_dir.join("workspace");
            app.manage(AppState {
                stack: Mutex::new(None),
                data_dir,
                workspace,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_settings,
            set_backend_mode,
            sign_in,
            sign_out,
            docker_probe,
            stack_status,
            stack_up,
            stack_down,
            stack_logs,
            control_health,
            control_ping,
            pick_source,
            stage_source,
            ingest_start,
            ingest_gate,
            video_start,
            video_gate,
            run_status,
            cancel_run,
            ingest_approve,
            libraries,
            library_documents,
            ask,
            answer_styles,
            set_answer_style,
            ask_result,
            chat_create,
            chat_list,
            chat_read,
            chat_delete,
            chat_turn,
            chat_stream,
            explore_outline,
            explore_section_chunks,
            explore_chunk_context,
            explore_concepts,
            explore_related,
            explore_claims,
            project_summary,
            library_graph,
            provider_settings,
            set_provider_project,
            document_detail,
            version_statistics,
            document_remove,
            version_remove,
            version_activate,
            version_build_epub,
            document_update,
            artifact_save,
            document_reindex,
            document_rebuild,
            rebuild_gate,
            runs_list,
            run_audit,
            run_events,
            provider_secrets,
            set_provider_secret,
            channel_sync,
            channels,
            channel_detail,
            channel_quote,
            channel_discover,
            channel_topics,
            channel_reading,
            channel_ask,
            channel_ask_result,
            save_text,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Company Brain");
}

#[cfg(test)]
mod tests {
    use super::{free_inbox_name, local_source_key, stage_into_inbox};

    /// Staging copies into the inbox and hands back the path **the worker**
    /// opens, not the one the person picked.
    ///
    /// This is the whole reason the function exists. The volume makes one set
    /// of bytes out of two filesystems, and two namespaces out of one: the app
    /// sees the workspace at its app-data directory, the container sees it at
    /// `/workspace`, and nothing between them translates. Before this, a host
    /// path failed the worker's containment check and a container path failed
    /// the app's own `metadata` read — so the import screen could not succeed
    /// with any string at all.
    #[test]
    fn staging_returns_a_path_the_container_can_open() {
        let home = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let picked = home.path().join("El reto de Dios.txt");
        std::fs::write(&picked, b"contenido").unwrap();

        let (path, size) = stage_into_inbox(workspace.path(), &picked).unwrap();

        assert_eq!(path, "/workspace/inbox/El reto de Dios.txt");
        assert_eq!(size, 9);
        // Copied, not moved: the file the person picked stays where they left it.
        assert!(picked.is_file());
        assert_eq!(
            std::fs::read(workspace.path().join("inbox/El reto de Dios.txt")).unwrap(),
            b"contenido"
        );
    }

    /// Staging the same file twice is ordinary — a failed run, a second
    /// attempt — and must not grow the inbox by a copy each time.
    #[test]
    fn staging_the_same_file_twice_reuses_the_one_already_there() {
        let home = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let picked = home.path().join("libro.pdf");
        std::fs::write(&picked, b"igual").unwrap();

        let (first, _) = stage_into_inbox(workspace.path(), &picked).unwrap();
        let (second, _) = stage_into_inbox(workspace.path(), &picked).unwrap();

        assert_eq!(first, second);
        let staged: Vec<_> = std::fs::read_dir(workspace.path().join("inbox"))
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(staged.len(), 1);
    }

    /// Content decides, never the name. Two different books can honestly both
    /// be called `capitulo 1.pdf`, and overwriting one with the other would
    /// index the wrong document under the first one's identity.
    #[test]
    fn a_different_file_with_a_taken_name_is_not_overwritten() {
        let home = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let inbox = workspace.path().join("inbox");
        std::fs::create_dir_all(&inbox).unwrap();
        std::fs::write(inbox.join("capitulo 1.pdf"), b"el primero").unwrap();

        let picked = home.path().join("capitulo 1.pdf");
        std::fs::write(&picked, b"otro distinto").unwrap();
        let (path, _) = stage_into_inbox(workspace.path(), &picked).unwrap();

        assert_eq!(path, "/workspace/inbox/capitulo 1 (2).pdf");
        assert_eq!(
            std::fs::read(inbox.join("capitulo 1.pdf")).unwrap(),
            b"el primero"
        );
    }

    /// A dotfile's leading dot is its whole name, not an extension, so a
    /// collision must not produce `` (2).gitignore``.
    #[test]
    fn a_leading_dot_is_not_an_extension() {
        let home = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let inbox = workspace.path().join("inbox");
        std::fs::create_dir_all(&inbox).unwrap();
        std::fs::write(inbox.join(".notas"), b"uno").unwrap();
        let picked = home.path().join(".notas");
        std::fs::write(&picked, b"dos").unwrap();

        let chosen = free_inbox_name(&inbox, &picked, ".notas").unwrap();
        assert_eq!(chosen.file_name().unwrap(), ".notas (2)");
    }


    /// The key is what a person reads in the library list, and it is
    /// library-relative so that moving a library root does not orphan a row.
    /// Two components is the rule the import screen has always used; this only
    /// moved it out of the screen so cloud mode's different answer sits beside
    /// it rather than replacing it invisibly.
    #[test]
    fn a_local_key_is_the_last_two_path_components() {
        assert_eq!(local_source_key("/home/a/libros/teologia/libro.pdf"), "teologia/libro.pdf");
        assert_eq!(local_source_key("C:\\Users\\a\\libro.pdf"), "a/libro.pdf");
    }

    #[test]
    fn a_shallow_path_keeps_what_there_is_rather_than_becoming_empty() {
        assert_eq!(local_source_key("libro.pdf"), "libro.pdf");
        assert_eq!(local_source_key("/libro.pdf"), "libro.pdf");
    }

    /// A trailing slash, a doubled one, and a bare separator all used to be
    /// able to produce an empty key — which would then be the document's whole
    /// visible name.
    #[test]
    fn no_input_produces_an_empty_key() {
        for path in ["/", "//", "\\", "///a//", ""] {
            assert!(!local_source_key(path).is_empty(), "empty for {path:?}");
        }
    }
}
