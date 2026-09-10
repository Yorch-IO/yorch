//! Asking YouTube, from the machine the person is sitting at.
//!
//! **Why this exists at all.** Measured 2026-09-05 against production: a
//! `yt-dlp extract_info` succeeds from a residential address in 2.6 s and is
//! refused from the EC2 egress address with "Sign in to confirm you're not a
//! bot", while that same host fetches every signed caption URL at 200 and
//! serves the watch page at 200. It is the player API that is bot-checked, not
//! the network. So the fix is to make that one call somewhere else — and in
//! paid mode the only machine the product can be sure of is this one.
//!
//! Two calls move, and only two. `resolve` is `extract_info`, and its result
//! travels to the plane as `VideoRequest.resolved`, small by construction: the
//! raw info dict is 1,656,277 bytes on `yq6uVBsVkeQ` and [`VideoInfo`] is a few
//! kilobytes. `download_audio` has to run **beside** it, for a different
//! measured reason — a `googlevideo` media URL carries the address that
//! resolved it and answers 403 anywhere else — and is only ever reached for a
//! video with no captions at all.
//!
//! Everything else stays where it was. A caption URL carries `ip=0.0.0.0` and
//! was served to the blocked host at 200, so the caption *download* stays on
//! the worker that owns the workspace, and 582,176 bytes of VTT never cross a
//! payload.
//!
//! **This module is a fork, and the two halves that are forked say so.**
//! [`tracks_of`] and [`choose_track`] mirror `_tracks` and `_choose_track` in
//! `worker/brainworker/activities/video.py`, and [`error_kind`] mirrors
//! `_download_error_kind` beside them. They are forked rather than shared for
//! the reason every fork in this product is: the other side is Python and this
//! one cannot call it. Each carries a test naming the Python behaviour it has
//! to reproduce.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use crate::control::CaptionTrack;
use crate::error::{AppError, Result};

/// The name Tauri gives the sidecar once it is next to the executable.
///
/// `bundle.externalBin` names `binaries/yt-dlp`, and the bundler copies
/// `yt-dlp-<target triple>` alongside the app binary with the triple stripped.
/// So the *installed* lookup is a sibling of `current_exe`, which is what
/// `tauri-plugin-shell`'s `sidecar()` resolves to as well — reached here
/// without the plugin for the same reason `pick_source` needs no capability:
/// the webview never invokes it, Rust does.
#[cfg(not(windows))]
const SIDECAR: &str = "yt-dlp";
#[cfg(windows)]
const SIDECAR: &str = "yt-dlp.exe";

/// How long `extract_info` may take. Measured at 2.6 s from a residential
/// address; a minute is forty times that and still fails rather than hanging a
/// window on a network that is simply gone.
const RESOLVE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(60);

/// What `--progress-template` is told to print, so progress can be read without
/// parsing yt-dlp's human line.
///
/// Verified against the real binary (2026.08.19): these lines arrive on
/// **stdout**, one per update, interleaved with yt-dlp's own `[youtube] …`
/// chatter — which is why the prefix is distinctive and every other line is
/// ignored rather than parsed.
const PROGRESS_PREFIX: &str = "brainprogress:";
const PROGRESS_TEMPLATE: &str =
    "brainprogress:%(progress.downloaded_bytes)s/%(progress.total_bytes_estimate)s/%(progress.total_bytes)s";

/// What one `extract_info` learned, narrowed to what may cross.
///
/// Field for field `brainworker.pipeline.VideoInfo`, and it serializes in
/// **snake_case** — unlike most types in `control.rs`, which rename on the way
/// out for the webview. This one only ever travels Rust → Python, nested inside
/// `VideoRequest.resolved`, so Python's spelling is the only one it needs.
/// (`CaptionTrack` renames on serialize and is unaffected: every one of its
/// fields is a single word.)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VideoInfo {
    pub video_id: String,
    pub title: String,
    pub channel: String,
    pub duration_s: i64,
    pub upload_date: String,
    pub format_id: String,
    pub tracks: Vec<CaptionTrack>,
    /// The track that will be read, or `None` when Amazon Transcribe must run.
    pub chosen: Option<CaptionTrack>,
    /// The signed URL for `chosen`. Empty when there is no chosen track.
    pub caption_url: String,
}

