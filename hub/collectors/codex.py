from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..launcher import build_native_deep_link
from .base import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_SNIPPET_CHARS,
    FileParseCache,
    build_session_record,
    clean_text,
    dedupe_preserve_order,
    extract_content_text,
    extract_prs_from_raw,
    merge_prs,
    parse_iso_to_ms,
)

logger = logging.getLogger("vibe_session_hub.collectors.codex")


def collect_codex_sessions(
    machine_id: str,
    machine_name: str,
    cache: FileParseCache,
    *,
    state_db: Path | None = None,
    hist_db: Path | None = None,
) -> list[dict[str, Any]]:
    state_db = state_db or Path(os.path.expanduser("~/.codex/state_5.sqlite"))
    hist_db = hist_db or Path(os.path.expanduser("~/.codex/thread_history_1.sqlite"))
    if not state_db.exists():
        return []

    results: list[dict[str, Any]] = []
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{quote(str(state_db))}?immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        threads = con.execute(
            """
            SELECT id, title, name, cwd, git_branch, first_user_message, preview,
                   created_at_ms, updated_at_ms, created_at, updated_at, archived, rollout_path
            FROM threads
            ORDER BY updated_at_ms DESC
            """
        ).fetchall()
    except Exception as exc:
        logger.debug("Failed to read Codex state DB %s: %s", state_db, exc)
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    hist_con: sqlite3.Connection | None = None
    if hist_db.exists():
        try:
            hist_con = sqlite3.connect(f"file:{quote(str(hist_db))}?immutable=1", uri=True)
        except Exception as exc:
            logger.debug("Failed to open Codex history DB %s: %s", hist_db, exc)
            hist_con = None

    try:
        for row in threads:
            tid = str(row["id"])
            updated_ms = int(
                row["updated_at_ms"] or parse_iso_to_ms(row["updated_at"]) or 0
            )
            created_ms = int(
                row["created_at_ms"] or parse_iso_to_ms(row["created_at"]) or 0
            )
            ck = f"codex_thread:{tid}"
            cached = cache.get(ck, updated_ms, 1)

            if cached is None:
                user_texts: list[str] = []
                agent_texts: list[str] = []
                prs: list[dict[str, str]] = []

                if hist_con is not None:
                    try:
                        items = hist_con.execute(
                            """
                            SELECT item_type, item_json FROM thread_items
                            WHERE thread_id = ?
                            ORDER BY rollout_ordinal ASC
                            """,
                            (tid,),
                        ).fetchall()
                        for itype, ijson in items:
                            if not isinstance(ijson, str):
                                continue
                            if "github.com/" in ijson and "/pull/" in ijson:
                                prs.extend(extract_prs_from_raw(ijson))
                            if itype == "userMessage":
                                try:
                                    jd = json.loads(ijson)
                                    t = clean_text(extract_content_text(jd.get("content")))
                                    if t:
                                        user_texts.append(t)
                                except Exception:
                                    pass
                            elif itype == "agentMessage":
                                try:
                                    jd = json.loads(ijson)
                                    t = clean_text(str(jd.get("text") or ""))
                                    if t:
                                        agent_texts.append(t)
                                except Exception:
                                    pass
                    except Exception as exc:
                        logger.debug("Failed to query Codex history for %s: %s", tid, exc)

                rpath = row["rollout_path"]
                if not user_texts and not agent_texts and rpath and os.path.exists(rpath):
                    try:
                        with open(rpath, "r", encoding="utf-8", errors="ignore") as rf:
                            for line in rf:
                                if "github.com/" in line and "/pull/" in line:
                                    prs.extend(extract_prs_from_raw(line))
                    except Exception as exc:
                        logger.debug("Failed to scan Codex rollout %s: %s", rpath, exc)

                dedup = dedupe_preserve_order(
                    user_texts[:5] + user_texts[-5:] + agent_texts[-4:]
                )
                cached = {
                    "prs": merge_prs(prs),
                    "last_agent": agent_texts[-1][:MAX_SNIPPET_CHARS]
                    if agent_texts
                    else "",
                    "search_text": "\n".join(dedup)[:MAX_SEARCH_TEXT_CHARS],
                }
                cache.put(ck, updated_ms, 1, cached)

            first_msg = clean_text(str(row["first_user_message"] or ""))[
                :MAX_SNIPPET_CHARS
            ]
            preview = clean_text(str(row["preview"] or cached.get("last_agent") or ""))[
                :MAX_SNIPPET_CHARS
            ]
            title = clean_text(
                str(row["name"] or row["title"] or first_msg or preview or tid)
            )[:110]
            branch = str(row["git_branch"] or "").strip()
            branches = [branch] if branch and branch != "HEAD" else []
            cwd = str(row["cwd"] or "")

            all_prs = merge_prs(
                cached.get("prs") or [],
                extract_prs_from_raw(f"{title} {first_msg} {preview}"),
            )

            search_blob = "\n".join(
                filter(
                    None, [title, first_msg, preview, cached.get("search_text") or ""]
                )
            )[:MAX_SEARCH_TEXT_CHARS]

            results.append(
                build_session_record(
                    machine_id=machine_id,
                    machine_name=machine_name,
                    tool="codex",
                    session_id=tid,
                    title=title,
                    first_prompt=first_msg,
                    summary=preview,
                    cwd=cwd,
                    branches=branches,
                    prs=all_prs,
                    created_at=created_ms,
                    updated_at=updated_ms,
                    is_archived=bool(row["archived"]),
                    deep_link=build_native_deep_link("codex", tid),
                    search_text=search_blob,
                )
            )
    finally:
        if hist_con is not None:
            try:
                hist_con.close()
            except Exception:
                pass
    return results
