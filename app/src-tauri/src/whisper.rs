//! Transcribing on the person's own machine, with the bundled `whisper-cli`.
//!
//! **Why the app carries whisper.cpp at all.** A bucket recording's transcript
//! is the one paid stage that dwarfs the rest — $0.024 a minute, about $233
//! for the first corpus — and the machine the person is sitting at may hold a
//! GPU that does the same work for nothing but electricity and hours. The
//! worker cannot use that GPU: it runs in a container on a host with none.
//! So, exactly as with `yt-dlp` next door, the app makes the one call only it
//! can make and sends the plane the few hundred kilobytes that result.
//!
//! **What travels, and what does not.** The audio comes down through a
//! presigned link the plane mints (`media_link`) — the customer's own bucket,
//! the customer's own role, and the app never holds a credential. The
//! transcript goes up through `POST /runs/{id}/transcript` to the plane the app
//! is already signed in to, lands in the organisation's inbox, and the
//! worker's `stage_transcript` does the one step only it can do. Nothing here
//! talks to S3 with anything but a URL.
//!
//! **Segments, not words.** `whisper-cli -oj` writes one segment every few
//! seconds with millisecond offsets; token-level timings are experimental on
//! the whisper.cpp side and a citation to the second is what the product
//! promises. `docagent.transcript.parse_whisper` reads exactly this shape.
//!
//! **The binary is not in git**, like yt-dlp: `binaries/fetch-whisper.sh`
//! builds or downloads it per target, pinned to a whisper.cpp release, and a
//! build without it fails at the bundler. The *models* are not bundled either
//! — the default is 1.6 GB — and are downloaded on first use into the app's
//! data directory, checked against a checksum pinned from the publisher's own
//! LFS metadata, so a truncated download is a refusal and not a model that
//! transcribes garbage.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::ipc::Channel;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use crate::error::{AppError, Result};

#[cfg(not(windows))]
const SIDECAR: &str = "whisper-cli";
#[cfg(windows)]
const SIDECAR: &str = "whisper-cli.exe";

/// Where the publisher keeps the ggml conversions of every Whisper model.
const MODEL_BASE: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main";

/// How long a download of the largest model may take: 3 GB at a slow home
/// link. It reports progress throughout, so a stall is visible long before
/// this fires.
const MODEL_DOWNLOAD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(3600 * 2);

/// How long one recording may take to transcribe. The longest in the first
/// corpus is 95 minutes; on a CPU at half real time that is three hours, and
/// the cap is well above it so a slow machine is slow rather than failed.
const TRANSCRIBE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(3600 * 8);

/// What the app's decoder reads without ffmpeg.
///
/// Read off the binary itself rather than assumed: `whisper-cli --help` at
/// v1.8.2 prints "supported audio formats: flac, mp3, ogg, wav", which is
/// miniaudio plus the project's own Vorbis decoder. The containers the bucket
/// walker can report and this list does not name are `mp4` and `webm`, and
/// both are common in a podcast archive — which is why the paid plane forks
/// this as `LOCAL_CONTAINERS` and quotes those on Amazon whatever the batch
/// asked for. A run parked for a transcript no machine on this side can make
/// would wait fourteen days to say so.
pub const DECODABLE: &[&str] = &["mp3", "wav", "flac", "ogg"];

/// One Whisper model as the publisher ships it.
///
/// The checksums are the `oid sha256` of each file's Git LFS pointer in the
/// `ggerganov/whisper.cpp` repository, read on 2026-09-17, and the sizes are
/// the pointers' too. A download is verified against both before it is
/// used, and a mismatch deletes the file rather than keeping a model that
/// would run and transcribe nonsense.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelInfo {
    pub name: &'static str,
    pub file: &'static str,
    pub bytes: u64,
    pub sha256: &'static str,
    /// What a person should know when choosing: the trade the size buys.
    pub note: &'static str,
}

pub const MODELS: &[ModelInfo] = &[
    ModelInfo {
        name: "large-v3-turbo",
        file: "ggml-large-v3-turbo.bin",
        bytes: 1_624_555_275,
        sha256: "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        note: "default: near large-v3 quality at several times its speed",
    },
    ModelInfo {
        name: "large-v3",
        file: "ggml-large-v3.bin",
        bytes: 3_095_033_483,
        sha256: "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        note: "best on degraded audio; needs a GPU to keep up with real time",
    },
    ModelInfo {
        name: "medium",
        file: "ggml-medium.bin",
        bytes: 1_533_763_059,
        sha256: "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
        note: "older; slower than turbo and worse in Spanish",
    },
    ModelInfo {
        name: "small",
        file: "ggml-small.bin",
        bytes: 487_601_967,
        sha256: "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        note: "fast on a CPU; clearly worse on tape",
    },
    ModelInfo {
        name: "base",
        file: "ggml-base.bin",
        bytes: 147_951_465,
        sha256: "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
        note: "for trying the flow, not for a corpus",
    },
];

pub const DEFAULT_MODEL: &str = "large-v3-turbo";

pub fn model(name: &str) -> Result<&'static ModelInfo> {
    MODELS
        .iter()
        .find(|m| m.name == name)
        .ok_or_else(|| whisper("whisper_model_unknown", format!("no known model called {name:?}")))
}

fn whisper(kind: &'static str, message: impl Into<String>) -> AppError {
    AppError::Whisper { kind, message: message.into() }
}

// ---------------------------------------------------------------------------
// The binary
// ---------------------------------------------------------------------------

