"""FastAPI 后端：提供会话列表、转写任务、波形/文本等 API。"""
import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import Query, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
from config import (
    ARCHIVE_DIR,
    AUDIO_EXTS,
    DATA_DIR,
    RECORDINGS_DIR,
    SESSIONS_DIR,
    VOICE_PROFILE_SAMPLE,
    VOICE_PROFILES_DIR,
)
from asr import run_pipeline
from speaker import get_recognizer, match_cloud_speakers, resolve_ref_audio
from aliyun_asr import get_api_key, set_api_key, transcribe_cloud
from audio_quality import analyze_energy, analyze_speech_ratio, is_low_content, speech_score
import hotwords

app = FastAPI(title="Fennec Local Transcriber")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8778", "http://localhost:8778"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type"]
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def reject_untrusted_origin(request: Request, call_next):
    # Loopback binding alone does not stop malicious websites reaching a local API.
    origin = request.headers.get("origin")
    if origin and origin not in {"http://127.0.0.1:8778", "http://localhost:8778"}:
        return Response("Untrusted origin", status_code=403)
    return await call_next(request)


_job_progress: dict[str, dict] = {}
_energy_state: dict = {"running": False, "done": 0, "total": 0, "low": []}
_archive_state: dict = {"running": False, "done": 0, "total": 0, "failed": [], "last_run": None}
_archive_lock = threading.Lock()


# ---------- 归档区（对抗 Fennec 15 天录音保留期） ----------
# 保护范围：仅收藏的录音。收藏时立即归档，并由后台巡检每 10 分钟补漏，
# 保证 Fennec 清理源文件后音频仍可读。已转写但未收藏的不做保护。

def _safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


def _needs_protection(session: dict) -> bool:
    """是否需要归档保护：只有收藏的才算受保护。"""
    return bool(session.get("favorite"))


def _resolve_audio(session: dict) -> Path | None:
    """返回实际可读的音频路径：优先 Fennec 源文件，被清理后回落归档副本。"""
    allowed_roots = (SESSIONS_DIR.resolve(), RECORDINGS_DIR.resolve(), ARCHIVE_DIR.resolve())
    for raw in (session.get("file_path"), session.get("archived_path")):
        if not raw:
            continue
        candidate = Path(raw).resolve()
        if (candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTS
                and any(candidate.is_relative_to(root) for root in allowed_roots)):
            return candidate
    return None


def _archive_session(sid: str, session: dict) -> bool:
    """把录音复制进归档区。已是归档状态或复制成功返回 True。"""
    src = _resolve_audio(session)
    if src is None:
        return False
    target = ARCHIVE_DIR / f"{sid}{src.suffix.lower()}"
    with _archive_lock:
        try:
            if not target.exists():
                shutil.copy2(src, target)
            store.set_archived_path(sid, str(target))
            return True
        except OSError:
            return False


def _archive_protected() -> dict:
    """把所有收藏的录音归档（幂等）。未归档或归档副本缺失的才处理。"""
    global _archive_state
    if _archive_state["running"]:
        return _archive_state

    sessions = store.list_sessions()
    targets = [
        s for s in sessions
        if _needs_protection(s)
        and not (s.get("archived_path") and Path(s["archived_path"]).exists())
    ]

    _archive_state = {
        "running": True, "done": 0, "total": len(targets), "failed": [], "last_run": None,
    }
    try:
        for s in targets:
            if _archive_session(s["id"], s):
                _archive_state["done"] += 1
            else:
                _archive_state["failed"].append(s["name"])
        _archive_state["last_run"] = datetime.now().isoformat(timespec="seconds")
    finally:
        _archive_state["running"] = False
    return _archive_state


def _archive_worker():
    """后台常驻：启动先跑一次，之后每 10 分钟巡检，保证新收藏的录音都能及时归档。"""
    _archive_protected()
    while True:
        time.sleep(600)
        try:
            _archive_protected()
        except Exception:
            pass


def _move_to_trash(source: Path) -> Path:
    """把源录音移到 macOS 废纸篓，避免不可恢复的直接删除。"""
    trash_dir = Path.home() / ".Trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = trash_dir / source.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = trash_dir / f"{source.stem}-{stamp}{source.suffix}"
    return Path(shutil.move(str(source), str(target)))


