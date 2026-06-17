from __future__ import annotations

import os
import queue
import re
import secrets
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from flask import Flask, Response, abort, after_this_request, jsonify, render_template_string, request, send_file, send_from_directory

from .core import (
    DownloadSettings,
    MEDIA_FORMAT_CHOICES,
    QUALITY_CHOICES,
    clean_message,
    default_output_dir,
    download_media,
    extract_media_preview,
)


ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


@dataclass
class DownloadJob:
    id: str
    settings: DownloadSettings
    save_to_server: bool
    cleanup_dir: Path | None = None
    events: "queue.Queue[tuple[str, Any]]" = field(default_factory=queue.Queue)
    status: str = "queued"
    progress: float = 0.0
    message: str = "Queued."
    log: list[str] = field(default_factory=list)
    output_path: Path | None = None
    output_paths: list[Path] = field(default_factory=list)
    archive_path: Path | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


jobs: dict[str, DownloadJob] = {}
jobs_lock = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__)
    output_dir = configured_output_dir()

    @app.before_request
    def require_basic_auth() -> Response | None:
        username = os.environ.get("SNAGGER_USERNAME")
        password = os.environ.get("SNAGGER_PASSWORD")
        if not (username and password):
            return None

        auth = request.authorization
        if auth and secrets.compare_digest(auth.username or "", username or "") and secrets.compare_digest(
            auth.password or "",
            password or "",
        ):
            return None

        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="snagger"'},
        )

    @app.get("/")
    def index() -> str:
        return render_template_string(
            INDEX_HTML,
            media_format_choices=MEDIA_FORMAT_CHOICES,
            quality_choices=list(QUALITY_CHOICES),
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return send_from_directory(Path(app.root_path) / "static", "favicon.ico", mimetype="image/vnd.microsoft.icon")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/preview")
    def preview() -> tuple[dict[str, Any], int] | Response:
        url = str(request.args.get("url") or "").strip()
        if not is_allowed_youtube_url(url):
            return {"error": "Enter a valid YouTube URL."}, 400

        try:
            return jsonify(extract_media_preview(url, allow_playlist=is_youtube_playlist_url(url)))
        except RuntimeError as exc:
            return {"error": clean_message(exc)}, 400

    @app.post("/api/jobs")
    def create_job() -> tuple[dict[str, Any], int]:
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url") or "").strip()
        output_format = str(payload.get("media_format") or "mp3").strip().lower()
        quality_label = str(payload.get("quality") or "Maximum VBR quality")
        keep_source_audio = output_format == "mp3" and bool(payload.get("keep_source_audio"))
        save_to_server = bool(payload.get("deploy_to_server"))
        download_playlist = (
            output_format == "mp3"
            and is_youtube_playlist_url(url)
            and payload.get("download_playlist", True) is not False
        )

        if not is_allowed_youtube_url(url):
            return {"error": "Enter a valid YouTube URL."}, 400
        if output_format not in MEDIA_FORMAT_CHOICES:
            return {"error": "Choose MP3 or MP4 output."}, 400
        if output_format == "mp3" and quality_label not in QUALITY_CHOICES:
            return {"error": "Choose a valid MP3 quality."}, 400
        if quality_label not in QUALITY_CHOICES:
            quality_label = "Maximum VBR quality"

        job_output_dir = output_dir if save_to_server else temporary_output_dir()
        settings = DownloadSettings(
            url=url,
            output_dir=job_output_dir,
            quality_label=quality_label,
            quality_value=QUALITY_CHOICES[quality_label],
            keep_source_audio=keep_source_audio,
            output_format=output_format,
            allow_playlist=download_playlist,
        )
        job = DownloadJob(
            id=uuid4().hex,
            settings=settings,
            save_to_server=save_to_server,
            cleanup_dir=None if save_to_server else job_output_dir,
        )

        with jobs_lock:
            jobs[job.id] = job

        worker = threading.Thread(target=run_job, args=(job,), daemon=True)
        worker.start()
        return job_payload(job), 202

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str) -> dict[str, Any]:
        job = find_job(job_id)
        return job_payload(job)

    @app.get("/downloads/<job_id>")
    def download_job(job_id: str) -> Response:
        job = find_job(job_id)
        drain_events(job)
        if job.status != "done" or not job.output_path:
            abort(404)

        output_path = job.output_path.resolve()
        output_root = job.settings.output_dir.resolve()
        if not output_path.is_file() or not output_path.is_relative_to(output_root):
            abort(404)

        if not job.save_to_server:
            @after_this_request
            def cleanup_after_download(response: Response) -> Response:
                cleanup_temporary_job(job)
                return response

        return send_file(output_path, as_attachment=True, download_name=output_path.name)

    return app


