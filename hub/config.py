from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import re
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

VALID_MACHINE_TYPES = frozenset(
    {
        "macbook-pro",
        "macbook-air",
        "macbook",
        "mac-studio",
        "mac-mini",
        "imac",
        "mac-pro",
        "mac",
        "shared",
    }
)

MACHINE_TYPE_LABELS: dict[str, str] = {
    "macbook-pro": "MacBook Pro",
    "macbook-air": "MacBook Air",
    "macbook": "MacBook",
    "mac-studio": "Mac Studio",
    "mac-mini": "Mac mini",
    "imac": "iMac",
    "mac-pro": "Mac Pro",
    "mac": "Mac",
    "shared": "跨设备共享",
}

# Verified Apple Silicon hw.model identifiers (M1 Ultra / M2 / M3 / M4 families).
# Primary detection reads live `product-name` from IORegistry so newer models work automatically.
_APPLE_SILICON_MODEL_TYPES: dict[str, str] = {
    # Mac Studio
    "mac13,1": "mac-studio",
    "mac13,2": "mac-studio",
    "mac14,13": "mac-studio",
    "mac14,14": "mac-studio",
    "mac15,14": "mac-studio",
    "mac16,9": "mac-studio",
    # Mac mini
    "mac14,3": "mac-mini",
    "mac14,12": "mac-mini",
    "mac16,10": "mac-mini",
    "mac16,11": "mac-mini",
    # MacBook Pro
    "mac14,5": "macbook-pro",
    "mac14,6": "macbook-pro",
    "mac14,7": "macbook-pro",
    "mac14,9": "macbook-pro",
    "mac14,10": "macbook-pro",
    "mac15,3": "macbook-pro",
    "mac15,6": "macbook-pro",
    "mac15,7": "macbook-pro",
    "mac15,8": "macbook-pro",
    "mac15,9": "macbook-pro",
    "mac15,10": "macbook-pro",
    "mac15,11": "macbook-pro",
    "mac16,1": "macbook-pro",
    "mac16,5": "macbook-pro",
    "mac16,6": "macbook-pro",
    "mac16,7": "macbook-pro",
    "mac16,8": "macbook-pro",
    # MacBook Air
    "mac14,2": "macbook-air",
    "mac14,15": "macbook-air",
    "mac15,12": "macbook-air",
    "mac15,13": "macbook-air",
    "mac16,12": "macbook-air",
    "mac16,13": "macbook-air",
    # iMac
    "mac15,4": "imac",
    "mac15,5": "imac",
    "mac16,2": "imac",
    "mac16,3": "imac",
    # Mac Pro
    "mac14,8": "mac-pro",
}

_MACHINE_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![a-z])mac\s*[-_]?\s*studio(?![a-z])", re.IGNORECASE), "mac-studio"),
    (re.compile(r"(?<![a-z])mac\s*[-_]?\s*mini(?![a-z])", re.IGNORECASE), "mac-mini"),
    (re.compile(r"(?<![a-z])(?:mac\s*[-_]?\s*book\s*[-_]?\s*pro|mbp)(?![a-z])", re.IGNORECASE), "macbook-pro"),
    (re.compile(r"(?<![a-z])(?:mac\s*[-_]?\s*book\s*[-_]?\s*air|mba)(?![a-z])", re.IGNORECASE), "macbook-air"),
    (re.compile(r"(?<![a-z])imac(?![a-z])", re.IGNORECASE), "imac"),
    (re.compile(r"(?<![a-z])mac\s*[-_]?\s*pro(?![a-z])", re.IGNORECASE), "mac-pro"),
    (re.compile(r"(?<![a-z])mac\s*[-_]?\s*book(?![a-z])", re.IGNORECASE), "macbook"),
)


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


def _match_machine_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    for pattern, mtype in _MACHINE_NAME_PATTERNS:
        if pattern.search(cleaned):
            return mtype
    return ""


def classify_machine_type(
    machine_model: str = "",
    product_name: str = "",
    machine_name: str = "",
    machine_id: str = "",
) -> str:
    """Classify a Mac into a fine-grained hardware type from product name, hw.model, or display name."""
    if str(machine_id or "").strip().lower() == "shared":
        return "shared"

    # 1. Explicit Apple marketing/product name (e.g. "Mac Studio (2023)", "MacBook Pro (14-inch, Nov 2024)")
    matched = _match_machine_text(product_name)
    if matched:
        return matched

    # 2. Raw hw.model identifier (e.g. "Mac14,13", "MacBookPro18,1", "Macmini9,1", "iMac21,1")
    raw_model = str(machine_model or "").strip()
    if raw_model:
        low_model = raw_model.lower()
        if low_model in _APPLE_SILICON_MODEL_TYPES:
            return _APPLE_SILICON_MODEL_TYPES[low_model]
        matched = _match_machine_text(raw_model)
        if matched:
            return matched

    # 3. Fallback heuristic on human display name (for legacy remote shards without machine_type)
    matched = _match_machine_text(machine_name)
    if matched:
        return matched

    return "mac"


