"""SQLite 存储层：会话状态 + 转写分段。"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH

_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                source TEXT NOT NULL,
                duration REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                error TEXT,
                speaker_ref TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                speaker TEXT,
                text TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
            CREATE INDEX IF NOT EXISTS idx_segments_position ON segments(session_id, start, id);
            CREATE TABLE IF NOT EXISTS speaker_labels (
                session_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY (session_id, speaker)
            );
            CREATE TABLE IF NOT EXISTS deleted_files (
                file_path TEXT PRIMARY KEY,
                deleted_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS voice_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ref_path TEXT NOT NULL,
                embedding BLOB,
                created_at TEXT DEFAULT (datetime('now'))
            );
            -- 录音标签（多对多）：用于事后按主题/客户/参会人检索
            CREATE TABLE IF NOT EXISTS session_tags (
                session_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (session_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON session_tags(tag);
            """
        )
        # 迁移：老库补 energy 列（平均音量 dB，用于低内容标记）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "energy_db" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN energy_db REAL")
        if "favorite" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN favorite INTEGER DEFAULT 0")
        if "engine" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN engine TEXT")
        # 人声占比（Silero VAD，0~1）——低内容判定的主口径（2026-09-22 起）
        if "speech_ratio" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN speech_ratio REAL")
        # 归档副本路径（Fennec 15 天保留期清理源文件后，仍能从归档区播放/转写）
        if "archived_path" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN archived_path TEXT")
        # 原生录音元数据：旧记录保持 NULL，既有内容、标签和时间均不改写。
        for column in ("capture_id", "started_at", "ended_at", "interruption_reason"):
            if column not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_capture "
            "ON sessions(capture_id) WHERE capture_id IS NOT NULL"
        )
        # 迁移：重命名后 file_path 会变，sid 用 md5(file_path) 计算会不一致，
        # 因此加唯一索引，扫描时按 file_path 去重，避免重命名后产生重复记录。
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_path ON sessions(file_path)")
        except Exception:
            pass


def upsert_session(session_id: str, name: str, file_path: str, source: str):
    """按 file_path 去重：已存在则忽略（重命名后 file_path 变化会重新计算 sid）。"""
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE file_path = ?", (file_path,)
        ).fetchone()
        if row:
            return row["id"]
        conn.execute(
            """INSERT OR IGNORE INTO sessions (id, name, file_path, source)
               VALUES (?, ?, ?, ?)""",
            (session_id, name, file_path, source),
        )
        return session_id


