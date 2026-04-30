"""
YouTubeBot — Flask + Web GUI
================================
Run:
    pip install flask undetected-chromedriver fake-useragent
    python app.py

Then open: http://localhost:5000
"""

import json
import queue
import random
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

# ── Try importing bot dependencies ─────────────────────────────────────────────
try:
    import undetected_chromedriver as uc
    from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_OK = True
except ImportError:
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

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Shared bot state ───────────────────────────────────────────────────────────
state = {
    "running": False,
    "paused": False,
    "current_url": "",
    "current_index": 0,
    "total_videos": 0,
    "loop_current": 0,
    "loop_total": 0,       # 0 = infinite
    "duration": 0,
    "elapsed": 0,
    "ads_skipped": 0,
    "videos_played": 0,
    "start_time": None,
    "log": [],
}

config = {
    "videos": [
        "https://www.youtube.com/watch?v=7BIIoZi7GVM",
        "https://www.youtube.com/watch?v=6f5pMmJFYeE",
        "https://www.youtube.com/watch?v=L_TVnSNVhgo",
    ],
    "loops": 0,
    "shuffle": True,
    "headless": False,
    "mute": False,
    "watch_ratio": 1.0,
    "delay_min": 3,
    "delay_max": 10,
    "max_retries": 2,
}

_driver = None
_bot_thread = None
_stop_event = threading.Event()
_pause_event = threading.Event()
_sse_clients: list[queue.Queue] = []
_state_lock = threading.Lock()


# ── SSE broadcasting ───────────────────────────────────────────────────────────
def broadcast(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)


