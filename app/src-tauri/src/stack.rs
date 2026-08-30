//! Lifecycle of the backing Docker Compose stack.
//!
//! Compose is driven through its CLI rather than the Docker socket API. The CLI
//! is the interface Compose actually documents and versions; reimplementing
//! dependency ordering, health gating and `.env` interpolation against the raw
//! socket would mean owning behaviour that already exists and changes upstream.
//!
//! Two ways the compose files reach disk:
//!
//! * **Packaged** — the files are embedded at compile time and written into the
//!   app data directory on first run. There is no source tree to build from, so
//!   the worker image is pulled by tag.
//! * **Dev** — `COMPANY_BRAIN_REPO_ROOT` points at the checkout, `infra/` is
//!   used in place, and the dev overlay is layered on so the worker image is
//!   built from local source. The overlay's build context is the repo root,
//!   which only exists in this mode.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::process::Command;

use crate::error::{AppError, Result};
use crate::ports::Ports;

// Only the base file and the init script are embedded. The dev overlay is not:
// it is used exclusively when compose runs from the repo's own `infra/`
// directory, where the file is already present, and its build context is a
// source tree that a packaged install does not have.
const COMPOSE_BASE: &str = include_str!("../../../infra/docker-compose.yaml");
const COMPOSE_ADC: &str = include_str!("../../../infra/docker-compose.adc.yaml");
const INITDB_BRAIN: &str = include_str!("../../../infra/initdb/01-brain.sql");

pub const PROJECT_NAME: &str = "company-brain";
pub const REPO_ROOT_ENV: &str = "COMPANY_BRAIN_REPO_ROOT";

/// Services the app expects to exist, in the order the Stack screen lists them.
///
/// `migrate` is here even though it exits immediately, and deliberately: it is
/// the one-shot that owns the catalog schema, and a migration that failed is a
/// stack state an operator has to be able to see. It used to happen inside the
/// API's startup, where it was invisible.
pub const SERVICES: [&str; 8] = [
    "qdrant",
    "memgraph",
    "postgres",
    "temporal",
    "temporal-ui",
    "migrate",
    "api",
    "worker",
];