def import_recording(*, name: str, file_path: str, capture_id: str,
                     started_at: str, ended_at: str, created_at: str,
                     interruption_reason: str | None, duration: float,
                     source: str = "self_recorder") -> tuple[str, bool]:
    """原生录音原子登记；跨线程/进程并发重试只创建一条记录。"""
    if source not in {"self_recorder", "self_clip"}:
        raise ValueError("unsupported recording source")
    with _lock, _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, capture_id, file_path, source FROM sessions "
            "WHERE capture_id = ? OR file_path = ?", (capture_id, file_path),
        ).fetchall()
        if rows:
            if len(rows) != 1 or any(
                row["capture_id"] != capture_id or row["file_path"] != file_path
                or row["source"] != source for row in rows
            ):
                raise ValueError("录音标识或路径已用于另一条录音")
            return rows[0]["id"], False
        sid = uuid.uuid4().hex[:12]
        conn.execute(
            """INSERT INTO sessions
               (id, name, file_path, source, duration, capture_id, started_at,
                ended_at, created_at, interruption_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, name, file_path, source, duration, capture_id, started_at, ended_at,
             created_at, interruption_reason),
        )
        return sid, True


def recording_timeline_candidates(start_iso: str, end_iso: str, date_tokens: list[str], limit: int = 201):
    """仅读音频元数据并在 SQL 层限定候选数，绝不读取分句或正文。

    老录音仅按文件名中的录音日期找候选，不能用转写入库时间代替事件时间。
    """
    legacy = " OR ".join("(name LIKE ? OR file_path LIKE ? OR archived_path LIKE ?)" for _ in date_tokens)
    params: list = [end_iso, start_iso]
    for day in date_tokens:
        params.extend([f"%rec_{day}_%"] * 3)
    params.append(min(max(limit, 1), 201))
    with _lock, _conn() as conn:
        rows = conn.execute(
            f"""SELECT id,name,file_path,archived_path,source,duration,started_at,ended_at
                FROM sessions WHERE source IN ('self_recorder','recordings','sessions') AND
                ((started_at IS NOT NULL AND julianday(started_at) <= julianday(?)
                  AND (CASE WHEN julianday(ended_at) > julianday(started_at)
                       THEN julianday(ended_at)
                       ELSE julianday(started_at) + coalesce(duration,0)/86400.0 END) >= julianday(?))
                 OR (started_at IS NULL AND ({legacy or '0'})))
                ORDER BY coalesce(started_at, file_path) DESC LIMIT ?""", params,
        ).fetchall()
        return [dict(row) for row in rows]


def get_session(session_id: str):
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        s = dict(row)
        segs = conn.execute(
            "SELECT id, start, end, speaker, text FROM segments WHERE session_id = ? ORDER BY start, id",
            (session_id,),
        ).fetchall()
        s["segments"] = [dict(x) for x in segs]
        labs = conn.execute(
            "SELECT speaker, label FROM speaker_labels WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        s["speaker_labels"] = {x["speaker"]: x["label"] for x in labs}
        s["tags"] = [r["tag"] for r in conn.execute(
            "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY tag", (session_id,)
        ).fetchall()]
        return s


def _tags_map(conn, session_ids=None) -> dict:
    """一次取回标签，避免列表页 N+1 查询。返回 {session_id: [tag, ...]}。"""
    if session_ids is not None:
        if not session_ids:
            return {}
        marks = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT session_id, tag FROM session_tags WHERE session_id IN ({marks})",
            tuple(session_ids),
        ).fetchall()
    else:
        rows = conn.execute("SELECT session_id, tag FROM session_tags").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["session_id"], []).append(r["tag"])
    for tags in out.values():
        tags.sort()
    return out


def list_sessions():
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ).fetchall()
        tags = _tags_map(conn)
        # 一次分组算出每条录音的段落数：前端判断「有没有转写内容」要用，不能只靠 status
        counts = {sid: n for sid, n in conn.execute(
            "SELECT session_id, COUNT(*) FROM segments GROUP BY session_id")}
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = tags.get(d["id"], [])
            d["seg_count"] = counts.get(d["id"], 0)
            out.append(d)
        return out





def update_status(session_id: str, status: str, error: str = None):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET status = ?, error = ? WHERE id = ?",
            (status, error, session_id),
        )


def set_engine(session_id: str, engine: str):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET engine = ? WHERE id = ?", (engine, session_id)
        )


def update_duration(session_id: str, duration: float):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET duration = ? WHERE id = ?", (duration, session_id)
        )


def update_energy(session_id: str, energy_db: float | None):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET energy_db = ? WHERE id = ?", (energy_db, session_id)
        )


def update_quality(session_id: str, energy_db: float | None, speech_ratio: float | None):
    """一次写入平均音量与「人声占比」（低内容判定用）。"""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET energy_db = ?, speech_ratio = ? WHERE id = ?",
            (energy_db, speech_ratio, session_id),
        )


def toggle_favorite(session_id: str, favorite: bool):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET favorite = ? WHERE id = ?",
            (1 if favorite else 0, session_id),
        )


def set_archived_path(session_id: str, archived_path: str | None):
    """记录归档副本路径；传 None 表示清空。"""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET archived_path = ? WHERE id = ?",
            (archived_path, session_id),
        )


def rename_session(session_id: str, new_name: str, new_file_path: str):
    """更新会话显示名与源文件路径（重命名后）。"""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET name = ?, file_path = ? WHERE id = ?",
            (new_name, new_file_path, session_id),
        )


def save_segments(session_id: str, segments: list):
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM segments WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO segments (session_id, start, end, speaker, text) VALUES (?, ?, ?, ?, ?)",
            [(session_id, s["start"], s["end"], s.get("speaker"), s["text"]) for s in segments],
        )


def set_speaker_ref(session_id: str, path: str):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET speaker_ref = ? WHERE id = ?", (path, session_id)
        )


def set_speaker_label(session_id: str, speaker: str, label: str):
    """保存某个说话人的显示名（label 可含中文）。"""
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO speaker_labels (session_id, speaker, label)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id, speaker) DO UPDATE SET label = excluded.label""",
            (session_id, speaker, label),
        )


def is_deleted(file_path: str) -> bool:
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM deleted_files WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row is not None


def mark_deleted(file_path: str):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO deleted_files (file_path) VALUES (?)", (file_path,)
        )


def delete_session(session_id: str, hidden_path: str | None = None):
    """在同一事务内删除会话及关联数据；可选保留源文件并隐藏它。"""
    with _lock, _conn() as conn:
        if hidden_path:
            conn.execute(
                "INSERT OR IGNORE INTO deleted_files (file_path) VALUES (?)", (hidden_path,)
            )
        conn.execute("DELETE FROM segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def mark_running_sessions_interrupted():
    """服务重启时，把卡在转写中的会话标记为中断，允许重新转写。"""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'pending', error = 'interrupted by restart' "
            "WHERE status IN ('transcribing','pending') AND error IS NULL"
        )


# ---------- 多说话人声纹库 ----------