/// Where the bundled `whisper-cli` is, or why there isn't one. Same three
/// places as `ytdlp::binary`, in the same order, for the same reasons.
pub fn binary() -> Result<PathBuf> {
    if let Some(from_env) = std::env::var_os("BRAIN_WHISPER") {
        let p = PathBuf::from(from_env);
        if p.is_file() {
            return Ok(p);
        }
        return Err(whisper(
            "whisper_missing",
            format!("BRAIN_WHISPER apunta a {}, que no existe", p.display()),
        ));
    }
    if let Some(beside) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|d| d.join(SIDECAR)))
        .filter(|p| p.is_file())
    {
        return Ok(beside);
    }
    if let Some(found) = std::env::var_os("COMPANY_BRAIN_REPO_ROOT")
        .map(PathBuf::from)
        .and_then(|root| std::fs::read_dir(root.join("app/src-tauri/binaries")).ok())
        .and_then(|entries| {
            entries.flatten().map(|e| e.path()).find(|p| {
                p.is_file()
                    && p.file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| n.starts_with("whisper-cli-"))
            })
        })
    {
        return Ok(found);
    }
    Err(whisper(
        "whisper_missing",
        "la aplicación no trae whisper-cli: ejecuta app/src-tauri/binaries/fetch-whisper.sh",
    ))
}

/// What the binary was built with, read off its own dynamic dependencies.
///
/// A hint rather than a fact: the fact arrives on every run in whisper.cpp's
/// own `system_info` line, which `transcribe` parses and reports. This is what
/// the Services screen shows before anything has run, and "cpu" here on a
/// machine with a GPU is the symptom of a build that was fetched without one.
pub fn backend_hint(bin: &Path) -> String {
    let dir = bin.parent().map(Path::to_path_buf).unwrap_or_default();
    let names: Vec<String> = std::fs::read_dir(&dir)
        .map(|entries| {
            entries
                .flatten()
                .filter_map(|e| e.file_name().to_str().map(str::to_lowercase))
                .collect()
        })
        .unwrap_or_default();
    if names.iter().any(|n| n.contains("ggml-cuda")) {
        return "cuda".into();
    }
    if names.iter().any(|n| n.contains("ggml-vulkan")) {
        return "vulkan".into();
    }
    if names.iter().any(|n| n.contains("ggml-metal")) {
        return "metal".into();
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(out) = std::process::Command::new("ldd").arg(bin).output() {
            let text = String::from_utf8_lossy(&out.stdout).to_lowercase();
            if text.contains("libcuda") || text.contains("libcublas") || text.contains("ggml-cuda") {
                return "cuda".into();
            }
            if text.contains("libvulkan") || text.contains("ggml-vulkan") {
                return "vulkan".into();
            }
        }
    }
    "cpu".into()
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelState {
    pub name: &'static str,
    pub file: &'static str,
    pub bytes: u64,
    pub note: &'static str,
    pub present: bool,
    pub path: String,
}

/// What whisper.cpp itself said it was running on, and how that was learned.
///
/// Kept apart from `WhisperStatus.backend`, which is a *guess* read off the
/// build's own dependencies. The difference is the whole point of this type: a
/// binary that links cuBLAS and finds no device at runtime — a driver too old,
/// a laptop that parks the discrete GPU, a container with no `/dev/nvidia*` —
/// hints `cuda` and runs on the processor, and the person would be told a
/// twelve-hour batch was a two-hour one.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceFact {
    /// `cuda`, `vulkan`, `metal`, `blas` or `cpu`.
    pub backend: String,
    /// What whisper.cpp called it: `CUDA0`, `Vulkan0`, `Metal`, or `CPU`.
    pub device: String,
    /// `probe` — the model was loaded and nothing was transcribed — or `run`,
    /// which is a whole recording and therefore the stronger evidence.
    pub how: String,
    pub model: String,
    pub at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WhisperStatus {
    /// Whether this build carries the transcriber at all. The one question
    /// that has to be answerable at a glance, so it is a boolean of its own
    /// rather than something to infer from `binary !== null`.
    pub installed: bool,
    /// The binary, or `null` with `error` saying why.
    pub binary: Option<String>,
    pub error: Option<String>,
    /// What the *build* looks capable of, from its own dependencies. A guess,
    /// and labelled as one until `device` exists.
    pub backend: String,
    /// What whisper.cpp said it actually used, or `null` before anything has
    /// asked it. This is the answer to "GPU or CPU?"; `backend` is not.
    pub device: Option<DeviceFact>,
    /// True when a model is present, which is what a probe needs: whisper.cpp
    /// names its device while *loading* a model, so with none downloaded the
    /// question cannot be settled at any price.
    pub can_probe: bool,
    pub models: Vec<ModelState>,
    pub default_model: &'static str,
    pub models_dir: String,
    /// `audio seconds / wall seconds` on the last run, or `null` before one.
    pub measured_speed: Option<f64>,
    pub measured_on: Option<String>,
    pub decodable: Vec<&'static str>,
}

pub fn models_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("models")
}

fn speed_file(data_dir: &Path) -> PathBuf {
    data_dir.join("whisper-speed.json")
}

fn device_file(data_dir: &Path) -> PathBuf {
    data_dir.join("whisper-device.json")
}

fn read_device(data_dir: &Path) -> Option<DeviceFact> {
    serde_json::from_str(&std::fs::read_to_string(device_file(data_dir)).ok()?).ok()
}

