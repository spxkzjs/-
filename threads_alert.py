"""쓰레드(Threads) 조회수 알리미.

공식 API 없이 공개 웹페이지에서 조회수를 읽어, 설정한 기준을 넘으면 텔레그램으로 알린다.

  python threads_alert.py            # 한 번 확인하고 종료 (GitHub Actions / cron 용)
  python threads_alert.py --loop     # 계속 실행하며 interval_minutes 마다 확인 (VPS 용)
  python threads_alert.py --dry-run  # 텔레그램 대신 화면에 출력
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

# 이 UA로 요청하면 서버가 조회수를 포함한 HTML을 바로 내려준다 (브라우저 불필요).
CRAWLER_UA = "facebookexternalhit/1.1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
VIEW_RE = re.compile(r'"view_counts":(\d+)')
POST_URL = "https://www.threads.com/@{user}/post/{code}"
POST_URL_RE = re.compile(r"threads\.(?:com|net)/@([\w.]+)/post/([\w-]+)")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 수집

class Browser:
    """Playwright는 필요할 때만 띄운다 (계정 목록 수집, 조회수 폴백)."""

    def __init__(self):
        self._pw = None
        self._browser = None

    def page(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        return self._browser.new_page(user_agent=BROWSER_UA, locale="ko-KR")

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


def recent_post_codes(browser, username, limit):
    """프로필 페이지에서 최근 게시물 코드를 위에서부터 limit개 가져온다."""
    page = browser.page()
    try:
        page.goto(f"https://www.threads.com/@{username}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(f'a[href*="/@{username}/post/"]', timeout=30000)
        hrefs = page.eval_on_selector_all(
            f'a[href*="/@{username}/post/"]', "els => els.map(e => e.getAttribute('href'))"
        )
    finally:
        page.close()
    codes = []
    for href in hrefs:
        m = re.search(r"/post/([\w-]+)", href)
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    return codes[:limit]


def parse_view_text(text):
    """화면에 보이는 '조회 42만회', '조회 1.2천회', '420K views' 같은 글자를 숫자로 바꾼다."""
    m = re.search(r"조회\s*([\d.,]+)\s*(천|만|억)?\s*회", text)
    if m:
        units = {"천": 1_000, "만": 10_000, "억": 100_000_000}
        return int(float(m.group(1).replace(",", "")) * units.get(m.group(2), 1))
    m = re.search(r"([\d.,]+)\s*([KMB])?\s*views?\b", text, re.I)
    if m:
        units = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        return int(float(m.group(1).replace(",", "")) * units.get((m.group(2) or "").upper(), 1))
    return None


def fetch_views(browser, username, code):
    """세 가지 방법을 차례로 시도한다. 하나가 막혀도 나머지로 읽을 수 있게."""
    url = POST_URL.format(user=username, code=code)
    # 1) 크롤러 UA로 HTML 요청 → 정확한 숫자
    try:
        r = requests.get(url, headers={"User-Agent": CRAWLER_UA}, timeout=30)
        m = VIEW_RE.search(r.text)
        if m:
            return int(m.group(1))
    except requests.RequestException as e:
        log(f"  요청 실패, 브라우저로 재시도: {e}")
    page = browser.page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        # 2) 브라우저로 연 페이지 데이터 → 정확한 숫자
        m = VIEW_RE.search(page.content())
        if m:
            return int(m.group(1))
        # 3) 화면에 보이는 글자 → 반올림된 숫자 (예: 42만)
        views = parse_view_text(page.inner_text("body"))
        if views is not None:
            log(f"  {username}/{code}: 화면 글자로 읽음 (반올림 값)")
        return views
    finally:
        page.close()


# ---------------------------------------------------------------- 알림

def send_telegram(text, token, chat_id, dry_run):
    if dry_run:
        log("[DRY-RUN 알림]\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    if not r.ok:
        log(f"텔레그램 전송 실패: {r.status_code} {r.text}")


# ---------------------------------------------------------------- 메인 로직

FAIL_RUNS_BEFORE_ALERT = 3  # 일시적인 네트워크 오류로 알림이 울리지 않게


def check_health(state, errors, token, chat_id, dry_run):
    """연속으로 실패하면 '알리미 고장' 알림을, 다시 정상이 되면 복구 알림을 한 번씩 보낸다."""
    if errors:
        state["fail_runs"] = state.get("fail_runs", 0) + 1
        if state["fail_runs"] >= FAIL_RUNS_BEFORE_ALERT and not state.get("broken"):
            state["broken"] = True
            detail = "\n".join(f"- {e}" for e in errors[:5])
            send_telegram(
                f"⚠️ 알리미 고장 의심 ({state['fail_runs']}회 연속 실패)\n{detail}\n"
                "쓰레드 페이지 구조가 바뀌었거나 접속이 막혔을 수 있습니다.",
                token, chat_id, dry_run,
            )
    else:
        if state.get("broken"):
            send_telegram("✅ 알리미가 다시 정상 동작합니다.", token, chat_id, dry_run)
        state["fail_runs"] = 0
        state["broken"] = False


def run_once(config, dry_run):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")
    if not dry_run and not (token and chat_id):
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")

    thresholds = sorted(config["thresholds"])
    limit = config.get("posts_per_account", 10)
    alert_existing = config.get("alert_existing_posts", False)
    state = load_json(STATE_PATH, {"posts": {}, "accounts_seen": []})
    posts = state["posts"]

    # 확인할 게시물 목록: (username, code, 새로 발견된 기존 게시물인지)
    targets = []
    errors = []
    browser = Browser()
    try:
        for username in config.get("accounts", []):
            username = username.lstrip("@")
            first_time = username not in state["accounts_seen"]
            try:
                codes = recent_post_codes(browser, username, limit)
            except Exception as e:
                log(f"@{username} 프로필 수집 실패: {e}")
                errors.append(f"@{username} 게시물 목록 수집 실패")
                continue
            log(f"@{username}: 최근 게시물 {len(codes)}개")
            for code in codes:
                targets.append((username, code, first_time))
            if first_time:
                state["accounts_seen"].append(username)

        for url in config.get("posts", []):
            m = POST_URL_RE.search(url)
            if m:
                targets.append((m.group(1), m.group(2), False))
            else:
                log(f"게시물 주소 형식이 이상합니다: {url}")

        for username, code, baseline in targets:
            key = f"{username}/{code}"
            try:
                views = fetch_views(browser, username, code)
            except Exception as e:
                log(f"  {key} 조회 실패: {e}")
                errors.append(f"{key} 조회 실패")
                continue
            if views is None:
                log(f"  {key} 조회수를 찾지 못했습니다 (페이지 구조 변경 가능성)")
                errors.append(f"{key} 조회수 못 찾음")
                continue

            entry = posts.setdefault(key, {"notified": [], "views": 0})
            entry["views"] = views
            entry["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            crossed = [t for t in thresholds if views >= t and t not in entry["notified"]]
            log(f"  {key}: {views:,}회")
            if not crossed:
                continue
            entry["notified"].extend(crossed)
            # 계정을 처음 등록할 때 이미 기준을 넘어 있던 게시물은 조용히 기록만 한다.
            if baseline and not alert_existing:
                continue
            text = (
                f"🔥 조회수 {max(crossed):,}회 돌파!\n"
                f"@{username} · 현재 {views:,}회\n"
                f"{POST_URL.format(user=username, code=code)}"
            )
            send_telegram(text, token, chat_id, dry_run)
            time.sleep(1)

        check_health(state, errors, token, chat_id, dry_run)
    finally:
        browser.close()
        save_json(STATE_PATH, state)


def main():
    parser = argparse.ArgumentParser(description="쓰레드 조회수 텔레그램 알리미")
    parser.add_argument("--loop", action="store_true", help="계속 실행")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 대신 화면 출력")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"설정 파일이 없습니다: {CONFIG_PATH}")

    while True:
        run_once(config, args.dry_run)
        if not args.loop:
            break
        minutes = config.get("interval_minutes", 15)
        log(f"{minutes}분 후 다시 확인합니다.")
        time.sleep(minutes * 60)
        config = load_json(CONFIG_PATH, config)  # 실행 중 설정 변경 반영


if __name__ == "__main__":
    main()
