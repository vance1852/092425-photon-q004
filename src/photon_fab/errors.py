"""光电芯片服务向 API 和调用方暴露的稳定错误。"""


class PhotonError(RuntimeError):
    code = "photon_error"
    status = 400


class NotFound(PhotonError):
    code = "not_found"
    status = 404


class Conflict(PhotonError):
    code = "conflict"
    status = 409


class Forbidden(PhotonError):
    code = "forbidden"
    status = 403


class InvalidState(PhotonError):
    code = "invalid_state"
    status = 409


class ValidationFailed(PhotonError):
    code = "validation_failed"
    status = 422
