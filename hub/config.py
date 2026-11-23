from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("vibe_session_hub.config")

FALLBACK_PORT = 17380


def parse_port(raw: Any, default: int = FALLBACK_PORT) -> int:
    """Parse and validate a TCP port (1..65535), falling back cleanly on invalid values."""
    if raw is None or isinstance(raw, bool):
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError:
        logger.warning("Invalid port value %r; falling back to %d", raw, default)
        return default
    if 1 <= port <= 65535:
        return port
    logger.warning("Out-of-range port %d; falling back to %d", port, default)
    return default


def resolve_configured_port(
    cli_port: int | None = None,
    preferences_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve effective listen port: CLI --port > VIBE_HUB_PORT env > preferences.json > 17380."""
    if cli_port is not None and not isinstance(cli_port, bool):
        parsed_cli = parse_port(cli_port, default=0)
        if parsed_cli:
            return parsed_cli

    environ = os.environ if env is None else env
    env_raw = environ.get("VIBE_HUB_PORT")
    if env_raw is not None and str(env_raw).strip():
        parsed_env = parse_port(env_raw, default=0)
        if parsed_env:
            return parsed_env

    target_file = preferences_file or PREFERENCES_FILE
    if target_file.exists():
        try:
            raw = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("port") is not None:
                parsed_pref = parse_port(raw.get("port"), default=0)
                if parsed_pref:
                    return parsed_pref
        except Exception as exc:
            logger.debug(
                "Ignoring unreadable port setting in %s: %s", target_file, exc
            )

    return FALLBACK_PORT


DEFAULT_PORT = parse_port(os.environ.get("VIBE_HUB_PORT"), FALLBACK_PORT)
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


@dataclass(frozen=True)
class HubPaths:
    """Immutable container for all runtime filesystem paths (supports clean DI in tests)."""

    state_dir: Path
    local_sync_root: Path
    nodes_dir: Path
    manual_links_dir: Path
    icloud_root: Path
    icloud_sync_root: Path
    index_db_path: Path
    machine_file: Path
    preferences_file: Path

    @classmethod
    def from_state_dir(
        cls,
        state_dir: Path,
        icloud_root: Path | None = None,
        icloud_sync_root: Path | None = None,
    ) -> HubPaths:
        state_dir = Path(state_dir)
        sync_root = state_dir / "sync"
        ic_root = Path(icloud_root) if icloud_root is not None else ICLOUD_ROOT
        ic_sync = (
            Path(icloud_sync_root)
            if icloud_sync_root is not None
            else (ic_root / "VibeSessionHub")
        )
        return cls(
            state_dir=state_dir,
            local_sync_root=sync_root,
            nodes_dir=sync_root / "nodes",
            manual_links_dir=sync_root / "manual_links",
            icloud_root=ic_root,
            icloud_sync_root=ic_sync,
            index_db_path=state_dir / "index.db",
            machine_file=state_dir / "machine.json",
            preferences_file=state_dir / "preferences.json",
        )

    def is_icloud_enabled(self, local_only_flag: bool = False) -> bool:
        if local_only_flag:
            return False
        if os.environ.get("VIBE_HUB_LOCAL_ONLY", "").strip() in ("1", "true", "yes"):
            return False
        return self.icloud_root.exists()

    def ensure_dirs(self, local_only: bool = False) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.manual_links_dir.mkdir(parents=True, exist_ok=True)
        if self.is_icloud_enabled(local_only):
            try:
                (self.icloud_sync_root / "nodes").mkdir(parents=True, exist_ok=True)
                (self.icloud_sync_root / "manual_links").mkdir(
                    parents=True, exist_ok=True
                )
            except OSError as exc:
                logger.debug("iCloud directory creation skipped: %s", exc)


def get_default_paths() -> HubPaths:
    """Resolve current paths from module globals so env overrides and test patches apply."""
    return HubPaths(
        state_dir=LOCAL_STATE_DIR,
        local_sync_root=LOCAL_SYNC_ROOT,
        nodes_dir=NODES_DIR,
        manual_links_dir=MANUAL_LINKS_DIR,
        icloud_root=ICLOUD_ROOT,
        icloud_sync_root=ICLOUD_SYNC_ROOT,
        index_db_path=INDEX_DB_PATH,
        machine_file=MACHINE_FILE,
        preferences_file=PREFERENCES_FILE,
    )


def configure_logging(verbose: bool = False) -> None:
    """Configure stdlib stderr logging once for the vibe_session_hub package."""
    pkg_logger = logging.getLogger("vibe_session_hub")
    if pkg_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    pkg_logger.addHandler(handler)
    env_debug = os.environ.get("VIBE_HUB_DEBUG", "").strip() in ("1", "true", "yes")
    pkg_logger.setLevel(logging.DEBUG if (verbose or env_debug) else logging.WARNING)
    pkg_logger.propagate = False


def is_icloud_enabled(local_only_flag: bool = False) -> bool:
    return get_default_paths().is_icloud_enabled(local_only_flag)


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


def ensure_dirs(local_only: bool = False, paths: HubPaths | None = None) -> None:
    (paths or get_default_paths()).ensure_dirs(local_only=local_only)


_MACHINE_INFO_LOCK = threading.Lock()
_MACHINE_INFO_CACHE: tuple[str, int, int, dict[str, Any]] | None = None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def get_machine_info(machine_file: Path | None = None) -> dict[str, Any]:
    global _MACHINE_INFO_CACHE
    target_file = machine_file or MACHINE_FILE
    path_str = str(target_file)

    try:
        st = target_file.stat()
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
            st = target_file.stat()
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
                data = json.loads(target_file.read_text(encoding="utf-8"))
                if data.get("machine_id") and data.get("machine_name"):
                    _MACHINE_INFO_CACHE = (
                        path_str,
                        st.st_mtime_ns,
                        st.st_size,
                        dict(data),
                    )
                    return dict(data)
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", target_file, exc)

        if machine_file is None:
            ensure_dirs(local_only=True)
        else:
            target_file.parent.mkdir(parents=True, exist_ok=True)
        hw = _detect_hardware_uuid()
        short_hash = hashlib.sha256(hw.encode("utf-8")).hexdigest()[:10]
        info = {
            "machine_id": f"mac-{short_hash}",
            "machine_name": _detect_computer_name(),
            "created_at": int(time.time() * 1000),
        }
        _write_json_atomic(target_file, info)
        try:
            new_st = target_file.stat()
            _MACHINE_INFO_CACHE = (
                path_str,
                new_st.st_mtime_ns,
                new_st.st_size,
                dict(info),
            )
        except OSError:
            _MACHINE_INFO_CACHE = None
        return dict(info)


def update_machine_name(
    new_name: str, machine_file: Path | None = None
) -> dict[str, Any]:
    global _MACHINE_INFO_CACHE
    target_file = machine_file or MACHINE_FILE
    info = get_machine_info(machine_file=target_file)
    cleaned = new_name.strip()
    if cleaned:
        with _MACHINE_INFO_LOCK:
            info["machine_name"] = cleaned
            if machine_file is None:
                ensure_dirs(local_only=True)
            else:
                target_file.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(target_file, info)
            try:
                st = target_file.stat()
                _MACHINE_INFO_CACHE = (
                    str(target_file),
                    st.st_mtime_ns,
                    st.st_size,
                    dict(info),
                )
            except OSError:
                _MACHINE_INFO_CACHE = None
    return dict(info)


def get_preferences(preferences_file: Path | None = None) -> dict[str, Any]:
    target_file = preferences_file or PREFERENCES_FILE
    if preferences_file is None:
        ensure_dirs(local_only=True)
    prefs: dict[str, Any] = dict(DEFAULT_PREFERENCES)
    if target_file.exists():
        try:
            raw = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("theme") in VALID_THEMES:
                    prefs["theme"] = str(raw["theme"])
                if raw.get("port") is not None:
                    parsed_port = parse_port(raw.get("port"), default=0)
                    if parsed_port:
                        prefs["port"] = parsed_port
        except Exception as exc:
            logger.debug("Ignoring unreadable preferences file %s: %s", target_file, exc)
    return prefs


def update_preferences(
    theme: str | None = None,
    port: int | None = None,
    preferences_file: Path | None = None,
) -> dict[str, Any]:
    target_file = preferences_file or PREFERENCES_FILE
    prefs = get_preferences(preferences_file=target_file)
    if theme and theme in VALID_THEMES:
        prefs["theme"] = theme
    if port is not None:
        parsed_port = parse_port(port, default=0)
        if parsed_port:
            prefs["port"] = parsed_port
    _write_json_atomic(target_file, prefs)
    return prefs
