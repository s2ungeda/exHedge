"""빌드 버전/날짜 표시용 (캡션에 노출).

build.ps1이 빌드 시 `trdbot/_buildstamp.py`(예: BUILD = "2026-07-31 16:30")를 생성해
PyInstaller가 exe(_internal)에 함께 심는다 → exe+_internal만 다른 PC로 옮겨도 빌드 날짜가 따라간다.

- 소스 실행(frozen 아님): 항상 'dev'
- 빌드된 exe: 심어진 빌드 시각 표시(없으면 '?')
"""

from __future__ import annotations

import sys


def build_label() -> str:
    """캡션에 표시할 빌드 라벨."""
    if not getattr(sys, "frozen", False):
        return "dev"
    try:
        from trdbot._buildstamp import BUILD
    except Exception:
        return "?"
    return BUILD
