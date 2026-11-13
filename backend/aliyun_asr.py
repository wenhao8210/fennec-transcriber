"""阿里云百炼（DashScope）Fun-ASR 云端转写。

流程：本地文件上传（SDK 自带 OSS 临时空间）→ async_call 提交转写任务
→ 轮询结果 → 解析带说话人分离的分段文本。

注意：说话人分离（diarization）仅支持单声道，上传前需先转单声道。
"""
import json
import os
import tempfile
import subprocess
import time
from http import HTTPStatus
from pathlib import Path
from typing import Callable

import dashscope
from dashscope.audio.asr import Transcription
from dashscope.common.constants import FilePurpose

from config import DASHSCOPE_MODEL, SETTINGS_PATH, WAV_CACHE

TIMEOUT_SEC = 900  # 单文件转写最长等待 15 分钟
SPEAKER_COUNT = 3  # fun-asr 说话人分离参考人数（2-100），会议/多人场景引导算法细分


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(data: dict):
    # Atomic replacement keeps the previous file intact if serialization fails.
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=SETTINGS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_api_key() -> str:
    return (os.environ.get("DASHSCOPE_API_KEY") or load_settings().get("dashscope_api_key") or "").strip()


def get_base_url() -> str:
    return (os.environ.get("DASHSCOPE_BASE_URL") or load_settings().get("dashscope_base_url") or "").strip()


def get_workspace() -> str:
    return (os.environ.get("DASHSCOPE_WORKSPACE") or load_settings().get("dashscope_workspace") or "").strip()


def set_api_key(key: str, base_url: str = "", workspace: str = ""):
    data = load_settings()
    data["dashscope_api_key"] = key.strip()
    if base_url:
        data["dashscope_base_url"] = base_url.strip()
    if workspace:
        data["dashscope_workspace"] = workspace.strip()
    save_settings(data)


def _ensure_configured():
    key = get_api_key()
    if not key:
        raise RuntimeError("未配置阿里云 API Key，请先在设置中填写")
    dashscope.api_key = key
    base_url = get_base_url()
    workspace = get_workspace()
    if base_url:
        dashscope.base_http_api_url = base_url
    return workspace or None


def _to_mono_wav(src: Path) -> Path:
    """转成 16k 单声道 wav（dashscope 说话人分离要求单声道）。"""
    out = WAV_CACHE / (src.stem + "_aliyun_mono.wav")
    if not out.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(out)],
            check=True, capture_output=True,
        )
    return out


def _upload(src: Path) -> str:
    """上传本地文件，返回公网可访问的 URL。"""
    result = dashscope.Files.upload(file_path=str(src), purpose=FilePurpose.assistants)
    if result.status_code != HTTPStatus.OK:
        raise RuntimeError(f"上传失败: {result.code} {result.message}")
    file_id = result.output["uploaded_files"][0]["file_id"]
    file_res = dashscope.Files.get(file_id)
    url = file_res.output["url"]
    return url


