from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from . import config as _config_mod
from .collectors import FileParseCache, collect_all_local_sessions
from .config import (
    ICLOUD_SYNC_ROOT,
    INDEX_DB_PATH,
    LOCAL_SYNC_ROOT,
    MANUAL_LINKS_DIR,
    NODES_DIR,
    HubPaths,
    ensure_dirs,
    get_machine_info,
    is_icloud_enabled,
)
from .git_pr_resolver import (
    parse_pr_query,
    resolve_pr_from_local_git,
    resolve_pr_via_gh,
)
from .search_engine import (
    PR_NEGATIVE_CACHE_TTL_SEC,
    SearchEngine,
    escape_fts_phrase as _escape_fts_phrase,
    normalize_search_term as _normalize_search_term,
)
from .shard_ingestor import (
    SESSIONS_COLUMNS as _SESSIONS_COLUMNS,
    SESSIONS_INSERT_SQL as _SESSIONS_INSERT_SQL,
    SESSIONS_UPDATE_SQL as _SESSIONS_UPDATE_SQL,
    STANDALONE_SHARD_KEY,
    ShardIngestor,
    parse_node_shard,
)
from .sync_mirror import (
    ICloudSyncMirror,
    atomic_write_bytes as _atomic_write_bytes,
    file_sha256 as _file_sha256,
    sha256_bytes as _sha256_bytes,
    trigger_icloud_downloads as _trigger_icloud_downloads,
    try_sync_file_lock as _try_sync_file_lock,
)

logger = logging.getLogger("vibe_session_hub.storage")

SCHEMA_VERSION = "3"
DEFAULT_READ_POOL_SIZE = 8


def _configure_sqlite_conn(conn: sqlite3.Connection, readonly: bool = False) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


