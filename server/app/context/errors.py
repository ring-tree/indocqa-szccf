"""上下文组装异常：按可降级程度分层，便于编排器选择回退策略。"""


class ContextError(Exception):
    """上下文组装基类。"""

    code = "CONTEXT_ERROR"

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class SessionIsolationError(ContextError):
    code = "CONTEXT_SESSION_ISOLATION"


class BudgetInfeasibleError(ContextError):
    code = "CONTEXT_BUDGET_INFEASIBLE"


class TemplateValidationError(ContextError):
    code = "CONTEXT_TEMPLATE_INVALID"


class TraceIntegrityError(ContextError):
    code = "CONTEXT_TRACE_INTEGRITY"


class DedupBackendError(ContextError):
    code = "CONTEXT_DEDUP_DEGRADED"