@app.post("/api/analyze-energy")
def start_energy_analysis():
    """后台批量分析「人声占比」（Silero VAD），标出低内容音频。

    口径（见 audio_quality.py）：人声占比 ≤10% → 1~2 分 → 低内容。
    增量执行：只跑 speech_ratio 还没算过的（全量约 969 条 / 10 分钟）。
    """
    global _energy_state
    if _energy_state["running"]:
        return {"status": "running", **_energy_state}

    sessions = store.list_sessions()
    targets = [s for s in sessions if s.get("speech_ratio") is None]
    if not targets:
        return {"status": "done", "done": 0, "total": 0, "low": []}

    _energy_state = {"running": True, "done": 0, "total": len(targets), "low": []}

    def job():
        try:
            for s in targets:
                audio = _resolve_audio(s)
                if audio is None:
                    continue
                ratio = analyze_speech_ratio(audio)
                db = s.get("energy_db")
                if db is None:
                    db = analyze_energy(audio)   # 保留平均音量作辅助指标
                store.update_quality(s["id"], db, ratio)
                if is_low_content(ratio):
                    _energy_state["low"].append(s["id"])
                _energy_state["done"] += 1
        finally:
            _energy_state["running"] = False

    t = threading.Thread(target=job, daemon=True)
    t.start()
    return {"status": "started", **_energy_state}


@app.get("/api/analyze-energy")
def energy_status():
    return _energy_state


def _scan_dir(directory: Path, source: str):
    """扫描目录中的音频文件，注册到数据库（跳过已删除的）。"""
    if not directory.exists():
        return
    for f in sorted(directory.iterdir()):
        if (f.is_file() and not f.is_symlink() and f.suffix.lower() in AUDIO_EXTS
                and not store.is_deleted(str(f))):
            sid = uuid.uuid4().hex[:12]  # sid 用 uuid 与路径解耦；upsert 按 file_path 去重，已有文件复用原 id
            store.upsert_session(sid, f.name, str(f), source)
            # 补时长（已填过的不重复 ffprobe）
            s = store.get_session(sid)
            if s and not s["duration"]:
                try:
                    import subprocess
                    out = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "csv=p=0", str(f)],
                        capture_output=True, text=True, timeout=15,
                    )
                    if out.returncode == 0 and out.stdout.strip():
                        store.update_duration(sid, float(out.stdout.strip()))
                except Exception:
                    pass


def _ensure_scan():
    """首次访问时扫描 Fennec 目录。"""
    _scan_dir(SESSIONS_DIR, "sessions")
    _scan_dir(RECORDINGS_DIR, "recordings")


def _progress_cb(sid: str):
    def cb(frac: float, phase: str):
        _job_progress[sid] = {"frac": round(frac, 3), "phase": phase}
    return cb


@app.on_event("startup")
def _startup():
    store.init_db()
    store.mark_running_sessions_interrupted()
    _ensure_scan()
    threading.Thread(target=_archive_worker, daemon=True).start()
    threading.Thread(target=_marker_worker, daemon=True).start()


@app.get("/api/sessions")
def list_sessions():
    _ensure_scan()
    rows = store.list_sessions()
    # 把「人声占比 → 1~5 分 → 低内容」的判定统一放后端算，前端只读结论
    for s in rows:
        score = speech_score(s.get("speech_ratio"))
        s["speech_score"] = score
        s["low_content"] = score is not None and score <= 2
    return rows


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, purge: bool = True):
    """删除会话；默认将源文件移入项目回收站。
    purge=False：仅从本应用移除，保留 Fennec 目录源文件。

    已收藏的录音禁止删除（Fennec 有 15 天保留期，收藏即视为受保护资产）。
    """
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    if s["status"] == "transcribing":
        raise HTTPException(409, "正在转写，暂时不能删除")
    if s.get("favorite"):
        raise HTTPException(409, "已收藏的录音不允许删除，请先取消收藏")

    source_path = Path(s["file_path"])
    trashed_to = None
    if purge:
        try:
            if source_path.exists():
                if not source_path.is_file():
                    raise HTTPException(400, "source path is not a regular file")
                trashed_to = _move_to_trash(source_path)
        except HTTPException:
            raise
        except OSError as exc:
            # 移动失败时保留数据库记录，列表不会出现“假删除”。
            raise HTTPException(500, f"源文件移入回收站失败：{exc.strerror or str(exc)}") from exc

    # 删除会话时同步清理归档副本（移入系统废纸篓，可恢复），避免归档区留下孤儿文件
    archived = s.get("archived_path")
    archived_trashed = False
    if archived and Path(archived).exists():
        try:
            _move_to_trash(Path(archived))
            archived_trashed = True
        except OSError:
            pass

    # purge=False 时加入隐藏表，避免扫描任务马上把保留的源文件重新加回来。
    store.delete_session(sid, hidden_path=None if purge else s["file_path"])
    return {
        "ok": True,
        "moved_to_trash": trashed_to is not None,
        "archive_moved_to_trash": archived_trashed,
        "source_missing": purge and not source_path.exists() and trashed_to is None,
    }


