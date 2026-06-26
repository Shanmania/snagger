from __future__ import annotations

import io
import queue
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from youtube_audio_extractor import core, web


def latest_progress(events: "queue.Queue[tuple[str, object]]") -> float | None:
    progress = None
    while not events.empty():
        event, value = events.get_nowait()
        if event == "progress":
            progress = float(value)
    return progress


class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        web.jobs.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "SNAGGER_OUTPUT_DIR": self.tempdir.name,
                "SNAGGER_USERNAME": "",
                "SNAGGER_PASSWORD": "",
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.client = web.create_app().test_client()

    def test_rejects_non_youtube_url(self) -> None:
        response = self.client.post("/api/jobs", json={"url": "https://example.com/video"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_index_renders_mp3_mp4_mode_switch(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('role="radiogroup"', html)
        self.assertIn('name="media_format" value="mp3"', html)
        self.assertIn('name="media_format" value="mp4"', html)
        self.assertIn('name="deployToServer"', html)
        self.assertIn('name="linkedVideoOnly"', html)
        self.assertIn('name="downloadPlaylist"', html)
        self.assertIn('href="/favicon.ico?v=3"', html)
        self.assertIn('class="brand-title"', html)
        self.assertIn('class="log-shell"', html)
        self.assertIn('id="logCount"', html)
        self.assertIn('class="version-badge"', html)
        self.assertIn("v0.2.2", html)
        self.assertIn("Updated 2026-06-26 01:06 CDT", html)
        self.assertIn('id="previewPanel"', html)
        self.assertIn("Keep original source audio</span>", html)

    def test_serves_favicon(self) -> None:
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/vnd.microsoft.icon")
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertGreater(len(response.data), 0)
        response.close()

    def test_preview_endpoint_returns_video_metadata(self) -> None:
        with patch.object(
            web,
            "extract_media_preview",
            return_value={
                "kind": "video",
                "title": "Example Video",
                "thumbnail": "https://img.youtube.com/vi/abc/hqdefault.jpg",
                "channel": "Example Channel",
                "duration": "3:21",
            },
        ) as fake_preview:
            response = self.client.get("/api/preview?url=https://www.youtube.com/watch?v=qp1kjzd7uug")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["kind"], "video")
        self.assertEqual(payload["title"], "Example Video")
        fake_preview.assert_called_once_with("https://www.youtube.com/watch?v=qp1kjzd7uug", allow_playlist=False)

    def test_preview_endpoint_detects_playlist_metadata(self) -> None:
        with patch.object(
            web,
            "extract_media_preview",
            return_value={
                "kind": "playlist",
                "title": "Example Playlist",
                "thumbnail": "https://img.youtube.com/vi/abc/hqdefault.jpg",
                "channel": "Example Channel",
                "count": 42,
            },
        ) as fake_preview:
            response = self.client.get("/api/preview?url=https://www.youtube.com/playlist?list=PL123")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["kind"], "playlist")
        self.assertEqual(payload["title"], "Example Playlist")
        fake_preview.assert_called_once_with("https://www.youtube.com/playlist?list=PL123", allow_playlist=True)

    def test_preview_endpoint_treats_playlist_linked_video_as_video_by_default(self) -> None:
        with patch.object(
            web,
            "extract_media_preview",
            return_value={
                "kind": "video",
                "title": "Linked Video",
                "thumbnail": "https://img.youtube.com/vi/abc/hqdefault.jpg",
                "channel": "Example Channel",
                "duration": "3:21",
            },
        ) as fake_preview:
            response = self.client.get(
                "/api/preview",
                query_string={"url": "https://www.youtube.com/watch?v=abc123&list=PL123"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["kind"], "video")
        fake_preview.assert_called_once_with("https://www.youtube.com/watch?v=abc123&list=PL123", allow_playlist=False)

    def test_preview_endpoint_can_force_full_playlist_for_linked_video(self) -> None:
        with patch.object(
            web,
            "extract_media_preview",
            return_value={
                "kind": "playlist",
                "title": "Example Playlist",
                "thumbnail": "https://img.youtube.com/vi/abc/hqdefault.jpg",
                "channel": "Example Channel",
                "count": 42,
            },
        ) as fake_preview:
            response = self.client.get(
                "/api/preview",
                query_string={
                    "url": "https://www.youtube.com/watch?v=abc123&list=PL123",
                    "download_playlist": "true",
                },
            )

        self.assertEqual(response.status_code, 200)
        fake_preview.assert_called_once_with("https://www.youtube.com/watch?v=abc123&list=PL123", allow_playlist=True)

    def test_rejects_unknown_media_format(self) -> None:
        response = self.client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=qp1kjzd7uug",
                "media_format": "avi",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_job_completes_and_serves_download(self) -> None:
        captured_output: dict[str, Path] = {}

        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp3")
            self.assertNotEqual(Path(settings.output_dir).resolve(), Path(self.tempdir.name).resolve())
            output_path = Path(settings.output_dir) / "sample.mp3"
            output_path.write_bytes(b"mp3")
            captured_output["path"] = output_path
            events.put(("status", "Done: sample.mp3"))
            events.put(("progress", 100.0))
            events.put(("log", "Saved MP3: sample.mp3"))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={"url": "https://www.youtube.com/watch?v=qp1kjzd7uug"},
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["filename"], "sample.mp3")
        self.assertFalse(payload["save_to_server"])

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data, b"mp3")
        download_response.close()
        self.assertFalse(captured_output["path"].exists())

    def test_mp3_playlist_bundles_separate_files_as_zip(self) -> None:
        captured_paths: list[Path] = []

        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp3")
            self.assertTrue(settings.allow_playlist)
            playlist_dir = Path(settings.output_dir) / "Test Playlist"
            playlist_dir.mkdir(parents=True)
            first_path = playlist_dir / "001 - First [abc].mp3"
            second_path = playlist_dir / "002 - Second [def].mp3"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            captured_paths.extend([first_path, second_path])
            events.put(("status", "Done: 2 MP3 files"))
            events.put(("progress", 100.0))
            events.put(("log", "Playlist mode: downloading each video as a separate MP3."))
            events.put(("done", [first_path, second_path]))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={"url": "https://www.youtube.com/playlist?list=PL123"},
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["files_count"], 2)
        self.assertEqual(payload["download_label"], "ZIP")
        self.assertEqual(payload["filename"], "Test Playlist.zip")
        self.assertTrue(payload["playlist_download"])

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download_response.data)) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["Test Playlist/001 - First [abc].mp3", "Test Playlist/002 - Second [def].mp3"],
            )
        download_response.close()
        self.assertFalse(captured_paths[0].exists())

    def test_linked_video_playlist_defaults_to_single_video_download(self) -> None:
        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp3")
            self.assertFalse(settings.allow_playlist)
            output_path = Path(settings.output_dir) / "linked-video.mp3"
            output_path.write_bytes(b"mp3")
            events.put(("status", "Done: linked-video.mp3"))
            events.put(("progress", 100.0))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={"url": "https://www.youtube.com/watch?v=abc123&list=PL123"},
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertFalse(payload["playlist_download"])
        self.assertEqual(payload["filename"], "linked-video.mp3")

    def test_linked_video_playlist_can_request_full_playlist(self) -> None:
        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp3")
            self.assertTrue(settings.allow_playlist)
            playlist_dir = Path(settings.output_dir) / "Linked Playlist"
            playlist_dir.mkdir(parents=True)
            first_path = playlist_dir / "001 - First [abc].mp3"
            second_path = playlist_dir / "002 - Second [def].mp3"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            events.put(("status", "Done: 2 MP3 files"))
            events.put(("progress", 100.0))
            events.put(("done", [first_path, second_path]))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=abc123&list=PL123",
                    "linked_video_only": False,
                    "download_playlist": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertTrue(payload["playlist_download"])
        self.assertEqual(payload["download_label"], "ZIP")

    def test_linked_video_only_overrides_playlist_flag(self) -> None:
        def fake_download(settings, events):
            self.assertFalse(settings.allow_playlist)
            output_path = Path(settings.output_dir) / "linked-video.mp3"
            output_path.write_bytes(b"mp3")
            events.put(("progress", 100.0))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=abc123&list=PL123",
                    "linked_video_only": True,
                    "download_playlist": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertFalse(payload["playlist_download"])

    def test_linked_video_only_does_not_disable_pure_playlist_url(self) -> None:
        def fake_download(settings, events):
            self.assertTrue(settings.allow_playlist)
            playlist_dir = Path(settings.output_dir) / "Pure Playlist"
            playlist_dir.mkdir(parents=True)
            output_path = playlist_dir / "001 - First [abc].mp3"
            output_path.write_bytes(b"mp3")
            events.put(("progress", 100.0))
            events.put(("done", [output_path]))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/playlist?list=PL123",
                    "linked_video_only": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertTrue(payload["playlist_download"])

    def test_mp4_playlist_url_does_not_enable_playlist_download(self) -> None:
        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp4")
            self.assertFalse(settings.allow_playlist)
            output_path = Path(settings.output_dir) / "sample.mp4"
            output_path.write_bytes(b"mp4")
            events.put(("status", "Done: sample.mp4"))
            events.put(("progress", 100.0))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=qp1kjzd7uug&list=PL123",
                    "media_format": "mp4",
                    "download_playlist": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertFalse(payload["playlist_download"])

    def test_server_deploy_playlist_saves_folder_and_temp_browser_zip(self) -> None:
        captured_paths: list[Path] = []

        def fake_download(settings, events):
            self.assertEqual(Path(settings.output_dir).resolve(), Path(self.tempdir.name).resolve())
            self.assertTrue(settings.allow_playlist)
            playlist_dir = Path(settings.output_dir) / "Server Playlist"
            playlist_dir.mkdir(parents=True)
            first_path = playlist_dir / "001 - First [abc].mp3"
            second_path = playlist_dir / "002 - Second [def].mp3"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            captured_paths.extend([first_path, second_path])
            events.put(("status", "Done: 2 MP3 files"))
            events.put(("progress", 100.0))
            events.put(("done", [first_path, second_path]))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/playlist?list=PL123",
                    "deploy_to_server": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["files_count"], 2)
        self.assertEqual(payload["server_folder"], "Server Playlist")
        self.assertEqual(payload["message"], "Saved 2 files to server folder: Server Playlist")
        self.assertEqual(payload["server_result"], "Saved 2 files to server folder: Server Playlist")
        self.assertEqual(payload["download_label"], "ZIP")
        self.assertEqual(payload["filename"], "Server Playlist.zip")
        self.assertIn("download_url", payload)
        self.assertFalse((Path(self.tempdir.name) / "Server Playlist.zip").exists())
        self.assertTrue(captured_paths[0].exists())
        self.assertTrue(captured_paths[1].exists())

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download_response.data)) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["Server Playlist/001 - First [abc].mp3", "Server Playlist/002 - Second [def].mp3"],
            )
        download_response.close()
        self.assertTrue(captured_paths[0].exists())
        self.assertTrue(captured_paths[1].exists())

    def test_playlist_progress_aggregates_across_items(self) -> None:
        events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        settings = core.DownloadSettings(
            url="https://www.youtube.com/playlist?list=PL123",
            output_dir=Path(self.tempdir.name),
            quality_label="Maximum VBR quality",
            quality_value="0",
            keep_source_audio=False,
            output_format="mp3",
            allow_playlist=True,
        )

        with patch.object(core, "resolve_ffmpeg", return_value="/usr/bin/ffmpeg"):
            options = core.build_ydl_options(settings, events)

        progress_hook = options["progress_hooks"][0]
        postprocessor_hook = options["postprocessor_hooks"][0]

        postprocessor_hook(
            {
                "status": "finished",
                "postprocessor": "FFmpeg",
                "info_dict": {"playlist_index": 1, "playlist_count": 2},
            }
        )
        first_item_done = latest_progress(events)

        progress_hook(
            {
                "status": "downloading",
                "downloaded_bytes": 0,
                "total_bytes": 100,
                "_percent_str": "0%",
                "info_dict": {"playlist_index": 2, "playlist_count": 2},
            }
        )
        second_item_started = latest_progress(events)

        self.assertIsNotNone(first_item_done)
        self.assertIsNotNone(second_item_started)
        self.assertGreater(second_item_started, first_item_done)
        self.assertLess(second_item_started, 100.0)

    def test_mp4_options_prefer_h264_aac_for_premiere(self) -> None:
        events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        settings = core.DownloadSettings(
            url="https://www.youtube.com/watch?v=qp1kjzd7uug",
            output_dir=Path(self.tempdir.name),
            quality_label="Maximum VBR quality",
            quality_value="0",
            keep_source_audio=False,
            output_format="mp4",
            allow_playlist=False,
        )

        with patch.object(core, "resolve_ffmpeg", return_value="/usr/bin/ffmpeg"):
            options = core.build_ydl_options(settings, events)

        self.assertEqual(options["format"], core.PREMIERE_SAFE_MP4_FORMAT)
        self.assertIn("[vcodec^=avc1]", options["format"])
        self.assertIn("[acodec^=mp4a]", options["format"])
        self.assertNotIn("bestvideo+bestaudio/best", options["format"])
        self.assertEqual(options["merge_output_format"], "mp4")

    def test_mp4_transcoder_forces_premiere_safe_codecs(self) -> None:
        source_path = Path(self.tempdir.name) / "sample.mp4"
        source_path.write_bytes(b"original")
        captured_command: list[str] = []

        def fake_run(command, **kwargs):
            captured_command.extend(command)
            Path(command[-1]).write_bytes(b"normalized")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(core.subprocess, "run", side_effect=fake_run):
            core.transcode_mp4_for_premiere(source_path, "/usr/bin/ffmpeg")

        self.assertEqual(source_path.read_bytes(), b"normalized")
        self.assertIn("-map", captured_command)
        self.assertIn("0:v:0", captured_command)
        self.assertIn("0:a:0", captured_command)
        self.assertNotIn("0:a:0?", captured_command)
        self.assertIn("-c:v", captured_command)
        self.assertEqual(captured_command[captured_command.index("-c:v") + 1], "libx264")
        self.assertIn("-pix_fmt", captured_command)
        self.assertEqual(captured_command[captured_command.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("-tag:v", captured_command)
        self.assertEqual(captured_command[captured_command.index("-tag:v") + 1], "avc1")
        self.assertIn("-c:a", captured_command)
        self.assertEqual(captured_command[captured_command.index("-c:a") + 1], "aac")
        self.assertIn("-profile:a", captured_command)
        self.assertEqual(captured_command[captured_command.index("-profile:a") + 1], "aac_low")
        self.assertIn("-ar", captured_command)
        self.assertEqual(captured_command[captured_command.index("-ar") + 1], "48000")
        self.assertIn("-ac", captured_command)
        self.assertEqual(captured_command[captured_command.index("-ac") + 1], "2")
        self.assertIn("-tag:a", captured_command)
        self.assertEqual(captured_command[captured_command.index("-tag:a") + 1], "mp4a")
        self.assertIn("+faststart", captured_command)

    def test_server_deploy_uses_configured_output_dir_and_persists(self) -> None:
        captured_output: dict[str, Path] = {}

        def fake_download(settings, events):
            self.assertEqual(Path(settings.output_dir).resolve(), Path(self.tempdir.name).resolve())
            output_path = Path(settings.output_dir) / "server-sample.mp3"
            output_path.write_bytes(b"mp3")
            captured_output["path"] = output_path
            events.put(("status", "Done: server-sample.mp3"))
            events.put(("progress", 100.0))
            events.put(("log", "Saved MP3: server-sample.mp3"))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=qp1kjzd7uug",
                    "deploy_to_server": True,
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertTrue(payload["save_to_server"])

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data, b"mp3")
        download_response.close()
        self.assertTrue(captured_output["path"].exists())

    def test_mp4_job_completes_and_serves_download(self) -> None:
        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp4")
            output_path = Path(settings.output_dir) / "sample.mp4"
            output_path.write_bytes(b"mp4")
            events.put(("status", "Done: sample.mp4"))
            events.put(("progress", 100.0))
            events.put(("log", "Saved MP4: sample.mp4"))
            events.put(("done", output_path))

        with patch.object(web, "download_media", side_effect=fake_download):
            create_response = self.client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=qp1kjzd7uug",
                    "media_format": "mp4",
                },
            )

        self.assertEqual(create_response.status_code, 202)
        job_id = create_response.get_json()["id"]

        payload = None
        for _ in range(20):
            poll_response = self.client.get(f"/api/jobs/{job_id}")
            payload = poll_response.get_json()
            if payload["status"] == "done":
                break
            time.sleep(0.05)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["media_format"], "mp4")
        self.assertEqual(payload["filename"], "sample.mp4")

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data, b"mp4")
        download_response.close()

    def test_basic_auth_when_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SNAGGER_OUTPUT_DIR": self.tempdir.name,
                "SNAGGER_USERNAME": "user",
                "SNAGGER_PASSWORD": "pass",
            },
        ):
            client = web.create_app().test_client()

            self.assertEqual(client.get("/").status_code, 401)
            self.assertEqual(client.get("/", auth=("user", "pass")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
