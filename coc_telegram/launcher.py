"""Start a private API worker and Telegram bot in one Home Assistant app."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

BASE = Path("/opt")
SHUTDOWN = False


def stop(_signal, _frame):
    global SHUTDOWN
    SHUTDOWN = True


def build_env(options_path: Path) -> dict[str, str]:
    options = json.loads(options_path.read_text(encoding="utf-8"))
    for required in ("telegram_bot_token", "coc_token", "coc_clan_tag"):
        if not str(options.get(required, "")).strip():
            raise ValueError(f"Required Home Assistant setting is empty: {required}")
    env = os.environ.copy()
    fields = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "coc_token": "COC_TOKEN",
        "coc_clan_tag": "COC_CLAN_TAG",
        "clan_group_id": "CLAN_GROUP_ID",
        "invite_ttl_minutes": "INVITE_TTL_MINUTES",
        "enforce_clan_membership": "ENFORCE_CLAN_MEMBERSHIP",
        "require_player_verification": "REQUIRE_PLAYER_VERIFICATION",
        "war_results_enabled": "WAR_RESULTS_ENABLED",
        "war_reminder_enabled": "WAR_REMINDER_ENABLED",
        "war_reminder_window_hours": "WAR_REMINDER_WINDOW_HOURS",
        "war_reminder_interval_minutes": "WAR_REMINDER_INTERVAL_MINUTES",
        "cache_ttl_seconds": "CACHE_TTL_SECONDS",
        "request_timeout_seconds": "REQUEST_TIMEOUT_SECONDS",
        "debug": "DEBUG",
        "widgets_enabled": "WIDGETS_ENABLED",
    }
    for name, key in fields.items():
        value = options.get(name)
        if value is None or str(value).strip() == "":
            env.pop(key, None)
        else:
            env[key] = str(value).lower() if isinstance(value, bool) else str(value)
    env["BACKEND_URL"] = "http://127.0.0.1:8000"
    env["BINDINGS_DB_PATH"] = "/data/bindings.db"
    env["WAR_HISTORY_DB_PATH"] = "/data/war_history.db"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def is_backend_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
            return True
    except OSError:
        return False


def run() -> int:
    try:
        env = build_env(Path(os.environ.get("HA_OPTIONS_PATH", "/data/options.json")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Home Assistant add-on configuration error: {exc}", flush=True)
        return 2
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    try:
        print("Starting CoC API on private loopback (no published ports)", flush=True)
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(BASE / "backend"), env=env,
        )
        children.append(api)
        for _ in range(60):
            if SHUTDOWN or api.poll() is not None:
                print("CoC backend exited before ready", flush=True)
                return 1
            if is_backend_ready():
                break
            time.sleep(0.25)
        else:
            print("CoC backend startup timed out", flush=True)
            return 1
        print("Starting Telegram polling worker", flush=True)
        bot = subprocess.Popen([sys.executable, "-m", "app.bot"], cwd=str(BASE / "bot"), env=env)
        children.append(bot)
        while not SHUTDOWN:
            if any(child.poll() is not None for child in children):
                print("Worker exited; stopping both processes for Supervisor restart", flush=True)
                return 1
            time.sleep(1)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    sys.exit(run())
