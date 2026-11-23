from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

import hub.collectors as collectors_mod
import hub.config as config_mod
import hub.git_pr_resolver as git_pr_mod
import hub.server as server_mod
import hub.storage as storage_mod


def _make_session(
    machine_id: str,
    session_id: str,
    title: str,
    *,
    machine_name: str = "TestMac",
    tool: str = "claude-desktop",
    tool_label: str = "Claude",
    branches: list[str] | None = None,
    prs: list[dict[str, str]] | None = None,
    cwd: str = "/tmp/repo",
    search_text: str = "",
    updated_at: int = 1_700_000_000_000,
) -> dict:
    return {
        "uid": f"{machine_id}:{tool}:{session_id}",
        "session_id": session_id,
        "tool": tool,
        "tool_label": tool_label,
        "machine_id": machine_id,
        "machine_name": machine_name,
        "title": title,
        "first_prompt": title,
        "summary": search_text[:200],
        "cwd": cwd,
        "branches": branches or [],
        "prs": prs or [],
        "created_at": updated_at - 1000,
        "updated_at": updated_at,
        "is_archived": False,
        "deep_link": f"claude://claude.ai/code/{session_id}",
        "external_url": "",
        "search_text": search_text or title,
    }


def _write_node_shard(path: Path, machine_id: str, machine_name: str, sessions: list[dict]) -> None:
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
        lines.append(json.dumps(s, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class IsolatedHubTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="vsh-test-")
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.local_sync = self.state_dir / "sync"
        self.nodes_dir = self.local_sync / "nodes"
        self.links_dir = self.local_sync / "manual_links"
        self.icloud_root = self.root / "icloud-root"
        self.icloud_sync = self.icloud_root / "VibeSessionHub"
        self.db_path = self.state_dir / "index.db"
        self.machine_file = self.state_dir / "machine.json"
        self.prefs_file = self.state_dir / "preferences.json"

        self.icloud_root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.machine_file.write_text(
            json.dumps(
                {
                    "machine_id": "mac-local01",
                    "machine_name": "LocalMac",
                    "created_at": 1_700_000_000_000,
                }
            ),
            encoding="utf-8",
        )

        self._patches = [
            mock.patch.object(config_mod, "LOCAL_STATE_DIR", self.state_dir),
            mock.patch.object(config_mod, "LOCAL_SYNC_ROOT", self.local_sync),
            mock.patch.object(config_mod, "NODES_DIR", self.nodes_dir),
            mock.patch.object(config_mod, "MANUAL_LINKS_DIR", self.links_dir),
            mock.patch.object(config_mod, "ICLOUD_ROOT", self.icloud_root),
            mock.patch.object(config_mod, "ICLOUD_SYNC_ROOT", self.icloud_sync),
            mock.patch.object(config_mod, "INDEX_DB_PATH", self.db_path),
            mock.patch.object(config_mod, "MACHINE_FILE", self.machine_file),
            mock.patch.object(config_mod, "PREFERENCES_FILE", self.prefs_file),
            mock.patch.object(config_mod, "_MACHINE_INFO_CACHE", None),
            mock.patch.object(storage_mod, "LOCAL_SYNC_ROOT", self.local_sync),
            mock.patch.object(storage_mod, "NODES_DIR", self.nodes_dir),
            mock.patch.object(storage_mod, "MANUAL_LINKS_DIR", self.links_dir),
            mock.patch.object(storage_mod, "ICLOUD_SYNC_ROOT", self.icloud_sync),
            mock.patch.object(storage_mod, "INDEX_DB_PATH", self.db_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


class TestP0FTS5AndIncrementalIngest(IsolatedHubTestCase):
    def test_v1_schema_migrates_cleanly_preserving_caches(self) -> None:
        # Build a realistic v1 database before opening SessionHubStore
        con = sqlite3.connect(str(self.db_path))
        con.executescript(
            """
            CREATE TABLE sessions (uid TEXT PRIMARY KEY, title TEXT);
            CREATE VIRTUAL TABLE sessions_fts USING fts5(uid UNINDEXED, title, tokenize='unicode61');
            CREATE TABLE file_parse_cache (cache_key TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, payload_json TEXT);
            CREATE TABLE pr_cache (pr_number TEXT PRIMARY KEY, payload_json TEXT, updated_at INTEGER);
            INSERT INTO file_parse_cache VALUES ('k1', 123, 456, '{"ok": true}');
            INSERT INTO pr_cache VALUES ('777', '{"pr_number":"777","branch":"feat/old"}', 1000);
            """
        )
        con.commit()
        con.close()

        store = storage_mod.SessionHubStore(local_only=True)
        try:
            rconn = store._get_read_conn()
            ver = rconn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            self.assertEqual(ver, storage_mod.SCHEMA_VERSION)
            # Preserved caches
            fpc = rconn.execute("SELECT payload_json FROM file_parse_cache WHERE cache_key = 'k1'").fetchone()
            self.assertIsNotNone(fpc)
            prc = rconn.execute("SELECT payload_json FROM pr_cache WHERE pr_number = '777'").fetchone()
            self.assertIsNotNone(prc)
            # Upgraded FTS5 uses trigram external content
            fts_sql = rconn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions_fts'"
            ).fetchone()["sql"]
            self.assertIn("trigram", fts_sql)
            self.assertIn("content='sessions'", fts_sql)
        finally:
            store.close()

    def test_fts5_trigram_cjk_short_fallback_pr_and_syntax_safety(self) -> None:
        local_sessions = [
            _make_session(
                "mac-local01",
                "s1",
                "修复登录鉴权Bug",
                branches=["feat/auth-login-flow"],
                prs=[{"number": "1234", "repo": "org/app", "url": "https://github.com/org/app/pull/1234", "branch": "feat/auth-login-flow"}],
                cwd="/Users/dev/auth-service",
                search_text="修复登录鉴权Bug 排查 JWT token 过期和 authMiddlewareHandler.ts 问题",
                updated_at=1_700_000_005_000,
            ),
            _make_session(
                "mac-local01",
                "s2",
                "优化支付网关超时",
                branches=["fix/payment-timeout"],
                prs=[{"number": "42", "repo": "org/app", "url": "https://github.com/org/app/pull/42", "branch": ""}],
                cwd="/Users/dev/pay",
                search_text="讨论支付网关超时重试与 PR #1234 关联改动",
                updated_at=1_700_000_002_000,
            ),
            _make_session(
                "mac-local01",
                "s3",
                '特殊字符 "quote" (parens) OR NOT *',
                search_text='测试特殊符号 "quote" (parens) storage.py',
                updated_at=1_700_000_001_000,
            ),
        ]

        store = storage_mod.SessionHubStore(local_only=True)
        try:
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=local_sessions
            ):
                res = store.sync_now(force_local=True)
                self.assertEqual(res["total_sessions"], 3)
                self.assertTrue(res["reloaded_shards"])

            # Verify FTS5 integrity check passes
            with store._db_write_lock:
                store._write_conn.execute(
                    "INSERT INTO sessions_fts(sessions_fts) VALUES('integrity-check')"
                )

            # Verify query plan actually uses sessions_fts virtual table
            plan = store._get_read_conn().execute(
                """
                EXPLAIN QUERY PLAN
                SELECT s.* FROM sessions_fts JOIN sessions s ON s.rowid = sessions_fts.rowid
                WHERE sessions_fts MATCH '"登录鉴权"'
                """
            ).fetchall()
            plan_text = " ".join(str(row["detail"]) for row in plan)
            self.assertIn("sessions_fts", plan_text)

            # 1. CJK >= 3 chars via FTS5 trigram
            r_cjk3 = store.search("登录鉴权")
            self.assertEqual([i["session_id"] for i in r_cjk3["items"]], ["s1"])

            # 2. CJK 2-char short query falls back cleanly instead of returning empty
            r_cjk2 = store.search("登录")
            self.assertEqual([i["session_id"] for i in r_cjk2["items"]], ["s1"])

            # 3. Hybrid query: >=3 char FTS + 2-char short filter
            r_hybrid = store.search("登录 auth")
            self.assertEqual([i["session_id"] for i in r_hybrid["items"]], ["s1"])
            r_hybrid_miss = store.search("支付 auth")
            self.assertEqual(r_hybrid_miss["items"], [])

            # 4. Substring path / branch match
            r_branch = store.search("auth-login")
            self.assertEqual([i["session_id"] for i in r_branch["items"]], ["s1"])
            r_file = store.search("storage.py")
            self.assertEqual([i["session_id"] for i in r_file["items"]], ["s3"])

            # 5. Exact PR lookup (both 4-digit and 2-digit) + PR mention
            with mock.patch.object(storage_mod, "resolve_pr_from_local_git", return_value=None), \
                 mock.patch.object(storage_mod, "resolve_pr_via_gh", return_value=None):
                r_pr1234 = store.search("PR #1234")
                self.assertEqual([i["session_id"] for i in r_pr1234["items"]], ["s1", "s2"])
                self.assertIn("直接绑定", r_pr1234["items"][0]["match_reason"])
                self.assertIn("提及", r_pr1234["items"][1]["match_reason"])

                r_pr42 = store.search("#42")
                self.assertEqual([i["session_id"] for i in r_pr42["items"]], ["s2"])

            # 6. FTS5 syntax injection characters do not raise
            r_special = store.search('"quote" (parens) OR NOT *')
            self.assertEqual([i["session_id"] for i in r_special["items"]], ["s3"])

            # 7. Resolved PR branch + changed-file stem + title keyword FTS5 matching
            fake_pr_branch = {
                "pr_number": "5555",
                "title": "feat: overhaul authentication pipeline",
                "branch": "feat/auth-login-flow",
                "url": "https://github.com/org/app/pull/5555",
                "files": ["src/authMiddlewareHandler.ts"],
                "source": "gh_cli",
                "resolved_at": 1_700_000_000_000,
            }
            with mock.patch.object(storage_mod, "resolve_pr_from_local_git", return_value=None), \
                 mock.patch.object(storage_mod, "resolve_pr_via_gh", return_value=fake_pr_branch):
                r_pr5555 = store.search("PR #5555")
                self.assertEqual([i["session_id"] for i in r_pr5555["items"]], ["s1"])
                self.assertIn("匹配 PR 分支", r_pr5555["items"][0]["match_reason"])

            fake_pr_file = {
                "pr_number": "5556",
                "title": "chore: touch handler",
                "branch": "feat/unrelated-branch",
                "url": "https://github.com/org/app/pull/5556",
                "files": ["src/authMiddlewareHandler.ts"],
                "source": "gh_cli",
                "resolved_at": 1_700_000_000_000,
            }
            with mock.patch.object(storage_mod, "resolve_pr_from_local_git", return_value=None), \
                 mock.patch.object(storage_mod, "resolve_pr_via_gh", return_value=fake_pr_file):
                r_pr5556 = store.search("PR #5556")
                self.assertEqual([i["session_id"] for i in r_pr5556["items"]], ["s1"])
                self.assertIn("涉及 PR 改动文件", r_pr5556["items"][0]["match_reason"])
        finally:
            store.close()

    def test_incremental_shard_ingest_skips_unchanged_and_handles_manual_links(self) -> None:
        local_s1 = _make_session("mac-local01", "loc1", "本地会话一", updated_at=1_700_000_001_000)
        local_s2 = _make_session("mac-local01", "loc2", "本地会话二", updated_at=1_700_000_002_000)
        remote_s1 = _make_session("mac-remote02", "rem1", "远端会话一", machine_name="RemoteMac", updated_at=1_700_000_003_000)
        _write_node_shard(self.nodes_dir / "mac-remote02.jsonl", "mac-remote02", "RemoteMac", [remote_s1])

        store = storage_mod.SessionHubStore(local_only=True)
        try:
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[local_s1, local_s2]
            ):
                r1 = store.sync_now(force_local=True)
                self.assertTrue(r1["reloaded_shards"])
                self.assertEqual(r1["total_sessions"], 3)

                # Capture rowids before second sync
                before_rowids = {
                    r["uid"]: r["rowid"]
                    for r in store._get_read_conn().execute("SELECT rowid, uid FROM sessions").fetchall()
                }

                # Second 90s sync with force_local=True and zero data changes -> NO re-ingest!
                r2 = store.sync_now(force_local=True)
                self.assertFalse(r2["reloaded_shards"])

            # Cold restart with unchanged sessions -> NO shard rewrite and NO re-ingest!
            store.close()
            store = storage_mod.SessionHubStore(local_only=True)
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[local_s1, local_s2]
            ):
                r_cold = store.sync_now(force_local=True)
                self.assertFalse(r_cold["reloaded_shards"])

            # Modify ONLY local_s2; remote shard must NOT even be parsed and loc1/rem1 rowids stay untouched
            local_s2_updated = _make_session(
                "mac-local01", "loc2", "本地会话二-已更新关键词", updated_at=1_700_000_010_000
            )
            parse_calls: list[str] = []
            orig_parse = storage_mod.SessionHubStore._parse_node_shard

            def spy_parse(path: Path):
                parse_calls.append(path.name)
                return orig_parse(path)

            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[local_s1, local_s2_updated]
            ), mock.patch.object(storage_mod.SessionHubStore, "_parse_node_shard", side_effect=spy_parse):
                r3 = store.sync_now(force_local=True)
                self.assertTrue(r3["reloaded_shards"])
                self.assertEqual(parse_calls, ["mac-local01.jsonl"])

            after_rowids = {
                r["uid"]: r["rowid"]
                for r in store._get_read_conn().execute("SELECT rowid, uid FROM sessions").fetchall()
            }
            self.assertEqual(before_rowids, after_rowids)
            self.assertEqual(
                [i["session_id"] for i in store.search("已更新关键词")["items"]],
                ["loc2"],
            )

            # Attach manual PR link to remote session -> only remote shard reconciled
            link_entry = store.add_manual_link(
                {
                    "target_uid": remote_s1["uid"],
                    "pr_number": "8888",
                    "title": "远端手工标题标注",
                    "note": "跨机补充上下文备注",
                }
            )
            with mock.patch.object(storage_mod, "resolve_pr_from_local_git", return_value=None), \
                 mock.patch.object(storage_mod, "resolve_pr_via_gh", return_value=None):
                r_pr = store.search("#8888")
                self.assertEqual([i["session_id"] for i in r_pr["items"]], ["rem1"])
            self.assertEqual(
                [i["session_id"] for i in store.search("远端手工标题标注")["items"]],
                ["rem1"],
            )

            # Standalone manual link -> indexed under __manual_links__ and searchable
            store.add_manual_link(
                {
                    "target_uid": "",
                    "pr_number": "7777",
                    "title": "独立外链研讨记录",
                    "external_url": "https://example.com/chat/7777",
                    "note": "架构评审备忘",
                }
            )
            self.assertEqual(
                len(store.search("独立外链研讨记录")["items"]),
                1,
            )

            # Delete manual attachment -> remote session reverts to original title & drops PR #8888
            link_file = self.links_dir / "mac-local01-links.jsonl"
            lines = link_file.read_text(encoding="utf-8").splitlines()
            rewritten = []
            for ln in lines:
                obj = json.loads(ln)
                if obj.get("link_id") == link_entry["link_id"]:
                    obj["deleted"] = True
                rewritten.append(json.dumps(obj, ensure_ascii=False))
            link_file.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[local_s1, local_s2_updated]
            ):
                store.sync_now(force_local=False, skip_local=True, skip_icloud=True)
            reverted = store.get_session_by_uid(remote_s1["uid"])
            self.assertIsNotNone(reverted)
            self.assertEqual(reverted["title"], "远端会话一")
            self.assertEqual(reverted["prs"], [])
            self.assertEqual(store.search("远端手工标题标注")["items"], [])

            # Remove remote shard -> deletes only remote rows and maintains FTS5 integrity
            (self.nodes_dir / "mac-remote02.jsonl").unlink()
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[local_s1, local_s2_updated]
            ):
                r4 = store.sync_now(force_local=False, skip_local=True)
                self.assertTrue(r4["reloaded_shards"])
                # 2 local sessions + 1 standalone manual link
                self.assertEqual(r4["total_sessions"], 3)

            with store._db_write_lock:
                store._write_conn.execute(
                    "INSERT INTO sessions_fts(sessions_fts) VALUES('integrity-check')"
                )
        finally:
            store.close()