# 单 worker 转写队列：多任务串行执行。whisper/声纹模型经 get_transcriber()/get_recognizer()
# 全局单例复用（whisper small int8 约 500MB/份），串行 + 单例可避免并发时重复加载多份模型导致内存暴涨。
_transcribe_queue: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
_transcribe_worker_started = False
_transcribe_worker_lock = threading.Lock()


def _ensure_transcribe_worker() -> None:
    global _transcribe_worker_started
    with _transcribe_worker_lock:
        if _transcribe_worker_started:
            return
        _transcribe_worker_started = True
    threading.Thread(target=_transcribe_worker, daemon=True, name="transcribe-worker").start()


def _transcribe_worker() -> None:
    while True:
        sid, engine, audio_str = _transcribe_queue.get()
        try:
            _run_transcribe_job(sid, engine, audio_str)
        except Exception:
            pass  # _run_transcribe_job 内部已兜住异常并写入会话状态
        finally:
            _transcribe_queue.task_done()


def _launch_transcribe(sid: str, engine: str = "local") -> bool:
    """启动一次转写任务（接口与后台自动任务共用）。

    已在转写/排队、音频不存在、同引擎已完成时不重复启动，返回 False。
    任务进入单 worker 队列串行执行（见 _transcribe_queue），不再每个任务单独起线程。
    """
    s = store.get_session(sid)
    if not s or s["status"] == "transcribing":
        return False
    audio = _resolve_audio(s)
    if audio is None:
        return False
    if s["status"] == "done" and s["segments"] and s.get("engine") == engine:
        return False

    store.update_status(sid, "transcribing")
    _job_progress[sid] = {"frac": 0.0, "phase": "queued"}
    _ensure_transcribe_worker()
    _transcribe_queue.put((sid, engine, str(audio)))
    return True


def _run_transcribe_job(sid: str, engine: str, audio_str: str) -> None:
    """单个转写任务的实际执行体（由 _transcribe_worker 串行调用）。"""
    audio = Path(audio_str)
    try:
        if engine == "aliyun":
            result = transcribe_cloud(audio, _progress_cb(sid))
        else:
            result = run_pipeline(audio, _progress_cb(sid))
        store.save_segments(sid, result["segments"])
        store.set_engine(sid, engine)
        if result.get("ref_used"):
            store.set_speaker_ref(sid, result["ref_used"])
        # 多声纹自动认人：云端说话人分离（speaker_N）→ 与声纹库匹配写名字
        if engine == "aliyun" and result.get("segments"):
            try:
                _auto_label_cloud_speakers(sid, str(audio), result["segments"])
            except Exception:
                pass
        # 用转写信息补时长（whisper 未返回时用 ffprobe）
        try:
            import subprocess
            out = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(audio)],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0 and out.stdout.strip():
                store.update_duration(sid, float(out.stdout.strip()))
        except Exception:
            pass
        store.update_status(sid, "done")
    except Exception as e:
        store.update_status(sid, "error", str(e))
    finally:
        _job_progress.pop(sid, None)


@app.post("/api/sessions/{sid}/transcribe")
def start_transcribe(sid: str, engine: str = "local"):
    """后台启动转写任务（幂等：已在转写中则返回 409）。engine: local|aliyun"""
    if engine not in {"local", "aliyun"}:
        raise HTTPException(400, "engine must be local or aliyun")
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    if s["status"] == "transcribing":
        raise HTTPException(409, "already transcribing")
    # 源文件可能已被 Fennec 保留期清走，此时回落到归档副本
    if _resolve_audio(s) is None:
        raise HTTPException(410, "音频文件已不存在：Fennec 保留期已清理，且该录音未被归档")
    # 只有同一引擎的已有结果才能复用；切换引擎时重新转写并覆盖旧结果。
    if s["status"] == "done" and s["segments"] and s.get("engine") == engine:
        return {"status": "done", "cached": True, "engine": engine}
    _launch_transcribe(sid, engine)
    return {"status": "transcribing"}


@app.get("/api/sessions/{sid}/progress")
def get_progress(sid: str):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return {"status": s["status"], "error": s.get("error"), **_job_progress.get(sid, {})}


@app.get("/api/sessions/{sid}/audio")
def get_audio(sid: str):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    audio = _resolve_audio(s)
    if audio is None:
        raise HTTPException(410, "音频文件已不存在：Fennec 保留期已清理，且该录音未被归档")
    return FileResponse(str(audio), media_type="audio/mp4")


