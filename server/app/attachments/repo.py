"""附件与上传任务的 SQLite 仓库。"""

import json
import time

from app.session import store as session_store


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _attachment_row(row) -> dict:
    meta = {}
    if row["meta"]:
        try:
            meta = json.loads(row["meta"])
        except (TypeError, ValueError):
            meta = {}
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "filename": row["filename"],
        "stored_name": row["stored_name"],
        "mime": row["mime"],
        "kind": row["kind"],
        "size": int(row["size"] or 0),
        "text": row["text"],
        "meta": meta,
        "parse_status": row["parse_status"],
        "created_at": row["created_at"],
    }


def public_attachment(record: dict) -> dict:
    session_id = record["session_id"]
    url = f"/api/attachments/{record['id']}/content?sessionId={session_id}"
    return {
        "id": record["id"],
        "sessionId": session_id,
        "filename": record["filename"],
        "mime": record["mime"],
        "kind": record["kind"],
        "size": record["size"],
        "url": url,
        "downloadUrl": f"{url}&download=1",
        "parseStatus": record["parse_status"],
        "meta": record.get("meta") or {},
        "textChars": len(record.get("text") or ""),
    }


def create_upload(
    path: str,
    *,
    upload_id: str,
    session_id: str,
    filename: str,
    mime: str,
    kind: str,
    size: int,
    chunk_size: int,
    total_chunks: int,
    expires_at: str,
) -> None:
    with session_store.connect(path) as con:
        con.execute(
            """
            INSERT INTO uploads(id, session_id, filename, mime, kind, size,
                                chunk_size, total_chunks, status, created_at, expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                upload_id,
                session_id,
                filename,
                mime,
                kind,
                size,
                chunk_size,
                total_chunks,
                "uploading",
                _now(),
                expires_at,
            ),
        )


def get_upload(path: str, upload_id: str) -> dict | None:
    with session_store.connect(path) as con:
        row = con.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(row) if row else None


def set_upload_status(path: str, upload_id: str, status: str) -> None:
    with session_store.connect(path) as con:
        con.execute("UPDATE uploads SET status=? WHERE id=?", (status, upload_id))


def delete_upload(path: str, upload_id: str) -> None:
    with session_store.connect(path) as con:
        con.execute("DELETE FROM uploads WHERE id=?", (upload_id,))


def count_uploading(path: str, session_id: str) -> int:
    with session_store.connect(path) as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE session_id=? AND status IN ('uploading','completing')",
            (session_id,),
        ).fetchone()
    return int(row["n"] if row else 0)


def add_attachment(
    path: str,
    *,
    attachment_id: str,
    session_id: str,
    filename: str,
    stored_name: str,
    mime: str,
    kind: str,
    size: int,
    text: str | None,
    meta: dict,
    parse_status: str,
) -> dict:
    with session_store.connect(path) as con:
        con.execute(
            """
            INSERT INTO session_attachments(
                id, session_id, filename, stored_name, mime, kind, size,
                text, meta, parse_status, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attachment_id,
                session_id,
                filename,
                stored_name,
                mime,
                kind,
                size,
                text,
                json.dumps(meta or {}, ensure_ascii=False),
                parse_status,
                _now(),
            ),
        )
    record = get_attachment(path, attachment_id)
    if record is None:
        raise RuntimeError("附件写入后无法读取")
    return record


def get_attachment(path: str, attachment_id: str) -> dict | None:
    with session_store.connect(path) as con:
        row = con.execute(
            "SELECT * FROM session_attachments WHERE id=?", (attachment_id,)
        ).fetchone()
    return _attachment_row(row) if row else None


def list_attachments(
    path: str,
    session_id: str,
    *,
    ids: list[str] | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    if ids is not None and not ids:
        return []
    with session_store.connect(path) as con:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = con.execute(
                f"SELECT * FROM session_attachments WHERE session_id=? AND id IN ({placeholders}) "
                "ORDER BY created_at, rowid",
                (session_id, *ids),
            ).fetchall()
        else:
            order = "created_at DESC, rowid DESC" if newest_first else "created_at, rowid"
            sql = f"SELECT * FROM session_attachments WHERE session_id=? ORDER BY {order}"
            params: tuple = (session_id,)
            if limit is not None:
                sql += " LIMIT ?"
                params = (session_id, limit)
            rows = con.execute(sql, params).fetchall()
    return [_attachment_row(row) for row in rows]


def list_recent(path: str, session_id: str, limit: int) -> list[dict]:
    return list_attachments(path, session_id, limit=limit, newest_first=True)


def has_ready(path: str, session_id: str) -> bool:
    with session_store.connect(path) as con:
        row = con.execute(
            "SELECT 1 AS ok FROM session_attachments WHERE session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
    return bool(row)


def delete_attachment(path: str, attachment_id: str) -> None:
    with session_store.connect(path) as con:
        con.execute("DELETE FROM session_attachments WHERE id=?", (attachment_id,))


def session_attachment_records(path: str, session_id: str) -> list[dict]:
    """删除会话前取出附件记录，用于清理磁盘文件。"""
    return list_attachments(path, session_id)


def session_upload_records(path: str, session_id: str) -> list[dict]:
    with session_store.connect(path) as con:
        rows = con.execute("SELECT * FROM uploads WHERE session_id=?", (session_id,)).fetchall()
    return [dict(row) for row in rows]


def expired_upload_ids(path: str) -> list[str]:
    with session_store.connect(path) as con:
        rows = con.execute("SELECT id FROM uploads WHERE expires_at < ?", (_now(),)).fetchall()
    return [row["id"] for row in rows]


def all_stored_names(path: str) -> set[str]:
    with session_store.connect(path) as con:
        rows = con.execute("SELECT stored_name FROM session_attachments").fetchall()
    return {row["stored_name"] for row in rows}