class TestP0ICloudOwnershipAndAtomicSync(IsolatedHubTestCase):
    def test_directional_sync_atomic_write_and_manual_links_shrink(self) -> None:
        # Put a newer remote shard in iCloud and a stale local copy of the SAME remote shard locally
        cloud_remote = self.icloud_sync / "nodes" / "mac-other99.jsonl"
        local_remote = self.nodes_dir / "mac-other99.jsonl"
        _write_node_shard(
            cloud_remote,
            "mac-other99",
            "OtherMac",
            [_make_session("mac-other99", "r1", "来自云端的最新远端会话")],
        )
        _write_node_shard(
            local_remote,
            "mac-other99",
            "OtherMac",
            [_make_session("mac-other99", "r_stale", "本地陈旧缓存绝不能覆盖云端")],
        )
        # Artificially make local_remote mtime newer than cloud_remote!
        future_ns = time.time_ns() + 60_000_000_000
        os.utime(local_remote, ns=(future_ns, future_ns))

        local_s = [_make_session("mac-local01", "l1", "本机会话推动到云端")]
        store = storage_mod.SessionHubStore(local_only=False)
        try:
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=local_s
            ):
                res = store.sync_now(force_local=True)
                self.assertEqual(res["icloud_access"], "direct")
                self.assertEqual(res["sync_owner"], "python")
                self.assertEqual(res["icloud_status"], "synced")

            # Cloud's remote shard MUST NOT have been overwritten by local stale copy
            self.assertIn("来自云端的最新远端会话", cloud_remote.read_text(encoding="utf-8"))
            # Local copy MUST have been updated from cloud
            self.assertIn("来自云端的最新远端会话", local_remote.read_text(encoding="utf-8"))
            # Own shard pushed to cloud
            cloud_own = self.icloud_sync / "nodes" / "mac-local01.jsonl"
            self.assertTrue(cloud_own.is_file())
            self.assertIn("本机会话推动到云端", cloud_own.read_text(encoding="utf-8"))

            # Verify manual_links push works even when local file shrinks (old size < bug fixed)
            local_links = self.links_dir / "mac-local01-links.jsonl"
            cloud_links = self.icloud_sync / "manual_links" / "mac-local01-links.jsonl"
            local_links.write_text('{"link_id":"l1","note":"long note content here"}\n', encoding="utf-8")
            store._mirror_with_icloud("mac-local01")
            self.assertEqual(cloud_links.read_text(encoding="utf-8"), local_links.read_text(encoding="utf-8"))

            # Shrink local links file -> must still push via SHA-256 comparison
            local_links.write_text('{"link_id":"l1","note":"tiny"}\n', encoding="utf-8")
            store._mirror_with_icloud("mac-local01")
            self.assertEqual(cloud_links.read_text(encoding="utf-8"), local_links.read_text(encoding="utf-8"))

            # No temporary files left behind
            leftover_tmps = list(self.icloud_sync.rglob("*.tmp.*")) + list(self.local_sync.rglob("*.tmp.*"))
            self.assertEqual(leftover_tmps, [])
        finally:
            store.close()

    def test_flock_and_bsd_o_exlock_mutual_exclusion(self) -> None:
        lock_path = self.local_sync / ".sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Simulate Swift holding BSD O_EXLOCK | O_NONBLOCK (exact macOS flag used by VibeSessionHub.swift)
        o_exlock = getattr(os, "O_EXLOCK", 0)
        if o_exlock == 0:
            if sys.platform == "darwin":
                self.fail("macOS must expose os.O_EXLOCK")
            # Non-BSD platforms (e.g. Linux CI): verify cross-fd fcntl.flock mutual exclusion directly
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with storage_mod._try_sync_file_lock(lock_path) as acquired:
                    self.assertFalse(acquired)
            finally:
                os.close(fd)

            with storage_mod._try_sync_file_lock(lock_path) as acquired:
                self.assertTrue(acquired)
                fd2 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd2)
            return

        swift_fd = os.open(
            str(lock_path), os.O_CREAT | os.O_RDWR | o_exlock | os.O_NONBLOCK, 0o600
        )
        try:
            with storage_mod._try_sync_file_lock(lock_path) as acquired:
                self.assertFalse(acquired)
        finally:
            os.close(swift_fd)

        # Reverse direction: Python holds fcntl.flock -> BSD O_EXLOCK | O_NONBLOCK must fail immediately
        with storage_mod._try_sync_file_lock(lock_path) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(BlockingIOError):
                os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_RDWR | o_exlock | os.O_NONBLOCK,
                    0o600,
                )

    def test_permission_denied_delegates_to_app_bridge(self) -> None:
        store = storage_mod.SessionHubStore(local_only=False)
        try:
            with mock.patch.object(
                storage_mod, "collect_all_local_sessions", return_value=[]
            ), mock.patch.object(
                storage_mod, "_trigger_icloud_downloads", side_effect=PermissionError("TCC denied")
            ):
                res = store.sync_now(force_local=True)
                self.assertEqual(res["icloud_access"], "permission_denied")
                self.assertEqual(res["sync_owner"], "app_bridge")
                self.assertEqual(res["icloud_status"], "local_fallback")

            # Simulate Swift bridge pulling a remote shard and notifying /api/sync with bridge_synced=True
            _write_node_shard(
                self.nodes_dir / "mac-remote03.jsonl",
                "mac-remote03",
                "StudioMac",
                [_make_session("mac-remote03", "b1", "桥接拉取的远端会话")],
            )
            # Simulate >8s elapsed since last scan so skip_local=True is genuinely exercised
            store._last_local_scan_ms = 0
            collect_spy = mock.Mock(return_value=[])
            with mock.patch.object(storage_mod, "collect_all_local_sessions", collect_spy):
                res2 = store.sync_now(
                    force_local=False,
                    skip_local=True,
                    skip_icloud=True,
                    bridge_synced=True,
                )
                self.assertEqual(res2["icloud_status"], "synced_via_app")
                self.assertEqual(res2["total_sessions"], 1)
                self.assertTrue(res2["reloaded_shards"])
                # Should not have re-scanned local clients unnecessarily
                collect_spy.assert_not_called()
        finally:
            store.close()


