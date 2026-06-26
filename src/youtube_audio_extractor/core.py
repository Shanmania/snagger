from __future__ import annotations

import queue
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


QUALITY_CHOICES = {
    "Maximum VBR quality": "0",
    "320 kbps CBR": "320",
    "256 kbps CBR": "256",
    "192 kbps CBR": "192",
}

MEDIA_FORMAT_CHOICES = {
    "mp3": "MP3 audio",
    "mp4": "MP4 video",
}

PREMIERE_SAFE_MP4_FORMAT = (
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a][acodec^=mp4a]/"
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
    "best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
    "best[ext=mp4][vcodec^=avc1]"
)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class DownloadSettings:
    url: str
    output_dir: Path
    quality_label: str
    quality_value: str
    keep_source_audio: bool
    output_format: str = "mp3"
    allow_playlist: bool = False


class QueueLogger:
    def __init__(self, events: "queue.Queue[tuple[str, Any]]") -> None:
        self.events = events

    def debug(self, message: str) -> None:
        message = clean_message(message)
        if not message or message.startswith("[debug]"):
            return
        if message.startswith("[download]") and "%" in message:
            return
        self.events.put(("log", message))

    def warning(self, message: str) -> None:
        self.events.put(("log", f"Warning: {clean_message(message)}"))

    def error(self, message: str) -> None:
        self.events.put(("log", f"Error: {clean_message(message)}"))


def clean_message(value: object) -> str:
    return ANSI_RE.sub("", str(value)).strip()


def format_duration(seconds: object) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None

    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def best_thumbnail(info: dict[str, Any]) -> str | None:
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail

    thumbnails = [item for item in info.get("thumbnails") or [] if isinstance(item, dict) and item.get("url")]
    if not thumbnails:
        video_id = info.get("id")
        if isinstance(video_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        return None

    def thumbnail_size(item: dict[str, Any]) -> int:
        return int(item.get("width") or 0) * int(item.get("height") or 0)

    return str(max(thumbnails, key=thumbnail_size)["url"])


def as_positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def playlist_position(data: dict[str, Any]) -> tuple[int, int] | None:
    info = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}
    index = as_positive_int(data.get("playlist_index") or info.get("playlist_index"))
    count = as_positive_int(
        data.get("playlist_count")
        or data.get("n_entries")
        or info.get("playlist_count")
        or info.get("n_entries")
    )
    if not index or not count:
        return None
    return min(index, count), count


def playlist_status_suffix(data: dict[str, Any]) -> str:
    position = playlist_position(data)
    if not position:
        return ""
    index, count = position
    return f" {index}/{count}"


def aggregate_playlist_progress(data: dict[str, Any], item_fraction: float) -> float | None:
    position = playlist_position(data)
    if not position:
        return None
    index, count = position
    clamped_fraction = max(0.0, min(0.98, item_fraction))
    return min(98.0, ((index - 1) + clamped_fraction) / count * 98.0)


def extract_media_preview(url: str, allow_playlist: bool) -> dict[str, Any]:
    try:
        import yt_dlp

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not allow_playlist,
        }
        if allow_playlist:
            options["extract_flat"] = "in_playlist"

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        if not isinstance(info, dict):
            raise RuntimeError("Could not read this URL.")

        entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
        if allow_playlist and (info.get("_type") == "playlist" or entries):
            thumbnail = best_thumbnail(info)
            if not thumbnail and entries:
                thumbnail = best_thumbnail(entries[0])

            return {
                "kind": "playlist",
                "title": info.get("title") or info.get("playlist_title") or "Untitled playlist",
                "thumbnail": thumbnail,
                "channel": info.get("channel") or info.get("uploader"),
                "count": info.get("playlist_count") or len(entries) or None,
            }

        return {
            "kind": "video",
            "title": info.get("title") or "Untitled video",
            "thumbnail": best_thumbnail(info),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": format_duration(info.get("duration")),
        }
    except Exception as exc:
        raise RuntimeError(clean_message(exc)) from exc


def default_output_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.cwd()