def normalize_machine_type(
    machine_type: str = "",
    *,
    machine_model: str = "",
    machine_name: str = "",
    machine_id: str = "",
) -> str:
    """Return a valid canonical machine_type, upgrading empty/generic values via model/name fallback."""
    cleaned = str(machine_type or "").strip().lower()
    if cleaned in VALID_MACHINE_TYPES and cleaned != "mac":
        return cleaned
    inferred = classify_machine_type(
        machine_model=machine_model,
        machine_name=machine_name,
        machine_id=machine_id,
    )
    if inferred != "mac":
        return inferred
    if cleaned in VALID_MACHINE_TYPES:
        return cleaned
    return "mac"


def _detect_sysctl_hw_model() -> str:
    try:
        return subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "hw.model"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def _detect_ioreg_product_name() -> str:
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-ar", "-d1", "-n", "product"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if not raw.strip():
            return ""
        items = plistlib.loads(raw)
        if not isinstance(items, list):
            return ""
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("product-name", "product-description"):
                val = item.get(key)
                if isinstance(val, bytes):
                    val = val.rstrip(b"\x00").decode("utf-8", errors="ignore")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return ""


def _detect_system_profiler_hardware() -> tuple[str, str]:
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4,
        )
        parsed = json.loads(raw)
        rows = parsed.get("SPHardwareDataType")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            row = rows[0]
            return (
                str(row.get("machine_model") or "").strip(),
                str(row.get("machine_name") or "").strip(),
            )
    except Exception:
        pass
    return "", ""


def _detect_hardware_model() -> tuple[str, str]:
    """Detect local Mac hardware model string and canonical machine_type."""
    hw_model = _detect_sysctl_hw_model()
    product_name = _detect_ioreg_product_name()
    mtype = classify_machine_type(machine_model=hw_model, product_name=product_name)
    if mtype == "mac":
        sp_model, sp_name = _detect_system_profiler_hardware()
        if not hw_model and sp_model:
            hw_model = sp_model
        if not product_name and sp_name:
            product_name = sp_name
        mtype = classify_machine_type(
            machine_model=hw_model, product_name=product_name
        )
    display_model = product_name or hw_model
    return display_model, mtype


_HOST_HARDWARE_LOCK = threading.Lock()
_HOST_HARDWARE_CACHE: tuple[str, str] | None = None


def _get_cached_hardware_model() -> tuple[str, str]:
    global _HOST_HARDWARE_CACHE
    cached = _HOST_HARDWARE_CACHE
    if cached is not None:
        return cached
    with _HOST_HARDWARE_LOCK:
        cached = _HOST_HARDWARE_CACHE
        if cached is not None:
            return cached
        detected = _detect_hardware_model()
        _HOST_HARDWARE_CACHE = detected
        return detected


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
                    cur_type = str(data.get("machine_type") or "").strip().lower()
                    cur_model = str(data.get("machine_model") or "").strip()
                    needs_persist = False
                    if cur_type not in VALID_MACHINE_TYPES:
                        hw_model, hw_type = _get_cached_hardware_model()
                        data["machine_type"] = normalize_machine_type(
                            hw_type,
                            machine_model=cur_model or hw_model,
                            machine_name=str(data["machine_name"]),
                            machine_id=str(data["machine_id"]),
                        )
                        if not cur_model and hw_model:
                            data["machine_model"] = hw_model
                        else:
                            data.setdefault("machine_model", cur_model)
                        needs_persist = True
                    else:
                        data["machine_type"] = cur_type
                        data.setdefault("machine_model", cur_model)

                    cache_st = st
                    if needs_persist:
                        try:
                            _write_json_atomic(target_file, data)
                            cache_st = target_file.stat()
                        except OSError as exc:
                            logger.debug(
                                "Skipped persisting detected machine_type to %s: %s",
                                target_file,
                                exc,
                            )

                    _MACHINE_INFO_CACHE = (
                        path_str,
                        cache_st.st_mtime_ns,
                        cache_st.st_size,
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
        computer_name = _detect_computer_name()
        hw_model, hw_type = _get_cached_hardware_model()
        info = {
            "machine_id": f"mac-{short_hash}",
            "machine_name": computer_name,
            "machine_type": normalize_machine_type(
                hw_type, machine_model=hw_model, machine_name=computer_name
            ),
            "machine_model": hw_model,
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
