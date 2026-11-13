"""ASR 转写管道：m4a → 16k wav → faster-whisper → 分段(带时间戳) → 声纹标注。

支持进度回调（用于前端展示转写进度）。
"""
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from config import WAV_CACHE, WHISPER_LANGUAGE, WHISPER_MODEL
from speaker import SpeakerRecognizer, get_recognizer, resolve_ref_audio


def to_wav_16k(src: Path) -> Path:
    """把任意音频转成 16k 单声道 wav，缓存在本地。"""
    out = WAV_CACHE / (src.stem + "_16k.wav")
    if out.exists():
        return out
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(out)],
        check=True, capture_output=True,
    )
    return out


class Transcriber:
    def __init__(self):
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            # 使用本地模型目录（手动从 hf-mirror 下载），避免 huggingface_hub 网络问题
            model_dir = Path(__file__).resolve().parent.parent / "models" / f"whisper-{WHISPER_MODEL}"
            if not model_dir.is_dir():
                raise RuntimeError(
                    f"Missing Whisper model: {model_dir}. "
                    "Download the matching faster-whisper model as described in README.md."
                )
            self._model = WhisperModel(
                str(model_dir), device="auto", compute_type="int8"
            )
        return self._model

    def transcribe(
        self,
        src: Path,
        progress: Callable[[float, str], None] = None,
    ) -> list[dict]:
        """返回 segments: [{start, end, text}]"""
        model = self._load()
        wav = to_wav_16k(src)

        progress_cb = progress or (lambda *_: None)
        progress_cb(0.0, "converting audio")
        from hotwords import local_hint
        segments_iter, info = model.transcribe(
            str(wav),
            language=WHISPER_LANGUAGE,
            vad_filter=True,
            # 显式 VAD 参数：默认参数在长音频上偶发非确定性返回 0 段（faster-whisper 已知问题），
            # 收紧静音判定 + 语音边缘填充可稳定输出真实语音。
            vad_parameters={
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 200,
            },
            beam_size=5,
            hotwords=local_hint(model.hf_tokenizer),
        )

        # 先收集，便于按真实总时长算进度
        segs = []
        for seg in segments_iter:
            segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
            if info.duration and info.duration > 0:
                progress_cb(min(seg.end / info.duration, 0.95), "transcribing")

        progress_cb(1.0, "done")
        return segs


# 全局单例（懒加载 + 双重检查锁）：whisper small int8 约 500MB，
# 多任务并发时若各自 new Transcriber() 会重复加载多份进内存。全局只保留一份。
_transcriber: "Transcriber | None" = None
_transcriber_lock = threading.Lock()


def get_transcriber() -> "Transcriber":
    global _transcriber
    if _transcriber is None:
        with _transcriber_lock:
            if _transcriber is None:
                _transcriber = Transcriber()
    return _transcriber


def annotate_speakers(
    src: Path, segments: list[dict], recognizer: SpeakerRecognizer, ref_emb,
    progress: Callable[[float, str], None] = None,
) -> list[dict]:
    """对每个分段做声纹二分类，返回带 speaker 字段的 segments。

    性能：整段 wav 一次性读入内存，用 numpy 切片代替逐段 fork ffmpeg
    （旧版一段 10 分钟录音上百段就要 fork 上百次进程）。
    """
    progress = progress or (lambda *_: None)
    if ref_emb is None:
        for s in segments:
            s["speaker"] = "other"  # 无参考声纹时，统一标 other（或留给前端显示 unknown）
        return segments

    wav = to_wav_16k(src)
    import wave

    with wave.open(str(wav), "rb") as f:
        rate = f.getframerate()
        n_frames = f.getnframes()
        samples = np.frombuffer(f.readframes(n_frames), dtype=np.int16).astype(np.float32) / 32768.0

    n = len(segments)
    for i, s in enumerate(segments):
        start_f = int(s["start"] * rate)
        end_f = min(int(s["end"] * rate), n_frames)
        if end_f <= start_f:
            s["speaker"] = "other"
        else:
            speaker, score = recognizer.predict_samples(ref_emb, samples[start_f:end_f])
            s["speaker"] = speaker
            s["score"] = score
        # 声纹阶段占进度 0.95 → 1.0（旧版此阶段不报进度，长录音会卡在 95%）
        progress(0.95 + 0.05 * (i + 1) / n, "labeling speakers")
    return segments


def run_pipeline(
    src: Path,
    progress: Callable[[float, str], None] = None,
) -> dict:
    """完整管道：转写 + 声纹标注。返回 {segments, ref_used}"""
    progress = progress or (lambda *_: None)
    recognizer = get_recognizer()  # 复用全局单例，避免重复加载声纹模型

    # 1. 参考声纹
    ref_path = resolve_ref_audio()
    ref_emb = None
    ref_used = None
    if ref_path is not None:
        progress(0.0, "enrolling voice")
        try:
            ref_emb = recognizer.enroll(ref_path)
            ref_used = str(ref_path)
        except Exception as e:
            print(f"[warn] voice enroll failed: {e}")

    # 2. 转写
    t = get_transcriber()  # 复用全局单例，避免重复加载 whisper 模型
    segments = t.transcribe(src, progress)

    # 3. 声纹标注（只有有参考声纹才做）；传入 progress 让声纹阶段也能报进度
    if ref_emb is not None:
        segments = annotate_speakers(src, segments, recognizer, ref_emb, progress)

    return {"segments": segments, "ref_used": ref_used}
