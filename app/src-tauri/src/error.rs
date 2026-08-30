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
    ControlStatus {
        status: u16,
        /// The response body, truncated for a human to read.
        body: String,
        /// The control API's own `detail.kind`, taken from the body **before**
        /// it was cut. See `AppError::control_status`.
        control_kind: Option<String>,
    },

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

    /// Cloud mode with no token. Its own kind rather than a `Config`, because
    /// the fix is an action the user takes — sign in — and not a setting they
    /// mistyped.
    #[error("no hay sesión iniciada en el servicio de pago")]
    NotSignedIn,

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
            Self::NotSignedIn => "not_signed_in",
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

/// How much of a failed response is worth showing a person. The body is a
/// message, not data — anything the code acts on is read out before this.
const BODY_SHOWN: usize = 500;

impl AppError {
    /// A failure the control API reported, keeping its machine-readable tag.
    ///
    /// The tag is extracted **before** the body is truncated, and that ordering
    /// is the whole point of this constructor. `api.ts` used to dig
    /// `detail.kind` out of the message in the webview, which needed a balanced
    /// `{…}` to survive — so a long body lost its tag, and with it the one piece
    /// of advice the user could act on. `tenant_required` is the case that
    /// provoked it: it carries the caller's tenant ids as a sibling field, so
    /// its length grows with the account.
    ///
    /// Best-effort. A body that is not JSON, or that carries no `detail.kind`,
    /// simply has no tag — the same degradation as before, reached because the
    /// server sent none rather than because a truncation decided it.
    pub(crate) fn control_status(status: u16, body: String) -> Self {
        let control_kind = serde_json::from_str::<serde_json::Value>(&body)
            .ok()
            .as_ref()
            .and_then(|v| v.get("detail"))
            .and_then(|d| d.get("kind"))
            .and_then(|k| k.as_str())
            .map(str::to_owned);
        Self::ControlStatus {
            status,
            body: body.chars().take(BODY_SHOWN).collect(),
            control_kind,
        }
    }

    fn control_kind(&self) -> Option<&str> {
        match self {
            Self::ControlStatus { control_kind, .. } => control_kind.as_deref(),
            _ => None,
        }
    }
}

impl Serialize for AppError {
    // `std::result::Result` spelled out: the `Result<T>` alias below shadows the
    // prelude's within this module, and serde's signature needs the two-parameter
    // form.
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut s = serializer.serialize_struct("AppError", 3)?;
        s.serialize_field("kind", self.kind())?;
        s.serialize_field("message", &self.to_string())?;
        // Absent rather than null when there is none, so the webview's
        // `controlKind === undefined` check reads the same for a local error
        // and for a body that carried no tag.
        match self.control_kind() {
            Some(kind) => s.serialize_field("controlKind", &kind)?,
            None => s.skip_field("controlKind")?,
        }
        s.end()
    }
}

pub type Result<T> = std::result::Result<T, AppError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_long_body_keeps_the_tag_it_would_have_lost() {
        // The defect this constructor exists for. `tenant_required` carries the
        // caller's tenant ids as a sibling field, so its body grows with the
        // account — and the webview needed a balanced `{…}` in a message cut to
        // 500 characters to find the tag at all. Past the cut it found none,
        // and the user got a status code instead of the one line telling them
        // what to do about it.
        let tenants: Vec<String> = (0..40).map(|i| format!("tnt_{:024x}", i)).collect();
        let body = serde_json::json!({
            "detail": {
                "kind": "tenant_required",
                "message": "la cuenta pertenece a varias organizaciones",
                "tenants": tenants,
            }
        })
        .to_string();
        assert!(body.len() > BODY_SHOWN, "the body has to outgrow the cut");

        let e = AppError::control_status(400, body);
        assert_eq!(e.control_kind(), Some("tenant_required"));
        // And the message is still bounded, because it is shown to a person.
        let AppError::ControlStatus { body: shown, .. } = &e else {
            panic!("wrong variant")
        };
        assert_eq!(shown.chars().count(), BODY_SHOWN);
    }

    #[test]
    fn a_body_with_no_tag_simply_has_none() {
        // An unmatched route answers FastAPI's bare `{"detail": "Not Found"}` —
        // a string, not an object. A kind the guidance map has never heard of
        // would be worse than no kind, so nothing is invented here.
        for body in [
            r#"{"detail": "Not Found"}"#,
            r#"{"detail": {"message": "sin kind"}}"#,
            "no es json en absoluto",
            "",
        ] {
            assert_eq!(
                AppError::control_status(404, body.into()).control_kind(),
                None,
                "{body}"
            );
        }
    }

    #[test]
    fn the_tag_reaches_the_webview_and_a_missing_one_is_absent_rather_than_null() {
        // `controlKind === undefined` has to read the same for a local failure
        // and for a body that carried no tag, which is why the field is skipped
        // rather than serialised as null.
        let tagged = serde_json::to_value(AppError::control_status(
            403,
            r#"{"detail": {"kind": "no_membership", "message": "…"}}"#.into(),
        ))
        .unwrap();
        assert_eq!(tagged["kind"], "control_status");
        assert_eq!(tagged["controlKind"], "no_membership");

        let plain = serde_json::to_value(AppError::NotSignedIn).unwrap();
        assert_eq!(plain["kind"], "not_signed_in");
        assert!(plain.get("controlKind").is_none());
    }
}
