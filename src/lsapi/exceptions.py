class LSAPIError(Exception):
    """LS OpenAPI 호출 중 발생한 오류의 기반 클래스."""

    def __init__(self, message: str, *, code: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class LSAuthError(LSAPIError):
    """토큰 발급/갱신 실패."""


class LSOrderError(LSAPIError):
    """주문 거부/오류."""


class LSRateLimitError(LSAPIError):
    """요청 한도 초과."""
