from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
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


_SECTION_TITLE_MAP = {
    "added": "新增",
    "new": "新增",
    "feature": "新增",
    "features": "新增",
    "新增": "新增",
    "新功能": "新增",
    "changed": "优化",
    "improved": "优化",
    "improvements": "优化",
    "optimized": "优化",
    "optimization": "优化",
    "优化": "优化",
    "变更": "优化",
    "改进": "优化",
    "fixed": "修复",
    "fix": "修复",
    "fixes": "修复",
    "bug fix": "修复",
    "bug fixes": "修复",
    "bugfixes": "修复",
    "修复": "修复",
    "问题修复": "修复",
    "security": "安全",
    "安全": "安全",
    "removed": "移除",
    "deprecated": "移除",
    "移除": "移除",
    "废弃": "移除",
    "what's changed": "更新内容",
    "whats changed": "更新内容",
    "本次更新": "更新内容",
    "更新内容": "更新内容",
}


def normalize_changelog_section_title(raw_title: str) -> str:
    """Normalize English/Chinese changelog subheadings to concise Chinese labels."""
    text = str(raw_title or "").strip().strip("*`")
    if not text:
        return ""
    return _SECTION_TITLE_MAP.get(text.lower(), text)


def _append_section_item(
    sections: list[dict[str, Any]],
    title: str,
    item: str,
) -> None:
    if sections and sections[-1]["title"] == title:
        sections[-1]["items"].append(item)
    else:
        sections.append({"title": title, "items": [item]})


def parse_changelog_text(text: str) -> list[dict[str, Any]]:
    """Parse released version entries from CHANGELOG.md (newest first)."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_section = ""
    skip_until_next_version = False
    in_code_fence = False

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if line.startswith("## "):
            skip_until_next_version = False
            current_section = ""
            match = _CHANGELOG_HEADING_RE.match(line)
            if match:
                current = {
                    "version": match.group(1),
                    "date": match.group(2) or "",
                    "items": [],
                    "sections": [],
                }
                entries.append(current)
            else:
                current = None
            continue
        if current is None or skip_until_next_version:
            continue
        if line.startswith("### "):
            if _INSTALL_SECTION_RE.match(line):
                skip_until_next_version = True
                continue
            current_section = normalize_changelog_section_title(line[4:])
            continue
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item:
                current["items"].append(item)
                _append_section_item(current["sections"], current_section, item)
    return entries


def parse_release_notes_sections(notes: str) -> list[dict[str, Any]]:
    """Extract categorized bullet sections from cleaned GitHub Release markdown notes."""
    bullet_sections: list[dict[str, Any]] = []
    plain_sections: list[dict[str, Any]] = []
    current_section = ""
    in_code_fence = False

    for raw_line in str(notes or "").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence or not line or line.startswith(">"):
            continue
        if line.startswith("### "):
            if _INSTALL_SECTION_RE.match(line):
                break
            current_section = normalize_changelog_section_title(line[4:])
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("- ", "* ", "• ")):
            item = line[2:].strip()
            if item:
                _append_section_item(bullet_sections, current_section, item)
        else:
            _append_section_item(plain_sections, current_section, line)
    return bullet_sections if bullet_sections else plain_sections


def parse_release_notes_items(notes: str) -> list[str]:
    """Extract flat bullet items from cleaned GitHub Release markdown notes."""
    items: list[str] = []
    for sec in parse_release_notes_sections(notes):
        items.extend(sec["items"])
    return items


def parse_github_releases_changelog(payload: Any) -> list[dict[str, Any]]:
    """Parse a GitHub Releases list API response into changelog entries."""
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft") is True or item.get("prerelease") is True:
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag:
            continue
        version = tag[1:] if tag.startswith(("v", "V")) else tag
        if not version or _parse_semver(version) is None:
            continue
        cleaned_notes = clean_release_notes(str(item.get("body") or ""))
        note_sections = parse_release_notes_sections(cleaned_notes)
        note_items = [it for sec in note_sections for it in sec["items"]]
        if not note_items:
            continue
        pub_raw = str(item.get("published_at") or "").strip()
        date_str = pub_raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", pub_raw) else ""
        entries.append(
            {
                "version": version,
                "date": date_str,
                "items": note_items,
                "sections": note_sections,
            }
        )
    return entries


def _normalize_sections_field(
    raw_sections: Any,
    fallback_items: list[str],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if isinstance(raw_sections, list):
        for sec in raw_sections:
            if not isinstance(sec, dict):
                continue
            title = normalize_changelog_section_title(str(sec.get("title") or ""))
            sec_items = [str(x).strip() for x in (sec.get("items") or []) if str(x).strip()]
            for it in sec_items:
                _append_section_item(sections, title, it)
    if not sections and fallback_items:
        sections.append({"title": "", "items": list(fallback_items)})
    return sections


def merge_changelog_entries(
    local_entries: list[dict[str, Any]],
    remote_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge local CHANGELOG.md entries with remote GitHub Releases, preferring richer local items."""
    by_version: dict[str, dict[str, Any]] = {}

    def _upsert(entry: dict[str, Any], *, is_local: bool) -> None:
        raw_ver = str(entry.get("version") or "").strip()
        ver = raw_ver[1:] if raw_ver.startswith(("v", "V")) else raw_ver
        if not ver:
            return
        date_str = str(entry.get("date") or "").strip()
        items = [str(x).strip() for x in (entry.get("items") or []) if str(x).strip()]
        sections = _normalize_sections_field(entry.get("sections"), items)
        if not items and sections:
            items = [it for sec in sections for it in sec["items"]]
        existing = by_version.get(ver)
        if existing is None:
            by_version[ver] = {
                "version": ver,
                "date": date_str,
                "items": items,
                "sections": sections,
            }
            return
        if is_local:
            if items:
                existing["items"] = items
                existing["sections"] = sections
            if date_str:
                existing["date"] = date_str
        else:
            if not existing["items"] and items:
                existing["items"] = items
                existing["sections"] = sections
            if not existing["date"] and date_str:
                existing["date"] = date_str

    for e in local_entries:
        _upsert(e, is_local=True)
    for e in remote_entries:
        _upsert(e, is_local=False)

    merged = list(by_version.values())
    merged.sort(
        key=cmp_to_key(lambda a, b: compare_semver(b["version"], a["version"]))
    )
    return merged