/// Remember what the binary said it used.
///
/// A finished transcription never loses to a probe: the probe loads a model
/// and stops, a run is the thing itself. Anything else would let pressing the
/// button after a GPU batch report the same machine as something weaker.
fn write_device(data_dir: &Path, fact: &DeviceFact) {
    if fact.how == "probe" {
        if let Some(existing) = read_device(data_dir) {
            if existing.how == "run" && existing.model == fact.model {
                return;
            }
        }
    }
    if let Ok(text) = serde_json::to_string_pretty(fact) {
        let _ = std::fs::write(device_file(data_dir), text);
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct MeasuredSpeed {
    factor: f64,
    model: String,
    backend: String,
    audio_seconds: f64,
    wall_seconds: f64,
    measured_at: String,
}

fn read_speed(data_dir: &Path) -> Option<MeasuredSpeed> {
    let text = std::fs::read_to_string(speed_file(data_dir)).ok()?;
    serde_json::from_str(&text).ok()
}

fn write_speed(data_dir: &Path, speed: &MeasuredSpeed) {
    if let Ok(text) = serde_json::to_string_pretty(speed) {
        let _ = std::fs::write(speed_file(data_dir), text);
    }
}

pub fn status(data_dir: &Path) -> WhisperStatus {
    let (binary, error) = match binary() {
        Ok(p) => (Some(p), None),
        Err(e) => (None, Some(e.to_string())),
    };
    let backend = binary.as_deref().map(backend_hint).unwrap_or_else(|| "none".into());
    let installed = binary.is_some();
    let dir = models_dir(data_dir);
    let models = MODELS
        .iter()
        .map(|m| {
            let path = dir.join(m.file);
            ModelState {
                name: m.name,
                file: m.file,
                bytes: m.bytes,
                note: m.note,
                present: path.is_file(),
                path: path.display().to_string(),
            }
        })
        .collect();
    let speed = read_speed(data_dir);
    let can_probe = installed && MODELS.iter().any(|m| dir.join(m.file).is_file());
    WhisperStatus {
        installed,
        binary: binary.map(|p| p.display().to_string()),
        error,
        backend,
        device: read_device(data_dir),
        can_probe,
        models,
        default_model: DEFAULT_MODEL,
        models_dir: dir.display().to_string(),
        measured_speed: speed.as_ref().map(|s| s.factor),
        measured_on: speed.map(|s| format!("{} · {} · {}", s.model, s.backend, s.measured_at)),
        decodable: DECODABLE.to_vec(),
    }
}

/// One line of progress the window renders. `phase` is `download`,
/// `verify`, `audio` or `transcribe`; `done`/`total` are bytes for the first
/// three and percent for the last.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Progress {
    pub phase: &'static str,
    pub done: u64,
    pub total: u64,
    pub message: String,
}

/// Fetch a model into the data directory, verified. Idempotent: a present
/// file that hashes correctly is returned without a byte transferred, and one
/// that does not is deleted and fetched again.
pub async fn download_model(
    data_dir: &Path,
    name: &str,
    on_event: &Channel<Progress>,
) -> Result<PathBuf> {
    let info = model(name)?;
    let dir = models_dir(data_dir);
    std::fs::create_dir_all(&dir).map_err(|e| AppError::io(dir.display(), e))?;
    let target = dir.join(info.file);
    if target.is_file() {
        let _ = on_event.send(Progress {
            phase: "verify",
            done: 0,
            total: info.bytes,
            message: info.file.into(),
        });
        if verified(&target, info, on_event).await? {
            return Ok(target);
        }
        let _ = std::fs::remove_file(&target);
    }
    let url = format!("{MODEL_BASE}/{}", info.file);
    let partial = dir.join(format!("{}.partial", info.file));
    let client = reqwest::Client::builder()
        .timeout(MODEL_DOWNLOAD_TIMEOUT)
        .build()
        .map_err(|e| whisper("whisper_download_failed", e.to_string()))?;
    let mut resp = client
        .get(&url)
        .send()
        .await
        .and_then(|r| r.error_for_status())
        .map_err(|e| whisper("whisper_download_failed", format!("{url}: {e}")))?;
    let mut file = std::fs::File::create(&partial).map_err(|e| AppError::io(partial.display(), e))?;
    let mut hasher = Sha256::new();
    let mut done: u64 = 0;
    let mut last_report = Instant::now();
    // `chunk()` rather than `bytes_stream()`: the stream wants `StreamExt`,
    // which means a direct dependency on `futures-util` for one `next()`.
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| whisper("whisper_download_failed", e.to_string()))?
    {
        file.write_all(&chunk)
            .map_err(|e| AppError::io(partial.display(), e))?;
        hasher.update(&chunk);
        done += chunk.len() as u64;
        if last_report.elapsed().as_millis() >= 250 {
            let _ = on_event.send(Progress {
                phase: "download",
                done,
                total: info.bytes,
                message: info.file.into(),
            });
            last_report = Instant::now();
        }
    }
    drop(file);
    let digest = hex(&hasher.finalize());
    if done != info.bytes || digest != info.sha256 {
        let _ = std::fs::remove_file(&partial);
        return Err(whisper(
            "whisper_model_corrupt",
            format!(
                "{} llegó con {done} bytes y sha256 {digest}; se esperaban {} y {}",
                info.file, info.bytes, info.sha256
            ),
        ));
    }
    std::fs::rename(&partial, &target).map_err(|e| AppError::io(target.display(), e))?;
    let _ = on_event.send(Progress {
        phase: "download",
        done,
        total: info.bytes,
        message: info.file.into(),
    });
    Ok(target)
}

