# TECL 무한매수법 봇 (토스증권 Open API)

라오어 무한매수법(V3.0식 **20분할**)으로 TECL을 매일 자동 분할매수/매도하고,
주문·체결·사이클 완료를 **디스코드**와 **카카오톡(나에게 보내기)** 으로 알려주는 봇입니다.

- 파이썬 3.11+ 표준 라이브러리만 사용 (`pip install` 필요 없음)
- 토스증권 Open API (`https://openapi.tossinvest.com`) — LOC = `LIMIT` + `timeInForce: CLS`
- 기본값은 **모의 실행(dry_run)**: 실제 주문 없이 계획만 알림. 확인 후 직접 켜야 실거래

> ⚠️ 투자 판단과 결과의 책임은 본인에게 있습니다. 3배 레버리지 ETF는 손실도 큽니다.
> 처음 며칠은 모의 실행으로 계획을 보고, 실거래 전환 후에도 토스증권 앱에서 주문을 꼭 확인하세요.

---

## 하루 흐름

| 시각(한국) | 하는 일 |
|---|---|
| 정규장 시작 +15분 (서머타임 22:45 / 해제 23:45) | 보유·평단·현재가로 T값/별% 계산 → LOC·지정가 주문 → **주문 알림** |
| 정규장 마감 +20분 (서머타임 05:20 / 해제 06:20) | 주문별 체결 확인 → 평단·T 갱신 → **체결 알림** (전량 매도 시 **사이클 완료 알림**) |

장 시간·휴장일·서머타임은 토스증권 장 운영 API(`/api/v1/market-calendar/US`)로 판단합니다.
새벽 알림이 싫으면 `report_not_before_kst = "07:30"` 처럼 설정하세요.

## 매매 규칙 (기본값)

| 항목 | 규칙 |
|---|---|
| 1회 매수금 | 원금 ÷ 20 |
| T값 | (보유수량 × 평단) ÷ 1회 매수금, 소수 둘째 자리 올림 |
| 별% | `15 − 1.5 × T` (= 목표% × (1 − 2T/20)) |
| 별지점 | 평단 × (1 + 별%) |
| 첫 매수 (보유 0주) | 1회 매수금 전부, 현재가 +12% LOC (사실상 종가 매수) |
| 전반전 (T < 10) | 절반 **평단 LOC** + 절반 **별지점−0.01 LOC** |
| 후반전 (10 ≤ T < 19) | 전부 **별지점−0.01 LOC** |
| 소진 (T ≥ 19) | 매수 중단, 매도 주문만 유지 + 알림 (리버스모드는 직접 판단) |
| 매도 | 보유 ¼ **별지점 LOC** (쿼터매도) + ¾ **평단 +15% 지정가** |
| 사이클 종료 | 전량 매도되면 손익 정산 후 다음 정규장부터 새 사이클 |

- 매수가는 항상 쿼터매도가보다 0.01 낮아서 같은 날 LOC 매수·매도가 동시에 체결되지 않습니다.
- T값은 봇이 따로 세지 않고 **실제 잔고(수량×평단)에서 매일 다시 계산**합니다. 그래서 봇을 껐다 켜도,
  이미 TECL을 들고 있어도 그 상태에서 이어서 진행합니다.
- **TECL 전용 공식은 따로 없어서 TQQQ V3.0 값(목표 15%, 15−1.5T)을 기본으로 씁니다.**
  더 공격적으로(SOXL식) 하려면 `target_pct = 20` → 별% `20 − 2T`. `star_base_pct`, `star_slope`로 직접 지정도 가능합니다.
- 선택 기능: `extra_buy_levels` (하락 시 1주씩 추가 LOC 매수), `compound` (수익 재투자).
- 1회 매수금은 사이클을 시작할 때 정해집니다. `capital_usd` 를 바꾸면 **다음 사이클부터** 적용돼요.

## 준비물

