"""환경설정(config.py) 테스트 — 환경별 엔드포인트."""

from lsapi.config import Settings


def test_ws_포트_모의는_29443():
    assert ":29443/" in Settings(ls_env="mock").ws_base_url


def test_ws_포트_실거래는_9443():
    assert ":9443/" in Settings(ls_env="prod").ws_base_url


def test_rest는_모의실거래_공용_8080():
    assert ":8080" in Settings(ls_env="mock").rest_base_url
    assert ":8080" in Settings(ls_env="prod").rest_base_url
