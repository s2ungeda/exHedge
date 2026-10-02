import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from lsapi.config import get_settings
from lsapi.paths import app_base_dir

# 파일 로그 위치 — 소스 실행이면 프로젝트 루트, 배포(.exe)면 실행파일 옆의 logs/.
_LOG_DIR = app_base_dir() / "logs"


def setup_logging(level: str | None = None) -> None:
    """프로젝트 표준 로깅 설정. main 스크립트에서 한 번 호출.

    터미널(stderr) + 파일(logs/trdbot.log)에 동시에 남긴다. 파일은 자정마다 회전
    (trdbot.log → trdbot.log.YYYY-MM-DD), 14일치 보관 후 자동 삭제. 밤샘 실행 시
    재접속·체결·에러를 아침에 파일로 그대로 확인할 수 있다.
    """
    lvl = (level or get_settings().log_level).upper()
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    # 파일 핸들러 — 디스크/권한 문제로 실패해도 터미널 로그는 유지(절대 죽지 않게).
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fileh = TimedRotatingFileHandler(
            _LOG_DIR / "trdbot.log", when="midnight", backupCount=14, encoding="utf-8",
        )
        fileh.suffix = "%Y-%m-%d"
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
        root.info("파일 로그 시작: %s", _LOG_DIR / "trdbot.log")
    except OSError as e:
        root.warning("파일 로그 설정 실패(터미널만 사용): %s", e)
