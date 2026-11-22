"""全局配置：Fennec 数据目录、模型、声纹阈值等。

所有关键项支持环境变量覆盖（FENNEC_* 前缀），便于换机器或临时调整而不改代码。
"""
import os
from pathlib import Path

# Fennec 应用的数据目录（录音源）
FENNEC_DATA_DIR = Path(os.environ.get(
    "FENNEC_DATA_DIR",
    str(Path.home() / "Library/Application Support/com.kkapp.fennec/datas"),
)).expanduser()
SESSIONS_DIR = FENNEC_DATA_DIR / "Sessions"
RECORDINGS_DIR = FENNEC_DATA_DIR / "recordings"

# 本应用原生录音；与第三方 Fennec 的扫描目录分开，只通过完成事件导入。
SELF_RECORDINGS_DIR = Path(os.environ.get(
    "FENNEC_RECORDINGS_DIR",
    str(Path.home() / "Library/Application Support/local.fennec-transcriber/Recordings"),
)).expanduser()

# 声纹参考：优先复用 Fennec 的声纹样本，没有则提示用户录制
VOICE_PROFILE_SAMPLE = FENNEC_DATA_DIR / "voice_profile_sample.wav"

# 本项目自己的数据目录（转写结果、缓存、用户参考声纹）
DATA_DIR = Path(os.environ.get(
    "FENNEC_APP_DATA_DIR", os.environ.get("FENNEC_STORAGE_DIR", str(Path(__file__).resolve().parent.parent / "data")),
)).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "transcripts.db"
WAV_CACHE = DATA_DIR / "wav_cache"
WAV_CACHE.mkdir(parents=True, exist_ok=True)
USER_VOICE_REF = DATA_DIR / "my_voice.wav"  # 用户自己录的参考声纹（若不想用 Fennec 样本）

# 多说话人声纹库：每人一段参考音频（>=5s），用于转写后自动识别"这是谁"
VOICE_PROFILES_DIR = DATA_DIR / "voice_profiles"
VOICE_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

# 归档区：Fennec 有 15 天录音保留期（recorder_settings.json 的 recording_retention_days），
# 到期会把源文件移到它自己的 datas/Trash/recordings_expired_*，导致转写还在但音频播不了。
# 收藏过的录音会在收藏时立即复制一份到这里，之后音频播放/转写都从这里读，不再依赖 Fennec。
ARCHIVE_DIR = DATA_DIR / "archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# ASR 模型
WHISPER_MODEL = os.environ.get("FENNEC_WHISPER_MODEL", "small")  # small/medium：中文够用；追求准确可换 medium
WHISPER_LANGUAGE = os.environ.get("FENNEC_WHISPER_LANGUAGE", "zh")

# 声纹相似度阈值（cosine），>= 判为自己
SPEAKER_THRESHOLD = float(os.environ.get("FENNEC_SPEAKER_THRESHOLD", "0.55"))

# 阿里云百炼（DashScope）配置：API Key 存本地 settings.json，不硬编码
SETTINGS_PATH = DATA_DIR / "settings.json"
DASHSCOPE_MODEL = "fun-asr"   # 说话人分离 + 热词（fun-asr 官方支持多人分离；paraformer-v2 为旧代）

# 支持的音频扩展名
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".ogg", ".flac"}  # 不含 .wav：Fennec 录音目录下的 wav 通常是坏文件/不可读，不纳入扫描
