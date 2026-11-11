"""会话管理接口：新建、列表、详情（含消息）、重命名、删除。"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.attachments import storage as attachment_storage
from app.core.config import get_settings
from app.core.exceptions import ParamError, SessionNotFoundError
from app.session import store as session_store

router = APIRouter(prefix="/api/sessions")


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    session_id: str | None = Field(default=None, alias="sessionId")

    model_config = {"populate_by_name": True}


class SessionRename(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@router.post("")
def create_session(body: SessionCreate | None = None) -> dict:
    settings = get_settings()
    body = body or SessionCreate()
    if body.session_id and not session_store.valid_id(body.session_id):
        raise ParamError("sessionId 格式不合法")
    session = session_store.create_session(settings.session_db, body.title or "", body.session_id)
    return {"data": session}


@router.get("")
def list_sessions() -> dict:
    settings = get_settings()
    return {"data": session_store.list_sessions(settings.session_db)}


@router.get("/{session_id}")
def get_session(session_id: str) -> dict:
    settings = get_settings()
    session = session_store.get_session(settings.session_db, session_id)
    if not session:
        raise SessionNotFoundError(f"会话不存在: {session_id}")
    session["messages"] = session_store.get_messages(settings.session_db, session_id)
    return {"data": session}


@router.patch("/{session_id}")
def rename_session(session_id: str, body: SessionRename) -> dict:
    settings = get_settings()
    if not session_store.rename_session(settings.session_db, session_id, body.title):
        raise SessionNotFoundError(f"会话不存在: {session_id}")
    return {"data": session_store.get_session(settings.session_db, session_id)}


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    settings = get_settings()
    if session_store.get_session(settings.session_db, session_id) is None:
        raise SessionNotFoundError(f"会话不存在: {session_id}")
    # 先按附件记录清理磁盘文件，再删除会话（DB 级联会同时删掉附件行）
    attachment_storage.delete_session_files(settings, session_id)
    session_store.delete_session(settings.session_db, session_id)
    return {"data": True}
