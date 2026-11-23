from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..launcher import build_native_deep_link
from .base import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_SNIPPET_CHARS,
    FileParseCache,
    build_session_record,
    clean_text,
    connect_sqlite_ro,
    extract_content_text,
    extract_prs_from_raw,
    merge_prs,
    parse_iso_to_ms,
)

logger = logging.getLogger("vibe_session_hub.collectors.cursor")


def _clean_cursor_user_text(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"<user_query>([\s\S]*?)</user_query>", text)
    if m:
        text = m.group(1)
    text = re.sub(r"<timestamp>[\s\S]*?</timestamp>", " ", text)
    return clean_text(text)


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
    name = clean_text(str(jd.get("name") or ""))
    subtitle = clean_text(str(jd.get("subtitle") or ""))
    draft_text = clean_text(str(jd.get("text") or ""))
    if name:
        r["title"] = name
    if subtitle and not r["summary"]:
        r["summary"] = subtitle[:MAX_SNIPPET_CHARS]
    if draft_text and not r["first_prompt"]:
        r["first_prompt"] = draft_text[:MAX_SNIPPET_CHARS]
    cat = parse_iso_to_ms(jd.get("createdAt"))
    uat = parse_iso_to_ms(jd.get("lastUpdatedAt") or cat)
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


def _new_cursor_record() -> dict[str, Any]:
    return {
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


def _scan_workspace_storage(
    records: dict[str, dict[str, Any]],
) -> None:
    ws_root = Path(
        os.path.expanduser("~/Library/Application Support/Cursor/User/workspaceStorage")
    )
    if not ws_root.is_dir():
        return

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
        con: sqlite3.Connection | None = None
        try:
            con = connect_sqlite_ro(vscdb)
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
            ).fetchone()
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
                    r = records.setdefault(cid, _new_cursor_record())
                    if folder_path and not r["cwd"]:
                        r["cwd"] = folder_path
                    _extract_cursor_meta_into_rec(r, comp)
        except Exception as exc:
            logger.debug("Skipping Cursor workspace DB %s: %s", vscdb, exc)
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass


def _scan_global_storage(
    records: dict[str, dict[str, Any]],
    header_ids: set[str],
    subagent_parent: dict[str, str],
    empty_draft_ids: set[str],
) -> bool:
    gs_db = Path(
        os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
    )
    if not gs_db.is_file():
        return False

    has_composer_headers_table = False
    con: sqlite3.Connection | None = None
    try:
        con = connect_sqlite_ro(gs_db)
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
                    h_str = (
                        h_val
                        if isinstance(h_val, str)
                        else h_val.decode("utf-8", "ignore")
                    )
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
                r = records.setdefault(h_cid, _new_cursor_record())
                _extract_cursor_meta_into_rec(r, h_jd)

        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND value IS NOT NULL"
        ).fetchall()
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
            r = records.setdefault(cid, _new_cursor_record())
            _extract_cursor_meta_into_rec(r, jd)
            if "github.com/" in raw_s and "/pull/" in raw_s:
                r["prs"].extend(extract_prs_from_raw(raw_s))
    except Exception as exc:
        logger.debug("Failed to read Cursor globalStorage DB %s: %s", gs_db, exc)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return has_composer_headers_table