def derive_releases_list_url(
    feed_url: str,
    *,
    allow_loopback: bool = False,
    per_page: int = 15,
) -> str:
    """Derive a trusted `/releases?per_page=N` URL from a `/releases/latest` feed URL."""
    raw = str(feed_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    path = parsed.path.rstrip("/")
    if not path.endswith("/releases/latest"):
        return ""
    list_path = path[: -len("/latest")]
    query = urlencode({"per_page": max(1, min(30, int(per_page)))})
    candidate = urlunparse(
        (parsed.scheme, parsed.netloc, list_path, "", query, "")
    )
    if not is_trusted_update_url(candidate, allow_loopback=allow_loopback):
        return ""
    return candidate


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
        releases_url: str | None = None,
        allow_loopback: bool | None = None,
        timeout_sec: float = DEFAULT_UPDATE_TIMEOUT_SEC,
        remote_changelog_ttl_sec: float = 900.0,
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
        if releases_url is not None:
            self._releases_url = (
                releases_url
                if is_trusted_update_url(
                    releases_url, allow_loopback=self._allow_loopback
                )
                else ""
            )
        else:
            self._releases_url = derive_releases_list_url(
                self._feed_url, allow_loopback=self._allow_loopback
            )
        self.timeout_sec = timeout_sec
        self._remote_changelog_ttl_sec = max(1.0, float(remote_changelog_ttl_sec))
        self._lock = threading.Lock()
        self._check_inflight_lock = threading.Lock()
        self._changelog_fetch_lock = threading.Lock()
        self._remote_changelog_entries: list[dict[str, Any]] | None = None
        self._remote_changelog_fetched_at: float = 0.0
        self._remote_changelog_error_at: float = 0.0
        self._state = _CheckerState()

    def _fetch_remote_changelog(self) -> list[dict[str, Any]]:
        if not self._releases_url:
            return []
        now = time.monotonic()
        with self._lock:
            if (
                self._remote_changelog_entries is not None
                and (now - self._remote_changelog_fetched_at) < self._remote_changelog_ttl_sec
            ):
                return [dict(e) for e in self._remote_changelog_entries]
            if (
                self._remote_changelog_error_at > 0
                and (now - self._remote_changelog_error_at) < 120.0
            ):
                return [dict(e) for e in (self._remote_changelog_entries or [])]

        with self._changelog_fetch_lock:
            now = time.monotonic()
            with self._lock:
                if (
                    self._remote_changelog_entries is not None
                    and (now - self._remote_changelog_fetched_at) < self._remote_changelog_ttl_sec
                ):
                    return [dict(e) for e in self._remote_changelog_entries]
                if (
                    self._remote_changelog_error_at > 0
                    and (now - self._remote_changelog_error_at) < 120.0
                ):
                    return [dict(e) for e in (self._remote_changelog_entries or [])]

            try:
                req = Request(
                    self._releases_url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": f"VibeSessionHub/{self.current_version}",
                    },
                    method="GET",
                )
                with urlopen(req, timeout=min(self.timeout_sec, 6.0)) as resp:
                    raw_body = resp.read(2 * 1024 * 1024)
                payload = json.loads(raw_body.decode("utf-8"))
                parsed = parse_github_releases_changelog(payload)
                with self._lock:
                    self._remote_changelog_entries = parsed
                    self._remote_changelog_fetched_at = time.monotonic()
                    self._remote_changelog_error_at = 0.0
                return [dict(e) for e in parsed]
            except Exception as exc:
                logger.info("Remote changelog fetch failed: %s", exc)
                with self._lock:
                    self._remote_changelog_error_at = time.monotonic()
                    return [dict(e) for e in (self._remote_changelog_entries or [])]

    def get_changelog(self, *, include_remote: bool = False) -> list[dict[str, Any]]:
        changelog_file = self.backend_root / "CHANGELOG.md"
        local_entries: list[dict[str, Any]] = []
        try:
            text = changelog_file.read_text(encoding="utf-8")
            local_entries = parse_changelog_text(text)
        except OSError:
            local_entries = []

        has_current = any(
            compare_semver(e["version"], self.current_version) == 0
            for e in local_entries
        )
        auto_disabled = os.environ.get("VIBE_HUB_DISABLE_AUTO_UPDATE_CHECK") == "1"

        remote_entries: list[dict[str, Any]] = []
        if include_remote or (not has_current and not auto_disabled):
            remote_entries = self._fetch_remote_changelog()
        else:
            with self._lock:
                if self._remote_changelog_entries:
                    remote_entries = [dict(e) for e in self._remote_changelog_entries]

        if not remote_entries:
            return local_entries
        return merge_changelog_entries(local_entries, remote_entries)

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
                "updated_notes": "",
                "updated_published_at": "",
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