/// How far a download has got, for a window that would otherwise look hung.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct Progress {
    pub bytes: u64,
    /// yt-dlp's own figure, which is an estimate for a format it has not
    /// finished reading. Zero means it did not offer one — rendered as "no
    /// percentage" rather than as 0%, the same rule `/project-summary` applies
    /// to a leg it could not ask.
    pub total: u64,
}

fn youtube(kind: &'static str, message: impl Into<String>) -> AppError {
    AppError::YouTube {
        kind,
        message: message.into(),
    }
}

/// Where the bundled binary is, or why there isn't one.
///
/// Three places, in the order they are true. The environment variable is first
/// so a developer can point at a newer yt-dlp without rebuilding — this is the
/// one dependency in the app that goes stale on somebody else's schedule.
pub fn binary() -> Result<PathBuf> {
    if let Some(from_env) = std::env::var_os("BRAIN_YTDLP") {
        let p = PathBuf::from(from_env);
        if p.is_file() {
            return Ok(p);
        }
        return Err(youtube(
            "ytdlp_missing",
            format!("BRAIN_YTDLP apunta a {}, que no existe", p.display()),
        ));
    }

    if let Some(beside) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|d| d.join(SIDECAR)))
        .filter(|p| p.is_file())
    {
        return Ok(beside);
    }

    // The checkout, for `tauri dev` on a target the bundler has not staged.
    // Matched by prefix rather than by triple: this crate is not told its own
    // target, and the directory holds one binary per platform, at most one of
    // which can run here anyway.
    if let Some(found) = std::env::var_os("COMPANY_BRAIN_REPO_ROOT")
        .map(PathBuf::from)
        .and_then(|root| std::fs::read_dir(root.join("app/src-tauri/binaries")).ok())
        .and_then(|entries| {
            entries
                .flatten()
                .map(|e| e.path())
                .find(|p| {
                    p.is_file()
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .is_some_and(|n| n.starts_with("yt-dlp-"))
                })
        })
    {
        return Ok(found);
    }

    Err(youtube(
        "ytdlp_missing",
        "esta instalación no trae yt-dlp, así que no puede descargar de \
         YouTube desde este equipo",
    ))
}

/// Ask YouTube what this video is, here.
pub async fn resolve(url: &str, languages: &[String]) -> Result<VideoInfo> {
    let bin = binary()?;
    let run = Command::new(&bin)
        .args([
            "-J",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--",
            url,
        ])
        .stdin(Stdio::null())
        .output();
    let out = match tokio::time::timeout(RESOLVE_TIMEOUT, run).await {
        Err(_) => {
            return Err(youtube(
                "youtube_timed_out",
                format!(
                    "yt-dlp no respondió en {}s",
                    RESOLVE_TIMEOUT.as_secs()
                ),
            ))
        }
        Ok(r) => r.map_err(|e| AppError::io(bin.display(), e))?,
    };
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(youtube(error_kind(&stderr), first_line(&stderr)));
    }
    let info: Value = serde_json::from_slice(&out.stdout).map_err(|e| {
        youtube(
            "youtube_unreadable",
            format!("yt-dlp devolvió algo que no es JSON: {e}"),
        )
    })?;
    narrow(&info, languages)
}

/// Everything in [`resolve`] that is arithmetic, so it can be asserted.
///
/// The same testability split `radial.ts` and `force.ts` make on the other side
/// of the app: what is worth checking is the narrowing and the choice, and a
/// test that needed YouTube to answer would run rarely enough to be worth
/// nothing.
pub fn narrow(info: &Value, languages: &[String]) -> Result<VideoInfo> {
    let live = info.get("is_live").and_then(Value::as_bool).unwrap_or(false)
        || matches!(
            info.get("live_status").and_then(Value::as_str),
            Some("is_upcoming" | "is_live" | "post_live")
        );
    if live {
        return Err(youtube(
            "video_is_live",
            "un directo no tiene duración ni final",
        ));
    }
    let duration = info
        .get("duration")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .trunc() as i64;
    if duration <= 0 {
        return Err(youtube(
            "video_has_no_duration",
            "el vídeo no declara duración",
        ));
    }
    let tracks = tracks_of(info);
    let chosen = choose_track(&tracks, languages);
    let caption_url = chosen
        .as_ref()
        .map(|t| caption_url(info, t))
        .unwrap_or_default();
    Ok(VideoInfo {
        video_id: string_at(info, "id"),
        title: string_at(info, "title"),
        channel: {
            let uploader = string_at(info, "uploader");
            if uploader.is_empty() {
                string_at(info, "channel")
            } else {
                uploader
            }
        },
        duration_s: duration,
        upload_date: string_at(info, "upload_date"),
        format_id: string_at(info, "format_id"),
        tracks,
        chosen,
        caption_url,
    })
}