def log_msg(msg: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    with _state_lock:
        state["log"].append(entry)
        if len(state["log"]) > 200:
            state["log"] = state["log"][-200:]
    broadcast("log", entry)


def push_state():
    with _state_lock:
        snap = {k: v for k, v in state.items() if k != "log"}
    broadcast("state", snap)


# ── Bot helpers ────────────────────────────────────────────────────────────────
def parse_duration(text: str) -> int:
    parts = text.strip().split(":")
    secs = 0
    for i, p in enumerate(reversed(parts)):
        secs += int(p) * (60 ** i)
    return secs


def build_driver():
    opts = uc.ChromeOptions()
    if config["headless"]:
        opts.add_argument("--headless=new")
    if config["mute"]:
        opts.add_argument("--mute-audio")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={random_user_agent()}")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    return uc.Chrome(options=opts, use_subprocess=True, version_main=123)


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
    """Sleep watch_seconds, polling for skip-ad button every second."""
    ad_sels = [
        ".ytp-skip-ad-button",
        ".ytp-ad-skip-button",
        "button.ytp-ad-skip-button-modern",
    ]
    elapsed = 0
    while elapsed < watch_seconds:
        if _stop_event.is_set():
            return
        # Honour pause
        while _pause_event.is_set() and not _stop_event.is_set():
            time.sleep(0.5)

        # Ad skip
        for sel in ad_sels:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    btn.click()
                    with _state_lock:
                        state["ads_skipped"] += 1
                    log_msg("⏭  Ad skipped", "success")
                    push_state()
                    time.sleep(0.8)
                    break
            except Exception:
                pass

        time.sleep(1)
        elapsed += 1
        with _state_lock:
            state["elapsed"] = elapsed
        if elapsed % 5 == 0:
            push_state()


def play_video(driver, url: str, attempt: int = 1) -> bool:
    log_msg(f"▶  Loading video (attempt {attempt}): {url[:60]}...")
    with _state_lock:
        state["current_url"] = url
        state["elapsed"] = 0
        state["duration"] = 0
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

    watch_time = int(duration * config["watch_ratio"]) + 2
    with _state_lock:
        state["duration"] = duration

    log_msg(f"⏱  Duration: {duration}s | Watching: {watch_time}s")
    push_state()

    watch_with_ad_skip(driver, watch_time)

    with _state_lock:
        state["videos_played"] += 1

    delay = random.uniform(config["delay_min"], config["delay_max"])
    log_msg(f"✓  Done. Pausing {delay:.1f}s...", "success")
    time.sleep(delay)
    return True


# ── Bot main thread ────────────────────────────────────────────────────────────
def bot_worker():
    global _driver
    if not SELENIUM_OK:
        log_msg("selenium / undetected-chromedriver not installed!", "error")
        with _state_lock:
            state["running"] = False
        push_state()
        return

    log_msg("🚀 Bot starting — launching Chrome...")
    try:
        _driver = build_driver()
    except Exception as e:
        log_msg(f"Chrome failed to start: {e}", "error")
        with _state_lock:
            state["running"] = False
        push_state()
        return

    loop_num = 0
    try:
        while not _stop_event.is_set():
            loop_num += 1
            loop_total = config["loops"]
            if loop_total != 0 and loop_num > loop_total:
                log_msg("All loops complete.")
                break

            with _state_lock:
                state["loop_current"] = loop_num
                state["loop_total"] = loop_total

            videos = config["videos"][:]
            if config["shuffle"]:
                random.shuffle(videos)

            with _state_lock:
                state["total_videos"] = len(videos)

            log_msg(f"── Loop {loop_num}{' of ' + str(loop_total) if loop_total else ' (∞)'} ──")
            push_state()

            for idx, url in enumerate(videos, 1):
                if _stop_event.is_set():
                    break
                with _state_lock:
                    state["current_index"] = idx
                push_state()
                log_msg(f"Video {idx}/{len(videos)}")

                success = False
                for attempt in range(1, config["max_retries"] + 2):
                    if _stop_event.is_set():
                        break
                    if play_video(_driver, url, attempt):
                        success = True
                        break
                    log_msg(f"Retrying in 5s...", "warn")
                    time.sleep(5)

                if not success:
                    log_msg(f"Skipped after {config['max_retries']+1} attempts", "error")

            # Gap between loops
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
            state["running"] = False
            state["current_url"] = ""
        log_msg("Bot stopped.")
        push_state()


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify(state)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    for key in ["loops", "shuffle", "headless", "mute", "watch_ratio", "delay_min", "delay_max", "max_retries"]:
        if key in data:
            config[key] = data[key]
    if "videos" in data and isinstance(data["videos"], list):
        config["videos"] = [v.strip() for v in data["videos"] if v.strip()]
    return jsonify({"ok": True, "config": config})


@app.route("/api/start", methods=["POST"])
def api_start():
    global _bot_thread
    if state["running"]:
        return jsonify({"ok": False, "msg": "Already running"})
    _stop_event.clear()
    _pause_event.clear()
    with _state_lock:
        state["running"] = True
        state["paused"] = False
        state["ads_skipped"] = 0
        state["videos_played"] = 0
        state["start_time"] = datetime.now().isoformat()
        state["log"] = []
    _bot_thread = threading.Thread(target=bot_worker, daemon=True)
    _bot_thread.start()
    push_state()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _stop_event.set()
    _pause_event.clear()
    with _state_lock:
        state["paused"] = False
    push_state()
    return jsonify({"ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    if not state["running"]:
        return jsonify({"ok": False, "msg": "Not running"})
    with _state_lock:
        if state["paused"]:
            _pause_event.clear()
            state["paused"] = False
            log_msg("▶  Resumed")
        else:
            _pause_event.set()
            state["paused"] = True
            log_msg("⏸  Paused")
    push_state()
    return jsonify({"ok": True, "paused": state["paused"]})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    """Skip current video by signalling stop + immediate restart logic via _stop_event trick."""
    # We push elapsed = duration to force the watcher to exit the loop
    with _state_lock:
        state["elapsed"] = state["duration"] + 999
    log_msg("⏭  Skipping current video...")
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=50)
    _sse_clients.append(q)

    def generate():
        # Send current state immediately on connect
        with _state_lock:
            snap = {k: v for k, v in state.items() if k != "log"}
        yield f"event: state\ndata: {json.dumps(snap)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    print("\n  YouTubeBot  →  http://localhost:5000\n")
    app.run(debug=False, threaded=True, port=5000)