/// Hash a model already on disk, on a thread of its own.
///
/// 1.6 GB of sha256 is a second or two of pure arithmetic, which is exactly
/// what must not happen on a runtime thread — the window would stop answering
/// for the length of it, on the screen whose whole job is to report status.
async fn verified(path: &Path, info: &'static ModelInfo, on_event: &Channel<Progress>) -> Result<bool> {
    let path = path.to_path_buf();
    let channel = on_event.clone();
    tokio::task::spawn_blocking(move || verify(&path, info, &channel))
        .await
        .map_err(|e| whisper("whisper_failed", e.to_string()))?
}

fn verify(path: &Path, info: &ModelInfo, on_event: &Channel<Progress>) -> Result<bool> {
    let meta = std::fs::metadata(path).map_err(|e| AppError::io(path.display(), e))?;
    if meta.len() != info.bytes {
        return Ok(false);
    }
    let mut file = std::fs::File::open(path).map_err(|e| AppError::io(path.display(), e))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    let mut done: u64 = 0;
    let mut last = Instant::now();
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| AppError::io(path.display(), e))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        done += n as u64;
        if last.elapsed().as_millis() >= 250 {
            let _ = on_event.send(Progress {
                phase: "verify",
                done,
                total: info.bytes,
                message: info.file.into(),
            });
            last = Instant::now();
        }
    }
    Ok(hex(&hasher.finalize()) == info.sha256)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------------------------
// Asking the binary what it will run on
// ---------------------------------------------------------------------------

/// How long the probe waits for whisper.cpp to name its device.
///
/// **Measured, after a minute turned out to be too short.** On this machine —
/// RTX 4060, CUDA 12.4, the 1.6 GB default model — the line arrives **38.7 s**
/// after the process starts: a CUDA context and 1.6 GB into VRAM, most of it
/// before whisper.cpp says anything at all. The first version of this constant
/// was 60 s on the reasoning that a model load is "about a second from the
/// page cache", which was true of the CPU build and wrong of the one people
/// will actually ship. Three minutes leaves room for a cold disk and a larger
/// model and still refuses rather than hanging a settings screen for ever.
const PROBE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(180);

/// A second and a half of silence, as a 16 kHz mono 16-bit WAV.
///
/// whisper.cpp refuses audio under a second (`seek_end < seek_start + 100`),
/// and it has to be *some* file because the device is named while a model is
/// being loaded for a real call. Written by hand rather than shipped as a
/// fixture: a header is 44 bytes of documented layout and a binary asset is a
/// thing to keep in sync with nothing.
fn silence_wav() -> Vec<u8> {
    const RATE: u32 = 16_000;
    const SAMPLES: u32 = RATE * 3 / 2;
    let data_len = SAMPLES * 2;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes()); // PCM header length
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&1u16.to_le_bytes()); // mono
    out.extend_from_slice(&RATE.to_le_bytes());
    out.extend_from_slice(&(RATE * 2).to_le_bytes()); // byte rate
    out.extend_from_slice(&2u16.to_le_bytes()); // block align
    out.extend_from_slice(&16u16.to_le_bytes()); // bits per sample
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    out.resize(44 + data_len as usize, 0);
    out
}

/// Ask whisper.cpp which device it will use, and stop as soon as it says.
///
/// **Why this exists rather than reading the build.** `backend_hint` answers
/// "was this compiled with CUDA", and the question a person is asking is "will
/// my GPU be used" — which is a different question on a laptop that parks its
/// discrete card, on a driver too old for the toolkit the binary was built
/// with, and in any container without `/dev/nvidia*`. Each of those hints
/// `cuda` and runs on the processor, at roughly a tenth of the speed, and the
/// person would be planning a batch against a number that is wrong by an order
/// of magnitude.
///
/// It costs a model load and no inference: the device is named *while the
/// model loads*, so the child is killed the moment the line arrives. The
/// smallest downloaded model is used, because the answer does not depend on
/// which one and a 148 MB load beats a 1.6 GB one.
pub async fn probe_device(data_dir: &Path) -> Result<DeviceFact> {
    let bin = binary()?;
    let dir = models_dir(data_dir);
    let model = MODELS
        .iter()
        .filter(|m| dir.join(m.file).is_file())
        .min_by_key(|m| m.bytes)
        .ok_or_else(|| {
            whisper(
                "whisper_model_missing",
                "whisper.cpp nombra su dispositivo al cargar un modelo, y no hay ninguno descargado",
            )
        })?;
    let cache = data_dir.join("cache").join("whisper");
    std::fs::create_dir_all(&cache).map_err(|e| AppError::io(cache.display(), e))?;
    let silence = cache.join("probe.wav");
    std::fs::write(&silence, silence_wav()).map_err(|e| AppError::io(silence.display(), e))?;

    let started = Instant::now();
    let mut child = Command::new(&bin)
        .arg("-m")
        .arg(dir.join(model.file))
        .arg("-f")
        .arg(&silence)
        // `-ac 32` shrinks the audio context so that, if the line never comes
        // and the process is left to finish, it finishes in moments rather
        // than running a full encoder pass for nothing.
        .args(["-ac", "32", "-nt", "-bo", "1", "-bs", "1"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| whisper("whisper_failed", format!("no se pudo lanzar whisper-cli: {e}")))?;

    let mut found: Option<(String, String)> = None;
    if let Some(stderr) = child.stderr.take() {
        let mut lines = BufReader::new(stderr).lines();
        let deadline = tokio::time::sleep(PROBE_TIMEOUT);
        tokio::pin!(deadline);
        loop {
            tokio::select! {
                line = lines.next_line() => match line {
                    Ok(Some(line)) => {
                        if let Some(backend) = backend_named(&line) {
                            let device = line
                                .split("using ")
                                .nth(1)
                                .and_then(|rest| rest.split(" backend").next())
                                .unwrap_or("")
                                .trim()
                                .to_string();
                            found = Some((backend, device));
                            break;
                        }
                        // The other half of the answer, and the one this
                        // machine gives: the GPU path was tried and there was
                        // nothing there, so what follows is the processor.
                        if line.contains("no GPU found") {
                            found = Some(("cpu".into(), "CPU".into()));
                            break;
                        }
                    }
                    _ => break,
                },
                _ = &mut deadline => break,
            }
        }
    }
    let _ = child.kill().await;
    let _ = std::fs::remove_file(&silence);

    let (backend, device) = found.ok_or_else(|| {
        whisper(
            "whisper_failed",
            "whisper-cli no dijo qué dispositivo usa; revisa la instalación",
        )
    })?;
    let fact = DeviceFact {
        backend,
        device,
        how: "probe".into(),
        model: model.name.into(),
        at: now_iso(),
    };
    write_device(data_dir, &fact);
    let _ = started;
    Ok(fact)
}

// ---------------------------------------------------------------------------
// Transcribing
// ---------------------------------------------------------------------------

/// What one transcription produced, for the upload and for the record.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Transcribed {
    /// The `whisper-cli -oj` document, on disk, for `transcript_upload`.
    pub path: String,
    pub engine: String,
    pub model: String,
    pub language: String,
    /// `cuda`, `vulkan`, `metal` or `cpu`, as whisper.cpp's own system_info
    /// line reported it for this run — the fact, where `backend_hint` is a guess.
    pub backend: String,
    pub audio_seconds: f64,
    pub wall_seconds: f64,
    pub segments: usize,
}

