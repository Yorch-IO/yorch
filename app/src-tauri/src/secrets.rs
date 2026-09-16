//! Provider credentials: the OS keychain, and the file the worker reads.
//!
//! This is the half of the keychain that was documented and empty. The comment
//! in `stack::ensure_secrets_file` has described a pipe with nothing in it since
//! it was written — the file was created, given `0600`, mounted read-only, and
//! *never written to by anything* — because the only provider this product had
//! was Vertex, which refuses API keys outright and uses Application Default
//! Credentials instead. The YouTube Data API is the first one with a key, and
//! this is the two `Entry` calls that root `CLAUDE.md` said it would take.
//!
//! Three rules the shape here exists to enforce:
//!
//! * **A value goes in and never comes back out.** [`status`] reports *whether*
//!   a secret is stored and which store holds it, never the secret. A field the
//!   UI could read back is a field a screenshot can leak, and nothing above Rust
//!   has a reason to hold a key.
//! * **`secrets.env` is rewritten whole**, like `.env` next to it. A key the
//!   user cleared has to disappear from the file, and a file that is only ever
//!   appended to would keep serving a credential that was revoked.
//! * **The entry is keyed by which install it belongs to**, exactly as
//!   `auth::session_entry` is and for the reason recorded there: a keychain is
//!   per-user and not per-directory, so two installs — or the test suite and the
//!   developer's own app — would otherwise share one entry and overwrite each
//!   other.

use std::path::Path;

use crate::error::{AppError, Result};
use crate::keychain::{Backend, Entry};

/// Every provider credential this app knows how to hold, and the environment
/// name the worker reads it under.
///
/// A table rather than a call site per secret, so that adding one is a line
/// here and the materialising, the clearing and the listing all follow. The
/// worker's own reader is `config.Settings.youtube_key`, which prefers this
/// file over the environment for the reason stated there.
pub const PROVIDER_SECRETS: &[(&str, &str)] = &[("youtube", "YOUTUBE_API_KEY")];

/// The environment name for an app-side secret name, if it is one we know.
pub fn env_name(name: &str) -> Option<&'static str> {
    PROVIDER_SECRETS
        .iter()
        .find(|(key, _)| *key == name)
        .map(|(_, env)| *env)
}

fn entry(app_data: &Path, name: &str) -> Entry {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    app_data.hash(&mut h);
    Entry::new(
        &format!("provider-{name}-{:016x}", h.finish()),
        app_data.join(format!("provider-{name}")),
    )
}

/// Store a credential, preferring the keychain, and rewrite `secrets.env`.
///
/// An empty value **clears** it rather than storing an empty string: "no key"
/// and "the key is the empty string" are the same thing to every reader, and
/// only one of them is a state a person can get out of.
pub fn set(app_data: &Path, secrets_file: &Path, name: &str, value: &str) -> Result<Backend> {
    if env_name(name).is_none() {
        return Err(AppError::Config(format!("no such provider secret: {name}")));
    }
    let trimmed = value.trim();
    let backend = if trimmed.is_empty() {
        entry(app_data, name).delete()?;
        Backend::Keychain
    } else {
        entry(app_data, name).set(trimmed)?
    };
    materialise(app_data, secrets_file)?;
    Ok(backend)
}

/// Whether a credential is stored, and where. **Never what it is.**
pub fn status(app_data: &Path, name: &str) -> (bool, Backend) {
    let (value, backend) = entry(app_data, name).get();
    (value.is_some_and(|v| !v.trim().is_empty()), backend)
}

/// Write every stored credential into the file the worker mounts.
///
/// Rewritten whole on every launch, like `.env`: a credential the user cleared
/// has to leave the file, and the alternative — appending — keeps serving a key
/// that was revoked. `0600` is re-applied afterwards because the write can
/// create the file.
///
/// Note what this does *not* do: restart anything. `config.load()` reads the
/// file once when the worker process starts, so a key set while the stack is up
/// reaches it on the next `up`. The screen says so, for the reason
/// `set_provider_project` says it about `.env`: silently accepting a setting
/// that does not take effect is how somebody concludes the feature is broken.
pub fn materialise(app_data: &Path, secrets_file: &Path) -> Result<()> {
    let mut body = String::from(
        "# Provider credentials, written from the OS keychain by Company Brain.\n\
         # Rewritten on every launch; edit the keychain, not this file.\n",
    );
    for (name, env) in PROVIDER_SECRETS {
        if let (Some(value), _) = entry(app_data, name).get() {
            let value = value.trim();
            if !value.is_empty() {
                body.push_str(&format!("{env}={value}\n"));
            }
        }
    }
    if let Some(parent) = secrets_file.parent() {
        std::fs::create_dir_all(parent).map_err(|e| AppError::io(parent.display(), e))?;
    }
    std::fs::write(secrets_file, body).map_err(|e| AppError::io(secrets_file.display(), e))?;
    restrict(secrets_file)
}