def resolve_ffmpeg() -> str | None:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        ffmpeg = get_ffmpeg_exe()
        if ffmpeg and Path(ffmpeg).exists():
            return ffmpeg
    except Exception:
        pass

    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def build_ydl_options(
    settings: DownloadSettings,
    events: "queue.Queue[tuple[str, Any]]",
) -> dict[str, Any]:
    if settings.output_format not in MEDIA_FORMAT_CHOICES:
        raise RuntimeError("Choose MP3 or MP4 output.")

    ffmpeg_path = resolve_ffmpeg()
    if not ffmpeg_path:
        raise RuntimeError(
            "FFmpeg was not found. Reinstall the app, rebuild with build_windows.ps1, "
            "or install FFmpeg and add it to PATH."
        )

    def progress_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        playlist_suffix = playlist_status_suffix(data) if settings.allow_playlist else ""
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            if total:
                item_fraction = downloaded / total
                playlist_progress = (
                    aggregate_playlist_progress(data, item_fraction * 0.80)
                    if settings.allow_playlist
                    else None
                )
                progress = playlist_progress if playlist_progress is not None else min(80.0, item_fraction * 80.0)
                events.put(("progress", progress))

            percent = clean_message(data.get("_percent_str", ""))
            speed = clean_message(data.get("_speed_str", ""))
            eta = clean_message(data.get("_eta_str", ""))
            detail = " ".join(part for part in (percent, speed, f"ETA {eta}" if eta else "") if part)
            if detail:
                media_type = "audio" if settings.output_format == "mp3" else "video"
                events.put(("status", f"Downloading {media_type}{playlist_suffix}... {detail}"))
        elif status == "finished":
            playlist_progress = (
                aggregate_playlist_progress(data, 0.85)
                if settings.allow_playlist
                else None
            )
            events.put(("progress", playlist_progress if playlist_progress is not None else 85.0))
            next_step = "Converting to MP3" if settings.output_format == "mp3" else "Finalizing MP4"
            next_step = f"{next_step}{playlist_suffix}..."
            events.put(("status", next_step))
            filename = data.get("filename")
            if filename:
                events.put(("log", f"Downloaded source file: {Path(filename).name}"))
        elif status == "error":
            events.put(("status", "Download failed."))

    def postprocessor_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        postprocessor = data.get("postprocessor") or "FFmpeg"
        playlist_suffix = playlist_status_suffix(data) if settings.allow_playlist else ""
        if status == "started":
            playlist_progress = (
                aggregate_playlist_progress(data, 0.90)
                if settings.allow_playlist
                else None
            )
            events.put(("progress", playlist_progress if playlist_progress is not None else 90.0))
            events.put(("status", f"{postprocessor}: converting{playlist_suffix}..."))
        elif status == "finished":
            playlist_progress = (
                aggregate_playlist_progress(data, 0.98)
                if settings.allow_playlist
                else None
            )
            events.put(("progress", playlist_progress if playlist_progress is not None else 96.0))
            events.put(("status", f"{postprocessor}: finished{playlist_suffix}."))

    options: dict[str, Any] = {
        "outtmpl": output_template(settings),
        "noplaylist": not settings.allow_playlist,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "logger": QueueLogger(events),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "ffmpeg_location": ffmpeg_path,
    }

    if settings.output_format == "mp3":
        options.update(
            {
                "format": "bestaudio/best",
                "keepvideo": settings.keep_source_audio,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": settings.quality_value,
                    }
                ],
            }
        )
        return options

    options.update(
        {
            "format": PREMIERE_SAFE_MP4_FORMAT,
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                }
            ],
        }
    )
    return options


