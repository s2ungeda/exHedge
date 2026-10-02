# TrdBot — LS증권 OpenAPI 자동매매 골격

LS증권 **OpenAPI(REST + WebSocket)** 를 사용한 Python 자동매매 토대.
국내주식 / ETF / 선물옵션 인증·시세·주문이 동작하는 최소 구조까지만 잡혀 있고,
전략 로직은 이 위에 모듈로 얹는 방향.

## 사전 준비

1. **OpenAPI 신청** — [openapi.ls-sec.co.kr](https://openapi.ls-sec.co.kr) 에서 신청 후
   `appkey`, `appsecretkey` 발급.
2. **모의투자 계좌** — 동일 포털에서 모의투자 신청. 처음에는 무조건 모의로 검증.
3. **Python 3.10 이상**.

## 설치

```powershell
cd C:\project\python\TrdBot
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## 환경설정

### 1) 자격 증명 등록 (최초 1회)

`appkey` / `appsecret`은 **Windows 자격 증명 관리자(DPAPI)**에 저장합니다 — 평문 파일 X.

> ⚠️ **LS는 appkey를 계좌(시장) 단위로 발급한다.** 주식계좌와 선물계좌의 appkey가 **다르다.**
> 따라서 주식용/선물용 appkey를 **각각** 등록해야 하고, 봇은 `LSClient(market="stock"|"futures")`로
> 시장에 맞는 appkey/토큰을 골라 쓴다. (한 appkey로 양쪽이 안 됨 — 안 되면 주문이 엉뚱한 계좌로 가 `01425`)

```powershell
python examples\00_setup_credentials.py            # 주식/선물 appkey + 계좌비번 각각 입력
python examples\00_setup_credentials.py --env prod # 실거래용 따로 등록
python examples\00_setup_credentials.py --check    # 등록 상태 확인 (시장별)
python examples\00_setup_credentials.py --delete   # 삭제
```

입력값은 `getpass`로 화면에 표시되지 않고, `WinVaultKeyring` 백엔드를 통해 같은 Windows 사용자만 복호화할 수 있게 저장됩니다.
GUI 확인은 Windows 검색 → "자격 증명 관리자" → "Windows 자격 증명" → `trdbot-ls-mock` / `trdbot-ls-prod`
(username: `stock_app_key` / `futures_app_key` 등).

### 2) .env (비민감 항목만)

```powershell
copy .env.example .env
```

| 키 | 설명 |
|---|---|
| `LS_ENV` | `mock` (모의) / `prod` (실거래). 처음엔 `mock`. |
| `LS_ACCOUNT_NO` | 주문에 쓰는 계좌번호 (모의/실거래 각각 다름) |
| `LOG_LEVEL` | `INFO` 권장 |

> appkey/appsecret을 환경변수(`LS_APP_KEY`, `LS_APP_SECRET`)로 넣으면 keyring 부재 시 폴백으로 동작합니다. CI 환경 등에서만 사용.

## 예제 실행

```powershell
# 1) 토큰 발급 (한 번 받으면 24시간 캐시)
python examples\01_token.py

# 2) 시세 조회
python examples\02_stock_quote.py

# 3) 주문 (반드시 모의에서 먼저)
python examples\03_stock_order.py

# 4) 실시간 시세 — 장중에 실행
python examples\04_realtime_ws.py

# 5) 선물옵션 (주간)
python examples\05_futures_quote.py 101W9000

# 6) 야간선물 (KRX 야간파생, 18:00~05:00 · CME 연계)
python examples\07_night_futures.py            # 인자 없이 → 야간 종목 목록
python examples\07_night_futures.py A0169000   # 단축코드로 시세/호가 (근월물 예)

# 7) 자동주문화면 (Tkinter 입력 폼 — 단독, 엔진 미연결)
python examples\10_order_screen.py
```

### 자동매매 실행 (전체 — 화면+시그널+집행)

```powershell
python -m trdbot
```
화면에서 종목코드(예 `A7567000` 미국달러선물 근월)·보유 원달러(계약수)·헤지비율·1회주문계약수·인터벌·최대잔고한도
입력 후 **실행** → 외부(델파이) 시그널 수신 시 목표계약수 계산 → maker 주문(주간/야간 자동 라우팅).

> 주간(08:45~15:45)은 모의로 end-to-end 검증됨. 야간(18:00~06:00) 주문/잔고는 모의 미제공이라 실거래 검증 필요.
> 시그널 형식: `{"id":"...","fx":1380.5,"total_domestic":500000000,"total_coin":200000000}` (매수/매도 없음 — 방향은 계산).

> 야간파생은 주간과 **TR이 다르다**: 시세 `t8455/t8456/t8457`, 주문 `CCENT00100~00300`,
> 잔고 `CCENQ90200`. 시세 InBlock 필드명도 다름(시세=`focode`, 호가=`shcode`).
> 코드는 [`NightFuturesAPI`](src/lsapi/futures.py) 참고.

### 주문 직접 검증 (모의, 안전판)

하한가 비체결 주문 → 즉시 취소로 주문 경로를 안전하게 검증한다. 비밀번호 필드는 마스킹.

```powershell
# 주식: 인자 없으면 읽기전용(현재가/잔고/미체결), --order 면 매수→취소 1회전
python examples\08_test_stock.py
python examples\08_test_stock.py --order

# 선물(삼성 주식선물 + 원달러): 마스터에서 활성종목 자동탐색
python examples\09_test_futures.py
python examples\09_test_futures.py --order
```

> **선물 주문이 `01425 주문가능금액 부족`으로 거부되면** — 토큰이 주식계좌에만 바인딩된 것.
> LS는 **appkey를 계좌 단위로 발급**하므로, 선물계좌로 발급한 appkey/secret으로 전환해야 한다.
> 주식선물 시세는 범용 `t2111`이 0을 주므로 전용 `t8402`(현재가)/`t8403`(호가)를 쓴다.

## 구조

두 패키지로 분리 — 의존 방향은 `trdbot → lsapi` 한쪽으로만 흐른다.

```
src/lsapi/           # LS 브로커 클라이언트 (순수 API)
├── config.py        .env → Settings (URL/계좌/로그레벨)
├── auth.py          OAuth 토큰 발급 + 디스크 캐시 + 자동 갱신
├── client.py        REST 공통 클라이언트 (tr_cd 헤더 / 401 재시도 / 응답 검증)
├── ws_client.py     WebSocket 실시간 (구독/핸들러/재연결)
├── endpoints.py     TR 코드 / path 한 곳에 모음
├── stock.py         주식·ETF 시세/주문/잔고
├── futures.py       선물옵션(주간 FuturesAPI / 야간 NightFuturesAPI)
├── exceptions.py    에러 타입
├── util.py          민감정보 마스킹 등
└── logger.py        로깅 셋업

src/trdbot/          # 앱/전략 레이어 (lsapi에 의존)
├── targets.py       목표계약수 산출 (C/D/목표)
├── order_screen.py  자동주문화면 (Tkinter)
├── signal_link.py   시그널 수신·송신 (델파이 udpchat 호환)
├── executor.py      (예정) 집행 상태머신
└── pricing.py       (예정) tick 역산 + 주문가격

examples/            바로 돌려볼 수 있는 사용 예시 (00~12)
tokens/              토큰 캐시 (gitignored)
config/              화면 입력 저장 (gitignored)
```

## 다음 단계 (전략 얹기)

> 집행 엔진(maker, 시그널 수신 프로토콜, 설정값, 미정 사항) 설계는 [docs/execution-engine.md](docs/execution-engine.md) 에 정리.
> 표면(Claude 앱 / VSCode)을 건너 공유되는 단일 소스이므로 결정이 바뀌면 그 문서를 갱신할 것.

골격 위에 전략을 붙일 때 권장 흐름:

1. **데이터 레이어** — 시세를 받아 메모리/디스크 캐시 (pandas DataFrame, parquet)
2. **시그널 모듈** — 지표 계산 → 진입/청산 신호 생성 (순수 함수로)
3. **포지션 관리** — 현재 잔고 vs 목표 포지션 diff → 주문 리스트
4. **실행 엔진** — 주문 발행, 체결 모니터링, 슬리피지/부분체결 처리
5. **리스크 가드** — 일중 손실 한도, 종목/섹터 익스포저, 주문 빈도 제한

## 주의

- **TR 코드/필드명**은 LS 공식 문서를 기준으로 최종 확인 후 사용. 본 골격의 값은 일반적으로 알려진 기준이며 포털 명세가 우선.
- `.env` 와 `tokens/` 는 절대 커밋 금지 (`.gitignore` 반영됨).
- **실거래로 전환 전 체크리스트**: 모의에서 ≥1주일 동작 / 주문 거부 케이스 / 네트워크 단절 / 토큰 만료 / 장 시작·종료 경계.