@app.get("/api/sessions/{sid}/export")
def export_txt(sid: str):
    """导出转写内容为 TXT（对话式：时间戳 + 说话人 + 文本）。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    if not s["segments"]:
        raise HTTPException(400, "no transcription yet")

    def fmt(t):
        m, sec = divmod(int(t), 60)
        return f"{m:02d}:{sec:02d}"

    speaker_map = {"self": "我", "other": "对方", "unknown": "未知"}
    labels = s.get("speaker_labels") or {}
    lines = [f"# {s['name']}", f"# 转写时间: {s['created_at']}", ""]
    for seg in s["segments"]:
        sp = seg.get("speaker")
        if labels.get(sp):
            who = labels[sp]
        elif sp in speaker_map:
            who = speaker_map[sp]
        elif sp and sp.startswith("speaker_"):
            who = f"说话人{int(sp.split('_')[1]) + 1}"
        else:
            who = "未知"
        lines.append(f"[{fmt(seg['start'])} - {fmt(seg['end'])}] {who}: {seg['text']}")
    content = "\n".join(lines)

    fname = Path(s["file_path"]).stem + ".txt"
    # 中文文件名需要 RFC 5987 编码，否则 HTTP 头 latin-1 报错
    from urllib.parse import quote
    return Response(
        content=content.encode("utf-8-sig"),  # BOM，保证中文在 Windows 记事本不乱码
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


def _local_ts(raw) -> str:
    """库里存的是 UTC，展示按本机时区"""
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw)


def _fmt_mmss(t) -> str:
    m, sec = divmod(int(float(t or 0)), 60)
    return f"{m:02d}:{sec:02d}"


def _speaker_name(sp, labels) -> str:
    if labels.get(sp):
        return labels[sp]
    if sp == "self":
        return "我"
    if sp == "other":
        return "对方"
    if sp and str(sp).startswith("speaker_"):
        try:
            return f"说话人{int(str(sp).split('_')[1]) + 1}"
        except Exception:
            return str(sp)
    return sp or "未知"


def _coalesce(segs, gap: float = 2.0, max_len: int = 260):
    """同一个人连续说的短句合并成一段，复制出来的文本才像对话"""
    out = []
    for seg in segs:
        text = (seg.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        sp = seg.get("speaker") or ""
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or 0)
        if out and out[-1][0] == sp and start - out[-1][2] <= gap and len(out[-1][3]) < max_len:
            out[-1][3] += " " + text
            out[-1][2] = end
        else:
            out.append([sp, start, end, text])
    return out


@app.get("/api/export-text")
def export_text(ids: str):
    """多个会话的转写合并成纯文本，供前端复制到剪贴板（无附件头，直接读响应体）。"""
    id_list = [x.strip() for x in (ids or "").split(",") if x.strip()]
    if not id_list:
        raise HTTPException(400, "no ids")
    blocks = []
    for sid in id_list:
        s = store.get_session(sid)
        if not s or not s.get("segments"):
            continue
        labels = s.get("speaker_labels") or {}
        lines = [f"## {s['name']}", f"{_local_ts(s.get('created_at'))} · {_fmt_mmss(s.get('duration'))}", ""]
        for sp, start, _end, text in _coalesce(s["segments"]):
            lines.append(f"[{_fmt_mmss(start)}] {_speaker_name(sp, labels)}: {text}")
        blocks.append("\n".join(lines))
    if not blocks:
        raise HTTPException(400, "选中的录音没有转写内容")
    return Response("\n\n".join(blocks), media_type="text/plain; charset=utf-8")


@app.get("/api/voice/status")
def voice_status():
    ref = resolve_ref_audio()
    return {"has_ref": ref is not None, "path": str(ref) if ref else None}


# ---------- 时间标记：监听时标记当前时间，事后回溯找录音 ----------
import threading as _thr
_MARKER_LOCK = _thr.Lock()
_MARKER_FILE = DATA_DIR / "markers.json"

def _load_markers():
    try:
        with open(_MARKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_markers(markers):
    with _MARKER_LOCK:
        with open(_MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(markers, f, ensure_ascii=False, indent=2)

@app.post("/api/markers")
async def add_marker(req: Request):
    """在时间轴上标记一个时间点（可选备注意为 note，与录音无关）。
    默认用服务端当前时间；前端可传 ts 指定"点下那一刻"，避免打字延迟导致时间偏后。
    """
    note, ts = "", None
    try:
        body = await req.json()
        note = (body.get("note") or "").strip()[:200]
        if body.get("ts"):
            ts = int(body["ts"])
    except Exception:
        pass
    markers = _load_markers()
    marker = {"id": uuid.uuid4().hex[:8], "ts": ts or int(time.time()), "note": note}
    markers.append(marker)
    _save_markers(markers)
    return marker

@app.get("/api/markers")
async def list_markers():
    return _load_markers()

@app.delete("/api/markers/{mid}")
async def del_marker(mid: str):
    markers = _load_markers()
    new = [m for m in markers if m["id"] != mid]
    if len(new) == len(markers):
        raise HTTPException(404, "marker not found")
    _save_markers(new)
    return {"ok": True}

@app.patch("/api/markers/{mid}")
async def update_marker(mid: str, req: Request):
    """修改某条标记的备注（note）。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    note = (body.get("note") or "").strip()[:200]
    markers = _load_markers()
    hit = next((m for m in markers if m["id"] == mid), None)
    if not hit:
        raise HTTPException(404, "marker not found")
    hit["note"] = note
    _save_markers(markers)
    return hit

