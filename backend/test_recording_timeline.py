"""时间轴/切片隔离测试：仅临时 DB 和人工正弦波，不启动或查询正式服务。"""
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# 复用先于后端模块导入的隔离环境；不启动 FastAPI lifespan。
import test_recording_import as isolated
import recording_timeline
import store
import main
import numpy as np
from fastapi.testclient import TestClient


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class RecordingTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=isolated._sandbox.name)
        self.root = Path(self.tmp.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.service = recording_timeline.TimelineService(self.root / "cache", self.recordings)
        self.patches = [patch.object(main, "SELF_RECORDINGS_DIR", self.recordings),
                        patch.object(main, "ARCHIVE_DIR", self.root),
                        patch.object(store, "DB_PATH", self.root / "test.db"),
                        patch.object(recording_timeline, "service", self.service)]
        for item in self.patches:
            item.start()
        store.init_db()
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8778")
        self.start = round((time.time() - 60) * 1000)

    def tearDown(self):
        self.service.close()
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def add_tone(self, identifier, start, seconds=1.0, frequency=440, source="self_recorder"):
        audio = self.recordings / f"{identifier}.m4a"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration={seconds}:sample_rate=16000",
                        "-c:a", "aac", str(audio)], check=True, capture_output=True)
        began = datetime.fromtimestamp(start / 1000, timezone.utc)
        sid, _ = store.import_recording(name=audio.name, file_path=str(audio), capture_id=identifier,
                                        started_at=began.isoformat(),
                                        ended_at=(began + timedelta(seconds=seconds)).isoformat(),
                                        created_at=began.strftime("%Y-%m-%d %H:%M:%S"),
                                        interruption_reason=None, duration=seconds, source=source)
        return sid, audio

    def wait_job(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/recordings/clip/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.json()
            if job["status"] in {"done", "error"}:
                self.assertEqual(job["status"], "done", job)
                return job
            time.sleep(0.02)
        self.fail("clip job timeout")

    def test_two_sources_with_gap_real_audio_clip_and_no_original_changes(self):
        first, path1 = self.add_tone("first", self.start)
        second, path2 = self.add_tone("second", self.start + 2000, frequency=660)
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path1, path2)]
        with patch.object(main, "_launch_transcribe") as transcribe, \
             patch.object(main, "analyze_speech_ratio") as vad:
            response = self.client.post("/api/recordings/clip", json={
                "start": self.start, "end": self.start + 3000, "name": "测试片段",
            })
            self.assertEqual(response.status_code, 202, response.text)
            job = self.wait_job(response.json()["job_id"])
        transcribe.assert_not_called()
        vad.assert_not_called()
        row = store.get_session(job["result"]["id"])
        self.assertEqual(row["source"], "self_clip")
        self.assertEqual(row["name"], "测试片段")
        self.assertEqual(row["segments"], [])
        self.assertEqual(row["status"], "pending")
        self.assertAlmostEqual(row["duration"], 3, delta=0.1)
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", row["file_path"],
                                 "-ar", "16000", "-ac", "1", "-f", "f32le", "-"],
                                capture_output=True, check=True)
        samples = np.frombuffer(result.stdout, dtype=np.float32)
        rms = lambda a, b: float(np.sqrt(np.mean(samples[int(a * 16000):int(b * 16000)] ** 2)))
        self.assertGreater(rms(.2, .7), .03)
        self.assertLess(rms(1.2, 1.8), .001)
        self.assertGreater(rms(2.2, 2.7), .03)
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path1, path2)])
        self.assertEqual({s["id"] for s in store.list_sessions()}, {first, second, row["id"]})
        timeline = self.client.get("/api/recordings/timeline").json()
        self.assertNotIn(row["id"], {s["id"] for s in timeline["segments"]})

    def test_preview_does_not_create_session_and_has_playable_url(self):
        sid, _ = self.add_tone("preview-source", self.start, seconds=2)
        result = self.client.post("/api/recordings/clip", json={
            "start": self.start + 200, "end": self.start + 1500, "preview": True,
        })
        job = self.wait_job(result.json()["job_id"])
        self.assertEqual([s["id"] for s in store.list_sessions()], [sid])
        url = job["result"]["preview_url"]
        self.assertTrue(url.startswith("/api/recordings/clip/"))
        audio = self.client.get(url)
        self.assertEqual(audio.status_code, 200)
        self.assertGreater(len(audio.content), 100)
        self.assertEqual(list(self.recordings.glob("clip_*.m4a")), [])

    def test_double_submit_and_restart_lookup_reuses_same_session(self):
        self.add_tone("dedup", self.start, seconds=2)
        body = recording_timeline.ClipIn(start=self.start, end=self.start + 1500)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.service.submit(body, main._resolve_audio), range(12)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        done = self.wait_job(jobs[0]["job_id"])
        recovered = recording_timeline.TimelineService(self.root / "cache", self.recordings)
        try:
            result = recovered.submit(body, main._resolve_audio)
            self.assertEqual(result["result"]["id"], done["result"]["id"])
            self.assertEqual(len(store.list_sessions()), 2)
        finally:
            recovered.close()

    def test_timeline_uses_archived_audio_and_async_cached_peaks(self):
        sid, original = self.add_tone("archived", self.start)
        archived = self.root / "preserved.m4a"
        original.replace(archived)  # 只操作临时合成测试音频。
        store.set_archived_path(sid, str(archived))
        with patch.object(store, "get_session", side_effect=AssertionError("must not load transcripts")):
            first = self.client.get("/api/recordings/timeline?hours=3").json()
            self.assertEqual(first["segments"][0]["waveform_status"], "pending")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                latest = self.client.get("/api/recordings/timeline?hours=3").json()
                row = latest["segments"][0]
                if row["waveform_status"] != "pending":
                    break
                time.sleep(.02)
            self.assertEqual(row["waveform_status"], "ready", row)
            self.assertTrue(row["peaks"])
            self.assertLessEqual(len(row["peaks"]), 322)
            self.assertTrue(all(0 <= peak <= 1 for peak in row["peaks"]))
            with patch.object(recording_timeline.subprocess, "run", side_effect=AssertionError("cache should not decode")):
                self.assertEqual(self.client.get("/api/recordings/timeline").json()["segments"][0]["waveform_status"], "ready")

    def test_legacy_name_time_used_instead_of_created_at(self):
        event = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
        name = event.strftime("rec_%Y-%m-%d_%H-%M-%S.m4a")
        source = self.recordings / name
        source.write_bytes(b"metadata only")
        store.upsert_session("legacy", name, str(source), "recordings")
        store.update_duration("legacy", 10)
        with patch.object(self.service, "_waveform", return_value={"peaks": [], "waveform_status": "pending"}):
            data = self.client.get("/api/recordings/timeline?hours=1").json()
        self.assertAlmostEqual(data["segments"][0]["start"], event.timestamp() * 1000, delta=1)
        # 无可确认事件时间的名称，即使刚入库，也不伪装成最近录音。
        store.upsert_session("undated", "unknown.m4a", str(self.recordings / "unknown.m4a"), "recordings")
        store.update_duration("undated", 10)
        rows, _ = self.service._candidates(self.start - 3600000, self.start + 60000, main._resolve_audio)
        self.assertNotIn("undated", {r["id"] for r in rows})

    def test_rejects_live_edges_empty_selection_invalid_range_and_arbitrary_paths(self):
        self.add_tone("bounds", self.start)
        for payload, code in (({"start": self.start, "end": self.start}, 400),
                              ({"start": self.start, "end": self.start + 3 * 3600000 + 1}, 400),
                              ({"start": self.start - 500, "end": self.start + 500}, 409),
                              ({"start": self.start, "end": self.start + 2000}, 409),
                              ({"start": self.start + 2000, "end": self.start + 3000}, 409),
                              ({"start": self.start, "end": self.start + 500, "path": "/etc/passwd"}, 422)):
            with self.subTest(payload=payload):
                response = self.client.post("/api/recordings/clip", json=payload)
                self.assertEqual(response.status_code, code, response.text)
        self.assertEqual(self.client.get("/api/recordings/timeline?hours=2").status_code, 400)
        self.assertEqual(self.client.get("/api/recordings/clip/not-a-job/audio").status_code, 404)

    def test_preview_cleanup_only_removes_owned_preview_files(self):
        previews = self.root / "cache/previews"
        previews.mkdir(parents=True)
        protected = self.recordings / "do-not-delete.m4a"
        protected.write_bytes(b"original")
        unrelated = previews / "unrelated.m4a"
        unrelated.write_bytes(b"keep")
        for index in range(12):
            (previews / f"preview_{index:032x}.m4a").write_bytes(b"generated")
        self.service._prune_previews()
        self.assertEqual(len(list(previews.glob("preview_*.m4a"))), 10)
        self.assertTrue(protected.is_file())
        self.assertTrue(unrelated.is_file())

    def test_missing_audio_is_not_silently_replaced_with_gap(self):
        self.add_tone("available-first", self.start)
        _, missing = self.add_tone("missing-middle", self.start + 1000)
        self.add_tone("available-last", self.start + 2000)
        missing.unlink()  # 仅删除本用例生成的人工音频。
        response = self.client.post("/api/recordings/clip", json={"start": self.start, "end": self.start + 3000})
        self.assertEqual(response.status_code, 409)
        self.assertIn("不可读", response.json()["detail"])

    def test_native_clips_skip_automatic_vad_and_marker_transcription(self):
        self.add_tone("clip-source", self.start, source="self_clip")
        with patch.object(main, "analyze_speech_ratio") as vad, \
             patch.object(main, "_launch_transcribe") as launch, \
             patch.object(main, "_load_markers", return_value=[{"id": "clip-marker", "ts": self.start / 1000}]), \
             patch.object(main, "_ensure_scan"):
            main.start_energy_analysis()
            main._markers_auto_transcribe()
        vad.assert_not_called()
        launch.assert_not_called()

    def test_timeline_and_clip_bound_metadata_parse_to_200(self):
        row = {"id": "limited", "name": "limited.m4a", "file_path": str(self.recordings / "missing"),
               "archived_path": None, "source": "self_recorder", "duration": 1,
               "started_at": datetime.fromtimestamp(self.start / 1000, timezone.utc).isoformat(), "ended_at": None}
        with patch.object(store, "recording_timeline_candidates", return_value=[row] * 201), \
             patch.object(recording_timeline, "_event_start", wraps=recording_timeline._event_start) as parse:
            response = self.client.get("/api/recordings/timeline")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["truncated"])
            self.assertEqual(parse.call_count, 200)
            clip = self.client.post("/api/recordings/clip", json={"start": self.start, "end": self.start + 500})
            self.assertEqual(clip.status_code, 400)

    def test_native_wall_clock_end_ignores_aac_padding(self):
        path = self.recordings / "padded.m4a"
        path.write_bytes(b"metadata fixture")
        began = datetime.fromtimestamp(self.start / 1000, timezone.utc)
        sid, _ = store.import_recording(
            name=path.name, file_path=str(path), capture_id="padded",
            started_at=began.isoformat(), ended_at=(began + timedelta(seconds=9.9)).isoformat(),
            created_at=began.strftime("%Y-%m-%d %H:%M:%S"), interruption_reason=None, duration=10.048,
        )
        rows, _ = self.service._candidates(self.start - 1000, self.start + 20000, main._resolve_audio)
        row = next(r for r in rows if r["id"] == sid)
        self.assertAlmostEqual(row["end"], self.start + 9900, delta=.01)
        self.assertAlmostEqual(row["duration"], 9.9, places=3)
        self.assertEqual(row["audio_duration"], 10.048)
        # 查询从真实结束后开始，不应因为 AAC 尾部填充仍找到假覆盖。
        after, _ = self.service._candidates(self.start + 9950, self.start + 11000, main._resolve_audio)
        self.assertEqual(after, [])

    def test_wall_clock_candidate_spanning_window_is_not_filtered_by_audio_duration(self):
        path = self.recordings / "crossing-window.m4a"
        path.write_bytes(b"metadata fixture")
        began = datetime.fromtimestamp(self.start / 1000, timezone.utc)
        sid, _ = store.import_recording(
            name=path.name, file_path=str(path), capture_id="crossing-window",
            started_at=began.isoformat(), ended_at=(began + timedelta(seconds=10)).isoformat(),
            created_at=began.strftime("%Y-%m-%d %H:%M:%S"), interruption_reason=None, duration=9.8,
        )
        rows, _ = self.service._candidates(self.start + 9900, self.start + 11000, main._resolve_audio)
        self.assertEqual([r["id"] for r in rows], [sid])
        self.assertAlmostEqual(rows[0]["end"], self.start + 10000, delta=.01)


if __name__ == "__main__":
    unittest.main()