fn string_at(info: &Value, key: &str) -> String {
    info.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// Every caption track the video offers, manual ones first.
///
/// Forked from `_tracks`. Manual before automatic is not a preference but a
/// quality ordering: an automatic track has no punctuation and carries the
/// rolling duplicates `dedupe_rolling` exists to remove.
///
/// **`serde_json` is built with `preserve_order` for this function.** Python
/// walks the caption dictionaries in insertion order, and `choose_track`'s
/// final pass returns the *first* track that matches nothing in particular — so
/// sorted keys would silently pick a different language from the server for the
/// same video. The feature costs no new crate: `indexmap` is already in the
/// lock file.
pub fn tracks_of(info: &Value) -> Vec<CaptionTrack> {
    let mut out = Vec::new();
    for (kind, key) in [("manual", "subtitles"), ("auto", "automatic_captions")] {
        let Some(langs) = info.get(key).and_then(Value::as_object) else {
            continue;
        };
        for (language, formats) in langs {
            let exts: Vec<&str> = formats
                .as_array()
                .map(|fs| {
                    fs.iter()
                        .filter_map(|f| f.get("ext").and_then(Value::as_str))
                        .collect()
                })
                .unwrap_or_default();
            // `vtt` when it is on offer, otherwise whatever is — the same
            // preference the Python builds out of a set, spelled as a search
            // because a Rust set has no useful "any other one" either.
            let ext = match exts.iter().find(|e| **e == "vtt") {
                Some(vtt) => *vtt,
                None => match exts.first() {
                    Some(first) => first,
                    None => continue,
                },
            };
            out.push(CaptionTrack {
                language: language.clone(),
                kind: kind.to_string(),
                ext: ext.to_string(),
                name: String::new(),
            });
        }
    }
    out
}

/// Which track to read, or `None` when Amazon Transcribe has to run.
///
/// Forked from `_choose_track`. Language preference first, then manual over
/// automatic — in that order, because a human transcript in the wrong language
/// answers no question while a machine transcript in the right one answers
/// most of them — and the **original** language before any translation of it.
///
/// YouTube offers an automatic track in every language it can translate into
/// and marks the source one `-orig`. Measured 2026-09-10 on `yq6uVBsVkeQ`, a
/// 76-minute talk in Spanish: 157 automatic tracks in alphabetical order by
/// code, `ab` first, with the real one at `es-orig`. Expressing no preference
/// used to mean Abkhazian — a machine translation of a machine transcription.
///
/// The sort is **stable**, so it decides ties and reorders nothing else.
pub fn choose_track(tracks: &[CaptionTrack], preferred: &[String]) -> Option<CaptionTrack> {
    let mut vtt: Vec<&CaptionTrack> = tracks.iter().filter(|t| t.ext == "vtt").collect();
    vtt.sort_by_key(|t| !t.language.ends_with("-orig"));
    let mut passes: Vec<&str> = preferred.iter().map(String::as_str).collect();
    passes.push("");
    for lang in passes {
        for kind in ["manual", "auto"] {
            for t in &vtt {
                if t.kind == kind && (lang.is_empty() || t.language.starts_with(lang)) {
                    return Some((*t).clone());
                }
            }
        }
    }
    None
}

/// The signed URL for one track, out of the listing yt-dlp returned.
fn caption_url(info: &Value, track: &CaptionTrack) -> String {
    let key = if track.kind == "manual" {
        "subtitles"
    } else {
        "automatic_captions"
    };
    info.get(key)
        .and_then(|m| m.get(&track.language))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .find(|f| {
            f.get("ext").and_then(Value::as_str) == Some(track.ext.as_str())
                && f.get("url").and_then(Value::as_str).is_some_and(|u| !u.is_empty())
        })
        .and_then(|f| f.get("url").and_then(Value::as_str))
        .unwrap_or_default()
        .to_string()
}

/// `video_unavailable` is a fact about the video; this may be about us.
///
/// Forked from `_download_error_kind`, markers and all. A private, deleted,
/// age-gated or geo-blocked video is a decision that will not change on a
/// second attempt; a refused *caller* is the opposite, and the guidance a
/// person reads is keyed on which one it was.
pub fn error_kind(message: &str) -> &'static str {
    const REFUSALS: [&str; 5] = [
        "confirm you're not a bot",
        "confirm you are not a bot",
        "sign in to confirm",
        "too many requests",
        "http error 429",
    ];
    let low = message.to_lowercase();
    if REFUSALS.iter().any(|m| low.contains(m)) {
        // Reached here it means the *user's own* address is being refused,
        // which is a different situation from the server's and gets its own
        // kind: the advice cannot be "let your machine do it" when it just did.
        return "youtube_refused_this_machine";
    }
    "video_unavailable"
}