def _file_candidate_times(fp: str):
    """录音的候选时间戳：文件名 rec_YYYY-MM-DD_HH-MM-SS 或文件 mtime。"""
    cand = []
    base = os.path.basename(fp)
    m = re.search(r"rec_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", base)
    if m:
        try:
            cand.append(datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            pass
    try:
        cand.append(os.path.getmtime(fp))
    except Exception:
        pass
    return cand

@app.get("/api/markers/{mid}/nearby")
async def marker_nearby(mid: str, window: int = 600):
    """返回标记时间附近（默认 ±10 分钟）已回库与尚未入库的录音。"""
    markers = _load_markers()
    marker = next((m for m in markers if m["id"] == mid), None)
    if not marker:
        raise HTTPException(404, "marker not found")
    target = marker["ts"]
    _ensure_scan()  # 触发扫描，让刚回库的录音进入列表
    sessions = store.list_sessions()
    sess_hits, raw_hits = [], []
    scanned_paths = set()
    for s in sessions:
        fp = s.get("file_path") or ""
        scanned_paths.add(fp)
        cand = _file_candidate_times(fp)
        d = min((abs(c - target) for c in cand), default=None)
        if d is not None and d <= window:
            sess_hits.append({"id": s["id"], "name": s["name"],
                              "status": s.get("status"), "delta": int(d)})
    for d in (RECORDINGS_DIR, SESSIONS_DIR):
        if not d or not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                continue
            fp = os.path.join(d, fn)
            if fp in scanned_paths:
                continue
            cand = _file_candidate_times(fp)
            dd = min((abs(c - target) for c in cand), default=None)
            if dd is not None and dd <= window:
                raw_hits.append({"path": fp, "name": fn, "delta": int(dd)})
    sess_hits.sort(key=lambda x: x["delta"])
    raw_hits.sort(key=lambda x: x["delta"])
    return {"marker": marker, "sessions": sess_hits, "raw": raw_hits}

@app.post("/api/import")
async def import_file(req: Request):
    """把尚未入库的音频文件登记进 Fennec 并拿到 session id。"""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "bad body")
    path = body.get("path")
    if not isinstance(path, str) or not path:
        raise HTTPException(400, "文件不存在或路径无效")
    resolved = Path(path).expanduser().resolve()
    allowed_roots = (SESSIONS_DIR.resolve(), RECORDINGS_DIR.resolve(), ARCHIVE_DIR.resolve())
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(403, "只允许导入已配置录音目录或归档目录内的音频")
    if not resolved.is_file() or resolved.suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(400, "文件不存在或不是支持的音频格式")
    path = str(resolved)
    name = resolved.stem
    sid = uuid.uuid4().hex[:12]  # sid 用 uuid 与路径解耦；upsert 按 file_path 去重，已有文件复用原 id
    in_rec = os.path.dirname(os.path.abspath(path)) == os.path.abspath(str(RECORDINGS_DIR))
    sid = store.upsert_session(sid, name, path, "recordings" if in_rec else "sessions")
    return {"id": sid, "name": name}


# ---------- 标记自动转写：标记时间点附近的录音，回库后自动转写 ----------
MARKER_WINDOW = 600            # 秒；标记时间点前后多久内的录音算"这一段"
MARKER_AUTO_ENGINE = "local"   # local | aliyun（自动转写用哪个引擎）
MARKER_MAX_AGE = 30 * 86400    # 只对最近 30 天的标记自动转写，避免翻出陈年标记
_MARKER_JOB = {"running": False, "last_run": None, "started": []}