def list_voice_profiles() -> list[dict]:
    """返回声纹库元信息（不含 embedding），用于前端面板展示。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, ref_path, created_at FROM voice_profiles ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_voice_profiles_with_embedding() -> list[dict]:
    """返回带 embedding(原始字节) 的声纹库，用于转写后的说话人匹配。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, embedding FROM voice_profiles WHERE embedding IS NOT NULL"
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "embedding": bytes(r["embedding"])} for r in rows]


def add_voice_profile(pid: str, name: str, ref_path: str, embedding_bytes: bytes | None):
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO voice_profiles (id, name, ref_path, embedding)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, ref_path=excluded.ref_path,
                                             embedding=excluded.embedding""",
            (pid, name, ref_path, embedding_bytes),
        )


def delete_voice_profile(pid: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM voice_profiles WHERE id = ?", (pid,))
        return cur.rowcount > 0


# ---------- 录音标签（用于事后检索：客户 / 主题 / 参会人 / 场合）----------

TAG_MAX_LEN = 24


def normalize_tags(tags) -> list[str]:
    """清洗标签：合并内部空白、去空、限长、按首次出现去重。"""
    out, seen = [], set()
    for t in tags or []:
        s = " ".join(str(t).split())[:TAG_MAX_LEN]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_tags(session_id: str) -> list[str]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY tag", (session_id,)
        ).fetchall()
        return [r["tag"] for r in rows]


def set_tags(session_id: str, tags) -> list[str]:
    """整组覆盖某个会话的标签；传空列表等于清空该会话的标签。"""
    clean = normalize_tags(tags)
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO session_tags (session_id, tag) VALUES (?, ?)",
            [(session_id, t) for t in clean],
        )
    # 返回排序后的结果，与 list_sessions / get_session 的展示顺序保持一致
    return sorted(clean)


def list_all_tags() -> list[dict]:
    """全部标签及使用次数（用得多的在前），供筛选栏与编辑弹窗使用。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            """SELECT tag, COUNT(*) AS count FROM session_tags
               GROUP BY tag ORDER BY count DESC, tag ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- 跨录音搜索与人工纠正 ----------
def search_segments(query: str, offset: int = 0, limit: int = 50):
    # instr 为字面子串匹配，中文无需分词，%/_ 不被当成通配符。
    with _lock, _conn() as conn:
        where = "instr(lower(coalesce(g.text, '')), lower(?)) > 0"
        total = conn.execute(f"SELECT count(*) FROM segments g JOIN sessions s ON s.id=g.session_id WHERE {where}", (query,)).fetchone()[0]
        rows = conn.execute(f"""SELECT g.id, g.session_id, g.start, g.end, g.text,
            s.name, s.created_at,
            (SELECT text FROM segments p WHERE p.session_id=g.session_id AND
             (p.start<g.start OR (p.start=g.start AND p.id<g.id)) ORDER BY p.start DESC,p.id DESC LIMIT 1) AS before_text,
            (SELECT text FROM segments n WHERE n.session_id=g.session_id AND
             (n.start>g.start OR (n.start=g.start AND n.id>g.id)) ORDER BY n.start,n.id LIMIT 1) AS after_text
            FROM segments g JOIN sessions s ON s.id=g.session_id WHERE {where}
            ORDER BY s.created_at DESC,s.id,g.start,g.id LIMIT ? OFFSET ?""", (query,limit,offset)).fetchall()
        return {"total": total, "items": [dict(r) for r in rows], "offset": offset}


def edit_segment(sid: str, segment_id: int, text: str, speaker: str, expected_text: str, expected_speaker: str):
    with _lock, _conn() as conn:
        session = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            raise LookupError("录音不存在")
        if session[0] == "transcribing":
            raise ValueError("转写过程中暂不能编辑")
        cur = conn.execute("""UPDATE segments SET text=?,speaker=? WHERE session_id=? AND id=?
            AND text=? AND coalesce(speaker,'')=?""", (text,speaker or None,sid,segment_id,expected_text,expected_speaker))
        if not cur.rowcount:
            raise ValueError("片段已变化，请重新打开后编辑")


def merge_speakers(sid: str, source: str, target: str):
    with _lock, _conn() as conn:
        session = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            raise LookupError("录音不存在")
        if session[0] == "transcribing":
            raise ValueError("转写过程中暂不能合并")
        speakers = {r[0] for r in conn.execute("SELECT DISTINCT speaker FROM segments WHERE session_id=?", (sid,))}
        if source == target or source not in speakers or target not in speakers:
            raise ValueError("请选择两个不同的已有说话人")
        cur = conn.execute("UPDATE segments SET speaker=? WHERE session_id=? AND speaker=?", (target,sid,source))
        conn.execute("DELETE FROM speaker_labels WHERE session_id=? AND speaker=?", (sid,source))
        return cur.rowcount
