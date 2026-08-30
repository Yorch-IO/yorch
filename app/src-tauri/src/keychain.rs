//! The OS keychain, and the one honest thing to do when it is not there.
//!
//! Three secrets belong here rather than in a file under the app data
//! directory: the OAuth session (a refresh token good for thirty days), and
//! whatever provider credentials the paid stages ever need. `0600` on a file
//! protects against another *user*; the keychain also protects against another
//! *process running as this user*, which on a desktop is the realistic threat —
//! a browser extension, a random npm postinstall, anything the user ran once.
//!
//! **The fallback is deliberate, and it is loud.** A Linux box with no Secret
//! Service running — a server, a bare window manager, a container — has no
//! keychain at all, and `keyring` reports that as an error indistinguishable at
//! the call site from "the entry is not there". Refusing to run would make the
//! app unusable in exactly the environment its own integration tests live in.
//! So a store that cannot be reached degrades to the `0600` file the app used
//! before this module existed, and [`Backend::describe`] says which one is in
//! use so the UI can tell the user rather than quietly downgrading their
//! security.
//!
//! Values are opaque strings. The session is stored as its own JSON, so this
//! module never learns what a session is.

use std::path::{Path, PathBuf};

use crate::error::{AppError, Result};

/// Namespace for every entry. Changing it orphans every stored secret, which
/// costs the user one sign-in and no data.
const SERVICE: &str = "io.sek.companybrain";

/// Which store answered. Reported to the UI, never inferred from a failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub enum Backend {
    /// The OS keychain: Secret Service, Keychain Services, or Credential Manager.
    Keychain,
    /// A `0600` file. No keychain was reachable.
    File,
}

impl Backend {
    /// A stable token, not prose.
    ///
    /// The app ships `en` and `es` bundles and a test enforces key parity, so a
    /// Spanish string returned from Rust would be a user-facing message no
    /// bundle owns and no test can see. Same shape as an error `kind`: the
    /// frontend maps it.
    pub fn tag(self) -> &'static str {
        match self {
            Self::Keychain => "keychain",
            Self::File => "file",
        }
    }
}

/// One named secret, with a file to fall back to.
///
/// The fallback path is supplied by the caller rather than derived here,
/// because the app data directory is `AppState`'s to know and this module is
/// used from tests that must not touch it.
pub struct Entry {
    key: String,
    fallback: PathBuf,
}

impl Entry {
    pub fn new(key: &str, fallback: PathBuf) -> Self {
        Self { key: key.to_string(), fallback }
    }

    fn keyring(&self) -> Option<keyring::Entry> {
        keyring::Entry::new(SERVICE, &self.key).ok()
    }

    /// Read the secret. `None` means "not stored", never "could not ask".
    ///
    /// A keychain that is present but has no entry, and a keychain that is not
    /// there at all, both fall through to the file — the second because that is
    /// where the value would have been written, and the first because a
    /// previous version of this app wrote one there and has not migrated it
    /// yet. [`Self::set`] performs that migration on the next write.
    pub fn get(&self) -> (Option<String>, Backend) {
        if let Some(entry) = self.keyring() {
            match entry.get_password() {
                Ok(v) => return (Some(v), Backend::Keychain),
                // `NoEntry` from a *working* keychain still falls through: an
                // install that predates this module has its value in the file.
                Err(keyring::Error::NoEntry) => {
                    if let Some(v) = self.read_file() {
                        return (Some(v), Backend::File);
                    }
                    return (None, Backend::Keychain);
                }
                Err(_) => {}
            }
        }
        (self.read_file(), Backend::File)
    }

    /// Store the secret, preferring the keychain.
    ///
    /// On success the fallback file is **removed**, which is what makes this a
    /// migration rather than a second copy: leaving it would keep a refresh
    /// token readable on disk for the entry's whole life while the UI reported
    /// that it was in the keychain.
    pub fn set(&self, value: &str) -> Result<Backend> {
        if let Some(entry) = self.keyring() {
            if entry.set_password(value).is_ok() {
                let _ = std::fs::remove_file(&self.fallback);
                return Ok(Backend::Keychain);
            }
        }
        self.write_file(value)?;
        Ok(Backend::File)
    }

    /// Remove it from both stores. Absent is success — signing out twice is not
    /// a failure, and a half-cleared secret is worse than either outcome.
    pub fn delete(&self) -> Result<()> {
        if let Some(entry) = self.keyring() {
            let _ = entry.delete_credential();
        }
        match std::fs::remove_file(&self.fallback) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(AppError::io(self.fallback.display(), e)),
        }
    }

    fn read_file(&self) -> Option<String> {
        std::fs::read_to_string(&self.fallback).ok()
    }

    fn write_file(&self, value: &str) -> Result<()> {
        if let Some(parent) = self.fallback.parent() {
            std::fs::create_dir_all(parent).map_err(|e| AppError::io(parent.display(), e))?;
        }
        std::fs::write(&self.fallback, value)
            .map_err(|e| AppError::io(self.fallback.display(), e))?;
        restrict(&self.fallback)
    }
}

#[cfg(unix)]
fn restrict(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|e| AppError::io(path.display(), e))
}

#[cfg(not(unix))]
fn restrict(_path: &Path) -> Result<()> {
    // Windows inherits the per-user app data directory's owner-only ACL.
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A unique key per test run, so a suite run twice on a developer's machine
    /// does not read the previous run's secret out of their real keychain —
    /// and so two tests cannot see each other's.
    fn key(name: &str) -> String {
        format!(
            "test-{name}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        )
    }

    #[test]
    fn a_secret_round_trips_through_whichever_store_is_available() {
        let dir = tempfile::tempdir().unwrap();
        let e = Entry::new(&key("round"), dir.path().join("s"));
        assert_eq!(e.get().0, None);
        let backend = e.set("hola").unwrap();
        assert_eq!(e.get(), (Some("hola".to_string()), backend));
        e.delete().unwrap();
        assert_eq!(e.get().0, None);
    }

    /// The property that makes this a migration: after a successful keychain
    /// write there is no plaintext left on disk. Skipped, loudly, where there
    /// is no keychain — asserting it there would assert the fallback works,
    /// which is a different test.
    #[test]
    fn a_keychain_write_removes_the_file_it_replaces() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("s");
        let e = Entry::new(&key("migrate"), path.clone());
        std::fs::write(&path, "de-antes").unwrap();
        // What an install predating this module looks like.
        assert_eq!(e.get(), (Some("de-antes".to_string()), Backend::File));

        match e.set("nuevo").unwrap() {
            Backend::Keychain => {
                assert!(!path.exists(), "el fichero sobrevivió a la migración");
                assert_eq!(e.get(), (Some("nuevo".to_string()), Backend::Keychain));
                e.delete().unwrap();
            }
            Backend::File => {
                eprintln!("no hay llavero aquí; sólo se probó el respaldo");
                assert_eq!(std::fs::read_to_string(&path).unwrap(), "nuevo");
            }
        }
    }

    #[test]
    fn deleting_a_secret_that_is_not_there_is_not_a_failure() {
        let dir = tempfile::tempdir().unwrap();
        let e = Entry::new(&key("absent"), dir.path().join("s"));
        e.delete().unwrap();
        e.delete().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn the_fallback_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("s");
        // Written directly, so the assertion holds regardless of whether this
        // machine has a keychain that would have taken the value instead.
        let e = Entry::new(&key("mode"), path.clone());
        e.write_file("secreto").unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600, "modo {:o}", mode & 0o777);
    }
}
