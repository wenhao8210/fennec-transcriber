"""声纹说话人识别：ECAPA-TDNN embedding + cosine 相似度，区分"自己/他人"。

用 sherpa-onnx 的 speaker embedding 提取器：
- 输入参考音频（自己的声音，>=5s），得到参考 embedding
- 对转写后的每个分段音频，提取 embedding，与参考做 cosine 相似度
- 相似度 >= 阈值 => 自己(self)，否则 => 他人(other)
"""
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from config import WAV_CACHE, USER_VOICE_REF, VOICE_PROFILE_SAMPLE, SPEAKER_THRESHOLD

# sherpa-onnx 使用的 3D-Speaker ERes2Net 模型，从上游 Hugging Face 仓库下载。
_MODEL_BASE = "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main"
_MODEL_URLS = {
    "model.onnx": f"{_MODEL_BASE}/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx",
}
_MODEL_DIR = WAV_CACHE / "speaker_model"


def _ensure_model() -> Path:
    model_file = _MODEL_DIR / "model.onnx"
    if model_file.exists() and model_file.stat().st_size > 1_000_000:
        return model_file
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request

    url = _MODEL_URLS["model.onnx"]
    print(f"[speaker] downloading model: {url}")
    # 用 curl 直链下载，绕过 huggingface_hub 的代理问题
    subprocess.run(
        ["curl", "-sL", "--retry", "3", "-o", str(model_file), url],
        check=True, capture_output=True,
    )
    print(f"[speaker] model ready: {model_file} ({model_file.stat().st_size/1e6:.1f} MB)")
    return model_file


def _ensure_sample_rate(wav_path: Path) -> Path:
    """sherpa-onnx 期望 16k 单声道 wav；不足则重采样。"""
    out = wav_path
    try:
        import wave

        with wave.open(str(wav_path), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
        if rate == 16000 and channels == 1:
            return out
        out = WAV_CACHE / (wav_path.stem + "_16k.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-ar", "16000", "-ac", "1", str(out)],
            check=True, capture_output=True,
        )
    except Exception:
        pass
    return out


def _segment_wav(src: Path, start: float, end: float, tag: str) -> Path:
    """从源音频切出 [start,end] 秒一段，返回 16k 单声道临时 wav。"""
    out = WAV_CACHE / f"seg_{tag}_{int(start * 1000)}_{int(end * 1000)}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ss", str(start), "-to", str(end),
         "-ar", "16000", "-ac", "1", str(out)],
        check=True, capture_output=True,
    )
    return out


class SpeakerRecognizer:
    def __init__(self):
        self._extractor = None
        self._model_path = None

    def _load(self):
        if self._extractor is None:
            import sherpa_onnx

            self._model_path = str(_ensure_model())
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self._model_path,
                num_threads=2,
                debug=False,
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return self._extractor

    def _embed(self, wav_path: Path) -> np.ndarray:
        extractor = self._load()
        wav = _ensure_sample_rate(wav_path)
        try:
            import wave

            with wave.open(str(wav), "rb") as f:
                frames = f.readframes(f.getnframes())
                samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            samples = np.zeros(16000, dtype=np.float32)

        assert extractor.dim > 0, "extractor not initialized"
        stream = extractor.create_stream()
        # sherpa-onnx 的 SpeakerEmbeddingExtractor 期望逐帧特征；为简化，
        # 直接用 read_audio + compute。此处按 SDK 提供的方式：
        stream.accept_waveform(16000, samples)
        tail_padding = np.zeros(int(0.5 * 16000), dtype=np.float32)
        stream.accept_waveform(16000, tail_padding)
        stream.input_finished()
        if not extractor.is_ready(stream):
            return np.zeros(extractor.dim, dtype=np.float32)
        return extractor.compute(stream)

    def enroll(self, ref_path: Path) -> np.ndarray:
        """注册参考声纹，返回 embedding。"""
        return self._embed(ref_path)

    def predict(self, ref_emb: np.ndarray, seg_wav: Path) -> tuple[str, float]:
        """对一段音频做二分类。返回 (self|other, score)。"""
        emb = self._embed(seg_wav)
        if np.linalg.norm(ref_emb) == 0 or np.linalg.norm(emb) == 0:
            return "other", 0.0
        score = float(np.dot(ref_emb, emb) / (np.linalg.norm(ref_emb) * np.linalg.norm(emb)))
        return ("self" if score >= SPEAKER_THRESHOLD else "other", round(score, 4))

    def _embed_samples(self, samples: np.ndarray) -> np.ndarray:
        """对内存中的 16k 单声道 float32 采样直接提取 embedding，不经过临时文件。"""
        extractor = self._load()
        assert extractor.dim > 0, "extractor not initialized"
        stream = extractor.create_stream()
        stream.accept_waveform(16000, samples)
        tail_padding = np.zeros(int(0.5 * 16000), dtype=np.float32)
        stream.accept_waveform(16000, tail_padding)
        stream.input_finished()
        if not extractor.is_ready(stream):
            return np.zeros(extractor.dim, dtype=np.float32)
        # sherpa-onnx 的 compute 返回 list，统一成 ndarray 保持类型一致
        return np.asarray(extractor.compute(stream), dtype=np.float32)

    def predict_samples(self, ref_emb: np.ndarray, samples: np.ndarray) -> tuple[str, float]:
        """直接对内存采样做二分类（跳过读文件与 fork ffmpeg 切片进程）。"""
        emb = self._embed_samples(samples)
        if np.linalg.norm(ref_emb) == 0 or np.linalg.norm(emb) == 0:
            return "other", 0.0
        score = float(np.dot(ref_emb, emb) / (np.linalg.norm(ref_emb) * np.linalg.norm(emb)))
        return ("self" if score >= SPEAKER_THRESHOLD else "other", round(score, 4))

    def embed_segment(self, src: Path, start: float, end: float, tag: str) -> np.ndarray:
        """从整段音频切出 [start,end] 并提取该段 embedding。"""
        wav = _segment_wav(src, start, end, tag)
        try:
            return self._embed(wav)
        finally:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass


