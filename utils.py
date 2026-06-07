"""
Soomario Swing Executor — Utilities
═════════════════════
Logging setup, Telegram notifier, JSON state helpers.
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from config import (
    LOG_FILE, LOG_LEVEL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

# ─── Logging ────────────────────────────────────────────────────

def setup_logging():
    """Configure logging to console + rotating file. Safe to call multiple times."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Clear any basicConfig handlers to avoid duplicates
    if root.handlers:
        root.handlers.clear()

    fmt = logging.Formatter("%(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
        root.addHandler(fh)
    except Exception as e:
        print(f"⚠️  Could not open log file {LOG_FILE}: {e}")

    # Quiet noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ─── Time helpers ───────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso() -> str:
    return now_utc().isoformat()

def utc_date_str() -> str:
    """Today's UTC date as YYYY-MM-DD."""
    return now_utc().strftime("%Y-%m-%d")


# ─── JSON state helpers ─────────────────────────────────────────

def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"⚠️  Bad JSON at {path}: {e} — returning default")
        return default if default is not None else {}

def save_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)  # atomic

def append_jsonl(path: Path, entry: dict):
    """Append one JSON entry per line. Used for signal_log, trade_log, equity_log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {**entry, "ts": entry.get("ts") or iso()}
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

def tail_jsonl(path: Path, n: int = 50) -> list:
    """Return last n entries from a JSONL file."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
        return [json.loads(ln) for ln in lines if ln.strip()]
    except Exception:
        return []


# ─── Telegram notifier (silently no-ops if unset) ───────────────

def tg_notify(msg: str, level: str = "info") -> Optional[bool]:
    """
    Send a Telegram message. Returns True on success, False on fail, None if unconfigured.
    Never raises. Never blocks the main flow.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    icons = {"info": "ℹ️", "warn": "⚠️", "error": "🚨", "trade": "💱", "trail": "📉", "defense": "🛡️"}
    prefix = icons.get(level, "ℹ️")
    text = f"{prefix} *Swing*\n{msg}"
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        logging.debug(f"Telegram send failed: {e}")
        return False


# ─── Math helpers ───────────────────────────────────────────────

def safe_pct(numer: float, denom: float) -> float:
    if denom == 0:
        return 0.0
    return numer / denom

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
