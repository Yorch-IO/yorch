//! Company Brain — desktop shell.
//!
//! All application logic lives here rather than in `main.rs`: Tauri replaces
//! `main()` on mobile targets, so anything defined there is unreachable in
//! those builds.

mod control;
mod error;
mod ports;
mod stack;

use std::path::PathBuf;
use std::sync::Arc;

use serde::Serialize;
use tauri::{Manager, State};
use tokio::sync::Mutex;

use control::{
    Approval, AskProgress, AskStarted, ChunkContext, ConceptClaims, Control, DocumentDetail,
    GateReport, Health, IngestRequest, Libraries, Library, LibraryGraph, Outline, PingResult,
    ProjectSummary, Question, RebuildReport, RelatedDocuments, Removal, SectionChunks,
    StageOptions, StartedRun, VersionConcepts,
};
use error::Result;
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api).health().await
}

#[tauri::command]
async fn control_ping(state: State<'_, AppState>) -> Result<PingResult> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).ping().await
}

// Each of these is an explicit command rather than a generic pass-through. That
// costs a wrapper per endpoint and buys the webview keeping a `default-src
// 'self'` CSP with no localhost exception, and one place where a connection
// failure becomes a typed error the UI can branch on.

#[tauri::command]
async fn ingest_start(
    state: State<'_, AppState>,
    request: IngestRequest,
    options: Option<StageOptions>,
) -> Result<StartedRun> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .start_ingest(&request, &options.unwrap_or_default())
        .await
}

#[tauri::command]
async fn ingest_gate(state: State<'_, AppState>, workflow_id: String) -> Result<Option<GateReport>> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).gate(&workflow_id).await
}

#[tauri::command]
async fn ingest_approve(
    state: State<'_, AppState>,
    workflow_id: String,
    approval: Approval,
) -> Result<()> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .approve(&workflow_id, &approval)
        .await
}

#[tauri::command]
async fn libraries(state: State<'_, AppState>) -> Result<Libraries> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).libraries().await
}

#[tauri::command]
async fn library_documents(
    state: State<'_, AppState>,
    library_id: String,
    include_absent: Option<bool>,
) -> Result<Library> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .remove_document(&library_id, &document_id)
        .await
}

#[tauri::command]
async fn version_remove(
    state: State<'_, AppState>,
    library_id: String,
    version_id: String,
) -> Result<Removal> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .reindex(&library_id, &document_id, &options.unwrap_or_default())
        .await
}

#[tauri::command]
async fn document_rebuild(
    state: State<'_, AppState>,
    library_id: String,
    document_id: String,
) -> Result<StartedRun> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .rebuild(&library_id, &document_id)
        .await
}

#[tauri::command]
async fn rebuild_gate(
    state: State<'_, AppState>,
    workflow_id: String,
) -> Result<Option<RebuildReport>> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).rebuild_gate(&workflow_id).await
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api).outline(&version_id).await
}

#[tauri::command]
async fn explore_section_chunks(
    state: State<'_, AppState>,
    section_id: String,
) -> Result<SectionChunks> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).section_chunks(&section_id).await
}

#[tauri::command]
async fn explore_chunk_context(
    state: State<'_, AppState>,
    chunk_id: String,
) -> Result<ChunkContext> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).chunk_context(&chunk_id).await
}

#[tauri::command]
async fn explore_concepts(
    state: State<'_, AppState>,
    version_id: String,
) -> Result<VersionConcepts> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).version_concepts(&version_id).await
}

#[tauri::command]
async fn explore_related(
    state: State<'_, AppState>,
    version_id: String,
) -> Result<RelatedDocuments> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).related_documents(&version_id).await
}

/// What the whole installation holds. The landing screen's only request.
///
/// Not library-scoped: its figures are project-wide, which is why Inicio owns no
/// library picker.
#[tauri::command]
async fn project_summary(state: State<'_, AppState>) -> Result<ProjectSummary> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).project_summary().await
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
    let stack = state.stack().await?;
    Control::new(stack.ports.api)
        .library_graph(&library_id, confidence_floor, min_documents)
        .await
}

#[tauri::command]
async fn explore_claims(
    state: State<'_, AppState>,
    concept_id: String,
) -> Result<ConceptClaims> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).concept_claims(&concept_id).await
}

/// Hand a question over. Returns as soon as the API has taken it, not when it
/// has an answer — `ask_result` collects that.
#[tauri::command]
async fn ask(state: State<'_, AppState>, question: Question) -> Result<AskStarted> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).ask(&question).await
}

#[tauri::command]
async fn ask_result(state: State<'_, AppState>, question_id: String) -> Result<AskProgress> {
    let stack = state.stack().await?;
    Control::new(stack.ports.api).ask_result(&question_id).await
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
            docker_probe,
            stack_status,
            stack_up,
            stack_down,
            stack_logs,
            control_health,
            control_ping,
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
