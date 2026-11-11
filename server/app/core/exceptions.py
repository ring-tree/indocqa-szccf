"""业务异常与错误码。API 边界统一转换为响应包 {"error": {code, message, detail}}。"""


class AppError(Exception):
    code = 50002
    http_status = 500

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class ParamError(AppError):
    """40001 参数校验失败。"""

    code = 40001
    http_status = 400


class FileTooLargeError(AppError):
    """40002 上传文件超过大小限制。"""

    code = 40002
    http_status = 400


class UnsupportedFormatError(AppError):
    """40003 文件格式不支持。"""

    code = 40003
    http_status = 400


class KBEmptyError(AppError):
    """40401 知识库为空，无法执行知识库问答。"""

    code = 40401
    http_status = 404


class DocumentNotFoundError(AppError):
    """40402 指定文档不存在。"""

    code = 40402
    http_status = 404


class SessionNotFoundError(AppError):
    """40403 指定会话不存在或已过期。"""

    code = 40403
    http_status = 404


class AttachmentLimitError(AppError):
    """40004 单条消息附件数量超过限制。"""

    code = 40004
    http_status = 400


class UploadNotFoundError(AppError):
    """40005 上传会话不存在、已过期或已完成。"""

    code = 40005
    http_status = 400


class ChunkError(AppError):
    """40006 分片序号、大小或哈希不合法。"""

    code = 40006
    http_status = 400


class PermissionDeniedError(AppError):
    """40301 附件或会话无权访问。"""

    code = 40301
    http_status = 403


class AttachmentNotFoundError(AppError):
    """40404 附件不存在。"""

    code = 40404
    http_status = 404


class UploadConflictError(AppError):
    """40901 上传状态冲突（重复完成、分片写入冲突等）。"""

    code = 40901
    http_status = 409


class AttachmentStorageError(AppError):
    """50003 附件存储、合并或清理失败。"""

    code = 50003
    http_status = 500


class BackendMismatchError(AppError):
    """40902 现有向量库与当前嵌入后端不一致，需重建后再入库。"""

    code = 40902
    http_status = 409


class ChromaBusyError(AppError):
    """40903 同一向量库正在被另一个进程写入。"""

    code = 40903
    http_status = 409


class AIUnavailableError(AppError):
    """40904 未配置输出层模型，AI 辅助能力不可用。"""

    code = 40904
    http_status = 409


class UpstreamError(AppError):
    """42901 上游模型限流或不可用（已尝试主备切换）。"""

    code = 42901
    http_status = 502


class RetrievalError(AppError):
    """50001 检索异常。"""

    code = 50001
    http_status = 500