def _markers_auto_transcribe() -> dict:
    """扫描所有标记：把标记时间点附近、已回库但未转写的录音自动转写。"""
    if _MARKER_JOB["running"]:
        return _MARKER_JOB
    _MARKER_JOB["running"] = True
    started = []
    try:
        markers = _load_markers()
        if markers:
            _ensure_scan()  # 让刚回库的录音进入列表
            sessions = store.list_sessions()
            cutoff = time.time() - MARKER_MAX_AGE
            changed = False
            for m in markers:
                if m.get("ts", 0) < cutoff:
                    continue
                # 一个标记只自动触发一次；仅当上次触发的会话失败/不存在时才重试
                prev_id = m.get("auto_session")
                if prev_id:
                    prev = store.get_session(prev_id)
                    if prev and prev.get("status") in ("done", "transcribing"):
                        continue
                # 该标记只挑"最贴近"的一段录音，避免把相邻两通电话都转掉
                best = None
                for s in sessions:
                    if s.get("status") in ("done", "transcribing", "error"):
                        continue  # 已完成 / 进行中 / 失败的不再自动触发
                    cand = _file_candidate_times(s.get("file_path") or "")
                    d = min((abs(c - m["ts"]) for c in cand), default=None)
                    if d is None or d > MARKER_WINDOW:
                        continue
                    if best is None or d < best[0]:
                        best = (d, s)
                if best is None:
                    continue
                d, s = best
                fp = s.get("file_path") or ""
                try:
                    if time.time() - os.path.getmtime(fp) < 60:
                        continue  # 录音还在写盘，下一轮再转
                except OSError:
                    continue
                if _launch_transcribe(s["id"], MARKER_AUTO_ENGINE):
                    m["auto_session"] = s["id"]
                    changed = True
                    started.append({"marker": m["id"], "session": s["name"], "delta": int(d)})
            if changed:
                _save_markers(markers)
        _MARKER_JOB["started"] = started
        _MARKER_JOB["last_run"] = datetime.now().isoformat(timespec="seconds")
    finally:
        _MARKER_JOB["running"] = False
    return _MARKER_JOB


def _marker_worker():
    """后台常驻：每 2 分钟巡检一次标记，自动转写其附近的待转写录音。"""
    time.sleep(15)  # 等启动扫描完成
    while True:
        try:
            _markers_auto_transcribe()
        except Exception:
            pass
        time.sleep(120)


@app.get("/api/markers/auto/status")
def markers_auto_status():
    return _MARKER_JOB


@app.post("/api/markers/auto/run")
def markers_auto_run():
    """手动触发一次标记自动转写巡检。"""
    return _markers_auto_transcribe()


def _auto_label_cloud_speakers(sid: str, src: str, segments: list):
    """云端说话人分离后，把匹配到的声纹名字写入 speaker_labels。"""
    profiles = store.get_voice_profiles_with_embedding()
    if not profiles:
        return
    mapping = match_cloud_speakers(Path(src), segments, profiles)
    for sp, name in mapping.items():
        store.set_speaker_label(sid, sp, name)


def _to_16k_mono(src: Path, tag: str) -> Path:
    import subprocess

    out = VOICE_PROFILES_DIR / f"{tag}_16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(out)],
        check=True, capture_output=True,
    )
    return out


@app.get("/api/voice/profiles")
def voice_profiles_list():
    return {"profiles": store.list_voice_profiles()}