/// Download the audio through the link the plane minted and run the model on it.
///
/// One at a time by construction — the caller serialises — because a
/// transcription saturates whatever it runs on and two would each take twice
/// as long. The audio lands in the app's cache and is deleted whether or not
/// the run succeeded; the transcript stays until it has been uploaded.
#[allow(clippy::too_many_arguments)]
pub async fn transcribe(
    data_dir: &Path,
    run_id: &str,
    audio_url: &str,
    container: &str,
    model_name: &str,
    language: &str,
    audio_seconds: f64,
    threads: usize,
    on_event: &Channel<Progress>,
) -> Result<Transcribed> {
    if !DECODABLE.contains(&container) {
        return Err(whisper(
            "whisper_container_unsupported",
            format!(
                "whisper-cli decodifica {} y no {container}",
                DECODABLE.join(", ")
            ),
        ));
    }
    let bin = binary()?;
    let info = model(model_name)?;
    let model_path = models_dir(data_dir).join(info.file);
    if !model_path.is_file() {
        return Err(whisper(
            "whisper_model_missing",
            format!("el modelo {} no está descargado", info.name),
        ));
    }
    let cache = data_dir.join("cache").join("whisper");
    std::fs::create_dir_all(&cache).map_err(|e| AppError::io(cache.display(), e))?;
    let audio = cache.join(format!("{run_id}.{container}"));
    let out_base = cache.join(run_id);
    let out_json = cache.join(format!("{run_id}.json"));

    let result = transcribe_into(
        &bin,
        &model_path,
        audio_url,
        &audio,
        &out_base,
        &out_json,
        language,
        threads,
        on_event,
    )
    .await;
    // Whether or not it worked: this is a transient on the person's own disk
    // and can be a couple of hundred megabytes, exactly as the video path
    // deletes its own download.
    let _ = std::fs::remove_file(&audio);
    let (backend, wall, segments) = result?;

    let done = Transcribed {
        path: out_json.display().to_string(),
        engine: "whisper.cpp".into(),
        model: info.name.into(),
        language: language.into(),
        backend,
        audio_seconds,
        wall_seconds: wall,
        segments,
    };
    write_device(
        data_dir,
        &DeviceFact {
            backend: done.backend.clone(),
            // A run reports the backend rather than the device's own name —
            // that is what `run_cli` reads — so the two agree where it matters
            // and the panel says which kind of evidence it is showing.
            device: done.backend.to_uppercase(),
            how: "run".into(),
            model: done.model.clone(),
            at: now_iso(),
        },
    );
    if done.audio_seconds > 0.0 && done.wall_seconds > 0.0 {
        write_speed(
            data_dir,
            &MeasuredSpeed {
                factor: done.audio_seconds / done.wall_seconds,
                model: done.model.clone(),
                backend: done.backend.clone(),
                audio_seconds: done.audio_seconds,
                wall_seconds: done.wall_seconds,
                measured_at: now_iso(),
            },
        );
    }
    Ok(done)
}

#[allow(clippy::too_many_arguments)]
async fn transcribe_into(
    bin: &Path,
    model_path: &Path,
    audio_url: &str,
    audio: &Path,
    out_base: &Path,
    out_json: &Path,
    language: &str,
    threads: usize,
    on_event: &Channel<Progress>,
) -> Result<(String, f64, usize)> {
    download_audio(audio_url, audio, on_event).await?;
    let started = Instant::now();
    let backend = run_cli(bin, model_path, audio, out_base, language, threads, on_event).await?;
    let wall = started.elapsed().as_secs_f64();
    let text = std::fs::read_to_string(out_json).map_err(|e| AppError::io(out_json.display(), e))?;
    let segments = count_segments(&text)?;
    Ok((backend, wall, segments))
}