def configured_output_dir() -> Path:
    output = os.environ.get("SNAGGER_OUTPUT_DIR")
    return Path(output).expanduser() if output else default_output_dir()


def temporary_output_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="snagger-"))


def cleanup_temporary_job(job: DownloadJob) -> None:
    if job.cleanup_dir:
        shutil.rmtree(job.cleanup_dir, ignore_errors=True)
    with jobs_lock:
        jobs.pop(job.id, None)


def is_allowed_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return (parsed.hostname or "").lower() in ALLOWED_YOUTUBE_HOSTS


def is_youtube_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    if not (parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in ALLOWED_YOUTUBE_HOSTS):
        return False

    query = parse_qs(parsed.query)
    return bool(query.get("list")) or parsed.path.rstrip("/") == "/playlist"


def find_job(job_id: str) -> DownloadJob:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


def run_job(job: DownloadJob) -> None:
    job.status = "running"
    job.message = "Starting..."
    download_media(job.settings, job.events)
    drain_events(job)


def drain_events(job: DownloadJob) -> None:
    while True:
        try:
            event, value = job.events.get_nowait()
        except queue.Empty:
            break

        if event == "status":
            job.message = str(value)
        elif event == "progress":
            job.progress = float(value)
        elif event == "log":
            job.log.append(str(value))
            job.log = job.log[-200:]
        elif event == "done":
            job.output_paths = normalize_output_paths(value)
            job.archive_path = None
            should_bundle = (
                not job.save_to_server
                and (len(job.output_paths) > 1 or (job.settings.allow_playlist and job.output_paths))
            )
            if should_bundle:
                try:
                    job.archive_path = create_archive(job.settings.output_dir, job.output_paths)
                    job.output_path = job.archive_path
                    job.log.append(f"Created browser download bundle: {job.archive_path.name}")
                except OSError as exc:
                    job.error = str(exc)
                    job.status = "error"
                    job.message = "Failed."
                    job.progress = 0.0
                    job.log.append(str(exc))
                    continue
            elif job.save_to_server and len(job.output_paths) > 1:
                job.output_path = None
                job.log.append(f"Saved {len(job.output_paths)} files to the server folder without creating a zip.")
            else:
                job.output_path = job.output_paths[0] if job.output_paths else None
            job.status = "done"
            job.progress = 100.0
        elif event == "error":
            job.error = str(value)
            job.status = "error"
            job.message = "Failed."
            job.progress = 0.0
            job.log.append(str(value))


def normalize_output_paths(value: Any) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    if isinstance(value, list):
        return [path for path in value if isinstance(path, Path)]
    return []


def create_archive(output_dir: Path, output_paths: list[Path]) -> Path:
    output_root = output_dir.resolve()
    archive_path = unique_path(output_root / f"{sanitize_filename(output_group_name(output_root, output_paths))}.zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in output_paths:
            resolved_path = path.resolve()
            if not resolved_path.is_file() or not resolved_path.is_relative_to(output_root):
                continue
            archive.write(resolved_path, resolved_path.relative_to(output_root))
    return archive_path


def output_group_name(output_root: Path, output_paths: list[Path]) -> str:
    parents = [path.resolve().parent for path in output_paths]
    common_parent = Path(os.path.commonpath([str(parent) for parent in parents])) if parents else output_root
    if common_parent != output_root:
        return common_parent.name
    return "snagger-playlist"


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" ._")
    return cleaned[:120] or "snagger-playlist"