# 全局单例（懒加载）
_recognizer = None


def get_recognizer() -> SpeakerRecognizer:
    global _recognizer
    if _recognizer is None:
        _recognizer = SpeakerRecognizer()
    return _recognizer


def resolve_ref_audio() -> Path | None:
    """返回参考声纹音频路径：优先用户自录，其次 Fennec 样本。"""
    if USER_VOICE_REF.exists():
        return USER_VOICE_REF
    if VOICE_PROFILE_SAMPLE.exists():
        return VOICE_PROFILE_SAMPLE
    return None


def match_cloud_speakers(src: Path, segments: list, profiles: list) -> dict:
    """云端说话人分离后，按 speaker_N 聚类，与声纹库匹配，返回 {speaker_N: 名字}。

    profiles: [{name, embedding(bytes)}]，embedding 由 store 读出。
    同一 speaker 的所有段取平均 embedding 后与每个参考比对，取最高分且达到阈值才认定。
    """
    if not profiles:
        return {}
    refs = []
    for p in profiles:
        try:
            emb = np.frombuffer(p["embedding"], dtype=np.float32).astype(np.float64)
        except Exception:
            continue
        if emb.size == 0:
            continue
        refs.append((p["name"], emb))
    if not refs:
        return {}

    recognizer = get_recognizer()
    by_speaker: dict[str, list] = {}
    for seg in segments:
        sp = seg.get("speaker") or ""
        if not sp.startswith("speaker_"):
            continue
        if seg.get("end", 0) - seg.get("start", 0) <= 0.4:
            continue
        by_speaker.setdefault(sp, []).append(seg)

    result = {}
    for sp, segs in by_speaker.items():
        total = None
        cnt = 0
        for i, seg in enumerate(segs):
            try:
                raw = recognizer.embed_segment(src, seg["start"], seg["end"], f"{sp}_{i}")
            except Exception:
                continue
            if raw is None:
                continue
            emb = np.asarray(raw, dtype=np.float64)
            if np.linalg.norm(emb) > 0:
                total = emb if total is None else total + emb
                cnt += 1
        if total is None or cnt == 0:
            continue
        avg = total / cnt
        n = np.linalg.norm(avg)
        if n == 0:
            continue
        avg = avg / n
        best_name, best_score = None, -1.0
        for name, ref in refs:
            rn = np.linalg.norm(ref)
            if rn == 0:
                continue
            score = float(np.dot(ref, avg) / rn)
            if score > best_score:
                best_name, best_score = name, score
        if best_name and best_score >= SPEAKER_THRESHOLD:
            result[sp] = best_name
    return result