async fn download_audio(url: &str, into: &Path, on_event: &Channel<Progress>) -> Result<()> {
    let client = reqwest::Client::builder()
        .timeout(MODEL_DOWNLOAD_TIMEOUT)
        .build()
        .map_err(|e| whisper("whisper_audio_failed", e.to_string()))?;
    let mut resp = client
        .get(url)
        .send()
        .await
        .and_then(|r| r.error_for_status())
        .map_err(|e| whisper("whisper_audio_failed", format!("no se pudo descargar el audio: {e}")))?;
    let total = resp.content_length().unwrap_or(0);
    let mut file = std::fs::File::create(into).map_err(|e| AppError::io(into.display(), e))?;
    let mut done: u64 = 0;
    let mut last = Instant::now();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| whisper("whisper_audio_failed", e.to_string()))?
    {
        file.write_all(&chunk)
            .map_err(|e| AppError::io(into.display(), e))?;
        done += chunk.len() as u64;
        if last.elapsed().as_millis() >= 250 {
            let _ = on_event.send(Progress {
                phase: "audio",
                done,
                total,
                message: String::new(),
            });
            last = Instant::now();
        }
    }
    Ok(())
}

/// Run `whisper-cli` and read its progress and its device line.
///
/// `-pp` makes whisper.cpp print `progress = NN%` lines on stderr, which is
/// why the stream is read rather than the process merely waited on — the same
/// decision as yt-dlp's `--progress-template` next door, and for the same
/// reason: a person watching an hour-long transcription needs to see it move.
/// The output JSON is written by whisper.cpp itself to `<out_base>.json`,
/// never assembled here.
///
/// **`-mc 0` is the flag that keeps the transcript honest, and it was added
/// after the damage.** whisper.cpp feeds the text it has just produced into
/// the next window as a prompt (`--max-context`, default -1: carry
/// everything). Once it repeats a phrase it reads that repetition back to
/// itself and latches on, emitting the same line until the audio ends.
/// Nothing fails: the JSON is valid, the size is ordinary, the process
/// succeeds. Only a measurement sees it.
///
/// Measured on this machine with `large-v3-turbo` over a real 63-minute
/// sermon, the worst of thirteen transcribed with the defaults:
///
/// | | segments | longest identical run | repeated | distinct words |
/// |---|---|---|---|---|
/// | default | 1013 | **167** | 7.3% | 1477 |
/// | `-mc 0` | 1061 | **3** | 1.9% | **1533** |
///
/// Read the last column before the others. The default produced *657 more
/// words and 56 fewer distinct ones*: in the final 2.9 minutes it emitted one
/// phrase 167 times where `-mc 0` finds 44 segments of 43 different lines —
/// the closing altar call. The loop does not pile junk on top of speech, it
/// **replaces** it. Two of the thirteen were damaged this way and both had
/// already been indexed before anybody measured.
///
/// `--vad` was measured too (longest run 1, coverage 98.3%) and is
/// deliberately *not* used: it merges segments — 1013 to 748 on the same
/// recording — and a segment's start is what a citation points at, so it
/// would coarsen every locator in the corpus to buy what `-mc 0` already
/// bought.
async fn run_cli(
    bin: &Path,
    model: &Path,
    audio: &Path,
    out_base: &Path,
    language: &str,
    threads: usize,
    on_event: &Channel<Progress>,
) -> Result<String> {
    let mut child = Command::new(bin)
        .args(cli_args(model, audio, out_base, language, threads))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| whisper("whisper_failed", format!("no se pudo lanzar whisper-cli: {e}")))?;

    let mut backend = String::from("cpu");
    let mut tail: Vec<String> = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(pct) = parse_percent(&line) {
                let _ = on_event.send(Progress {
                    phase: "transcribe",
                    done: pct,
                    total: 100,
                    message: String::new(),
                });
            }
            if let Some(named) = backend_named(&line) {
                backend = named;
            }
            tail.push(line);
            if tail.len() > 40 {
                tail.remove(0);
            }
        }
    }

    let status = match tokio::time::timeout(TRANSCRIBE_TIMEOUT, child.wait()).await {
        Ok(status) => status.map_err(|e| whisper("whisper_failed", e.to_string()))?,
        Err(_) => {
            let _ = child.kill().await;
            return Err(whisper(
                "whisper_failed",
                "whisper-cli superó el tiempo máximo",
            ));
        }
    };
    if !status.success() {
        return Err(whisper(
            "whisper_failed",
            format!("whisper-cli terminó con {status}: {}", tail.join(" | ")),
        ));
    }
    Ok(backend)
}

/// Everything `whisper-cli` is told, as a list.
///
/// Pure so the one argument that decides whether the transcript is honest can
/// be asserted without running a transcription — the same reason this codebase
/// keeps its graph arithmetic out of its components.
fn cli_args(
    model: &Path,
    audio: &Path,
    out_base: &Path,
    language: &str,
    threads: usize,
) -> Vec<std::ffi::OsString> {
    let threads = threads.to_string();
    let flat: [&std::ffi::OsStr; 13] = [
        "-m".as_ref(),
        model.as_os_str(),
        "-f".as_ref(),
        audio.as_os_str(),
        "-l".as_ref(),
        language.as_ref(),
        "-oj".as_ref(),
        // A *prefix*, never a filename: whisper.cpp appends the extension
        // itself, so `-of x.json` writes `x.json.json` and the reader reports
        // a transcription that happened, and was paid for in GPU time, as
        // missing.
        "-of".as_ref(),
        out_base.as_os_str(),
        "-t".as_ref(),
        threads.as_ref(),
        // Do not let the transcript become its own prompt. See `run_cli`.
        "-mc".as_ref(),
        "0".as_ref(),
    ];
    let mut args: Vec<std::ffi::OsString> = flat.iter().map(|a| (*a).to_owned()).collect();
    args.push(std::ffi::OsString::from("-pp"));
    args
}

