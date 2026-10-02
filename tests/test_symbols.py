"""미국달러선물 월물 콤보 파싱(app.usd_futures_options) 테스트 — t8426 행 기반."""

from trdbot.app import usd_futures_options


def _rows():
    # 실측 t8426 형태: 월물=A756…(hname 'F YYYYMM'), 플렉스=G756…, 스프레드=D756…(SP)
    return [
        {"shcode": "A7568000", "hname": "미국달러 F 202608"},   # 차근월
        {"shcode": "A7567000", "hname": "미국달러 F 202607"},   # 근월(정렬로 먼저)
        {"shcode": "A7569000", "hname": "미국달러 F 202609"},   # 3월물(2개만 사용)
        {"shcode": "G7566Q10", "hname": "미국달러 플렉스 F 26"},  # 플렉스 → 제외
        {"shcode": "D756768S", "hname": "미국달러 SP 2607-260"},  # 스프레드 → 제외
        {"shcode": "A0050000", "hname": "코스피200 F 202607"},   # 다른 상품 → 제외
    ]


def test_근월_차근월_2개_정렬():
    # 만기 정렬 → 근월, 차근월 코드 2개만(콤보엔 코드만 표시)
    assert usd_futures_options(_rows()) == ["A7567000", "A7568000"]


def test_플렉스_스프레드_제외():
    codes = usd_futures_options(_rows())
    assert not any(c.startswith(("G", "D")) for c in codes)


def test_빈입력_빈목록():
    assert usd_futures_options([]) == []
