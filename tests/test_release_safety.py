"""Release smoke tests: temporary data only; no cloud calls or real recordings."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="fennec-release-test-")
BASE = Path(TEMP.name)
os.environ["FENNEC_DATA_DIR"] = str(BASE / "source")
os.environ["FENNEC_STORAGE_DIR"] = str(BASE / "runtime")
for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "DASHSCOPE_WORKSPACE",
             "ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET"):
    os.environ.pop(name, None)
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient
import config
import main
import store
import aliyun_asr

for directory in (config.SESSIONS_DIR, config.RECORDINGS_DIR):
    directory.mkdir(parents=True, exist_ok=True)
store.init_db()
CLIENT = TestClient(main.app, base_url="http://127.0.0.1:8778")


class ReleaseSafetyTests(unittest.TestCase):
    def test_page_loads(self):
        response = CLIENT.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fennec", response.text)

    def test_settings_do_not_return_api_key(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-only-not-a-real-credential"}):
            response = CLIENT.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dashscope_configured"])
        self.assertNotIn("test-only-not-a-real-credential", response.text)
        self.assertNotIn("api_key", response.json())

    def test_untrusted_origins_blocked(self):
        for origin in ("https://untrusted.example", "null", "http://127.0.0.1:9999"):
            with self.subTest(origin=origin):
                self.assertEqual(CLIENT.get("/api/settings", headers={"Origin": origin}).status_code, 403)
                self.assertEqual(CLIENT.post("/api/archive/run", headers={"Origin": origin}).status_code, 403)

    def test_local_origin_accepted(self):
        self.assertEqual(CLIENT.get("/api/settings", headers={"Origin": "http://127.0.0.1:8778"}).status_code, 200)

    def test_rebinding_host_blocked(self):
        self.assertEqual(CLIENT.get("/api/settings", headers={"Host": "untrusted.example"}).status_code, 400)

    def test_arbitrary_file_import_blocked(self):
        outside = BASE / "private.txt"
        outside.write_text("not real private data")
        self.assertEqual(CLIENT.post("/api/import", json={"path": str(outside)}).status_code, 403)

    def test_non_audio_file_in_source_blocked(self):
        inside = config.SESSIONS_DIR / "settings.json"
        inside.write_text("{}")
        self.assertEqual(CLIENT.post("/api/import", json={"path": str(inside)}).status_code, 400)

    def test_supported_audio_import_and_read(self):
        audio = config.SESSIONS_DIR / "synthetic.m4a"
        audio.write_bytes(b"synthetic audio fixture, no actual speech")
        response = CLIENT.post("/api/import", json={"path": str(audio)})
        self.assertEqual(response.status_code, 200)
        sid = response.json()["id"]
        self.assertEqual(CLIENT.get(f"/api/sessions/{sid}/audio").content, audio.read_bytes())

    def test_symlink_escape_blocked(self):
        outside = BASE / "outside.m4a"
        outside.write_bytes(b"not real speech")
        link = config.SESSIONS_DIR / "escape.m4a"
        link.symlink_to(outside)
        self.assertEqual(CLIENT.post("/api/import", json={"path": str(link)}).status_code, 403)

    def test_audio_route_rejects_out_of_scope_record(self):
        outside = BASE / "outside-audio.m4a"
        outside.write_bytes(b"not real speech")
        sid = store.upsert_session("outside-test", "outside", str(outside), "sessions")
        self.assertEqual(CLIENT.get(f"/api/sessions/{sid}/audio").status_code, 410)

    def test_new_settings_file_is_private(self):
        aliyun_asr.save_settings({"dashscope_api_key": "test-only-not-a-real-credential"})
        self.assertEqual(stat.S_IMODE(config.SETTINGS_PATH.stat().st_mode), 0o600)
        config.SETTINGS_PATH.unlink()

    def test_missing_model_has_actionable_error(self):
        from asr import Transcriber
        with patch("asr.WHISPER_MODEL", "missing-release-test-model"):
            with self.assertRaisesRegex(RuntimeError, "README"):
                Transcriber()._load()

    def test_rename_favorite_and_archive_fallback(self):
        audio = config.SESSIONS_DIR / "rename-fixture.m4a"
        audio.write_bytes(b"synthetic archived content")
        sid = CLIENT.post("/api/import", json={"path": str(audio)}).json()["id"]
        renamed = CLIENT.post(f"/api/sessions/{sid}/rename", json={"name": "renamed-fixture"})
        self.assertEqual(renamed.status_code, 200)
        favorite = CLIENT.post(f"/api/sessions/{sid}/favorite", json={"favorite": True})
        self.assertTrue(favorite.json()["archived"])
        archived = Path(store.get_session(sid)["archived_path"])
        self.assertEqual(archived.suffix, ".m4a")
        Path(renamed.json()["file_path"]).unlink()
        response = CLIENT.get(f"/api/sessions/{sid}/audio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"synthetic archived content")

    def test_daily_helper_defaults_to_local(self):
        text = (ROOT / "backend/fennec_transcribe_today.py").read_text()
        self.assertIn("default='local'", text)


if __name__ == "__main__":
    unittest.main()