1. **Python 3.11 이상** — https://www.python.org/downloads/ (Windows는 설치 시 "Add to PATH" 체크)
2. **토스증권 Open API 키** — [토스증권 Open API](https://corp.tossinvest.com/ko/open-api) 신청 후
   [개발자센터](https://developers.tossinvest.com)에서 Client ID / Client Secret 발급
   (종합매매 계좌 필요. 메뉴 이름은 바뀔 수 있어요)
3. **알림 채널** (둘 중 하나 이상)
   - 디스코드: 서버 설정 → 연동 → 웹후크 → 새 웹후크 → 채널 선택 → **웹후크 URL 복사**
   - 카카오톡: 아래 [카카오톡 설정](#카카오톡-설정) 참고

## Windows 빠른 설치 (더블클릭)

1. Python 3.11+ 설치 (설치 화면에서 **Add python.exe to PATH** 체크)
2. 이 폴더를 내려받기 (아래 둘 중 하나)
   - Git: `git clone -b claude/awesome-tesla-i33q3e https://github.com/choy1379/shibuya-sky-status.git`
   - 또는 GitHub에서 브랜치 `claude/awesome-tesla-i33q3e` 선택 → Code → **Download ZIP** → 압축 풀기
3. `tecl-infinite-buy` 폴더에서

| 파일 | 하는 일 |
|---|---|
| `setup.cmd` | 파이썬 확인 → `config.toml` 생성 → 자체 테스트 → 메모장으로 설정 열기 |
| `kakao-login.cmd` | (카톡 쓸 때) 카카오 토큰 발급 |
| `check.cmd` | 알림 테스트 + 상태 + 다음 장 주문 미리보기 |
| `start.cmd` / `stop.cmd` | 봇을 백그라운드로 시작 / 중지 (로그: `state\bot.log`) |
| `autostart-on.cmd` / `autostart-off.cmd` | 윈도우 로그인 시 자동 시작 켜기 / 끄기 |

## 설치 · 설정

```bash
cd tecl-infinite-buy
copy config.example.toml config.toml     # Windows   (mac/linux: cp config.example.toml config.toml)
```

`config.toml` 에서 최소한 아래를 채웁니다.

```toml
[toss]
client_id = "c_..."
client_secret = "s_..."

[strategy]
capital_usd = 10000        # 이번 사이클 원금(달러) → 1회 매수금 $500

[notify.discord]
webhook_url = "https://discord.com/api/webhooks/..."
```

비밀값을 파일에 두기 싫으면 비워두고 환경변수 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`,
`DISCORD_WEBHOOK_URL`, `KAKAO_REST_API_KEY` 로 넣어도 됩니다. `config.toml` 과 `state/` 는 git에 올라가지 않습니다.

## 처음 실행 순서

```bash
python -m laoer notify-test   # 1. 디스코드/카톡에 테스트 메시지가 오는지
python -m laoer status        # 2. 계좌·보유·T값·다음 장 시간이 맞는지
python -m laoer plan          # 3. 다음 정규장에 낼 주문 미리보기 (아무것도 안 바뀜)
python -m laoer run           # 4. 상시 실행 (dry_run = true 상태로 며칠 지켜보기)
```

계획이 기대대로면 `config.toml` 에서 `dry_run = false` 로 바꾸고 `run` 을 다시 시작하세요.

### 알림 전용 모드 (주문은 내가 직접)

봇이 주문하지 않고 **"오늘 넣을 주문"만 알려주게** 하려면 `config.toml` 에서

```toml
[run]
mode = "alert"
order_offset_minutes = -60   # 선택: 정규장 1시간 전에 미리 알림 받기
```

- 매일 계산된 주문(평단LOC·별LOC·쿼터LOC·목표지정가, 가격·수량)을 알림으로 받고 토스 앱에서 직접 넣습니다.
- 직접 넣은 미체결 주문이 있어도 건너뛰지 않습니다.
- 마감 후에는 장 전/후 **잔고를 비교**해서 보유 수량·평단·T 변화를 알려주고, 보유가 0주가 되면 사이클 완료를 알려줍니다.
  (직접 넣은 주문의 체결가는 봇이 모르니 실현손익은 토스 앱에서 확인)
- 매매 기능은 그대로라서 나중에 `mode = "trade"` 로 바꾸면 자동매매로 돌아갑니다.

| 명령 | 설명 |
|---|---|
| `run` | 상시 실행. 매 정규장 주문(알림 모드면 주문 안내) → 마감 후 결과 알림 |
| `plan` | 다음 정규장 주문 미리보기 (주문/상태 변경 없음) |
| `order [--force]` | 지금 바로 오늘 주문 (`--force`: 봇이 낸 주문을 취소하고 다시 냄) |
| `report [--date YYYY-MM-DD]` | 체결 결과를 지금 확인해 알림 |
| `status [--notify]` | 보유·T값·사이클 상태 (`--notify`면 알림으로도) |
| `notify-test` | 알림 테스트 |
| `kakao-login` | 카카오 토큰 발급 |

## 상시 실행

봇은 미국 장 시간(한국 밤~새벽)에 주문하므로 **그 시간에 컴퓨터가 켜져 있어야** 합니다.

- **Windows**: 터미널에서 `python -m laoer run` 을 켜두거나, 작업 스케줄러에
  "로그온할 때 / 프로그램: `python`, 인수: `-m laoer run`, 시작 위치: `...\tecl-infinite-buy`" 로 등록.
  절전 모드에 들어가지 않게 전원 설정을 바꿔 두세요.
- **상시 서버 (라즈베리파이·클라우드 VM)**: `nohup python3 -m laoer run &` 또는 systemd 서비스.

봇이 꺼져 있다가 켜져도 괜찮습니다. 이미 낸 날은 다시 내지 않고(상태 파일 + `clientOrderId` 멱등 키),
마감 20분 전이 지나서 켜졌다면 그날은 건너뛰었다고 알려줍니다.

## 카카오톡 설정

카톡 알림은 내 카카오톡 **'나와의 채팅'** 으로 옵니다.

1. [Kakao Developers](https://developers.kakao.com) → 내 애플리케이션 → 애플리케이션 추가
2. **앱 키 → REST API 키** 를 `config.toml` 의 `[notify.kakao] rest_api_key` 에 입력
3. **카카오 로그인** 활성화, **Redirect URI** 에 `https://localhost` 등록 (config 의 `redirect_uri` 와 같게)
4. **동의항목** 에서 "카카오톡 메시지 전송(talk_message)" 을 사용으로 설정
5. **플랫폼 → Web 사이트 도메인** 에 `https://tossinvest.com` 등록 (메시지 버튼 링크용)
6. `python -m laoer kakao-login` 실행 → 출력된 주소를 브라우저로 열어 동의 →
   `https://localhost/?code=...` 로 이동하면(페이지가 안 떠도 정상) **주소창 URL 전체를 붙여넣기**

토큰은 `state/kakao_token.json` 에 저장되고 자동 갱신됩니다. 오래(약 2달) 안 쓰다 만료되면 `kakao-login` 을 다시 하세요.
카톡 텍스트 메시지는 200자 제한이 있어서 긴 알림은 `(1/2)`, `(2/2)` 로 나눠서 보냅니다.

## 알림 예시

```
📝 TECL 주문 · 10/07 · 전반전 T7.50
보유 37주 · 평단 $101.23 · 현재가 $98.40 (-2.80%)
T 7.50/20 · 별% +3.75% · 별지점 $105.03 · 1회 $500.00
매수 평단LOC 2주 @ $101.23 ✅
매수 별LOC 2주 @ $105.02 ✅
매도 쿼터LOC 9주 @ $105.03 ✅
매도 목표지정가 28주 @ $116.42 ✅
매수가능 $6,250.11
```

```
📊 TECL 체결 결과 · 10/07
✅ 매수 평단LOC 2/2주 @ $99.10
✅ 매수 별LOC 2/2주 @ $99.10
▫️ 매도 쿼터LOC 미체결
▫️ 매도 목표지정가 미체결
보유 41주 · 평단 $101.02 · 현재가 $99.10
평가손익 -1.90% · T 8.29/20
사이클 #1 · 9거래일째
```

```
🎉 TECL 사이클 #1 완료
매수 $5,512.40 · 매도 $6,190.25 · 비용 $6.20
실현손익 $671.65 (원금 대비 +6.72%)
```

## 안전장치

- `dry_run = true` 기본값 — 직접 꺼야 실거래
- 같은 종목에 **미체결 수동 주문이 있으면 그날 봇 주문을 건너뛰고** 알림 (`skip_if_open_orders`)
- 매수 총액은 **매수가능금액**과 **사이클 잔여예산** 이하로 자동 축소, 매도 수량은 **매도가능수량** 이하
- 모든 주문에 `clientOrderId`(멱등 키) — 네트워크 오류로 재시도해도 중복 주문 없음
- 주문이 전부 거부되면 10분 간격으로 최대 3번까지 다시 시도, 그래도 안 되면 오류 알림
- 토스 토큰은 `state/toss_token.json` 에 캐시해서 공유 (토스는 클라이언트당 토큰 1개만 유효)

## 구현하지 않은 것 / 알아둘 점

- **리버스모드**(V3.0 소진 이후 규칙)는 구현하지 않았습니다. T ≥ 19면 매수를 멈추고 매도 주문만 내면서 알려주니,
  그 뒤는 직접 판단하세요.
- 소수점(1주 미만) 보유분은 주문에서 무시합니다 (LOC·지정가는 정수 수량만 가능).
- 토스증권의 LOC 접수 가능 시간은 공식 문서에서 확인하지 못해 **정규장 중**(시작 15분 후)에 주문하도록 했습니다.
  거부되면 `order_offset_minutes` 를 조정하세요.
- 3/4 목표 매도는 토스 API가 지원하는 `DAY` 지정가(정규장 종료 시 자동 취소)라서 애프터마켓에서는 체결되지 않습니다.

## 개발

```bash
python -m unittest discover -s tests -t .
```

```
laoer/
  strategy.py   T값·별%·주문 계산 (순수 함수)
  toss.py       토스증권 Open API 클라이언트
  bot.py        스케줄·주문·체결 리포트·사이클 관리
  notify.py     디스코드 웹훅, 카카오 나에게 보내기
  config.py     config.toml 로딩
  state.py      state/state.json 저장
```
