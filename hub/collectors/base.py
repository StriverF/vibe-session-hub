from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from ..tool_registry import get_tool_label

logger = logging.getLogger("vibe_session_hub.collectors")

APPLE_COREDATA_EPOCH_OFFSET = 978_307_200

GH_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"
)
COMMIT_PR_RE = re.compile(r"\(#(\d{3,7})\)")

MAX_SEARCH_TEXT_CHARS = 12000
MAX_SNIPPET_CHARS = 360

CollectorFn = Callable[[str, str, "FileParseCache"], list[dict[str, Any]]]


@dataclass(frozen=True)
class CollectorEntry:
    """Named local-client session collector registered in COLLECTOR_REGISTRY."""

    name: str
    collect: CollectorFn


def connect_sqlite_ro(path: Path) -> sqlite3.Connection:
    quoted = quote(str(path))
    try:
        return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    except Exception:
        return sqlite3.connect(f"file:{quoted}?immutable=1", uri=True)


def unwrap_summary_value(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ("status_detail", "summary", "recap", "text", "message"):
            v = val.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = item.get("type", "")
                if itype in ("text", "input_text", "output_text"):
                    t = item.get("text") or item.get("content") or ""
                    if isinstance(t, str):
                        parts.append(t)
        return "\n".join(parts)
    if isinstance(content, dict):
        t = content.get("text") or content.get("content")
        if isinstance(t, (str, list)):
            return extract_content_text(t)
    return ""


def extract_prs_from_raw(raw: str) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    if not raw:
        return []
    for repo, num in GH_PR_URL_RE.findall(raw):
        url = f"https://github.com/{repo}/pull/{num}"
        found[num] = {"number": str(num), "repo": repo, "url": url}
    return list(found.values())


def merge_prs(*pr_lists: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for plist in pr_lists:
        for p in plist or []:
            num = str(p.get("number") or p.get("prNumber") or "").strip()
            if not num:
                continue
            existing = merged.get(
                num, {"number": num, "repo": "", "url": "", "branch": ""}
            )
            if p.get("repo") and not existing["repo"]:
                existing["repo"] = str(p["repo"])
            if p.get("url") and not existing["url"]:
                existing["url"] = str(p["url"])
            if p.get("branch") and not existing["branch"]:
                existing["branch"] = str(p["branch"])
            if not existing["url"] and existing["repo"]:
                existing["url"] = f"https://github.com/{existing['repo']}/pull/{num}"
            merged[num] = existing
    return sorted(
        merged.values(),
        key=lambda x: int(x["number"]) if x["number"].isdigit() else 0,
        reverse=True,
    )


def parse_iso_to_ms(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val * 1000) if val < 10_000_000_000 else int(val)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.isdigit():
            v = int(s)
            return v * 1000 if v < 10_000_000_000 else v
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0
    return 0


def coredata_to_ms(val: Any) -> int:
    try:
        f = float(val)
        if f <= 0:
            return 0
        # CoreData timestamps are seconds since 2001-01-01 (~7e8 - 1e9 in 2023-2032)
        if f < 2_000_000_000:
            return int((f + APPLE_COREDATA_EPOCH_OFFSET) * 1000)
        return parse_iso_to_ms(f)
    except Exception:
        return 0


def dedupe_preserve_order(parts: list[str]) -> list[str]:
    dedup: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            dedup.append(part)
    return dedup


def build_session_record(
    *,
    machine_id: str,
    machine_name: str,
    tool: str,
    session_id: str,
    title: str,
    first_prompt: str = "",
    summary: str = "",
    cwd: str = "",
    branches: list[str] | None = None,
    prs: list[dict[str, str]] | None = None,
    created_at: int = 0,
    updated_at: int = 0,
    is_archived: bool = False,
    deep_link: str = "",
    external_url: str = "",
    search_text: str = "",
    tool_label: str = "",
) -> dict[str, Any]:
    """Construct a normalized session record shared across all collectors."""
    return {
        "uid": f"{machine_id}:{tool}:{session_id}",
        "session_id": session_id,
        "tool": tool,
        "tool_label": tool_label or get_tool_label(tool),
        "machine_id": machine_id,
        "machine_name": machine_name,
        "title": title,
        "first_prompt": first_prompt,
        "summary": summary,
        "cwd": cwd,
        "branches": list(branches or []),
        "prs": list(prs or []),
        "created_at": int(created_at or 0),
        "updated_at": int(updated_at or 0),
        "is_archived": bool(is_archived),
        "deep_link": deep_link,
        "external_url": external_url,
        "search_text": search_text[:MAX_SEARCH_TEXT_CHARS],
    }


class FileParseCache:
    """In-memory snapshot + dirty-batch write cache for file parsing.

    Separates disk scanning from SQLite write transactions so callers can load
    existing entries via a read-only connection, parse all client stores without
    holding any DB lock, and flush only modified entries in a short transaction.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        initial_entries: dict[str, tuple[int, int, str]] | None = None,
    ) -> None:
        self.conn = conn
        self._entries: dict[str, tuple[int, int, str]] = {}
        self._dirty: dict[str, tuple[int, int, str]] = {}
        if initial_entries is not None:
            self._entries.update(initial_entries)
        elif conn is not None:
            self.ensure_table(conn)
            self._entries.update(self.load_entries(conn))

    @staticmethod
    def ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_parse_cache (
                cache_key TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def load_entries(conn: sqlite3.Connection) -> dict[str, tuple[int, int, str]]:
        entries: dict[str, tuple[int, int, str]] = {}
        try:
            rows = conn.execute(
                "SELECT cache_key, mtime_ns, size, payload_json FROM file_parse_cache"
            ).fetchall()
        except sqlite3.OperationalError:
            return entries
        for row in rows:
            entries[str(row[0])] = (int(row[1]), int(row[2]), str(row[3]))
        return entries

    def get(self, key: str, mtime_ns: int, size: int) -> dict[str, Any] | None:
        hit = self._entries.get(key)
        if not hit or hit[0] != mtime_ns or hit[1] != size:
            return None
        try:
            return json.loads(hit[2])
        except Exception:
            return None

    def put(
        self, key: str, mtime_ns: int, size: int, payload: dict[str, Any]
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        entry = (mtime_ns, size, raw)
        self._entries[key] = entry
        self._dirty[key] = entry

    @property
    def dirty_rows(self) -> list[tuple[str, int, int, str]]:
        return [(k, v[0], v[1], v[2]) for k, v in self._dirty.items()]

    def flush_to_conn(self, conn: sqlite3.Connection) -> int:
        if not self._dirty:
            return 0
        self.ensure_table(conn)
        rows = self.dirty_rows
        conn.executemany(
            """
            INSERT INTO file_parse_cache (cache_key, mtime_ns, size, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                payload_json = excluded.payload_json
            """,
            rows,
        )
        self._dirty.clear()
        return len(rows)


# Backward-compatible underscore aliases
_connect_sqlite_ro = connect_sqlite_ro
_unwrap_summary_value = unwrap_summary_value
_clean_text = clean_text
_extract_content_text = extract_content_text
_extract_prs_from_raw = extract_prs_from_raw
_merge_prs = merge_prs
_parse_iso_to_ms = parse_iso_to_ms
_coredata_to_ms = coredata_to_ms