def transcode_mp4_for_premiere(path: Path, ffmpeg_path: str) -> None:
    temp_path = path.with_name(f".snagger-premiere-{time.time_ns()}{path.suffix}")
    counter = 2
    while temp_path.exists():
        temp_path = path.with_name(f".snagger-premiere-{time.time_ns()}-{counter}{path.suffix}")
        counter += 1

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "avc1",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-af",
        "aresample=async=1:first_pts=0",
        "-tag:a",
        "mp4a",
        "-map_metadata",
        "0",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            message = clean_message(result.stderr or result.stdout or "unknown FFmpeg error")
            raise RuntimeError(f"Could not make a Premiere-friendly MP4: {message}")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def locate_output_file(output_dir: Path, started_at: float, suffix: str) -> Path | None:
    candidates = [
        path
        for path in output_dir.glob(f"*.{suffix}")
        if path.is_file() and path.stat().st_mtime >= started_at - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def locate_output_files(output_dir: Path, started_at: float, suffix: str) -> list[Path]:
    return sorted(
        (
            path
            for path in output_dir.rglob(f"*.{suffix}")
            if path.is_file() and path.stat().st_mtime >= started_at - 2
        ),
        key=lambda path: str(path),
    )


def locate_output_mp3(output_dir: Path, started_at: float) -> Path | None:
    return locate_output_file(output_dir, started_at, "mp3")


def output_template(settings: DownloadSettings) -> str:
    if settings.allow_playlist:
        return str(
            settings.output_dir
            / "%(playlist_title).180B"
            / "%(playlist_index)03d - %(title).180B [%(id)s].%(ext)s"
        )
    return str(settings.output_dir / "%(title).200B [%(id)s].%(ext)s")


def iter_download_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") == "playlist" or info.get("entries"):
        return [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    return [info]


def expected_output_path(ydl: Any, info: dict[str, Any], output_format: str) -> Path:
    paths = expected_output_paths(ydl, info, output_format)
    if paths:
        return paths[0]
    return Path(ydl.prepare_filename(info)).with_suffix(f".{output_format}")


def expected_output_paths(ydl: Any, info: dict[str, Any], output_format: str) -> list[Path]:
    paths: list[Path] = []
    for entry in iter_download_entries(info):
        if not entry.get("id"):
            continue

        requested_downloads = entry.get("requested_downloads") or []
        matched_output = False
        for download in requested_downloads:
            filepath = download.get("filepath")
            if filepath:
                path = Path(filepath)
                if path.suffix.lower() == f".{output_format}":
                    matched_output = True
                    paths.append(path)

        if not matched_output:
            paths.append(Path(ydl.prepare_filename(entry)).with_suffix(f".{output_format}"))

    return paths


def download_media(
    settings: DownloadSettings,
    events: "queue.Queue[tuple[str, Any]]",
) -> None:
    started_at = time.time()
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        import yt_dlp

        output_label = MEDIA_FORMAT_CHOICES.get(settings.output_format, settings.output_format.upper())
        events.put(("status", "Preparing download..."))
        events.put(("log", f"Output format: {output_label}."))
        if settings.output_format == "mp3":
            events.put(("log", "Using best available YouTube audio stream."))
            events.put(("log", f"MP3 quality: {settings.quality_label}"))
            if settings.allow_playlist:
                events.put(("log", "Playlist mode: downloading each video as a separate MP3."))
            if settings.keep_source_audio:
                events.put(("log", "Keeping the original source audio file too."))
        else:
            events.put(("log", "Using Premiere-friendly MP4 video: H.264 video with AAC-LC audio."))

        ydl_options = build_ydl_options(settings, events)
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(settings.url, download=True)
            expected_paths = expected_output_paths(ydl, info, settings.output_format)

        output_paths = [path for path in expected_paths if path.exists()]
        if not output_paths:
            output_paths = locate_output_files(settings.output_dir, started_at, settings.output_format)
        if settings.output_format == "mp4" and output_paths:
            events.put(("status", "Transcoding MP4 for Premiere..."))
            for output_path in output_paths:
                transcode_mp4_for_premiere(output_path, ydl_options["ffmpeg_location"])
            events.put(("log", "Transcoded MP4 for Premiere: H.264 video and AAC-LC stereo at 48 kHz."))
        events.put(("progress", 100.0))
        if settings.allow_playlist:
            if output_paths:
                events.put(("status", f"Done: {len(output_paths)} MP3 files"))
                events.put(("done", output_paths))
            else:
                events.put(("status", "Done."))
                events.put(("done", []))
        elif output_paths:
            events.put(("status", f"Done: {output_paths[0].name}"))
            events.put(("done", output_paths[0]))
        else:
            events.put(("status", "Done."))
            events.put(("done", None))
    except Exception as exc:
        events.put(("error", clean_message(exc)))


def download_audio(
    settings: DownloadSettings,
    events: "queue.Queue[tuple[str, Any]]",
) -> None:
    download_media(settings, events)
