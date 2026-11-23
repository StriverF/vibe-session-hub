from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .base import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_SNIPPET_CHARS,
    FileParseCache,
    build_session_record,
    clean_text,
    extract_prs_from_raw,
    merge_prs,
    parse_iso_to_ms,
)

logger = logging.getLogger("vibe_session_hub.collectors.antigravity")


def _extract_primary_workspace(workspace_uris_raw: str) -> str:
    if not workspace_uris_raw:
        return ""
    try:
        uris = json.loads(workspace_uris_raw)
        if isinstance(uris, list):
            for u in uris:
                if isinstance(u, str) and u.startswith("file://"):
                    parsed = unquote(urlparse(u).path)
                    if parsed:
                        return parsed
    except Exception:
        pass
    return ""


def collect_antigravity_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    db_paths = [
        Path(os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")),
        Path(os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")),
    ]
    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    for db_path in db_paths:
        if not db_path.exists():
            continue
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT conversation_id, title, preview, workspace_uris, last_modified_time
                FROM conversation_summaries
                ORDER BY last_modified_time DESC
                """
            ).fetchall()

            fts_map: dict[str, tuple[str, str]] = {}
            try:
                fts_rows = con.execute(
                    "SELECT conversation_id, user_body, agent_body FROM conversations_fts"
                ).fetchall()
                for fr in fts_rows:
                    cid = str(fr["conversation_id"])
                    prev_u, prev_a = fts_map.get(cid, ("", ""))
                    fts_map[cid] = (
                        (prev_u + "\n" + str(fr["user_body"] or "")).strip(),
                        (prev_a + "\n" + str(fr["agent_body"] or "")).strip(),
                    )
            except Exception as exc:
                logger.debug("Antigravity FTS table skipped (%s): %s", db_path, exc)
        except Exception as exc:
            logger.debug("Skipping Antigravity DB %s: %s", db_path, exc)
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

        for row in rows:
            cid = str(row["conversation_id"])
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            ubody, abody = fts_map.get(cid, ("", ""))
            clean_u = clean_text(ubody)
            clean_a = clean_text(abody)
            preview = clean_text(str(row["preview"] or clean_a))[:MAX_SNIPPET_CHARS]
            first_prompt = clean_u[:MAX_SNIPPET_CHARS]
            title = clean_text(str(row["title"] or first_prompt or preview or cid))[
                :110
            ]
            cwd = _extract_primary_workspace(str(row["workspace_uris"] or ""))
            updated_at = parse_iso_to_ms(row["last_modified_time"])

            prs = merge_prs(
                extract_prs_from_raw(ubody),
                extract_prs_from_raw(abody),
                extract_prs_from_raw(title),
            )

            search_blob = "\n".join(
                filter(None, [title, preview, clean_u[:6000], clean_a[:6000]])
            )[:MAX_SEARCH_TEXT_CHARS]

            results.append(
                build_session_record(
                    machine_id=machine_id,
                    machine_name=machine_name,
                    tool="antigravity",
                    session_id=cid,
                    title=title,
                    first_prompt=first_prompt,
                    summary=preview,
                    cwd=cwd,
                    prs=prs,
                    created_at=updated_at,
                    updated_at=updated_at,
                    search_text=search_blob,
                )
            )
    return results