/// One `progress = NN%` line, or `None` for anything else on stderr. Pure so
/// the format can be asserted without a transcription.
pub fn parse_percent(line: &str) -> Option<u64> {
    let rest = line.split("progress =").nth(1)?;
    rest.trim().trim_end_matches('%').trim().parse::<u64>().ok()
}

/// What whisper.cpp says it is running on, read off its own startup lines.
///
/// This is the fact where `backend_hint` is a guess, and the line it reads is
/// the one whisper.cpp prints for exactly this question — verified against
/// v1.8.2's output on this machine, where a CPU build says
/// `whisper_backend_init_gpu: no GPU found` and `src/whisper.cpp:1316` prints
/// `whisper_backend_init_gpu: using <device> backend` when it finds one.
///
/// `system_info` is deliberately **not** read. It lists what the build was
/// compiled with — `WHISPER : COREML = 0 | OPENVINO = 0 | CPU : AVX = 1 | …` —
/// and a build that *has* CUDA and found no device prints the same line as one
/// that is using it. Reporting that as a GPU run would make a CPU-speed
/// measurement look like a GPU one, which is the one number this whole feature
/// is judged on.
pub fn backend_named(line: &str) -> Option<String> {
    let rest = line.split("using ").nth(1)?;
    let device = rest.split(" backend").next()?.trim().to_lowercase();
    if device.is_empty() || device.contains(' ') {
        return None;
    }
    // `CUDA0`, `Vulkan0`, `Metal`, `BLAS`: the ordinal is the device index and
    // says nothing a person wants on a settings screen.
    let name = device.trim_end_matches(|c: char| c.is_ascii_digit());
    Some(name.to_string())
}

fn count_segments(text: &str) -> Result<usize> {
    let value: serde_json::Value = serde_json::from_str(text)
        .map_err(|e| whisper("whisper_failed", format!("la salida de whisper-cli no es JSON: {e}")))?;
    value
        .get("transcription")
        .and_then(|t| t.as_array())
        .map(|a| a.len())
        .ok_or_else(|| whisper("whisper_failed", "la salida de whisper-cli no trae `transcription`"))
}

fn now_iso() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Seconds are enough for "when was this measured"; a dependency for the
    // calendar arithmetic is not worth one line on a settings screen.
    format!("{secs}")
}