def unique_path(path: Path) -> Path:
    candidate = path
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        counter += 1
    return candidate


def job_payload(job: DownloadJob) -> dict[str, Any]:
    drain_events(job)
    payload: dict[str, Any] = {
        "id": job.id,
        "status": job.status,
        "progress": round(job.progress, 1),
        "message": job.message,
        "log": job.log,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "media_format": job.settings.output_format,
        "save_to_server": job.save_to_server,
        "playlist_download": job.settings.allow_playlist,
        "files_count": len(job.output_paths),
    }
    if job.status == "done" and job.output_path:
        payload["filename"] = job.output_path.name
        payload["download_url"] = f"/downloads/{job.id}"
        payload["download_label"] = "ZIP" if job.archive_path else job.settings.output_format.upper()
    elif job.status == "done" and job.save_to_server and job.output_paths:
        file_label = "file" if len(job.output_paths) == 1 else "files"
        folder = output_group_name(job.settings.output_dir.resolve(), job.output_paths)
        payload["server_result"] = f"Saved {len(job.output_paths)} {file_label} to server folder: {folder}"
        payload["server_folder"] = folder
    return payload


app = create_app()


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Snagger</title>
  <link rel="icon" href="/favicon.ico" sizes="any">
  <style>
    :root {
      color-scheme: dark;
      --bg: #10110f;
      --panel: #181b17;
      --panel-strong: #20241f;
      --ink: #edf2ec;
      --muted: #a3ada0;
      --line: #333b31;
      --accent: #23c7a7;
      --accent-strong: #18a88c;
      --accent-warm: #f0b84a;
      --danger: #ff746d;
      --shadow: 0 18px 42px rgba(0, 0, 0, 0.36);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(35, 199, 167, 0.16), transparent 360px),
        linear-gradient(180deg, #141713, var(--bg) 340px),
        var(--bg);
      color: var(--ink);
      font-family: "Inter", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      letter-spacing: 0;
    }

    main {
      width: min(920px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 24px 0 28px;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 16px;
    }

    h1 {
      margin: 0;
      font-size: clamp(28px, 4vw, 38px);
      line-height: 1.08;
      font-weight: 760;
    }

    .status-pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 12px;
      color: var(--muted);
      background: rgba(32, 36, 31, 0.82);
      white-space: nowrap;
      font-size: 13px;
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 14px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    form.panel {
      padding: 16px;
    }

    .field {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
    }

    .settings-row {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 12px;
      align-items: end;
    }

    label {
      font-size: 13px;
      font-weight: 700;
      color: #c6d0c3;
    }

    input[type="url"], select {
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      color: var(--ink);
      background: #111410;
      font: inherit;
    }

    input[type="url"]:focus, select:focus {
      outline: 2px solid rgba(35, 199, 167, 0.28);
      border-color: var(--accent);
    }

    .mode-switch {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      width: 100%;
      min-height: 48px;
      margin: 0;
      padding: 5px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #101410;
    }

    .mode-switch label {
      display: block;
      min-width: 0;
      color: inherit;
      font-size: 14px;
      font-weight: 800;
    }

    .mode-switch input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }

    .mode-switch span {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: 36px;
      border: 1px solid transparent;
      border-radius: 8px;
      color: var(--muted);
      text-align: center;
      font-weight: 760;
      letter-spacing: 0.02em;
      cursor: pointer;
      user-select: none;
      transition: background 140ms ease, border-color 140ms ease, color 140ms ease, box-shadow 140ms ease;
    }

    .mode-switch input:not(:checked) + span:hover {
      color: var(--ink);
      background: #171d17;
      border-color: #3c4739;
    }

    .mode-switch input:checked + span {
      color: #071310;
      background: linear-gradient(135deg, var(--accent), #75e08f);
      border-color: rgba(206, 255, 225, 0.22);
      box-shadow: 0 10px 22px rgba(35, 199, 167, 0.26);
    }

    .mode-switch input:focus-visible + span {
      outline: 2px solid rgba(35, 199, 167, 0.42);
      outline-offset: 2px;
    }

    .inline {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 32px;
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 14px;
    }

    .playlist-row {
      color: #cfeee6;
    }

    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }

    .server-toggle {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      min-height: 44px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--muted);
      background: #101410;
      font-size: 14px;
      font-weight: 740;
      cursor: pointer;
      user-select: none;
    }

    .server-toggle input {
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
      cursor: pointer;
    }

    .server-toggle:has(input:checked) {
      color: var(--ink);
      border-color: rgba(35, 199, 167, 0.52);
      background: rgba(35, 199, 167, 0.1);
    }

    button, .download {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      border: 0;
      border-radius: 6px;
      padding: 0 18px;
      color: #071310;
      background: linear-gradient(135deg, var(--accent), #80e493);
      font: inherit;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
      box-shadow: 0 12px 24px rgba(35, 199, 167, 0.2);
      transition: transform 140ms ease, box-shadow 140ms ease, filter 140ms ease;
    }

    button:hover, .download:hover {
      filter: brightness(1.08);
      transform: translateY(-1px);
      box-shadow: 0 16px 30px rgba(35, 199, 167, 0.24);
    }

    button:disabled {
      cursor: wait;
      opacity: 0.64;
      transform: none;
    }

    .progress-panel {
      padding: 15px 16px;
      min-height: 118px;
      margin-top: 12px;
    }

    .progress-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 10px;
    }

    .message {
      margin: 0;
      min-height: 22px;
      font-weight: 720;
    }

    .percent {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }

    .track {
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #30382f;
    }

    .bar {
      width: 0%;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-warm));
      transition: width 180ms ease;
    }

    .result {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 12px;
      min-height: 44px;
    }

    .filename {
      min-width: 0;
      color: var(--muted);
      overflow-wrap: anywhere;
      font-size: 14px;
    }

    .side {
      padding: 14px;
      background: var(--panel-strong);
    }

    .side-column {
      display: grid;
      gap: 12px;
    }

    .side h2 {
      margin: 0 0 10px;
      font-size: 16px;
    }

    .side p {
      margin: 0;
      color: var(--muted);
      line-height: 1.4;
      font-size: 14px;
    }

    .preview-content {
      display: grid;
      gap: 10px;
    }

    .preview-thumb {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101410;
    }

    .preview-kicker {
      margin-bottom: 4px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .preview-title {
      color: var(--ink);
      font-size: 15px;
      font-weight: 760;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .preview-detail {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }

    .log {
      margin-top: 12px;
      min-height: 132px;
      max-height: 240px;
      overflow: auto;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0c0a;
      color: #dbe7d7;
      font: 13px/1.42 Consolas, "SFMono-Regular", monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .error {
      color: var(--danger);
      font-weight: 720;
    }

    [hidden] {
      display: none !important;
    }

    @media (max-width: 760px) {
      main {
        width: min(100vw - 20px, 640px);
        padding-top: 20px;
      }

      .topbar, .workspace {
        display: block;
      }

      .status-pill {
        display: inline-flex;
        margin-top: 12px;
      }

      .progress-panel, .side-column {
        margin-top: 12px;
      }

      .settings-row {
        display: block;
      }

      .result {
        align-items: stretch;
        flex-direction: column;
      }
    }
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <h1>Snagger</h1>
      <div class="status-pill" id="appState">Ready</div>
    </div>

    <div class="workspace">
      <section>
        <form class="panel" id="convertForm">
          <div class="field">
            <label for="url">YouTube URL</label>
            <input id="url" name="url" type="url" placeholder="https://www.youtube.com/watch?v=..." required>
          </div>

          <div class="settings-row">
            <div class="field">
              <label>Mode</label>
              <div class="mode-switch" role="radiogroup" aria-label="Output mode">
                {% for value, label in media_format_choices.items() %}
                  <label>
                    <input type="radio" name="media_format" value="{{ value }}" {% if value == "mp3" %}checked{% endif %}>
                    <span>{{ value.upper() }}</span>
                  </label>
                {% endfor %}
              </div>
            </div>

            <div class="field" id="qualityField">
              <label for="quality">MP3 quality</label>
              <select id="quality" name="quality">
                {% for quality in quality_choices %}
                  <option value="{{ quality }}">{{ quality }}</option>
                {% endfor %}
              </select>
            </div>
          </div>

          <label class="inline" id="keepSourceRow">
            <input id="keepSource" name="keepSource" type="checkbox">
            <span>Keep original source audio too</span>
          </label>

          <label class="inline playlist-row" id="playlistRow" hidden>
            <input id="downloadPlaylist" name="downloadPlaylist" type="checkbox">
            <span>Download playlist as separate MP3s</span>
          </label>

          <div class="actions">
            <button id="submitButton" type="submit">Snag MP3</button>
            <label class="server-toggle" title="Also save the finished file to the server downloads folder.">
              <input id="deployToServer" name="deployToServer" type="checkbox">
              <span>Deploy to server</span>
            </label>
            <span class="error" id="errorText" hidden></span>
          </div>
        </form>

        <div class="panel progress-panel">
          <div class="progress-header">
            <p class="message" id="message">Waiting for a link.</p>
            <span class="percent" id="percent">0%</span>
          </div>
          <div class="track" aria-label="Conversion progress">
            <div class="bar" id="bar"></div>
          </div>
          <div class="result" id="result" hidden>
            <div class="filename" id="filename"></div>
            <a class="download" id="downloadLink" href="#">Download file</a>
          </div>
        </div>

        <div class="log" id="log" aria-live="polite"></div>
      </section>

      <aside class="side-column">
        <div class="panel side">
          <h2>Server Notes</h2>
          <p>Files are sent to your browser by default. Check Deploy to server to also save the finished file in the mounted downloads folder.</p>
        </div>

        <div class="panel side" id="previewPanel" hidden>
          <h2>Preview</h2>
          <p id="previewLoading">Looking up URL...</p>
          <div class="preview-content" id="previewContent" hidden>
            <img class="preview-thumb" id="previewThumb" alt="">
            <div>
              <div class="preview-kicker" id="previewKind">Video</div>
              <div class="preview-title" id="previewTitle"></div>
              <div class="preview-detail" id="previewDetail"></div>
            </div>
          </div>
        </div>
      </aside>
    </div>
  </main>

  <script>
    const form = document.querySelector("#convertForm");
    const urlInput = document.querySelector("#url");
    const mediaFormatInputs = Array.from(document.querySelectorAll('input[name="media_format"]'));
    const qualityField = document.querySelector("#qualityField");
    const keepSourceRow = document.querySelector("#keepSourceRow");
    const playlistRow = document.querySelector("#playlistRow");
    const downloadPlaylist = document.querySelector("#downloadPlaylist");
    const appState = document.querySelector("#appState");
    const submitButton = document.querySelector("#submitButton");
    const errorText = document.querySelector("#errorText");
    const message = document.querySelector("#message");
    const percent = document.querySelector("#percent");
    const bar = document.querySelector("#bar");
    const log = document.querySelector("#log");
    const result = document.querySelector("#result");
    const filename = document.querySelector("#filename");
    const downloadLink = document.querySelector("#downloadLink");
    const previewPanel = document.querySelector("#previewPanel");
    const previewLoading = document.querySelector("#previewLoading");
    const previewContent = document.querySelector("#previewContent");
    const previewThumb = document.querySelector("#previewThumb");
    const previewKind = document.querySelector("#previewKind");
    const previewTitle = document.querySelector("#previewTitle");
    const previewDetail = document.querySelector("#previewDetail");

    let pollTimer = null;
    let previewTimer = null;
    let previewController = null;
    let previewRequestId = 0;
    let playlistChoiceTouched = false;

    mediaFormatInputs.forEach((input) => input.addEventListener("change", syncFormatControls));
    urlInput.addEventListener("input", () => {
      syncPlaylistControls();
      schedulePreview();
    });
    downloadPlaylist.addEventListener("change", () => {
      playlistChoiceTouched = true;
    });
    syncFormatControls();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearError();
      resetResult();
      submitButton.disabled = true;
      appState.textContent = "Starting";
      message.textContent = "Creating job...";

      const payload = {
        url: form.url.value,
        media_format: currentMediaFormat(),
        quality: form.quality.value,
        keep_source_audio: currentMediaFormat() === "mp3" && form.keepSource.checked,
        deploy_to_server: form.deployToServer.checked,
        download_playlist: currentMediaFormat() === "mp3" && form.downloadPlaylist.checked
      };

      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Could not create job.");
        }
        renderJob(data);
        pollJob(data.id);
      } catch (error) {
        showError(error.message);
        submitButton.disabled = false;
        appState.textContent = "Ready";
        message.textContent = "Waiting for a link.";
      }
    });

    async function pollJob(jobId) {
      window.clearTimeout(pollTimer);
      try {
        const response = await fetch(`/api/jobs/${jobId}`);
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Job not found.");
        }
        renderJob(data);
        if (data.status === "running" || data.status === "queued") {
          pollTimer = window.setTimeout(() => pollJob(jobId), 900);
        } else {
          submitButton.disabled = false;
        }
      } catch (error) {
        showError(error.message);
        submitButton.disabled = false;
        appState.textContent = "Error";
      }
    }

    function renderJob(job) {
      appState.textContent = job.status.charAt(0).toUpperCase() + job.status.slice(1);
      message.textContent = job.error || job.message || "Working...";
      const value = Number(job.progress || 0);
      percent.textContent = `${Math.round(value)}%`;
      bar.style.width = `${Math.max(0, Math.min(100, value))}%`;
      log.textContent = (job.log || []).join("\\n");
      log.scrollTop = log.scrollHeight;

      if (job.status === "done" && job.download_url) {
        const label = job.download_label || (job.media_format || "file").toUpperCase();
        const fileCount = Number(job.files_count || 0);
        filename.textContent = fileCount > 1
          ? `${fileCount} files bundled as ${job.filename}`
          : job.filename || `${label} ready`;
        downloadLink.href = job.download_url;
        downloadLink.textContent = `Download ${label}`;
        downloadLink.hidden = false;
        result.hidden = false;
      } else if (job.status === "done" && job.server_result) {
        filename.textContent = job.server_result;
        downloadLink.removeAttribute("href");
        downloadLink.hidden = true;
        result.hidden = false;
      }

      if (job.status === "error") {
        showError(job.error || "Conversion failed.");
      }
    }

    function clearError() {
      errorText.textContent = "";
      errorText.hidden = true;
    }

    function showError(text) {
      errorText.textContent = text;
      errorText.hidden = false;
    }

    function resetResult() {
      window.clearTimeout(pollTimer);
      result.hidden = true;
      filename.textContent = "";
      downloadLink.removeAttribute("href");
      downloadLink.hidden = false;
      log.textContent = "";
      percent.textContent = "0%";
      bar.style.width = "0%";
    }

    function syncFormatControls() {
      const wantsMp3 = currentMediaFormat() === "mp3";
      qualityField.hidden = !wantsMp3;
      keepSourceRow.hidden = !wantsMp3;
      submitButton.textContent = wantsMp3 ? "Snag MP3" : "Snag MP4";
      syncPlaylistControls();
      schedulePreview();
    }

    function currentMediaFormat() {
      const selected = mediaFormatInputs.find((input) => input.checked);
      return selected ? selected.value : "mp3";
    }

    function syncPlaylistControls() {
      const showPlaylistOption = currentMediaFormat() === "mp3" && looksLikePlaylist(urlInput.value);
      playlistRow.hidden = !showPlaylistOption;
      if (showPlaylistOption && !playlistChoiceTouched) {
        downloadPlaylist.checked = true;
      }
      if (!showPlaylistOption) {
        downloadPlaylist.checked = false;
        playlistChoiceTouched = false;
      }
    }

    function schedulePreview() {
      window.clearTimeout(previewTimer);
      const value = urlInput.value.trim();
      if (!looksLikeYouTubeUrl(value)) {
        clearPreview();
        return;
      }

      previewPanel.hidden = false;
      previewContent.hidden = true;
      previewLoading.hidden = false;
      previewLoading.textContent = "Looking up URL...";
      previewTimer = window.setTimeout(() => fetchPreview(value), 650);
    }

    async function fetchPreview(value) {
      const requestId = ++previewRequestId;
      if (previewController) {
        previewController.abort();
      }
      previewController = new AbortController();

      try {
        const params = new URLSearchParams({url: value, media_format: currentMediaFormat()});
        const response = await fetch(`/api/preview?${params.toString()}`, {signal: previewController.signal});
        const data = await response.json();
        if (requestId !== previewRequestId) {
          return;
        }
        if (!response.ok) {
          throw new Error(data.error || "Could not preview this URL.");
        }
        renderPreview(data);
      } catch (error) {
        if (error.name === "AbortError" || requestId !== previewRequestId) {
          return;
        }
        previewPanel.hidden = false;
        previewContent.hidden = true;
        previewLoading.hidden = false;
        previewLoading.textContent = "Preview unavailable.";
      }
    }

    function renderPreview(preview) {
      previewPanel.hidden = false;
      previewLoading.hidden = true;
      previewContent.hidden = false;
      previewKind.textContent = preview.kind === "playlist" ? "Playlist" : "Video";
      previewTitle.textContent = preview.title || "Untitled";
      const parts = [];
      if (preview.channel) {
        parts.push(preview.channel);
      }
      if (preview.kind === "playlist" && preview.count) {
        parts.push(`${preview.count} videos`);
      }
      if (preview.kind === "video" && preview.duration) {
        parts.push(preview.duration);
      }
      previewDetail.textContent = parts.join(" / ");
      if (preview.thumbnail) {
        previewThumb.src = preview.thumbnail;
        previewThumb.hidden = false;
      } else {
        previewThumb.removeAttribute("src");
        previewThumb.hidden = true;
      }
    }

    function clearPreview() {
      window.clearTimeout(previewTimer);
      ++previewRequestId;
      if (previewController) {
        previewController.abort();
      }
      previewPanel.hidden = true;
      previewContent.hidden = true;
      previewLoading.hidden = false;
      previewLoading.textContent = "Looking up URL...";
      previewThumb.removeAttribute("src");
      previewTitle.textContent = "";
      previewDetail.textContent = "";
    }

    function looksLikePlaylist(value) {
      try {
        const parsed = new URL(value);
        const host = parsed.hostname.toLowerCase();
        const youtubeHost = [
          "youtube.com",
          "www.youtube.com",
          "m.youtube.com",
          "music.youtube.com",
          "youtube-nocookie.com",
          "www.youtube-nocookie.com",
          "youtu.be",
          "www.youtu.be"
        ].includes(host);
        const path = parsed.pathname.endsWith("/") ? parsed.pathname.slice(0, -1) : parsed.pathname;
        return youtubeHost && (parsed.searchParams.has("list") || path === "/playlist");
      } catch {
        return false;
      }
    }

    function looksLikeYouTubeUrl(value) {
      try {
        const parsed = new URL(value);
        return [
          "youtube.com",
          "www.youtube.com",
          "m.youtube.com",
          "music.youtube.com",
          "youtube-nocookie.com",
          "www.youtube-nocookie.com",
          "youtu.be",
          "www.youtu.be"
        ].includes(parsed.hostname.toLowerCase());
      } catch {
        return false;
      }
    }
  </script>
</body>
</html>
"""


def main() -> None:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
