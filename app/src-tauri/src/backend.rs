//! Which control plane this app is talking to.
//!
//! Company Brain ships as two products against one codebase: a free,
//! self-managed stack the user runs on their own machine, and a paid,
//! multi-tenant service. They speak the *same 26 paths* with the same payloads —
//! that parity is deliberate and tested on the server side — so the difference
//! the app has to carry is small: a base URL, and whether to send credentials.
//!
//! Keeping it that small is the point. Everything above this module addresses
//! endpoints by path and knows nothing about which plane answered.
//!
//! **Cloud mode needs no Docker.** `AppState::control` only lays out the local
//! stack when the mode is local, so a user of the paid service never has the
//! app try to allocate ports or find a container runtime.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{AppError, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum BackendMode {
    /// The FastAPI plane on this machine. No authentication, loopback only.
    #[default]
    Local,
    /// The NestJS plane. Cognito bearer token, and a tenant when the account
    /// belongs to more than one.
    Cloud,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, rename_all = "camelCase")]
pub struct BackendSettings {
    pub mode: BackendMode,
    /// Where the paid plane lives. Empty until someone sets it.
    pub base_url: String,
    /// Sent as `X-Tenant-Id`. Optional: an account belonging to exactly one
    /// organisation does not need it, and the server refuses to guess for one
    /// that belongs to several rather than picking the first.
    pub tenant_id: String,
}

impl BackendSettings {
    fn path(app_data: &Path) -> PathBuf {
        app_data.join("backend.json")
    }

    /// Read the saved settings, falling back to local mode.
    ///
    /// A malformed file is *not* an error: it lands the user in local mode,
    /// which is the mode that works without any configuration at all. Refusing
    /// to start over a settings file would take away the screen that could fix
    /// it.
    pub fn load(app_data: &Path) -> Self {
        std::fs::read_to_string(Self::path(app_data))
            .ok()
            .and_then(|body| serde_json::from_str(&body).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, app_data: &Path) -> Result<()> {
        let path = Self::path(app_data);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| AppError::io(parent.display(), e))?;
        }
        let body = serde_json::to_string_pretty(self)
            .map_err(|e| AppError::Config(format!("cannot serialise backend settings: {e}")))?;
        std::fs::write(&path, body).map_err(|e| AppError::io(path.display(), e))
    }

    /// Rejects a base URL that would silently not work, in the request that set
    /// it rather than on the next call to every screen.
    pub fn validated(mode: BackendMode, base_url: &str, tenant_id: &str) -> Result<Self> {
        let base_url = base_url.trim().trim_end_matches('/').to_string();
        let tenant_id = tenant_id.trim().to_string();

        if mode == BackendMode::Cloud {
            if base_url.is_empty() {
                return Err(AppError::Config(
                    "el modo de pago necesita la dirección del servicio".into(),
                ));
            }
            if !base_url.starts_with("http://") && !base_url.starts_with("https://") {
                return Err(AppError::Config(format!(
                    "la dirección debe empezar por http:// o https://, no {base_url:?}"
                )));
            }
        }
        Ok(Self {
            mode,
            base_url,
            tenant_id,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_to_the_local_plane() {
        let dir = tempfile::tempdir().unwrap();
        let settings = BackendSettings::load(dir.path());
        assert_eq!(settings.mode, BackendMode::Local);
        assert!(settings.base_url.is_empty());
    }

    #[test]
    fn a_saved_choice_survives_a_restart() {
        let dir = tempfile::tempdir().unwrap();
        let saved =
            BackendSettings::validated(BackendMode::Cloud, "https://brain.example.com/", "tnt_x")
                .unwrap();
        saved.save(dir.path()).unwrap();
        let read = BackendSettings::load(dir.path());
        assert_eq!(read.mode, BackendMode::Cloud);
        // The trailing slash is dropped on the way in, so no path is ever built
        // with a double one.
        assert_eq!(read.base_url, "https://brain.example.com");
        assert_eq!(read.tenant_id, "tnt_x");
    }

    #[test]
    fn a_corrupt_settings_file_lands_in_local_mode_rather_than_failing() {
        // Refusing to start over a settings file would take away the screen
        // that could fix it.
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(BackendSettings::path(dir.path()), "{ no es json").unwrap();
        assert_eq!(BackendSettings::load(dir.path()).mode, BackendMode::Local);
    }

    #[test]
    fn cloud_mode_without_an_address_is_refused_where_it_is_set() {
        let err = BackendSettings::validated(BackendMode::Cloud, "  ", "").unwrap_err();
        assert_eq!(err.kind(), "config");
    }

    #[test]
    fn an_address_with_no_scheme_is_refused() {
        // reqwest would fail per request with something far less specific.
        let err = BackendSettings::validated(BackendMode::Cloud, "brain.example.com", "").unwrap_err();
        assert_eq!(err.kind(), "config");
    }

    #[test]
    fn local_mode_needs_no_address() {
        assert!(BackendSettings::validated(BackendMode::Local, "", "").is_ok());
    }

}