/// The most useful line of a yt-dlp failure, which is the last one it printed.
fn first_line(stderr: &str) -> String {
    stderr
        .lines()
        .rev()
        .find(|l| l.trim_start().starts_with("ERROR"))
        .or_else(|| stderr.lines().next_back())
        .unwrap_or("yt-dlp falló sin decir por qué")
        .trim()
        .to_string()
}

/// Download the audio into `into`, reporting progress, and return the file.
///
/// `bestaudio` with **no postprocessor**, which is the same decision the worker
/// makes and for the same reason: Transcribe reads m4a/mp4/webm/ogg directly,
/// so recoding would mean shipping ffmpeg for nothing. If nothing acceptable
/// comes back that is a refusal with a message, not a silent 80 MB of toolchain.
///
/// `into` must be a directory this call owns: the finished file is found by
/// reading it back rather than by parsing yt-dlp's output, because the
/// extension is not known until the format is chosen and every way of asking
/// for it is a string to parse.
pub async fn download_audio(
    url: &str,
    into: &Path,
    mut on_progress: impl FnMut(Progress),
) -> Result<PathBuf> {
    let bin = binary()?;
    std::fs::create_dir_all(into).map_err(|e| AppError::io(into.display(), e))?;

    let mut child = Command::new(&bin)
        .args([
            "--no-playlist",
            "--no-part",
            "--newline",
            "--no-warnings",
            "--progress-template",
            PROGRESS_TEMPLATE,
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "-o",
        ])
        .arg(into.join("audio.%(ext)s"))
        .args(["--", url])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| AppError::io(bin.display(), e))?;

    // Read stdout as it arrives. Progress lines land there, verified against
    // the real binary — the point of a template rather than of parsing
    // `[download]  12.3% of …`, which is prose and changes.
    if let Some(stdout) = child.stdout.take() {
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(p) = parse_progress(&line) {
                on_progress(p);
            }
        }
    }

    let out = child
        .wait_with_output()
        .await
        .map_err(|e| AppError::io(bin.display(), e))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let kind = error_kind(&stderr);
        return Err(youtube(
            if kind == "video_unavailable" {
                "audio_unavailable"
            } else {
                kind
            },
            first_line(&stderr),
        ));
    }

    let mut found = std::fs::read_dir(into)
        .map_err(|e| AppError::io(into.display(), e))?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_file());
    found.next().ok_or_else(|| {
        youtube(
            "audio_unavailable",
            "yt-dlp terminó sin dejar ningún fichero de audio",
        )
    })
}

