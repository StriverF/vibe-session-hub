from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

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
)

logger = logging.getLogger("vibe_session_hub.collectors.grok_bot")


def collect_grok_bot_sessions(
    machine_id: str,
    machine_name: str,
    cache: FileParseCache,
    *,
    persistence_dir: Path | None = None,
) -> list[dict[str, Any]]:
    scp = persistence_dir or Path(
        os.path.expanduser(
            "~/Library/Application Support/Grok Bot/sand-client-persistence"
        )
    )
    if not scp.exists():
        return []

    roster_by_id: dict[str, dict[str, Any]] = {}
    transcript_files: dict[str, Path] = {}

    for fp in scp.glob("*.blob"):
        stem = fp.name[:-5]
        pad = "=" * ((8 - len(stem) % 8) % 8)
        try:
            dec = base64.b32decode(stem.upper() + pad).decode("utf-8", "ignore")
        except Exception:
            continue

        if ".roster.last-roster" in dec:
            try:
                jd = json.loads(fp.read_text(encoding="utf-8"))
                rows = (jd.get("value") or {}).get("rows") or []
                for r in rows:
                    if isinstance(r, dict) and r.get("id"):
                        roster_by_id[str(r["id"])] = r
            except Exception as exc:
                logger.debug("Failed to parse Grok Bot roster %s: %s", fp, exc)
                continue
        elif ".transcript.replicas." in dec:
            aid = dec.rsplit(".transcript.replicas.", 1)[-1].strip()
            if aid:
                transcript_files[aid] = fp

    all_ids = set(roster_by_id.keys()) | set(transcript_files.keys())
    results: list[dict[str, Any]] = []

    for aid in all_ids:
        meta = roster_by_id.get(aid, {})
        if meta.get("isRemovedFromSidebar"):
            continue
        tf = transcript_files.get(aid)
        parsed_tx: dict[str, Any] = {}
        if tf and tf.exists():
            try:
                st = tf.stat()
                ck = f"grok_tx:{tf}"
                cval = cache.get(ck, st.st_mtime_ns, st.st_size)
                if cval is None:
                    raw = tf.read_text(encoding="utf-8", errors="ignore")
                    prs = extract_prs_from_raw(raw)
                    jd = json.loads(raw)
                    entries = (jd.get("value") or {}).get("entries") or []
                    u_msgs: list[str] = []
                    a_msgs: list[str] = []
                    max_ts = 0
                    min_ts = 0
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        ets = int(entry.get("timestampMs") or 0)
                        if ets:
                            max_ts = max(max_ts, ets)
                            min_ts = ets if min_ts == 0 else min(min_ts, ets)
                        kind = entry.get("kind")
                        if kind == "send-message":
                            mobj = entry.get("message")
                            if isinstance(mobj, dict):
                                txt = clean_text(
                                    extract_content_text(mobj.get("content"))
                                )
                                if txt:
                                    u_msgs.append(txt)
                        elif kind == "message":
                            role = entry.get("role")
                            txt = clean_text(extract_content_text(entry.get("content")))
                            if txt:
                                if role == "user":
                                    u_msgs.append(txt)
                                else:
                                    a_msgs.append(txt)
                    dedup = dedupe_preserve_order(
                        u_msgs[:6] + u_msgs[-6:] + a_msgs[-4:]
                    )
                    cval = {
                        "first_prompt": u_msgs[0][:MAX_SNIPPET_CHARS] if u_msgs else "",
                        "summary": a_msgs[-1][:MAX_SNIPPET_CHARS] if a_msgs else "",
                        "search_text": "\n".join(dedup)[:MAX_SEARCH_TEXT_CHARS],
                        "prs": merge_prs(prs),
                        "min_ts": min_ts,
                        "max_ts": max_ts,
                    }
                    cache.put(ck, st.st_mtime_ns, st.st_size, cval)
                parsed_tx = cval
            except Exception as exc:
                logger.debug("Failed to parse Grok Bot transcript %s: %s", tf, exc)
                parsed_tx = {}

        first_prompt = parsed_tx.get("first_prompt") or ""
        summary = clean_text(
            str(parsed_tx.get("summary") or meta.get("description") or "")
        )[:MAX_SNIPPET_CHARS]
        title = clean_text(
            str(meta.get("title") or meta.get("name") or first_prompt or summary or aid)
        )[:110]
        if (
            not first_prompt
            and not summary
            and not meta.get("title")
            and not meta.get("name")
        ):
            continue

        created_at = int(meta.get("createdAt") or parsed_tx.get("min_ts") or 0)
        updated_at = int(
            meta.get("lastActivityAt")
            or meta.get("updatedAt")
            or parsed_tx.get("max_ts")
            or created_at
        )
        raw_path = str(meta.get("path") or "")
        cwd = raw_path if raw_path.startswith("/Users/") else ""

        results.append(
            build_session_record(
                machine_id=machine_id,
                machine_name=machine_name,
                tool="grok-bot",
                session_id=aid,
                title=title,
                first_prompt=first_prompt,
                summary=summary,
                cwd=cwd,
                prs=parsed_tx.get("prs") or [],
                created_at=created_at,
                updated_at=updated_at,
                is_archived=bool(meta.get("isHiddenFromSidebar")),
                deep_link=build_native_deep_link("grok-bot", aid),
                search_text="\n".join(
                    filter(
                        None,
                        [
                            title,
                            first_prompt,
                            summary,
                            parsed_tx.get("search_text") or "",
                        ],
                    )
                ),
            )
        )
    return results
