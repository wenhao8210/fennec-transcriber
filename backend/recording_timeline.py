"""只读录音时间轴、异步低清波形与保留墙钟空隙的本地切片。"""
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

import store
from config import DATA_DIR, SELF_RECORDINGS_DIR

MAX_SEGMENTS = 200
MAX_CLIP_MS = 3 * 3600 * 1000
RATE = 16000
PREVIEW_LIMIT = 10
PREVIEW_BYTE_LIMIT = 256 * 1024 * 1024
_REC_TIME = re.compile(r"rec_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")


class ClipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float
    end: float
    name: str | None = Field(default=None, max_length=120)
    preview: StrictBool = False

    @field_validator("start", "end", mode="before")
    @classmethod
    def finite_time(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("时间必须是有限的 Unix 毫秒数")
        return value


def _event_start(row):
    if row.get("started_at"):
        try:
            moment = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                return moment.timestamp() * 1000
        except (ValueError, OverflowError):
            return None
    for raw in (row.get("name"), row.get("file_path"), row.get("archived_path")):
        match = _REC_TIME.search(Path(raw or "").name)
        if match:
            try:
                # 第三方 rec_ 文件名一直用本机当地时间，datetime.timestamp 处理当地时区。
                return datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H %M %S").timestamp() * 1000
            except (ValueError, OverflowError):
                continue
    return None


def _event_end(row, began, audio_duration):
    """原生结束时间是墙钟真值；AAC 编码尾部 padding 不能变成事件覆盖区间。"""
    if row.get("ended_at"):
        try:
            moment = datetime.fromisoformat(row["ended_at"].replace("Z", "+00:00"))
            finished = moment.timestamp() * 1000
            if moment.tzinfo is not None and math.isfinite(finished) and finished > began:
                return finished
        except (ValueError, OverflowError, OSError):
            pass
    return began + audio_duration * 1000


def _version(path):
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".writing")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class TimelineService:
    def __init__(self, cache_dir=None, recordings_dir=None):
        self.root = Path(cache_dir or DATA_DIR / "recording_timeline")
        self.recordings = Path(recordings_dir or SELF_RECORDINGS_DIR)
        self.lock = threading.RLock()
        self.wave_queue = queue.Queue(maxsize=MAX_SEGMENTS)
        self.clip_queue = queue.Queue(maxsize=16)
        self.wave_pending = set()
        self.jobs = {}
        self.wave_thread = None
        self.clip_thread = None

    def active_jobs(self):
        with self.lock:
            return sum(job["status"] in {"queued", "processing"} for job in self.jobs.values())

    def _candidates(self, start, end, resolve_audio):
        try:
            first = datetime.fromtimestamp(start / 1000) - timedelta(days=1)
            last = datetime.fromtimestamp(end / 1000)
            utc_start = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
            utc_end = datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError) as exc:
            raise HTTPException(400, "时间范围无效") from exc
        dates = []
        day = first.date()
        while day <= last.date() and len(dates) < 4:
            dates.append(day.isoformat())
            day += timedelta(days=1)
        candidates = store.recording_timeline_candidates(utc_start, utc_end, dates)
        truncated = len(candidates) > MAX_SEGMENTS
        result = []
        for row in candidates[:MAX_SEGMENTS]:
            began = _event_start(row)
            audio_duration = float(row.get("duration") or 0)
            if began is None or not math.isfinite(audio_duration) or audio_duration <= 0:
                continue
            finished = _event_end(row, began, audio_duration)
            if finished <= start or began >= end:
                continue
            resolved = resolve_audio(row)
            audio = Path(resolved) if resolved is not None else None
            available = bool(audio and audio.is_file() and audio.stat().st_size > 0)
            result.append({"id": row["id"], "name": row["name"], "start": began,
                           "end": finished, "duration": (finished - began) / 1000,
                           "audio_duration": audio_duration, "source": row["source"],
                           "available": available, "_path": audio if available else None})
        return sorted(result, key=lambda row: (row["start"], row["id"])), truncated

    def timeline(self, hours, resolve_audio):
        if hours not in {1, 3, 6, 12, 24}:
            raise HTTPException(400, "时间范围仅支持 1、3、6、12、24 小时")
        end = round(time.time() * 1000)
        start = end - hours * 3600 * 1000
        rows, truncated = self._candidates(start, end, resolve_audio)
        result = []
        for row in rows:
            path = row.pop("_path")
            waveform = self._waveform(path, row["duration"]) if path else {"peaks": [], "waveform_status": "error"}
            result.append({**row, **waveform})
        return {"window_start": start, "window_end": end, "segments": result,
                "truncated": truncated, "pending_waveforms": sum(r["waveform_status"] == "pending" for r in result)}

    def _waveform(self, path, duration):
        try:
            version = _version(path)
            key = hashlib.sha256(json.dumps([version, duration, "wave-v1"]).encode()).hexdigest()
            cached = self.root / "waves" / (key + ".json")
            if cached.is_file():
                return json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"peaks": [], "waveform_status": "error"}
        with self.lock:
            if key not in self.wave_pending:
                try:
                    self.wave_queue.put_nowait((key, path, duration, version))
                    self.wave_pending.add(key)
                except queue.Full:
                    pass
            if self.wave_thread is None:
                self.wave_thread = threading.Thread(target=self._wave_worker, daemon=True, name="recording-waveform")
                self.wave_thread.start()
        return {"peaks": [], "waveform_status": "pending"}

    def _wave_worker(self):
        while True:
            item = self.wave_queue.get()
            if item is None:
                self.wave_queue.task_done()
                return
            key, path, duration, version = item
            try:
                block = max(1, math.ceil(min(duration, 86400) * 4000 / 320))
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(path),
                     "-t", str(min(duration, 86400)), "-vn", "-af",
                     f"aresample=4000,asetnsamples=n={block}:p=0,astats=metadata=1:reset=1,"
                     "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-",
                     "-f", "null", "-"], capture_output=True, text=True, timeout=120,
                )
                if result.returncode or _version(path) != version:
                    raise ValueError("waveform source changed or failed")
                peaks = []
                for raw in re.findall(r"lavfi\.astats\.Overall\.Peak_level=([^\s]+)", result.stdout):
                    level = float(raw)
                    peaks.append(min(1.0, max(0.0, 10 ** (level / 20))) if math.isfinite(level) else 0.0)
                if not peaks:
                    raise ValueError("empty waveform")
                maximum = max(peaks)
                peaks = [round(value / maximum, 4) if maximum else 0.0 for value in peaks[:322]]
                payload = {"peaks": peaks, "waveform_status": "ready"}
            except Exception:
                payload = {"peaks": [], "waveform_status": "error"}
            try:
                _write_json(self.root / "waves" / (key + ".json"), payload)
            except OSError:
                pass  # 缓存不可写不终止整条后台队列；下次请求可重新排队。
            finally:
                with self.lock:
                    self.wave_pending.discard(key)
                self.wave_queue.task_done()

    def submit(self, body, resolve_audio):
        start, end = round(body.start), round(body.end)
        if start <= 0 or end <= start or end - start > MAX_CLIP_MS or end > time.time() * 1000 + 1000:
            raise HTTPException(400, "请选择已结束的音频区间，最长 3 小时")
        rows, truncated = self._candidates(start, end, resolve_audio)
        if truncated:
            raise HTTPException(400, "选择范围内片段过多，请缩小范围后重试")
        if any(not row["available"] for row in rows):
            raise HTTPException(409, "选区内有录音文件不可读，请先恢复文件或缩小选区")
        rows = [row for row in rows if row["available"]]
        # 首尾必须确实落在已保存音频中。内部允许录音中断，用等长静音保留墙钟位置。
        if not rows or not any(r["start"] <= start + 2 and r["end"] > start for r in rows) \
                or not any(r["start"] < end and r["end"] >= end - 2 for r in rows):
            raise HTTPException(409, "所选首尾尚无已保存音频；请先保存当前录音或缩小选区")
        boundaries = sorted({start, end, *(max(start, min(end, round(r[k]))) for r in rows for k in ("start", "end"))})
        plan = []
        versions = {}
        for left, right in zip(boundaries, boundaries[1:]):
            if right <= left:
                continue
            covering = [r for r in rows if r["start"] <= (left + right) / 2 < r["end"]]
            chosen = max(covering, key=lambda r: (r["start"], r["id"])) if covering else None
            part = {"start": left, "end": right, "id": chosen["id"] if chosen else None}
            if chosen:
                path = chosen["_path"]
                try:
                    versions[chosen["id"]] = _version(path)
                except OSError as exc:
                    raise HTTPException(409, "源录音已移动，请刷新时间轴后重试") from exc
                part.update(path=str(path), offset=max(0, (left - chosen["start"]) / 1000))
            plan.append(part)
        request_key = [start, end, bool(body.preview), sorted(versions.items())]
        job_id = hashlib.sha256(json.dumps(request_key, sort_keys=True).encode()).hexdigest()[:32]
        with self.lock:
            old = self._load_job(job_id)
            if old and old["status"] in {"queued", "processing", "done"}:
                if old["status"] != "done" or Path(old.get("audio_path", "")).is_file():
                    return self._public_job(old)
            if self.clip_queue.full():
                raise HTTPException(429, "片段任务队列已满，请稍后重试")
            label = re.sub(r"[/\\:\x00-\x1f]", "_", (body.name or "").strip()).strip(". ")
            if not label:
                label = "片段_" + datetime.fromtimestamp(start / 1000).strftime("%Y%m%d_%H%M%S")
            job = {"job_id": job_id, "status": "queued", "preview": body.preview,
                   "start": start, "end": end, "name": label, "plan": plan,
                   "versions": versions, "created_at": time.time()}
            self.jobs[job_id] = job
            self._save_job(job)
            self.clip_queue.put_nowait(job_id)
            if self.clip_thread is None:
                self.clip_thread = threading.Thread(target=self._clip_worker, daemon=True, name="recording-clip")
                self.clip_thread.start()
            return self._public_job(job)

    def _load_job(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return None
        if job_id in self.jobs:
            return self.jobs[job_id]
        try:
            job = json.loads((self.root / "jobs" / (job_id + ".json")).read_text(encoding="utf-8"))
            if job["status"] in {"queued", "processing"}:
                job.update(status="error", error="服务曾中断，请重新生成片段")
            self.jobs[job_id] = job
            return job
        except (OSError, ValueError, KeyError):
            return None

    def _save_job(self, job):
        _write_json(self.root / "jobs" / (job["job_id"] + ".json"), job)

    @staticmethod
    def _public_job(job):
        return {key: job[key] for key in ("job_id", "status", "result", "error") if key in job}

    def job(self, job_id):
        with self.lock:
            job = self._load_job(job_id)
            if job is None:
                raise HTTPException(404, "片段任务不存在")
            return self._public_job(job)

    def audio(self, job_id):
        with self.lock:
            job = self._load_job(job_id)
            if not job or job["status"] != "done":
                raise HTTPException(404, "片段尚未完成")
            path = Path(job.get("audio_path", ""))
            expected = self.root / "previews" if job["preview"] else self.recordings
            if not path.is_file() or not path.resolve().is_relative_to(expected.resolve()):
                raise HTTPException(410, "试听已过期或片段文件已移动，请重新生成")
            return path

    def _clip_worker(self):
        while True:
            job_id = self.clip_queue.get()
            if job_id is None:
                self.clip_queue.task_done()
                return
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "processing"
                self._save_job(job)
            try:
                result, output = self._render_clip(job)
                with self.lock:
                    job.update(status="done", result=result, audio_path=str(output))
                    self._save_job(job)
                if job["preview"]:
                    self._prune_previews()
            except Exception as exc:
                with self.lock:
                    job.update(status="error", error=str(exc) if isinstance(exc, ValueError) else "生成片段失败，原始音频未修改，请稍后重试")
                    self._save_job(job)
            finally:
                self.clip_queue.task_done()

    def _render_clip(self, job):
        work = self.root / "work" / job["job_id"]
        work.mkdir(parents=True, exist_ok=True)
        combined, piece = work / "combined.raw", work / "part.raw"
        output_dir = self.root / "previews" if job["preview"] else self.recordings
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / (("preview_" if job["preview"] else "clip_") + job["job_id"] + ".m4a")
        temporary = output.with_name(output.stem + ".encoding.m4a")
        try:
            probe_path = output
            if not output.is_file():
                with combined.open("wb") as target:
                    for part in job["plan"]:
                        seconds = (part["end"] - part["start"]) / 1000
                        expected = round(seconds * RATE) * 2
                        if part["id"] is None:
                            self._silence(target, expected)
                            continue
                        path = Path(part["path"])
                        if _version(path) != job["versions"][part["id"]]:
                            raise ValueError("源录音已变化，请刷新时间轴后重新选择")
                        with piece.open("wb") as pcm:
                            subprocess.run(
                                ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-ss", str(part["offset"]),
                                 "-i", str(path), "-t", str(seconds), "-vn", "-ar", str(RATE), "-ac", "1",
                                 "-f", "s16le", "-"], stdout=pcm, stderr=subprocess.PIPE,
                                timeout=max(30, min(900, seconds * 2)), check=True,
                            )
                        actual = piece.stat().st_size
                        if actual == 0 or actual < expected - max(RATE // 3, expected // 100):
                            raise ValueError("源录音不完整，无法可靠生成所选时间范围")
                        if _version(path) != job["versions"][part["id"]]:
                            raise ValueError("源录音仍在变化，请先保存当前录音")
                        with piece.open("rb") as pcm:
                            remaining = expected
                            while remaining > 0:
                                chunk = pcm.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    self._silence(target, remaining)
                                    break
                                target.write(chunk)
                                remaining -= len(chunk)
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-y", "-f", "s16le",
                     "-ar", str(RATE), "-ac", "1", "-i", str(combined), "-c:a", "aac", "-b:a", "64k",
                     "-movflags", "+faststart", str(temporary)],
                    capture_output=True, timeout=900, check=True,
                )
                probe_path = temporary
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(probe_path)],
                capture_output=True, text=True, timeout=30, check=True,
            )
            duration = float(probe.stdout.strip())
            if not math.isfinite(duration) or abs(duration - (job["end"] - job["start"]) / 1000) > 0.2:
                raise ValueError("片段时长校验失败，未创建会话")
            if probe_path == temporary:
                temporary.replace(output)
            if job["preview"]:
                return {"preview_url": f"/api/recordings/clip/{job['job_id']}/audio", "duration": duration,
                        "start": job["start"], "end": job["end"]}, output
            started = datetime.fromtimestamp(job["start"] / 1000, timezone.utc)
            ended = datetime.fromtimestamp(job["end"] / 1000, timezone.utc)
            sid, _ = store.import_recording(
                name=job["name"], file_path=str(output.resolve()), capture_id="clip_" + job["job_id"],
                started_at=started.isoformat(), ended_at=ended.isoformat(),
                created_at=started.strftime("%Y-%m-%d %H:%M:%S"), interruption_reason=None,
                duration=duration, source="self_clip",
            )
            return {"id": sid, "name": job["name"], "source": "self_clip", "duration": duration}, output
        finally:
            # 仅本任务生成的可再生 PCM / 未完成输出；绝不清理输入录音或已完成片段。
            for generated in (combined, piece, temporary):
                generated.unlink(missing_ok=True)

    @staticmethod
    def _silence(target, count):
        while count > 0:
            chunk = min(count, 1024 * 1024)
            target.write(b"\0" * chunk)
            count -= chunk

    def _prune_previews(self):
        directory = self.root / "previews"
        candidates = [p for p in directory.glob("preview_*.m4a")
                      if re.fullmatch(r"preview_[0-9a-f]{32}\.m4a", p.name) and not p.is_symlink()]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, candidate in enumerate(candidates):
            size = candidate.stat().st_size
            if index >= PREVIEW_LIMIT or total + size > PREVIEW_BYTE_LIMIT:
                candidate.unlink(missing_ok=True)
            else:
                total += size

    def close(self):
        """隔离测试等待任务结束；生产由 daemon 线程随进程退出。"""
        for worker, tasks in ((self.wave_thread, self.wave_queue), (self.clip_thread, self.clip_queue)):
            if worker:
                tasks.put(None)
                worker.join(timeout=10)


service = TimelineService()
