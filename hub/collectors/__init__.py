from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable

from .antigravity import collect_antigravity_sessions
from .base import (
    APPLE_COREDATA_EPOCH_OFFSET,
    COMMIT_PR_RE,
    GH_PR_URL_RE,
    MAX_SEARCH_TEXT_CHARS,
    MAX_SNIPPET_CHARS,
    CollectorEntry,
    CollectorFn,
    FileParseCache,
    build_session_record,
    clean_text,
    connect_sqlite_ro,
    coredata_to_ms,
    dedupe_preserve_order,
    extract_content_text,
    extract_prs_from_raw,
    merge_prs,
    parse_iso_to_ms,
    unwrap_summary_value,
)
from .claude import (
    collect_claude_desktop_family,
    collect_claude_desktop_sessions,
    collect_claude_magpie_sessions,
)
from .codex import collect_codex_sessions
from .cursor import collect_cursor_sessions
from .gemini import collect_gemini_sessions
from .grok_bot import collect_grok_bot_sessions

logger = logging.getLogger("vibe_session_hub.collectors")

COLLECTOR_REGISTRY: tuple[CollectorEntry, ...] = (
    CollectorEntry(name="claude-magpie", collect=collect_claude_magpie_sessions),
    CollectorEntry(name="claude-desktop", collect=collect_claude_desktop_sessions),
    CollectorEntry(name="codex", collect=collect_codex_sessions),
    CollectorEntry(name="cursor", collect=collect_cursor_sessions),
    CollectorEntry(name="gemini", collect=collect_gemini_sessions),
    CollectorEntry(name="grok-bot", collect=collect_grok_bot_sessions),
    CollectorEntry(name="antigravity", collect=collect_antigravity_sessions),
)


def collect_all_local_sessions(
    machine_id: str,
    machine_name: str,
    conn: sqlite3.Connection | None = None,
    cache: FileParseCache | None = None,
    collectors: Iterable[CollectorEntry] | None = None,
) -> list[dict[str, Any]]:
    own_cache = cache is None
    if cache is None:
        if conn is not None:
            cache = FileParseCache(conn=conn)
        else:
            cache = FileParseCache(initial_entries={})

    sessions: list[dict[str, Any]] = []
    active_collectors = COLLECTOR_REGISTRY if collectors is None else collectors
    for entry in active_collectors:
        try:
            collected = entry.collect(machine_id, machine_name, cache)
            if collected:
                sessions.extend(collected)
        except Exception as exc:
            logger.warning(
                "Collector '%s' failed; skipping this source: %s",
                entry.name,
                exc,
                exc_info=True,
            )

    if own_cache and conn is not None:
        cache.flush_to_conn(conn)
        conn.commit()
    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return sessions


__all__ = [
    "APPLE_COREDATA_EPOCH_OFFSET",
    "COLLECTOR_REGISTRY",
    "COMMIT_PR_RE",
    "CollectorEntry",
    "CollectorFn",
    "FileParseCache",
    "GH_PR_URL_RE",
    "MAX_SEARCH_TEXT_CHARS",
    "MAX_SNIPPET_CHARS",
    "build_session_record",
    "clean_text",
    "collect_all_local_sessions",
    "collect_antigravity_sessions",
    "collect_claude_desktop_family",
    "collect_claude_desktop_sessions",
    "collect_claude_magpie_sessions",
    "collect_codex_sessions",
    "collect_cursor_sessions",
    "collect_gemini_sessions",
    "collect_grok_bot_sessions",
    "connect_sqlite_ro",
    "coredata_to_ms",
    "dedupe_preserve_order",
    "extract_content_text",
    "extract_prs_from_raw",
    "merge_prs",
    "parse_iso_to_ms",
    "unwrap_summary_value",
]
