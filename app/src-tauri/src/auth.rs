//! Signing in to the paid plane: authorization code with PKCE.
//!
//! The desktop app is a **public client** — there is no secret it could keep, a
//! shipped binary being readable — which is exactly the case PKCE exists for.
//! The app invents a high-entropy `code_verifier`, sends only its SHA-256 to the
//! authorization server, and proves possession by presenting the original when
//! it redeems the code. An intercepted authorization code is then worth nothing
//! without the verifier, which never leaves this process except in that one
//! request.
//!
//! Two more things are load-bearing and easy to leave out:
//!
//! * **`state` is generated, and checked on the way back.** Without it a
//!   crafted callback to the loopback listener could hand this app somebody
//!   else's authorization code.
//! * **The listener binds `127.0.0.1` and answers exactly one request.** The
//!   redirect URI registered in Cognito is `http://localhost:8789/auth/callback`
//!   — Cognito refuses plain http for every host except localhost, which is the
//!   exception an installed app needs — and a socket left listening after a
//!   sign-in is a socket nobody is watching.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{AppError, Result};

/// Must match the redirect URI registered on the app client, exactly.
pub const CALLBACK_PORT: u16 = 8789;
const CALLBACK_PATH: &str = "/auth/callback";

/// How long the browser half may take. Generous: it includes typing a password
/// and possibly a one-time code.
const CALLBACK_TIMEOUT: Duration = Duration::from_secs(300);

/// Refresh this long before the token actually expires, so a request started
/// just under the wire does not arrive just over it.
const REFRESH_MARGIN: Duration = Duration::from_secs(60);

fn base64url(bytes: &[u8]) -> String {
    // Unpadded base64url, RFC 7636 §4.2. Hand-written rather than pulled in: it
    // is a fixed alphabet and a three-into-four regrouping, with no branch that
    // could be subtly wrong the way a hash implementation could.
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = u32::from(b[0]) << 16 | u32::from(b[1]) << 8 | u32::from(b[2]);
        let indices = [n >> 18 & 63, n >> 12 & 63, n >> 6 & 63, n & 63];
        // One output character per 6 bits that actually came from input.
        for i in 0..chunk.len() + 1 {
            out.push(ALPHABET[indices[i] as usize] as char);
        }
    }
    out
}

fn random_base64url(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    rand::thread_rng().fill(&mut buf[..]);
    base64url(&buf)
}

/// The secret half of a PKCE exchange, and the value derived from it.
#[derive(Debug, Clone)]
pub struct Pkce {
    pub verifier: String,
    pub challenge: String,
    pub state: String,
}

impl Pkce {
    pub fn new() -> Self {
        // 64 bytes → 86 characters, inside RFC 7636's 43..128 and well above the
        // 256 bits of entropy it asks for.
        let verifier = random_base64url(64);
        let challenge = base64url(&Sha256::digest(verifier.as_bytes()));
        Self {
            verifier,
            challenge,
            state: random_base64url(24),
        }
    }
}

/// What the paid plane says about signing in. Fetched from `/auth/config`, which
/// is public precisely so a client needs to know one address instead of three.
#[derive(Debug, Clone, Deserialize)]
pub struct AuthConfig {
    pub hosted_ui_domain: String,
    pub client_id: String,
}

/// Tokens as stored. `expires_at` is absolute so a restart can tell whether the
/// ID token is still usable without having to remember when it was fetched.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub id_token: String,
    pub refresh_token: String,
    pub expires_at: u64,
    /// For the Backend screen, so a person can see who they are signed in as.
    #[serde(default)]
    pub email: String,
}

impl Session {
    pub fn fresh(&self) -> bool {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.expires_at > now + REFRESH_MARGIN.as_secs()
    }
}

/// Where the session lives when there is no keychain to hold it.
///
/// Not the store of record any more — see [`session_entry`]. Kept as a named
/// function because it is also the file an installation predating the keychain
/// left behind, and [`crate::keychain::Entry`] migrates from it on the next
/// write.
pub fn session_path(app_data: &Path) -> PathBuf {
    app_data.join("session.json")
}

