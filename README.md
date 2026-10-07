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
   - ⚠️ **API 키에 허용 IP를 등록해야 합니다.** 봇을 돌릴 PC의 인터넷 IP(브라우저에서 "내 IP" 검색)를
     개발자센터의 키 설정에 추가하세요. 등록 안 된 PC에서는 `403`으로 막힙니다.
3. **알림 채널** (둘 중 하나 이상)
   - 디스코드: 서버 설정 → 연동 → 웹후크 → 새 웹후크 → 채널 선택 → **웹후크 URL 복사**
   - 카카오톡: 아래 [카카오톡 설정](#카카오톡-설정) 참고

## Windows 빠른 설치 (더블클릭)

1. Python 3.11+ 설치 (설치 화면에서 **Add python.exe to PATH** 체크)
2. 이 폴더를 내려받기 (아래 둘 중 하나)
   - Git: `git clone https://github.com/choy1379/infinite-buy-bot.git`
   - 또는 https://github.com/choy1379/infinite-buy-bot → Code → **Download ZIP** → 압축 풀기
3. `infinite-buy-bot` 폴더에서

| 파일 | 하는 일 |
|---|---|
| `setup.cmd` | 파이썬 확인 → `config.toml` 생성 → 자체 테스트 → 메모장으로 설정 열기 |
| `kakao-login.cmd` | (카톡 쓸 때) 카카오 토큰 발급 |
| `check.cmd` | 알림 테스트 + 상태 + 다음 장 주문 미리보기 |
| `start.cmd` / `stop.cmd` | 봇을 백그라운드로 시작 / 중지 (로그: `state\bot.log`) |
| `autostart-on.cmd` / `autostart-off.cmd` | 윈도우 로그인 시 자동 시작 켜기 / 끄기 |
| `wake-task-on.cmd` / `wake-task-off.cmd` | 절전 중이어도 매일 22:50·23:50·05:30·06:30에 PC를 깨워 봇 재시작 (조용히) |

## 설치 · 설정

```bash
cd infinite-buy-bot
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
| `dashboard` | 모니터링 페이지 데이터를 지금 갱신 |
| `live` | 실시간 호가 페이지만 띄움 (`run` 은 자동으로 같이 띄움) |
| `market` | '시장' 탭 선물 시세가 받아지는지 확인 (설정 없이 됨) |
| `notify-test` | 알림 테스트 |
| `kakao-login` | 카카오 토큰 발급 |

## 상시 실행

봇은 미국 장 시간(한국 밤~새벽)에 주문하므로 **그 시간에 컴퓨터가 켜져 있어야** 합니다.

- **Windows**: 터미널에서 `python -m laoer run` 을 켜두거나, 작업 스케줄러에
  "로그온할 때 / 프로그램: `python`, 인수: `-m laoer run`, 시작 위치: `...\infinite-buy-bot`" 로 등록.
  절전 모드에 들어가지 않게 전원 설정을 바꿔 두세요.
- **상시 서버 (라즈베리파이·클라우드 VM)**: `nohup python3 -m laoer run &` 또는 systemd 서비스.

봇이 꺼져 있다가 켜져도 괜찮습니다. 이미 낸 날은 다시 내지 않고(상태 파일 + `clientOrderId` 멱등 키),
마감 20분 전이 지나서 켜졌다면 그날은 건너뛰었다고 알려줍니다.

### 절전·잠금·재시작

| 동작 | 봇 |
|---|---|
| 잠금(`Ctrl+Alt+Del` → 잠금) / 화면 꺼짐 | ✅ 계속 동작 |
| 절전 | `wake-task-on.cmd` 를 실행해 두면 매일 22:50·23:50·05:30·06:30 에 PC를 깨워 처리 |
| 로그아웃 / 시스템 종료 | ❌ 멈춤 (다시 로그인하면 `autostart-on.cmd` 로 자동 시작) |

- 전원 옵션의 "컴퓨터를 절전 모드로 설정"은 **해당 없음**으로 두세요 (`powercfg /change standby-timeout-ac 0`).
- Windows 업데이트가 밤에 재시작하지 않게 **설정 → Windows 업데이트 → 고급 옵션 → 사용 시간**을 밤 시간으로 지정하세요.
- 이미 넣은 주문은 토스 서버에 있으니 PC가 꺼져도 체결은 됩니다. 꺼져 있던 동안의 체결 알림은 다음에 켜질 때 보냅니다.

## 다른 PC로 옮기기

봇의 기억은 전부 `config.toml`(설정·키)과 `state\` 폴더(사이클 기록·오늘 주문 기록·토큰)에 있습니다.
이 둘만 옮기면 그대로 이어서 돕니다.

1. 기존 PC에서 `stop.cmd` (그리고 쓰고 있었다면 `wake-task-off.cmd`, `autostart-off.cmd`)
2. 새 PC에서 `git clone https://github.com/choy1379/infinite-buy-bot.git`
3. 기존 PC의 **`config.toml` 과 `state` 폴더**를 새 폴더에 복사
4. 새 PC의 IP를 토스 개발자센터 허용 IP에 추가
5. `check.cmd` 로 상태가 전과 같은지 확인 → `start.cmd` (필요하면 `wake-task-on.cmd`, `autostart-on.cmd`)

⚠️ **봇은 한 대에서만 돌리세요.** 두 PC에서 동시에 `start.cmd` 를 켜면 서로의 기록을 몰라서 **같은 주문을 두 번** 넣습니다.
다른 PC에서는 `check.cmd`·`plan`·`dashboard` 처럼 조회만 하는 명령까지만 쓰세요
(토스 토큰이 재발급되지만 실행 중인 봇이 알아서 다시 받습니다).

`config.toml` 에는 키와 토큰이 들어 있으니 USB·나에게 보내기처럼 나만 보는 경로로 옮기고, 깃·채팅에는 올리지 마세요.

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `토스증권 API 오류: [403 ...]` | 이 PC의 IP가 토스 API 키 허용 IP에 없음 → 개발자센터에서 IP 추가. 집 인터넷은 공유기 재부팅 등으로 **IP가 바뀔 수 있으니** 갑자기 403이 나면 먼저 확인 |
| `알림 채널이 설정되지 않았습니다` | `config.toml` 에 디스코드 웹후크/카카오 키가 비어 있음 |
| `1회 매수금이 현재가보다 작아 …` | `capital_usd ÷ splits` 가 1주 값보다 작음 → 원금을 올리거나 분할 수를 줄이기 |
| `오늘 주문 건너뜀 · 미체결 주문이 있어…` | 같은 종목에 수동 주문이 걸려 있음 → 정리 후 `python -m laoer order --force` |
| 모니터링 페이지 "소식 없음" | 8시간 넘게 갱신 없음 → 봇 PC가 꺼졌거나 절전 중 |
| 원금을 바꿨는데 1회 매수금이 그대로 | 1회 매수금은 사이클 시작 때 고정 → `stop.cmd` → `state\state.json` 삭제 → 원금 변경 → `start.cmd` (보유분은 이어받음) |

## 모니터링 페이지

폰에서도 보는 페이지: **https://choy1379.github.io/infinite-buy-bot/**

- 이 저장소는 **공개**라서 (그래야 무료로 GitHub Pages를 쓸 수 있음) 두 단계로 보여줍니다.
  - 누구나: T값, 평가손익(%), 사이클, 다음 주문 시각, 날짜별 주문의 체결 여부, 완료 사이클 수익률(%)
  - **비밀번호 입력 시**: 보유 수량·평단·현재가·평가금액·매수가능금액·원금, 주문 가격/수량/체결가, 실현손익($)
- 금액 정보는 봇이 비밀번호로 **암호화해서** 올리고, 페이지가 브라우저 안에서 풉니다. 비밀번호는 어디로도 전송되지 않아요.
  ("이 기기에서 기억"을 체크하면 그 브라우저에만 저장)
- 날짜별 주문은 **달력**(월~금)으로 보이고, 칸마다 그날의 **일간 수익률**과 체결된 매수/매도가 표시돼요. 날짜를 누르면 그날 주문 상세가 아래에 나와요.
  일간 수익률 = 그날 손익 ÷ (전일 종가 기준 평가금 + 그날 매수금). 잠금을 풀면 일간 손익($)과 월간 합계도 보여요.
- **다음 주문 계획**: 봇이 아직 주문을 안 낸 다음 정규장에 낼 주문(평단/별 LOC, 쿼터매도, 목표가)과 별%·별지점. 가격·수량은 잠금 해제 시. 첫 매수 한도가는 예상치(주문 직전 현재가로 다시 계산).
- 날짜를 누르면(잠금 해제 시) **일간 수익률이 어디서 나왔는지** 보여줘요: 전일 종가→종가 변동, 보유분 가격 변동, 매수·매도 몫, 평단 대비 수익률, 환율과 원화 손익(어림). 일간 수익률은 달러 기준이고 환율은 반영하지 않아요. 환율은 야후 시세를 봇이 종가 기록 때 함께 남겨요.
- 데이터가 아직 없을 때 화면을 미리 보려면 주소 끝에 **`?demo=1`** (가짜 모의 매매 데이터).
- 봇이 주문·체결 리포트·오류 때, 그리고 3시간마다 `tecl-data` 브랜치의 `dashboard.json` 을 갱신합니다.

설정:
1. GitHub → 오른쪽 위 프로필 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
   - Repository access: **Only select repositories → infinite-buy-bot**
   - Permissions → Repository permissions → **Contents: Read and write**
2. `config.toml` 의 `[dashboard]` 에 토큰과 비밀번호(8자 이상) 입력
3. `python -m laoer dashboard` 로 한 번 올려보고 페이지 확인 → `stop.cmd` / `start.cmd` 로 봇 재시작



### 실시간 호가 (봇 PC에서)

GitHub 페이지는 봇이 몇 시간마다 올리는 파일만 볼 수 있어서 실시간 호가는 봇 PC가 직접 띄웁니다.

- 봇(`start.cmd`)이 돌면 이 PC에서 **http://localhost:8765/** 가 열립니다. 위 모니터링 페이지와 같은 화면에 **실시간 호가**(1초마다 갱신, 매도·매수 10단계)가 더해져요.
- 같은 와이파이의 폰에서는 **http://<봇 PC의 IP>:8765/** (PC IP는 `ipconfig` 의 IPv4 주소). 처음에 Windows 방화벽 창이 뜨면 **개인 네트워크** 허용.
- 호가는 화면을 보고 있을 때만, 여러 기기가 봐도 초당 1번만 토스에서 가져옵니다 (시세 API 한도 초당 15회).
- **봇 PC에서는 GitHub 페이지(https://choy1379.github.io/infinite-buy-bot/)에도 호가가 떠요.** 페이지가 이 PC의 `http://localhost:8765` 에 물어보기 때문이에요. 처음에 브라우저가 "이 기기의 다른 앱 및 서비스에 액세스" 같은 권한을 물으면 **허용**.
  다른 기기(폰)에서는 GitHub 페이지가 집 PC에 닿을 수 없어서(https 페이지는 공유기 안의 http 주소를 못 부름) 위의 PC IP 주소로 여세요.
  https 터널 주소가 있다면 `https://choy1379.github.io/infinite-buy-bot/?live=https://터널주소` 로 한 번 열면 그 브라우저에 기억돼요 (`?live=off` 로 해제).
- **밖(폰 LTE·회사)에서도 보려면 Cloudflare 무료 터널**: 봇 PC에 `winget install Cloudflare.cloudflared` 로 설치하고 `config.toml` 의 `[live]` 에 `tunnel = true` 후 봇 재시작. 계정·도메인·카드 없이 무료(Cloudflare quick tunnel)이고, 봇이 `https://xxxx.trycloudflare.com` 주소를 받아 대시보드 **잠금 칸**에 올려요. GitHub 페이지에서 비밀번호로 잠금 해제하면 그 주소로 호가·시세가 떠요. 주소는 봇을 켤 때마다 바뀌지만 페이지가 알아서 따라가요. 무료 임시 터널이라 가끔 끊길 수 있고(봇이 다시 켬), 주소를 아는 사람은 공개 시세만 볼 수 있어요.
- 봇 없이 페이지만 띄우려면 `python -m laoer live`. 끄려면 `config.toml` 에 `[live]` `port = 0`.
- 공개 시세와 이미 공개된 `dashboard.json` 만 내보내고 계좌 정보는 다루지 않아요.

### 시장 탭

페이지 위 **시장** 탭 (`#market` 으로 바로 열기, `?demo=1#market` 은 가짜 시세).

- **선물 시세**: 코스피200 선물(18시~06시는 야간선물), WTI 원유, S&P500·나스닥100 E-mini. 네이버·야후 공개 시세를 봇 PC가 10초마다 대신 받아오므로(브라우저가 직접 못 부름) 호가처럼 **봇 PC에서 열었을 때만** 보여요. 코스피200 야간선물은 네이버에 없어서 18시~06시엔 prober.kr 1분봉의 마지막 종가를 쓰고(변동은 주간 종가 대비), 받지 못하면 "주간 종가"로 표시해요. prober는 곧 닫을 예정이라 그때부터는 주간 종가만 보여요. 비공식 주소라 바뀔 수 있어서, 안 뜨면 봇 PC에서 `python -m laoer market` 으로 확인하세요.
- **미국 주식 히트맵**: TradingView 위젯이라 어디서나 보여요. 위쪽 메뉴로 S&P500·나스닥100 등을 바꿀 수 있어요.

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
  dashboard.py  모니터링 페이지 데이터 (암호화 후 GitHub에 올림)
  live.py       봇 PC의 실시간 호가 페이지 서버
  market.py     시장 탭 선물 시세 (네이버·야후)
  config.py     config.toml 로딩
  state.py      state/state.json 저장
index.html      모니터링 페이지 (GitHub Pages, 봇 PC에서는 실시간 호가 포함)
tests/          python -m unittest discover -s tests -t .
```