/// One `--progress-template` line, or `None` for anything else on stdout.
///
/// Pure so the format can be asserted without a download. yt-dlp prints `NA`
/// for a figure it does not have, which is why the total is optional and zero
/// means "it did not say" rather than "zero bytes".
pub fn parse_progress(line: &str) -> Option<Progress> {
    let rest = line.trim().strip_prefix(PROGRESS_PREFIX)?;
    let mut parts = rest.split('/');
    let bytes = parts.next()?.trim().parse::<u64>().ok()?;
    // The estimate first, then the exact figure, because a format being read
    // for the first time has only the estimate and one already known has both.
    let estimate = parts.next().and_then(|s| s.trim().parse::<u64>().ok());
    let exact = parts.next().and_then(|s| s.trim().parse::<u64>().ok());
    Some(Progress {
        bytes,
        total: exact.or(estimate).unwrap_or(0),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn info() -> Value {
        serde_json::json!({
            "id": "jNQXAC9IVRw",
            "title": "Me at the zoo",
            "uploader": "jawed",
            "duration": 19.0,
            "upload_date": "20050424",
            "format_id": "395+251",
            "subtitles": {
                "en": [
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?j"},
                    {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v"}
                ],
                "de": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?d"}]
            },
            "automatic_captions": {
                "es": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?a"}]
            }
        })
    }

    #[test]
    fn a_track_is_offered_once_per_language_and_prefers_vtt() {
        // `_tracks`' shape: one entry per (kind, language), `vtt` when the
        // language offers it. English offers json3 first and vtt second, and
        // the second is the one that must come out — a `json3` track would be
        // chosen and then parsed as WebVTT by `group_transcript`.
        let tracks = tracks_of(&info());
        assert_eq!(tracks.len(), 3);
        assert!(tracks.iter().all(|t| t.ext == "vtt"));
        assert_eq!(
            tracks.iter().map(|t| t.kind.as_str()).collect::<Vec<_>>(),
            vec!["manual", "manual", "auto"],
            "manual tracks come first, which is what `choose_track` relies on"
        );
    }

    #[test]
    fn language_beats_quality_and_quality_breaks_the_tie() {
        // The ordering `_choose_track` documents: a human transcript in the
        // wrong language answers no question, a machine one in the right
        // language answers most of them. So Spanish wins even though it is
        // automatic and English is manual.
        let tracks = tracks_of(&info());
        let es = choose_track(&tracks, &["es".to_string()]).unwrap();
        assert_eq!((es.language.as_str(), es.kind.as_str()), ("es", "auto"));

        let de = choose_track(&tracks, &["de".to_string()]).unwrap();
        assert_eq!((de.language.as_str(), de.kind.as_str()), ("de", "manual"));

        // With nothing to prefer, quality decides and insertion order breaks
        // the remaining tie — which is why `serde_json` preserves it.
        let any = choose_track(&tracks, &[]).unwrap();
        assert_eq!((any.language.as_str(), any.kind.as_str()), ("en", "manual"));
    }

    #[test]
    fn the_original_language_beats_a_translation_of_it() {
        // The real shape, measured on `yq6uVBsVkeQ`: 157 automatic tracks in
        // alphabetical order by code, and the one YouTube actually heard at
        // `es-orig`. No preference used to mean Abkhazian.
        let tracks: Vec<CaptionTrack> = ["ab", "aa", "af", "es", "es-orig", "sq"]
            .iter()
            .map(|l| CaptionTrack {
                language: (*l).into(),
                kind: "auto".into(),
                ext: "vtt".into(),
                name: String::new(),
            })
            .collect();
        assert_eq!(choose_track(&tracks, &[]).unwrap().language, "es-orig");
        // It holds inside a preference too: `es` matches both by prefix, and
        // one of them has been round tripped through a translator.
        assert_eq!(
            choose_track(&tracks, &["es".into()]).unwrap().language,
            "es-orig"
        );
        // And it decides ties rather than overruling the caller.
        assert_eq!(choose_track(&tracks, &["af".into()]).unwrap().language, "af");
        // Stable, so a video with nothing marked is unchanged.
        assert_eq!(choose_track(&tracks[..3], &[]).unwrap().language, "ab");
    }

    #[test]
    fn a_language_is_matched_by_prefix_so_es_419_counts_as_es() {
        let tracks = vec![CaptionTrack {
            language: "es-419".into(),
            kind: "auto".into(),
            ext: "vtt".into(),
            name: String::new(),
        }];
        assert!(choose_track(&tracks, &["es".to_string()]).is_some());
        assert!(choose_track(&tracks, &["en".to_string()]).is_some(), "the empty pass still matches");
    }

    #[test]
    fn no_captions_at_all_means_transcribe_must_run() {
        let bare = serde_json::json!({"id": "x", "duration": 5});
        let narrowed = narrow(&bare, &[]).unwrap();
        assert!(narrowed.chosen.is_none());
        assert_eq!(
            narrowed.caption_url, "",
            "a caption URL with no chosen track is what `check_resolved` refuses"
        );
    }

    #[test]
    fn the_caption_url_is_the_chosen_tracks_own() {
        let narrowed = narrow(&info(), &["es".to_string()]).unwrap();
        assert_eq!(narrowed.caption_url, "https://www.youtube.com/api/timedtext?a");
        assert_eq!(narrowed.video_id, "jNQXAC9IVRw");
        assert_eq!(narrowed.duration_s, 19);
        assert_eq!(narrowed.channel, "jawed");
    }

    #[test]
    fn a_live_stream_and_a_video_with_no_duration_are_refused_before_anything_is_started() {
        let live = serde_json::json!({"id": "x", "duration": 60, "live_status": "is_live"});
        let err = narrow(&live, &[]).unwrap_err();
        assert_eq!(err.kind(), "video_is_live");

        let no_duration = serde_json::json!({"id": "x"});
        assert_eq!(narrow(&no_duration, &[]).unwrap_err().kind(), "video_has_no_duration");
    }

    #[test]
    fn a_refusal_of_this_machine_is_not_a_missing_video() {
        // The distinction `_download_error_kind` makes, with the same markers.
        // It matters more here than there: the whole point of this module is
        // that this machine is the one YouTube answers, so being refused *here*
        // cannot be met with the advice that the server's refusal gets.
        assert_eq!(
            error_kind("ERROR: [youtube] xyz: Sign in to confirm you're not a bot"),
            "youtube_refused_this_machine"
        );
        assert_eq!(error_kind("ERROR: HTTP Error 429: Too Many Requests"), "youtube_refused_this_machine");
        assert_eq!(
            error_kind("ERROR: [youtube] xyz: Video unavailable. This video is private"),
            "video_unavailable"
        );
    }

    #[test]
    fn progress_reads_the_template_and_nothing_else_on_stdout() {
        // Verified against yt-dlp 2026.08.19: these arrive on stdout, one per
        // update, interleaved with `[youtube] …` lines.
        assert!(parse_progress("[youtube] jNQXAC9IVRw: Downloading webpage").is_none());
        let p = parse_progress("brainprogress:1024/4096/NA").unwrap();
        assert_eq!((p.bytes, p.total), (1024, 4096));
        // `NA` where the estimate should be, an exact figure after it.
        let exact = parse_progress("brainprogress:2048/NA/8192").unwrap();
        assert_eq!((exact.bytes, exact.total), (2048, 8192));
        // Neither: a total of zero means "it did not say", never "no bytes".
        let unknown = parse_progress("brainprogress:16/NA/NA").unwrap();
        assert_eq!((unknown.bytes, unknown.total), (16, 0));
    }

    /// The one test here that talks to YouTube, and therefore the only one
    /// that can say the bundled binary works at all.
    ///
    /// `#[ignore]` because a suite that needs the network is a suite that goes
    /// red for reasons that are nothing to do with the code — the same
    /// judgement `tests/corpus.py` makes about a document it cannot find. Run
    /// it deliberately:
    ///
    /// ```text
    /// COMPANY_BRAIN_REPO_ROOT=/home/kheiron/yorch cargo test -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore = "talks to YouTube"]
    fn it_really_resolves_a_real_video() {
        let info = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(resolve("https://youtu.be/jNQXAC9IVRw", &["en".into()]))
            .expect("yt-dlp should answer this machine — that is the premise");
        assert_eq!(info.video_id, "jNQXAC9IVRw");
        assert_eq!(info.duration_s, 19);
        let chosen = info.chosen.as_ref().expect("this video has captions");
        assert_eq!(chosen.language, "en");
        assert!(info.caption_url.starts_with("https://www.youtube.com/api/timedtext"));
        println!("{}", serde_json::to_string_pretty(&info).unwrap());
    }

    #[test]
    fn a_missing_binary_is_its_own_kind_rather_than_a_download_failure() {
        // Guidance keys on the kind, and "install the app again" is nothing
        // like "the video is private".
        std::env::set_var("BRAIN_YTDLP", "/definitely/not/here/yt-dlp");
        let err = binary().unwrap_err();
        std::env::remove_var("BRAIN_YTDLP");
        assert_eq!(err.kind(), "ytdlp_missing");
    }
}