/// The session as a keychain entry, falling back to the file above.
///
/// It holds a refresh token good for thirty days, which is the single most
/// valuable secret this app stores: it mints ID tokens for the paid plane
/// without any further interaction. `0600` keeps it from another *user*; the
/// keychain also keeps it from another *process running as this user*, which on
/// a desktop is the threat that actually happens.
/// The session as a keychain entry, keyed by *which install* it belongs to.
///
/// The key carries a digest of the app data directory rather than being the
/// bare string "session", for two reasons that turned out to be the same one.
/// A keychain is per-user and not per-directory, so two installs pointed at
/// different data directories would otherwise share one entry and sign each
/// other out — and so would the test suite, which found this by clobbering the
/// entry the developer's own app was using and then reading it back in the next
/// test. A `tempdir` per test now yields a key per test, for free.
fn session_entry(app_data: &Path) -> crate::keychain::Entry {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    app_data.hash(&mut h);
    crate::keychain::Entry::new(&format!("session-{:016x}", h.finish()), session_path(app_data))
}

/// The stored session, and which store it came out of.
///
/// The backend is returned rather than logged, because it is something the user
/// should be able to see: an install that silently fell back to a file is less
/// protected than one that did not, and saying so is the difference between a
/// documented trade-off and a quiet downgrade.
pub fn load_session_with_backend(
    app_data: &Path,
) -> (Option<Session>, crate::keychain::Backend) {
    let (raw, backend) = session_entry(app_data).get();
    // A stored value that will not parse is treated as absent: the shape
    // changed between versions, and the fix is to sign in again, not to fail.
    (raw.and_then(|v| serde_json::from_str(&v).ok()), backend)
}

pub fn load_session(app_data: &Path) -> Option<Session> {
    load_session_with_backend(app_data).0
}

/// Store the session, preferring the OS keychain.
///
/// A successful keychain write deletes `session.json`, so an install that
/// predates this does not keep a readable refresh token on disk for the rest of
/// its life. Where no keychain is reachable — a bare window manager, a
/// container, a server — this is the `0600` file it always was.
pub fn save_session(app_data: &Path, session: &Session) -> Result<crate::keychain::Backend> {
    let body = serde_json::to_string(session)
        .map_err(|e| AppError::Config(format!("cannot serialise the session: {e}")))?;
    session_entry(app_data).set(&body)
}

/// Sign out. Clears both stores, because a keychain entry left behind after the
/// file was removed would sign the user straight back in.
pub fn clear_session(app_data: &Path) -> Result<()> {
    session_entry(app_data).delete()
}

/// The URL to send the browser to.
pub fn authorize_url(config: &AuthConfig, pkce: &Pkce) -> String {
    let redirect = format!("http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}");
    format!(
        "https://{domain}/oauth2/authorize\
         ?response_type=code&client_id={client}&redirect_uri={redirect}\
         &scope=openid+email+profile&code_challenge_method=S256&code_challenge={challenge}\
         &state={state}",
        domain = config.hosted_ui_domain,
        client = config.client_id,
        redirect = urlencode(&redirect),
        challenge = pkce.challenge,
        state = pkce.state,
    )
}

/// Percent-encoding for the handful of characters a redirect URI can contain.
fn urlencode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Wait for the browser to come back, on loopback, once.
///
/// Returns the authorization code. The `state` is compared here rather than by
/// the caller: a mismatch means this callback was not the one this process
/// started, and the code in it must not be redeemed.
pub fn await_callback(listener: TcpListener, expected_state: &str) -> Result<String> {
    listener
        .set_nonblocking(false)
        .map_err(|e| AppError::io("callback listener", e))?;

    let (mut socket, _) = listener
        .accept()
        .map_err(|e| AppError::io("callback listener", e))?;
    socket
        .set_read_timeout(Some(CALLBACK_TIMEOUT))
        .map_err(|e| AppError::io("callback listener", e))?;

    let mut line = String::new();
    BufReader::new(
        socket
            .try_clone()
            .map_err(|e| AppError::io("callback listener", e))?,
    )
    .read_line(&mut line)
    .map_err(|e| AppError::io("callback listener", e))?;

    let outcome = parse_callback(&line, expected_state);
    let page = match &outcome {
        Ok(_) => "<!doctype html><meta charset=utf-8><title>Listo</title><p>Sesión iniciada. Ya puedes volver a Company Brain.",
        Err(_) => "<!doctype html><meta charset=utf-8><title>Error</title><p>No se pudo completar el inicio de sesión. Vuelve a Company Brain e inténtalo otra vez.",
    };
    let _ = socket.write_all(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{page}",
            page.len()
        )
        .as_bytes(),
    );
    outcome
}