def _scan_conversation_search_db(records: dict[str, dict[str, Any]]) -> None:
    cs_db = Path(
        os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/conversation-search.db"
        )
    )
    if not cs_db.is_file():
        return

    con: sqlite3.Connection | None = None
    try:
        con = connect_sqlite_ro(cs_db)
        rows = con.execute(
            """
            SELECT c.id, c.title, c.branches, c.updated_at, c.is_archived, f.body
            FROM conversations c
            LEFT JOIN conversation_fts f ON f.rowid = c.fts_rowid
            """
        ).fetchall()
        for cid_raw, ctitle, cbranches, cupdated, carchived, fbody in rows:
            cid = str(cid_raw or "").strip()
            if not cid:
                continue
            title_s = clean_text(str(ctitle or ""))
            body_s = str(fbody or "").strip()
            if not title_s and not body_s and cid not in records:
                continue
            r = records.setdefault(cid, _new_cursor_record())
            if title_s and not r["title"]:
                r["title"] = title_s
            uat = parse_iso_to_ms(cupdated)
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
                    r["prs"].extend(extract_prs_from_raw(body_s))
                lines = [
                    clean_text(ln) for ln in body_s.splitlines() if clean_text(ln)
                ]
                if lines and not r["first_prompt"]:
                    r["first_prompt"] = lines[0][:MAX_SNIPPET_CHARS]
                if len(lines) > 1 and not r["summary"]:
                    r["summary"] = lines[-1][:MAX_SNIPPET_CHARS]
                r["search_parts"].append(clean_text(body_s)[:8000])
    except Exception as exc:
        logger.debug("Failed to read Cursor conversation-search.db: %s", exc)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _scan_agent_transcripts(
    records: dict[str, dict[str, Any]], cache: FileParseCache
) -> None:
    cp_root = Path(os.path.expanduser("~/.cursor/projects"))
    if not cp_root.is_dir():
        return

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
                                prs.extend(extract_prs_from_raw(line))
                            if len(line) > 500_000:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            role = rec.get("role")
                            msg = rec.get("message") or {}
                            content = (
                                msg.get("content")
                                if isinstance(msg, dict)
                                else rec.get("content")
                            )
                            raw_txt = extract_content_text(content)
                            if role == "user":
                                ct = _clean_cursor_user_text(raw_txt)
                                if ct:
                                    u_msgs.append(ct)
                            elif role == "assistant":
                                ct = clean_text(raw_txt)
                                if ct:
                                    a_msgs.append(ct)
                except Exception as exc:
                    logger.debug("Failed to parse Cursor transcript %s: %s", jp, exc)
                combined = u_msgs[:5] + u_msgs[-5:] + a_msgs[-4:]
                cval = {
                    "first_prompt": u_msgs[0][:MAX_SNIPPET_CHARS] if u_msgs else "",
                    "summary": a_msgs[-1][:MAX_SNIPPET_CHARS] if a_msgs else "",
                    "search_text": "\n".join(combined)[:MAX_SEARCH_TEXT_CHARS],
                    "prs": merge_prs(prs),
                    "mtime_ms": int(st.st_mtime * 1000),
                }
                cache.put(ck, st.st_mtime_ns, st.st_size, cval)

            r = records.setdefault(cid, _new_cursor_record())
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


def _fold_subagents(
    records: dict[str, dict[str, Any]], subagent_parent: dict[str, str]
) -> None:
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


def collect_cursor_sessions(
    machine_id: str, machine_name: str, cache: FileParseCache
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    header_ids: set[str] = set()
    subagent_parent: dict[str, str] = {}
    empty_draft_ids: set[str] = set()

    _scan_workspace_storage(records)
    has_composer_headers_table = _scan_global_storage(
        records, header_ids, subagent_parent, empty_draft_ids
    )
    _scan_conversation_search_db(records)
    _scan_agent_transcripts(records, cache)
    _fold_subagents(records, subagent_parent)

    results: list[dict[str, Any]] = []
    for cid, r in records.items():
        if cid in subagent_parent or cid in empty_draft_ids:
            continue
        title = clean_text(r["title"] or r["first_prompt"] or r["summary"])[:110]
        if not title and not r["search_parts"]:
            continue
        if not title:
            title = cid
        prs = merge_prs(
            r["prs"],
            extract_prs_from_raw(f"{title} {r['first_prompt']} {r['summary']}"),
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
            build_session_record(
                machine_id=machine_id,
                machine_name=machine_name,
                tool="cursor",
                session_id=cid,
                title=title,
                first_prompt=r["first_prompt"],
                summary=r["summary"],
                cwd=r["cwd"],
                branches=sorted(r["branches"]),
                prs=prs,
                created_at=created_at,
                updated_at=updated_at,
                is_archived=bool(r["is_archived"]),
                deep_link=build_native_deep_link("cursor", cid)
                if can_deep_link
                else "",
                search_text=search_blob,
            )
        )
    return results
