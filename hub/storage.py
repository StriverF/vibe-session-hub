from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .collectors import FileParseCache, collect_all_local_sessions
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

SCHEMA_VERSION = "2"
STANDALONE_SHARD_KEY = "__manual_links__"
PR_NEGATIVE_CACHE_TTL_SEC = 60.0

_SESSIONS_COLUMNS = (
    "uid",
    "session_id",
    "tool",
    "tool_label",
    "machine_id",
    "machine_name",
    "title",
    "first_prompt",
    "summary",
    "cwd",
    "branches_json",
    "branches_text",
    "prs_json",
    "pr_numbers",
    "pr_search_text",
    "created_at",
    "updated_at",
    "is_archived",
    "deep_link",
    "external_url",
    "search_text",
    "source_shard",
    "record_hash",
)

_SESSIONS_INSERT_SQL = f"""
    INSERT INTO sessions ({", ".join(_SESSIONS_COLUMNS)})
    VALUES ({", ".join("?" for _ in _SESSIONS_COLUMNS)})
    ON CONFLICT(uid) DO UPDATE SET
        {", ".join(f"{c} = excluded.{c}" for c in _SESSIONS_COLUMNS if c != "uid")}
"""

_SESSIONS_UPDATE_SQL = f"""
    UPDATE sessions SET
        {", ".join(f"{c} = ?" for c in _SESSIONS_COLUMNS if c != "uid")}
    WHERE uid = ?
"""


def _configure_sqlite_conn(conn: sqlite3.Connection, readonly: bool = False) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def _try_sync_file_lock(lock_path: Path) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _trigger_icloud_downloads(folder: Path, raise_on_os_error: bool = False) -> None:
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
        if raise_on_os_error:
            raise
        return


def _escape_fts_phrase(term: str) -> str:
    cleaned = term.replace("\x00", "").replace('"', '""')
    return f'"{cleaned}"'


def _normalize_search_term(term: str) -> str:
    if term.startswith("#") and len(term) > 1 and term[1:].isdigit():
        return term[1:]
    return term