/// Split out from the socket so the parsing rules can be tested without one.
pub fn parse_callback(request_line: &str, expected_state: &str) -> Result<String> {
    let target = request_line
        .split_whitespace()
        .nth(1)
        .ok_or_else(|| AppError::Config("la respuesta del navegador no tiene ruta".into()))?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != CALLBACK_PATH {
        return Err(AppError::Config(format!(
            "el navegador volvió a {path}, que no es la ruta de retorno"
        )));
    }

    let mut code = None;
    let mut state = None;
    let mut error = None;
    for pair in query.split('&') {
        match pair.split_once('=') {
            Some(("code", v)) => code = Some(v.to_string()),
            Some(("state", v)) => state = Some(v.to_string()),
            Some(("error", v)) => error = Some(v.to_string()),
            _ => {}
        }
    }

    if let Some(error) = error {
        return Err(AppError::Config(format!(
            "el proveedor de identidad rechazó el inicio de sesión: {error}"
        )));
    }
    // Compared before the code is even read out: a callback carrying somebody
    // else's code must not be redeemed, and the check is worthless if it comes
    // after the value it guards has been used.
    if state.as_deref() != Some(expected_state) {
        return Err(AppError::Config(
            "el parámetro `state` no coincide: esta respuesta no pertenece a este intento".into(),
        ));
    }
    code.ok_or_else(|| AppError::Config("el navegador volvió sin código".into()))
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    id_token: String,
    #[serde(default)]
    refresh_token: String,
    expires_in: u64,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

/// Redeem the authorization code. This is the only place the verifier travels.
pub async fn exchange_code(
    http: &reqwest::Client,
    config: &AuthConfig,
    pkce: &Pkce,
    code: &str,
) -> Result<Session> {
    let redirect = format!("http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}");
    let form = [
        ("grant_type", "authorization_code"),
        ("client_id", config.client_id.as_str()),
        ("code", code),
        ("redirect_uri", redirect.as_str()),
        ("code_verifier", pkce.verifier.as_str()),
    ];
    let tokens: TokenResponse = post_token(http, config, &form).await?;
    Ok(Session {
        email: email_from(&tokens.id_token),
        id_token: tokens.id_token,
        refresh_token: tokens.refresh_token,
        expires_at: now_secs() + tokens.expires_in,
    })
}

/// Trade a refresh token for a new ID token.
///
/// The response carries no `refresh_token`, so the stored one is kept: Cognito
/// reissues only the short-lived halves, and overwriting with an empty string
/// would sign the user out an hour later for no reason.
pub async fn refresh(
    http: &reqwest::Client,
    config: &AuthConfig,
    session: &Session,
) -> Result<Session> {
    let form = [
        ("grant_type", "refresh_token"),
        ("client_id", config.client_id.as_str()),
        ("refresh_token", session.refresh_token.as_str()),
    ];
    let tokens: TokenResponse = post_token(http, config, &form).await?;
    Ok(Session {
        email: email_from(&tokens.id_token),
        id_token: tokens.id_token,
        refresh_token: session.refresh_token.clone(),
        expires_at: now_secs() + tokens.expires_in,
    })
}

async fn post_token(
    http: &reqwest::Client,
    config: &AuthConfig,
    form: &[(&str, &str)],
) -> Result<TokenResponse> {
    let url = format!("https://{}/oauth2/token", config.hosted_ui_domain);
    let response = http
        .post(&url)
        .form(form)
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|source| AppError::ControlUnreachable { url: url.clone(), source })?;

    if !response.status().is_success() {
        let status = response.status().as_u16();
        let body = response.text().await.unwrap_or_default();
        return Err(AppError::control_status(status, body));
    }
    response
        .json()
        .await
        .map_err(|source| AppError::ControlUnreachable { url, source })
}