def _submit_task(url: str, vocabulary_id: str | None = None, speaker_count: int | None = None) -> str:
    """通过 HTTP API 提交 fun-asr 转写任务，返回 task_id。

    fun-asr 热词用 parameters.vocabulary_id（预编译热词表 ID），
    说话人分离用 diarization_enabled + speaker_count（期望人数提示，2-100）。
    """
    import urllib.request

    base = get_base_url()
    if not base:
        base = "https://dashscope.aliyuncs.com/api/v1"
    submit_url = base.rstrip("/") + "/services/audio/asr/transcription"

    parameters = {
        "channel_id": [0],
        "language_hints": ["zh", "en"],
        "diarization_enabled": True,
        "timestamp_alignment_enabled": True,
    }
    if vocabulary_id:
        parameters["vocabulary_id"] = vocabulary_id
    if speaker_count:
        parameters["speaker_count"] = speaker_count

    body = json.dumps({
        "model": DASHSCOPE_MODEL,
        "input": {"file_urls": [url]},
        "parameters": parameters,
    }).encode("utf-8")

    req = urllib.request.Request(
        submit_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
    )
    import urllib.error
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        code = ""
        try:
            code = json.loads(raw).get("code") or ""
        except Exception:
            pass
        if "Unpurchased" in code or "AccessDenied" in code:
            raise RuntimeError(
                "阿里云拒绝调用该模型（AccessDenied.Unpurchased）：通常是【账户余额不足/欠费】或模型未开通。"
                "请到阿里云费用中心充值，或到百炼控制台确认 fun-asr 已开通。"
            ) from e
        raise RuntimeError(f"提交转写任务失败 HTTP {e.code}：{raw[:200] or '无响应体'}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"提交转写任务网络错误：{getattr(e, 'reason', e)}") from e
    out = data.get("output", {})
    task_id = out.get("task_id")
    if not task_id:
        raise RuntimeError(f"任务提交失败: {data.get('code')} {data.get('message')}")
    return task_id


def transcribe_cloud(
    src: Path,
    progress: Callable[[float, str], None] = None,
) -> dict:
    """云端转写（Paraformer + 说话人分离）。返回 {segments, engine, ref_used}"""
    progress = progress or (lambda *_: None)
    workspace = _ensure_configured()

    # 1. 转单声道并上传
    progress(0.05, "uploading")
    mono = _to_mono_wav(src)
    url = _upload(mono)

    # 2. 提交转写任务（开启说话人分离 + 热词 + 中英混合）
    #    fun-asr 系列不支持 SDK Transcription.async_call 的 phrase_id（那是 paraformer 的
    #    resources 机制），须走 HTTP API 直连，热词用 parameters.vocabulary_id。
    progress(0.15, "submitting")
    phrase_id = None
    try:
        from hotwords import get_phrase_id
        phrase_id = get_phrase_id()
    except Exception:
        pass
    task_id = _submit_task(url=url, vocabulary_id=phrase_id, speaker_count=SPEAKER_COUNT)

    # 3. 轮询等待
    start = time.time()
    while True:
        if time.time() - start > TIMEOUT_SEC:
            raise TimeoutError("云端转写超时（15 分钟）")
        progress(min(0.2 + (time.time() - start) / TIMEOUT_SEC * 0.7, 0.9), "transcribing")
        fetch = Transcription.fetch(task=task_id)
        status = fetch.output.get("task_status")
        if status == "SUCCEEDED":
            break
        if status == "FAILED":
            raise RuntimeError(f"转写任务失败: {fetch.output.get('message', 'unknown')}")
        time.sleep(3)

    # 4. 拉取结果（transcription_url → JSON）
    progress(0.92, "parsing")
    results = fetch.output.get("results", [])
    if not results:
        raise RuntimeError("转写结果为空")
    transcription_url = results[0].get("transcription_url")
    import urllib.request
    with urllib.request.urlopen(transcription_url) as f:
        data = json.loads(f.read().decode("utf-8"))

    # 5. 解析成 segments: [{start, end, speaker, text}]
    segments = []
    for item in data.get("transcripts", []):
        sentences = item.get("sentences", [])
        if not sentences:
            # 整段文本，按 speaker 归并
            text = item.get("text", "")
            if text.strip():
                segments.append({
                    "start": 0.0, "end": 0.0,
                    "speaker": f"speaker_{item.get('speaker_id', 0)}",
                    "text": text.strip(),
                })
            continue
        for s in sentences:
            speaker = s.get("speaker_id")
            text = s.get("text", "").strip()
            if not text:
                continue
            segments.append({
                "start": round(float(s.get("begin_time", 0)) / 1000, 2),
                "end": round(float(s.get("end_time", 0)) / 1000, 2),
                "speaker": f"speaker_{speaker}" if speaker is not None else "speaker_0",
                "text": text,
            })

    progress(1.0, "done")
    return {"segments": segments, "engine": "aliyun-paraformer", "ref_used": None}