class SessionHubStore:
    def __init__(self, local_only: bool = False, db_path: Path | None = None) -> None:
        self.local_only = local_only
        self.db_path = db_path or INDEX_DB_PATH
        self.icloud_access = "disabled" if not is_icloud_enabled(local_only) else "init"
        self.icloud_status = "local_only" if not is_icloud_enabled(local_only) else "init"

        ensure_dirs(local_only=local_only)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Short-lived lock protecting SQLite write transactions only.
        self._db_write_lock = threading.Lock()
        # Serializes sync/ingestion pipelines without ever blocking HTTP readers.
        self._sync_lock = threading.Lock()
        # Per-PR singleflight & short negative cache for external git/gh lookups.
        self._pr_inflight_guard = threading.Lock()
        self._pr_inflight_locks: dict[str, threading.Lock] = {}
        self._pr_negative_cache: dict[tuple[str, str, bool], float] = {}

        self._read_local = threading.local()
        self._read_conns_lock = threading.Lock()
        self._read_conns: list[sqlite3.Connection] = []

        self._last_export_digest = ""
        self._last_local_scan_ms = 0

        self._write_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        _configure_sqlite_conn(self._write_conn, readonly=False)
        self._init_schema()

    def _get_read_conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._read_local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        _configure_sqlite_conn(conn, readonly=True)
        self._read_local.conn = conn
        with self._read_conns_lock:
            self._read_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._read_conns_lock:
            for c in self._read_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._read_conns.clear()
        self._read_local = threading.local()
        with self._db_write_lock:
            try:
                self._write_conn.close()
            except Exception:
                pass

    def _init_schema(self) -> None:
        with self._db_write_lock:
            conn = self._write_conn
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pr_cache (
                    pr_number TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            FileParseCache.ensure_table(conn)

            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_ver = str(row["value"]) if row else ""

            if current_ver != SCHEMA_VERSION:
                conn.executescript(
                    """
                    DROP TRIGGER IF EXISTS sessions_ai;
                    DROP TRIGGER IF EXISTS sessions_ad;
                    DROP TRIGGER IF EXISTS sessions_au;
                    DROP TABLE IF EXISTS sessions_fts;
                    DROP TABLE IF EXISTS session_prs;
                    DROP TABLE IF EXISTS shard_state;
                    DROP TABLE IF EXISTS manual_link_targets;
                    DROP TABLE IF EXISTS sessions;
                    """
                )

            conn.executescript(
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
                    pr_search_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    is_archived INTEGER NOT NULL,
                    deep_link TEXT NOT NULL,
                    external_url TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    source_shard TEXT NOT NULL DEFAULT '',
                    record_hash TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool);
                CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_source_shard ON sessions(source_shard);

                CREATE TABLE IF NOT EXISTS session_prs (
                    pr_number TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    PRIMARY KEY (pr_number, uid)
                );
                CREATE INDEX IF NOT EXISTS idx_session_prs_uid ON session_prs(uid);

                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                    title,
                    branches_text,
                    pr_search_text,
                    cwd,
                    search_text,
                    content='sessions',
                    content_rowid='rowid',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
                    INSERT INTO sessions_fts(rowid, title, branches_text, pr_search_text, cwd, search_text)
                    VALUES (new.rowid, new.title, new.branches_text, new.pr_search_text, new.cwd, new.search_text);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
                    INSERT INTO sessions_fts(sessions_fts, rowid, title, branches_text, pr_search_text, cwd, search_text)
                    VALUES ('delete', old.rowid, old.title, old.branches_text, old.pr_search_text, old.cwd, old.search_text);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
                    INSERT INTO sessions_fts(sessions_fts, rowid, title, branches_text, pr_search_text, cwd, search_text)
                    VALUES ('delete', old.rowid, old.title, old.branches_text, old.pr_search_text, old.cwd, old.search_text);
                    INSERT INTO sessions_fts(rowid, title, branches_text, pr_search_text, cwd, search_text)
                    VALUES (new.rowid, new.title, new.branches_text, new.pr_search_text, new.cwd, new.search_text);
                END;

                CREATE TABLE IF NOT EXISTS shard_state (
                    shard_key TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_link_targets (
                    uid TEXT PRIMARY KEY,
                    source_shard TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SCHEMA_VERSION,),
            )
            conn.commit()

    def _probe_icloud_access(self) -> str:
        if not is_icloud_enabled(self.local_only):
            self.icloud_access = "disabled"
            self.icloud_status = "local_only"
            return self.icloud_access
        try:
            (ICLOUD_SYNC_ROOT / "nodes").mkdir(parents=True, exist_ok=True)
            (ICLOUD_SYNC_ROOT / "manual_links").mkdir(parents=True, exist_ok=True)
            list((ICLOUD_SYNC_ROOT / "nodes").iterdir())
            self.icloud_access = "direct"
        except OSError:
            self.icloud_access = "permission_denied"
            if self.icloud_status != "synced_via_app":
                self.icloud_status = "local_fallback"
        return self.icloud_access

    def get_sync_health(self) -> dict[str, Any]:
        if self.icloud_access == "init":
            self._probe_icloud_access()
        if self.local_only or self.icloud_access == "disabled":
            sync_owner = "none"
        elif self.icloud_access == "permission_denied":
            sync_owner = "app_bridge"
        else:
            sync_owner = "python"
        return {
            "local_only": self.local_only,
            "icloud_access": self.icloud_access,
            "icloud_status": self.icloud_status,
            "sync_owner": sync_owner,
        }

    def sync_now(
        self,
        force_local: bool = False,
        force_reindex: bool = False,
        skip_local: bool = False,
        skip_icloud: bool = False,
        bridge_synced: bool = False,
    ) -> dict[str, Any]:
        with self._sync_lock:
            t0 = time.time()
            ensure_dirs(local_only=self.local_only or skip_icloud)
            minfo = get_machine_info()
            mid = minfo["machine_id"]
            mname = minfo["machine_name"]

            dirty_shards: set[str] = set()
            now_ms = int(t0 * 1000)

            # Phase 1: Collect local sessions completely outside _db_write_lock
            if not skip_local and (force_local or (now_ms - self._last_local_scan_ms > 8_000)):
                initial_cache = FileParseCache.load_entries(self._get_read_conn())
                parse_cache = FileParseCache(initial_entries=initial_cache)
                local_sessions = collect_all_local_sessions(mid, mname, cache=parse_cache)
                if parse_cache.dirty_rows:
                    with self._db_write_lock:
                        parse_cache.flush_to_conn(self._write_conn)
                        self._write_conn.commit()
                if self._export_local_shard(mid, mname, local_sessions):
                    dirty_shards.add(f"node:{mid}.jsonl")
                self._last_local_scan_ms = now_ms

            # Phase 2: iCloud mirror completely outside DB locks
            if bridge_synced and is_icloud_enabled(self.local_only):
                self.icloud_status = "synced_via_app"
            if not skip_icloud:
                self._mirror_with_icloud(mid)

            # Phase 3 & 4: Incremental shard ingest (file parse lock-free, short commit under _db_write_lock)
            reloaded = self._ingest_sync_shards(
                force_reindex=force_reindex,
                dirty_shards=dirty_shards,
            )

            elapsed_ms = int((time.time() - t0) * 1000)
            total = self._get_read_conn().execute(
                "SELECT count(*) FROM sessions"
            ).fetchone()[0]
            active_sync_root = (
                str(ICLOUD_SYNC_ROOT)
                if is_icloud_enabled(self.local_only)
                else str(LOCAL_SYNC_ROOT)
            )
            health = self.get_sync_health()
            return {
                "ok": True,
                "elapsed_ms": elapsed_ms,
                "reloaded_shards": reloaded,
                "total_sessions": total,
                "sync_root": active_sync_root,
                "icloud_status": health["icloud_status"],
                "icloud_access": health["icloud_access"],
                "sync_owner": health["sync_owner"],
                "machine": minfo,
            }

    def _export_local_shard(
        self, machine_id: str, machine_name: str, sessions: list[dict[str, Any]]
    ) -> bool:
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

        # Hash content excluding exported_at so identical session payloads don't churn disk or iCloud.
        payload_digest = hashlib.sha256(
            ("\n".join([machine_id, machine_name, *lines[1:]])).encode("utf-8")
        ).hexdigest()
        shard_path = NODES_DIR / f"{machine_id}.jsonl"
        if not self._last_export_digest and shard_path.is_file():
            try:
                existing_lines = shard_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
                if existing_lines:
                    meta = json.loads(existing_lines[0])
                    if (
                        isinstance(meta, dict)
                        and meta.get("_meta")
                        and meta.get("machine_id") == machine_id
                        and meta.get("machine_name") == machine_name
                    ):
                        self._last_export_digest = hashlib.sha256(
                            (
                                "\n".join(
                                    [machine_id, machine_name, *existing_lines[1:]]
                                )
                            ).encode("utf-8")
                        ).hexdigest()
            except Exception:
                pass
        if payload_digest == self._last_export_digest and shard_path.exists():
            return False

        blob = ("\n".join(lines) + "\n").encode("utf-8")
        _atomic_write_bytes(shard_path, blob)
        self._last_export_digest = payload_digest
        return True

    def _mirror_with_icloud(self, machine_id: str) -> bool:
        if not is_icloud_enabled(self.local_only):
            self.icloud_access = "disabled"
            self.icloud_status = "local_only"
            return False

        lock_path = LOCAL_SYNC_ROOT / ".sync.lock"
        with _try_sync_file_lock(lock_path) as acquired:
            if not acquired:
                return False

            icloud_nodes = ICLOUD_SYNC_ROOT / "nodes"
            icloud_links = ICLOUD_SYNC_ROOT / "manual_links"
            changed = False
            try:
                icloud_nodes.mkdir(parents=True, exist_ok=True)
                icloud_links.mkdir(parents=True, exist_ok=True)
                _trigger_icloud_downloads(icloud_nodes, raise_on_os_error=True)
                _trigger_icloud_downloads(icloud_links, raise_on_os_error=True)

                own_node_name = f"{machine_id}.jsonl"
                own_links_name = f"{machine_id}-links.jsonl"

                # PUSH strictly local machine's own node shard (content-hashed + atomic write)
                local_shard = NODES_DIR / own_node_name
                cloud_shard = icloud_nodes / own_node_name
                if local_shard.is_file():
                    local_bytes = local_shard.read_bytes()
                    if _sha256_bytes(local_bytes) != _file_sha256(cloud_shard):
                        _atomic_write_bytes(cloud_shard, local_bytes)
                        changed = True

                # PULL strictly remote machines' node shards
                for remote_file in sorted(icloud_nodes.glob("*.jsonl")):
                    if remote_file.name.startswith(".") or remote_file.name == own_node_name:
                        continue
                    dest = NODES_DIR / remote_file.name
                    remote_bytes = remote_file.read_bytes()
                    if _sha256_bytes(remote_bytes) != _file_sha256(dest):
                        _atomic_write_bytes(dest, remote_bytes)
                        changed = True

                # PUSH strictly local machine's own manual links (content-hashed + atomic write)
                local_links = MANUAL_LINKS_DIR / own_links_name
                cloud_links = icloud_links / own_links_name
                if local_links.is_file():
                    local_link_bytes = local_links.read_bytes()
                    if _sha256_bytes(local_link_bytes) != _file_sha256(cloud_links):
                        _atomic_write_bytes(cloud_links, local_link_bytes)
                        changed = True

                # PULL strictly remote machines' manual links
                for rlink in sorted(icloud_links.glob("*.jsonl")):
                    if rlink.name.startswith(".") or rlink.name == own_links_name:
                        continue
                    dest_link = MANUAL_LINKS_DIR / rlink.name
                    rlink_bytes = rlink.read_bytes()
                    if _sha256_bytes(rlink_bytes) != _file_sha256(dest_link):
                        _atomic_write_bytes(dest_link, rlink_bytes)
                        changed = True

                self.icloud_access = "direct"
                self.icloud_status = "synced"
                return changed
            except OSError:
                self.icloud_access = "permission_denied"
                if self.icloud_status != "synced_via_app":
                    self.icloud_status = "local_fallback"
                return False

    def _load_manual_links(
        self,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        attachments: dict[str, list[dict[str, Any]]] = {}
        standalones: list[dict[str, Any]] = []
        if not MANUAL_LINKS_DIR.exists():
            return attachments, standalones

        seen_ids: set[str] = set()
        for fp in sorted(MANUAL_LINKS_DIR.glob("*.jsonl")):
            if fp.name.startswith("."):
                continue
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

    @staticmethod
    def _parse_node_shard(shard_path: Path) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        shard_machine_name = ""
        try:
            with shard_path.open("r", encoding="utf-8", errors="ignore") as f:
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
                    records[uid] = obj
        except Exception:
            return {}
        return records

    @staticmethod
    def _apply_attachments(
        rec: dict[str, Any], extra_list: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not extra_list:
            return rec
        rec = dict(rec)
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
        return rec

    @staticmethod
    def _build_standalone_records(
        standalones: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
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
            prs = (
                [{"number": pr_num, "repo": "", "url": pr_url, "branch": ""}]
                if pr_num
                else []
            )
            branch = str(item.get("branch") or "").strip()
            title = str(
                item.get("title") or item.get("note") or item.get("external_url") or lid
            )
            note = str(item.get("note") or "")
            ts = int(item.get("created_at") or int(time.time() * 1000))
            uid = f"{mid}:{tool}:{lid}"
            records[uid] = {
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
        return records

    @staticmethod
    def _prepare_row(
        rec: dict[str, Any], source_shard: str
    ) -> tuple[tuple[Any, ...], str, list[str]]:
        branches = [str(b) for b in (rec.get("branches") or []) if b]
        prs = [p for p in (rec.get("prs") or []) if isinstance(p, dict)]
        pr_nums_unique: list[str] = []
        seen_pr: set[str] = set()
        pr_search_parts: list[str] = []
        for p in prs:
            n = str(p.get("number") or "").strip()
            if n:
                if n not in seen_pr:
                    seen_pr.add(n)
                    pr_nums_unique.append(n)
                url = str(p.get("url") or "").strip()
                pr_search_parts.append(f"#{n} {n} {url}".strip())

        pr_pipe = ("|" + "|".join(pr_nums_unique) + "|") if pr_nums_unique else ""
        pr_search_text = " ".join(pr_search_parts)
        branches_text = " ".join(branches)
        branches_json = json.dumps(branches, ensure_ascii=False)
        prs_json = json.dumps(prs, ensure_ascii=False)

        core_values = (
            str(rec["uid"]),
            str(rec.get("session_id") or ""),
            str(rec.get("tool") or ""),
            str(rec.get("tool_label") or ""),
            str(rec.get("machine_id") or ""),
            str(rec.get("machine_name") or ""),
            str(rec.get("title") or ""),
            str(rec.get("first_prompt") or ""),
            str(rec.get("summary") or ""),
            str(rec.get("cwd") or ""),
            branches_json,
            branches_text,
            prs_json,
            pr_pipe,
            pr_search_text,
            int(rec.get("created_at") or 0),
            int(rec.get("updated_at") or 0),
            1 if rec.get("is_archived") else 0,
            str(rec.get("deep_link") or ""),
            str(rec.get("external_url") or ""),
            str(rec.get("search_text") or ""),
            source_shard,
        )
        record_hash = hashlib.sha256(
            json.dumps(core_values, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        full_row = (*core_values, record_hash)
        return full_row, record_hash, pr_nums_unique

    def _scan_disk_shards(self) -> tuple[dict[str, tuple[int, int]], dict[str, Path]]:
        curr_state: dict[str, tuple[int, int]] = {}
        node_paths: dict[str, Path] = {}
        if NODES_DIR.exists():
            for p in sorted(NODES_DIR.glob("*.jsonl")):
                if p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                    key = f"node:{p.name}"
                    curr_state[key] = (st.st_mtime_ns, st.st_size)
                    node_paths[key] = p
                except OSError:
                    continue
        if MANUAL_LINKS_DIR.exists():
            for p in sorted(MANUAL_LINKS_DIR.glob("*.jsonl")):
                if p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                    curr_state[f"link:{p.name}"] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
        return curr_state, node_paths

    def _ingest_sync_shards(
        self,
        force_reindex: bool = False,
        dirty_shards: set[str] | None = None,
    ) -> bool:
        read_conn = self._get_read_conn()
        prev_state: dict[str, tuple[int, int]] = {
            str(r["shard_key"]): (int(r["mtime_ns"]), int(r["size"]))
            for r in read_conn.execute(
                "SELECT shard_key, mtime_ns, size FROM shard_state"
            ).fetchall()
        }
        curr_state, node_paths = self._scan_disk_shards()
        dirty = set(dirty_shards or set())

        if not force_reindex and not dirty and curr_state == prev_state:
            return False

        # Identify changed / removed node shards and whether manual links changed.
        curr_node_keys = {k for k in curr_state if k.startswith("node:")}
        prev_node_keys = {k for k in prev_state if k.startswith("node:")}
        removed_node_keys = prev_node_keys - curr_node_keys

        changed_node_keys: set[str] = set()
        for k in curr_node_keys:
            if force_reindex or k in dirty or prev_state.get(k) != curr_state.get(k):
                changed_node_keys.add(k)

        link_keys_union = {
            k for k in (set(prev_state) | set(curr_state)) if k.startswith("link:")
        }
        links_changed = (
            force_reindex
            or any(k in dirty for k in link_keys_union)
            or any(prev_state.get(k) != curr_state.get(k) for k in link_keys_union)
        )

        # Load manual links outside _db_write_lock
        attachments, standalones = self._load_manual_links()

        if links_changed:
            prev_attached_rows = read_conn.execute(
                "SELECT uid, source_shard FROM manual_link_targets"
            ).fetchall()
            affected_shards: set[str] = {
                str(r["source_shard"])
                for r in prev_attached_rows
                if str(r["source_shard"]) in curr_node_keys
            }
            if attachments:
                target_uids = list(attachments.keys())
                # Look up existing source_shard for target_uids
                placeholders = ",".join("?" for _ in target_uids)
                rows = read_conn.execute(
                    f"SELECT uid, source_shard FROM sessions WHERE uid IN ({placeholders})",
                    target_uids,
                ).fetchall()
                for r in rows:
                    ss = str(r["source_shard"])
                    if ss in curr_node_keys:
                        affected_shards.add(ss)
                # Also infer shard name from uid prefix (`{machine_id}:{tool}:{id}`)
                for tuid in target_uids:
                    if ":" in tuid:
                        inferred = f"node:{tuid.split(':', 1)[0]}.jsonl"
                        if inferred in curr_node_keys:
                            affected_shards.add(inferred)
            changed_node_keys.update(affected_shards)

        # Parse ONLY changed shards outside _db_write_lock
        prepared_by_shard: dict[
            str, dict[str, tuple[tuple[Any, ...], str, list[str]]]
        ] = {}
        new_attached_targets: dict[str, str] = {}

        for shard_key in sorted(changed_node_keys):
            shard_path = node_paths.get(shard_key)
            if not shard_path or not shard_path.is_file():
                continue
            raw_records = self._parse_node_shard(shard_path)
            shard_prepared: dict[str, tuple[tuple[Any, ...], str, list[str]]] = {}
            for uid, rec in raw_records.items():
                extra = attachments.get(uid)
                if extra:
                    rec = self._apply_attachments(rec, extra)
                    new_attached_targets[uid] = shard_key
                shard_prepared[uid] = self._prepare_row(rec, shard_key)
            prepared_by_shard[shard_key] = shard_prepared

        if links_changed:
            standalone_recs = self._build_standalone_records(standalones)
            prepared_by_shard[STANDALONE_SHARD_KEY] = {
                uid: self._prepare_row(rec, STANDALONE_SHARD_KEY)
                for uid, rec in standalone_recs.items()
            }

        # Phase 4: Short atomic DB write transaction (millisecond-level lock)
        with self._db_write_lock:
            conn = self._write_conn

            if force_reindex:
                existing_shards = {
                    str(r["source_shard"])
                    for r in conn.execute(
                        "SELECT DISTINCT source_shard FROM sessions"
                    ).fetchall()
                }
                for orphan in existing_shards - curr_node_keys - {STANDALONE_SHARD_KEY}:
                    removed_node_keys.add(orphan)

            for rem_key in removed_node_keys:
                conn.execute(
                    "DELETE FROM session_prs WHERE uid IN (SELECT uid FROM sessions WHERE source_shard = ?)",
                    (rem_key,),
                )
                conn.execute("DELETE FROM sessions WHERE source_shard = ?", (rem_key,))
                conn.execute("DELETE FROM shard_state WHERE shard_key = ?", (rem_key,))
                conn.execute(
                    "DELETE FROM manual_link_targets WHERE source_shard = ?", (rem_key,)
                )

            for shard_key, prepared in prepared_by_shard.items():
                existing_hashes: dict[str, str] = {
                    str(r["uid"]): str(r["record_hash"])
                    for r in conn.execute(
                        "SELECT uid, record_hash FROM sessions WHERE source_shard = ?",
                        (shard_key,),
                    ).fetchall()
                }
                incoming_uids = set(prepared.keys())
                existing_uids = set(existing_hashes.keys())

                # 1. Delete removed sessions for this shard
                to_delete = list(existing_uids - incoming_uids)
                if to_delete:
                    conn.executemany(
                        "DELETE FROM session_prs WHERE uid = ?",
                        [(u,) for u in to_delete],
                    )
                    conn.executemany(
                        "DELETE FROM sessions WHERE uid = ?",
                        [(u,) for u in to_delete],
                    )

                # 2. Insert new or update changed sessions; skip identical records
                to_insert: list[tuple[Any, ...]] = []
                to_update: list[tuple[Any, ...]] = []
                prs_to_clear: list[tuple[str]] = []
                prs_to_insert: list[tuple[str, str]] = []

                for uid, (row_vals, rec_hash, pr_list) in prepared.items():
                    old_hash = existing_hashes.get(uid)
                    if old_hash is None:
                        to_insert.append(row_vals)
                        prs_to_clear.append((uid,))
                        for pn in pr_list:
                            prs_to_insert.append((pn, uid))
                    elif old_hash != rec_hash:
                        # UPDATE values: all columns except uid, then uid for WHERE clause
                        to_update.append((*row_vals[1:], uid))
                        prs_to_clear.append((uid,))
                        for pn in pr_list:
                            prs_to_insert.append((pn, uid))

                if to_insert:
                    conn.executemany(_SESSIONS_INSERT_SQL, to_insert)
                if to_update:
                    conn.executemany(_SESSIONS_UPDATE_SQL, to_update)
                if prs_to_clear:
                    conn.executemany(
                        "DELETE FROM session_prs WHERE uid = ?", prs_to_clear
                    )
                if prs_to_insert:
                    conn.executemany(
                        "INSERT OR IGNORE INTO session_prs (pr_number, uid) VALUES (?, ?)",
                        prs_to_insert,
                    )

                if shard_key.startswith("node:"):
                    conn.execute(
                        "DELETE FROM manual_link_targets WHERE source_shard = ?",
                        (shard_key,),
                    )
                    shard_targets = [
                        (u, s)
                        for u, s in new_attached_targets.items()
                        if s == shard_key
                    ]
                    if shard_targets:
                        conn.executemany(
                            """
                            INSERT INTO manual_link_targets (uid, source_shard)
                            VALUES (?, ?)
                            ON CONFLICT(uid) DO UPDATE SET source_shard = excluded.source_shard
                            """,
                            shard_targets,
                        )

            # Update persisted shard_state
            removed_state_keys = set(prev_state.keys()) - set(curr_state.keys())
            if removed_state_keys:
                conn.executemany(
                    "DELETE FROM shard_state WHERE shard_key = ?",
                    [(k,) for k in removed_state_keys],
                )
            if curr_state:
                conn.executemany(
                    """
                    INSERT INTO shard_state (shard_key, mtime_ns, size)
                    VALUES (?, ?, ?)
                    ON CONFLICT(shard_key) DO UPDATE SET
                        mtime_ns = excluded.mtime_ns,
                        size = excluded.size
                    """,
                    [(k, v[0], v[1]) for k, v in curr_state.items()],
                )

            conn.commit()
        return True

    def add_manual_link(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._sync_lock:
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
            MANUAL_LINKS_DIR.mkdir(parents=True, exist_ok=True)
            with link_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._mirror_with_icloud(mid)
            self._ingest_sync_shards(dirty_shards={f"link:{link_file.name}"})
            return entry

    def get_session_by_uid(self, uid: str) -> dict[str, Any] | None:
        conn = self._get_read_conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE uid = ?", (uid,)
        ).fetchone()
        if not row:
            return None
        minfo = get_machine_info()
        return self._row_to_dict(row, local_machine_id=minfo["machine_id"])

    def get_facets(self) -> dict[str, Any]:
        conn = self._get_read_conn()
        minfo = get_machine_info()
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
        health = self.get_sync_health()
        return {
            "current_machine": minfo,
            "sync_root": str(ICLOUD_SYNC_ROOT)
            if is_icloud_enabled(self.local_only)
            else str(LOCAL_SYNC_ROOT),
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

    def _read_pr_cache(
        self, conn: sqlite3.Connection, pr_num: str
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
            exp = self._pr_negative_cache.get(neg_key, 0.0)
            if now_mono < exp:
                return cached_data

        pr_lock = self._get_pr_inflight_lock(pr_num)
        with pr_lock:
            # Double-check cache & negative cache after winning singleflight lock
            cached_data = self._read_pr_cache(conn, pr_num)
            if cached_data and (cached_data.get("branch") or not allow_remote):
                return cached_data

            now_mono = time.monotonic()
            with self._pr_inflight_guard:
                exp = self._pr_negative_cache.get(neg_key, 0.0)
                if now_mono < exp:
                    return cached_data

            # External commands execute completely outside _db_write_lock and _sync_lock
            repos = self._get_candidate_repos(conn)
            info = cached_data or resolve_pr_from_local_git(
                pr_num, repos, total_budget_sec=2.5
            )
            if allow_remote and (not info or not info.get("branch")):
                gh_info = resolve_pr_via_gh(pr_num, repo_slug, repos)
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

    def _row_to_dict(
        self,
        row: sqlite3.Row,
        match_reason: str = "",
        matched_snippet: str = "",
        local_machine_id: str | None = None,
    ) -> dict[str, Any]:
        local_mid = (
            local_machine_id
            if local_machine_id is not None
            else get_machine_info()["machine_id"]
        )
        return {
            "uid": row["uid"],
            "session_id": row["session_id"],
            "tool": row["tool"],
            "tool_label": row["tool_label"],
            "machine_id": row["machine_id"],
            "machine_name": row["machine_name"],
            "is_local": row["machine_id"] == local_mid,
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
        local_mid = get_machine_info()["machine_id"]

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
                "items": [
                    self._row_to_dict(r, local_machine_id=local_mid) for r in rows
                ],
                "resolved_pr": None,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        pr_num, repo_slug = parse_pr_query(q)
        scored: dict[str, tuple[float, sqlite3.Row, str, str]] = {}
        resolved_pr: dict[str, Any] | None = None

        if pr_num:
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
            if len(pr_num) >= 3:
                try:
                    mention_rows = self._fts_search_rows(
                        conn,
                        f"{{title branches_text search_text}} : {_escape_fts_phrase(pr_num)}",
                        filter_sql,
                        base_params,
                        limit=80,
                    )
                except sqlite3.OperationalError:
                    mention_rows = conn.execute(
                        f"""
                        SELECT s.* FROM sessions s
                        WHERE (s.search_text LIKE ? OR s.title LIKE ? OR s.branches_text LIKE ?) {filter_sql}
                        ORDER BY s.updated_at DESC
                        LIMIT 80
                        """,
                        [f"%{pr_num}%", f"%{pr_num}%", f"%{pr_num}%", *base_params],
                    ).fetchall()
            else:
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
                    snip = self._extract_snippet(r["search_text"], [pr_num])
                    scored[r["uid"]] = (
                        650.0 + (r["updated_at"] / 1e13),
                        r,
                        f"会话提及 #{pr_num}",
                        snip,
                    )

            # 3. Lock-free PR resolution (local git + optional gh CLI)
            need_remote = resolve_remote_pr or (len(direct_rows) == 0)
            resolved_pr = self._resolve_pr_info(
                pr_num, repo_slug, allow_remote=need_remote
            )
            if resolved_pr:
                pr_branch = str(resolved_pr.get("branch") or "").strip()
                if pr_branch and pr_branch not in ("main", "master", "dev"):
                    if len(pr_branch) >= 3:
                        try:
                            b_rows = self._fts_search_rows(
                                conn,
                                f"branches_text : {_escape_fts_phrase(pr_branch)}",
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

                generic_stems = {
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
                file_stems: list[str] = []
                for fp in resolved_pr.get("files") or []:
                    stem = Path(fp).stem
                    if (
                        len(stem) >= 7
                        and stem.lower() not in generic_stems
                        and stem not in file_stems
                    ):
                        file_stems.append(stem)
                if file_stems and len(scored) < 5:
                    fts_files_expr = " OR ".join(
                        f"search_text : {_escape_fts_phrase(st)}"
                        for st in file_stems[:6]
                    )
                    try:
                        f_rows = self._fts_search_rows(
                            conn, fts_files_expr, filter_sql, base_params, limit=15
                        )
                    except sqlite3.OperationalError:
                        or_conds = " OR ".join(
                            ["s.search_text LIKE ?" for _ in file_stems[:6]]
                        )
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
                            snip = self._extract_snippet(
                                r["search_text"], hit_stems or file_stems[:2]
                            )
                            scored[r["uid"]] = (
                                520.0
                                + len(hit_stems) * 40.0
                                + (r["updated_at"] / 1e13),
                                r,
                                f"涉及 PR 改动文件 ({', '.join(hit_stems[:2])})",
                                snip,
                            )

                pr_title = str(resolved_pr.get("title") or "").strip()
                key_tokens = [
                    tok
                    for tok in re.split(r"[\s:()/#,._-]+", pr_title)
                    if len(tok) >= 4
                    and tok.lower()
                    not in {
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
                ][:4]
                if key_tokens and len(scored) < 3:
                    fts_title_expr = " AND ".join(
                        f"search_text : {_escape_fts_phrase(tk)}" for tk in key_tokens
                    )
                    try:
                        t_rows = self._fts_search_rows(
                            conn, fts_title_expr, filter_sql, base_params, limit=15
                        )
                    except sqlite3.OperationalError:
                        like_conds = " AND ".join(
                            ["s.search_text LIKE ?" for _ in key_tokens]
                        )
                        t_rows = conn.execute(
                            f"SELECT s.* FROM sessions s WHERE ({like_conds}) {filter_sql} LIMIT 15",
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

        # 4. General keyword search: FTS5 trigram primary path + short-term LIKE hybrid/fallback
        raw_terms = [t for t in re.split(r"\s+", q) if t]
        clean_terms = [_normalize_search_term(t) for t in raw_terms]
        clean_terms = [t for t in clean_terms if t]

        if clean_terms and (not pr_num or len(scored) == 0):
            fts_terms = [t for t in clean_terms if len(t) >= 3]
            short_terms = [t for t in clean_terms if len(t) < 3]

            kw_rows: list[sqlite3.Row] = []
            used_fts = False

            if fts_terms:
                fts_expr = " AND ".join(_escape_fts_phrase(t) for t in fts_terms)
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
                snip = self._extract_snippet(r["search_text"], clean_terms)
                reason_label = (
                    f"匹配{' / '.join(reasons)}" if reasons else "正文关键词命中"
                )
                scored[r["uid"]] = (score, r, reason_label, snip)

        ordered = sorted(scored.values(), key=lambda x: x[0], reverse=True)[:limit]
        return {
            "items": [
                self._row_to_dict(
                    r,
                    match_reason=reason,
                    matched_snippet=snip,
                    local_machine_id=local_mid,
                )
                for _, r, reason, snip in ordered
            ],
            "resolved_pr": resolved_pr,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
