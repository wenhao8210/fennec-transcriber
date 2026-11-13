"""音频能量分析：用 ffmpeg volumedetect 检测平均音量（dB），标记低内容音频。

不转码、逐文件几秒内出结果；平均音量过低 => 疑似静音/无有效内容。
"""
import re
import subprocess
from pathlib import Path

# 平均音量低于此值（dB）视为"低内容"（正常语音通常 -20 ~ -35dB，静音约 -50+）
LOW_ENERGY_THRESHOLD_DB = -45.0


def analyze_energy(src: Path) -> float | None:
    """返回平均音量 dB；失败返回 None。"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-i", str(src), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", out.stderr)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def is_low_energy(energy_db: float | None) -> bool:
    return energy_db is None or energy_db < LOW_ENERGY_THRESHOLD_DB


# ---------- 人声占比（Silero VAD）：比平均音量可靠 ----------
# 为什么不用平均音量：平均 dB 会被环境音/键盘声顶高，分不出"有人说话"和"只有底噪"。
# 2026-09-22 实测 700 条：人声占比 <5% 有 288 条（41%），而 energy_db < -45 只抓到 4 条。
# 模型来自已安装的 faster_whisper（自带 silero_vad_v6.onnx），零额外依赖、零下载。

_VAD_OPTIONS = None


def _vad_options():
    global _VAD_OPTIONS
    if _VAD_OPTIONS is None:
        from faster_whisper.vad import VadOptions
        _VAD_OPTIONS = VadOptions()
    return _VAD_OPTIONS


def analyze_speech_ratio(src: Path) -> float | None:
    """返回「人声占比」0~1（有人说话的时长 / 总时长）；失败返回 None。

    实测约 0.6s/条（10 分钟录音）。
    """
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps

        audio = decode_audio(str(src), sampling_rate=16000)
        total = len(audio) / 16000
        if total <= 0:
            return 0.0
        stamps = get_speech_timestamps(audio, _vad_options(), sampling_rate=16000)
        speech = sum(x["end"] - x["start"] for x in stamps) / 16000
        return max(0.0, min(1.0, speech / total))
    except Exception:
        return None


# 人声占比 → 1~5 分。档位按 2026-09-22 全量扫描的实测分布定（700 条）。
#   5 = 一直有人说话(≥60%)  4 = 间歇有人(30~60%)  3 = 大半是空的(10~30%)
#   2 = 偶尔一声(1~10%)     1 = 全程没人说话(0%)
SPEECH_SCORE_BANDS = ((0.60, 5), (0.30, 4), (0.10, 3), (0.01, 2))


def speech_score(ratio: float | None) -> int | None:
    """人声占比 → 1~5 分；ratio 为 None（未分析/文件已清理）返回 None。"""
    if ratio is None:
        return None
    for lower, score in SPEECH_SCORE_BANDS:
        if ratio >= lower:
            return score
    return 1


def is_low_content(ratio: float | None) -> bool:
    """≤2 分视为「低内容」。None 不算（未分析，不能当结论）。"""
    score = speech_score(ratio)
    return score is not None and score <= 2
