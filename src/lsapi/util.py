"""공통 유틸 — 민감정보 마스킹 등."""

from typing import Any

# LS 응답은 InptPwd/Pwd 같은 계좌 비밀번호 필드를 평문으로 echo한다.
# 출력/로깅 전에 반드시 가린다.
_SECRET_HINTS = ("pwd", "passwd", "pass", "secret")


def redact(obj: Any) -> Any:
    """dict/list를 재귀적으로 복사하며 비밀번호류 필드 값을 '****'로 마스킹.

    원본은 건드리지 않는다. 콘솔 출력·로깅·파일 저장 직전에 사용.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in k.lower() for h in _SECRET_HINTS) and isinstance(v, (str, int)):
                out[k] = "****"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj
