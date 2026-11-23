from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .git_pr_resolver import (
    parse_pr_query,
    resolve_pr_from_local_git,
    resolve_pr_via_gh,
)

logger = logging.getLogger("vibe_session_hub.search_engine")

PR_NEGATIVE_CACHE_TTL_SEC = 60.0

_GENERIC_FILE_STEMS = frozenset(
    {
        "index",
        "route",
        "page",
        "page.client",
        "layout",
        "types",
        "schema",
        "config",
        "utils",
        "helpers",
        "constants",
        "readme",
        "claude",
        "agents",
        "package",
    }
)

_PR_TITLE_STOPWORDS = frozenset(
    {
        "feat",
        "chore",
        "refactor",
        "docs",
        "test",
        "with",
        "from",
        "that",
        "this",
    }
)


def escape_fts_phrase(term: str) -> str:
    cleaned = term.replace("\x00", "").replace('"', '""')
    return f'"{cleaned}"'


def normalize_search_term(term: str) -> str:
    if term.startswith("#") and len(term) > 1 and term[1:].isdigit():
        return term[1:]
    return term


def extract_snippet(text: str, terms: list[str], radius: int = 90) -> str:
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


def row_to_dict(
    row: sqlite3.Row,
    local_machine_id: str,
    match_reason: str = "",
    matched_snippet: str = "",
) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "session_id": row["session_id"],
        "tool": row["tool"],
        "tool_label": row["tool_label"],
        "machine_id": row["machine_id"],
        "machine_name": row["machine_name"],
        "is_local": row["machine_id"] == local_machine_id,
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


