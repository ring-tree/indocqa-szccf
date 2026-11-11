"""会话存储：SQLite 单文件，按 session_id 隔离消息，供多轮上下文与历史列表使用。"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# 客户端可用 crypto.randomUUID()（含连字符），也兼容服务端生成的 hex
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    task_state TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT,
    trace TEXT,
    confidence TEXT,
    confidence_reason TEXT,
    boundary TEXT,
    model TEXT,
    context_manifest TEXT,
    attachments TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploading',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_uploads_session ON uploads(session_id, created_at);

CREATE TABLE IF NOT EXISTS session_attachments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    text TEXT,
    meta TEXT,
    parse_status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attachments_session
    ON session_attachments(session_id, created_at);
"""


def valid_id(session_id: str | None) -> bool:
    return bool(session_id and _ID_RE.match(session_id))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def _connect(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(_SCHEMA)
        # 轻量迁移：旧库缺列时补齐，避免升级后丢字段
        message_columns = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        if "model" not in message_columns:
            con.execute("ALTER TABLE messages ADD COLUMN model TEXT")
        if "context_manifest" not in message_columns:
            con.execute("ALTER TABLE messages ADD COLUMN context_manifest TEXT")
        if "attachments" not in message_columns:
            con.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
        session_columns = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        if "task_state" not in session_columns:
            con.execute("ALTER TABLE sessions ADD COLUMN task_state TEXT")
        yield con
        con.commit()
    finally:
        con.close()


def _session_row(row: sqlite3.Row) -> dict:
    task_state = None
    if row["task_state"]:
        try:
            task_state = json.loads(row["task_state"])
        except (TypeError, ValueError):
            task_state = None
    return {
        "id": row["id"],
        "title": row["title"],
        "taskState": task_state,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _message_row(row: sqlite3.Row) -> dict:
    manifest = None
    if row["context_manifest"]:
        try:
            manifest = json.loads(row["context_manifest"])
        except (TypeError, ValueError):
            manifest = None
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "sources": json.loads(row["sources"]) if row["sources"] else [],
        "trace": json.loads(row["trace"]) if row["trace"] else [],
        "confidence": row["confidence"],
        "confidenceReason": row["confidence_reason"],
        "boundary": row["boundary"],
        "model": row["model"],
        "contextManifest": manifest,
        "attachments": json.loads(row["attachments"]) if row["attachments"] else [],
        "createdAt": row["created_at"],
    }


def ensure_session(path: str, session_id: str | None, title: str = "") -> dict:
    """取已有会话；不存在则用客户端 id（合法时）或新 id 建一个。"""
    sid = session_id if valid_id(session_id) else uuid.uuid4().hex
    with _connect(path) as con:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row:
            return _session_row(row)
        now = _now()
        con.execute(
            "INSERT INTO sessions(id, title, created_at, updated_at) VALUES(?,?,?,?)",
            (sid, _title_of(title), now, now),
        )
        return {"id": sid, "title": _title_of(title), "createdAt": now, "updatedAt": now}


def create_session(path: str, title: str = "", session_id: str | None = None) -> dict:
    return ensure_session(path, session_id, title or "新会话")


def get_session(path: str, session_id: str) -> dict | None:
    with _connect(path) as con:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _session_row(row) if row else None


def list_sessions(path: str, limit: int = 50) -> list[dict]:
    with _connect(path) as con:
        rows = con.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
                   (SELECT content FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                     ORDER BY m.id DESC LIMIT 1) AS last_message
              FROM sessions s
             ORDER BY s.updated_at DESC, s.rowid DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = _session_row(row)
        item["messageCount"] = row["message_count"]
        item["lastMessage"] = (row["last_message"] or "")[:80]
        out.append(item)
    return out


def rename_session(path: str, session_id: str, title: str) -> bool:
    with _connect(path) as con:
        cur = con.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (_title_of(title), _now(), session_id),
        )
        return cur.rowcount > 0


def delete_session(path: str, session_id: str) -> bool:
    with _connect(path) as con:
        cur = con.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0


def append_message(
    path: str,
    session_id: str,
    role: str,
    content: str,
    sources: list | None = None,
    trace: list | None = None,
    confidence: str | None = None,
    confidence_reason: str = "",
    boundary: str | None = None,
    model: str | None = None,
    context_manifest: dict | None = None,
    attachments: list | None = None,
) -> int:
    now = _now()
    with _connect(path) as con:
        cur = con.execute(
            """
            INSERT INTO messages(session_id, role, content, sources, trace,
                                 confidence, confidence_reason, boundary, model,
                                 context_manifest, attachments, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                role,
                content,
                json.dumps(sources or [], ensure_ascii=False),
                json.dumps(trace or [], ensure_ascii=False),
                confidence,
                confidence_reason,
                boundary,
                model,
                json.dumps(context_manifest, ensure_ascii=False) if context_manifest else None,
                json.dumps(attachments or [], ensure_ascii=False),
                now,
            ),
        )
        if role == "user":
            # 客户端先建空会话、后发首问时补标题；已有自定义标题不覆盖
            con.execute(
                "UPDATE sessions SET title=CASE WHEN title='' OR title='新会话' THEN ? "
                "ELSE title END WHERE id=?",
                (_title_of(content), session_id),
            )
        con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return int(cur.lastrowid)


def get_messages(path: str, session_id: str, limit: int | None = None) -> list[dict]:
    with _connect(path) as con:
        if limit:
            rows = con.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (session_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
    return [_message_row(r) for r in rows]


def get_task_state(path: str, session_id: str) -> dict | None:
    with _connect(path) as con:
        row = con.execute("SELECT task_state FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or not row["task_state"]:
        return None
    try:
        return json.loads(row["task_state"])
    except (TypeError, ValueError):
        return None


def save_task_state(path: str, session_id: str, state: dict) -> None:
    with _connect(path) as con:
        con.execute(
            "UPDATE sessions SET task_state=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), session_id),
        )


def connect(path: str):
    """公开的数据库连接上下文，供附件仓库复用同一套建表/迁移逻辑。"""
    return _connect(path)


def expired_session_ids(path: str, ttl_days: int) -> list[str]:
    """返回超过 TTL 的会话 ID，供附件文件清理先行执行。"""
    if ttl_days <= 0:
        return []
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ttl_days * 86400))
    with _connect(path) as con:
        rows = con.execute("SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)).fetchall()
    return [row["id"] for row in rows]


def cleanup(path: str, ttl_days: int) -> int:
    """删除最后活跃时间超过 TTL 的会话（默认 7 天），返回删除条数。"""
    if ttl_days <= 0:
        return 0
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ttl_days * 86400))
    with _connect(path) as con:
        cur = con.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


def compact(path: str) -> None:
    """关闭进程时回收 WAL 并压缩数据库文件。"""
    if not Path(path).exists():
        return
    con = sqlite3.connect(path, timeout=5)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
    finally:
        con.close()


def _title_of(text: str) -> str:
    clean = " ".join((text or "").split())
    return (clean[:40] or "新会话").strip()