/// How many threads to hand whisper-cli: the machine's cores, capped. On a
/// GPU build the encoder runs on the device and threads matter little; on a
/// CPU they are the whole speed.
pub fn threads() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).clamp(1, 16)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_model_is_one_the_table_knows() {
        assert!(model(DEFAULT_MODEL).is_ok());
        assert!(model("gigantic").is_err());
    }

    #[test]
    fn every_pinned_checksum_is_a_sha256_and_every_size_is_plausible() {
        for m in MODELS {
            assert_eq!(m.sha256.len(), 64, "{}", m.name);
            assert!(m.sha256.chars().all(|c| c.is_ascii_hexdigit()), "{}", m.name);
            assert!(m.bytes > 100_000_000, "{}", m.name);
        }
    }

    #[test]
    fn a_missing_binary_is_a_kind_with_the_fix_in_it() {
        std::env::set_var("BRAIN_WHISPER", "/definitely/not/here/whisper-cli");
        let err = binary().unwrap_err();
        assert!(err.to_string().contains("BRAIN_WHISPER"));
        std::env::remove_var("BRAIN_WHISPER");
    }

    #[test]
    fn segments_are_counted_off_whisper_cpps_own_shape() {
        let doc = r#"{"transcription":[{"offsets":{"from":0,"to":1000},"text":" a"},{"offsets":{"from":1000,"to":2000},"text":" b"}]}"#;
        assert_eq!(count_segments(doc).unwrap(), 2);
        assert!(count_segments(r#"{"results":{"items":[]}}"#).is_err());
        assert!(count_segments("not json").is_err());
    }

    /// Verified against whisper.cpp v1.8.2's own `-pp` output, which is what
    /// makes this worth a test: the line is `whisper_print_progress_callback:
    /// progress =  12%`, with the percent padded and a space before the `%`.
    #[test]
    fn progress_is_read_off_whisper_cpps_own_line() {
        assert_eq!(
            parse_percent("whisper_print_progress_callback: progress =  12%"),
            Some(12)
        );
        assert_eq!(parse_percent("progress = 100%"), Some(100));
        assert_eq!(parse_percent("whisper_full_with_state: auto-detected"), None);
        assert_eq!(parse_percent("progress = nope%"), None);
    }

    /// Read off whisper.cpp v1.8.2's real output, not a plausible imitation of
    /// it. The CPU lines below are verbatim from a run on this machine; the
    /// GPU one is the format `src/whisper.cpp:1316` prints.
    #[test]
    fn the_backend_is_the_device_whisper_cpp_says_it_found() {
        assert_eq!(
            backend_named("whisper_backend_init_gpu: using CUDA0 backend"),
            Some("cuda".into())
        );
        assert_eq!(
            backend_named("whisper_backend_init_gpu: using Vulkan0 backend"),
            Some("vulkan".into())
        );
        assert_eq!(
            backend_named("whisper_backend_init: using BLAS backend"),
            Some("blas".into())
        );
        // A CPU build, verbatim: it must leave the backend where it was.
        assert_eq!(backend_named("whisper_backend_init_gpu: no GPU found"), None);
        // And `system_info` must not be read at all — a build compiled with
        // CUDA prints the same line whether or not it found a device.
        assert_eq!(
            backend_named(
                "system_info: n_threads = 16 / 16 | WHISPER : COREML = 0 | OPENVINO = 0 | \
                 CPU : SSE3 = 1 | AVX = 1 | OPENMP = 1 |"
            ),
            None
        );
        assert_eq!(backend_named("whisper_model_load: n_vocab = 51866"), None);
    }

    /// `LOCAL_CONTAINERS` on the paid plane is this list, and the two are
    /// compared by `buckets.parity.spec.ts`. Verified against the binary's own
    /// `--help` at v1.8.2: flac, mp3, ogg, wav.
    /// The transcript must never become its own prompt.
    ///
    /// `--max-context` defaults to -1, which carries every word already
    /// transcribed into the next window: once whisper.cpp repeats a phrase it
    /// reads that back to itself and emits it until the audio ends. Measured
    /// here on a real 63-minute sermon with `large-v3-turbo`: a run of **167**
    /// identical segments by default against **3** with this flag — and, the
    /// part that matters, 1477 distinct words against 1533, because the loop
    /// *replaced* the closing three minutes rather than adding to them.
    ///
    /// Nothing else catches this. The JSON is valid either way, the file size
    /// is ordinary, the process exits 0, and two damaged recordings were
    /// indexed and answerable before anybody measured.
    #[test]
    fn the_transcript_is_never_fed_back_as_its_own_prompt() {
        let args: Vec<String> = cli_args(
            Path::new("/m/ggml-large-v3-turbo.bin"),
            Path::new("/tmp/a.mp3"),
            Path::new("/tmp/out/run-1"),
            "es",
            16,
        )
        .iter()
        .map(|a| a.to_string_lossy().into_owned())
        .collect();

        let at = args.iter().position(|a| a == "-mc").expect("-mc is passed");
        assert_eq!(args[at + 1], "0", "-mc must be 0, not merely present");

        // And the two that are easy to get wrong beside it.
        let of = args.iter().position(|a| a == "-of").unwrap();
        assert_eq!(args[of + 1], "/tmp/out/run-1", "`-of` is a prefix, not a filename");
        assert!(!args[of + 1].ends_with(".json"), "whisper.cpp appends the extension");
        let l = args.iter().position(|a| a == "-l").unwrap();
        assert_eq!(args[l + 1], "es", "the language is fixed, never auto-detected");
        assert!(!args.iter().any(|a| a == "-ng"), "the GPU is never switched off");
    }

    /// The probe's own input, checked as bytes: whisper.cpp reads this with
    /// miniaudio and refuses anything under a second, so both the header and
    /// the length are load-bearing.
    #[test]
    fn the_silence_the_probe_feeds_is_a_wav_longer_than_a_second() {
        let wav = silence_wav();
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[36..40], b"data");
        let declared = u32::from_le_bytes(wav[40..44].try_into().unwrap()) as usize;
        assert_eq!(wav.len(), 44 + declared);
        // 16 kHz, mono, 16-bit: two bytes a sample.
        let seconds = declared as f64 / 2.0 / 16_000.0;
        assert!(seconds > 1.0, "{seconds} s would be refused as too short");
        // Silence, so nothing is transcribed and nothing is inferred from it.
        assert!(wav[44..].iter().all(|b| *b == 0));
    }

    #[test]
    fn the_decodable_list_is_what_whisper_cli_says_it_reads() {
        let mut sorted = DECODABLE.to_vec();
        sorted.sort_unstable();
        assert_eq!(sorted, ["flac", "mp3", "ogg", "wav"]);
        // The two containers the bucket walker reports that it cannot read.
        assert!(!DECODABLE.contains(&"mp4"));
        assert!(!DECODABLE.contains(&"webm"));
    }
}

#[cfg(test)]
mod live {
    //! One test that talks to the bundled binary, `#[ignore]`d like the
    //! yt-dlp one next door and for the same reason: it is the only thing that
    //! can say the sidecar works at all, and it needs a binary and a model
    //! that are not in git.
    //!
    //!   COMPANY_BRAIN_REPO_ROOT=/home/kheiron/yorch cargo test -- --ignored
    use super::*;

    #[tokio::test]
    #[ignore = "needs the bundled whisper-cli and a downloaded model"]
    async fn the_probe_gets_a_device_out_of_the_real_binary() {
        let data_dir = dirs_data();
        let fact = probe_device(&data_dir).await.expect("the probe answered");
        // Whatever this machine is, it must be one of the names the screen has
        // a label for — a raw token there is the recorded dynamic-key hazard.
        assert!(
            ["cuda", "vulkan", "metal", "blas", "cpu"].contains(&fact.backend.as_str()),
            "unknown backend {:?}",
            fact.backend
        );
        assert_eq!(fact.how, "probe");
        assert!(!fact.device.is_empty());
        // And it is written down, so the next launch shows it without asking.
        assert!(device_file(&data_dir).is_file());
    }

    fn dirs_data() -> PathBuf {
        std::env::var_os("BRAIN_DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("HOME").expect("HOME"))
                    .join(".local/share/io.sek.companybrain")
            })
    }
}