class SearchEngine:
    """Lock-free WAL query engine with FTS5 trigram search, PR singleflight resolution, and facets."""

    def __init__(
        self,
        get_read_conn: Callable[[], sqlite3.Connection],
        write_conn: sqlite3.Connection,
        db_write_lock: threading.Lock,
        get_machine_info_fn: Callable[[], dict[str, Any]],
        resolve_local_git_fn: Callable[..., dict[str, Any] | None] | None = None,
        resolve_gh_fn: Callable[..., dict[str, Any] | None] | None = None,
    ) -> None:
        self._get_read_conn = get_read_conn
        self._write_conn = write_conn
        self._db_write_lock = db_write_lock
        self._get_machine_info = get_machine_info_fn
        self._resolve_local_git = resolve_local_git_fn or resolve_pr_from_local_git
        self._resolve_gh = resolve_gh_fn or resolve_pr_via_gh

        self._pr_inflight_guard = threading.Lock()
        self._pr_inflight_locks: dict[str, threading.Lock] = {}
        self._pr_negative_cache: dict[tuple[str, str, bool], float] = {}

    def get_session_by_uid(self, uid: str) -> dict[str, Any] | None:
        conn = self._get_read_conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE uid = ?", (uid,)
        ).fetchone()
        if not row:
            return None
        minfo = self._get_machine_info()
        return row_to_dict(row, local_machine_id=minfo["machine_id"])

    def get_facets(
        self, sync_root: str, health: dict[str, Any]
    ) -> dict[str, Any]:
        conn = self._get_read_conn()
        minfo = self._get_machine_info()
        tools = conn.execute(
            "SELECT tool, tool_label, count(*) AS cnt FROM sessions GROUP BY tool ORDER BY cnt DESC"
        ).fetchall()
        machines = conn.execute(
            "SELECT machine_id, MAX(machine_name) AS machine_name, count(*) AS cnt FROM sessions GROUP BY machine_id ORDER BY cnt DESC"
        ).fetchall()
        pr_count = conn.execute(
            "SELECT count(*) FROM sessions WHERE pr_numbers != ''"
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        return {
            "current_machine": minfo,
            "sync_root": sync_root,
            "icloud_status": health["icloud_status"],
            "icloud_access": health["icloud_access"],
            "sync_owner": health["sync_owner"],
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

    def _get_candidate_repos(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
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

    def _get_pr_inflight_lock(self, pr_num: str) -> threading.Lock:
        with self._pr_inflight_guard:
            lock = self._pr_inflight_locks.get(pr_num)
            if lock is None:
                lock = threading.Lock()
                self._pr_inflight_locks[pr_num] = lock
            return lock

    @staticmethod
    def _read_pr_cache(
        conn: sqlite3.Connection, pr_num: str
    ) -> dict[str, Any] | None:
        cached = conn.execute(
            "SELECT payload_json FROM pr_cache WHERE pr_number = ?", (pr_num,)
        ).fetchone()
        if not cached:
            return None
        try:
            data = json.loads(cached[0])
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _resolve_pr_info(
        self, pr_num: str, repo_slug: str | None, allow_remote: bool
    ) -> dict[str, Any] | None:
        conn = self._get_read_conn()
        cached_data = self._read_pr_cache(conn, pr_num)
        if cached_data and (cached_data.get("branch") or not allow_remote):
            return cached_data

        neg_key = (pr_num, repo_slug or "", allow_remote)
        now_mono = time.monotonic()
        with self._pr_inflight_guard:
            if now_mono < self._pr_negative_cache.get(neg_key, 0.0):
                return cached_data

        pr_lock = self._get_pr_inflight_lock(pr_num)
        with pr_lock:
            cached_data = self._read_pr_cache(conn, pr_num)
            if cached_data and (cached_data.get("branch") or not allow_remote):
                return cached_data

            now_mono = time.monotonic()
            with self._pr_inflight_guard:
                if now_mono < self._pr_negative_cache.get(neg_key, 0.0):
                    return cached_data

            repos = self._get_candidate_repos(conn)
            info = cached_data or self._resolve_local_git(
                pr_num, repos, total_budget_sec=2.5
            )
            if allow_remote and (not info or not info.get("branch")):
                gh_info = self._resolve_gh(pr_num, repo_slug, repos)
                if gh_info:
                    info = gh_info

            if info:
                with self._db_write_lock:
                    self._write_conn.execute(
                        """
                        INSERT INTO pr_cache (pr_number, payload_json, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(pr_number) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            pr_num,
                            json.dumps(info, ensure_ascii=False),
                            int(time.time() * 1000),
                        ),
                    )
                    self._write_conn.commit()
                if allow_remote and not info.get("branch"):
                    with self._pr_inflight_guard:
                        self._pr_negative_cache[neg_key] = (
                            time.monotonic() + PR_NEGATIVE_CACHE_TTL_SEC
                        )
            else:
                with self._pr_inflight_guard:
                    if len(self._pr_negative_cache) > 256:
                        cutoff = time.monotonic()
                        self._pr_negative_cache = {
                            k: v
                            for k, v in self._pr_negative_cache.items()
                            if v > cutoff
                        }
                    self._pr_negative_cache[neg_key] = (
                        time.monotonic() + PR_NEGATIVE_CACHE_TTL_SEC
                    )
            return info

    @staticmethod
    def _fts_search_rows(
        conn: sqlite3.Connection,
        fts_expr: str,
        filter_sql: str,
        base_params: list[Any],
        extra_where_sql: str = "",
        extra_params: list[Any] | None = None,
        limit: int = 150,
    ) -> list[sqlite3.Row]:
        sql = f"""
            SELECT s.*, bm25(sessions_fts, 8.0, 10.0, 12.0, 3.0, 1.0) AS fts_rank
            FROM sessions_fts
            JOIN sessions s ON s.rowid = sessions_fts.rowid
            WHERE sessions_fts MATCH ? {extra_where_sql} {filter_sql}
            ORDER BY fts_rank ASC, s.updated_at DESC
            LIMIT ?
        """
        params: list[Any] = [fts_expr, *(extra_params or []), *base_params, limit]
        return conn.execute(sql, params).fetchall()

    def _search_pr_signals(
        self,
        conn: sqlite3.Connection,
        pr_num: str,
        repo_slug: str | None,
        resolve_remote_pr: bool,
        filter_sql: str,
        base_params: list[Any],
        scored: dict[str, tuple[float, sqlite3.Row, str, str]],
    ) -> dict[str, Any] | None:
        # 1. Exact B-Tree lookup via session_prs
        direct_rows = conn.execute(
            f"""
            SELECT s.*
            FROM session_prs sp
            JOIN sessions s ON s.uid = sp.uid
            WHERE sp.pr_number = ? {filter_sql}
            ORDER BY s.updated_at DESC
            """,
            [pr_num, *base_params],
        ).fetchall()
        for r in direct_rows:
            scored[r["uid"]] = (
                1000.0 + (r["updated_at"] / 1e13),
                r,
                f"直接绑定 PR #{pr_num}",
                "",
            )

        # 2. PR mention lookup (FTS5 trigram for >=3 digits, LIKE fallback for 1-2 digits)
        mention_rows: list[sqlite3.Row] = []
        if len(pr_num) >= 3:
            try:
                mention_rows = self._fts_search_rows(
                    conn,
                    f"{{title branches_text search_text}} : {escape_fts_phrase(pr_num)}",
                    filter_sql,
                    base_params,
                    limit=80,
                )
            except sqlite3.OperationalError:
                mention_rows = []
        if not mention_rows and len(pr_num) < 3:
            mention_rows = conn.execute(
                f"""
                SELECT s.* FROM sessions s
                WHERE (s.search_text LIKE ? OR s.title LIKE ? OR s.branches_text LIKE ?) {filter_sql}
                ORDER BY s.updated_at DESC
                LIMIT 80
                """,
                [f"%{pr_num}%", f"%{pr_num}%", f"%{pr_num}%", *base_params],
            ).fetchall()

        for r in mention_rows:
            if r["uid"] not in scored:
                snip = extract_snippet(r["search_text"], [pr_num])
                scored[r["uid"]] = (
                    650.0 + (r["updated_at"] / 1e13),
                    r,
                    f"会话提及 #{pr_num}",
                    snip,
                )

        # 3. Lock-free PR resolution (local git + optional gh CLI)
        need_remote = resolve_remote_pr or (len(direct_rows) == 0)
        resolved_pr = self._resolve_pr_info(pr_num, repo_slug, allow_remote=need_remote)
        if not resolved_pr:
            return None

        self._apply_resolved_pr_matches(
            conn, resolved_pr, filter_sql, base_params, scored
        )
        return resolved_pr

    def _apply_resolved_pr_matches(
        self,
        conn: sqlite3.Connection,
        resolved_pr: dict[str, Any],
        filter_sql: str,
        base_params: list[Any],
        scored: dict[str, tuple[float, sqlite3.Row, str, str]],
    ) -> None:
        pr_branch = str(resolved_pr.get("branch") or "").strip()
        if pr_branch and pr_branch not in ("main", "master", "dev"):
            if len(pr_branch) >= 3:
                try:
                    b_rows = self._fts_search_rows(
                        conn,
                        f"branches_text : {escape_fts_phrase(pr_branch)}",
                        filter_sql,
                        base_params,
                        limit=40,
                    )
                except sqlite3.OperationalError:
                    b_rows = conn.execute(
                        f"SELECT s.* FROM sessions s WHERE s.branches_text LIKE ? {filter_sql} LIMIT 40",
                        [f"%{pr_branch}%", *base_params],
                    ).fetchall()
            else:
                b_rows = conn.execute(
                    f"SELECT s.* FROM sessions s WHERE s.branches_text LIKE ? {filter_sql} LIMIT 40",
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

        file_stems: list[str] = []
        for fp in resolved_pr.get("files") or []:
            stem = Path(fp).stem
            if (
                len(stem) >= 7
                and stem.lower() not in _GENERIC_FILE_STEMS
                and stem not in file_stems
            ):
                file_stems.append(stem)
        if file_stems and len(scored) < 5:
            fts_files_expr = " OR ".join(
                f"search_text : {escape_fts_phrase(st)}" for st in file_stems[:6]
            )
            try:
                f_rows = self._fts_search_rows(
                    conn, fts_files_expr, filter_sql, base_params, limit=15
                )
            except sqlite3.OperationalError:
                or_conds = " OR ".join(["s.search_text LIKE ?" for _ in file_stems[:6]])
                f_rows = conn.execute(
                    f"SELECT s.* FROM sessions s WHERE ({or_conds}) {filter_sql} LIMIT 15",
                    [*[f"%{st}%" for st in file_stems[:6]], *base_params],
                ).fetchall()
            for r in f_rows:
                if r["uid"] not in scored:
                    hit_stems = [
                        st
                        for st in file_stems[:6]
                        if st.lower() in r["search_text"].lower()
                    ]
                    snip = extract_snippet(
                        r["search_text"], hit_stems or file_stems[:2]
                    )
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
            if len(tok) >= 4 and tok.lower() not in _PR_TITLE_STOPWORDS
        ][:4]
        if key_tokens and len(scored) < 3:
            fts_title_expr = " AND ".join(
                f"search_text : {escape_fts_phrase(tk)}" for tk in key_tokens
            )
            try:
                t_rows = self._fts_search_rows(
                    conn, fts_title_expr, filter_sql, base_params, limit=15
                )
            except sqlite3.OperationalError:
                like_conds = " AND ".join(["s.search_text LIKE ?" for _ in key_tokens])
                t_rows = conn.execute(
                    f"SELECT s.* FROM sessions s WHERE ({like_conds}) {filter_sql} LIMIT 15",
                    [*[f"%{tk}%" for tk in key_tokens], *base_params],
                ).fetchall()
            for r in t_rows:
                if r["uid"] not in scored:
                    snip = extract_snippet(r["search_text"], key_tokens)
                    scored[r["uid"]] = (
                        400.0 + (r["updated_at"] / 1e13),
                        r,
                        "匹配 PR 标题关键词",
                        snip,
                    )

    def _search_keywords(
        self,
        conn: sqlite3.Connection,
        clean_terms: list[str],
        filter_sql: str,
        base_params: list[Any],
        scored: dict[str, tuple[float, sqlite3.Row, str, str]],
    ) -> None:
        fts_terms = [t for t in clean_terms if len(t) >= 3]
        short_terms = [t for t in clean_terms if len(t) < 3]

        kw_rows: list[sqlite3.Row] = []
        used_fts = False

        if fts_terms:
            fts_expr = " AND ".join(escape_fts_phrase(t) for t in fts_terms)
            short_where_parts: list[str] = []
            short_params: list[Any] = []
            for st in short_terms:
                pat = f"%{st}%"
                short_where_parts.append(
                    "(s.title LIKE ? OR s.branches_text LIKE ? OR s.pr_search_text LIKE ? OR s.cwd LIKE ? OR s.search_text LIKE ?)"
                )
                short_params.extend([pat, pat, pat, pat, pat])
            extra_where = (
                (" AND " + " AND ".join(short_where_parts))
                if short_where_parts
                else ""
            )
            try:
                kw_rows = self._fts_search_rows(
                    conn,
                    fts_expr,
                    filter_sql,
                    base_params,
                    extra_where_sql=extra_where,
                    extra_params=short_params,
                    limit=150,
                )
                used_fts = True
            except sqlite3.OperationalError:
                used_fts = False

        if not used_fts:
            term_clauses: list[str] = []
            term_params: list[Any] = []
            for clean_t in clean_terms:
                pat = f"%{clean_t}%"
                term_clauses.append(
                    "(s.title LIKE ? OR s.branches_text LIKE ? OR s.pr_search_text LIKE ? OR s.cwd LIKE ? OR s.search_text LIKE ?)"
                )
                term_params.extend([pat, pat, pat, pat, pat])
            sql = f"""
                SELECT s.* FROM sessions s
                WHERE {" AND ".join(term_clauses)} {filter_sql}
                ORDER BY s.updated_at DESC
                LIMIT 150
            """
            kw_rows = conn.execute(sql, [*term_params, *base_params]).fetchall()

        for r in kw_rows:
            if r["uid"] in scored:
                continue
            score = r["updated_at"] / 1e13
            if used_fts:
                try:
                    bm25_val = float(r["fts_rank"])
                    score += min(80.0, max(0.0, -bm25_val * 5.0))
                except (KeyError, IndexError, TypeError, ValueError):
                    pass

            title_l = r["title"].lower()
            branch_l = r["branches_text"].lower()
            pr_l = (r["pr_search_text"] + " " + r["pr_numbers"]).lower()
            prompt_l = (r["first_prompt"] + " " + r["summary"]).lower()
            reasons: list[str] = []
            for term in clean_terms:
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
            snip = extract_snippet(r["search_text"], clean_terms)
            reason_label = (
                f"匹配{' / '.join(reasons)}" if reasons else "正文关键词命中"
            )
            scored[r["uid"]] = (score, r, reason_label, snip)

    def search(
        self,
        query: str = "",
        tool: str = "",
        machine_id: str = "",
        only_with_pr: bool = False,
        resolve_remote_pr: bool = False,
        limit: int = 80,
    ) -> dict[str, Any]:
        t0 = time.time()
        conn = self._get_read_conn()
        local_mid = self._get_machine_info()["machine_id"]

        where_clauses: list[str] = []
        base_params: list[Any] = []
        if tool:
            where_clauses.append("s.tool = ?")
            base_params.append(tool)
        if machine_id:
            where_clauses.append("s.machine_id = ?")
            base_params.append(machine_id)
        if only_with_pr:
            where_clauses.append("s.pr_numbers != ''")

        filter_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
        q = query.strip()

        if not q:
            rows = conn.execute(
                f"SELECT s.* FROM sessions s WHERE 1=1 {filter_sql} ORDER BY s.updated_at DESC LIMIT ?",
                [*base_params, limit],
            ).fetchall()
            return {
                "items": [row_to_dict(r, local_machine_id=local_mid) for r in rows],
                "resolved_pr": None,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        pr_num, repo_slug = parse_pr_query(q)
        scored: dict[str, tuple[float, sqlite3.Row, str, str]] = {}
        resolved_pr: dict[str, Any] | None = None

        if pr_num:
            resolved_pr = self._search_pr_signals(
                conn,
                pr_num,
                repo_slug,
                resolve_remote_pr,
                filter_sql,
                base_params,
                scored,
            )

        raw_terms = [t for t in re.split(r"\s+", q) if t]
        clean_terms = [normalize_search_term(t) for t in raw_terms]
        clean_terms = [t for t in clean_terms if t]

        if clean_terms and (not pr_num or len(scored) == 0):
            self._search_keywords(conn, clean_terms, filter_sql, base_params, scored)

        ordered = sorted(scored.values(), key=lambda x: x[0], reverse=True)[:limit]
        return {
            "items": [
                row_to_dict(
                    r,
                    local_machine_id=local_mid,
                    match_reason=reason,
                    matched_snippet=snip,
                )
                for _, r, reason, snip in ordered
            ],
            "resolved_pr": resolved_pr,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
