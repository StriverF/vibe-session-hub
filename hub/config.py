from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_PORT = int(os.environ.get("VIBE_HUB_PORT", "17380"))
DEFAULT_HOST = "127.0.0.1"

LOCAL_STATE_DIR = Path(
    os.environ.get("VIBE_HUB_STATE_DIR", os.path.expanduser("~/.vibe-session-hub"))
)
LOCAL_SYNC_ROOT = LOCAL_STATE_DIR / "sync"
NODES_DIR = LOCAL_SYNC_ROOT / "nodes"
MANUAL_LINKS_DIR = LOCAL_SYNC_ROOT / "manual_links"

ICLOUD_ROOT = Path(
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
)
ICLOUD_SYNC_ROOT = Path(
    os.environ.get("VIBE_HUB_SYNC_DIR", str(ICLOUD_ROOT / "VibeSessionHub"))
)

INDEX_DB_PATH = LOCAL_STATE_DIR / "index.db"
MACHINE_FILE = LOCAL_STATE_DIR / "machine.json"
PREFERENCES_FILE = LOCAL_STATE_DIR / "preferences.json"

VALID_THEMES = {
    "obsidian-amber",
    "graphite-mono",
    "forest-sage",
    "espresso-copper",
    "plum-amethyst",
    "crimson-rose",
    "warm-paper",
    "mist-sage",
}
DEFAULT_PREFERENCES = {
    "theme": "obsidian-amber",
}


def is_icloud_enabled(local_only_flag: bool = False) -> bool:
    if local_only_flag:
        return False
    if os.environ.get("VIBE_HUB_LOCAL_ONLY", "").strip() in ("1", "true", "yes"):
        return False
    return ICLOUD_ROOT.exists()


def _detect_hardware_uuid() -> str:
    try:
        out = subprocess.check_output(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                parts = line.split('"')
                if len(parts) >= 4:
                    return parts[3].strip()
    except Exception:
        pass
    return socket.gethostname()


def _detect_computer_name() -> str:
    for key in ("ComputerName", "LocalHostName"):
        try:
            name = subprocess.check_output(
                ["/usr/sbin/scutil", "--get", key],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
            if name:
                return name
        except Exception:
            continue
    return socket.gethostname().removesuffix(".local")


def ensure_dirs(local_only: bool = False) -> None:
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    if is_icloud_enabled(local_only):
        try:
            (ICLOUD_SYNC_ROOT / "nodes").mkdir(parents=True, exist_ok=True)
            (ICLOUD_SYNC_ROOT / "manual_links").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def get_machine_info() -> dict[str, Any]:
    ensure_dirs(local_only=True)
    if MACHINE_FILE.exists():
        try:
            data = json.loads(MACHINE_FILE.read_text(encoding="utf-8"))
            if data.get("machine_id") and data.get("machine_name"):
                return data
        except Exception:
            pass

    hw = _detect_hardware_uuid()
    short_hash = hashlib.sha256(hw.encode("utf-8")).hexdigest()[:10]
    info = {
        "machine_id": f"mac-{short_hash}",
        "machine_name": _detect_computer_name(),
        "created_at": int(time.time() * 1000),
    }
    MACHINE_FILE.write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return info


def update_machine_name(new_name: str) -> dict[str, Any]:
    info = get_machine_info()
    cleaned = new_name.strip()
    if cleaned:
        info["machine_name"] = cleaned
        MACHINE_FILE.write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return info


def get_preferences() -> dict[str, str]:
    ensure_dirs(local_only=True)
    prefs = dict(DEFAULT_PREFERENCES)
    if PREFERENCES_FILE.exists():
        try:
            raw = json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("theme") in VALID_THEMES:
                prefs["theme"] = str(raw["theme"])
        except Exception:
            pass
    return prefs


def update_preferences(theme: str | None = None) -> dict[str, Any]:
    prefs = get_preferences()
    if theme and theme in VALID_THEMES:
        prefs["theme"] = theme
    PREFERENCES_FILE.write_text(
        json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return prefs
