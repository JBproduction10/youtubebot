"""
YouTubeBot — Local Bot
======================
Runs on YOUR machine (Windows/Mac/Linux) where Chrome is available.
Polls your PythonAnywhere server for commands, runs Chrome, and reports
state back so the dashboard stays in sync.

Setup:
  pip install undetected-chromedriver fake-useragent selenium requests

Usage:
  BOT_SERVER=https://jbangala.pythonanywhere.com BOT_SECRET=change-me-please python bot_local.py

  Or just edit the CONFIG section below and run: python bot_local.py
"""

import json
import os
import queue
import random
import threading
import time
from datetime import datetime

import requests

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_OK = True
except ImportError:
    print("ERROR: Run:  pip install undetected-chromedriver selenium requests fake-useragent")
    SELENIUM_OK = False

try:
    from fake_useragent import UserAgent
    _ua = UserAgent()
    def random_user_agent():
        return _ua.chrome
except Exception:
    def random_user_agent():
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36")

# ── CONFIG — edit these or set as environment variables ───────────────────────
BOT_SERVER = os.environ.get("BOT_SERVER", "https://jbangala.pythonanywhere.com")
BOT_SECRET = os.environ.get("BOT_SECRET", "change-me-please")
POLL_INTERVAL = 2  # seconds between polls

# ── Runtime state ─────────────────────────────────────────────────────────────
_driver = None
_bot_thread = None
_stop_event = threading.Event()
_pause_event = threading.Event()
_state_lock = threading.Lock()

local_state = {
    "running": False,
    "paused": False,
    "current_url": "",
    "current_index": 0,
    "total_videos": 0,
    "loop_current": 0,
    "loop_total": 0,
    "duration": 0,
    "elapsed": 0,
    "ads_skipped": 0,
    "videos_played": 0,
    "start_time": None,
    "log": [],
}

config = {}  # loaded from server on first poll


# ── Server communication ──────────────────────────────────────────────────────
HEADERS = {"X-Bot-Secret": BOT_SECRET, "Content-Type": "application/json"}


def report(updates: dict, log_entry: dict = None):
    """Push state updates to the server."""
    payload = {k: v for k, v in updates.items()}
    if log_entry:
        payload["log_entry"] = log_entry
    try:
        requests.post(f"{BOT_SERVER}/bot/report", json=payload, headers=HEADERS, timeout=5)
    except Exception as e:
        print(f"[report error] {e}")


def poll_commands():
    """Fetch pending commands + latest config from server."""
    try:
        r = requests.get(f"{BOT_SERVER}/bot/poll", headers=HEADERS, timeout=5)
        return r.json()
    except Exception as e:
        print(f"[poll error] {e}")
        return {"commands": [], "config": {}}


def log_msg(msg: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    print(f"[{entry['time']}] [{level.upper()}] {msg}")
    with _state_lock:
        local_state["log"].append(entry)
        if len(local_state["log"]) > 200:
            local_state["log"] = local_state["log"][-200:]
    report({}, log_entry=entry)


def push_state():
    with _state_lock:
        snap = {k: v for k, v in local_state.items() if k != "log"}
    report(snap)


# ── Chrome helpers ────────────────────────────────────────────────────────────
def build_driver():
    opts = uc.ChromeOptions()
    if config.get("mute"):
        opts.add_argument("--mute-audio")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={random_user_agent()}")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    return uc.Chrome(options=opts, use_subprocess=True)


def parse_duration(text: str) -> int:
    parts = text.strip().split(":")
    secs = 0
    for i, p in enumerate(reversed(parts)):
        secs += int(p) * (60 ** i)
    return secs


def dismiss_consent(driver):
    xpaths = [
        "//button[contains(.,'Accept all')]",
        "//button[contains(.,'I Agree')]",
        "//button[contains(.,'Agree')]",
        "//button[contains(.,'Accept')]",
    ]
    for xp in xpaths:
        try:
            driver.find_element(By.XPATH, xp).click()
            time.sleep(random.uniform(0.5, 1.5))
            return
        except Exception:
            pass


def ensure_playing(driver):
    try:
        btn = driver.find_element(By.CSS_SELECTOR, "button.ytp-play-button")
        title = btn.get_attribute("title") or ""
        if "Play" in title or "Paused" in title:
            btn.click()
    except Exception:
        pass


def get_duration(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CLASS_NAME, "ytp-time-duration"))
        )
        text = driver.find_element(By.CLASS_NAME, "ytp-time-duration").text
        if text:
            return parse_duration(text)
    except Exception:
        pass
    return None


def watch_with_ad_skip(driver, watch_seconds: int):
    ad_sels = [
        ".ytp-skip-ad-button",
        ".ytp-ad-skip-button",
        "button.ytp-ad-skip-button-modern",
    ]
    elapsed = 0
    while elapsed < watch_seconds:
        if _stop_event.is_set():
            return

        while _pause_event.is_set() and not _stop_event.is_set():
            time.sleep(0.5)

        for sel in ad_sels:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    btn.click()
                    with _state_lock:
                        local_state["ads_skipped"] += 1
                    log_msg("⏭  Ad skipped", "success")
                    push_state()
                    time.sleep(0.8)
                    break
            except Exception:
                pass

        time.sleep(1)
        elapsed += 1
        with _state_lock:
            local_state["elapsed"] = elapsed
        if elapsed % 5 == 0:
            push_state()


