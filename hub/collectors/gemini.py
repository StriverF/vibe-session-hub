from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..launcher import build_native_deep_link
from .base import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_SNIPPET_CHARS,
    FileParseCache,
    build_session_record,
    clean_text,
    connect_sqlite_ro,
    coredata_to_ms,
    extract_content_text,
    extract_prs_from_raw,
    merge_prs,
    parse_iso_to_ms,
)

logger = logging.getLogger("vibe_session_hub.collectors.gemini")


def _collect_gemini_desktop(
    machine_id: str, machine_name: str, seen_ids: set[str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    gem_cache_root = Path(
        os.path.expanduser("~/Library/Caches/com.google.GeminiMacOS/Gemini")
    )
    if not gem_cache_root.is_dir():
        return results

    for store_path in sorted(gem_cache_root.glob("*/ChatInfo2.store")):
        if not store_path.is_file():
            continue
        con: sqlite3.Connection | None = None
        try:
            con = connect_sqlite_ro(store_path)
            msg_stats: dict[str, tuple[int, int, int, int, int]] = {}
            try:
                mrows = con.execute(
                    """
                    SELECT ZCHATUUID,
                           COUNT(*) AS cnt,
                           SUM(CASE WHEN ZISUSERTURN = 1 THEN 1 ELSE 0 END) AS uturns,
                           MIN(ZCREATEDTIME) AS min_t,
                           MAX(ZCREATEDTIME) AS max_t,
                           MAX(COALESCE(ZHASTASKARTIFACTS, 0)) AS has_art
                    FROM ZCHATMESSAGESTOREDMODEL
                    GROUP BY ZCHATUUID
                    """
                ).fetchall()
                for cuuid, cnt, uturns, min_t, max_t, has_art in mrows:
                    if cuuid:
                        msg_stats[str(cuuid)] = (
                            int(cnt or 0),
                            int(uturns or 0),
                            coredata_to_ms(min_t),
                            coredata_to_ms(max_t),
                            int(has_art or 0),
                        )
            except Exception as exc:
                logger.debug("Gemini message stats query skipped (%s): %s", store_path, exc)

            crows = con.execute(
                """
                SELECT ZCHATUUID, ZROBINCONVERSATIONID, ZLASTUPDATEDTIME
                FROM ZCHATINFOSTOREDMODEL
                ORDER BY ZLASTUPDATEDTIME DESC
                """
            ).fetchall()
        except Exception as exc:
            logger.debug("Skipping Gemini desktop store %s: %s", store_path, exc)
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

        for chat_uuid_raw, robin_id_raw, last_upd in crows:
            chat_uuid = str(chat_uuid_raw or "").strip()
            robin_id = str(robin_id_raw or "").strip()
            cnt, uturns, min_ms, max_ms, has_art = msg_stats.get(
                chat_uuid, (0, 0, 0, 0, 0)
            )
            # Skip empty unsent draft windows with neither server conversation ID nor messages
            if not robin_id and cnt == 0:
                continue
            sid = robin_id or chat_uuid
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)

            updated_at = max(coredata_to_ms(last_upd), max_ms)
            created_at = min_ms or updated_at
            dt_str = (
                datetime.fromtimestamp(updated_at / 1000).strftime("%m-%d %H:%M")
                if updated_at
                else ""
            )
            art_tag = " · 含文档/代码产物" if has_art else ""
            turns_desc = (
                f"共 {cnt} 条消息（{uturns} 轮提问）{art_tag}"
                if cnt > 0
                else "Gemini 桌面端同步会话"
            )
            title = (
                f"Gemini 桌面会话 · {dt_str} ({sid[:10]})"
                if dt_str
                else f"Gemini 桌面会话 ({sid})"
            )
            summary = (
                f"{turns_desc}。Gemini 官方客户端对本地对话采用加密存储；点击上方按钮可直接在 Gemini 桌面端打开此会话，也可点「+ 绑定 PR 号」补充标题与关键词。"
            )

            results.append(
                build_session_record(
                    machine_id=machine_id,
                    machine_name=machine_name,
                    tool="gemini",
                    session_id=sid,
                    title=title,
                    summary=summary,
                    created_at=created_at,
                    updated_at=updated_at,
                    deep_link=build_native_deep_link("gemini", sid),
                    search_text=f"{title}\n{sid}\n{chat_uuid}\n{turns_desc}\ngemini desktop",
                )
            )
    return results


def _collect_gemini_cli(
    machine_id: str,
    machine_name: str,
    cache: FileParseCache,
    seen_ids: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    gem_dot = Path(os.path.expanduser("~/.gemini"))
    if not gem_dot.is_dir():
        return results

    cli_files = list(gem_dot.glob("tmp/*/chats/session-*.json")) + list(
        gem_dot.glob("sessions/*.json")
    )
    for jf in cli_files:
        if not jf.is_file():
            continue
        try:
            st = jf.stat()
        except OSError:
            continue
        ck = f"gemini_cli:{jf}"
        cval = cache.get(ck, st.st_mtime_ns, st.st_size)
        if cval is None:
            try:
                raw = jf.read_text(encoding="utf-8", errors="ignore")
                jd = json.loads(raw)
                sid = str(jd.get("sessionId") or jf.stem)
                msgs = jd.get("messages") or []
                u_msgs: list[str] = []
                a_msgs: list[str] = []
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    mtype = str(m.get("type") or m.get("role") or "").lower()
                    txt = clean_text(
                        extract_content_text(m.get("content") or m.get("text"))
                    )
                    if not txt:
                        continue
                    if mtype in ("user", "human"):
                        u_msgs.append(txt)
                    else:
                        a_msgs.append(txt)
                cval = {
                    "sid": sid,
                    "first_prompt": u_msgs[0][:MAX_SNIPPET_CHARS] if u_msgs else "",
                    "summary": a_msgs[-1][:MAX_SNIPPET_CHARS] if a_msgs else "",
                    "search_text": "\n".join(u_msgs[:5] + u_msgs[-5:] + a_msgs[-4:])[
                        :MAX_SEARCH_TEXT_CHARS
                    ],
                    "prs": merge_prs(extract_prs_from_raw(raw)),
                    "created_at": parse_iso_to_ms(jd.get("startTime"))
                    or int(st.st_mtime * 1000),
                    "updated_at": parse_iso_to_ms(jd.get("lastUpdated"))
                    or int(st.st_mtime * 1000),
                    "cwd": str(jd.get("cwd") or jd.get("projectRoot") or ""),
                }
                cache.put(ck, st.st_mtime_ns, st.st_size, cval)
            except Exception as exc:
                logger.debug("Skipping unreadable Gemini CLI session %s: %s", jf, exc)
                continue

        sid = str(cval.get("sid") or jf.stem)
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        fp = cval.get("first_prompt") or ""
        sm = cval.get("summary") or ""
        if not fp and not sm:
            continue
        title = (fp or sm or sid)[:110]
        results.append(
            build_session_record(
                machine_id=machine_id,
                machine_name=machine_name,
                tool="gemini",
                session_id=sid,
                title=title,
                first_prompt=fp,
                summary=sm,
                cwd=cval.get("cwd") or "",
                prs=cval.get("prs") or [],
                created_at=int(cval.get("created_at") or 0),
                updated_at=int(cval.get("updated_at") or 0),
                search_text=f"{title}\n{cval.get('search_text') or ''}",
            )
        )
    return results


def collect_gemini_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    results = _collect_gemini_desktop(machine_id, machine_name, seen_ids)
    results.extend(_collect_gemini_cli(machine_id, machine_name, cache, seen_ids))
    return results
