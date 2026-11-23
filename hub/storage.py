from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .collectors import collect_all_local_sessions
from .config import (
    ICLOUD_SYNC_ROOT,
    INDEX_DB_PATH,
    LOCAL_SYNC_ROOT,
    MANUAL_LINKS_DIR,
    NODES_DIR,
    ensure_dirs,
    get_machine_info,
    is_icloud_enabled,
)
from .git_pr_resolver import (
    parse_pr_query,
    resolve_pr_from_local_git,
    resolve_pr_via_gh,
)


def _trigger_icloud_downloads(folder: Path) -> None:
    try:
        if not folder.exists():
            return
        for item in folder.iterdir():
            if item.name.startswith(".") and item.name.endswith(".icloud"):
                try:
                    subprocess.Popen(
                        ["/usr/bin/brctl", "download", str(item)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
    except OSError:
        return


class SessionHubStore:
    def __init__(self, local_only: bool = False) -> None:
        self.local_only = local_only
        self.icloud_status = "local_only" if local_only else "init"
        ensure_dirs(local_only=local_only)
        self._lock = threading.RLock()
        self._last_export_digest = ""
        self._shard_fingerprints: dict[str, tuple[int, int]] = {}
        self._last_local_scan_ms = 0
        self.conn = sqlite3.connect(str(INDEX_DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    uid TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    tool_label TEXT NOT NULL,
                    machine_id TEXT NOT NULL,
                    machine_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    first_prompt TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    branches_json TEXT NOT NULL,
                    branches_text TEXT NOT NULL,
                    prs_json TEXT NOT NULL,
                    pr_numbers TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    is_archived INTEGER NOT NULL,
                    deep_link TEXT NOT NULL,
                    external_url TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool);
                CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                    uid UNINDEXED,
                    title,
                    branches_text,
                    pr_numbers,
                    cwd,
                    search_text,
                    tokenize='unicode61'
                );

                CREATE TABLE IF NOT EXISTS pr_cache (
                    pr_number TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            self.conn.commit()

    def sync_now(self, force_local: bool = False) -> dict[str, Any]:
        with self._lock:
            t0 = time.time()
            ensure_dirs(local_only=self.local_only)
            minfo = get_machine_info()
            mid = minfo["machine_id"]
            mname = minfo["machine_name"]

            now_ms = int(t0 * 1000)
            if force_local or (now_ms - self._last_local_scan_ms > 8_000):
                local_sessions = collect_all_local_sessions(mid, mname, self.conn)
                self._export_local_shard(mid, mname, local_sessions)
                self._last_local_scan_ms = now_ms

            self._mirror_with_icloud(mid)
            reloaded = self._ingest_sync_shards(force=force_local)
            elapsed_ms = int((time.time() - t0) * 1000)
            total = self.conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            active_sync_root = (
                str(ICLOUD_SYNC_ROOT)
                if is_icloud_enabled(self.local_only)
                else str(LOCAL_SYNC_ROOT)
            )
            return {
                "ok": True,
                "elapsed_ms": elapsed_ms,
                "reloaded_shards": reloaded,
                "total_sessions": total,
                "sync_root": active_sync_root,
                "icloud_status": self.icloud_status,
                "machine": minfo,
            }

    def _export_local_shard(
        self, machine_id: str, machine_name: str, sessions: list[dict[str, Any]]
    ) -> None:
        lines = [
            json.dumps(
                {
                    "_meta": True,
                    "machine_id": machine_id,
                    "machine_name": machine_name,
                    "exported_at": int(time.time() * 1000),
                },
                ensure_ascii=False,
            )
        ]
        for s in sessions:
            s_copy = dict(s)
            s_copy["machine_name"] = machine_name
            lines.append(json.dumps(s_copy, ensure_ascii=False))

        blob = "\n".join(lines) + "\n"
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        shard_path = NODES_DIR / f"{machine_id}.jsonl"
        if digest == self._last_export_digest and shard_path.exists():
            return

        tmp_path = NODES_DIR / f".{machine_id}.jsonl.tmp"
        tmp_path.write_text(blob, encoding="utf-8")
        os.replace(tmp_path, shard_path)
        self._last_export_digest = digest

    def _mirror_with_icloud(self, machine_id: str) -> None:
        if not is_icloud_enabled(self.local_only):
            self.icloud_status = "local_only"
            return

        icloud_nodes = ICLOUD_SYNC_ROOT / "nodes"
        icloud_links = ICLOUD_SYNC_ROOT / "manual_links"
        try:
            icloud_nodes.mkdir(parents=True, exist_ok=True)
            icloud_links.mkdir(parents=True, exist_ok=True)
            _trigger_icloud_downloads(icloud_nodes)
            _trigger_icloud_downloads(icloud_links)

            local_shard = NODES_DIR / f"{machine_id}.jsonl"
            cloud_shard = icloud_nodes / f"{machine_id}.jsonl"
            if local_shard.exists():
                l_stat = local_shard.stat()
                need_push = (
                    not cloud_shard.exists()
                    or cloud_shard.stat().st_size != l_stat.st_size
                    or cloud_shard.stat().st_mtime_ns < l_stat.st_mtime_ns
                )
                if need_push:
                    cloud_shard.write_bytes(local_shard.read_bytes())

            for remote_file in icloud_nodes.glob("*.jsonl"):
                if remote_file.name == f"{machine_id}.jsonl":
                    continue
                dest = NODES_DIR / remote_file.name
                r_stat = remote_file.stat()
                if not dest.exists() or dest.stat().st_mtime_ns < r_stat.st_mtime_ns or dest.stat().st_size != r_stat.st_size:
                    tmp = NODES_DIR / f".{remote_file.name}.tmp"
                    tmp.write_bytes(remote_file.read_bytes())
                    os.replace(tmp, dest)

            local_links = MANUAL_LINKS_DIR / f"{machine_id}-links.jsonl"
            cloud_links = icloud_links / f"{machine_id}-links.jsonl"
            if local_links.exists():
                ll_stat = local_links.stat()
                if not cloud_links.exists() or cloud_links.stat().st_size < ll_stat.st_size:
                    cloud_links.write_bytes(local_links.read_bytes())

            for rlink in icloud_links.glob("*.jsonl"):
                if rlink.name == f"{machine_id}-links.jsonl":
                    continue
                dest_link = MANUAL_LINKS_DIR / rlink.name
                rl_stat = rlink.stat()
                if not dest_link.exists() or dest_link.stat().st_size != rl_stat.st_size or dest_link.stat().st_mtime_ns < rl_stat.st_mtime_ns:
                    tmp_l = MANUAL_LINKS_DIR / f".{rlink.name}.tmp"
                    tmp_l.write_bytes(rlink.read_bytes())
                    os.replace(tmp_l, dest_link)

            self.icloud_status = "synced"
        except OSError:
            self.icloud_status = "local_fallback"

    def _load_manual_links(self) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        _trigger_icloud_downloads(MANUAL_LINKS_DIR)
        attachments: dict[str, list[dict[str, Any]]] = {}
        standalones: list[dict[str, Any]] = []
        if not MANUAL_LINKS_DIR.exists():
            return attachments, standalones

        seen_ids: set[str] = set()
        files = sorted(MANUAL_LINKS_DIR.glob("*.jsonl"))
        for fp in files:
            try:
                for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    lid = str(item.get("link_id") or "")
                    if lid and lid in seen_ids:
                        continue
                    if lid:
                        seen_ids.add(lid)
                    if item.get("deleted"):
                        continue
                    target_uid = str(item.get("target_uid") or "").strip()
                    if target_uid:
                        attachments.setdefault(target_uid, []).append(item)
                    else:
                        standalones.append(item)
            except Exception:
                continue
        return attachments, standalones

    def _ingest_sync_shards(self, force: bool = False) -> bool:
        _trigger_icloud_downloads(NODES_DIR)
        current_fp: dict[str, tuple[int, int]] = {}
        for folder in (NODES_DIR, MANUAL_LINKS_DIR):
            if not folder.exists():
                continue
            for p in sorted(folder.glob("*.jsonl")):
                try:
                    st = p.stat()
                    current_fp[str(p)] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue

        if not force and current_fp == self._shard_fingerprints:
            return False

        attachments, standalones = self._load_manual_links()
        all_records: dict[str, dict[str, Any]] = {}

        for shard in sorted(NODES_DIR.glob("*.jsonl")):
            shard_machine_name = ""
            try:
                with shard.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if obj.get("_meta"):
                            shard_machine_name = str(obj.get("machine_name") or "")
                            continue
                        uid = str(obj.get("uid") or "")
                        if not uid:
                            continue
                        if shard_machine_name:
                            obj["machine_name"] = shard_machine_name
                        all_records[uid] = obj
            except Exception:
                continue

        for uid, extra_list in attachments.items():
            if uid not in all_records:
                continue
            rec = all_records[uid]
            prs = list(rec.get("prs") or [])
            notes: list[str] = []
            for ex in extra_list:
                pr_num = str(ex.get("pr_number") or "").strip()
                pr_url = str(ex.get("pr_url") or "").strip()
                if pr_num:
                    prs.append({"number": pr_num, "repo": "", "url": pr_url, "branch": ""})
                if ex.get("external_url") and not rec.get("external_url"):
                    rec["external_url"] = str(ex["external_url"])
                if ex.get("title"):
                    rec["title"] = str(ex["title"])
                    notes.append(str(ex["title"]))
                if ex.get("note"):
                    notes.append(str(ex["note"]))
            dedup_prs: dict[str, dict[str, str]] = {}
            for p in prs:
                n = str(p.get("number") or "").strip()
                if n:
                    if n not in dedup_prs or (p.get("url") and not dedup_prs[n].get("url")):
                        dedup_prs[n] = {
                            "number": n,
                            "repo": str(p.get("repo") or ""),
                            "url": str(p.get("url") or ""),
                            "branch": str(p.get("branch") or ""),
                        }
            rec["prs"] = list(dedup_prs.values())
            if notes:
                rec["search_text"] = (
                    " ".join(notes) + "\n" + str(rec.get("search_text") or "")
                )

        for item in standalones:
            lid = str(item.get("link_id") or "")
            if not lid:
                continue
            mid = str(item.get("machine_id") or "shared")
            mname = str(item.get("machine_name") or "跨设备共享")
            tool = str(item.get("tool") or "web-link")
            tool_label = str(item.get("tool_label") or "外部会话链接")
            pr_num = str(item.get("pr_number") or "").strip()
            pr_url = str(item.get("pr_url") or "").strip()
            prs = [{"number": pr_num, "repo": "", "url": pr_url, "branch": ""}] if pr_num else []
            branch = str(item.get("branch") or "").strip()
            title = str(item.get("title") or item.get("note") or item.get("external_url") or lid)
            note = str(item.get("note") or "")
            ts = int(item.get("created_at") or int(time.time() * 1000))
            uid = f"{mid}:{tool}:{lid}"
            all_records[uid] = {
                "uid": uid,
                "session_id": lid,
                "tool": tool,
                "tool_label": tool_label,
                "machine_id": mid,
                "machine_name": mname,
                "title": title,
                "first_prompt": note,
                "summary": str(item.get("external_url") or ""),
                "cwd": str(item.get("cwd") or ""),
                "branches": [branch] if branch else [],
                "prs": prs,
                "created_at": ts,
                "updated_at": ts,
                "is_archived": False,
                "deep_link": str(item.get("external_url") or ""),
                "external_url": str(item.get("external_url") or ""),
                "search_text": f"{title}\n{note}\n{branch}\n{pr_num}",
            }

        self.conn.execute("DELETE FROM sessions")
        self.conn.execute("DELETE FROM sessions_fts")

        rows_to_insert = []
        fts_to_insert = []
        for rec in all_records.values():
            branches = rec.get("branches") or []
            prs = rec.get("prs") or []
            pr_nums = " ".join(
                f"#{p.get('number')} {p.get('number')} {p.get('url', '')}"
                for p in prs
                if p.get("number")
            )
            pr_pipe = "|" + "|".join(str(p.get("number")) for p in prs if p.get("number")) + "|"
            branches_text = " ".join(branches)
            rows_to_insert.append(
                (
                    rec["uid"],
                    rec["session_id"],
                    rec["tool"],
                    rec["tool_label"],
                    rec["machine_id"],
                    rec["machine_name"],
                    rec.get("title") or "",
                    rec.get("first_prompt") or "",
                    rec.get("summary") or "",
                    rec.get("cwd") or "",
                    json.dumps(branches, ensure_ascii=False),
                    branches_text,
                    json.dumps(prs, ensure_ascii=False),
                    pr_pipe if prs else "",
                    int(rec.get("created_at") or 0),
                    int(rec.get("updated_at") or 0),
                    1 if rec.get("is_archived") else 0,
                    rec.get("deep_link") or "",
                    rec.get("external_url") or "",
                    rec.get("search_text") or "",
                )
            )
            fts_to_insert.append(
                (
                    rec["uid"],
                    rec.get("title") or "",
                    branches_text,
                    pr_nums,
                    rec.get("cwd") or "",
                    rec.get("search_text") or "",
                )
            )

        self.conn.executemany(
            """
            INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        self.conn.executemany(
            """
            INSERT INTO sessions_fts (uid, title, branches_text, pr_numbers, cwd, search_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            fts_to_insert,
        )
        self.conn.commit()
        self._shard_fingerprints = current_fp
        return True

    def add_manual_link(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            minfo = get_machine_info()
            mid = minfo["machine_id"]
            lid = f"link_{int(time.time() * 1000)}"
            raw_ext = str(data.get("external_url") or "").strip()
            safe_ext = raw_ext if raw_ext.startswith(("https://", "http://")) else ""
            entry = {
                "link_id": lid,
                "target_uid": str(data.get("target_uid") or "").strip(),
                "pr_number": re.sub(r"[^0-9]", "", str(data.get("pr_number") or "")),
                "pr_url": str(data.get("pr_url") or "").strip(),
                "external_url": safe_ext,
                "title": str(data.get("title") or "").strip(),
                "branch": str(data.get("branch") or "").strip(),
                "note": str(data.get("note") or "").strip(),
                "tool": str(data.get("tool") or "grok-web").strip(),
                "tool_label": str(data.get("tool_label") or "Grok / Web").strip(),
                "machine_id": mid,
                "machine_name": minfo["machine_name"],
                "created_at": int(time.time() * 1000),
            }
            link_file = MANUAL_LINKS_DIR / f"{mid}-links.jsonl"
            with link_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._mirror_with_icloud(mid)
            self._ingest_sync_shards(force=True)
            return entry

    def get_session_by_uid(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE uid = ?", (uid,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_facets(self) -> dict[str, Any]:
        with self._lock:
            minfo = get_machine_info()
            tools = self.conn.execute(
                "SELECT tool, tool_label, count(*) AS cnt FROM sessions GROUP BY tool ORDER BY cnt DESC"
            ).fetchall()
            machines = self.conn.execute(
                "SELECT machine_id, MAX(machine_name) AS machine_name, count(*) AS cnt FROM sessions GROUP BY machine_id ORDER BY cnt DESC"
            ).fetchall()
            pr_count = self.conn.execute(
                "SELECT count(*) FROM sessions WHERE pr_numbers != ''"
            ).fetchone()[0]
            total = self.conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            return {
                "current_machine": minfo,
                "sync_root": str(ICLOUD_SYNC_ROOT)
                if is_icloud_enabled(self.local_only)
                else str(LOCAL_SYNC_ROOT),
                "icloud_status": self.icloud_status,
                "total": total,
                "with_pr_count": pr_count,
                "tools": [
                    {"tool": r["tool"], "label": r["tool_label"], "count": r["cnt"]}
                    for r in tools
                ],
                "machines": [
                    {
                        "machine_id": r["machine_id"],
                        "machine_name": r["machine_name"],
                        "is_local": r["machine_id"] == minfo["machine_id"],
                        "count": r["cnt"],
                    }
                    for r in machines
                ],
            }

    def _get_candidate_repos(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT cwd, count(*) AS c FROM sessions WHERE cwd != '' GROUP BY cwd ORDER BY c DESC LIMIT 20"
        ).fetchall()
        repos = [str(r["cwd"]) for r in rows]
        extra = os.environ.get("VIBE_HUB_EXTRA_REPOS", "").strip()
        if extra:
            for p in extra.split(os.pathsep):
                clean_p = os.path.expanduser(p.strip())
                if clean_p and clean_p not in repos and os.path.isdir(clean_p):
                    repos.insert(0, clean_p)
        return repos

    def _resolve_pr_info(
        self, pr_num: str, repo_slug: str | None, allow_remote: bool
    ) -> dict[str, Any] | None:
        cached = self.conn.execute(
            "SELECT payload_json FROM pr_cache WHERE pr_number = ?", (pr_num,)
        ).fetchone()
        if cached:
            try:
                data = json.loads(cached[0])
                if data.get("branch") or not allow_remote:
                    return data
            except Exception:
                pass

        repos = self._get_candidate_repos()
        info = resolve_pr_from_local_git(pr_num, repos)
        if allow_remote and (not info or not info.get("branch")):
            gh_info = resolve_pr_via_gh(pr_num, repo_slug, repos)
            if gh_info:
                info = gh_info

        if info:
            self.conn.execute(
                """
                INSERT INTO pr_cache (pr_number, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pr_number) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (pr_num, json.dumps(info, ensure_ascii=False), int(time.time() * 1000)),
            )
            self.conn.commit()
        return info

    def _row_to_dict(
        self,
        row: sqlite3.Row,
        match_reason: str = "",
        matched_snippet: str = "",
    ) -> dict[str, Any]:
        minfo = get_machine_info()
        return {
            "uid": row["uid"],
            "session_id": row["session_id"],
            "tool": row["tool"],
            "tool_label": row["tool_label"],
            "machine_id": row["machine_id"],
            "machine_name": row["machine_name"],
            "is_local": row["machine_id"] == minfo["machine_id"],
            "title": row["title"],
            "first_prompt": row["first_prompt"],
            "summary": row["summary"],
            "cwd": row["cwd"],
            "branches": json.loads(row["branches_json"] or "[]"),
            "prs": json.loads(row["prs_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "is_archived": bool(row["is_archived"]),
            "deep_link": row["deep_link"],
            "external_url": row["external_url"],
            "match_reason": match_reason,
            "matched_snippet": matched_snippet,
        }

    @staticmethod
    def _extract_snippet(text: str, terms: list[str], radius: int = 90) -> str:
        if not text or not terms:
            return ""
        low = text.lower()
        for t in terms:
            tl = t.lower()
            if not tl:
                continue
            pos = low.find(tl)
            if pos != -1:
                start = max(0, pos - radius)
                end = min(len(text), pos + len(t) + radius)
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(text) else ""
                return prefix + text[start:end].replace("\n", " ").strip() + suffix
        return ""

    def search(
        self,
        query: str = "",
        tool: str = "",
        machine_id: str = "",
        only_with_pr: bool = False,
        resolve_remote_pr: bool = False,
        limit: int = 80,
    ) -> dict[str, Any]:
        with self._lock:
            t0 = time.time()
            self._ingest_sync_shards(force=False)

            where_clauses: list[str] = []
            base_params: list[Any] = []
            if tool:
                where_clauses.append("tool = ?")
                base_params.append(tool)
            if machine_id:
                where_clauses.append("machine_id = ?")
                base_params.append(machine_id)
            if only_with_pr:
                where_clauses.append("pr_numbers != ''")

            filter_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
            q = query.strip()

            if not q:
                rows = self.conn.execute(
                    f"SELECT * FROM sessions WHERE 1=1 {filter_sql} ORDER BY updated_at DESC LIMIT ?",
                    [*base_params, limit],
                ).fetchall()
                return {
                    "items": [self._row_to_dict(r) for r in rows],
                    "resolved_pr": None,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            pr_num, repo_slug = parse_pr_query(q)
            scored: dict[str, tuple[float, sqlite3.Row, str, str]] = {}
            resolved_pr: dict[str, Any] | None = None

            if pr_num:
                direct_rows = self.conn.execute(
                    f"SELECT * FROM sessions WHERE pr_numbers LIKE ? {filter_sql}",
                    [f"%|{pr_num}|%", *base_params],
                ).fetchall()
                for r in direct_rows:
                    scored[r["uid"]] = (
                        1000.0 + (r["updated_at"] / 1e13),
                        r,
                        f"直接绑定 PR #{pr_num}",
                        "",
                    )

                mention_rows = self.conn.execute(
                    f"""
                    SELECT * FROM sessions
                    WHERE (search_text LIKE ? OR title LIKE ? OR branches_text LIKE ?) {filter_sql}
                    """,
                    [f"%{pr_num}%", f"%{pr_num}%", f"%{pr_num}%", *base_params],
                ).fetchall()
                for r in mention_rows:
                    if r["uid"] not in scored:
                        snip = self._extract_snippet(r["search_text"], [pr_num])
                        scored[r["uid"]] = (
                            650.0 + (r["updated_at"] / 1e13),
                            r,
                            f"会话提及 #{pr_num}",
                            snip,
                        )

                need_remote = resolve_remote_pr or (len(direct_rows) == 0)
                resolved_pr = self._resolve_pr_info(pr_num, repo_slug, allow_remote=need_remote)
                if resolved_pr:
                    pr_branch = str(resolved_pr.get("branch") or "").strip()
                    if pr_branch and pr_branch not in ("main", "master", "dev"):
                        b_rows = self.conn.execute(
                            f"SELECT * FROM sessions WHERE branches_text LIKE ? {filter_sql}",
                            [f"%{pr_branch}%", *base_params],
                        ).fetchall()
                        for r in b_rows:
                            if r["uid"] not in scored:
                                scored[r["uid"]] = (
                                    850.0 + (r["updated_at"] / 1e13),
                                    r,
                                    f"匹配 PR 分支 {pr_branch}",
                                    "",
                                )

                    generic_stems = {
                        "index", "route", "page", "page.client", "layout",
                        "types", "schema", "config", "utils", "helpers",
                        "constants", "readme", "claude", "agents", "package",
                    }
                    file_stems: list[str] = []
                    for fp in resolved_pr.get("files") or []:
                        stem = Path(fp).stem
                        if len(stem) >= 7 and stem.lower() not in generic_stems and stem not in file_stems:
                            file_stems.append(stem)
                    if file_stems and len(scored) < 5:
                        or_conds = " OR ".join(["search_text LIKE ?" for _ in file_stems[:6]])
                        f_rows = self.conn.execute(
                            f"SELECT * FROM sessions WHERE ({or_conds}) {filter_sql} LIMIT 15",
                            [*[f"%{st}%" for st in file_stems[:6]], *base_params],
                        ).fetchall()
                        for r in f_rows:
                            if r["uid"] not in scored:
                                hit_stems = [st for st in file_stems[:6] if st.lower() in r["search_text"].lower()]
                                snip = self._extract_snippet(r["search_text"], hit_stems or file_stems[:2])
                                scored[r["uid"]] = (
                                    520.0 + len(hit_stems) * 40.0 + (r["updated_at"] / 1e13),
                                    r,
                                    f"涉及 PR 改动文件 ({', '.join(hit_stems[:2])})",
                                    snip,
                                )

                    pr_title = str(resolved_pr.get("title") or "").strip()
                    key_tokens = [
                        tok
                        for tok in re.split(r"[\s:()/#,._-]+", pr_title)
                        if len(tok) >= 4 and tok.lower() not in {"feat", "chore", "refactor", "docs", "test", "with", "from", "that", "this"}
                    ][:4]
                    if key_tokens and len(scored) < 3:
                        like_conds = " AND ".join(["search_text LIKE ?" for _ in key_tokens])
                        t_rows = self.conn.execute(
                            f"SELECT * FROM sessions WHERE ({like_conds}) {filter_sql} LIMIT 15",
                            [*[f"%{tk}%" for tk in key_tokens], *base_params],
                        ).fetchall()
                        for r in t_rows:
                            if r["uid"] not in scored:
                                snip = self._extract_snippet(r["search_text"], key_tokens)
                                scored[r["uid"]] = (
                                    400.0 + (r["updated_at"] / 1e13),
                                    r,
                                    "匹配 PR 标题关键词",
                                    snip,
                                )

            terms = [t for t in re.split(r"\s+", q) if t]
            if terms and (not pr_num or len(scored) == 0):
                term_clauses: list[str] = []
                term_params: list[Any] = []
                for term in terms:
                    clean_t = term.lstrip("#") if term.startswith("#") and term[1:].isdigit() else term
                    pat = f"%{clean_t}%"
                    term_clauses.append(
                        "(title LIKE ? OR branches_text LIKE ? OR pr_numbers LIKE ? OR cwd LIKE ? OR search_text LIKE ?)"
                    )
                    term_params.extend([pat, pat, pat, pat, pat])

                sql = f"""
                    SELECT * FROM sessions
                    WHERE {' AND '.join(term_clauses)} {filter_sql}
                    ORDER BY updated_at DESC
                    LIMIT 150
                """
                kw_rows = self.conn.execute(sql, [*term_params, *base_params]).fetchall()
                for r in kw_rows:
                    if r["uid"] in scored:
                        continue
                    score = r["updated_at"] / 1e13
                    title_l = r["title"].lower()
                    branch_l = r["branches_text"].lower()
                    pr_l = r["pr_numbers"].lower()
                    prompt_l = (r["first_prompt"] + " " + r["summary"]).lower()
                    reasons: list[str] = []
                    for term in terms:
                        tl = term.lower()
                        if tl in pr_l:
                            score += 120
                            if "PR" not in reasons:
                                reasons.append("PR")
                        if tl in branch_l:
                            score += 90
                            if "分支" not in reasons:
                                reasons.append("分支")
                        if tl in title_l:
                            score += 70
                            if "标题" not in reasons:
                                reasons.append("标题")
                        if tl in prompt_l:
                            score += 35
                        score += 10
                    snip = self._extract_snippet(r["search_text"], terms)
                    reason_label = f"匹配{' / '.join(reasons)}" if reasons else "正文关键词命中"
                    scored[r["uid"]] = (score, r, reason_label, snip)

            ordered = sorted(scored.values(), key=lambda x: x[0], reverse=True)[:limit]
            return {
                "items": [
                    self._row_to_dict(r, match_reason=reason, matched_snippet=snip)
                    for _, r, reason, snip in ordered
                ],
                "resolved_pr": resolved_pr,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
