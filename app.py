"""
YouTubeBot — PythonAnywhere Server (Dashboard + API)
=====================================================
This file runs on PythonAnywhere.
It has NO Selenium dependency — it's purely a dashboard + state relay.

Your local bot (bot_local.py) connects to this server and does the actual
Chrome automation on your machine.

Deploy:
  1. Upload this file + templates/index.html to PythonAnywhere
  2. Set BOT_SECRET env var in PythonAnywhere's dashboard (Web tab → Env vars)
  3. Run bot_local.py on your local machine
"""

import json
import os
import queue
import threading
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

SECRET_KEY = os.environ.get("BOT_SECRET", "change-me-please")

# ── Shared state (written by local bot via /bot/report, read by dashboard) ────
state = {
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
    "last_seen": None,  # last heartbeat from local bot
}

config = {
    "videos": [
        "https://www.youtube.com/watch?v=7BIIoZi7GVM",
        "https://www.youtube.com/watch?v=6f5pMmJFYeE",
        "https://www.youtube.com/watch?v=L_TVnSNVhgo",
    ],
    "loops": 0,
    "shuffle": True,
    "mute": False,
    "watch_ratio": 1.0,
    "delay_min": 3,
    "delay_max": 10,
    "max_retries": 2,
}

# Commands queued for the local bot to pick up on next poll
_pending_commands: list = []  # "start" | "stop" | "pause" | "skip"
_sse_clients: list = []
_state_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────
def check_secret(req):
    return req.headers.get("X-Bot-Secret") == SECRET_KEY


def broadcast(event, data):
    msg = "event: {}\ndata: {}\n\n".format(event, json.dumps(data))
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)


def push_state():
    with _state_lock:
        snap = {k: v for k, v in state.items() if k != "log"}
    broadcast("state", snap)


# ── Dashboard ─────────────────────────────────────────────────────────────────
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
    for key in ["loops", "shuffle", "mute", "watch_ratio", "delay_min", "delay_max", "max_retries"]:
        if key in data:
            config[key] = data[key]
    if "videos" in data and isinstance(data["videos"], list):
        config["videos"] = [v.strip() for v in data["videos"] if v.strip()]
    return jsonify({"ok": True, "config": config})


# ── Control endpoints (dashboard buttons → server → local bot via polling) ────
@app.route("/api/start", methods=["POST"])
def api_start():
    with _state_lock:
        if state["running"]:
            return jsonify({"ok": False, "msg": "Already running"})
        _pending_commands.append("start")
    return jsonify({"ok": True, "msg": "Start command queued for local bot"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _pending_commands.append("stop")
    return jsonify({"ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    _pending_commands.append("pause")
    return jsonify({"ok": True})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    _pending_commands.append("skip")
    return jsonify({"ok": True})


# ── Bot relay endpoints (called by bot_local.py on your machine) ──────────────
@app.route("/bot/poll", methods=["GET"])
def bot_poll():
    """Local bot polls this every 2s to get pending commands + latest config."""
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 403
    with _state_lock:
        cmds = _pending_commands[:]
        _pending_commands.clear()
        state["last_seen"] = datetime.now().isoformat()
    return jsonify({"commands": cmds, "config": config})


@app.route("/bot/report", methods=["POST"])
def bot_report():
    """Local bot pushes state updates and log entries here."""
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    with _state_lock:
        for k, v in data.items():
            if k in state:
                state[k] = v
        state["last_seen"] = datetime.now().isoformat()
    push_state()
    if "log_entry" in data:
        broadcast("log", data["log_entry"])
    return jsonify({"ok": True})


# ── SSE stream for dashboard ───────────────────────────────────────────────────
@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=50)
    _sse_clients.append(q)

    def generate():
        with _state_lock:
            snap = {k: v for k, v in state.items() if k != "log"}
        yield "event: state\ndata: {}\n\n".format(json.dumps(snap))
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

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=False, threaded=True, port=5000)