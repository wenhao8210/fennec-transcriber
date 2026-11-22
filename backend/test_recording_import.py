"""隔离自测：只使用临时目录/数据库/合成音频，不启动正式服务或加载模型。

运行：venv/bin/python -m unittest discover -s backend -p 'test_recording_import.py' -v
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

# 必须在任何后端模块导入之前隔离配置；TestClient 不进入上下文，因此不执行 startup。
_sandbox = tempfile.TemporaryDirectory(prefix="fennec-recorder-test-")
os.environ["FENNEC_APP_DATA_DIR"] = str(Path(_sandbox.name) / "app-data")
os.environ["FENNEC_RECORDINGS_DIR"] = str(Path(_sandbox.name) / "recordings")
os.environ["FENNEC_DATA_DIR"] = str(Path(_sandbox.name) / "third-party")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
import asr
import config
import main
import recording_import
import store

assert config.DATA_DIR.is_relative_to(Path(_sandbox.name)), "tests require isolated module imports"


class RecordingImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_sandbox.name)
        self.root = Path(self.tmp.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.source = self.recordings / "rec-example.m4a"
        self.source.write_bytes(b"test audio placeholder")
        self.patches = [
            patch.object(store, "DB_PATH", self.root / "sessions.db"),
            patch.object(recording_import, "SELF_RECORDINGS_DIR", self.recordings),
            patch.object(main, "SELF_RECORDINGS_DIR", self.recordings),
            patch.object(asr, "WAV_CACHE", self.root / "cache"),
        ]
        for p in self.patches:
            p.start()
        asr.WAV_CACHE.mkdir()
        store.init_db()
        now = datetime.now(timezone.utc)
        self.payload = {
            "path": str(self.source), "capture_id": "capture-1",
            "started_at": (now - timedelta(seconds=10)).isoformat(),
            "ended_at": (now - timedelta(seconds=9)).isoformat(),
        }
        self.probe = Mock(returncode=0, stderr="", stdout=json.dumps({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"},
            "streams": [{"codec_type": "audio", "nb_read_packets": "48"}],
        }))
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8778")

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, **changes):
        with patch.object(recording_import.subprocess, "run", return_value=self.probe):
            return self.client.post("/api/recordings/import", json={**self.payload, **changes})

    def test_import_preserves_metadata_without_models_or_cloud(self):
        with patch.object(main, "_launch_transcribe") as launch, \
             patch.object(main, "analyze_energy") as energy, \
             patch.object(main, "analyze_speech_ratio") as vad, \
             patch.object(main, "transcribe_cloud") as cloud, \
             patch.object(main, "transcribe_tinet") as tinet:
            response = self.request(interruption_reason="sleep")
            self.assertEqual(response.status_code, 200, response.text)
            row = response.json()
            self.assertEqual(row["source"], "self_recorder")
            self.assertEqual(row["duration"], 1.0)
            self.assertEqual(row["capture_id"], "capture-1")
            self.assertEqual(row["interruption_reason"], "sleep")
            expected = datetime.fromisoformat(self.payload["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
            self.assertEqual(row["created_at"], expected)
            self.assertIsNone(row["speech_ratio"])
            self.assertFalse(row["auto_transcribe_started"])
            for mocked in (launch, energy, vad, cloud, tinet):
                mocked.assert_not_called()

    def test_health_counts_in_memory_jobs_without_scanning_or_database(self):
        with patch.dict(main._job_progress, {"queued": {"phase": "queued"},
                                            "running": {"phase": "transcribing"}}, clear=True), \
             patch.object(main, "_ensure_scan") as scan, \
             patch.object(store, "_conn") as database:
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok", "service": "fennec-transcriber", "active_transcriptions": 2,
            "active_recording_jobs": 0,
        })
        scan.assert_not_called()
        database.assert_not_called()

    def test_auto_transcribe_is_explicit_local_once(self):
        with patch.object(main, "_launch_transcribe", return_value=True) as launch:
            first = self.request(auto_transcribe=True).json()
            second = self.request(auto_transcribe=True).json()
            self.assertTrue(first["auto_transcribe_started"])
            self.assertFalse(second["imported"])
            self.assertEqual(first["id"], second["id"])
            launch.assert_called_once_with(first["id"], "local")

    def test_concurrent_import_is_idempotent(self):
        body = recording_import.RecordingImportIn(**self.payload)
        with patch.object(recording_import.subprocess, "run", return_value=self.probe):
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda _: main.import_self_recording(body), range(20)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        self.assertEqual(sum(row["imported"] for row in rows), 1)
        self.assertEqual(len(store.list_sessions()), 1)

    def test_normalized_path_is_idempotent(self):
        first = self.request().json()
        alias = self.recordings / "alias.m4a"
        alias.symlink_to(self.source)
        second = self.request(path=str(alias)).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["file_path"], str(self.source.resolve()))

    def test_capture_and_path_cannot_be_reassigned(self):
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.request(capture_id="capture-2").status_code, 409)
        other = self.recordings / "other.m4a"
        other.write_bytes(b"other audio")
        self.assertEqual(self.request(path=str(other)).status_code, 409)

    def test_rejects_outside_and_symlink_escape(self):
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"outside")
        alias = self.recordings / "escape.m4a"
        alias.symlink_to(outside)
        for path in (outside, alias, self.recordings / ".." / "outside.m4a"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=str(path)).status_code, 400)
        self.assertEqual(store.list_sessions(), [])

    def test_rejects_empty_temporary_missing_and_wrong_extension(self):
        for name, data in (("empty.m4a", b""), ("rec.partial.m4a", b"x"),
                           ("rec.encoding-123.m4a", b"x"),
                           ("rec.m4a.tmp", b"x"), (".hidden.m4a", b"x"), ("rec.wav", b"x")):
            path = self.recordings / name
            path.write_bytes(data)
            with self.subTest(name=name):
                self.assertEqual(self.request(path=str(path)).status_code, 400)
        self.assertEqual(self.request(path=str(self.recordings / "missing.m4a")).status_code, 400)

    def test_rejects_failed_probe_and_no_audio(self):
        for probe in (Mock(returncode=1, stderr="bad", stdout="{}"),
                      Mock(returncode=0, stderr="", stdout='{"format":{"duration":"1"},"streams":[]}')):
            self.probe = probe
            self.assertEqual(self.request().status_code, 400)

    def test_rejects_invalid_times_and_loose_boolean(self):
        for change in ({"ended_at": self.payload["started_at"]},
                       {"started_at": "2026-01-01T00:00:00"},
                       {"ended_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
                       {"started_at": "not-a-date"}):
            with self.subTest(change=change):
                self.assertEqual(self.request(**change).status_code, 400)
        self.assertEqual(self.request(auto_transcribe="false").status_code, 422)
        self.assertEqual(self.request(engine="aliyun").status_code, 422)

    def test_rejects_file_changed_during_probe(self):
        def mutate(*args, **kwargs):
            self.source.write_bytes(b"still writing a changed file")
            return self.probe
        with patch.object(recording_import.subprocess, "run", side_effect=mutate):
            response = self.client.post("/api/recordings/import", json=self.payload)
        self.assertEqual(response.status_code, 400)

    def test_legacy_marker_cannot_auto_transcribe_self_recording(self):
        self.request()
        with patch.object(main, "_load_markers", return_value=[{"id": "m", "ts": int(datetime.now().timestamp())}]), \
             patch.object(main, "_ensure_scan"), patch.object(main, "_launch_transcribe") as launch:
            main._markers_auto_transcribe()
        launch.assert_not_called()

    def test_list_refresh_analysis_skips_native_but_keeps_legacy(self):
        self.request()
        legacy = self.recordings / "legacy.m4a"
        legacy.write_bytes(b"legacy fixture")
        store.upsert_session("legacy", "legacy", str(legacy), "recordings")
        # 同步运行任务以断言实际分析对象，而非仅检查入队参数。
        def inline_thread(*, target, **kwargs):
            return Mock(start=target)
        with patch.object(main.threading, "Thread", side_effect=inline_thread), \
             patch.object(main, "analyze_speech_ratio", return_value=0.5) as vad, \
             patch.object(main, "analyze_energy", return_value=-20) as energy:
            response = self.client.post("/api/analyze-energy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        vad.assert_called_once_with(legacy.resolve())
        energy.assert_called_once_with(legacy.resolve())
        native = next(s for s in store.list_sessions() if s["source"] == "self_recorder")
        self.assertIsNone(native["speech_ratio"])
        self.assertIsNone(native["energy_db"])

    def test_default_native_import_is_not_auto_archived(self):
        self.request()
        with patch.object(main, "_archive_session") as archive:
            result = main._archive_protected()
        archive.assert_not_called()
        self.assertEqual(result["total"], 0)

    def test_old_database_migration_preserves_content(self):
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    file_path TEXT NOT NULL, source TEXT NOT NULL, duration REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending', error TEXT, speaker_ref TEXT,
                    created_at TEXT DEFAULT (datetime('now')));
                INSERT INTO sessions VALUES ('old', 'old name', '/old/file.m4a', 'sessions',
                    42, 'done', NULL, NULL, '2026-01-01 01:02:03');
                CREATE TABLE segments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                    speaker TEXT, text TEXT);
                INSERT INTO segments VALUES (7, 'old', 0, 1, 'self', '保留旧转写');
            """)
        with patch.object(store, "DB_PATH", legacy):
            store.init_db()
            store.init_db()
            row = store.get_session("old")
            self.assertEqual(row["created_at"], "2026-01-01 01:02:03")
            self.assertEqual(row["duration"], 42)
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["segments"][0]["id"], 7)
            self.assertEqual(row["segments"][0]["text"], "保留旧转写")
            self.assertIsNone(row["capture_id"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_synthetic_m4a_and_truncated_file(self):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.5", "-c:a", "aac", "-y", str(self.source)],
                       check=True, capture_output=True)
        response = self.client.post("/api/recordings/import", json=self.payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertAlmostEqual(response.json()["duration"], 0.5, delta=0.1)
        broken = self.recordings / "broken.m4a"
        broken.write_bytes(self.source.read_bytes()[:100])
        response = self.client.post("/api/recordings/import", json={
            **self.payload, "path": str(broken), "capture_id": "broken",
        })
        self.assertEqual(response.status_code, 400)

    def test_wav_cache_separates_path_and_file_versions(self):
        other_dir = self.root / "other"
        other_dir.mkdir()
        other = other_dir / self.source.name
        other.write_bytes(b"different audio")
        def convert(command, **kwargs):
            Path(command[-1]).write_bytes(b"synthetic wav")
        with patch.object(asr.subprocess, "run", side_effect=convert) as ffmpeg:
            first = asr.to_wav_16k(self.source)
            self.assertEqual(first, asr.to_wav_16k(self.source))
            second = asr.to_wav_16k(other)
            self.source.write_bytes(b"replaced version of audio")
            third = asr.to_wav_16k(self.source)
        self.assertEqual(len({first, second, third}), 3)
        self.assertTrue(first.exists())
        self.assertEqual(ffmpeg.call_count, 3)


if __name__ == "__main__":
    unittest.main()