/// The `email` claim, read without verifying the signature — which is correct
/// here and would not be anywhere else.
///
/// This token came from the token endpoint over TLS moments ago; it is not an
/// input from a caller. It is used to *label the screen*, never to authorize
/// anything, and the server verifies every token properly on every request.
fn email_from(id_token: &str) -> String {
    let Some(payload) = id_token.split('.').nth(1) else {
        return String::new();
    };
    let Some(bytes) = base64url_decode(payload) else {
        return String::new();
    };
    serde_json::from_slice::<serde_json::Value>(&bytes)
        .ok()
        .and_then(|v| v.get("email")?.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn base64url_decode(value: &str) -> Option<Vec<u8>> {
    const fn index(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'-' => Some(62),
            b'_' => Some(63),
            _ => None,
        }
    }
    let mut out = Vec::with_capacity(value.len() * 3 / 4);
    for chunk in value.as_bytes().chunks(4) {
        let mut n = 0u32;
        for (i, c) in chunk.iter().enumerate() {
            n |= u32::from(index(*c)?) << (18 - 6 * i);
        }
        for i in 0..chunk.len() - 1 {
            out.push((n >> (16 - 8 * i)) as u8);
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64url_matches_rfc_7636_appendix_b() {
        // The worked example from the RFC itself, which is what makes this a
        // check on the encoder rather than on my reading of it.
        let verifier: [u8; 32] = [
            116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212,
            37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121,
        ];
        assert_eq!(
            base64url(&verifier),
            "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        );
        assert_eq!(
            base64url(&Sha256::digest(
                "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk".as_bytes()
            )),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn a_verifier_is_long_enough_and_never_repeats() {
        let a = Pkce::new();
        let b = Pkce::new();
        assert!((43..=128).contains(&a.verifier.len()), "{}", a.verifier.len());
        assert_ne!(a.verifier, b.verifier);
        assert_ne!(a.state, b.state);
        // The challenge is the hash, not the verifier: sending the verifier
        // would make PKCE decorative.
        assert_ne!(a.challenge, a.verifier);
    }

    #[test]
    fn the_config_the_paid_plane_actually_returns_deserialises() {
        // Byte-for-byte what `GET /auth/config` answered on 2026-08-29, keys
        // and all. `AuthConfig` carries no `rename_all`, so the plane must emit
        // snake_case — and a DTO defaulting to camelCase would not fail here,
        // it would yield empty strings and an authorize URL reading
        // `https:///oauth2/authorize`, which is a 30-second mystery at the one
        // moment the user is trying to sign in.
        let body = r#"{"hosted_ui_domain":"yorch-brain-auth.auth.us-west-2.amazoncognito.com",
            "client_id":"7ve86qhj2a1tao98op2mogija0","region":"us-west-2",
            "issuer":"https://cognito-idp.us-west-2.amazonaws.com/us-west-2_69PaEg3l8",
            "flow":"authorization_code_pkce","scopes":["openid","email","profile"]}"#;

        let config: AuthConfig = serde_json::from_str(body).expect("shape changed");
        assert_eq!(
            config.hosted_ui_domain,
            "yorch-brain-auth.auth.us-west-2.amazoncognito.com"
        );
        assert_eq!(config.client_id, "7ve86qhj2a1tao98op2mogija0");

        // The three fields this app does not read are extra, not forbidden:
        // the plane may add to that body without breaking a shipped client.
        assert!(serde_json::from_str::<AuthConfig>(
            r#"{"hosted_ui_domain":"d","client_id":"c","algo_nuevo":1}"#
        )
        .is_ok());
    }

    #[test]
    fn a_camel_cased_config_is_refused_rather_than_read_as_empty() {
        // The failure mode worth naming: serde would happily default two
        // missing fields if they were `Option` or `#[serde(default)]`. They are
        // neither, so a plane that renamed them fails here instead of building
        // an authorize URL with no domain in it.
        assert!(serde_json::from_str::<AuthConfig>(
            r#"{"hostedUiDomain":"d","clientId":"c"}"#
        )
        .is_err());
    }

    #[test]
    fn the_authorize_url_carries_the_challenge_and_never_the_verifier() {
        let config = AuthConfig {
            hosted_ui_domain: "brain.auth.us-west-2.amazoncognito.com".into(),
            client_id: "cliente".into(),
        };
        let pkce = Pkce::new();
        let url = authorize_url(&config, &pkce);
        assert!(url.contains("code_challenge_method=S256"), "{url}");
        assert!(url.contains(&format!("code_challenge={}", pkce.challenge)), "{url}");
        assert!(!url.contains(&pkce.verifier), "el verificador no puede viajar al navegador");
        assert!(url.contains("redirect_uri=http%3A%2F%2Flocalhost%3A8789%2Fauth%2Fcallback"), "{url}");
    }

    #[test]
    fn a_callback_with_the_wrong_state_is_refused() {
        // Without this, a crafted request to the loopback listener hands this
        // app an authorization code it did not ask for.
        let err = parse_callback(
            "GET /auth/callback?code=abc&state=de-otro HTTP/1.1",
            "el-mio",
        )
        .unwrap_err();
        assert!(format!("{err}").contains("state"), "{err}");
    }

    #[test]
    fn a_matching_callback_yields_the_code() {
        let code = parse_callback("GET /auth/callback?code=abc&state=s HTTP/1.1", "s").unwrap();
        assert_eq!(code, "abc");
    }

    #[test]
    fn a_provider_error_is_reported_rather_than_read_as_a_missing_code() {
        let err = parse_callback(
            "GET /auth/callback?error=access_denied&state=s HTTP/1.1",
            "s",
        )
        .unwrap_err();
        assert!(format!("{err}").contains("access_denied"), "{err}");
    }

    #[test]
    fn a_request_for_another_path_is_not_a_callback() {
        let err = parse_callback("GET /favicon.ico HTTP/1.1", "s").unwrap_err();
        assert!(format!("{err}").contains("favicon"), "{err}");
    }

    #[test]
    fn a_session_is_stale_before_it_actually_expires() {
        // The margin exists so a request started just under the wire does not
        // arrive just over it.
        let almost = Session {
            id_token: "t".into(),
            refresh_token: "r".into(),
            expires_at: now_secs() + 30,
            email: String::new(),
        };
        assert!(!almost.fresh());
        let plenty = Session { expires_at: now_secs() + 3600, ..almost.clone() };
        assert!(plenty.fresh());
    }

    #[test]
    fn the_email_is_read_from_the_token_for_the_screen() {
        // Unverified on purpose: it came from the token endpoint over TLS and
        // labels a screen. Nothing is authorized with it.
        let payload = base64url(br#"{"email":"alguien@example.com"}"#);
        assert_eq!(email_from(&format!("h.{payload}.s")), "alguien@example.com");
        assert_eq!(email_from("no-es-un-jwt"), "");
    }

    /// The refresh token round-trips, and wherever it went it is not readable
    /// by anyone else.
    ///
    /// This used to assert `0600` on `session.json` unconditionally. It cannot
    /// any more, and the change is the point: on a machine with a keychain
    /// there *is* no file, and demanding one would be asserting the weaker of
    /// the two outcomes. The mode of the fallback is `keychain.rs`'s own test.
    #[test]
    fn a_stored_session_comes_back_and_leaves_nothing_readable_behind() {
        let dir = tempfile::tempdir().unwrap();
        let session = Session {
            id_token: "t".into(),
            refresh_token: "vale-treinta-dias".into(),
            expires_at: now_secs() + 3600,
            email: String::new(),
        };
        let backend = save_session(dir.path(), &session).unwrap();

        assert_eq!(
            load_session(dir.path()).unwrap().refresh_token,
            "vale-treinta-dias"
        );
        assert_eq!(load_session_with_backend(dir.path()).1, backend);

        match backend {
            crate::keychain::Backend::Keychain => {
                assert!(
                    !session_path(dir.path()).exists(),
                    "el token quedó también en disco"
                );
            }
            #[cfg(unix)]
            crate::keychain::Backend::File => {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(session_path(dir.path()))
                    .unwrap()
                    .permissions()
                    .mode();
                assert_eq!(mode & 0o077, 0, "modo {mode:o}");
            }
            #[cfg(not(unix))]
            crate::keychain::Backend::File => {}
        }

        clear_session(dir.path()).unwrap();
        assert!(load_session(dir.path()).is_none());
        // Signing out twice is not a failure.
        clear_session(dir.path()).unwrap();
    }

    /// An install that predates the keychain has its session in a file, and
    /// must not be signed out by the upgrade.
    #[test]
    fn a_session_left_in_a_file_by_an_older_version_is_still_read() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            session_path(dir.path()),
            r#"{"id_token":"t","refresh_token":"viejo","expires_at":0,"email":""}"#,
        )
        .unwrap();
        let (session, backend) = load_session_with_backend(dir.path());
        assert_eq!(session.unwrap().refresh_token, "viejo");
        assert_eq!(backend, crate::keychain::Backend::File);
    }
}
