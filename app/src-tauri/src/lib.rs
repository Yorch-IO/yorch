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
mod stack;

use std::path::PathBuf;
use std::sync::Arc;

use serde::Serialize;
use tauri::{Manager, State};
use tokio::sync::Mutex;

use auth::{AuthConfig, Pkce, Session};
use backend::{BackendMode, BackendSettings};
use control::{
    Approval, Auth, AskProgress, AskStarted, ChunkContext, ConceptClaims, Control, DocumentDetail,
    GateReport, Health, IngestRequest, Libraries, Library, LibraryGraph, Outline, PingResult,
    ProjectSummary, Question, RebuildReport, RelatedDocuments, Removal, SectionChunks,
    StageOptions, StagedSource, StartedRun, VersionConcepts,
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
            return Err(AppError::ControlStatus {
                status: response.status().as_u16(),
                body: response.text().await.unwrap_or_default().chars().take(500).collect(),
            });
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
        let current = self.stack().await?;
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
    state.stack().await?.status().await
}

#[tauri::command]
async fn stack_up(
    state: State<'_, AppState>,
    on_event: tauri::ipc::Channel<StackEvent>,
) -> Result<StackStatus> {
    let stack = state.stack().await?;

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
    let stack = state.stack().await?;
    stack.down().await?;
    stack.status().await
}

#[tauri::command]
async fn stack_logs(state: State<'_, AppState>, service: String, tail: u32) -> Result<String> {
    state.stack().await?.logs(&service, tail.min(2000)).await
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
/// settled.** In local mode the app and the worker share a filesystem through
/// the compose volume, so the path the user picked is already the path the
/// worker will open and this copies nothing. In cloud mode the worker is on
/// somebody else's machine and the file has to be sent; the server decides
/// where it lands and hands back a container path.
///
/// The frontend calls this before `ingest_start` in both modes and uses what
/// comes back. That is the point of doing it here rather than branching in the
/// UI: the import screen has no business knowing which plane it is talking to,
/// and a branch there is one somebody adds a second, inconsistent copy of.
///
/// `source_key` is the library-relative name a person reads. Local mode keeps
/// the last two path components, which is what the import screen did before
/// this existed; cloud mode takes what the server recorded, which is the
/// original filename. Neither is the stored filename — in cloud mode the
/// server names the file itself, because a name arriving over HTTP is a name
/// an attacker chose.
#[tauri::command]
async fn stage_source(state: State<'_, AppState>, path: String) -> Result<StagedSource> {
    let settings = BackendSettings::load(&state.data_dir);
    let p = std::path::Path::new(&path);
    match settings.mode {
        BackendMode::Local => {
            let meta = std::fs::metadata(p).map_err(|e| AppError::io(p.display(), e))?;
            Ok(StagedSource {
                source_path: path.clone(),
                source_key: local_source_key(&path),
                byte_size: meta.len(),
            })
        }
        BackendMode::Cloud => state.control().await?.upload_source(p).await,
    }
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
    let stack = state.stack().await?;
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
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
            stage_source,
            ingest_start,
            ingest_gate,
            ingest_approve,
            libraries,
            library_documents,
            ask,
            ask_result,
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
            document_remove,
            version_remove,
            document_reindex,
            document_rebuild,
            rebuild_gate,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Company Brain");
}

#[cfg(test)]
mod tests {
    use super::local_source_key;

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
