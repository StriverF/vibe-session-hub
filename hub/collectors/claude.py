from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..launcher import build_native_deep_link
from ..tool_registry import get_tool_label
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
    unwrap_summary_value,
)

logger = logging.getLogger("vibe_session_hub.collectors.claude")


def _parse_claude_jsonl(path: Path, cache: FileParseCache) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {}
    ck = f"claude_jsonl:{path}"
    cached = cache.get(ck, st.st_mtime_ns, st.st_size)
    if cached is not None:
        return cached

    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    prs: list[dict[str, str]] = []
    branches: set[str] = set()
    cwd = ""

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "github.com/" in line and "/pull/" in line:
                    prs.extend(extract_prs_from_raw(line))
                if len(line) > 300_000 and ("base64" in line or "image/" in line):
                    continue
                if len(line) > 1_200_000:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                gb = rec.get("gitBranch")
                if isinstance(gb, str) and gb.strip() and gb != "HEAD":
                    branches.add(gb.strip())
                if not cwd and isinstance(rec.get("cwd"), str):
                    cwd = rec["cwd"]

                rtype = rec.get("type")
                msg = rec.get("message")
                if rtype == "user" and isinstance(msg, dict):
                    txt = clean_text(extract_content_text(msg.get("content")))
                    if txt and not txt.startswith("[Request interrupted"):
                        user_msgs.append(txt)
                elif rtype == "assistant" and isinstance(msg, dict):
                    txt = clean_text(extract_content_text(msg.get("content")))
                    if txt:
                        assistant_msgs.append(txt)
    except Exception as exc:
        logger.debug("Failed to read Claude JSONL %s: %s", path, exc)

    first_prompt = user_msgs[0][:MAX_SNIPPET_CHARS] if user_msgs else ""
    last_summary = assistant_msgs[-1][:MAX_SNIPPET_CHARS] if assistant_msgs else ""
    dedup_parts = dedupe_preserve_order(
        user_msgs[:6] + user_msgs[-6:] + assistant_msgs[-4:]
    )
    search_text = "\n".join(dedup_parts)[:MAX_SEARCH_TEXT_CHARS]

    payload = {
        "first_prompt": first_prompt,
        "last_summary": last_summary,
        "search_text": search_text,
        "prs": merge_prs(prs),
        "branches": sorted(branches),
        "cwd": cwd,
    }
    cache.put(ck, st.st_mtime_ns, st.st_size, payload)
    return payload


def collect_claude_desktop_family(
    tool_id: str,
    tool_label: str,
    sessions_root: Path,
    projects_root: Path,
    machine_id: str,
    machine_name: str,
    cache: FileParseCache,
) -> list[dict[str, Any]]:
    if not sessions_root.exists():
        return []

    jsonl_map: dict[str, Path] = {}
    if projects_root.exists():
        for jp in projects_root.rglob("*.jsonl"):
            if "subagents" in jp.parts:
                continue
            jsonl_map[jp.stem] = jp

    results: list[dict[str, Any]] = []
    for meta_file in sessions_root.rglob("local_*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Skipping unreadable Claude session meta %s: %s", meta_file, exc)
            continue
        if not isinstance(meta, dict):
            continue
        session_id = str(meta.get("sessionId") or meta_file.stem)
        cli_id = str(meta.get("cliSessionId") or "")
        prior_ids = [str(x) for x in (meta.get("priorCliSessionIds") or []) if x]

        parsed_jsonl: dict[str, Any] = {}
        for cid in [cli_id, *prior_ids]:
            if cid and cid in jsonl_map:
                parsed_jsonl = _parse_claude_jsonl(jsonl_map[cid], cache)
                break

        meta_prs = meta.get("prs") or []
        prs = merge_prs(
            meta_prs if isinstance(meta_prs, list) else [],
            parsed_jsonl.get("prs") or [],
        )

        branches_set: set[str] = set(parsed_jsonl.get("branches") or [])
        for wb in meta.get("writtenBranches") or []:
            if isinstance(wb, str) and wb.strip():
                branches_set.add(wb.strip())
        for pr_item in prs:
            if pr_item.get("branch"):
                branches_set.add(pr_item["branch"])

        cwd = str(
            meta.get("originCwd")
            or meta.get("cwd")
            or parsed_jsonl.get("cwd")
            or ""
        )
        if not cwd:
            anchors = meta.get("gitAnchors") or []
            if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict):
                cwd = str(anchors[0].get("gitRoot") or "")

        title = clean_text(str(meta.get("title") or ""))
        first_prompt = parsed_jsonl.get("first_prompt") or ""
        raw_summary = (
            unwrap_summary_value(meta.get("postTurnSummary"))
            or unwrap_summary_value(meta.get("lastTurnReport"))
            or parsed_jsonl.get("last_summary")
            or ""
        )
        summary = clean_text(str(raw_summary))[:MAX_SNIPPET_CHARS]
        if not title:
            title = (first_prompt or summary or session_id)[:90]

        created_at = parse_iso_to_ms(meta.get("createdAt"))
        updated_at = parse_iso_to_ms(
            meta.get("lastActivityAt") or meta.get("lastTurnEdgeAt") or created_at
        )

        search_blob = "\n".join(
            filter(
                None,
                [
                    title,
                    summary,
                    first_prompt,
                    parsed_jsonl.get("search_text") or "",
                    " ".join(meta.get("previousTitles") or []),
                ],
            )
        )[:MAX_SEARCH_TEXT_CHARS]

        results.append(
            build_session_record(
                machine_id=machine_id,
                machine_name=machine_name,
                tool=tool_id,
                tool_label=tool_label or get_tool_label(tool_id),
                session_id=session_id,
                title=title,
                first_prompt=first_prompt,
                summary=summary,
                cwd=cwd,
                branches=sorted(branches_set),
                prs=prs,
                created_at=created_at,
                updated_at=updated_at,
                is_archived=bool(meta.get("isArchived")),
                deep_link=build_native_deep_link(tool_id, session_id),
                search_text=search_blob,
            )
        )
    return results


def collect_claude_magpie_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    return collect_claude_desktop_family(
        tool_id="claude-magpie",
        tool_label=get_tool_label("claude-magpie"),
        sessions_root=Path(
            os.path.expanduser(
                "~/Library/Application Support/Claude-Magpie-3p/claude-code-sessions"
            )
        ),
        projects_root=Path(os.path.expanduser("~/.claude-magpie-desktop/projects")),
        machine_id=machine_id,
        machine_name=machine_name,
        cache=cache,
    )


def collect_claude_desktop_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    return collect_claude_desktop_family(
        tool_id="claude-desktop",
        tool_label=get_tool_label("claude-desktop"),
        sessions_root=Path(
            os.path.expanduser(
                "~/Library/Application Support/Claude/claude-code-sessions"
            )
        ),
        projects_root=Path(os.path.expanduser("~/.claude/projects")),
        machine_id=machine_id,
        machine_name=machine_name,
        cache=cache,
    )
