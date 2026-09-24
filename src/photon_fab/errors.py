"""工艺版本管理相关的可观察错误，携带稳定错误码与 HTTP 状态。"""


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class NotFound(ServiceError):
    code = "not_found"
    status = 404


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class InvalidState(ServiceError):
    """工艺版本或批次处于不允许该操作的状态。"""

    code = "invalid_state"
    status = 409


class ValidationFailed(ServiceError):
    code = "validation_failed"
    status = 422
