from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import hub
import hub.collectors as collectors_mod
import hub.config as config_mod
import hub.launcher as launcher_mod
import hub.server as server_mod
import hub.storage as storage_mod

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _seed_sqlite_from_sql(
    sql_path: Path, db_path: Path, replacements: dict[str, str] | None = None
) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql_text = sql_path.read_text(encoding="utf-8")
    for k, v in (replacements or {}).items():
        sql_text = sql_text.replace(k, v)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(sql_text)
        con.commit()
    finally:
        con.close()
    return db_path


def _b32_blob_name(raw_key: str) -> str:
    return base64.b32encode(raw_key.encode("utf-8")).decode("ascii").rstrip("=").lower() + ".blob"


class TestP2CollectorFixtures(unittest.TestCase):
    def test_claude_desktop_and_magpie_fixture_collection(self) -> None:
        cache = collectors_mod.FileParseCache(initial_entries={})
        sessions_root = FIXTURES_DIR / "claude" / "sessions"
        projects_root = FIXTURES_DIR / "claude" / "projects"

        for tool_id, expected_label in (
            ("claude-desktop", "Claude"),
            ("claude-magpie", "Claude Magpie"),
        ):
            items = collectors_mod.collect_claude_desktop_family(
                tool_id=tool_id,
                tool_label=expected_label,
                sessions_root=sessions_root,
                projects_root=projects_root,
                machine_id="mac-ci",
                machine_name="CI-Mac",
                cache=cache,
            )
            by_id = {s["session_id"]: s for s in items}
            self.assertEqual(set(by_id.keys()), {"local_claude_001", "local_claude_002"})

            s1 = by_id["local_claude_001"]
            self.assertEqual(s1["tool"], tool_id)
            self.assertEqual(s1["tool_label"], expected_label)
            self.assertEqual(s1["title"], "Fix OAuth callback redirect loop")
            self.assertEqual(s1["cwd"], "/Users/dev/workspace/demo-app")
            self.assertEqual(s1["branches"], ["fix/oauth-callback"])
            self.assertNotIn("should-ignore-subagent", s1["branches"])
            self.assertNotIn("system-reminder", s1["first_prompt"])
            self.assertNotIn("Request interrupted", s1["search_text"])
            self.assertIn("OAuth callback redirect loop", s1["first_prompt"])
            self.assertIn("nonce validation", s1["summary"])
            self.assertEqual({p["number"] for p in s1["prs"]}, {"1042", "1043"})
            self.assertEqual(
                s1["deep_link"], "claude://claude.ai/epitaxy/local_claude_001"
            )

            s2 = by_id["local_claude_002"]
            self.assertEqual(s2["cwd"], "/Users/dev/workspace/infra-tools")
            self.assertTrue(s2["is_archived"])
            self.assertIn("Terraform module outputs", s2["title"])

        # Verify FileParseCache skips re-reading the JSONL transcript on repeat runs
        orig_open = Path.open

        def guard_jsonl(path_self: Path, *args, **kwargs):
            if path_self.suffix == ".jsonl":
                raise AssertionError(f"Expected FileParseCache hit for {path_self}")
            return orig_open(path_self, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=guard_jsonl):
            cached_items = collectors_mod.collect_claude_desktop_family(
                tool_id="claude-desktop",
                tool_label="Claude",
                sessions_root=sessions_root,
                projects_root=projects_root,
                machine_id="mac-ci",
                machine_name="CI-Mac",
                cache=cache,
            )
        self.assertEqual(len(cached_items), 2)

    def test_codex_fixture_collection_with_spaces_and_rollout_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Include spaces in directory name to verify SQLite URI percent-encoding
            codex_dir = Path(tmp) / "codex dir with spaces"
            rollout_file = codex_dir / "rollout_fallback.jsonl"
            codex_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURES_DIR / "codex" / "rollout_fallback.jsonl", rollout_file)

            state_db = _seed_sqlite_from_sql(
                FIXTURES_DIR / "codex" / "state_5.sql",
                codex_dir / "state_5.sqlite",
                replacements={"__ROLLOUT_PATH__": str(rollout_file)},
            )
            hist_db = _seed_sqlite_from_sql(
                FIXTURES_DIR / "codex" / "thread_history_1.sql",
                codex_dir / "thread_history_1.sqlite",
            )

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_codex_sessions(
                "mac-ci",
                "CI-Mac",
                cache,
                state_db=state_db,
                hist_db=hist_db,
            )
            by_id = {s["session_id"]: s for s in items}
            self.assertEqual(set(by_id.keys()), {"codex-thread-101", "codex-thread-102"})

            s1 = by_id["codex-thread-101"]
            self.assertEqual(s1["title"], "Billing Retry Queue")
            self.assertEqual(s1["branches"], ["feat/billing-retry-queue"])
            self.assertEqual(s1["prs"][0]["number"], "2088")
            self.assertIn("exponential backoff with jitter", s1["summary"])
            self.assertEqual(s1["deep_link"], "codex://threads/codex-thread-101")
            self.assertFalse(s1["is_archived"])

            s2 = by_id["codex-thread-102"]
            self.assertEqual(s2["branches"], [])  # HEAD ignored
            self.assertTrue(s2["is_archived"])
            self.assertEqual(s2["prs"][0]["number"], "2099")  # Extracted from rollout fallback

            # Second run should hit cache and not need hist_db re-querying
            items2 = collectors_mod.collect_codex_sessions(
                "mac-ci",
                "CI-Mac",
                cache,
                state_db=state_db,
                hist_db=codex_dir / "nonexistent_history.sqlite",
            )
            by_id2 = {s["session_id"]: s for s in items2}
            self.assertEqual(by_id2["codex-thread-101"]["prs"][0]["number"], "2088")

    def test_cursor_fixture_collection_all_five_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_dir = root / "workspaceStorage" / "ws-001"
            ws_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                FIXTURES_DIR / "cursor" / "workspace.json", ws_dir / "workspace.json"
            )
            _seed_sqlite_from_sql(
                FIXTURES_DIR / "cursor" / "workspace_state.sql", ws_dir / "state.vscdb"
            )

            gs_db = _seed_sqlite_from_sql(
                FIXTURES_DIR / "cursor" / "global_state.sql",
                root / "globalStorage" / "state.vscdb",
            )
            cs_db = _seed_sqlite_from_sql(
                FIXTURES_DIR / "cursor" / "conversation_search.sql",
                root / "globalStorage" / "conversation-search.db",
            )

            # Synthetic users_root to verify slug-to-cwd decoding cross-platform (Linux + macOS)
            users_root = root / "fake_users"
            target_repo = users_root / "dev" / "repo" / "demo-app"
            target_repo.mkdir(parents=True, exist_ok=True)

            tx_dir = (
                root
                / "cursor_projects"
                / "Users-dev-repo-demo-app"
                / "agent-transcripts"
            )
            tx_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                FIXTURES_DIR / "cursor" / "agent_transcript.jsonl",
                tx_dir / "cursor-tx-003.jsonl",
            )
            # Subagent transcript must be ignored
            sub_tx_dir = tx_dir / "subagents"
            sub_tx_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                FIXTURES_DIR / "cursor" / "agent_transcript.jsonl",
                sub_tx_dir / "cursor-sub-ignored.jsonl",
            )

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_cursor_sessions(
                "mac-ci",
                "CI-Mac",
                cache,
                ws_root=root / "workspaceStorage",
                gs_db=gs_db,
                cs_db=cs_db,
                projects_root=root / "cursor_projects",
                users_root=users_root,
            )
            by_id = {s["session_id"]: s for s in items}

            # Subagent folded into parent; empty draft and subagent transcript excluded
            self.assertNotIn("cursor-sub-001", by_id)
            self.assertNotIn("cursor-draft-empty", by_id)
            self.assertNotIn("cursor-sub-ignored", by_id)
            self.assertEqual(
                set(by_id.keys()),
                {
                    "cursor-comp-001",
                    "bc-agent-777",
                    "cursor-search-only-002",
                    "cursor-tx-003",
                },
            )

            comp = by_id["cursor-comp-001"]
            self.assertEqual(comp["cwd"], "/Users/dev/workspace/demo-app")
            self.assertEqual(
                set(comp["branches"]), {"feat/fts5-search", "feat/fts5-bench"}
            )
            self.assertEqual({p["number"] for p in comp["prs"]}, {"3105", "3106"})
            self.assertEqual(
                comp["deep_link"],
                "cursor://anysphere.cursor-deeplink/agent?id=cursor-comp-001",
            )

            bc = by_id["bc-agent-777"]
            self.assertEqual(
                bc["deep_link"],
                "cursor://anysphere.cursor-deeplink/background-agent?bcId=bc-agent-777",
            )

            cs_only = by_id["cursor-search-only-002"]
            self.assertTrue(cs_only["is_archived"])
            self.assertEqual(cs_only["branches"], ["feat/cjk-fallback"])
            # Not in composerHeaders -> no broken agent deep link
            self.assertEqual(cs_only["deep_link"], "")

            tx = by_id["cursor-tx-003"]
            self.assertEqual(tx["cwd"], str(target_repo))
            self.assertEqual(
                tx["first_prompt"], "Tune SQLite WAL busy_timeout for concurrent reads"
            )
            self.assertNotIn("<timestamp>", tx["first_prompt"])
            self.assertEqual(tx["prs"][0]["number"], "3110")

    def test_gemini_desktop_and_cli_fixture_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "gemini_desktop"
            _seed_sqlite_from_sql(
                FIXTURES_DIR / "gemini" / "chat_info2.sql",
                cache_root / "acct-1" / "ChatInfo2.store",
            )

            cli_root = root / "gemini_cli"
            chat_dir = cli_root / "tmp" / "proj-a" / "chats"
            chat_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                FIXTURES_DIR / "gemini" / "cli_session.json",
                chat_dir / "session-001.json",
            )
            # Duplicate session sharing the desktop ID must be deduplicated
            dup_dir = cli_root / "sessions"
            dup_dir.mkdir(parents=True, exist_ok=True)
            (dup_dir / "dup.json").write_text(
                json.dumps(
                    {
                        "sessionId": "c_gemini_desktop_001",
                        "messages": [{"role": "user", "content": "Duplicate CLI record"}],
                    }
                ),
                encoding="utf-8",
            )

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_gemini_sessions(
                "mac-ci",
                "CI-Mac",
                cache,
                desktop_cache_root=cache_root,
                cli_root=cli_root,
            )
            by_id = {s["session_id"]: s for s in items}
            self.assertEqual(set(by_id.keys()), {"c_gemini_desktop_001", "gemini-cli-001"})

            desk = by_id["c_gemini_desktop_001"]
            self.assertIn("含文档/代码产物", desk["summary"])
            self.assertIn("共 2 条消息（1 轮提问）", desk["summary"])
            self.assertEqual(
                desk["deep_link"], "googlegemini://conversation/c_gemini_desktop_001"
            )

            cli = by_id["gemini-cli-001"]
            self.assertEqual(cli["cwd"], "/Users/dev/workspace/demo-app")
            self.assertEqual(cli["prs"][0]["number"], "4120")
            self.assertIn("universal DMG", cli["summary"])

    def test_grok_bot_fixture_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scp = Path(tmp) / "sand-client-persistence"
            scp.mkdir(parents=True, exist_ok=True)

            roster_blob = scp / _b32_blob_name("acct.roster.last-roster")
            tx_blob = scp / _b32_blob_name("acct.transcript.replicas.grok-agent-001")
            shutil.copyfile(FIXTURES_DIR / "grok_bot" / "roster.json", roster_blob)
            shutil.copyfile(FIXTURES_DIR / "grok_bot" / "transcript_001.json", tx_blob)
            # Corrupted non-base32 blob filename should be ignored gracefully
            (scp / "!!!invalid!!!.blob").write_text("{not-json", encoding="utf-8")

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_grok_bot_sessions(
                "mac-ci", "CI-Mac", cache, persistence_dir=scp
            )
            by_id = {s["session_id"]: s for s in items}

            self.assertEqual(set(by_id.keys()), {"grok-agent-001", "grok-agent-archived"})
            self.assertNotIn("grok-agent-removed", by_id)

            active = by_id["grok-agent-001"]
            self.assertEqual(active["title"], "Audit iCloud atomic mirror lock")
            self.assertEqual(active["cwd"], "/Users/dev/workspace/demo-app")
            self.assertFalse(active["is_archived"])
            self.assertEqual(active["prs"][0]["number"], "5011")
            self.assertIn("kernel lock state", active["summary"])
            self.assertEqual(
                active["deep_link"], "grokbot://app/v1/agent?id=grok-agent-001"
            )

            archived = by_id["grok-agent-archived"]
            self.assertTrue(archived["is_archived"])
            self.assertEqual(archived["cwd"], "")  # Non-/Users path filtered out

    def test_antigravity_fixture_collection_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "antigravity dir"
            db1 = _seed_sqlite_from_sql(
                FIXTURES_DIR / "antigravity" / "primary.sql",
                root / "antigravity" / "conversation_summaries.db",
            )
            db2 = _seed_sqlite_from_sql(
                FIXTURES_DIR / "antigravity" / "cli.sql",
                root / "antigravity-cli" / "conversation_summaries.db",
            )

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_antigravity_sessions(
                "mac-ci", "CI-Mac", cache, db_paths=[db1, db2]
            )
            by_id = {s["session_id"]: s for s in items}
            self.assertEqual(set(by_id.keys()), {"agy-conv-001", "agy-conv-cli-002"})

            s1 = by_id["agy-conv-001"]
            self.assertEqual(s1["title"], "Design DMG release pipeline")
            self.assertEqual(s1["cwd"], "/Users/dev/workspace/My Demo App")
            self.assertEqual(s1["prs"][0]["number"], "6020")
            self.assertIn("SHA-256 verification", s1["search_text"])

    def test_end_to_end_store_search_with_fixture_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            try:
                cache = collectors_mod.FileParseCache(initial_entries={})
                claude_sessions = collectors_mod.collect_claude_desktop_family(
                    tool_id="claude-desktop",
                    tool_label="Claude",
                    sessions_root=FIXTURES_DIR / "claude" / "sessions",
                    projects_root=FIXTURES_DIR / "claude" / "projects",
                    machine_id="mac-ci",
                    machine_name="CI-Mac",
                    cache=cache,
                )
                with mock.patch.object(
                    storage_mod,
                    "collect_all_local_sessions",
                    return_value=claude_sessions,
                ):
                    sync_res = store.sync_now(force_local=True)
                self.assertEqual(sync_res["total_sessions"], 2)

                pr_search = store.search(query="#1042")
                self.assertEqual(len(pr_search["items"]), 1)
                self.assertEqual(pr_search["items"][0]["session_id"], "local_claude_001")

                text_search = store.search(query="Terraform")
                self.assertEqual(len(text_search["items"]), 1)
                self.assertEqual(text_search["items"][0]["session_id"], "local_claude_002")
            finally:
                store.close()