class _ReadConnectionPool:
    """Bounded, non-blocking pool of read-only SQLite WAL connections.

    Each checked-out connection is exclusively owned by a single caller for the
    duration of its context manager block, then returned to the idle pool (or
    closed immediately when the idle pool is already at capacity). Because
    checkouts never block on pool exhaustion, re-entrant reads and burst
    concurrency cannot deadlock while steady-state open FDs stay strictly bounded.
    """

    def __init__(self, db_path: Path, max_idle: int = DEFAULT_READ_POOL_SIZE) -> None:
        self._db_path = Path(db_path)
        self._max_idle = max(0, int(max_idle))
        self._lock = threading.Lock()
        self._idle: list[sqlite3.Connection] = []
        self._in_use: set[sqlite3.Connection] = set()
        self._closed = False

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            _configure_sqlite_conn(conn, readonly=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        return conn

    def acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed store.")
            conn = self._idle.pop() if self._idle else None
            if conn is not None:
                self._in_use.add(conn)
                return conn

        conn = self._open_conn()
        with self._lock:
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                raise sqlite3.ProgrammingError("Cannot operate on a closed store.")
            self._in_use.add(conn)
            return conn

    def release(self, conn: sqlite3.Connection, *, broken: bool = False) -> None:
        should_close = broken
        if not should_close:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except Exception:
                should_close = True

        with self._lock:
            self._in_use.discard(conn)
            if (
                not should_close
                and not self._closed
                and len(self._idle) < self._max_idle
            ):
                self._idle.append(conn)
                return

        try:
            conn.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.acquire()
        broken = False
        try:
            yield conn
        except sqlite3.Error:
            broken = True
            raise
        finally:
            self.release(conn, broken=broken)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "idle": len(self._idle),
                "in_use": len(self._in_use),
                "max_idle": self._max_idle,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            conns = list(self._idle)
            self._idle.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


class SessionHubStore:
    """Coordinator facade combining SQLite WAL storage, iCloud mirroring, shard ingestion, and search."""

    def __init__(
        self,
        local_only: bool = False,
        db_path: Path | None = None,
        paths: HubPaths | None = None,
        read_pool_size: int = DEFAULT_READ_POOL_SIZE,
    ) -> None:
        self.local_only = local_only
        self._has_explicit_paths = paths is not None

        if paths is not None:
            resolved_paths = (
                replace(paths, index_db_path=Path(db_path))
                if db_path is not None
                else paths
            )
        else:
            # Build paths from current module globals so legacy test patches keep working seamlessly.
            resolved_paths = HubPaths(
                state_dir=_config_mod.LOCAL_STATE_DIR,
                local_sync_root=LOCAL_SYNC_ROOT,
                nodes_dir=NODES_DIR,
                manual_links_dir=MANUAL_LINKS_DIR,
                icloud_root=_config_mod.ICLOUD_ROOT,
                icloud_sync_root=ICLOUD_SYNC_ROOT,
                index_db_path=Path(db_path) if db_path is not None else INDEX_DB_PATH,
                machine_file=_config_mod.MACHINE_FILE,
                preferences_file=_config_mod.PREFERENCES_FILE,
            )

        self.paths = resolved_paths
        self.db_path = self.paths.index_db_path

        ensure_dirs(local_only=local_only, paths=self.paths)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Short-lived lock protecting SQLite write transactions only.
        self._db_write_lock = threading.Lock()
        # Serializes sync/ingestion pipelines without ever blocking HTTP readers.
        self._sync_lock = threading.Lock()

        self._closed = False
        self._read_pool = _ReadConnectionPool(
            self.db_path, max_idle=read_pool_size
        )
        self._legacy_read_lock = threading.Lock()
        self._legacy_read_conns: dict[
            int, tuple[threading.Thread, sqlite3.Connection]
        ] = {}

        self._last_local_scan_ms = 0

        self._write_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        _configure_sqlite_conn(self._write_conn, readonly=False)
        self._init_schema()

        self._mirror = ICloudSyncMirror(
            paths=self.paths,
            local_only=local_only,
            trigger_downloads_fn=lambda folder, raise_on_os_error=False: _trigger_icloud_downloads(
                folder, raise_on_os_error=raise_on_os_error
            ),
        )
        self._ingestor = ShardIngestor(
            paths=self.paths,
            get_read_conn=self._get_read_conn,
            acquire_read_conn=self._read_conn,
            write_conn=self._write_conn,
            db_write_lock=self._db_write_lock,
            parse_node_shard_fn=lambda p: self._parse_node_shard(p),
        )
        self._search_engine = SearchEngine(
            get_read_conn=self._get_read_conn,
            acquire_read_conn=self._read_conn,
            write_conn=self._write_conn,
            db_write_lock=self._db_write_lock,
            get_machine_info_fn=self._get_machine_info,
            resolve_local_git_fn=lambda pr_num, repos, total_budget_sec=2.5: resolve_pr_from_local_git(
                pr_num, repos, total_budget_sec=total_budget_sec
            ),
            resolve_gh_fn=lambda pr_num, repo_slug, repos: resolve_pr_via_gh(
                pr_num, repo_slug, repos
            ),
        )

    @property
    def icloud_access(self) -> str:
        return self._mirror.icloud_access

    @icloud_access.setter
    def icloud_access(self, value: str) -> None:
        self._mirror.icloud_access = value

    @property
    def icloud_status(self) -> str:
        return self._mirror.icloud_status

    @icloud_status.setter
    def icloud_status(self, value: str) -> None:
        self._mirror.icloud_status = value

    def _get_machine_info(self) -> dict[str, Any]:
        if self._has_explicit_paths:
            return get_machine_info(machine_file=self.paths.machine_file)
        return get_machine_info()

    def _read_conn(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return self._read_pool.connection()

    def read_pool_stats(self) -> dict[str, int]:
        return self._read_pool.stats()

    def _get_read_conn(self) -> sqlite3.Connection:
        """Legacy per-thread read connection accessor (kept for test compatibility).

        Connections created for short-lived threads are pruned and closed once
        their owning thread exits; internal callers should prefer `_read_conn()`.
        """
        current = threading.current_thread()
        tid = current.ident if current.ident is not None else id(current)
        with self._legacy_read_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed store.")
            dead_keys = [
                k
                for k, (th, _) in self._legacy_read_conns.items()
                if not th.is_alive()
            ]
            dead_conns = [self._legacy_read_conns.pop(k)[1] for k in dead_keys]
            existing = self._legacy_read_conns.get(tid)
        for dc in dead_conns:
            try:
                dc.close()
            except Exception:
                pass
        if existing is not None and existing[0] is current:
            return existing[1]

        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            _configure_sqlite_conn(conn, readonly=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        with self._legacy_read_lock:
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                raise sqlite3.ProgrammingError("Cannot operate on a closed store.")
            prev = self._legacy_read_conns.get(tid)
            self._legacy_read_conns[tid] = (current, conn)
        if prev is not None and prev[1] is not conn:
            try:
                prev[1].close()
            except Exception:
                pass
        return conn

    def close(self) -> None:
        with self._legacy_read_lock:
            self._closed = True
            legacy_conns = [c for _, c in self._legacy_read_conns.values()]
            self._legacy_read_conns.clear()
        for c in legacy_conns:
            try:
                c.close()
            except Exception:
                pass
        self._read_pool.close()
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
                    machine_type TEXT NOT NULL DEFAULT '',
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

    @staticmethod
    def _parse_node_shard(shard_path: Path) -> dict[str, dict[str, Any]]:
        return parse_node_shard(shard_path)

    def _export_local_shard(
        self,
        machine_id: str,
        machine_name: str,
        sessions: list[dict[str, Any]],
        machine_type: str = "",
        machine_model: str = "",
    ) -> bool:
        return self._mirror.export_local_shard(
            machine_id,
            machine_name,
            sessions,
            machine_type=machine_type,
            machine_model=machine_model,
        )

    def _mirror_with_icloud(self, machine_id: str) -> bool:
        return self._mirror.mirror(machine_id)

    def _ingest_sync_shards(
        self,
        force_reindex: bool = False,
        dirty_shards: set[str] | None = None,
    ) -> bool:
        return self._ingestor.ingest(
            force_reindex=force_reindex, dirty_shards=dirty_shards
        )

    def get_sync_health(self) -> dict[str, Any]:
        return self._mirror.get_health()

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
            ensure_dirs(
                local_only=self.local_only or skip_icloud, paths=self.paths
            )
            minfo = self._get_machine_info()
            mid = minfo["machine_id"]
            mname = minfo["machine_name"]

            dirty_shards: set[str] = set()
            now_ms = int(t0 * 1000)

            # Phase 1: Collect local sessions completely outside _db_write_lock
            if not skip_local and (
                force_local or (now_ms - self._last_local_scan_ms > 8_000)
            ):
                with self._read_conn() as read_conn:
                    initial_cache = FileParseCache.load_entries(read_conn)
                parse_cache = FileParseCache(initial_entries=initial_cache)
                local_sessions = collect_all_local_sessions(
                    mid, mname, cache=parse_cache
                )
                if parse_cache.dirty_rows:
                    with self._db_write_lock:
                        parse_cache.flush_to_conn(self._write_conn)
                        self._write_conn.commit()
                if self._export_local_shard(
                    mid,
                    mname,
                    local_sessions,
                    machine_type=str(minfo.get("machine_type") or ""),
                    machine_model=str(minfo.get("machine_model") or ""),
                ):
                    dirty_shards.add(f"node:{mid}.jsonl")
                self._last_local_scan_ms = now_ms

            # Phase 2: iCloud mirror completely outside DB locks
            if bridge_synced:
                self._mirror.mark_bridge_synced()
            if not skip_icloud:
                self._mirror_with_icloud(mid)

            # Phase 3 & 4: Incremental shard ingest
            reloaded = self._ingest_sync_shards(
                force_reindex=force_reindex,
                dirty_shards=dirty_shards,
            )

            elapsed_ms = int((time.time() - t0) * 1000)
            with self._read_conn() as read_conn:
                total = read_conn.execute(
                    "SELECT count(*) FROM sessions"
                ).fetchone()[0]
            health = self.get_sync_health()
            return {
                "ok": True,
                "elapsed_ms": elapsed_ms,
                "reloaded_shards": reloaded,
                "total_sessions": total,
                "sync_root": self._mirror.active_sync_root(),
                "icloud_status": health["icloud_status"],
                "icloud_access": health["icloud_access"],
                "sync_owner": health["sync_owner"],
                "machine": minfo,
            }

    def add_manual_link(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._sync_lock:
            minfo = self._get_machine_info()
            mid = minfo["machine_id"]
            entry, link_file = self._ingestor.append_manual_link(data, minfo)
            self._mirror_with_icloud(mid)
            self._ingest_sync_shards(dirty_shards={f"link:{link_file.name}"})
            return entry

    def get_session_by_uid(self, uid: str) -> dict[str, Any] | None:
        return self._search_engine.get_session_by_uid(uid)

    def get_facets(self) -> dict[str, Any]:
        return self._search_engine.get_facets(
            sync_root=self._mirror.active_sync_root(),
            health=self.get_sync_health(),
        )

    def search(
        self,
        query: str = "",
        tool: str = "",
        machine_id: str = "",
        only_with_pr: bool = False,
        resolve_remote_pr: bool = False,
        limit: int = 80,
    ) -> dict[str, Any]:
        return self._search_engine.search(
            query=query,
            tool=tool,
            machine_id=machine_id,
            only_with_pr=only_with_pr,
            resolve_remote_pr=resolve_remote_pr,
            limit=limit,
        )


__all__ = [
    "DEFAULT_READ_POOL_SIZE",
    "ICLOUD_SYNC_ROOT",
    "INDEX_DB_PATH",
    "LOCAL_SYNC_ROOT",
    "MANUAL_LINKS_DIR",
    "NODES_DIR",
    "PR_NEGATIVE_CACHE_TTL_SEC",
    "SCHEMA_VERSION",
    "STANDALONE_SHARD_KEY",
    "SessionHubStore",
    "_SESSIONS_COLUMNS",
    "_SESSIONS_INSERT_SQL",
    "_SESSIONS_UPDATE_SQL",
    "_atomic_write_bytes",
    "_escape_fts_phrase",
    "_file_sha256",
    "_normalize_search_term",
    "_sha256_bytes",
    "_trigger_icloud_downloads",
    "_try_sync_file_lock",
    "collect_all_local_sessions",
    "is_icloud_enabled",
    "parse_pr_query",
    "resolve_pr_from_local_git",
    "resolve_pr_via_gh",
]