def play_video(driver, url: str, attempt: int = 1) -> bool:
    log_msg(f"▶  Loading video (attempt {attempt}): {url[:60]}...")
    with _state_lock:
        local_state["current_url"] = url
        local_state["elapsed"] = 0
        local_state["duration"] = 0
    push_state()

    try:
        driver.get(url)
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        log_msg(f"Load error: {e}", "error")
        return False

    dismiss_consent(driver)
    ensure_playing(driver)

    duration = get_duration(driver)
    if duration is None:
        log_msg("Duration unknown — defaulting to 3 min", "warn")
        duration = 180

    watch_ratio = config.get("watch_ratio", 1.0)
    watch_time = int(duration * watch_ratio) + 2
    with _state_lock:
        local_state["duration"] = duration

    log_msg(f"⏱  Duration: {duration}s | Watching: {watch_time}s")
    push_state()

    watch_with_ad_skip(driver, watch_time)

    with _state_lock:
        local_state["videos_played"] += 1

    delay_min = config.get("delay_min", 3)
    delay_max = config.get("delay_max", 10)
    delay = random.uniform(delay_min, delay_max)
    log_msg(f"✓  Done. Pausing {delay:.1f}s...", "success")
    time.sleep(delay)
    return True


# ── Bot main thread ────────────────────────────────────────────────────────────
def bot_worker():
    global _driver
    log_msg("🚀 Bot starting — launching Chrome...")
    try:
        _driver = build_driver()
    except Exception as e:
        log_msg(f"Chrome failed to start: {e}", "error")
        with _state_lock:
            local_state["running"] = False
        push_state()
        return

    loop_num = 0
    try:
        while not _stop_event.is_set():
            loop_num += 1
            loop_total = config.get("loops", 0)
            if loop_total != 0 and loop_num > loop_total:
                log_msg("All loops complete.")
                break

            with _state_lock:
                local_state["loop_current"] = loop_num
                local_state["loop_total"] = loop_total

            videos = config.get("videos", [])[:]
            if config.get("shuffle", True):
                random.shuffle(videos)

            with _state_lock:
                local_state["total_videos"] = len(videos)

            log_msg(f"── Loop {loop_num}{' of ' + str(loop_total) if loop_total else ' (∞)'} ──")
            push_state()

            for idx, url in enumerate(videos, 1):
                if _stop_event.is_set():
                    break
                with _state_lock:
                    local_state["current_index"] = idx
                push_state()
                log_msg(f"Video {idx}/{len(videos)}")

                max_retries = config.get("max_retries", 2)
                success = False
                for attempt in range(1, max_retries + 2):
                    if _stop_event.is_set():
                        break
                    if play_video(_driver, url, attempt):
                        success = True
                        break
                    log_msg("Retrying in 5s...", "warn")
                    time.sleep(5)

                if not success:
                    log_msg(f"Skipped after {max_retries + 1} attempts", "error")

            if not _stop_event.is_set():
                gap = random.uniform(5, 15)
                log_msg(f"Loop done. Next in {gap:.1f}s...")
                time.sleep(gap)

    except Exception as e:
        log_msg(f"Bot crashed: {e}", "error")
    finally:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None
        with _state_lock:
            local_state["running"] = False
            local_state["current_url"] = ""
        log_msg("Bot stopped.")
        push_state()


# ── Command polling loop (main thread) ────────────────────────────────────────
def handle_command(cmd: str):
    global _bot_thread

    if cmd == "start":
        if local_state["running"]:
            return
        _stop_event.clear()
        _pause_event.clear()
        with _state_lock:
            local_state.update({
                "running": True,
                "paused": False,
                "ads_skipped": 0,
                "videos_played": 0,
                "start_time": datetime.now().isoformat(),
                "log": [],
            })
        _bot_thread = threading.Thread(target=bot_worker, daemon=True)
        _bot_thread.start()
        push_state()

    elif cmd == "stop":
        _stop_event.set()
        _pause_event.clear()
        with _state_lock:
            local_state["paused"] = False
        push_state()

    elif cmd == "pause":
        if not local_state["running"]:
            return
        with _state_lock:
            if local_state["paused"]:
                _pause_event.clear()
                local_state["paused"] = False
                log_msg("▶  Resumed")
            else:
                _pause_event.set()
                local_state["paused"] = True
                log_msg("⏸  Paused")
        push_state()

    elif cmd == "skip":
        with _state_lock:
            local_state["elapsed"] = local_state["duration"] + 999
        log_msg("⏭  Skipping current video...")


def main():
    global config
    print(f"[YouTubeBot] Connecting to {BOT_SERVER}")
    print(f"[YouTubeBot] Polling every {POLL_INTERVAL}s for commands...")
    print("[YouTubeBot] Open your dashboard and click Start.\n")

    while True:
        result = poll_commands()
        # Sync config from server
        if result.get("config"):
            config = result["config"]
        # Handle any queued commands
        for cmd in result.get("commands", []):
            print(f"[command] {cmd}")
            handle_command(cmd)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not SELENIUM_OK:
        exit(1)
    main()