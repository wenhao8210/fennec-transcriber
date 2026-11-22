"""ASR Studio 文件转写适配器；任务先落盘，重试只查询已有任务。"""
import fcntl
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from config import DATA_DIR

TASKS_DIR = DATA_DIR / "tinet_tasks"
WAIT_TIMEOUT = 1800
POLL_INTERVAL = 1
PENDING = {"uploaded", "processing"}
DEFAULT_SERVER = ""


def configuration() -> tuple[str, str]:
    config = {}
    path = Path.home() / ".asr-studio" / "config.json"
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    server = (os.environ.get("FENNEC_TINET_ASR_SERVER") or os.environ.get("ASR_SERVER")
              or config.get("server") or DEFAULT_SERVER).rstrip("/")
    parsed = urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("请通过 FENNEC_TINET_ASR_SERVER 或 ASR Studio 配置有效的服务地址")
    engine = os.environ.get("FENNEC_TINET_ASR_ENGINE") or os.environ.get("ASR_ENGINE") or config.get("engine") or "doubao"
    if engine not in {"doubao", "fun_asr", "tencent", "tencent_public"}:
        raise RuntimeError("天润识别引擎配置无效")
    return server, engine


def _checkpoint(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _request(session, server, method, path, **kwargs):
    try:
        response = session.request(method, server + "/api" + path, timeout=(15, 120), allow_redirects=False, **kwargs)
        if not response.ok or 300 <= response.status_code < 400:
            raise RuntimeError(f"天润服务请求失败（HTTP {response.status_code}）")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except (requests.RequestException, ValueError) as exc:
        # 不自动重发 POST；上传失败也可能已在远端创建。
        raise RuntimeError("天润网络或响应异常；已记录任务可重试查询，上传结果不明时请先核对任务中心") from exc


def _milliseconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("分句时间无效")
    return float(value)


def _segment(start, end, text, speaker):
    start, end = _milliseconds(start), _milliseconds(end)
    if start < 0 or end <= start or not isinstance(text, str) or not text.strip():
        raise ValueError("分句内容或时间无效")
    return {"start": start / 1000, "end": end / 1000, "text": text.strip(), "speaker": speaker}


def details_segments(details: dict, channel: int) -> list[dict]:
    """服务 TranscriptionSegmentOut 的 startMs/endMs 均为毫秒。"""
    segments = []
    items = details.get("segments")
    if not isinstance(items, list):
        raise ValueError("天润分句响应无效")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("天润分句响应无效")
        if not item.get("text", "").strip():
            continue
        segments.append(_segment(item.get("startMs"), item.get("endMs"), item["text"], f"tinet_channel_{channel}"))
    if not segments and str(details.get("resultText") or "").strip():
        raise ValueError("天润返回文本但缺少有效分句，未覆盖原转写")
    return segments


def recover_doubao(details: dict, raw: dict, channel: int, expected_duration=None) -> list[dict]:
    """只恢复完整发送且处理至尾的豆包结束等待超时，保留远端失败状态。"""
    message = str(details.get("errorMessage") or "").lower()
    if details.get("engineId") != "doubao" or details.get("status") != "failed" or "timed out waiting for the next audio packet" not in message:
        raise ValueError("天润任务未完成，不能恢复")
    packets = raw.get("audioPackets") or []
    if not packets or packets[-1].get("isLast") is not True:
        raise ValueError("音频末包未完整发送，未覆盖原转写")
    duration = _milliseconds(expected_duration or details.get("durationMs"))
    sent_end = _milliseconds(packets[-1].get("audioEndMs"))
    if duration <= 0 or abs(sent_end - duration) > 10:
        raise ValueError("音频发送未覆盖录音结尾，未覆盖原转写")
    utterances = {}
    provisional = []
    final_ranges = []
    last_duration = None
    for response_index, response in enumerate(raw.get("responses", [])):
        payload = (response.get("decoded") or {}).get("raw") or {}
        result = payload.get("result") or {}
        if payload.get("audio_info", {}).get("duration") is not None:
            last_duration = _milliseconds(payload["audio_info"]["duration"])
        for item in result.get("utterances", []):
            if item.get("definite") is True:
                segment = _segment(item.get("start_time"), item.get("end_time"), item.get("text"), f"tinet_channel_{channel}")
                if segment["end"] * 1000 > duration + 10:
                    raise ValueError("恢复分句超出录音范围")
                previous = utterances.get(segment["start"])
                if previous is not None and previous != segment:
                    raise ValueError("定稿分句发生冲突，未覆盖原转写")
                utterances[segment["start"]] = segment
                final_ranges.append((response_index, segment["start"], segment["end"]))
            elif str(item.get("text") or "").strip():
                segment = _segment(item.get("start_time"), item.get("end_time"), item["text"], f"tinet_channel_{channel}")
                provisional.append((response_index, segment["start"], segment["end"]))
    if last_duration is None or abs(last_duration - duration) > 10 or not utterances:
        raise ValueError("天润处理未覆盖录音结尾，未覆盖原转写")
    # 已处理至尾不代表最后一句已定稿；中间稿必须被后续定稿完整覆盖。
    # 不把旧定稿当作新中间稿的确认，也不静默丢弃未完成尾句。
    for response_index, start, end in provisional:
        if not any(index > response_index and final_start <= start and final_end >= end
                   for index, final_start, final_end in final_ranges):
            raise ValueError("仍有未定稿分句，恢复不完整，未覆盖原转写")
    segments = sorted(utterances.values(), key=lambda item: (item["start"], item["end"]))
    for left, right in zip(segments, segments[1:]):
        if left["end"] > right["start"] + .01:
            raise ValueError("恢复分句时间重叠，未覆盖原转写")
    return segments


def transcribe_tinet(src: Path, progress=None) -> dict:
    progress = progress or (lambda *_: None)
    server, engine = configuration()
    digest = hashlib.sha256()
    # 同一录音移动到归档后仍复用远程任务，避免再次上传。
    digest.update(f"{server}\n{engine}\n".encode())
    with src.open("rb") as audio:
        for block in iter(lambda: audio.read(1024 * 1024), b""):
            digest.update(block)
    path = TASKS_DIR / (digest.hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 相同内容共用任务记录；只锁这份录音，避免并行上传同一文件。
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _transcribe_with_record(src, progress, server, engine, path)


def _transcribe_with_record(src: Path, progress, server: str, engine: str, path: Path) -> dict:
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
    else:
        record = {"server": server, "engine": engine, "file": str(src.resolve()), "channels": []}
    session = requests.Session()
    token = os.environ.get("ASR_TOKEN")
    if token:
        if urlsplit(server).scheme != "https" and urlsplit(server).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("携带令牌访问远程天润服务必须使用 HTTPS")
        session.headers["Authorization"] = "Bearer " + token
    try:
        if not record["channels"]:
            progress(.02, "检查天润音频声道")
            with src.open("rb") as audio:
                inspected = _request(session, server, "POST", "/transcriptions/inspect", files={"file": (src.name, audio)})
            count = inspected.get("sourceChannels")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise RuntimeError("天润返回的声道数量无效")
            record["duration_ms"] = inspected.get("durationMs")
            record["channels"] = [{"channel": n, "status": "unsubmitted"} for n in range(1, count + 1)]
            _checkpoint(path, record)

        segments = []
        for index, task in enumerate(record["channels"]):
            channel = task["channel"]
            if not task.get("id"):
                if task["status"] != "unsubmitted":
                    raise RuntimeError(f"声道 {channel} 上传结果不明，请核对天润任务中心；记录：{path.name}")
                task["status"] = "creating"
                _checkpoint(path, record)
                progress((index + .05) / len(record["channels"]), f"上传天润声道 {channel}")
                with src.open("rb") as audio:
                    created = _request(session, server, "POST", "/transcriptions/file", files={"file": (src.name, audio)},
                                       data={"engine_id": engine, "language": "zh", "speed": "4", "source_channel": str(channel)})
                if not isinstance(created.get("id"), str) or not created["id"]:
                    raise RuntimeError("天润未返回任务 ID，请核对任务中心后继续")
                task.update(id=created["id"], status=created.get("status", "uploaded"))
                _checkpoint(path, record)

            task_id = quote(task["id"], safe="")
            deadline = time.monotonic() + WAIT_TIMEOUT
            while True:
                remote = _request(session, server, "GET", f"/transcriptions/{task_id}")
                task["status"] = remote.get("status")
                _checkpoint(path, record)
                if remote.get("status") not in PENDING:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"天润等待超时，任务 {task['id']} 仍可查询；重试将继续等待，不重新上传")
                progress((index + .2) / len(record["channels"]), f"天润声道 {channel} 转写中")
                time.sleep(POLL_INTERVAL)
            details = _request(session, server, "GET", f"/transcriptions/{task_id}/details.json")
            task["remote_status"] = remote.get("status")
            task["error_code"] = remote.get("errorCode")
            task["error_message"] = remote.get("errorMessage")
            _checkpoint(path, record)
            if remote.get("status") == "completed":
                channel_segments = details_segments(details, channel)
                if not channel_segments and str(remote.get("resultText") or "").strip():
                    raise ValueError("天润返回文本但缺少有效分句，未覆盖原转写")
                task["local_result"] = "completed"
            else:
                # 异常回包不可作为远端成功；仅完整豆包末尾等待超时可提取已定稿句子。
                if details.get("engineId") != "doubao" or details.get("status") != "failed" or "timed out waiting for the next audio packet" not in str(details.get("errorMessage") or "").lower():
                    raise RuntimeError(f"天润任务 {task['id']} 未完成（{remote.get('status') or '未知状态'}），未覆盖原转写")
                raw = _request(session, server, "GET", f"/transcriptions/{task_id}/raw.json")
                channel_segments = recover_doubao(details, raw, channel, record.get("duration_ms"))
                task["local_result"] = "recovered"
                task["recovery_note"] = "远端仍为失败；末音频包已发送且处理时长覆盖录音结尾，从 definite=true 回包完整恢复分句"
            _checkpoint(path, record)
            segments.extend(channel_segments)
        progress(1, "done")
        return {"segments": sorted(segments, key=lambda s: (s["start"], s["speaker"])), "ref_used": None}
    except Exception:
        _checkpoint(path, record)
        raise
    finally:
        session.close()