#[cfg(unix)]
fn restrict(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|e| AppError::io(path.display(), e))
}

#[cfg(not(unix))]
fn restrict(_path: &Path) -> Result<()> {
    // Windows inherits the ACL of the per-user app data directory, which is
    // already owner-only. There is no mode bit to set.
    Ok(())
}

/// Clear every provider credential this install holds. Used by the tests, and
/// by nothing else — a user clears one at a time from the screen.
#[cfg(test)]
pub fn clear_all(app_data: &Path) -> Result<()> {
    for (name, _) in PROVIDER_SECRETS {
        entry(app_data, name).delete()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn dirs() -> (TempDir, PathBuf) {
        let dir = TempDir::new().unwrap();
        let secrets = dir.path().join("secrets.env");
        (dir, secrets)
    }

    #[test]
    fn a_stored_key_reaches_the_file_the_worker_reads() {
        let (dir, secrets) = dirs();
        set(dir.path(), &secrets, "youtube", "AIzaSyTEST").unwrap();
        let body = std::fs::read_to_string(&secrets).unwrap();
        assert!(body.contains("YOUTUBE_API_KEY=AIzaSyTEST"), "{body}");
        clear_all(dir.path()).unwrap();
    }

    #[test]
    fn the_status_says_whether_and_never_what() {
        // A value the UI could read back is a value a screenshot can leak.
        let (dir, secrets) = dirs();
        assert_eq!(status(dir.path(), "youtube").0, false);
        set(dir.path(), &secrets, "youtube", "AIzaSyTEST").unwrap();
        assert_eq!(status(dir.path(), "youtube").0, true);
        clear_all(dir.path()).unwrap();
    }

    #[test]
    fn clearing_a_key_removes_it_from_the_file() {
        // The whole reason the file is rewritten rather than appended to: a
        // revoked key that stayed in it would keep being served.
        let (dir, secrets) = dirs();
        set(dir.path(), &secrets, "youtube", "AIzaSyTEST").unwrap();
        set(dir.path(), &secrets, "youtube", "   ").unwrap();
        let body = std::fs::read_to_string(&secrets).unwrap();
        assert!(!body.contains("AIzaSyTEST"), "{body}");
        assert!(!body.contains("YOUTUBE_API_KEY="), "{body}");
        assert_eq!(status(dir.path(), "youtube").0, false);
    }

    #[test]
    fn materialising_with_nothing_stored_leaves_a_readable_file() {
        // The stack mounts this path read-only; a missing file makes compose
        // create a *directory* at the mount point, which is the failure the ADC
        // overlay is applied conditionally to avoid.
        let (dir, secrets) = dirs();
        materialise(dir.path(), &secrets).unwrap();
        assert!(secrets.is_file());
        assert!(std::fs::read_to_string(&secrets).unwrap().starts_with('#'));
    }

    #[cfg(unix)]
    #[test]
    fn the_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let (dir, secrets) = dirs();
        set(dir.path(), &secrets, "youtube", "AIzaSyTEST").unwrap();
        let mode = std::fs::metadata(&secrets).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        clear_all(dir.path()).unwrap();
    }

    #[test]
    fn an_unknown_secret_is_refused_rather_than_stored_nowhere() {
        let (dir, secrets) = dirs();
        assert!(set(dir.path(), &secrets, "openai", "sk-test").is_err());
    }

    #[test]
    fn two_installs_do_not_share_an_entry() {
        // The reason `auth::session_entry` carries a digest of the app data
        // directory: a keychain is per-user, so without it the test suite and
        // the developer's own app clobber each other.
        let (a, sa) = dirs();
        let (b, sb) = dirs();
        set(a.path(), &sa, "youtube", "key-a").unwrap();
        set(b.path(), &sb, "youtube", "key-b").unwrap();
        assert!(std::fs::read_to_string(&sa).unwrap().contains("key-a"));
        assert!(std::fs::read_to_string(&sb).unwrap().contains("key-b"));
        clear_all(a.path()).unwrap();
        clear_all(b.path()).unwrap();
    }
}

