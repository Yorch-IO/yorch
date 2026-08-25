//! The single error type crossing the IPC boundary.
//!
//! Tauri requires command error types to be `Serialize`. Rather than
//! stringifying at each call site, every command returns this enum and the
//! frontend receives a tagged object it can branch on — a missing Docker
//! install needs a different screen from a failed HTTP call, and the UI cannot
//! tell them apart from a flat string.

use serde::{Serialize, Serializer};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("Docker is not available: {0}")]
    DockerMissing(String),

    #[error("docker compose {command} failed ({code}): {stderr}")]
    Compose {
        command: String,
        code: i32,
        stderr: String,
    },

    #[error("the control API is not reachable at {url}: {source}")]
    ControlUnreachable {
        url: String,
        #[source]
        source: reqwest::Error,
    },

    /// Told apart from `ControlUnreachable` because the two need opposite
    /// advice. "It may still be starting" is true of a refused connection and a
    /// lie about an expired deadline — the API answered, just not in time — and
    /// saying it anyway sent two investigations at the wrong component.
    #[error("the control API did not answer within the time allowed at {url}: {source}")]
    ControlTimeout {
        url: String,
        #[source]
        source: reqwest::Error,
    },

    #[error("the control API returned {status}: {body}")]
    ControlStatus { status: u16, body: String },

    #[error("no free port found in {start}..{end} for {purpose}")]
    NoFreePort {
        purpose: String,
        start: u16,
        end: u16,
    },

    #[error("workspace must live on the native filesystem, not {path} — bind mounts there are slow and lose permissions")]
    WorkspaceNotNative { path: String },

    #[error("io error at {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },

    #[error("{0}")]
    Config(String),
}

impl AppError {
    /// A stable machine-readable tag. The frontend switches on this; the
    /// message is for humans and may be reworded freely.
    pub(crate) fn kind(&self) -> &'static str {
        match self {
            Self::DockerMissing(_) => "docker_missing",
            Self::Compose { .. } => "compose_failed",
            Self::ControlUnreachable { .. } => "control_unreachable",
            Self::ControlTimeout { .. } => "control_timeout",
            Self::ControlStatus { .. } => "control_status",
            Self::NoFreePort { .. } => "no_free_port",
            Self::WorkspaceNotNative { .. } => "workspace_not_native",
            Self::Io { .. } => "io",
            Self::Config(_) => "config",
        }
    }

    /// Takes `Display` rather than `Into<String>` so `path.display()` can be
    /// passed straight through — the common case at every call site.
    pub fn io(path: impl std::fmt::Display, source: std::io::Error) -> Self {
        Self::Io {
            path: path.to_string(),
            source,
        }
    }
}

impl Serialize for AppError {
    // `std::result::Result` spelled out: the `Result<T>` alias below shadows the
    // prelude's within this module, and serde's signature needs the two-parameter
    // form.
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut s = serializer.serialize_struct("AppError", 2)?;
        s.serialize_field("kind", self.kind())?;
        s.serialize_field("message", &self.to_string())?;
        s.end()
    }
}

pub type Result<T> = std::result::Result<T, AppError>;
