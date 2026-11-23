from __future__ import annotations

import base64
import glob
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .launcher import build_native_deep_link

APPLE_COREDATA_EPOCH_OFFSET = 978_307_200


def _connect_sqlite_ro(path: Path) -> sqlite3.Connection:
    quoted = quote(str(path))
    try:
        return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    except Exception:
        return sqlite3.connect(f"file:{quoted}?immutable=1", uri=True)


def _unwrap_summary_value(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ("status_detail", "summary", "recap", "text", "message"):
            v = val.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""

GH_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"
)
COMMIT_PR_RE = re.compile(r"\(#(\d{3,7})\)")

MAX_SEARCH_TEXT_CHARS = 12000
MAX_SNIPPET_CHARS = 360


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_content_text(content: Any) -> str:
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
            return _extract_content_text(t)
    return ""


def _extract_prs_from_raw(raw: str) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    if not raw:
        return []
    for repo, num in GH_PR_URL_RE.findall(raw):
        url = f"https://github.com/{repo}/pull/{num}"
        found[num] = {"number": str(num), "repo": repo, "url": url}
    return list(found.values())


def _merge_prs(*pr_lists: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for plist in pr_lists:
        for p in plist or []:
            num = str(p.get("number") or p.get("prNumber") or "").strip()
            if not num:
                continue
            existing = merged.get(num, {"number": num, "repo": "", "url": "", "branch": ""})
            if p.get("repo") and not existing["repo"]:
                existing["repo"] = str(p["repo"])
            if p.get("url") and not existing["url"]:
                existing["url"] = str(p["url"])
            if p.get("branch") and not existing["branch"]:
                existing["branch"] = str(p["branch"])
            if not existing["url"] and existing["repo"]:
                existing["url"] = f"https://github.com/{existing['repo']}/pull/{num}"
            merged[num] = existing
    return sorted(merged.values(), key=lambda x: int(x["number"]) if x["number"].isdigit() else 0, reverse=True)


def _parse_iso_to_ms(val: Any) -> int:
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

    def put(self, key: str, mtime_ns: int, size: int, payload: dict[str, Any]) -> None:
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
                    prs.extend(_extract_prs_from_raw(line))
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
                    txt = _clean_text(_extract_content_text(msg.get("content")))
                    if txt and not txt.startswith("[Request interrupted"):
                        user_msgs.append(txt)
                elif rtype == "assistant" and isinstance(msg, dict):
                    txt = _clean_text(_extract_content_text(msg.get("content")))
                    if txt:
                        assistant_msgs.append(txt)
    except Exception:
        pass

    first_prompt = user_msgs[0][:MAX_SNIPPET_CHARS] if user_msgs else ""
    last_summary = assistant_msgs[-1][:MAX_SNIPPET_CHARS] if assistant_msgs else ""
    combined_parts = user_msgs[:6] + user_msgs[-6:] + assistant_msgs[-4:]
    dedup_parts: list[str] = []
    seen_p: set[str] = set()
    for part in combined_parts:
        if part not in seen_p:
            seen_p.add(part)
            dedup_parts.append(part)
    search_text = "\n".join(dedup_parts)[:MAX_SEARCH_TEXT_CHARS]

    payload = {
        "first_prompt": first_prompt,
        "last_summary": last_summary,
        "search_text": search_text,
        "prs": _merge_prs(prs),
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
        except Exception:
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
        prs = _merge_prs(
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

        title = _clean_text(str(meta.get("title") or ""))
        first_prompt = parsed_jsonl.get("first_prompt") or ""
        raw_summary = (
            _unwrap_summary_value(meta.get("postTurnSummary"))
            or _unwrap_summary_value(meta.get("lastTurnReport"))
            or parsed_jsonl.get("last_summary")
            or ""
        )
        summary = _clean_text(str(raw_summary))[:MAX_SNIPPET_CHARS]
        if not title:
            title = (first_prompt or summary or session_id)[:90]

        created_at = _parse_iso_to_ms(meta.get("createdAt"))
        updated_at = _parse_iso_to_ms(
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
            {
                "uid": f"{machine_id}:{tool_id}:{session_id}",
                "session_id": session_id,
                "tool": tool_id,
                "tool_label": tool_label,
                "machine_id": machine_id,
                "machine_name": machine_name,
                "title": title,
                "first_prompt": first_prompt,
                "summary": summary,
                "cwd": cwd,
                "branches": sorted(branches_set),
                "prs": prs,
                "created_at": created_at,
                "updated_at": updated_at,
                "is_archived": bool(meta.get("isArchived")),
                "deep_link": build_native_deep_link(tool_id, session_id),
                "external_url": "",
                "search_text": search_blob,
            }
        )
    return results


def collect_codex_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    state_db = Path(os.path.expanduser("~/.codex/state_5.sqlite"))
    hist_db = Path(os.path.expanduser("~/.codex/thread_history_1.sqlite"))
    if not state_db.exists():
        return []

    results: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{state_db}?immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        threads = con.execute(
            """
            SELECT id, title, name, cwd, git_branch, first_user_message, preview,
                   created_at_ms, updated_at_ms, created_at, updated_at, archived, rollout_path
            FROM threads
            ORDER BY updated_at_ms DESC
            """
        ).fetchall()
        con.close()
    except Exception:
        return []

    hist_con: sqlite3.Connection | None = None
    if hist_db.exists():
        try:
            hist_con = sqlite3.connect(f"file:{hist_db}?immutable=1", uri=True)
        except Exception:
            hist_con = None

    for row in threads:
        tid = str(row["id"])
        updated_ms = int(row["updated_at_ms"] or _parse_iso_to_ms(row["updated_at"]) or 0)
        created_ms = int(row["created_at_ms"] or _parse_iso_to_ms(row["created_at"]) or 0)
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
                            prs.extend(_extract_prs_from_raw(ijson))
                        if itype == "userMessage":
                            try:
                                jd = json.loads(ijson)
                                t = _clean_text(_extract_content_text(jd.get("content")))
                                if t:
                                    user_texts.append(t)
                            except Exception:
                                pass
                        elif itype == "agentMessage":
                            try:
                                jd = json.loads(ijson)
                                t = _clean_text(str(jd.get("text") or ""))
                                if t:
                                    agent_texts.append(t)
                            except Exception:
                                pass
                except Exception:
                    pass

            rpath = row["rollout_path"]
            if not user_texts and not agent_texts and rpath and os.path.exists(rpath):
                try:
                    with open(rpath, "r", encoding="utf-8", errors="ignore") as rf:
                        for line in rf:
                            if "github.com/" in line and "/pull/" in line:
                                prs.extend(_extract_prs_from_raw(line))
                except Exception:
                    pass

            combined = user_texts[:5] + user_texts[-5:] + agent_texts[-4:]
            dedup: list[str] = []
            seen: set[str] = set()
            for part in combined:
                if part not in seen:
                    seen.add(part)
                    dedup.append(part)

            cached = {
                "prs": _merge_prs(prs),
                "last_agent": agent_texts[-1][:MAX_SNIPPET_CHARS] if agent_texts else "",
                "search_text": "\n".join(dedup)[:MAX_SEARCH_TEXT_CHARS],
            }
            cache.put(ck, updated_ms, 1, cached)

        first_msg = _clean_text(str(row["first_user_message"] or ""))[:MAX_SNIPPET_CHARS]
        preview = _clean_text(str(row["preview"] or cached.get("last_agent") or ""))[
            :MAX_SNIPPET_CHARS
        ]
        title = _clean_text(
            str(row["name"] or row["title"] or first_msg or preview or tid)
        )[:110]
        branch = str(row["git_branch"] or "").strip()
        branches = [branch] if branch and branch != "HEAD" else []
        cwd = str(row["cwd"] or "")

        all_prs = _merge_prs(
            cached.get("prs") or [],
            _extract_prs_from_raw(f"{title} {first_msg} {preview}"),
        )

        search_blob = "\n".join(
            filter(None, [title, first_msg, preview, cached.get("search_text") or ""])
        )[:MAX_SEARCH_TEXT_CHARS]

        results.append(
            {
                "uid": f"{machine_id}:codex:{tid}",
                "session_id": tid,
                "tool": "codex",
                "tool_label": "Codex",
                "machine_id": machine_id,
                "machine_name": machine_name,
                "title": title,
                "first_prompt": first_msg,
                "summary": preview,
                "cwd": cwd,
                "branches": branches,
                "prs": all_prs,
                "created_at": created_ms,
                "updated_at": updated_ms,
                "is_archived": bool(row["archived"]),
                "deep_link": build_native_deep_link("codex", tid),
                "external_url": "",
                "search_text": search_blob,
            }
        )

    if hist_con is not None:
        hist_con.close()
    return results


def collect_grok_bot_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    scp = Path(
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
            except Exception:
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
                    prs = _extract_prs_from_raw(raw)
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
                                txt = _clean_text(_extract_content_text(mobj.get("content")))
                                if txt:
                                    u_msgs.append(txt)
                        elif kind == "message":
                            role = entry.get("role")
                            txt = _clean_text(_extract_content_text(entry.get("content")))
                            if txt:
                                if role == "user":
                                    u_msgs.append(txt)
                                else:
                                    a_msgs.append(txt)
                    combined = u_msgs[:6] + u_msgs[-6:] + a_msgs[-4:]
                    dedup: list[str] = []
                    seen: set[str] = set()
                    for part in combined:
                        if part not in seen:
                            seen.add(part)
                            dedup.append(part)
                    cval = {
                        "first_prompt": u_msgs[0][:MAX_SNIPPET_CHARS] if u_msgs else "",
                        "summary": a_msgs[-1][:MAX_SNIPPET_CHARS] if a_msgs else "",
                        "search_text": "\n".join(dedup)[:MAX_SEARCH_TEXT_CHARS],
                        "prs": _merge_prs(prs),
                        "min_ts": min_ts,
                        "max_ts": max_ts,
                    }
                    cache.put(ck, st.st_mtime_ns, st.st_size, cval)
                parsed_tx = cval
            except Exception:
                parsed_tx = {}

        first_prompt = parsed_tx.get("first_prompt") or ""
        summary = _clean_text(
            str(
                parsed_tx.get("summary")
                or meta.get("description")
                or ""
            )
        )[:MAX_SNIPPET_CHARS]
        title = _clean_text(
            str(meta.get("title") or meta.get("name") or first_prompt or summary or aid)
        )[:110]
        if not first_prompt and not summary and not meta.get("title") and not meta.get("name"):
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
            {
                "uid": f"{machine_id}:grok-bot:{aid}",
                "session_id": aid,
                "tool": "grok-bot",
                "tool_label": "Grok Bot",
                "machine_id": machine_id,
                "machine_name": machine_name,
                "title": title,
                "first_prompt": first_prompt,
                "summary": summary,
                "cwd": cwd,
                "branches": [],
                "prs": parsed_tx.get("prs") or [],
                "created_at": created_at,
                "updated_at": updated_at,
                "is_archived": bool(meta.get("isHiddenFromSidebar")),
                "deep_link": build_native_deep_link("grok-bot", aid),
                "external_url": "",
                "search_text": "\n".join(
                    filter(None, [title, first_prompt, summary, parsed_tx.get("search_text") or ""])
                )[:MAX_SEARCH_TEXT_CHARS],
            }
        )
    return results


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
            except Exception:
                pass
            con.close()
        except Exception:
            continue

        for row in rows:
            cid = str(row["conversation_id"])
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            ubody, abody = fts_map.get(cid, ("", ""))
            clean_u = _clean_text(ubody)
            clean_a = _clean_text(abody)
            preview = _clean_text(str(row["preview"] or clean_a))[:MAX_SNIPPET_CHARS]
            first_prompt = clean_u[:MAX_SNIPPET_CHARS]
            title = _clean_text(str(row["title"] or first_prompt or preview or cid))[:110]
            cwd = _extract_primary_workspace(str(row["workspace_uris"] or ""))
            updated_at = _parse_iso_to_ms(row["last_modified_time"])

            prs = _merge_prs(
                _extract_prs_from_raw(ubody),
                _extract_prs_from_raw(abody),
                _extract_prs_from_raw(title),
            )

            search_blob = "\n".join(
                filter(None, [title, preview, clean_u[:6000], clean_a[:6000]])
            )[:MAX_SEARCH_TEXT_CHARS]

            results.append(
                {
                    "uid": f"{machine_id}:antigravity:{cid}",
                    "session_id": cid,
                    "tool": "antigravity",
                    "tool_label": "Antigravity",
                    "machine_id": machine_id,
                    "machine_name": machine_name,
                    "title": title,
                    "first_prompt": first_prompt,
                    "summary": preview,
                    "cwd": cwd,
                    "branches": [],
                    "prs": prs,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                    "is_archived": False,
                    "deep_link": "",
                    "external_url": "",
                    "search_text": search_blob,
                }
            )
    return results


def _clean_cursor_user_text(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"<user_query>([\s\S]*?)</user_query>", text)
    if m:
        text = m.group(1)
    text = re.sub(r"<timestamp>[\s\S]*?</timestamp>", " ", text)
    return _clean_text(text)


def _decode_cursor_project_slug_cwd(slug: str) -> str:
    if not slug or not slug.startswith("Users-"):
        return ""
    parts = slug.split("-")
    if len(parts) < 2 or parts[0] != "Users":
        return ""
    user_home = f"/Users/{parts[1]}"
    if not os.path.isdir(user_home):
        return ""
    rem_parts = parts[2:]
    if not rem_parts:
        return user_home
    direct = f"{user_home}/{'/'.join(rem_parts)}"
    if os.path.isdir(direct):
        return direct

    def _walk(cur: str, idx: int) -> str:
        if idx >= len(rem_parts):
            return cur
        for end in range(len(rem_parts), idx, -1):
            seg = "-".join(rem_parts[idx:end])
            cand = os.path.join(cur, seg)
            if os.path.isdir(cand):
                hit = _walk(cand, end)
                if hit:
                    return hit
        return ""

    return _walk(user_home, 0)


def _extract_cursor_meta_into_rec(r: dict[str, Any], jd: dict[str, Any]) -> None:
    name = _clean_text(str(jd.get("name") or ""))
    subtitle = _clean_text(str(jd.get("subtitle") or ""))
    draft_text = _clean_text(str(jd.get("text") or ""))
    if name:
        r["title"] = name
    if subtitle and not r["summary"]:
        r["summary"] = subtitle[:MAX_SNIPPET_CHARS]
    if draft_text and not r["first_prompt"]:
        r["first_prompt"] = draft_text[:MAX_SNIPPET_CHARS]
    cat = _parse_iso_to_ms(jd.get("createdAt"))
    uat = _parse_iso_to_ms(jd.get("lastUpdatedAt") or cat)
    if cat and (not r["created_at"] or cat < r["created_at"]):
        r["created_at"] = cat
    if uat > r["updated_at"]:
        r["updated_at"] = uat
    if jd.get("isArchived"):
        r["is_archived"] = True
    for bk in ("gitBranch", "branch", "createdOnBranch", "branchName"):
        bv = str(jd.get(bk) or "").strip()
        if bv and bv != "HEAD":
            r["branches"].add(bv)
    wuri = str(jd.get("workspaceUri") or "")
    if wuri.startswith("file://") and not r["cwd"]:
        r["cwd"] = unquote(urlparse(wuri).path)
    wi = jd.get("workspaceIdentifier")
    if isinstance(wi, dict) and not r["cwd"]:
        uri_obj = wi.get("uri")
        if isinstance(uri_obj, dict):
            fsp = str(uri_obj.get("fsPath") or uri_obj.get("path") or "").strip()
            if fsp and os.path.isabs(fsp):
                r["cwd"] = fsp
    tracked_repos = jd.get("trackedGitRepos")
    if isinstance(tracked_repos, list):
        for repo_info in tracked_repos:
            if not isinstance(repo_info, dict):
                continue
            rpath = str(repo_info.get("repoPath") or "").strip()
            if rpath and os.path.isabs(rpath) and not r["cwd"]:
                r["cwd"] = rpath
            b_list = repo_info.get("branches")
            if isinstance(b_list, list):
                for b_item in b_list:
                    if isinstance(b_item, dict):
                        bname = str(b_item.get("branchName") or "").strip()
                        if bname and bname != "HEAD":
                            r["branches"].add(bname)


def collect_cursor_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    header_ids: set[str] = set()
    has_composer_headers_table = False
    subagent_parent: dict[str, str] = {}
    empty_draft_ids: set[str] = set()

    def ensure_rec(cid: str) -> dict[str, Any]:
        if cid not in records:
            records[cid] = {
                "title": "",
                "first_prompt": "",
                "summary": "",
                "cwd": "",
                "branches": set(),
                "prs": [],
                "created_at": 0,
                "updated_at": 0,
                "is_archived": False,
                "search_parts": [],
            }
        return records[cid]

    # 1. Workspace storage: maps composerId -> workspace folder (cwd) + metadata
    ws_root = Path(
        os.path.expanduser("~/Library/Application Support/Cursor/User/workspaceStorage")
    )
    if ws_root.is_dir():
        for ws_dir in ws_root.iterdir():
            if not ws_dir.is_dir():
                continue
            vscdb = ws_dir / "state.vscdb"
            if not vscdb.is_file():
                continue
            folder_path = ""
            ws_json = ws_dir / "workspace.json"
            if ws_json.is_file():
                try:
                    wjd = json.loads(ws_json.read_text(encoding="utf-8", errors="ignore"))
                    raw_uri = str(wjd.get("folder") or wjd.get("workspace") or "")
                    if raw_uri.startswith("file://"):
                        folder_path = unquote(urlparse(raw_uri).path)
                except Exception:
                    pass
            try:
                con = _connect_sqlite_ro(vscdb)
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
                ).fetchone()
                con.close()
                if row and row[0]:
                    raw_val = (
                        row[0]
                        if isinstance(row[0], str)
                        else row[0].decode("utf-8", "ignore")
                    )
                    cjd = json.loads(raw_val)
                    for comp in cjd.get("allComposers") or []:
                        if not isinstance(comp, dict):
                            continue
                        cid = str(comp.get("composerId") or "").strip()
                        if not cid:
                            continue
                        r = ensure_rec(cid)
                        if folder_path and not r["cwd"]:
                            r["cwd"] = folder_path
                        _extract_cursor_meta_into_rec(r, comp)
            except Exception:
                continue

    # 2. Global storage state.vscdb: composerHeaders + composerData:<id>
    gs_db = Path(
        os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
    )
    if gs_db.is_file():
        try:
            con = _connect_sqlite_ro(gs_db)
            hdr_tbl = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'composerHeaders'"
            ).fetchone()
            if hdr_tbl:
                has_composer_headers_table = True
                h_rows = con.execute(
                    "SELECT composerId, isSubagent, value FROM composerHeaders"
                ).fetchall()
                for h_cid_raw, is_sub, h_val in h_rows:
                    h_cid = str(h_cid_raw or "").strip()
                    if not h_cid:
                        continue
                    header_ids.add(h_cid)
                    if not h_val:
                        continue
                    try:
                        h_str = h_val if isinstance(h_val, str) else h_val.decode("utf-8", "ignore")
                        h_jd = json.loads(h_str)
                    except Exception:
                        continue
                    if not isinstance(h_jd, dict):
                        continue
                    spid = str(h_jd.get("subagentParentId") or "").strip()
                    if spid and spid != h_cid:
                        subagent_parent[h_cid] = spid
                    elif is_sub:
                        subagent_parent[h_cid] = ""
                    if h_jd.get("isDraft"):
                        empty_draft_ids.add(h_cid)
                    r = ensure_rec(h_cid)
                    _extract_cursor_meta_into_rec(r, h_jd)

            rows = con.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND value IS NOT NULL"
            ).fetchall()
            con.close()
            for key, val in rows:
                cid = str(key).split(":", 1)[1].strip() if ":" in str(key) else ""
                if not cid:
                    continue
                try:
                    raw_s = val if isinstance(val, str) else val.decode("utf-8", "ignore")
                    jd = json.loads(raw_s)
                except Exception:
                    continue
                if not isinstance(jd, dict):
                    continue
                sub_info = jd.get("subagentInfo")
                spid = ""
                if isinstance(sub_info, dict):
                    spid = str(sub_info.get("parentComposerId") or "").strip()
                if not spid:
                    spid = str(jd.get("subagentParentId") or "").strip()
                if spid and spid != cid:
                    subagent_parent[cid] = spid
                conv_headers = jd.get("fullConversationHeadersOnly")
                if jd.get("isDraft") and not conv_headers:
                    empty_draft_ids.add(cid)
                elif conv_headers and cid in empty_draft_ids:
                    empty_draft_ids.discard(cid)
                r = ensure_rec(cid)
                _extract_cursor_meta_into_rec(r, jd)
                if "github.com/" in raw_s and "/pull/" in raw_s:
                    r["prs"].extend(_extract_prs_from_raw(raw_s))
        except Exception:
            pass

    # 3. Global conversation-search.db: full FTS body, branches, PR links
    cs_db = Path(
        os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/conversation-search.db"
        )
    )
    if cs_db.is_file():
        try:
            con = _connect_sqlite_ro(cs_db)
            rows = con.execute(
                """
                SELECT c.id, c.title, c.branches, c.updated_at, c.is_archived, f.body
                FROM conversations c
                LEFT JOIN conversation_fts f ON f.rowid = c.fts_rowid
                """
            ).fetchall()
            con.close()
            for cid_raw, ctitle, cbranches, cupdated, carchived, fbody in rows:
                cid = str(cid_raw or "").strip()
                if not cid:
                    continue
                title_s = _clean_text(str(ctitle or ""))
                body_s = str(fbody or "").strip()
                if not title_s and not body_s and cid not in records:
                    continue
                r = ensure_rec(cid)
                if title_s and not r["title"]:
                    r["title"] = title_s
                uat = _parse_iso_to_ms(cupdated)
                if uat > r["updated_at"]:
                    r["updated_at"] = uat
                if not r["created_at"] and uat:
                    r["created_at"] = uat
                if carchived:
                    r["is_archived"] = True
                if cbranches:
                    for bpart in re.split(r"[\s,]+", str(cbranches).strip()):
                        if bpart and bpart != "HEAD":
                            r["branches"].add(bpart)
                if body_s:
                    if "github.com/" in body_s and "/pull/" in body_s:
                        r["prs"].extend(_extract_prs_from_raw(body_s))
                    lines = [
                        _clean_text(ln)
                        for ln in body_s.splitlines()
                        if _clean_text(ln)
                    ]
                    if lines and not r["first_prompt"]:
                        r["first_prompt"] = lines[0][:MAX_SNIPPET_CHARS]
                    if len(lines) > 1 and not r["summary"]:
                        r["summary"] = lines[-1][:MAX_SNIPPET_CHARS]
                    r["search_parts"].append(_clean_text(body_s)[:8000])
        except Exception:
            pass

    # 4. ~/.cursor/projects/*/agent-transcripts/**/*.jsonl
    cp_root = Path(os.path.expanduser("~/.cursor/projects"))
    if cp_root.is_dir():
        for proj_dir in cp_root.iterdir():
            if not proj_dir.is_dir() or proj_dir.name.startswith("."):
                continue
            at_dir = proj_dir / "agent-transcripts"
            if not at_dir.is_dir():
                continue
            inferred_cwd = _decode_cursor_project_slug_cwd(proj_dir.name)
            for jp in at_dir.rglob("*.jsonl"):
                if "subagents" in jp.parts:
                    continue
                cid = jp.stem
                try:
                    st = jp.stat()
                except OSError:
                    continue
                ck = f"cursor_tx:{jp}"
                cval = cache.get(ck, st.st_mtime_ns, st.st_size)
                if cval is None:
                    u_msgs: list[str] = []
                    a_msgs: list[str] = []
                    prs: list[dict[str, str]] = []
                    try:
                        with jp.open("r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                if "github.com/" in line and "/pull/" in line:
                                    prs.extend(_extract_prs_from_raw(line))
                                if len(line) > 500_000:
                                    continue
                                try:
                                    rec = json.loads(line)
                                except Exception:
                                    continue
                                role = rec.get("role")
                                msg = rec.get("message") or {}
                                content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
                                raw_txt = _extract_content_text(content)
                                if role == "user":
                                    ct = _clean_cursor_user_text(raw_txt)
                                    if ct:
                                        u_msgs.append(ct)
                                elif role == "assistant":
                                    ct = _clean_text(raw_txt)
                                    if ct:
                                        a_msgs.append(ct)
                    except Exception:
                        pass
                    combined = u_msgs[:5] + u_msgs[-5:] + a_msgs[-4:]
                    cval = {
                        "first_prompt": u_msgs[0][:MAX_SNIPPET_CHARS] if u_msgs else "",
                        "summary": a_msgs[-1][:MAX_SNIPPET_CHARS] if a_msgs else "",
                        "search_text": "\n".join(combined)[:MAX_SEARCH_TEXT_CHARS],
                        "prs": _merge_prs(prs),
                        "mtime_ms": int(st.st_mtime * 1000),
                    }
                    cache.put(ck, st.st_mtime_ns, st.st_size, cval)

                r = ensure_rec(cid)
                if inferred_cwd and not r["cwd"]:
                    r["cwd"] = inferred_cwd
                if cval.get("first_prompt") and not r["first_prompt"]:
                    r["first_prompt"] = cval["first_prompt"]
                if cval.get("summary") and not r["summary"]:
                    r["summary"] = cval["summary"]
                if cval.get("prs"):
                    r["prs"].extend(cval["prs"])
                if cval.get("search_text"):
                    r["search_parts"].append(cval["search_text"])
                mtime_ms = int(cval.get("mtime_ms") or 0)
                if mtime_ms > r["updated_at"]:
                    r["updated_at"] = mtime_ms
                if not r["created_at"] and mtime_ms:
                    r["created_at"] = mtime_ms

    # Fold internal subagent PRs & search context into their parent session
    for sub_cid, parent_cid in list(subagent_parent.items()):
        sub_rec = records.get(sub_cid)
        if not sub_rec or not parent_cid:
            continue
        cur = parent_cid
        for _ in range(5):
            nxt = subagent_parent.get(cur)
            if nxt and nxt != cur:
                cur = nxt
            else:
                break
        if cur in records and cur != sub_cid:
            records[cur]["prs"].extend(sub_rec["prs"])
            if sub_rec["search_parts"]:
                records[cur]["search_parts"].extend(sub_rec["search_parts"][:2])
            records[cur]["branches"].update(sub_rec["branches"])

    results: list[dict[str, Any]] = []
    for cid, r in records.items():
        if cid in subagent_parent or cid in empty_draft_ids:
            continue
        title = _clean_text(r["title"] or r["first_prompt"] or r["summary"])[:110]
        if not title and not r["search_parts"]:
            continue
        if not title:
            title = cid
        prs = _merge_prs(
            r["prs"],
            _extract_prs_from_raw(f"{title} {r['first_prompt']} {r['summary']}"),
        )
        updated_at = int(r["updated_at"] or r["created_at"] or 0)
        created_at = int(r["created_at"] or updated_at)
        search_blob = "\n".join(
            filter(
                None,
                [title, r["first_prompt"], r["summary"], *r["search_parts"]],
            )
        )[:MAX_SEARCH_TEXT_CHARS]
        can_deep_link = (
            cid.startswith("bc-")
            or (not has_composer_headers_table)
            or (cid in header_ids)
        )

        results.append(
            {
                "uid": f"{machine_id}:cursor:{cid}",
                "session_id": cid,
                "tool": "cursor",
                "tool_label": "Cursor",
                "machine_id": machine_id,
                "machine_name": machine_name,
                "title": title,
                "first_prompt": r["first_prompt"],
                "summary": r["summary"],
                "cwd": r["cwd"],
                "branches": sorted(r["branches"]),
                "prs": prs,
                "created_at": created_at,
                "updated_at": updated_at,
                "is_archived": bool(r["is_archived"]),
                "deep_link": build_native_deep_link("cursor", cid) if can_deep_link else "",
                "external_url": "",
                "search_text": search_blob,
            }
        )
    return results


def _coredata_to_ms(val: Any) -> int:
    try:
        f = float(val)
        if f <= 0:
            return 0
        # CoreData timestamps are seconds since 2001-01-01 (~7e8 - 1e9 in 2023-2032)
        if f < 2_000_000_000:
            return int((f + APPLE_COREDATA_EPOCH_OFFSET) * 1000)
        return _parse_iso_to_ms(f)
    except Exception:
        return 0


def collect_gemini_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1. Official Gemini macOS Desktop App (com.google.GeminiMacOS)
    gem_cache_root = Path(
        os.path.expanduser("~/Library/Caches/com.google.GeminiMacOS/Gemini")
    )
    if gem_cache_root.is_dir():
        for store_path in sorted(gem_cache_root.glob("*/ChatInfo2.store")):
            if not store_path.is_file():
                continue
            try:
                con = _connect_sqlite_ro(store_path)
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
                                _coredata_to_ms(min_t),
                                _coredata_to_ms(max_t),
                                int(has_art or 0),
                            )
                except Exception:
                    pass

                crows = con.execute(
                    """
                    SELECT ZCHATUUID, ZROBINCONVERSATIONID, ZLASTUPDATEDTIME
                    FROM ZCHATINFOSTOREDMODEL
                    ORDER BY ZLASTUPDATEDTIME DESC
                    """
                ).fetchall()
                con.close()
            except Exception:
                continue

            for chat_uuid_raw, robin_id_raw, last_upd in crows:
                chat_uuid = str(chat_uuid_raw or "").strip()
                robin_id = str(robin_id_raw or "").strip()
                cnt, uturns, min_ms, max_ms, has_art = msg_stats.get(
                    chat_uuid, (0, 0, 0, 0, 0)
                )
                # Skip empty unsent draft windows that have neither server conversation ID nor messages
                if not robin_id and cnt == 0:
                    continue
                sid = robin_id or chat_uuid
                if not sid or sid in seen_ids:
                    continue
                seen_ids.add(sid)

                updated_at = max(_coredata_to_ms(last_upd), max_ms)
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
                title = f"Gemini 桌面会话 · {dt_str} ({sid[:10]})" if dt_str else f"Gemini 桌面会话 ({sid})"
                summary = (
                    f"{turns_desc}。Gemini 官方客户端对本地对话采用加密存储；点击上方按钮可直接在 Gemini 桌面端打开此会话，也可点「+ 绑定 PR 号」补充标题与关键词。"
                )

                results.append(
                    {
                        "uid": f"{machine_id}:gemini:{sid}",
                        "session_id": sid,
                        "tool": "gemini",
                        "tool_label": "Gemini",
                        "machine_id": machine_id,
                        "machine_name": machine_name,
                        "title": title,
                        "first_prompt": "",
                        "summary": summary,
                        "cwd": "",
                        "branches": [],
                        "prs": [],
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "is_archived": False,
                        "deep_link": build_native_deep_link("gemini", sid),
                        "external_url": "",
                        "search_text": f"{title}\n{sid}\n{chat_uuid}\n{turns_desc}\ngemini desktop",
                    }
                )

    # 2. Gemini CLI plaintext session logs (~/.gemini/tmp/*/chats/session-*.json)
    gem_dot = Path(os.path.expanduser("~/.gemini"))
    if gem_dot.is_dir():
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
                        txt = _clean_text(_extract_content_text(m.get("content") or m.get("text")))
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
                        "search_text": "\n".join(u_msgs[:5] + u_msgs[-5:] + a_msgs[-4:])[:MAX_SEARCH_TEXT_CHARS],
                        "prs": _merge_prs(_extract_prs_from_raw(raw)),
                        "created_at": _parse_iso_to_ms(jd.get("startTime")) or int(st.st_mtime * 1000),
                        "updated_at": _parse_iso_to_ms(jd.get("lastUpdated")) or int(st.st_mtime * 1000),
                        "cwd": str(jd.get("cwd") or jd.get("projectRoot") or ""),
                    }
                    cache.put(ck, st.st_mtime_ns, st.st_size, cval)
                except Exception:
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
                {
                    "uid": f"{machine_id}:gemini:{sid}",
                    "session_id": sid,
                    "tool": "gemini",
                    "tool_label": "Gemini",
                    "machine_id": machine_id,
                    "machine_name": machine_name,
                    "title": title,
                    "first_prompt": fp,
                    "summary": sm,
                    "cwd": cval.get("cwd") or "",
                    "branches": [],
                    "prs": cval.get("prs") or [],
                    "created_at": int(cval.get("created_at") or 0),
                    "updated_at": int(cval.get("updated_at") or 0),
                    "is_archived": False,
                    "deep_link": "",
                    "external_url": "",
                    "search_text": f"{title}\n{cval.get('search_text') or ''}"[:MAX_SEARCH_TEXT_CHARS],
                }
            )

    return results


def collect_all_local_sessions(
    machine_id: str,
    machine_name: str,
    conn: sqlite3.Connection | None = None,
    cache: FileParseCache | None = None,
) -> list[dict[str, Any]]:
    own_cache = cache is None
    if cache is None:
        if conn is not None:
            cache = FileParseCache(conn=conn)
        else:
            cache = FileParseCache(initial_entries={})
    sessions: list[dict[str, Any]] = []

    sessions.extend(
        collect_claude_desktop_family(
            tool_id="claude-magpie",
            tool_label="Claude Magpie",
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
    )
    sessions.extend(
        collect_claude_desktop_family(
            tool_id="claude-desktop",
            tool_label="Claude",
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
    )
    sessions.extend(collect_codex_sessions(machine_id, machine_name, cache))
    sessions.extend(collect_cursor_sessions(machine_id, machine_name, cache))
    sessions.extend(collect_gemini_sessions(machine_id, machine_name, cache))
    sessions.extend(collect_grok_bot_sessions(machine_id, machine_name, cache))
    sessions.extend(collect_antigravity_sessions(machine_id, machine_name, cache))

    if own_cache and conn is not None:
        cache.flush_to_conn(conn)
        conn.commit()
    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return sessions
