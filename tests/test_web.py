from __future__ import annotations

import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from youtube_audio_extractor import web


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
        self.assertIn('name="downloadPlaylist"', html)
        self.assertIn('href="/favicon.ico"', html)
        self.assertIn('id="previewPanel"', html)

    def test_serves_favicon(self) -> None:
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/vnd.microsoft.icon")
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
