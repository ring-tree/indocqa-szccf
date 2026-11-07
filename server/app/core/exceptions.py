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


class BackendMismatchError(AppError):
    """40902 现有向量库与当前嵌入后端不一致，需重建后再入库。"""

    code = 40902
    http_status = 409


class UpstreamError(AppError):
    """42901 上游模型限流或不可用（已尝试主备切换）。"""

    code = 42901
    http_status = 502


class RetrievalError(AppError):
    """50001 检索异常。"""

    code = 50001
    http_status = 500
