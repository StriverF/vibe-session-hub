from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__

logger = logging.getLogger("vibe_session_hub.updater")

DEFAULT_GITHUB_REPO = "StriverF/vibe-session-hub"
DEFAULT_UPDATE_TIMEOUT_SEC = 12.0
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHANGELOG_HEADING_RE = re.compile(
    r"^##\s+\[?v?(\d+\.\d+\.\d+[^\]\s]*)\]?(?:\s*-\s*(\S+))?"
)
_INSTALL_SECTION_RE = re.compile(
    r"^###\s+(?:下载与安装|校验和|Download|Install|Checksum)", re.MULTILINE | re.IGNORECASE
)
_TOP_RELEASE_HEADING_RE = re.compile(
    r"^##\s+Vibe Session Hub\s+v?\d+\.\d+\.\d+[^\n]*\n+", re.IGNORECASE
)

TRUSTED_GITHUB_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def _parse_semver(raw: str) -> tuple[tuple[int, ...], tuple[str, ...]] | None:
    text = str(raw or "").strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    if "+" in text:
        text = text.split("+", 1)[0]
    if not text:
        return None
    if "-" in text:
        core_part, pre_part = text.split("-", 1)
    else:
        core_part, pre_part = text, ""
    raw_nums = core_part.split(".")
    if not raw_nums:
        return None
    nums: list[int] = []
    for part in raw_nums:
        if not part.isdigit():
            return None
        nums.append(int(part))
    pre = tuple(p for p in pre_part.split(".") if p) if pre_part else ()
    return tuple(nums), pre


def compare_semver(lhs: str, rhs: str) -> int:
    """Compare two semantic version strings; returns -1, 0, or 1."""
    a = _parse_semver(lhs)
    b = _parse_semver(rhs)
    if a is None or b is None:
        al = str(lhs or "").strip()
        bl = str(rhs or "").strip()
        if al == bl:
            return 0
        return -1 if al < bl else 1

    a_nums, a_pre = a
    b_nums, b_pre = b
    max_len = max(len(a_nums), len(b_nums))
    for i in range(max_len):
        av = a_nums[i] if i < len(a_nums) else 0
        bv = b_nums[i] if i < len(b_nums) else 0
        if av != bv:
            return -1 if av < bv else 1

    if not a_pre and not b_pre:
        return 0
    if not a_pre:
        return 1
    if not b_pre:
        return -1

    for ap, bp in zip(a_pre, b_pre):
        if ap == bp:
            continue
        a_is_num = ap.isdigit()
        b_is_num = bp.isdigit()
        if a_is_num and b_is_num:
            return -1 if int(ap) < int(bp) else 1
        if a_is_num:
            return -1
        if b_is_num:
            return 1
        return -1 if ap < bp else 1

    if len(a_pre) == len(b_pre):
        return 0
    return -1 if len(a_pre) < len(b_pre) else 1


def is_trusted_update_url(raw_url: str, *, allow_loopback: bool = False) -> bool:
    """Validate that an update feed or asset download URL is from a trusted host."""
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if allow_loopback and scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return True
    if scheme != "https":
        return False
    if host in TRUSTED_GITHUB_HOSTS:
        return True
    if host.endswith(".githubusercontent.com"):
        return True
    return False


def resolve_update_feed_url() -> tuple[str, bool]:
    """Return (feed_url, allow_loopback) from environment or default GitHub repo."""
    custom_feed = os.environ.get("VIBE_HUB_UPDATE_FEED_URL", "").strip()
    if custom_feed:
        parsed = urlparse(custom_feed)
        host = (parsed.hostname or "").lower()
        allow_loopback = parsed.scheme.lower() == "http" and host in {
            "127.0.0.1",
            "localhost",
        }
        if is_trusted_update_url(custom_feed, allow_loopback=allow_loopback):
            return custom_feed, allow_loopback
        logger.warning("Ignoring untrusted VIBE_HUB_UPDATE_FEED_URL: %r", custom_feed)

    repo = os.environ.get("VIBE_HUB_UPDATE_REPO", DEFAULT_GITHUB_REPO).strip()
    if not _REPO_SLUG_RE.match(repo):
        repo = DEFAULT_GITHUB_REPO
    return f"https://api.github.com/repos/{repo}/releases/latest", False


