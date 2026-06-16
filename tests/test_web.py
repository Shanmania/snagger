from __future__ import annotations

import tempfile
import time
import unittest
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
        def fake_download(settings, events):
            self.assertEqual(settings.output_format, "mp3")
            output_path = Path(settings.output_dir) / "sample.mp3"
            output_path.write_bytes(b"mp3")
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

        download_response = self.client.get(payload["download_url"])
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data, b"mp3")
        download_response.close()

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
