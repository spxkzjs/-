# 쓰레드 조회수 알리미

지정한 쓰레드 계정(또는 게시물)의 조회수가 기준을 넘으면 텔레그램으로 알려줍니다.
공식 API를 쓰지 않고 공개 웹페이지에서 조회수를 읽습니다.

## 1. 텔레그램 봇 준비

1. 텔레그램에서 **@BotFather** → `/newbot` → 봇 이름 정하기 → **토큰** 복사
2. 만든 봇에게 아무 메시지나 보내기
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열기 → `"chat":{"id":123456789` 의 숫자가 **chat_id**

## 2. 설정 (`config.json`)

| 항목 | 설명 |
|---|---|
| `accounts` | 감시할 계정 아이디 목록 (`@` 없이). 최근 게시물을 자동으로 확인 |
| `posts` | 특정 게시물만 감시하려면 주소를 넣기 (`https://www.threads.com/@아이디/post/코드`) |
| `thresholds` | 알림 기준 조회수. 각 기준을 넘을 때마다 한 번씩 알림 |
| `posts_per_account` | 계정마다 확인할 최근 게시물 수 |
| `alert_existing_posts` | `false`면 계정을 처음 등록할 때 이미 기준을 넘은 게시물은 알리지 않음 |
| `interval_minutes` | `--loop` 모드일 때 확인 간격(분) |

## 3-A. 집 PC에서 돌리기 (추천 — 차단 위험이 가장 낮음)

1. `config.json` 에 토큰 넣기:
   ```json
   "telegram_bot_token": "봇 토큰",
   "telegram_chat_id": "chat_id",
   ```
2. `설치.bat` 더블클릭 (처음 한 번만)
3. `실행.bat` 더블클릭 → 창이 켜져 있는 동안 계속 확인합니다. 오류로 꺼져도 1분 뒤 자동으로 다시 시작합니다.
4. 컴퓨터를 켤 때 자동으로 실행하려면: `Win + R` → `shell:startup` 입력 → 열린 폴더에 `실행.bat` 의 **바로가기**를 넣기

## 3-B. 서버에서 돌리기 — GitHub Actions (무료, PC를 꺼도 동작)

1. GitHub에 **비공개(Private)** 저장소를 만들고 이 폴더의 파일을 전부 올리기
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서
   - `TELEGRAM_BOT_TOKEN` = 봇 토큰
   - `TELEGRAM_CHAT_ID` = chat_id
3. **Actions** 탭 → "Threads 조회수 확인" → **Run workflow** 로 한 번 실행해 동작 확인
4. 이후 15분마다 자동 실행됩니다. 진행 상태는 `state.json` 에 자동 저장됩니다.

설정을 바꾸려면 GitHub에서 `config.json` 을 직접 수정하면 됩니다.

## (선택) PC나 VPS에서 직접 돌리기

```bash
pip install -r requirements.txt
python -m playwright install chromium
python threads_alert.py --dry-run   # 알림 대신 화면에 출력해서 테스트
```

토큰은 환경변수(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)로 주거나 `config.json` 에
`"telegram_bot_token"`, `"telegram_chat_id"` 항목으로 넣으면 됩니다.
계속 켜두려면 `python threads_alert.py --loop`.

## 고장 대비

- **조회수를 세 가지 방법으로 읽습니다.** 하나가 막혀도 나머지로 계속 읽습니다.
  1. 페이지 HTML의 정확한 숫자
  2. 브라우저로 연 페이지의 정확한 숫자
  3. 화면에 보이는 "조회 42만회" 글자 (반올림된 값)
- **3회 연속 실패하면 텔레그램으로 "⚠️ 알리미 고장 의심" 알림이 옵니다.** 다시 정상이 되면 "✅ 복구" 알림이 옵니다.
- GitHub Actions는 Meta가 서버 IP를 막을 수 있습니다. 고장 알림이 계속 오면 집 PC 방식으로 바꾸세요.
- 너무 많은 계정을 짧은 간격으로 확인하면 차단될 수 있으니 적당히 사용하세요.