def clean_release_notes(raw_notes: str) -> str:
    """Trim auto-appended DMG install/checksum boilerplate from GitHub Release body."""
    text = str(raw_notes or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    text = _TOP_RELEASE_HEADING_RE.sub("", text, count=1).strip()
    match = _INSTALL_SECTION_RE.search(text)
    if match:
        trimmed = text[: match.start()].strip()
        if trimmed:
            return trimmed
    return text


def parse_changelog_text(text: str) -> list[dict[str, Any]]:
    """Parse released version entries from CHANGELOG.md (newest first)."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            match = _CHANGELOG_HEADING_RE.match(line)
            if match:
                current = {
                    "version": match.group(1),
                    "date": match.group(2) or "",
                    "items": [],
                }
                entries.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        if line.startswith("- "):
            item = line[2:].strip()
            if item:
                current["items"].append(item)
    return entries


def parse_sha256_digest(raw: str) -> str:
    """Normalize either 'sha256:<hex>' or bare '<hex>' into lowercase 64-hex or ''."""
    text = str(raw or "").strip()
    if text.lower().startswith("sha256:"):
        text = text[7:].strip()
    if _SHA256_HEX_RE.match(text):
        return text.lower()
    return ""


def parse_github_release(
    payload: dict[str, Any],
    current_version: str,
    *,
    allow_loopback: bool = False,
) -> dict[str, Any] | None:
    """Parse a GitHub Release API object and return an update dict if newer."""
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag[1:] if tag.startswith(("v", "V")) else tag
    if not version or compare_semver(version, current_version) <= 0:
        return None

    assets = payload.get("assets")
    dmg_asset: dict[str, Any] | None = None
    fallback_dmg: dict[str, Any] | None = None
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if not name.lower().endswith(".dmg"):
                continue
            if not is_trusted_update_url(dl_url, allow_loopback=allow_loopback):
                continue
            if "universal" in name.lower():
                dmg_asset = asset
                break
            if fallback_dmg is None:
                fallback_dmg = asset
        if dmg_asset is None:
            dmg_asset = fallback_dmg

    dmg_name = str((dmg_asset or {}).get("name") or "")
    dmg_url = str((dmg_asset or {}).get("browser_download_url") or "")
    dmg_size = 0
    if dmg_asset is not None:
        try:
            dmg_size = max(0, int(dmg_asset.get("size") or 0))
        except (TypeError, ValueError):
            dmg_size = 0

    sha256_url = ""
    if dmg_name and isinstance(assets, list):
        expected_sidecar = f"{dmg_name}.sha256"
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if str(asset.get("name") or "").strip() == expected_sidecar:
                cand_url = str(asset.get("browser_download_url") or "").strip()
                if is_trusted_update_url(cand_url, allow_loopback=allow_loopback):
                    sha256_url = cand_url
                break

    html_url = str(payload.get("html_url") or "").strip()
    if html_url and not is_trusted_update_url(html_url, allow_loopback=allow_loopback):
        html_url = ""

    return {
        "version": version,
        "tag": tag,
        "name": str(payload.get("name") or f"Vibe Session Hub {tag}"),
        "published_at": str(payload.get("published_at") or ""),
        "notes": clean_release_notes(str(payload.get("body") or "")),
        "html_url": html_url,
        "dmg_name": dmg_name,
        "dmg_url": dmg_url,
        "dmg_size": dmg_size,
        "sha256_url": sha256_url,
        "sha256": parse_sha256_digest(str((dmg_asset or {}).get("digest") or "")),
    }


@dataclass
class _CheckerState:
    status: str = "idle"
    checking: bool = False
    latest_confirmed: bool = False
    check_error: str = ""
    checked_at: int = 0
    update: dict[str, Any] | None = None


class UpdateChecker:
    """Lightweight read-only GitHub Release checker for browser mode and changelog API."""

    def __init__(
        self,
        *,
        current_version: str = __version__,
        backend_root: Path | None = None,
        feed_url: str | None = None,
        allow_loopback: bool | None = None,
        timeout_sec: float = DEFAULT_UPDATE_TIMEOUT_SEC,
    ) -> None:
        self.current_version = current_version.lstrip("vV").strip() or "1.0.0"
        self.backend_root = (
            Path(backend_root).resolve()
            if backend_root is not None
            else Path(__file__).resolve().parent.parent.resolve()
        )
        if feed_url is not None:
            self._feed_url = feed_url
            self._allow_loopback = bool(allow_loopback)
        else:
            resolved_url, resolved_loopback = resolve_update_feed_url()
            self._feed_url = resolved_url
            self._allow_loopback = (
                resolved_loopback if allow_loopback is None else bool(allow_loopback)
            )
        self.timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._check_inflight_lock = threading.Lock()
        self._state = _CheckerState()

    def get_changelog(self) -> list[dict[str, Any]]:
        changelog_file = self.backend_root / "CHANGELOG.md"
        try:
            text = changelog_file.read_text(encoding="utf-8")
        except OSError:
            return []
        return parse_changelog_text(text)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            st = self._state
            return {
                "ok": True,
                "mode": "browser",
                "current_version": self.current_version,
                "can_self_update": False,
                "cannot_update_reason": "browser_mode",
                "status": st.status,
                "checking": st.checking,
                "latest_confirmed": st.latest_confirmed,
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "check_error": st.check_error,
                "checked_at": st.checked_at,
                "update": dict(st.update) if st.update is not None else None,
                "just_updated": None,
                "updated_from": "",
            }

    def check_now(self) -> dict[str, Any]:
        """Fetch latest release metadata from GitHub (singleflight serialized)."""
        with self._check_inflight_lock:
            with self._lock:
                self._state.checking = True
                self._state.latest_confirmed = False
                self._state.check_error = ""

            found: dict[str, Any] | None = None
            err_msg = ""
            try:
                req = Request(
                    self._feed_url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": f"VibeSessionHub/{self.current_version}",
                    },
                    method="GET",
                )
                with urlopen(req, timeout=self.timeout_sec) as resp:
                    raw_body = resp.read(2 * 1024 * 1024)
                payload = json.loads(raw_body.decode("utf-8"))
                found = parse_github_release(
                    payload,
                    self.current_version,
                    allow_loopback=self._allow_loopback,
                )
            except HTTPError as exc:
                err_msg = f"GitHub API HTTP {exc.code}"
                logger.info("Update check failed: %s", err_msg)
            except (URLError, TimeoutError, OSError) as exc:
                err_msg = f"无法连接更新服务：{exc}"
                logger.info("Update check network error: %s", exc)
            except Exception as exc:
                err_msg = f"解析版本信息失败：{exc}"
                logger.warning("Update check parse error: %s", exc)

            now_ms = int(time.time() * 1000)
            with self._lock:
                self._state.checking = False
                self._state.checked_at = now_ms
                if err_msg:
                    self._state.check_error = err_msg
                elif found is not None:
                    self._state.update = found
                    self._state.status = "available"
                    self._state.latest_confirmed = False
                    self._state.check_error = ""
                else:
                    self._state.update = None
                    self._state.status = "idle"
                    self._state.latest_confirmed = True
                    self._state.check_error = ""

            return self.get_status()