/// Declared in the compose file but behind a profile, so a default `up` never
/// starts them.
///
/// `backend` is the paid control plane: a free, self-managed stack has no
/// tenants and no Cognito pool, and listing it on the Stack screen would show a
/// permanently stopped service that is not supposed to be running. It is here
/// so `logs` can still reach it and so the inventory test stays exhaustive.
pub const PROFILE_SERVICES: [&str; 1] = ["backend"];

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DockerInfo {
    pub docker_version: String,
    pub compose_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceState {
    pub name: String,
    pub state: String,
    pub health: String,
    pub publishers: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StackStatus {
    pub project: String,
    pub compose_dir: String,
    pub workspace: String,
    pub ports: Ports,
    pub dev_mode: bool,
    pub services: Vec<ServiceState>,
}

#[derive(Debug, Clone)]
pub struct Stack {
    /// Directory compose runs from: `.env` and relative volume paths resolve here.
    pub compose_dir: PathBuf,
    pub workspace: PathBuf,
    pub secrets_file: PathBuf,
    pub ports: Ports,
    pub dev_mode: bool,
    pub pg_password: String,
    /// The Vertex AI billing project, or empty when the user has not named one.
    /// Empty is a working state, not a broken one: the stack comes up and every
    /// free stage runs; only paid ones report `provider_unconfigured`.
    pub gemini_project: String,
    /// Application Default Credentials, if a file was found. `None` means the
    /// ADC overlay is not applied at all — see `docker-compose.adc.yaml` for
    /// why an unconditional mount is worse than none.
    pub adc_file: Option<PathBuf>,
}

impl Stack {
    /// Ports from a previous launch's `.env`, if one is already there.
    ///
    /// Read back the same way `prepare` reads back the Postgres password: a
    /// port bound by our own still-running containers reads as "taken" to a
    /// fresh probe, so re-probing here would hand the new launch a *different*
    /// set and orphan the containers publishing the old one.
    pub fn saved_ports(app_data: &Path) -> Option<Ports> {
        let repo_root = std::env::var(REPO_ROOT_ENV).ok().map(PathBuf::from);
        let dev_mode = repo_root.as_ref().is_some_and(|r| r.join("infra").is_dir());
        let compose_dir = match (&repo_root, dev_mode) {
            (Some(root), true) => root.join("infra"),
            _ => app_data.join("stack"),
        };
        let env_path = compose_dir.join(".env");
        let get = |key: &str| existing_value(&env_path, key)?.parse::<u16>().ok();
        Some(Ports {
            qdrant_http: get("BRAIN_QDRANT_HTTP_PORT")?,
            qdrant_grpc: get("BRAIN_QDRANT_GRPC_PORT")?,
            memgraph: get("BRAIN_MEMGRAPH_PORT")?,
            postgres: get("BRAIN_POSTGRES_PORT")?,
            temporal: get("BRAIN_TEMPORAL_PORT")?,
            temporal_ui: get("BRAIN_TEMPORAL_UI_PORT")?,
            api: get("BRAIN_API_HOST_PORT")?,
        })
    }

    /// Lay out the stack directory and write the files compose needs.
    ///
    /// Idempotent: re-running keeps the existing Postgres password, because
    /// rotating it would lock the app out of the volume that still holds the
    /// old one.
    pub fn prepare(app_data: &Path, workspace: PathBuf, ports: Ports) -> Result<Self> {
        let repo_root = std::env::var(REPO_ROOT_ENV).ok().map(PathBuf::from);
        let dev_mode = repo_root.as_ref().is_some_and(|r| r.join("infra").is_dir());

        let compose_dir = match (&repo_root, dev_mode) {
            (Some(root), true) => root.join("infra"),
            _ => app_data.join("stack"),
        };

        if !dev_mode {
            write_if_changed(&compose_dir.join("docker-compose.yaml"), COMPOSE_BASE)?;
            write_if_changed(&compose_dir.join("docker-compose.adc.yaml"), COMPOSE_ADC)?;
            write_if_changed(&compose_dir.join("initdb").join("01-brain.sql"), INITDB_BRAIN)?;
        }

        check_native_filesystem(&workspace)?;
        std::fs::create_dir_all(&workspace).map_err(|e| AppError::io(workspace.display(), e))?;
        std::fs::create_dir_all(workspace.join("snapshots"))
            .map_err(|e| AppError::io(workspace.display(), e))?;

        let secrets_file = app_data.join("secrets.env");
        ensure_secrets_file(&secrets_file)?;

        let env_path = compose_dir.join(".env");
        let pg_password = existing_value(&env_path, "BRAIN_PG_PASSWORD").unwrap_or_else(new_password);
        // Read back for the same reason the password is: `write_env` rewrites
        // the whole file on every launch, so a value the user set once would be
        // erased by the next one.
        let gemini_project = existing_value(&env_path, "BRAIN_GEMINI_PROJECT_ID").unwrap_or_default();

        let stack = Self {
            compose_dir,
            workspace,
            secrets_file,
            ports,
            dev_mode,
            pg_password,
            gemini_project,
            adc_file: find_adc(),
        };
        stack.write_env(&env_path)?;
        Ok(stack)
    }

    fn write_env(&self, path: &Path) -> Result<()> {
        let mut body = String::from(
            "# Written by Company Brain. Edits are overwritten on the next launch.\n",
        );
        body.push_str(&format!("BRAIN_PROJECT={PROJECT_NAME}\n"));
        for (key, port) in self.ports.as_pairs() {
            body.push_str(&format!("{key}={port}\n"));
        }
        body.push_str(&format!("BRAIN_WORKSPACE={}\n", compose_path(&self.workspace)));
        body.push_str(&format!(
            "BRAIN_SECRETS_FILE={}\n",
            compose_path(&self.secrets_file)
        ));
        body.push_str("BRAIN_PG_USER=brain\n");
        body.push_str(&format!("BRAIN_PG_PASSWORD={}\n", self.pg_password));
        body.push_str("BRAIN_WORKER_IMAGE=company-brain-worker:dev\n");
        body.push_str(&format!(
            "BRAIN_GEMINI_PROJECT_ID={}\n",
            self.gemini_project
        ));
        if let Some(adc) = &self.adc_file {
            body.push_str(&format!("BRAIN_ADC_FILE={}\n", compose_path(adc)));
        }
        write_if_changed(path, &body)
    }

    /// The same stack with a different billing project, written to `.env`.
    ///
    /// Returns a new value rather than mutating: the caller holds an `Arc` and
    /// swaps it, which keeps the field immutable everywhere else.
    ///
    /// **The containers do not see this until they are recreated.** Compose
    /// interpolates `.env` at `up` time, so a project set while the stack is
    /// running reaches the API on the next `stack_up` and not before. The UI
    /// says so rather than leaving the user to discover it from an unchanged
    /// error message.
    pub fn with_gemini_project(&self, project_id: &str) -> Result<Self> {
        let project_id = project_id.trim();
        validate_project_id(project_id)?;
        let updated = Self {
            gemini_project: project_id.to_string(),
            ..self.clone()
        };
        updated.write_env(&updated.compose_dir.join(".env"))?;
        Ok(updated)
    }

    fn compose_args(&self) -> Vec<String> {
        let mut args = vec![
            "compose".to_string(),
            "-p".to_string(),
            PROJECT_NAME.to_string(),
            "-f".to_string(),
            "docker-compose.yaml".to_string(),
        ];
        if self.dev_mode {
            args.push("-f".to_string());
            args.push("docker-compose.dev.yaml".to_string());
        }
        // Applied only when a credentials file was actually found. Compose
        // creates a *directory* at a bind mount's source when it does not
        // exist, so mounting unconditionally would put an empty directory where
        // the credentials belong and fail at authentication with no hint why.
        if self.adc_file.is_some() {
            args.push("-f".to_string());
            args.push("docker-compose.adc.yaml".to_string());
        }
        args
    }

    async fn compose(&self, extra: &[&str]) -> Result<String> {
        let mut args = self.compose_args();
        args.extend(extra.iter().map(|s| s.to_string()));

        let output = Command::new("docker")
            .args(&args)
            .current_dir(&self.compose_dir)
            .stdin(Stdio::null())
            .output()
            .await
            .map_err(|e| AppError::DockerMissing(e.to_string()))?;

        if !output.status.success() {
            return Err(AppError::Compose {
                command: extra.join(" "),
                code: output.status.code().unwrap_or(-1),
                stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
            });
        }
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    }

    pub async fn up(&self) -> Result<String> {
        // --wait blocks until every service with a healthcheck reports healthy,
        // so a green return actually means the stack is usable. Without it
        // compose returns as soon as the containers exist and the first API
        // call races Temporal's schema setup.
        let mut args = vec!["up", "-d", "--wait", "--remove-orphans"];
        if self.dev_mode {
            args.push("--build");
        }
        self.compose(&args).await
    }

    pub async fn down(&self) -> Result<String> {
        // No -v: the volumes hold the vectors and the catalog. Removing them
        // because the user stopped the stack would be a data loss they never
        // asked for.
        self.compose(&["down", "--remove-orphans"]).await
    }

    pub async fn logs(&self, service: &str, tail: u32) -> Result<String> {
        if !SERVICES.contains(&service) && !PROFILE_SERVICES.contains(&service) {
            return Err(AppError::Config(format!("unknown service {service:?}")));
        }
        let tail = tail.to_string();
        self.compose(&["logs", "--no-color", "--tail", &tail, service])
            .await
    }

    pub async fn status(&self) -> Result<StackStatus> {
        let raw = self.compose(&["ps", "--all", "--format", "json"]).await?;
        Ok(StackStatus {
            project: PROJECT_NAME.to_string(),
            compose_dir: self.compose_dir.display().to_string(),
            workspace: self.workspace.display().to_string(),
            ports: self.ports,
            dev_mode: self.dev_mode,
            services: parse_ps(&raw),
        })
    }
}

/// Parse `docker compose ps --format json`.
///
/// The output is JSON Lines on Compose v2 and a JSON array on some builds, so
/// both are accepted. Services compose has never created are reported as
/// `absent` rather than omitted — the Stack screen needs a row for every
/// expected service, not only the ones that happen to exist.
pub fn parse_ps(raw: &str) -> Vec<ServiceState> {
    #[derive(Deserialize)]
    struct Row {
        #[serde(default, alias = "Service")]
        service: String,
        #[serde(default, alias = "State")]
        state: String,
        #[serde(default, alias = "Health")]
        health: String,
        #[serde(default, alias = "Publishers")]
        publishers: Option<Vec<Publisher>>,
    }
    #[derive(Deserialize)]
    struct Publisher {
        #[serde(default, alias = "PublishedPort")]
        published_port: u16,
        #[serde(default, alias = "TargetPort")]
        target_port: u16,
    }

    let mut rows: Vec<Row> = Vec::new();
    let trimmed = raw.trim();
    if trimmed.starts_with('[') {
        rows = serde_json::from_str(trimmed).unwrap_or_default();
    } else {
        for line in trimmed.lines().filter(|l| !l.trim().is_empty()) {
            if let Ok(row) = serde_json::from_str::<Row>(line) {
                rows.push(row);
            }
        }
    }

    SERVICES
        .iter()
        .map(|name| match rows.iter().find(|r| r.service == *name) {
            Some(r) => ServiceState {
                name: (*name).to_string(),
                state: if r.state.is_empty() {
                    "unknown".to_string()
                } else {
                    r.state.clone()
                },
                health: if r.health.is_empty() {
                    "none".to_string()
                } else {
                    r.health.clone()
                },
                publishers: r
                    .publishers
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .filter(|p| p.published_port != 0)
                    .map(|p| format!("{}→{}", p.published_port, p.target_port))
                    .collect(),
            },
            None => ServiceState {
                name: (*name).to_string(),
                state: "absent".to_string(),
                health: "none".to_string(),
                publishers: Vec::new(),
            },
        })
        .collect()
}

pub async fn probe_docker() -> Result<DockerInfo> {
    let docker = run_capture("docker", &["version", "--format", "{{.Server.Version}}"]).await?;
    let compose = run_capture("docker", &["compose", "version", "--short"]).await?;
    Ok(DockerInfo {
        docker_version: docker.trim().to_string(),
        compose_version: compose.trim().to_string(),
    })
}

async fn run_capture(program: &str, args: &[&str]) -> Result<String> {
    let out = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .await
        .map_err(|e| AppError::DockerMissing(format!("could not run `{program}`: {e}")))?;
    if !out.status.success() {
        return Err(AppError::DockerMissing(
            String::from_utf8_lossy(&out.stderr).trim().to_string(),
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

/// Refuse a workspace on a Windows drive mounted into WSL.
///
/// Bind mounts through `/mnt/c` cross a 9p filesystem boundary: throughput
/// collapses and POSIX permissions do not survive, so Postgres and Qdrant both
/// misbehave in ways that look like corruption rather than configuration.
fn check_native_filesystem(workspace: &Path) -> Result<()> {
    if cfg!(target_os = "linux") && workspace.starts_with("/mnt/") {
        return Err(AppError::WorkspaceNotNative {
            path: workspace.display().to_string(),
        });
    }
    Ok(())
}

fn ensure_secrets_file(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| AppError::io(parent.display(), e))?;
    }
    if !path.exists() {
        std::fs::write(path, "# Provider credentials, written from the OS keychain.\n")
            .map_err(|e| AppError::io(path.display(), e))?;
    }
    restrict_permissions(path)
}

#[cfg(unix)]
fn restrict_permissions(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|e| AppError::io(path.display(), e))
}

#[cfg(not(unix))]
fn restrict_permissions(_path: &Path) -> Result<()> {
    // Windows inherits the ACL of the per-user app data directory, which is
    // already owner-only. There is no mode bit to set.
    Ok(())
}

/// A path as compose should see it in `.env`.
///
/// Backslashes are replaced with forward slashes, which Docker Desktop accepts
/// in volume definitions on Windows. Whether a backslash in a `.env` value is
/// treated as an escape has varied between Compose versions, so a Windows path
/// written verbatim is a coin flip on `C:\Users\...` becoming `C:Users...`.
/// Forward slashes remove the question. No-op on Unix, where paths contain no
/// backslashes to begin with.
fn compose_path(path: &Path) -> String {
    path.display().to_string().replace('\\', "/")
}

fn write_if_changed(path: &Path, body: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| AppError::io(parent.display(), e))?;
    }
    if std::fs::read_to_string(path).is_ok_and(|existing| existing == body) {
        return Ok(());
    }
    std::fs::write(path, body).map_err(|e| AppError::io(path.display(), e))
}

fn existing_value(env_path: &Path, key: &str) -> Option<String> {
    let body = std::fs::read_to_string(env_path).ok()?;
    body.lines()
        .find_map(|l| l.strip_prefix(&format!("{key}=")))
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// Where Application Default Credentials live, if they do.
///
/// Two places, in the order the Google libraries themselves consult them:
/// `GOOGLE_APPLICATION_CREDENTIALS` when it points at a file that exists, then
/// the well-known path `gcloud auth application-default login` writes. Looking
/// only at the well-known path would ignore a user who had already pointed the
/// standard variable at a service-account key.
///
/// Returns `None` rather than a default path, and the difference matters: a path
/// that does not exist must not reach a compose bind mount, which would create a
/// directory there. See `docker-compose.adc.yaml`.
fn find_adc() -> Option<PathBuf> {
    if let Some(from_env) = std::env::var_os("GOOGLE_APPLICATION_CREDENTIALS") {
        let path = PathBuf::from(from_env);
        if path.is_file() {
            return Some(path);
        }
    }
    well_known_adc().filter(|p| p.is_file())
}

fn well_known_adc() -> Option<PathBuf> {
    // Windows keeps it under APPDATA; every other platform uses ~/.config,
    // including macOS, which is unusual for it but is what gcloud does.
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(not(windows))]
    let base = std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config"));

    base.map(|b| {
        b.join("gcloud")
            .join("application_default_credentials.json")
    })
}

/// Google's own rule: 6-30 characters, lowercase letter first, then lowercase
/// letters, digits or hyphens, and no trailing hyphen.
///
/// Checked here rather than left to the API for two reasons. A typo would
/// otherwise surface as a 403 three stages into a paid run, far from its cause.
/// And this value is written into `.env` verbatim, where a newline or a `$`
/// would break compose interpolation for every service — so rejecting the
/// characters that cannot appear in a real project id is also what keeps a
/// stray one out of the file.
fn validate_project_id(id: &str) -> Result<()> {
    let ok = (6..=30).contains(&id.len())
        && id.starts_with(|c: char| c.is_ascii_lowercase())
        && !id.ends_with('-')
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
    if ok {
        return Ok(());
    }
    Err(AppError::Config(format!(
        "{id:?} is not a Google Cloud project id: 6-30 characters, starting with a \
         lowercase letter, then lowercase letters, digits or hyphens"
    )))
}

fn new_password() -> String {
    use rand::Rng;
    const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    let mut rng = rand::thread_rng();
    (0..32)
        .map(|_| ALPHABET[rng.gen_range(0..ALPHABET.len())] as char)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ps_reports_a_row_for_every_expected_service() {
        let states = parse_ps("");
        assert_eq!(states.len(), SERVICES.len());
        assert!(states.iter().all(|s| s.state == "absent"));
    }

    #[test]
    fn ps_parses_json_lines() {
        let raw = r#"{"Service":"qdrant","State":"running","Health":"healthy","Publishers":[{"PublishedPort":6433,"TargetPort":6333}]}
{"Service":"postgres","State":"running","Health":"healthy","Publishers":[]}"#;
        let states = parse_ps(raw);
        let qdrant = states.iter().find(|s| s.name == "qdrant").expect("qdrant");
        assert_eq!(qdrant.state, "running");
        assert_eq!(qdrant.health, "healthy");
        assert_eq!(qdrant.publishers, vec!["6433→6333".to_string()]);
        // Not started yet, but still listed.
        let worker = states.iter().find(|s| s.name == "worker").expect("worker");
        assert_eq!(worker.state, "absent");
    }

    #[test]
    fn ps_parses_a_json_array() {
        let raw = r#"[{"Service":"api","State":"exited","Health":""}]"#;
        let api = parse_ps(raw)
            .into_iter()
            .find(|s| s.name == "api")
            .expect("api");
        assert_eq!(api.state, "exited");
        assert_eq!(api.health, "none");
    }

    #[test]
    fn ps_ignores_unpublished_ports() {
        let raw = r#"{"Service":"worker","State":"running","Health":"","Publishers":[{"PublishedPort":0,"TargetPort":8000}]}"#;
        let worker = parse_ps(raw)
            .into_iter()
            .find(|s| s.name == "worker")
            .expect("worker");
        assert!(worker.publishers.is_empty());
    }

    #[test]
    fn the_service_list_matches_the_compose_file() {
        // SERVICES drives the Stack screen's rows. If compose gains or loses a
        // service and this list does not follow, the UI either shows a
        // permanently "absent" row for something that no longer exists or hides
        // a service that is genuinely failing.
        let declared: Vec<&str> = COMPOSE_BASE
            .lines()
            .skip_while(|l| !l.starts_with("services:"))
            .skip(1)
            .take_while(|l| l.starts_with(' ') || l.trim().is_empty())
            .filter_map(|l| {
                let indent = l.len() - l.trim_start().len();
                let trimmed = l.trim_end();
                // Comments are skipped rather than shaped around. A prose line
                // ending in a colon at this indent is ordinary in a file that
                // explains itself, and reading one as a service name produced a
                // failure naming the sentence.
                if trimmed.trim_start().starts_with('#') {
                    return None;
                }
                (indent == 2 && trimmed.ends_with(':')).then(|| trimmed.trim().trim_end_matches(':'))
            })
            .collect();

        // Both lists, because the file declares both and a service missing
        // from *either* is what this test exists to catch.
        let mut expected = SERVICES.to_vec();
        expected.extend_from_slice(&PROFILE_SERVICES);
        expected.sort_unstable();
        let mut found = declared;
        found.sort_unstable();
        assert_eq!(found, expected);
    }

    #[test]
    fn every_published_port_is_bound_to_loopback() {
        // The stack has no authentication anywhere: Qdrant runs without an API
        // key and Temporal without TLS. A port published on 0.0.0.0 would put
        // both on the network.
        // Scoped to `ports:` blocks rather than matched on shape. The earlier
        // version treated any quoted sequence entry containing a colon as a
        // mapping, which a `command:` flag like "--memory-limit=${X:-2048}"
        // satisfies — so adding one to a service failed this test with a
        // message about loopback binding that had nothing to do with the cause.
        let mut in_ports = false;
        let mut checked = 0;
        for (n, line) in COMPOSE_BASE.lines().enumerate() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            if !trimmed.starts_with('-') {
                let indent = line.len() - line.trim_start().len();
                in_ports = indent == 4 && trimmed == "ports:";
                continue;
            }
            if !in_ports {
                continue;
            }
            let mapping = trimmed.trim_start_matches("- ").trim_matches('"');
            assert!(
                mapping.starts_with("127.0.0.1:"),
                "line {}: port mapping {trimmed} is not bound to loopback",
                n + 1
            );
            checked += 1;
        }
        // A scoping bug that matched nothing would make this test pass silently.
        // Seven stores and control planes, plus the paid plane's own port.
        // `migrate` publishes nothing: it runs and exits.
        assert_eq!(checked, 8, "expected one published mapping per exposed port");
    }

    #[test]
    fn env_paths_use_forward_slashes_for_compose() {
        // A Windows app-data path written verbatim into .env can lose its
        // separators depending on how the Compose version in use treats a
        // backslash. Docker Desktop accepts C:/... in volumes, so normalising
        // sidesteps the question entirely.
        assert_eq!(
            compose_path(Path::new(r"C:\Users\me\AppData\Roaming\brain\workspace")),
            "C:/Users/me/AppData/Roaming/brain/workspace"
        );
        // Unchanged on Unix.
        assert_eq!(
            compose_path(Path::new("/home/me/.local/share/company-brain")),
            "/home/me/.local/share/company-brain"
        );
    }

    #[test]
    fn generated_passwords_differ() {
        assert_ne!(new_password(), new_password());
        assert_eq!(new_password().len(), 32);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_windows_drive_mount_is_refused_as_a_workspace() {
        let err = check_native_filesystem(Path::new("/mnt/c/Users/me/brain")).unwrap_err();
        assert!(matches!(err, AppError::WorkspaceNotNative { .. }));
        check_native_filesystem(Path::new("/home/me/.local/share/company-brain"))
            .expect("a native path is fine");
    }
}

#[cfg(test)]
mod provider {
    //! Plumbing the billing project and the credentials to the containers.
    //!
    //! Until this existed, compose passed no project id and mounted no ADC, so
    //! `POST /ask` from the window answered `provider_unconfigured` and an
    //! ingest would have died at its first paid stage. Every real run had gone
    //! through host dev mode with the environment exported by hand, which is
    //! exactly why nobody had noticed.

    use super::*;

    fn stack(dir: &Path) -> Stack {
        Stack {
            compose_dir: dir.to_path_buf(),
            workspace: dir.join("workspace"),
            secrets_file: dir.join("secrets.env"),
            ports: Ports::default(),
            dev_mode: false,
            pg_password: "pw".into(),
            gemini_project: String::new(),
            adc_file: None,
        }
    }

    #[test]
    fn the_project_reaches_compose_through_the_env_file() {
        let dir = tempfile::tempdir().unwrap();
        let s = stack(dir.path()).with_gemini_project("yorch-platform-prod").unwrap();
        let body = std::fs::read_to_string(dir.path().join(".env")).unwrap();
        assert!(body.contains("BRAIN_GEMINI_PROJECT_ID=yorch-platform-prod\n"), "{body}");
        assert_eq!(s.gemini_project, "yorch-platform-prod");
    }

    #[test]
    fn an_unset_project_is_written_empty_rather_than_omitted() {
        // The key must exist: `${BRAIN_GEMINI_PROJECT_ID:-}` would interpolate
        // fine either way, but compose warns on an undefined variable and a
        // warning per service on every launch trains the user to ignore them.
        let dir = tempfile::tempdir().unwrap();
        stack(dir.path()).write_env(&dir.path().join(".env")).unwrap();
        let body = std::fs::read_to_string(dir.path().join(".env")).unwrap();
        assert!(body.contains("BRAIN_GEMINI_PROJECT_ID=\n"), "{body}");
    }

    #[test]
    fn a_project_set_once_survives_the_next_launch() {
        // `write_env` rewrites the whole file every time, so this is the same
        // hazard the Postgres password has: without the read-back, relaunching
        // would silently unconfigure the provider.
        let dir = tempfile::tempdir().unwrap();
        let env = dir.path().join(".env");
        stack(dir.path()).with_gemini_project("yorch-platform-prod").unwrap();
        assert_eq!(
            existing_value(&env, "BRAIN_GEMINI_PROJECT_ID").as_deref(),
            Some("yorch-platform-prod")
        );
    }

    #[test]
    fn the_adc_overlay_is_applied_only_when_credentials_were_found() {
        let dir = tempfile::tempdir().unwrap();
        let without = stack(dir.path());
        assert!(!without.compose_args().contains(&"docker-compose.adc.yaml".to_string()));

        let with = Stack { adc_file: Some(dir.path().join("adc.json")), ..without };
        assert!(with.compose_args().contains(&"docker-compose.adc.yaml".to_string()));
    }

    #[test]
    fn the_credentials_path_is_written_only_alongside_the_overlay() {
        // A `BRAIN_ADC_FILE` naming a file that does not exist would become a
        // *directory* at the bind mount's source the moment the overlay was
        // applied, and authentication would then fail with nothing pointing at
        // the cause.
        let dir = tempfile::tempdir().unwrap();
        stack(dir.path()).write_env(&dir.path().join(".env")).unwrap();
        let body = std::fs::read_to_string(dir.path().join(".env")).unwrap();
        assert!(!body.contains("BRAIN_ADC_FILE"), "{body}");
    }

    #[test]
    fn a_typo_is_refused_before_it_reaches_a_paid_run() {
        for bad in [
            "",                                // unset is set through the UI's own path
            "ab",                              // too short
            "Yorch-Platform",                  // uppercase
            "9yorch-platform",                 // leading digit
            "yorch-platform-",                 // trailing hyphen
            "yorch platform",                  // space
            "yorch\nBRAIN_PG_PASSWORD=hunter2", // would forge a second .env line
            "a-very-long-project-id-that-exceeds-the-limit",
        ] {
            assert!(validate_project_id(bad).is_err(), "accepted {bad:?}");
        }
        for good in ["yorch-platform-prod", "abcdef", "a1-b2-c3"] {
            assert!(validate_project_id(good).is_ok(), "refused {good:?}");
        }
    }

    #[test]
    fn a_rejected_project_leaves_the_previous_one_in_place() {
        // The setter must be all-or-nothing: half-applying it would leave the
        // file naming a project the struct does not.
        let dir = tempfile::tempdir().unwrap();
        let good = stack(dir.path()).with_gemini_project("yorch-platform-prod").unwrap();
        assert!(good.with_gemini_project("NOPE").is_err());
        assert_eq!(
            existing_value(&dir.path().join(".env"), "BRAIN_GEMINI_PROJECT_ID").as_deref(),
            Some("yorch-platform-prod")
        );
    }

    #[test]
    fn the_project_is_trimmed_because_a_pasted_id_carries_whitespace() {
        let dir = tempfile::tempdir().unwrap();
        let s = stack(dir.path()).with_gemini_project("  yorch-platform-prod\n").unwrap();
        assert_eq!(s.gemini_project, "yorch-platform-prod");
    }

    #[test]
    fn the_adc_overlay_mounts_read_only_into_both_services() {
        // The container has no reason to write the user's credentials, and a
        // refresh written inside a container is lost with it.
        assert!(COMPOSE_ADC.contains("/run/secrets/adc.json:ro"));
        assert!(COMPOSE_ADC.contains("GOOGLE_APPLICATION_CREDENTIALS"));
        assert!(COMPOSE_ADC.contains("worker: *adc"));
    }

    #[test]
    fn compose_declares_the_project_but_no_other_gemini_setting() {
        // `brainworker/config.py` owns those defaults. Repeating them here as
        // `${X:-default}` would put one constant in two files, which has
        // already drifted once in this repository.
        assert!(COMPOSE_BASE.contains("BRAIN_GEMINI_PROJECT_ID: ${BRAIN_GEMINI_PROJECT_ID:-}"));
        for absent in ["BRAIN_GEMINI_MODEL", "BRAIN_EMBEDDING_MODEL", "BRAIN_GEMINI_LOCATION"] {
            assert!(!COMPOSE_BASE.contains(absent), "{absent} duplicated in compose");
        }
    }
}
