"""실행 기준 경로 — 소스 실행이면 프로젝트 루트, 배포(.exe)면 실행파일 폴더.

PyInstaller 번들에서는 __file__ 이 임시 추출 폴더(_MEIPASS)를 가리켜 상대경로가 깨진다.
그래서 .env·tokens·config·logs 는 **실행파일이 놓인 폴더**를 기준으로 잡는다
(배포 시 사용자가 .env 등을 실행파일 옆에 두는 위치와 일치).
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_base_dir() -> Path:
    """앱의 기준 폴더. 배포(.exe)면 실행파일 폴더, 소스 실행이면 프로젝트 루트."""
    if getattr(sys, "frozen", False):           # PyInstaller 번들로 실행 중
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]   # 소스 실행 → 프로젝트 루트