class TestP2PathValidationAndCustomPort(unittest.TestCase):
    def test_resolve_static_file_blocks_traversal_sibling_prefix_symlink_and_nul(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            static_dir = base / "static"
            sibling_dir = base / "static_secret"
            static_dir.mkdir(parents=True)
            sibling_dir.mkdir(parents=True)

            (static_dir / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
            (static_dir / "js").mkdir()
            (static_dir / "js" / "main.js").write_text("export {};\n", encoding="utf-8")
            secret = sibling_dir / "secret.txt"
            secret.write_text("TOP_SECRET", encoding="utf-8")

            escaped_link = static_dir / "escape.txt"
            escaped_link.symlink_to(secret)

            # Legitimate static requests succeed
            self.assertEqual(
                server_mod.resolve_static_file("/", static_dir=static_dir),
                (static_dir / "index.html").resolve(),
            )
            self.assertEqual(
                server_mod.resolve_static_file("/js/main.js", static_dir=static_dir),
                (static_dir / "js" / "main.js").resolve(),
            )

            # Attack vectors must all resolve to None
            attack_paths = [
                "/../static_secret/secret.txt",
                "/%2e%2e/static_secret/secret.txt",
                "/..%2fstatic_secret/secret.txt",
                "/js/../../static_secret/secret.txt",
                "/escape.txt",  # symlink escape outside static_dir
                "/index.html%00.js",  # NUL byte injection
                "/..\\static_secret\\secret.txt",  # backslash traversal
                "//etc/passwd",
                "/%2fetc/passwd",
                "/js/",  # directory, not a file
            ]
            for attack in attack_paths:
                with self.subTest(attack=attack):
                    self.assertIsNone(
                        server_mod.resolve_static_file(attack, static_dir=static_dir)
                    )

    def test_parse_port_and_precedence_resolution(self) -> None:
        self.assertEqual(config_mod.parse_port("18380"), 18380)
        self.assertEqual(config_mod.parse_port(65535), 65535)
        self.assertEqual(config_mod.parse_port(None), 17380)
        self.assertEqual(config_mod.parse_port(""), 17380)
        self.assertEqual(config_mod.parse_port("not-a-port"), 17380)
        self.assertEqual(config_mod.parse_port(0), 17380)
        self.assertEqual(config_mod.parse_port(70000), 17380)
        self.assertEqual(config_mod.parse_port(True), 17380)

        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            # 1. Default fallback
            self.assertEqual(
                config_mod.resolve_configured_port(
                    cli_port=None, preferences_file=prefs_file, env={}
                ),
                17380,
            )
            # 2. preferences.json port
            config_mod.update_preferences(
                theme="forest-sage", port=18400, preferences_file=prefs_file
            )
            self.assertEqual(
                config_mod.resolve_configured_port(
                    cli_port=None, preferences_file=prefs_file, env={}
                ),
                18400,
            )
            # 3. VIBE_HUB_PORT overrides preferences.json
            self.assertEqual(
                config_mod.resolve_configured_port(
                    cli_port=None,
                    preferences_file=prefs_file,
                    env={"VIBE_HUB_PORT": "18500"},
                ),
                18500,
            )
            # 4. CLI --port overrides both env and preferences.json
            self.assertEqual(
                config_mod.resolve_configured_port(
                    cli_port=18600,
                    preferences_file=prefs_file,
                    env={"VIBE_HUB_PORT": "18500"},
                ),
                18600,
            )
            # Updating theme preserves configured port
            updated = config_mod.update_preferences(
                theme="prism-aurora", preferences_file=prefs_file
            )
            self.assertEqual(updated["port"], 18400)
            self.assertEqual(updated["theme"], "prism-aurora")

    def test_custom_port_http_propagation_head_and_origin_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            srv = server_mod.create_http_server(store, host="127.0.0.1", port=0)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            port = srv.server_port
            self.assertNotEqual(port, 17380)

            try:
                # /api/health reports actual bound port and version
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertEqual(health["port"], port)
                self.assertEqual(health["version"], hub.__version__)
                self.assertEqual(
                    health["server"]["base_url"], f"http://127.0.0.1:{port}"
                )

                # /api/facets exposes dynamic server base_url for frontend jump links
                with urlopen(f"http://127.0.0.1:{port}/api/facets", timeout=2) as resp:
                    facets = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(facets["server"]["port"], port)
                self.assertEqual(
                    facets["server"]["base_url"], f"http://127.0.0.1:{port}"
                )

                # HEAD works on /api/health and /js/main.js
                for head_path in ("/api/health", "/js/main.js"):
                    req = Request(f"http://127.0.0.1:{port}{head_path}", method="HEAD")
                    with urlopen(req, timeout=2) as resp:
                        self.assertEqual(resp.status, 200)

                # Same-origin POST on exact bound port succeeds
                ok_req = Request(
                    f"http://127.0.0.1:{port}/api/preferences",
                    data=json.dumps({"theme": "warm-paper"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with urlopen(ok_req, timeout=2) as resp:
                    self.assertEqual(resp.status, 200)

                # Spoofed Origin prefix and cross-port Origin must be rejected (403)
                bad_origins = [
                    f"http://127.0.0.1:{port}.evil.com",
                    "http://127.0.0.1:9999",
                    "https://evil.example.com",
                ]
                for bad_origin in bad_origins:
                    with self.subTest(bad_origin=bad_origin):
                        bad_req = Request(
                            f"http://127.0.0.1:{port}/api/preferences",
                            data=json.dumps({"theme": "prism-aurora"}).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "X-Vibe-Hub": "1",
                                "Host": f"127.0.0.1:{port}",
                                "Origin": bad_origin,
                            },
                            method="POST",
                        )
                        with self.assertRaises(HTTPError) as ctx:
                            urlopen(bad_req, timeout=2)
                        self.assertEqual(ctx.exception.code, 403)
            finally:
                srv.shutdown()
                srv.server_close()
                store.close()

    def test_frontend_jump_url_uses_dynamic_port_not_hardcoded_17380(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")
        # state.js reads document at module evaluation time, so stub globals before dynamic import
        preload = (
            "globalThis.document = { documentElement: { dataset: {} }, getElementById: () => null };\n"
            "globalThis.window = { location: { origin: 'http://127.0.0.1:19456' } };\n"
            "const { state } = await import('./static/js/state.js');\n"
            "const { buildCrossMacJumpUrl } = await import('./static/js/utils.js');\n"
            "const item = { uid: 'mac-1:codex:t1', tool: 'codex', session_id: 't1' };\n"
            "const u1 = buildCrossMacJumpUrl(item);\n"
            "if (!u1.startsWith('http://127.0.0.1:19456/jump?')) throw new Error('u1=' + u1);\n"
            "state.serverBaseUrl = 'http://127.0.0.1:18380';\n"
            "const u2 = buildCrossMacJumpUrl(item);\n"
            "if (!u2.startsWith('http://127.0.0.1:18380/jump?')) throw new Error('u2=' + u2);\n"
            "console.log('JS_PORT_OK');\n"
        )
        res = subprocess.run(
            [node_bin, "--input-type=module", "-e", preload],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        self.assertIn("JS_PORT_OK", res.stdout)

    def test_launcher_send_url_helper_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "send_url_to_pid"
            fake_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            with mock.patch.dict(os.environ, {"VIBE_HUB_SEND_URL_BIN": str(fake_bin)}):
                self.assertEqual(launcher_mod._find_send_url_helper(), str(fake_bin))

    def test_extract_release_notes_reads_changelog_and_appends_checksum(self) -> None:
        import importlib.util

        script_path = (
            Path(__file__).resolve().parent.parent
            / "scripts"
            / "extract_release_notes.py"
        )
        spec = importlib.util.spec_from_file_location("extract_release_notes", script_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sample_changelog = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "## [1.2.0] - 2026-12-01\n\n"
            "### Added\n- New DMG auto-release pipeline.\n\n"
            "## [1.0.0] - 2026-11-23\n\n"
            "### Added\n- Initial release.\n"
        )
        section = mod.extract_changelog_section(sample_changelog, "v1.2.0")
        self.assertIn("New DMG auto-release pipeline", section)
        self.assertNotIn("Initial release", section)

        with tempfile.TemporaryDirectory() as tmp:
            cl = Path(tmp) / "CHANGELOG.md"
            cl.write_text(sample_changelog, encoding="utf-8")
            notes = mod.build_release_notes(
                version="1.2.0",
                tag="v1.2.0",
                changelog_path=cl,
                dmg_name="Vibe-Session-Hub-v1.2.0-macos-universal.dmg",
                sha256_line="deadbeef  Vibe-Session-Hub-v1.2.0-macos-universal.dmg",
                signed_notarized=False,
            )
            self.assertIn("## Vibe Session Hub v1.2.0", notes)
            self.assertIn("New DMG auto-release pipeline", notes)
            self.assertIn("Vibe-Session-Hub-v1.2.0-macos-universal.dmg", notes)
            self.assertIn("xattr -cr", notes)
            self.assertIn("deadbeef", notes)

            notarized_notes = mod.build_release_notes(
                version="1.2.0",
                tag="v1.2.0",
                changelog_path=cl,
                dmg_name="Vibe-Session-Hub-v1.2.0-macos-universal.dmg",
                sha256_line="deadbeef  Vibe-Session-Hub-v1.2.0-macos-universal.dmg",
                signed_notarized=True,
            )
            self.assertNotIn("xattr -cr", notarized_notes)
            self.assertIn("Apple Developer ID", notarized_notes)

            # Unreleased promotion & bundled CHANGELOG stamping when version heading is absent
            unreleased_cl = Path(tmp) / "CHANGELOG_UNRELEASED.md"
            unreleased_cl.write_text(
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "### Fixed\n- **Updater Notes**: Keep release notes across restart.\n\n"
                "## [1.0.5] - 2026-11-25\n\n"
                "### Fixed\n- Antigravity launch fix.\n",
                encoding="utf-8",
            )
            body, source = mod.resolve_release_body(
                version="1.0.6",
                tag="v1.0.6",
                changelog_path=unreleased_cl,
                repo_root=Path(tmp),
            )
            self.assertEqual(source, "unreleased")
            self.assertIn("Keep release notes across restart", body)

            bundled_out = Path(tmp) / "BUNDLED_CHANGELOG.md"
            rc = mod.main(
                [
                    "--version",
                    "1.0.6",
                    "--tag",
                    "v1.0.6",
                    "--changelog",
                    str(unreleased_cl),
                    "--repo-root",
                    str(tmp),
                    "--date",
                    "2026-11-26",
                    "--bundle-changelog",
                    str(bundled_out),
                ]
            )
            self.assertEqual(rc, 0)
            stamped_text = bundled_out.read_text(encoding="utf-8")
            self.assertIn("## [1.0.6] - 2026-11-26", stamped_text)
            self.assertIn("Keep release notes across restart", stamped_text)
            self.assertIn("## [1.0.5] - 2026-11-25", stamped_text)
            from hub import updater as updater_mod

            parsed_bundled = updater_mod.parse_changelog_text(stamped_text)
            self.assertEqual([e["version"] for e in parsed_bundled], ["1.0.6", "1.0.5"])
            self.assertIn("Updater Notes", parsed_bundled[0]["items"][0])
            self.assertEqual(parsed_bundled[0]["sections"][0]["title"], "修复")

        # Repo CHANGELOG.md must include current VERSION and categorized v1.0.0..v1.0.6 entries
        repo_root = Path(__file__).resolve().parent.parent
        repo_ver = (repo_root / "VERSION").read_text(encoding="utf-8").strip().lstrip("vV")
        repo_entries = updater_mod.parse_changelog_text(
            (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        )
        repo_versions = [e["version"] for e in repo_entries]
        for expected_ver in ("1.0.6", "1.0.5", "1.0.4", "1.0.3", "1.0.2", "1.0.1", "1.0.0"):
            self.assertIn(expected_ver, repo_versions)
        self.assertIn(repo_ver, repo_versions)
        valid_titles = {"新增", "优化", "修复", "安全", "移除", "更新内容"}
        for entry in repo_entries:
            self.assertTrue(entry["items"], f"empty items for {entry['version']}")
            self.assertTrue(entry["sections"], f"empty sections for {entry['version']}")
            for sec in entry["sections"]:
                self.assertIn(sec["title"], valid_titles)
                self.assertTrue(sec["items"])

    def test_macos_entitlements_and_packaging_signing_hardening(self) -> None:
        import plistlib

        repo_root = Path(__file__).resolve().parent.parent
        ent_path = repo_root / "macos" / "entitlements.plist"
        self.assertTrue(ent_path.is_file(), "macos/entitlements.plist must exist")
        with ent_path.open("rb") as fh:
            ent = plistlib.load(fh)
        self.assertIs(ent.get("com.apple.security.automation.apple-events"), True)

        pkg_script = (repo_root / "scripts" / "package_macos.sh").read_text(encoding="utf-8")
        self.assertIn("NSAppleEventsUsageDescription", pkg_script)
        self.assertIn("--entitlements", pkg_script)
        self.assertNotIn("codesign --force --deep", pkg_script)
        self.assertIn('"$DMG_PATH"', pkg_script)
        self.assertIn("BUILD_ID", pkg_script)
        self.assertIn("BackendUpgradeLogic.swift", pkg_script)
        self.assertIn("AntigravityOpener.swift", pkg_script)
        self.assertIn("open_antigravity_session", pkg_script)
        self.assertIn("AppUpdater.swift", pkg_script)
        self.assertIn("CHANGELOG.md", pkg_script)
        self.assertIn("--bundle-changelog", pkg_script)

    def test_server_health_metadata_cache_headers_and_graceful_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            srv = server_mod.create_http_server(store, host="127.0.0.1", port=0)
            t = threading.Thread(
                target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            t.start()
            port = srv.server_port

            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertEqual(health["backend_root"], str(server_mod.BACKEND_ROOT))
                self.assertEqual(
                    health["server"]["backend_root"], str(server_mod.BACKEND_ROOT)
                )
                self.assertEqual(health["server"]["pid"], os.getpid())
                self.assertEqual(health["build_id"], hub.__build_id__)
                self.assertEqual(health["server"]["build_id"], hub.__build_id__)

                # Static assets must disable disk caching to avoid stale UI across upgrades
                with urlopen(f"http://127.0.0.1:{port}/js/main.js", timeout=2) as resp:
                    cc = resp.headers.get("Cache-Control", "")
                self.assertIn("no-store", cc)

                # Unauthorized / cross-origin POST /api/shutdown must be rejected (403)
                bad_shutdown = Request(
                    f"http://127.0.0.1:{port}/api/shutdown",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": "https://evil.example.com",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(bad_shutdown, timeout=2)
                self.assertEqual(ctx.exception.code, 403)
                self.assertTrue(t.is_alive())

                # Same-origin POST /api/shutdown stops the server cleanly
                ok_shutdown = Request(
                    f"http://127.0.0.1:{port}/api/shutdown",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with urlopen(ok_shutdown, timeout=2) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["stopping"])
                self.assertEqual(payload["pid"], os.getpid())

                t.join(timeout=3.0)
                self.assertFalse(t.is_alive(), "Server thread should exit after /api/shutdown")
            finally:
                if t.is_alive():
                    srv.shutdown()
                    t.join(timeout=2.0)
                srv.server_close()
                store.close()

    def test_swift_backend_upgrade_logic_on_macos(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS-only Swift test")
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc not available")

        repo_root = Path(__file__).resolve().parent.parent
        logic_file = repo_root / "macos" / "BackendUpgradeLogic.swift"
        self.assertTrue(logic_file.is_file())

        driver = r'''
import Foundation

func expect(_ cond: Bool, _ msg: String) {
    if !cond {
        fputs("FAIL: \(msg)\n", stderr)
        exit(1)
    }
}

// 1. SemVer comparison
expect(compareSemver("1.0.1", "1.0.2") < 0, "1.0.1 < 1.0.2")
expect(compareSemver("1.0.2", "1.0.10") < 0, "1.0.2 < 1.0.10")
expect(compareSemver("v1.0.0", "1.0") == 0, "v1.0.0 == 1.0")
expect(compareSemver("1.0.2-rc.1", "1.0.2") < 0, "prerelease < release")
expect(compareSemver("1.0.2-rc.2", "1.0.2-rc.10") < 0, "rc.2 < rc.10")
expect(compareSemver("1.0.2+build.9", "1.0.2") == 0, "build metadata ignored")
expect(compareSemver("1.1.0", "1.0.9") > 0, "1.1.0 > 1.0.9")

// 2. Health payload identity guard
expect(isVibeHubHealthPayload(["ok": true, "machine": ["machine_id": "mac-1"]]), "valid legacy payload")
expect(!isVibeHubHealthPayload(["ok": true]), "missing machine must be rejected")
expect(!isVibeHubHealthPayload(["ok": false, "machine": ["machine_id": "mac-1"]]), "ok=false rejected")

// 3. Upgrade decision matrix
let fm = FileManager.default
let tmpDir = fm.temporaryDirectory.appendingPathComponent("vsh-swift-test-\(UUID().uuidString)")
try! fm.createDirectory(at: tmpDir, withIntermediateDirectories: true)
defer { try? fm.removeItem(at: tmpDir) }

let appRoot = tmpDir.appendingPathComponent("Vibe Session Hub.app/Contents/Resources/app")
try! fm.createDirectory(at: appRoot, withIntermediateDirectories: true)
try! "v1.0.2\n".write(to: appRoot.appendingPathComponent("VERSION"), atomically: true, encoding: .utf8)
try! "1.0.2-abc1234-20261123\n".write(to: appRoot.appendingPathComponent("BUILD_ID"), atomically: true, encoding: .utf8)

let symlinkRoot = tmpDir.appendingPathComponent("app-link")
try! fm.createSymbolicLink(at: symlinkRoot, withDestinationURL: appRoot)

let expected = readExpectedBackendIdentity(backendRoot: appRoot, bundleVersion: "1.0.0")
expect(expected.version == "1.0.2", "VERSION overrides bundle version and strips v")
expect(expected.buildId == "1.0.2-abc1234-20261123", "BUILD_ID loaded")

// 3a. Pre-v1.0.0 backend (no version field) -> stale
let legacyHealth: [String: Any] = ["ok": true, "machine": ["machine_id": "mac-1"]]
expect(evaluateRunningBackend(health: legacyHealth, expected: expected) == .stale(reason: "missing_version"), "legacy missing version is stale")

// 3b. Older version (v1.0.1) -> stale
let olderHealth: [String: Any] = [
    "ok": true,
    "version": "1.0.1",
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: olderHealth, expected: expected) == .stale(reason: "older_version:1.0.1<1.0.2"), "older version is stale")

// 3c. Newer version (v1.1.0) -> keep newer, never downgrade
let newerHealth: [String: Any] = [
    "ok": true,
    "version": "1.1.0",
    "backend_root": "/Applications/Newer.app/Contents/Resources/app",
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: newerHealth, expected: expected) == .newerRunning(version: "1.1.0"), "newer version must not be downgraded")

// 3d. Same version, missing backend_root -> stale
let noRootHealth: [String: Any] = [
    "ok": true,
    "version": "1.0.2",
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: noRootHealth, expected: expected) == .stale(reason: "missing_backend_root"), "missing backend_root is stale")

// 3e. Same version, different backend_root -> stale
let wrongRootHealth: [String: Any] = [
    "ok": true,
    "version": "1.0.2",
    "backend_root": tmpDir.appendingPathComponent("old-checkout").path,
    "build_id": expected.buildId,
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: wrongRootHealth, expected: expected) == .stale(reason: "different_backend_root"), "different root is stale")

// 3f. Same version & root, different build_id -> stale
let wrongBuildHealth: [String: Any] = [
    "ok": true,
    "version": "1.0.2",
    "backend_root": appRoot.path,
    "build_id": "1.0.2-oldsha-20261122",
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: wrongBuildHealth, expected: expected) == .stale(reason: "different_build_id"), "different build_id is stale")

// 3g. Exact match via symlink path -> current
let currentHealth: [String: Any] = [
    "ok": true,
    "version": "1.0.2",
    "server": [
        "version": "1.0.2",
        "backend_root": symlinkRoot.path,
        "build_id": expected.buildId,
        "pid": 4242,
    ],
    "machine": ["machine_id": "mac-1"],
]
expect(evaluateRunningBackend(health: currentHealth, expected: expected) == .current, "matching symlinked root + build_id is current")
expect(extractHealthServerMetadata(currentHealth).pid == 4242, "extracts pid")

// 4. LaunchAgent plist port & WorkingDirectory parsing
let legacyPlist: [String: Any] = [
    "Label": launchAgentLabel,
    "ProgramArguments": ["/usr/bin/python3", "-m", "hub.server"],
    "WorkingDirectory": appRoot.path + "/",
]
expect(extractLaunchAgentConfiguredPort(plistDict: legacyPlist, fallbackPort: 17380) == 17380, "legacy plist uses fallback port")
expect(extractLaunchAgentWorkingDirectory(plistDict: legacyPlist) == expected.backendRoot, "working dir normalized")

let customPlist = buildLaunchAgentPlist(
    label: launchAgentLabel,
    backendRoot: appRoot,
    hubPort: 18421,
    stateDir: tmpDir,
    sendURLHelperPath: "/Applications/Vibe Session Hub.app/Contents/MacOS/send_url_to_pid"
)
expect(extractLaunchAgentConfiguredPort(plistDict: customPlist, fallbackPort: 17380) == 18421, "custom --port extracted")
let softLimits = customPlist["SoftResourceLimits"] as? [String: Any]
expect((softLimits?["NumberOfFiles"] as? Int) == launchAgentSoftFileLimit, "SoftResourceLimits.NumberOfFiles matches constant")
expect((softLimits?["NumberOfFiles"] as? Int ?? 0) >= 4096, "SoftResourceLimits.NumberOfFiles >= 4096")

// 5. Ephemeral bundle detection & ps parsing
expect(
    isEphemeralBundleLocation(URL(fileURLWithPath: "/private/var/folders/ab/cd/T/AppTranslocation/1234/d/Vibe Session Hub.app")),
    "AppTranslocation detected"
)
expect(!isEphemeralBundleLocation(tmpDir), "writable temp dir is not ephemeral")

let parsedPs = parsePsUidAndCommand("  501 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python -m hub.server\n")
expect(parsedPs?.uid == 501, "uid parsed")
expect(isVibeHubServerCommand(parsedPs?.command ?? ""), "python -m hub.server matched")
expect(!isVibeHubServerCommand("/usr/local/bin/node server.js"), "unrelated node process rejected")

// 6. In-app self-update URL trust, SHA-256 reconciliation, release parsing, and swap script
expect(isTrustedUpdateURL("https://github.com/StriverF/vibe-session-hub/releases/download/v1.0.2/app.dmg"), "github.com https trusted")
expect(isTrustedUpdateURL("https://objects.githubusercontent.com/github-production-release-asset/123"), "githubusercontent https trusted")
expect(isTrustedUpdateURL("https://release-assets.githubusercontent.com/abc"), "release-assets https trusted")
expect(!isTrustedUpdateURL("http://github.com/StriverF/vibe-session-hub/releases/latest"), "http github rejected")
expect(!isTrustedUpdateURL("https://github.com.evil.com/app.dmg"), "spoofed suffix host rejected")
expect(!isTrustedUpdateURL("http://127.0.0.1:18888/release.json", allowLoopback: false), "loopback blocked by default")
expect(isTrustedUpdateURL("http://127.0.0.1:18888/release.json", allowLoopback: true), "loopback allowed when enabled")

let validHash = "d29548924d5f24d9a1322558e4b03370902236e5b6905a941866582992eaec52"
let otherHash = "0000000000000000000000000000000000000000000000000000000000000000"
expect(parseSha256SidecarFile("\(validHash)  Vibe-Session-Hub-v1.0.2-macos-universal.dmg\n", expectedFilename: "Vibe-Session-Hub-v1.0.2-macos-universal.dmg") == validHash, "sidecar matched")
expect(parseSha256SidecarFile("\(validHash)  other.dmg\n", expectedFilename: "Vibe-Session-Hub-v1.0.2-macos-universal.dmg") == nil, "mismatched filename rejected")
expect(reconcileExpectedSha256(apiDigest: "sha256:\(validHash)", sidecarDigest: validHash) == .ok(validHash), "matching digests ok")
expect(reconcileExpectedSha256(apiDigest: "sha256:\(validHash)", sidecarDigest: otherHash) == .mismatch(api: validHash, sidecar: otherHash), "conflicting digests detected")
expect(reconcileExpectedSha256(apiDigest: nil, sidecarDigest: nil) == .missing, "missing digest rejected")

let rawNotes = """
## Vibe Session Hub v1.0.2

### Fixed
- Auto-upgrade stale backend

### 下载与安装（macOS 13+ Universal）
1. Drag to Applications
"""
let cleanedNotes = cleanReleaseNotes(rawNotes)
expect(cleanedNotes.contains("Auto-upgrade stale backend"), "changelog body retained")
expect(!cleanedNotes.contains("Drag to Applications"), "install boilerplate stripped")

let releasePayload: [String: Any] = [
    "tag_name": "v1.0.2",
    "name": "Vibe Session Hub v1.0.2",
    "draft": false,
    "prerelease": false,
    "published_at": "2026-11-24T00:00:00Z",
    "html_url": "https://github.com/StriverF/vibe-session-hub/releases/tag/v1.0.2",
    "body": rawNotes,
    "assets": [
        [
            "name": "Vibe-Session-Hub-v1.0.2-macos-universal.dmg",
            "size": 1414825,
            "digest": "sha256:\(validHash)",
            "browser_download_url": "https://github.com/StriverF/vibe-session-hub/releases/download/v1.0.2/Vibe-Session-Hub-v1.0.2-macos-universal.dmg",
        ],
        [
            "name": "Vibe-Session-Hub-v1.0.2-macos-universal.dmg.sha256",
            "size": 100,
            "browser_download_url": "https://github.com/StriverF/vibe-session-hub/releases/download/v1.0.2/Vibe-Session-Hub-v1.0.2-macos-universal.dmg.sha256",
        ],
    ],
]
let parsedRelease = parseGitHubReleasePayload(releasePayload, currentVersion: "1.0.1")
expect(parsedRelease?.version == "1.0.2", "newer release parsed")
expect(parsedRelease?.apiSha256 == validHash, "api digest parsed")
expect(parsedRelease?.sha256URL.hasSuffix(".dmg.sha256") == true, "sidecar url parsed")
expect(parseGitHubReleasePayload(releasePayload, currentVersion: "1.0.2") == nil, "same version ignored")
expect(parseGitHubReleasePayload(releasePayload, currentVersion: "1.0.3") == nil, "older release ignored")

expect(parseCodesignTeamIdentifier("Authority=Developer ID\nTeamIdentifier=ABCDE12345\n") == "ABCDE12345", "team id parsed")
expect(parseCodesignTeamIdentifier("TeamIdentifier=not set\n") == nil, "ad-hoc team id nil")
expect(extractCliPortOverride(arguments: ["VibeSessionHub", "--port", "18421"]) == 18421, "cli --port extracted")
expect(extractCliPortOverride(arguments: ["VibeSessionHub", "--port=18422"]) == 18422, "cli --port= extracted")
expect(extractCliPortOverride(arguments: ["VibeSessionHub"]) == nil, "no cli port returns nil")

// 6b. UpdateRestartMarker Codable backward compatibility & notes persistence
let legacyMarkerData = Data("{\"from\":\"1.0.4\",\"to\":\"1.0.5\"}".utf8)
let decodedLegacy = try JSONDecoder().decode(UpdateRestartMarker.self, from: legacyMarkerData)
expect(decodedLegacy.from == "1.0.4" && decodedLegacy.to == "1.0.5" && decodedLegacy.notes == nil, "legacy marker decodes")
let richMarker = UpdateRestartMarker(from: "1.0.5", to: "1.0.6", notes: "- Persisted release notes", publishedAt: "2026-11-26T00:00:00Z")
let encodedRich = try JSONEncoder().encode(richMarker)
let decodedRich = try JSONDecoder().decode(UpdateRestartMarker.self, from: encodedRich)
expect(decodedRich == richMarker, "rich marker round-trips with notes and publishedAt")

// 6c. AppUpdater persists restart notes into update-last.json and survives dismissJustUpdated()
let updaterStateDir = tmpDir.appendingPathComponent("updater-state")
try fm.createDirectory(at: updaterStateDir, withIntermediateDirectories: true)
try encodedRich.write(to: updaterStateDir.appendingPathComponent(AppUpdater.restartMarkerFilename))
let updaterInst = AppUpdater(
    currentVersion: "1.0.6",
    stateDir: updaterStateDir,
    bundleURL: tmpDir.appendingPathComponent("NonExistent.app"),
    resourceURL: nil,
    env: ["VIBE_HUB_DISABLE_AUTO_UPDATE_CHECK": "1"],
    cliArguments: ["VibeSessionHub"]
)
let snapBeforeDismiss = updaterInst.snapshotDictionary()
let justDict = snapBeforeDismiss["just_updated"] as? [String: Any]
expect((justDict?["ok"] as? Bool) == true, "just_updated ok")
expect((justDict?["notes"] as? String) == "- Persisted release notes", "just_updated exposes notes")
expect((snapBeforeDismiss["updated_notes"] as? String) == "- Persisted release notes", "top-level updated_notes exposed")
expect((snapBeforeDismiss["updated_published_at"] as? String) == "2026-11-26T00:00:00Z", "top-level updated_published_at exposed")
updaterInst.dismissJustUpdated()
let snapAfterDismiss = updaterInst.snapshotDictionary()
expect(snapAfterDismiss["just_updated"] is NSNull, "just_updated dismissed")
expect((snapAfterDismiss["updated_notes"] as? String) == "- Persisted release notes", "updated_notes survives dismiss")

// 7. Execute real swap-on-exit script against a temporary app bundle directory
let swapParent = tmpDir.appendingPathComponent("swap-test")
let targetApp = swapParent.appendingPathComponent("Vibe Session Hub.app")
let workDir = swapParent.appendingPathComponent(".Vibe Session Hub.app.update-test")
let newApp = workDir.appendingPathComponent("Vibe Session Hub.app")
try fm.createDirectory(at: targetApp, withIntermediateDirectories: true)
try fm.createDirectory(at: newApp, withIntermediateDirectories: true)
try "old-1.0.1".write(to: targetApp.appendingPathComponent("marker.txt"), atomically: true, encoding: .utf8)
try "new-1.0.2".write(to: newApp.appendingPathComponent("marker.txt"), atomically: true, encoding: .utf8)

let sleeper = Process()
sleeper.executableURL = URL(fileURLWithPath: "/bin/sleep")
sleeper.arguments = ["0.25"]
try sleeper.run()

let swapHelper = Process()
swapHelper.executableURL = URL(fileURLWithPath: "/bin/sh")
swapHelper.arguments = ["-c", buildSwapOnExitScript(), "vibe-hub-updater", String(sleeper.processIdentifier), targetApp.path, newApp.path, workDir.path]
try swapHelper.run()

sleeper.waitUntilExit()
swapHelper.waitUntilExit()
expect(swapHelper.terminationStatus == 0, "swap helper exited 0")
let swappedMarker = try String(contentsOf: targetApp.appendingPathComponent("marker.txt"), encoding: .utf8)
expect(swappedMarker == "new-1.0.2", "target bundle replaced with new bundle")
expect(!fm.fileExists(atPath: workDir.path), "staging work dir cleaned up")

print("SWIFT_UPGRADE_LOGIC_OK")
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main_swift = tmp_path / "main.swift"
            bin_path = tmp_path / "test_upgrade_logic"
            main_swift.write_text(driver, encoding="utf-8")

            app_updater_file = repo_root / "macos" / "AppUpdater.swift"
            subprocess.run(
                [
                    swiftc,
                    "-framework",
                    "AppKit",
                    str(logic_file),
                    str(app_updater_file),
                    str(main_swift),
                    "-o",
                    str(bin_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            res = subprocess.run(
                [str(bin_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertIn("SWIFT_UPGRADE_LOGIC_OK", res.stdout)

    def test_python_updater_release_parsing_changelog_and_http_endpoints(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from hub import updater as updater_mod

        # 1. Pure helpers
        self.assertEqual(updater_mod.compare_semver("1.0.1", "1.0.2"), -1)
        self.assertEqual(updater_mod.compare_semver("v1.0.2", "1.0.2"), 0)
        self.assertEqual(updater_mod.compare_semver("1.0.2-rc.1", "1.0.2"), -1)
        self.assertTrue(
            updater_mod.is_trusted_update_url(
                "https://github.com/StriverF/vibe-session-hub/releases/tag/v1.0.2"
            )
        )
        self.assertFalse(
            updater_mod.is_trusted_update_url("https://github.com.evil.com/app.dmg")
        )
        self.assertFalse(
            updater_mod.is_trusted_update_url("http://127.0.0.1:18888/release.json")
        )
        self.assertTrue(
            updater_mod.is_trusted_update_url(
                "http://127.0.0.1:18888/release.json", allow_loopback=True
            )
        )

        # 2. Local mock GitHub Releases feed + HubHTTPServer integration
        fake_sha = "d29548924d5f24d9a1322558e4b03370902236e5b6905a941866582992eaec52"

        class MockFeedHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                body = json.dumps(
                    {
                        "tag_name": "v9.9.9",
                        "name": "Vibe Session Hub v9.9.9",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-11-24T00:00:00Z",
                        "html_url": f"http://127.0.0.1:{self.server.server_port}/releases/v9.9.9",
                        "body": "## Vibe Session Hub v9.9.9\n\n### Added\n- In-app updater\n\n### 下载与安装\n1. Drag DMG\n",
                        "assets": [
                            {
                                "name": "Vibe-Session-Hub-v9.9.9-macos-universal.dmg",
                                "size": 123456,
                                "digest": f"sha256:{fake_sha}",
                                "browser_download_url": f"http://127.0.0.1:{self.server.server_port}/v9.9.9.dmg",
                            }
                        ],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        feed_srv = HTTPServer(("127.0.0.1", 0), MockFeedHandler)
        feed_thread = threading.Thread(target=feed_srv.serve_forever, daemon=True)
        feed_thread.start()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            checker = updater_mod.UpdateChecker(
                current_version="1.0.0",
                backend_root=Path(__file__).resolve().parent.parent,
                feed_url=f"http://127.0.0.1:{feed_srv.server_port}/releases/latest",
                allow_loopback=True,
            )
            srv = server_mod.create_http_server(
                store, host="127.0.0.1", port=0, updater=checker
            )
            srv_thread = threading.Thread(
                target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            srv_thread.start()
            port = srv.server_port

            try:
                with urlopen(f"http://127.0.0.1:{port}/api/update/status", timeout=2) as resp:
                    status = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(status["status"], "idle")
                self.assertEqual(status["current_version"], "1.0.0")
                self.assertFalse(status["can_self_update"])

                with urlopen(f"http://127.0.0.1:{port}/api/update/changelog", timeout=2) as resp:
                    clog = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(clog["ok"])
                self.assertTrue(any(e["version"] == "1.0.0" for e in clog["entries"]))

                # Cross-origin POST /api/update/check must be blocked (403)
                bad_req = Request(
                    f"http://127.0.0.1:{port}/api/update/check",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": "https://evil.example.com",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(bad_req, timeout=2)
                self.assertEqual(ctx.exception.code, 403)

                # Same-origin POST /api/update/check finds v9.9.9 with cleaned notes
                ok_req = Request(
                    f"http://127.0.0.1:{port}/api/update/check",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with urlopen(ok_req, timeout=3) as resp:
                    checked = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(checked["status"], "available")
                self.assertEqual(checked["update"]["version"], "9.9.9")
                self.assertIn("In-app updater", checked["update"]["notes"])
                self.assertNotIn("Drag DMG", checked["update"]["notes"])
            finally:
                srv.shutdown()
                srv.server_close()
                store.close()
                feed_srv.shutdown()
                feed_srv.server_close()

        # 3. Verify remote GitHub Releases merge when local CHANGELOG is stale or remote=1 requested
        self.assertEqual(
            updater_mod.derive_releases_list_url(
                "https://api.github.com/repos/StriverF/vibe-session-hub/releases/latest"
            ),
            "https://api.github.com/repos/StriverF/vibe-session-hub/releases?per_page=15",
        )
        self.assertEqual(
            updater_mod.derive_releases_list_url("https://evil.example.com/releases/latest"),
            "",
        )

        class MockReleasesListHandler(BaseHTTPRequestHandler):
            calls = 0

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                MockReleasesListHandler.calls += 1
                if self.path.startswith("/releases?"):
                    payload = [
                        {
                            "tag_name": "v1.0.5",
                            "draft": False,
                            "prerelease": False,
                            "published_at": "2026-11-25T14:25:01Z",
                            "body": "## Vibe Session Hub v1.0.5\n\n### Fixed\n- Antigravity focus picker\n\n### 下载与安装\n1. Drag\n",
                        },
                        {
                            "tag_name": "v1.0.4",
                            "draft": False,
                            "prerelease": False,
                            "published_at": "2026-11-24T08:57:38Z",
                            "body": "## Vibe Session Hub v1.0.4\n\n### Fixed\n- Bounded SQLite pool\n",
                        },
                        {
                            "tag_name": "v1.0.6-rc.1",
                            "draft": False,
                            "prerelease": True,
                            "published_at": "2026-11-26T00:00:00Z",
                            "body": "- Prerelease should be skipped\n",
                        },
                        {
                            "tag_name": "v1.0.0",
                            "draft": False,
                            "prerelease": False,
                            "published_at": "2026-11-23T00:00:00Z",
                            "body": "- Short remote summary\n",
                        },
                    ]
                    raw = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_error(404)

        rel_srv = HTTPServer(("127.0.0.1", 0), MockReleasesListHandler)
        rel_thread = threading.Thread(target=rel_srv.serve_forever, daemon=True)
        rel_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # Simulate a stale bundled CHANGELOG.md that only has [Unreleased] and [1.0.0]
                (root / "CHANGELOG.md").write_text(
                    "# Changelog\n\n"
                    "## [Unreleased]\n- Unversioned draft\n\n"
                    "## [1.0.0] - 2026-11-23\n- Rich local v1.0.0 summary\n",
                    encoding="utf-8",
                )
                paths = config_mod.HubPaths.from_state_dir(
                    root / "state", icloud_root=root / "no-icloud"
                )
                store = storage_mod.SessionHubStore(local_only=True, paths=paths)
                checker = updater_mod.UpdateChecker(
                    current_version="1.0.5",
                    backend_root=root,
                    feed_url=f"http://127.0.0.1:{rel_srv.server_port}/releases/latest",
                    allow_loopback=True,
                )
                srv = server_mod.create_http_server(
                    store, host="127.0.0.1", port=0, updater=checker
                )
                srv_thread = threading.Thread(
                    target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
                )
                srv_thread.start()
                port = srv.server_port
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/update/changelog", timeout=3) as resp:
                        merged = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(merged["ok"])
                    self.assertEqual(
                        [e["version"] for e in merged["entries"]],
                        ["1.0.5", "1.0.4", "1.0.0"],
                    )
                    self.assertIn("Antigravity focus picker", merged["entries"][0]["items"][0])
                    self.assertEqual(merged["entries"][0]["sections"][0]["title"], "修复")
                    # Rich local v1.0.0 description must be preserved over short remote text
                    self.assertIn("Rich local v1.0.0 summary", merged["entries"][2]["items"][0])
                    self.assertEqual(MockReleasesListHandler.calls, 1)

                    # Subsequent ?remote=1 hits TTL cache rather than re-fetching
                    with urlopen(f"http://127.0.0.1:{port}/api/update/changelog?remote=1", timeout=3) as resp:
                        cached = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(cached["entries"]), 3)
                    self.assertEqual(MockReleasesListHandler.calls, 1)
                finally:
                    srv.shutdown()
                    srv.server_close()
                    store.close()
        finally:
            rel_srv.shutdown()
            rel_srv.server_close()

        # 4. Verify updateControl appears strictly BEFORE openThemeModalBtn in index.html
        html_text = (
            Path(__file__).resolve().parent.parent / "static" / "index.html"
        ).read_text(encoding="utf-8")
        update_pos = html_text.find('id="updateControl"')
        theme_pos = html_text.find('id="openThemeModalBtn"')
        self.assertGreater(update_pos, 0)
        self.assertGreater(theme_pos, update_pos)
        self.assertIn('id="updateModal"', html_text)

    def test_antigravity_collector_fts_title_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "conversation_summaries.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.executescript(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        preview TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT
                    );
                    CREATE TABLE conversations_fts (
                        conversation_id TEXT,
                        chunk_index INTEGER,
                        title TEXT,
                        user_body TEXT,
                        agent_body TEXT
                    );
                    INSERT INTO conversation_summaries VALUES (
                        'agy-empty-title-1',
                        '',
                        'Reviewed Slack thread',
                        '["file:///Users/dev/work/euka"]',
                        '2026-11-24T19:20:57Z'
                    );
                    INSERT INTO conversations_fts VALUES (
                        'agy-empty-title-1',
                        0,
                        'Chunk Fallback Title',
                        'raw user message without title',
                        'agent reply'
                    );
                    INSERT INTO conversations_fts VALUES (
                        'agy-empty-title-1',
                        -1,
                        'Pylon Fix Slack Thread Review',
                        '',
                        ''
                    );
                    """
                )
                con.commit()
            finally:
                con.close()

            cache = collectors_mod.FileParseCache(initial_entries={})
            items = collectors_mod.collect_antigravity_sessions(
                "mac-ci", "CI-Mac", cache, db_paths=[db_path]
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Pylon Fix Slack Thread Review")
            self.assertEqual(items[0]["cwd"], "/Users/dev/work/euka")

            # Launcher exact-title resolver should also recover the FTS header title
            resolved = launcher_mod._resolve_antigravity_exact_title(
                "agy-empty-title-1", "stale fallback", db_paths=[db_path]
            )
            self.assertEqual(resolved, "Pylon Fix Slack Thread Review")

    def test_antigravity_launcher_native_defer_helper_and_focus_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "conversation_summaries.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.executescript(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT
                    );
                    INSERT INTO conversation_summaries VALUES (
                        '6c10d37b-110d-4acf-8325-795f935b29ef',
                        'Pylon Fix Slack Thread Review'
                    );
                    """
                )
                con.commit()
            finally:
                con.close()

            session = {
                "tool": "antigravity",
                "session_id": "6c10d37b-110d-4acf-8325-795f935b29ef",
                "cwd": "/Users/dev/work/euka",
                "title": "Stale Indexed Title",
            }

            orig_resolve = launcher_mod._resolve_antigravity_exact_title

            def resolve_from_tmp(sid: str, fb: str) -> str:
                return orig_resolve(sid, fb, db_paths=[db_path])

            with mock.patch.object(
                launcher_mod,
                "_resolve_antigravity_exact_title",
                side_effect=resolve_from_tmp,
            ):
                # 1. Native app defer mode returns exact DB title payload without running subprocess
                deferred = launcher_mod.open_verified_session_locally(
                    session, defer_native_ui=True
                )
                self.assertTrue(deferred["ok"])
                self.assertEqual(deferred["action"], "native_antigravity_open")
                self.assertEqual(deferred["target"], "antigravity://focus")
                self.assertEqual(
                    deferred["native_payload"]["title"],
                    "Pylon Fix Slack Thread Review",
                )

                # 2. Helper binary env override executes and returns structured JSON result
                fake_helper = root / "open_antigravity_session"
                fake_helper.write_text(
                    "#!/bin/sh\n"
                    'printf \'{"ok":true,"status":"opened","action":"fastpick_open",'
                    '"message":"已唤起 Antigravity 并打开对应会话","target":"antigravity://focus",'
                    '"needs_accessibility":false,"cold_started":false,"matched_index":0}\\n\'\n',
                    encoding="utf-8",
                )
                fake_helper.chmod(0o755)
                with mock.patch.dict(
                    os.environ, {"VIBE_HUB_ANTIGRAVITY_OPENER_BIN": str(fake_helper)}
                ):
                    self.assertEqual(
                        launcher_mod._find_antigravity_opener_helper(), str(fake_helper)
                    )
                    via_helper = launcher_mod.open_verified_session_locally(session)
                self.assertTrue(via_helper["ok"])
                self.assertEqual(via_helper["action"], "fastpick_open")
                self.assertEqual(via_helper["status"], "opened")

                # 3. Fallback when helper missing & Antigravity running -> send_url_to_pid(antigravity://focus)
                sent_urls: list[tuple[int, str]] = []
                copied: list[str] = []
                with mock.patch.object(
                    launcher_mod, "_find_antigravity_opener_helper", return_value=None
                ), mock.patch.object(
                    launcher_mod, "_find_antigravity_main_pid", return_value=543
                ), mock.patch.object(
                    launcher_mod,
                    "_send_url_to_pid",
                    side_effect=lambda pid, url: (sent_urls.append((pid, url)) or True, "ok"),
                ), mock.patch.object(
                    launcher_mod, "copy_to_clipboard", side_effect=copied.append
                ):
                    fb_running = launcher_mod.open_verified_session_locally(session)
                self.assertTrue(fb_running["ok"])
                self.assertEqual(fb_running["action"], "focus_and_copy")
                self.assertFalse(fb_running["cold_started"])
                self.assertEqual(sent_urls, [(543, "antigravity://focus")])
                self.assertEqual(copied, ["Pylon Fix Slack Thread Review"])

                # 4. Fallback when Antigravity is NOT running -> open -b com.google.antigravity antigravity://focus
                open_cmds: list[list[str]] = []
                with mock.patch.object(
                    launcher_mod, "_find_antigravity_opener_helper", return_value=None
                ), mock.patch.object(
                    launcher_mod, "_find_antigravity_main_pid", return_value=None
                ), mock.patch.object(
                    launcher_mod,
                    "_run_open",
                    side_effect=lambda args: (open_cmds.append(args) or True, "ok"),
                ), mock.patch.object(launcher_mod, "copy_to_clipboard"):
                    fb_cold = launcher_mod.open_verified_session_locally(session)
                self.assertTrue(fb_cold["ok"])
                self.assertTrue(fb_cold["cold_started"])
                self.assertEqual(
                    open_cmds,
                    [["/usr/bin/open", "-b", "com.google.antigravity", "antigravity://focus"]],
                )

    def test_api_open_forwards_defer_native_ui_for_verified_antigravity_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            seeded = collectors_mod.build_session_record(
                machine_id="mac-ci",
                machine_name="CI-Mac",
                tool="antigravity",
                session_id="6c10d37b-110d-4acf-8325-795f935b29ef",
                title="Pylon Fix Slack Thread Review",
                cwd="/Users/dev/work/euka",
                created_at=1000,
                updated_at=2000,
                search_text="Pylon Fix Slack Thread Review",
            )
            with mock.patch.object(
                storage_mod,
                "collect_all_local_sessions",
                return_value=[seeded],
            ):
                store.sync_now(force_local=True)
            verified_uid = seeded["uid"]

            srv = server_mod.create_http_server(store, host="127.0.0.1", port=0)
            t = threading.Thread(
                target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            t.start()
            port = srv.server_port
            try:
                # 1. Verified Antigravity UID with defer_native_ui=True returns native_antigravity_open
                req = Request(
                    f"http://127.0.0.1:{port}/api/open",
                    data=json.dumps(
                        {"uid": verified_uid, "defer_native_ui": True}
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with mock.patch.object(
                    launcher_mod,
                    "_resolve_antigravity_exact_title",
                    return_value="Pylon Fix Slack Thread Review",
                ):
                    with urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                self.assertEqual(data["action"], "native_antigravity_open")
                self.assertEqual(
                    data["native_payload"]["title"], "Pylon Fix Slack Thread Review"
                )

                # 2. Unknown UID must still be rejected with 404
                bad_req = Request(
                    f"http://127.0.0.1:{port}/api/open",
                    data=json.dumps(
                        {"uid": "mac-test:antigravity:does-not-exist", "defer_native_ui": True}
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(bad_req, timeout=2)
                self.assertEqual(ctx.exception.code, 404)

                # 3. /jump HTML page wires vibeHubLauncher bridge for native app opens
                with urlopen(
                    f"http://127.0.0.1:{port}/jump?uid={verified_uid}", timeout=2
                ) as resp:
                    jump_html = resp.read().decode("utf-8")
                self.assertIn("vibeHubLauncher", jump_html)
                self.assertIn("defer_native_ui", jump_html)
            finally:
                srv.shutdown()
                srv.server_close()
                store.close()

    def test_swift_antigravity_opener_logic_on_macos(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS-only Swift test")
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc not available")

        repo_root = Path(__file__).resolve().parent.parent
        opener_file = repo_root / "macos" / "AntigravityOpener.swift"
        self.assertTrue(opener_file.is_file())

        driver = r'''
import Foundation

func expect(_ cond: Bool, _ msg: String) {
    if !cond {
        fputs("FAIL: \(msg)\n", stderr)
        exit(1)
    }
}

// 1. Query normalization
expect(normalizeAntigravitySearchQuery("  Pylon   Fix\nSlack\tThread  ") == "Pylon Fix Slack Thread", "collapses whitespace")
expect(normalizeAntigravitySearchQuery("   ", fallbackSessionId: "abc-123") == "abc-123", "falls back to session id")

// 2. Placeholder & FastPick classification (must reject chat composer!)
expect(classifyAntigravityFastPickPlaceholder("Search for conversations...", hasFastPickAncestor: false) == .conversations, "conversations mode")
expect(classifyAntigravityFastPickPlaceholder("Search conversations, commands, and code...", hasFastPickAncestor: false) == .unified, "unified mode")
expect(classifyAntigravityFastPickPlaceholder("Select category to search...", hasFastPickAncestor: false) == .unified, "category mode")
expect(classifyAntigravityFastPickPlaceholder("Search for commands...", hasFastPickAncestor: true) == .otherCategory, "other category with ancestor")
expect(classifyAntigravityFastPickPlaceholder("Ask anything...", hasFastPickAncestor: false) == nil, "chat box must never match")
expect(classifyAntigravityFastPickPlaceholder("Search files...", hasFastPickAncestor: false) == nil, "generic search without fastpick ancestor must not match")
expect(classifyAntigravityFastPickPlaceholder("Custom localized text", hasFastPickAncestor: true) == .unknownFastPick, "fastpick ancestor fallback")

// 3. Paste payload prefix handling
expect(buildAntigravityPastePayload(query: "Pylon Fix Slack Thread Review", mode: .conversations) == "Pylon Fix Slack Thread Review", "conversations mode avoids prefix")
expect(buildAntigravityPastePayload(query: "Pylon Fix Slack Thread Review", mode: .unified) == "conversations:Pylon Fix Slack Thread Review", "unified mode adds prefix")
expect(buildAntigravityPastePayload(query: "> fix shell prompt", mode: .conversations) == "conversations:> fix shell prompt", "reserved prefix escaped")
expect(buildAntigravityPastePayload(query: "files: cleanup", mode: .conversations) == "conversations:files: cleanup", "files prefix escaped")

// 4. Workspace hint extraction
expect(extractWorkspaceHint(fromCwd: "/Users/alan/EukaWrok/worktrees/gemini/work2/euka") == "euka", "extracts basename")
expect(extractWorkspaceHint(fromCwd: "/") == "", "ignores root")
expect(extractWorkspaceHint(fromCwd: "") == "", "empty cwd")

// 5. Candidate matching & workspace disambiguation
let dupItems = [
    AntigravityCandidateItem(index: 0, primaryText: "Fix Auth Redirect", fullText: "Fix Auth Redirect backend-api", isHighlighted: true),
    AntigravityCandidateItem(index: 1, primaryText: "Fix Auth Redirect", fullText: "Fix Auth Redirect euka", isHighlighted: false),
]
expect(selectBestAntigravityCandidate(items: dupItems, query: "Fix Auth Redirect", workspaceHint: "euka") == 1, "workspace hint breaks duplicate title tie")
expect(selectBestAntigravityCandidate(items: dupItems, query: "Fix Auth Redirect", workspaceHint: "") == 0, "highlighted/first wins when no hint")

let truncatedItems = [
    AntigravityCandidateItem(index: 0, primaryText: "Investigate SQLite Connection Pool...", fullText: "Investigate SQLite Connection Pool... vibe-session-hub", isHighlighted: true),
]
expect(selectBestAntigravityCandidate(items: truncatedItems, query: "Investigate SQLite Connection Pool Leak in WAL Mode", workspaceHint: "vibe-session-hub") == 0, "handles ellipsis-truncated display title")

let unrelatedItems = [
    AntigravityCandidateItem(index: 0, primaryText: "No results found", fullText: "No results found", isHighlighted: false),
    AntigravityCandidateItem(index: 1, primaryText: "Show 12 more...", fullText: "Show 12 more...", isHighlighted: false),
    AntigravityCandidateItem(index: 2, primaryText: "Completely Different Thread", fullText: "Completely Different Thread other-repo", isHighlighted: true),
]
expect(selectBestAntigravityCandidate(items: unrelatedItems, query: "Pylon Fix Slack Thread Review", workspaceHint: "euka") == nil, "must never select unrelated item")

// 6. Result JSON encoding
let res = AntigravityOpenResult(
    ok: true,
    status: "opened",
    action: "fastpick_open",
    message: "ok",
    target: antigravityFocusURL,
    needsAccessibility: false,
    coldStarted: false,
    matchedIndex: 0
)
let jsonStr = res.toJSONString()
expect(jsonStr.contains("\"fastpick_open\""), "encodes action")
expect(jsonStr.contains("\"needs_accessibility\":false"), "encodes snake_case keys")

print("SWIFT_ANTIGRAVITY_OPENER_OK")
'''
        with tempfile.TemporaryDirectory() as tmp:
            driver_path = Path(tmp) / "main.swift"
            bin_path = Path(tmp) / "test_antigravity_opener"
            driver_path.write_text(driver, encoding="utf-8")
            subprocess.run(
                [
                    swiftc,
                    "-framework",
                    "AppKit",
                    "-framework",
                    "ApplicationServices",
                    "-framework",
                    "Carbon",
                    "-framework",
                    "CoreServices",
                    str(opener_file),
                    str(driver_path),
                    "-o",
                    str(bin_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            run_res = subprocess.run(
                [str(bin_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertIn("SWIFT_ANTIGRAVITY_OPENER_OK", run_res.stdout)


if __name__ == "__main__":
    unittest.main()
