"""热词管理：本地维护热词列表，同步到阿里云 ASR 热词表。

设计（2026-08-23 改为 fun-asr 兼容）：
- 本地存储 data/hotwords.json，方便随时增删
- 通过 VocabularyService 创建/更新热词表，返回 vocabulary_id（fun-asr 系列）
- 转写时把 vocabulary_id 传给云端转写（HTTP API 的 parameters.vocabulary_id）
- 权重策略：人名默认 3（避免全 5 过度偏向）
"""
import json
import threading
from functools import wraps
from pathlib import Path
from typing import Callable

from dashscope.audio.asr import VocabularyService
from http import HTTPStatus

from config import DASHSCOPE_MODEL, DATA_DIR

HOTWORDS_PATH = DATA_DIR / "hotwords.json"
# 同步状态缓存（vocabulary_id 存这里，避免每次重启都新建词表）
HOTWORDS_STATE_PATH = DATA_DIR / "hotwords_state.json"
VOCAB_PREFIX = "fennec"  # 热词表前缀（小写字母数字）


_word_lock = threading.RLock()


def _locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _word_lock:
            return fn(*args, **kwargs)
    return wrapped


_word_lock = threading.RLock()


def _locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _word_lock:
            return fn(*args, **kwargs)
    return wrapped


def _load() -> dict:
    if HOTWORDS_PATH.exists():
        try:
            return json.loads(HOTWORDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    temporary = HOTWORDS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(HOTWORDS_PATH)


def _load_state() -> dict:
    if HOTWORDS_STATE_PATH.exists():
        try:
            return json.loads(HOTWORDS_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(data: dict):
    HOTWORDS_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_hotwords() -> list:
    """返回热词列表 [{word, weight}]。"""
    data = _load()
    return [{"word": w, "weight": v} for w, v in data.items()]


@_locked
def add_hotword(word: str, weight: int = 3) -> bool:
    """添加热词（重复则忽略）。返回是否有变化。"""
    word = word.strip()
    if not word:
        return False
    data = _load()
    if word in data:
        return False
    data[word] = weight
    _save(data)
    return True


@_locked
def remove_hotword(word: str) -> bool:
    data = _load()
    if word not in data:
        return False
    del data[word]
    _save(data)
    return True


@_locked
def set_hotwords(words: list, weight: int = 3):
    """批量设置热词（保留原有权重，新增用默认权重）。"""
    data = _load()
    changed = False
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w not in data:
            data[w] = weight
            changed = True
    if changed:
        _save(data)
    return changed


def sync_to_aliyun(progress: Callable = None) -> dict:
    """同步本地热词到阿里云（fun-asr VocabularyService）。返回同步结果。"""
    progress = progress or (lambda *_: None)
    from aliyun_asr import _ensure_configured
    workspace = _ensure_configured()

    phrases = _load()
    if not phrases:
        return {"ok": True, "vocabulary_id": None, "count": 0, "msg": "无热词"}

    state = _load_state()
    vocabulary_id = state.get("vocabulary_id")

    progress(0.1, "syncing-hotwords")
    try:
        svc = VocabularyService()
        # 转为 SDK 需要的列表格式
        vocab_list = [{"text": w, "weight": v} for w, v in phrases.items()]
        if not vocabulary_id:
            # 首次创建
            vocabulary_id = svc.create_vocabulary(
                target_model=DASHSCOPE_MODEL,
                prefix=VOCAB_PREFIX,
                vocabulary=vocab_list,
            )
            if not vocabulary_id:
                return {"ok": False, "error": "创建热词未返回 vocabulary_id"}
            state["vocabulary_id"] = vocabulary_id
            _save_state(state)
        else:
            # 已存在则更新（覆盖全部词）
            svc.update_vocabulary(vocabulary_id=vocabulary_id, vocabulary=vocab_list)
    except Exception as e:
        return {"ok": False, "error": f"同步热词异常: {e}"}

    progress(1.0, "done")
    return {"ok": True, "vocabulary_id": vocabulary_id, "count": len(phrases)}


def get_phrase_id() -> str | None:
    """返回当前热词表 ID（兼容旧字段名，供转写调用）。"""
    return _load_state().get("vocabulary_id") or _load_state().get("phrase_id")


def local_hint(tokenizer, max_tokens: int = 160) -> str | None:
    """按真实 tokenizer 限制提示长度，避免词表挤占 Whisper 上下文。"""
    words = sorted(list_hotwords(), key=lambda h: -h["weight"])
    chosen = []
    for item in words:
        candidate = ", ".join(chosen + [item["word"]])
        if len(tokenizer.encode(candidate).ids) <= max_tokens:
            chosen.append(item["word"])
    return ", ".join(chosen) or None
