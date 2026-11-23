from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading
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
    "prism-aurora",
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
    "theme": "prism-aurora",
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


_MACHINE_INFO_LOCK = threading.Lock()
_MACHINE_INFO_CACHE: tuple[str, int, int, dict[str, Any]] | None = None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def get_machine_info() -> dict[str, Any]:
    global _MACHINE_INFO_CACHE
    path_str = str(MACHINE_FILE)

    try:
        st = MACHINE_FILE.stat()
    except OSError:
        st = None

    cached = _MACHINE_INFO_CACHE
    if (
        st is not None
        and cached is not None
        and cached[0] == path_str
        and cached[1] == st.st_mtime_ns
        and cached[2] == st.st_size
    ):
        return dict(cached[3])

    with _MACHINE_INFO_LOCK:
        try:
            st = MACHINE_FILE.stat()
        except OSError:
            st = None

        cached = _MACHINE_INFO_CACHE
        if (
            st is not None
            and cached is not None
            and cached[0] == path_str
            and cached[1] == st.st_mtime_ns
            and cached[2] == st.st_size
        ):
            return dict(cached[3])

        if st is not None:
            try:
                data = json.loads(MACHINE_FILE.read_text(encoding="utf-8"))
                if data.get("machine_id") and data.get("machine_name"):
                    _MACHINE_INFO_CACHE = (path_str, st.st_mtime_ns, st.st_size, dict(data))
                    return dict(data)
            except Exception:
                pass

        ensure_dirs(local_only=True)
        hw = _detect_hardware_uuid()
        short_hash = hashlib.sha256(hw.encode("utf-8")).hexdigest()[:10]
        info = {
            "machine_id": f"mac-{short_hash}",
            "machine_name": _detect_computer_name(),
            "created_at": int(time.time() * 1000),
        }
        _write_json_atomic(MACHINE_FILE, info)
        try:
            new_st = MACHINE_FILE.stat()
            _MACHINE_INFO_CACHE = (path_str, new_st.st_mtime_ns, new_st.st_size, dict(info))
        except OSError:
            _MACHINE_INFO_CACHE = None
        return dict(info)


def update_machine_name(new_name: str) -> dict[str, Any]:
    global _MACHINE_INFO_CACHE
    info = get_machine_info()
    cleaned = new_name.strip()
    if cleaned:
        with _MACHINE_INFO_LOCK:
            info["machine_name"] = cleaned
            ensure_dirs(local_only=True)
            _write_json_atomic(MACHINE_FILE, info)
            try:
                st = MACHINE_FILE.stat()
                _MACHINE_INFO_CACHE = (
                    str(MACHINE_FILE),
                    st.st_mtime_ns,
                    st.st_size,
                    dict(info),
                )
            except OSError:
                _MACHINE_INFO_CACHE = None
    return dict(info)


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