class TestP0ConcurrencyAndCaching(IsolatedHubTestCase):
    def test_wal_reads_unblocked_during_slow_sync(self) -> None:
        seed = [_make_session("mac-local01", f"s{i}", f"并发测试会话 {i} 登录鉴权") for i in range(25)]
        store = storage_mod.SessionHubStore(local_only=True)
        try:
            with mock.patch.object(storage_mod, "collect_all_local_sessions", return_value=seed):
                store.sync_now(force_local=True)

            scan_started = threading.Event()
            release_scan = threading.Event()

            def slow_collect(mid: str, mname: str, conn=None, cache=None):
                scan_started.set()
                release_scan.wait(timeout=2.0)
                return seed

            with mock.patch.object(storage_mod, "collect_all_local_sessions", side_effect=slow_collect):
                sync_thread = threading.Thread(
                    target=lambda: store.sync_now(force_local=True)
                )
                sync_thread.start()
                self.assertTrue(scan_started.wait(timeout=1.0))

                # While slow_collect is running, fire concurrent readers across multiple threads
                latencies_ms: list[float] = []
                errors: list[Exception] = []

                def reader(idx: int) -> None:
                    try:
                        t0 = time.perf_counter()
                        res = store.search("登录鉴权")
                        facets = store.get_facets()
                        s = store.get_session_by_uid("mac-local01:claude-desktop:s0")
                        dt_ms = (time.perf_counter() - t0) * 1000
                        latencies_ms.append(dt_ms)
                        assert len(res["items"]) == 25
                        assert facets["total"] == 25
                        assert s is not None
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=2.0)

                release_scan.set()
                sync_thread.join(timeout=2.0)

            self.assertEqual(errors, [])
            self.assertEqual(len(latencies_ms), 10)
            # All reads completed while sync was still blocked -> proving zero lock contention!
            self.assertLess(max(latencies_ms), 250.0, f"Reader stalled: {latencies_ms}")
        finally:
            store.close()

    def test_pr_resolution_lock_free_singleflight_and_negative_cache(self) -> None:
        seed = [_make_session("mac-local01", "s1", "普通搜索任务 登录鉴权")]
        store = storage_mod.SessionHubStore(local_only=True)
        try:
            with mock.patch.object(storage_mod, "collect_all_local_sessions", return_value=seed):
                store.sync_now(force_local=True)

            git_calls = 0
            git_started = threading.Event()
            git_release = threading.Event()
            call_lock = threading.Lock()

            def slow_git(pr_num: str, repos: list[str], total_budget_sec: float = 2.5):
                nonlocal git_calls
                with call_lock:
                    git_calls += 1
                git_started.set()
                git_release.wait(timeout=2.0)
                return None

            with mock.patch.object(storage_mod, "resolve_pr_from_local_git", side_effect=slow_git), \
                 mock.patch.object(storage_mod, "resolve_pr_via_gh", return_value=None):
                pr_threads = [
                    threading.Thread(target=lambda: store.search("PR #99999", resolve_remote_pr=False))
                    for _ in range(4)
                ]
                for t in pr_threads:
                    t.start()
                self.assertTrue(git_started.wait(timeout=1.0))

                # Unrelated search MUST NOT be blocked by in-flight PR git lookup!
                t0 = time.perf_counter()
                normal_res = store.search("登录鉴权")
                normal_ms = (time.perf_counter() - t0) * 1000
                self.assertEqual(len(normal_res["items"]), 1)
                self.assertLess(normal_ms, 150.0)

                git_release.set()
                for t in pr_threads:
                    t.join(timeout=2.0)

                # Singleflight coalesced all 4 concurrent lookups into 1 git invocation
                self.assertEqual(git_calls, 1)

                # Subsequent call within TTL hits negative cache -> 0 new git invocations
                store.search("PR #99999", resolve_remote_pr=False)
                self.assertEqual(git_calls, 1)
        finally:
            store.close()

    def test_machine_info_cache_and_http_health_sync_endpoints(self) -> None:
        info1 = config_mod.get_machine_info()
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("Should use cache")):
            info2 = config_mod.get_machine_info()
        self.assertEqual(info1, info2)

        updated = config_mod.update_machine_name("RenamedMac")
        self.assertEqual(updated["machine_name"], "RenamedMac")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("Should stay cached")):
            self.assertEqual(config_mod.get_machine_info()["machine_name"], "RenamedMac")

        # FileParseCache memory-only + batch flush check
        store = storage_mod.SessionHubStore(local_only=True)
        try:
            cache = collectors_mod.FileParseCache(
                initial_entries=collectors_mod.FileParseCache.load_entries(store._get_read_conn())
            )
            cache.put("test_file_1", 100, 200, {"first_prompt": "hello"})
            self.assertEqual(len(cache.dirty_rows), 1)
            with store._db_write_lock:
                flushed = cache.flush_to_conn(store._write_conn)
                store._write_conn.commit()
            self.assertEqual(flushed, 1)
            reloaded = collectors_mod.FileParseCache(
                initial_entries=collectors_mod.FileParseCache.load_entries(store._get_read_conn())
            )
            self.assertEqual(reloaded.get("test_file_1", 100, 200), {"first_prompt": "hello"})

            # End-to-end HTTP check on ephemeral port
            server_mod.HubRequestHandler.store = store
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.HubRequestHandler)
            port = httpd.server_address[1]
            srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            srv_thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertTrue(health["local_only"])
                self.assertEqual(health["sync_owner"], "none")
                self.assertEqual(health["machine"]["machine_name"], "RenamedMac")

                req = Request(
                    f"http://127.0.0.1:{port}/api/sync",
                    data=json.dumps({"bridge_synced": True, "skip_icloud": True}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=2) as resp:
                    sync_res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(sync_res["ok"])
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            store.close()

    def test_git_pr_resolver_respects_total_budget(self) -> None:
        fake_repos: list[str] = []
        for i in range(10):
            r = self.root / f"repo-{i}"
            (r / ".git").mkdir(parents=True, exist_ok=True)
            fake_repos.append(str(r))

        calls = 0

        def slow_check_output(*args, **kwargs):
            nonlocal calls
            calls += 1
            time.sleep(0.12)
            return ""

        t0 = time.monotonic()
        with mock.patch.object(git_pr_mod.subprocess, "check_output", side_effect=slow_check_output):
            res = git_pr_mod.resolve_pr_from_local_git("12345", fake_repos, total_budget_sec=0.3)
        elapsed = time.monotonic() - t0
        self.assertIsNone(res)
        self.assertLess(calls, 5)
        self.assertLess(elapsed, 0.7)


if __name__ == "__main__":
    unittest.main()