@app.post("/api/voice/profiles")
async def voice_profiles_add(name: str = Form(...), file: UploadFile = File(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "音频为空")
    pid = uuid.uuid4().hex[:12]
    ext = Path(file.filename or "voice.wav").suffix or ".wav"
    raw_path = VOICE_PROFILES_DIR / f"{pid}{ext}"
    raw_path.write_bytes(raw)
    try:
        recognizer = get_recognizer()
        wav_path = _to_16k_mono(raw_path, pid)
        emb = recognizer.enroll(wav_path)
    except Exception as e:
        raw_path.unlink(missing_ok=True)
        (VOICE_PROFILES_DIR / f"{pid}_16k.wav").unlink(missing_ok=True)
        raise HTTPException(400, f"声纹提取失败：{e}")
    store.add_voice_profile(pid, name, str(raw_path), np.asarray(emb, dtype=np.float32).tobytes())
    return {"ok": True, "profile": {"id": pid, "name": name}}


@app.delete("/api/voice/profiles/{pid}")
def voice_profiles_delete(pid: str):
    if not store.delete_voice_profile(pid):
        raise HTTPException(404, "声纹不存在")
    for p in VOICE_PROFILES_DIR.glob(f"{pid}*"):
        p.unlink(missing_ok=True)
    return {"ok": True}


class VoiceProfileFromSegIn(BaseModel):
    name: str
    start: float
    end: float


@app.post("/api/sessions/{sid}/voice-profile")
def voice_profile_from_segment(sid: str, payload: VoiceProfileFromSegIn):
    """从已有录音的 [start,end] 片段收录一段说话人声纹。"""
    session = store.get_session(sid)
    if not session:
        raise HTTPException(404, "会话不存在")
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    if payload.end - payload.start < 2:
        raise HTTPException(400, "片段过短，请选 ≥2s 的说话片段")
    src = _resolve_audio(session)
    if src is None:
        raise HTTPException(404, "源音频不存在（Fennec 保留期已清理且未归档）")
    try:
        recognizer = get_recognizer()
        emb = recognizer.embed_segment(src, payload.start, payload.end, "enroll")
    except Exception as e:
        raise HTTPException(400, f"声纹提取失败：{e}")
    pid = uuid.uuid4().hex[:12]
    store.add_voice_profile(pid, name, str(src), np.asarray(emb, dtype=np.float32).tobytes())
    return {"ok": True, "profile": {"id": pid, "name": name}}


class SpeakerLabelIn(BaseModel):
    speaker: str
    label: str


class FavoriteIn(BaseModel):
    favorite: bool


class RenameIn(BaseModel):
    name: str


@app.post("/api/sessions/{sid}/rename")
def rename_session(sid: str, body: RenameIn):
    """重命名：源文件 + 数据库同步改（保留扩展名，不可含路径分隔符）。
    sid 为 uuid、与路径解耦，改名后 sid 不变，无需迁移关联数据。
    """
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(400, "name cannot be empty")
    if "/" in new_name or "\\" in new_name or ":" in new_name:
        raise HTTPException(400, "name cannot contain / \\ :")
    old_path = Path(s["file_path"])
    new_path = old_path.with_name(new_name + old_path.suffix)
    if new_path.exists() and new_path != old_path:
        raise HTTPException(409, "同名文件已存在")
    try:
        import os
        os.rename(old_path, new_path)
    except Exception as e:
        raise HTTPException(500, f"文件重命名失败: {e}")

    # sid 为 uuid、与路径解耦，改名后 sid 不变，仅更新 name 与 file_path，无需迁移关联数据
    store.rename_session(sid, new_name, str(new_path))

    # Archive filenames use stable session IDs; renaming must not overwrite another archive.
    return {"ok": True, "name": new_name, "file_path": str(new_path), "id": sid}


@app.post("/api/sessions/{sid}/favorite")
def set_favorite(sid: str, body: FavoriteIn):
    """收藏/取消收藏。

    收藏即视为受保护：立即把录音复制进归档区，并禁止删除。
    取消收藏不删归档副本（避免二次丢失），只解除删除保护。
    """
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    archived = False
    if body.favorite:
        archived = _archive_session(sid, s)
    store.toggle_favorite(sid, body.favorite)
    return {"ok": True, "favorite": body.favorite, "archived": archived}


class TagsIn(BaseModel):
    tags: list[str] = []


@app.get("/api/tags")
def list_tags():
    """全部标签及使用次数（多的在前），供筛选栏与标签编辑弹窗使用。"""
    return {"tags": store.list_all_tags()}


@app.post("/api/sessions/{sid}/tags")
def set_session_tags(sid: str, body: TagsIn):
    """整组覆盖某个会话的标签（传空数组等于清空）。

    标签用于事后检索：客户名 / 主题 / 参会人 / 场合都可以。
    不做白名单限制，重复与空值由 store.normalize_tags 统一清洗。
    """
    if not store.get_session(sid):
        raise HTTPException(404, "session not found")
    return {"ok": True, "tags": store.set_tags(sid, body.tags)}


@app.post("/api/sessions/{sid}/speaker-label")
def update_speaker_label(sid: str, body: SpeakerLabelIn):
    """把某说话人（speaker_0/speaker_1/self/other）改名为自定义标签。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label cannot be empty")
    store.set_speaker_label(sid, body.speaker, label)
    return {"ok": True, "speaker": body.speaker, "label": label}


class ApiKeyIn(BaseModel):
    api_key: str
    base_url: str = ""
    workspace: str = ""


@app.get("/api/settings")
def get_settings():
    """返回阿里云 API Key / AccessKey 是否已配置（不回传任何密钥本身）。"""
    from aliyun_asr import get_base_url, get_workspace
    from billing import get_ak
    ak_id, ak_secret = get_ak()
    return {
        "dashscope_configured": bool(get_api_key()),
        "base_url": get_base_url(),
        "workspace": get_workspace(),
        # 用于「每日录音」右侧查余额；AccessKey ID 不算密钥，回传方便用户核对，Secret 绝不回传
        "aliyun_ak_configured": bool(ak_id and ak_secret),
        "aliyun_ak_id": ak_id,
    }


@app.post("/api/settings")
def update_settings(body: ApiKeyIn):
    """保存阿里云 API Key 到本地 settings.json。"""
    set_api_key(body.api_key, body.base_url, body.workspace)
    return {"ok": True, "dashscope_configured": bool(body.api_key.strip())}


class BillingAkIn(BaseModel):
    ak_id: str = ""
    ak_secret: str = ""


@app.post("/api/settings/billing")
def update_billing_settings(body: BillingAkIn):
    """保存查余额用的阿里云 AccessKey，并立即试查一次（两个都留空=清除）。"""
    from billing import get_billing, set_ak
    set_ak(body.ak_id, body.ak_secret)
    if not (body.ak_id.strip() and body.ak_secret.strip()):
        return {"ok": True, "configured": False, "error": ""}
    res = get_billing(force=True)
    return {
        "ok": not res.get("error"),
        "configured": True,
        "error": res.get("error") or "",
        "available": res.get("available"),
        "hours_left": res.get("hours_left"),
    }


@app.get("/api/billing")
def get_billing_api(force: int = 0):
    """阿里云账户余额（「每日录音」右侧额度）。按天缓存，force=1 强制刷新。"""
    from billing import get_billing
    return get_billing(force=bool(force))


@app.get("/api/archive/status")
def archive_status():
    """归档区状态：收藏数、已归档数、丢失清单、归档目录占用。"""
    sessions = store.list_sessions()
    favorites = [s for s in sessions if s.get("favorite")]
    archived = [s for s in favorites if s.get("archived_path") and Path(s["archived_path"]).exists()]
    lost = [s["name"] for s in favorites if _resolve_audio(s) is None]
    size = sum(p.stat().st_size for p in ARCHIVE_DIR.glob("*") if p.is_file())
    return {
        "dir": str(ARCHIVE_DIR),
        "favorites": len(favorites),
        "archived": len(archived),
        "lost": lost,
        "total_bytes": size,
        "job": _archive_state,
    }


@app.post("/api/archive/run")
def archive_run():
    """手动触发一次收藏录音归档（幂等，已归档的跳过）。"""
    return _archive_protected()


@app.get("/api/config")
def config():
    return {
        "sessions_dir": str(SESSIONS_DIR),
        "recordings_dir": str(RECORDINGS_DIR),
        "voice_profile_sample": str(VOICE_PROFILE_SAMPLE),
        "model": "faster-whisper small (local)",
    }


class HotwordIn(BaseModel):
    word: str
    weight: int = 3


class HotwordsIn(BaseModel):
    words: list[str]
    weight: int = 3


@app.get("/api/hotwords")
def get_hotwords():
    """返回本地热词列表 + 阿里云同步状态。"""
    return {
        "hotwords": hotwords.list_hotwords(),
        "phrase_id": hotwords.get_phrase_id(),
    }


@app.post("/api/hotwords")
def add_hotword(body: HotwordIn):
    added = hotwords.add_hotword(body.word, body.weight)
    return {"ok": True, "added": added}


@app.post("/api/hotwords/batch")
def add_hotwords(body: HotwordsIn):
    changed = hotwords.set_hotwords(body.words, body.weight)
    return {"ok": True, "changed": changed, "count": len(body.words)}


@app.delete("/api/hotwords/{word}")
def delete_hotword(word: str):
    removed = hotwords.remove_hotword(word)
    return {"ok": True, "removed": removed}


@app.post("/api/hotwords/sync")
def sync_hotwords():
    """同步本地热词到阿里云（创建或更新词表）。"""
    return hotwords.sync_to_aliyun()


# 挂载前端静态页面
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
@app.middleware("http")
async def no_cache_html(request, call_next):
    """页面本身不缓存，改完前端重开窗口就能生效"""
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.get("/api/search")
def search_transcripts(q: str = Query(min_length=1, max_length=200), offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
    if not q.strip():
        raise HTTPException(400, "请输入搜索内容")
    return store.search_segments(q.strip(), offset, limit)


class SegmentEditIn(BaseModel):
    text: str
    speaker: str
    expected_text: str
    expected_speaker: str
    remember_word: str = ""


@app.patch("/api/sessions/{sid}/segments/{segment_id}")
def edit_transcript_segment(sid: str, segment_id: int, body: SegmentEditIn):
    text = body.text.strip()
    word = body.remember_word.strip()
    if not text or len(text) > 10000 or len(body.speaker) > 100 or len(word) > 80:
        raise HTTPException(400, "文字不能为空，或输入过长")
    if word and word not in text:
        raise HTTPException(400, "请填写修正后文字中出现的专有词")
    try:
        store.edit_segment(sid, segment_id, text, body.speaker, body.expected_text, body.expected_speaker)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    remembered = False
    warning = None
    if word:
        try:
            hotwords.add_hotword(word)
            remembered = True
        except OSError:
            warning = "文字已保存，但词表保存失败，请在设置中重试"
    return {"ok": True, "remembered": remembered, "warning": warning}


class SpeakerMergeIn(BaseModel):
    source: str
    target: str


@app.post("/api/sessions/{sid}/speaker-merge")
def merge_session_speakers(sid: str, body: SpeakerMergeIn):
    try:
        count = store.merge_speakers(sid, body.source, body.target)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "count": count}


app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
