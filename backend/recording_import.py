"""原生录音完成后的边界校验；不扫描目录、不加载模型、不连接云端。"""
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from config import SELF_RECORDINGS_DIR


class RecordingImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    capture_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    started_at: str = Field(min_length=1, max_length=64)
    ended_at: str = Field(min_length=1, max_length=64)
    interruption_reason: str | None = Field(default=None, max_length=500)
    auto_transcribe: StrictBool = False


def _timestamp(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone required")
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, "录音时间必须是带时区的 ISO8601 时间") from exc


def validate_recording(body: RecordingImportIn) -> dict:
    started, ended = _timestamp(body.started_at), _timestamp(body.ended_at)
    elapsed = (ended - started).total_seconds()
    if elapsed <= 0 or elapsed > 7 * 86400 or ended > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise HTTPException(400, "录音起止时间无效或录音尚未结束")

    # resolve 检查真实目标，不能借 ../ 或符号链接导入目录之外的任意文件。
    try:
        root = SELF_RECORDINGS_DIR.resolve(strict=True)
        path = Path(body.path).expanduser().resolve(strict=True)
        relative = path.relative_to(root)
        if not path.is_file() or path.suffix.lower() != ".m4a":
            raise ValueError("not m4a")
        if any(part.startswith(".") for part in relative.parts) or re.search(
            r"(?:^|[._-])(tmp|temp|temporary|partial|part|inprogress|encoding)(?:[._-]|$)",
            path.stem, re.IGNORECASE,
        ):
            raise ValueError("unfinished filename")
        before = path.stat()
        if before.st_size <= 0:
            raise ValueError("empty audio")
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, "仅能导入本应用录音目录中已完成的非空 m4a 文件") from exc

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-count_packets", "-show_entries",
             "format=format_name,duration:stream=codec_type,duration,nb_read_packets",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if probe.returncode or probe.stderr.strip():
            raise ValueError("invalid audio")
        result = json.loads(probe.stdout)
        container = result.get("format", {})
        if not {"mov", "mp4", "m4a"}.intersection(container.get("format_name", "").split(",")):
            raise ValueError("not m4a container")
        audio = [stream for stream in result.get("streams", []) if stream.get("codec_type") == "audio"]
        if not audio or not any(int(stream.get("nb_read_packets", 0)) > 0 for stream in audio):
            raise ValueError("no audio packets")
        duration = float(container.get("duration", 0))
        if not math.isfinite(duration) or duration <= 0 or duration > elapsed + max(3, elapsed * 0.02):
            raise ValueError("invalid duration")
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("audio still being written")
    except FileNotFoundError as exc:
        raise HTTPException(503, "录音校验工具 ffprobe 不可用或录音已移动") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(400, "录音校验超时，请检查文件是否完整后重试") from exc
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(400, "录音无效、尚未完成或实际时长与录音时间不符") from exc

    return {
        "name": path.name,
        "file_path": str(path),
        "capture_id": body.capture_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        # 与旧记录同样使用 UTC SQLite 时间格式，字符串排序和旧前端继续兼容。
        "created_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "interruption_reason": (body.interruption_reason or "").strip() or None,
        "duration": duration,
    }
