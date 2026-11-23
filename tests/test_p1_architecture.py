from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import hub.collectors as collectors_mod
import hub.config as config_mod
import hub.launcher as launcher_mod
import hub.server as server_mod
import hub.storage as storage_mod
import hub.tool_registry as registry_mod


class TestP1ToolRegistryAndCatalog(unittest.TestCase):
    def test_tool_registry_integrity_and_launcher_alignment(self) -> None:
        ids = [spec.tool_id for spec in registry_mod.TOOL_SPECS]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate tool_id in TOOL_SPECS")
        for spec in registry_mod.TOOL_SPECS:
            self.assertTrue(spec.label)
            if spec.deep_link_scheme:
                self.assertIn(spec.deep_link_scheme, registry_mod.APPROVED_DEEP_LINK_SCHEMES)

        self.assertIs(
            launcher_mod.APPROVED_DEEP_LINK_SCHEMES,
            registry_mod.APPROVED_DEEP_LINK_SCHEMES,
        )

        catalog = registry_mod.export_client_tool_catalog()
        self.assertIn("codex", catalog["tools"])
        self.assertTrue(catalog["tools"]["grok-bot"]["allow_remote_open"])
        self.assertFalse(catalog["tools"]["codex"]["allow_remote_open"])
        manual_vals = [opt["value"] for opt in catalog["manual_link_options"]]
        self.assertEqual(
            manual_vals,
            ["gemini-web", "grok-web", "chatgpt-web", "cursor", "gemini", "web-link"],
        )

    def test_launcher_dispatch_uses_registry_bundle_ids_and_guards_urls(self) -> None:
        # Unsafe external URL blocked
        res_bad = launcher_mod.open_verified_session_locally(
            {"tool": "web-link", "session_id": "1", "external_url": "javascript:alert(1)"}
        )
        self.assertFalse(res_bad["ok"])
        self.assertEqual(res_bad["action"], "blocked")

        # Unknown tool handled cleanly
        res_unknown = launcher_mod.open_verified_session_locally(
            {"tool": "nonexistent-tool", "session_id": "abc"}
        )
        self.assertFalse(res_unknown["ok"])
        self.assertEqual(res_unknown["action"], "none")

        # Codex launcher uses exact bundle_id from TOOL_REGISTRY
        captured_cmds: list[list[str]] = []

        def fake_open(args: list[str]) -> tuple[bool, str]:
            captured_cmds.append(args)
            return True, "ok"

        with mock.patch.object(launcher_mod, "_run_open", side_effect=fake_open):
            res_codex = launcher_mod.open_verified_session_locally(
                {"tool": "codex", "session_id": "thread-123"}
            )
        self.assertTrue(res_codex["ok"])
        self.assertEqual(
            captured_cmds[-1],
            [
                "/usr/bin/open",
                "-b",
                registry_mod.TOOL_REGISTRY["codex"].bundle_id,
                "codex://threads/thread-123",
            ],
        )


class TestP1CollectorsPackage(unittest.TestCase):
    def test_registry_and_fault_isolation(self) -> None:
        names = [entry.name for entry in collectors_mod.COLLECTOR_REGISTRY]
        self.assertEqual(
            names,
            [
                "claude-magpie",
                "claude-desktop",
                "codex",
                "cursor",
                "gemini",
                "grok-bot",
                "antigravity",
            ],
        )

        def broken_collector(mid: str, mname: str, cache: collectors_mod.FileParseCache):
            raise RuntimeError("corrupt sqlite database")

        def healthy_collector(mid: str, mname: str, cache: collectors_mod.FileParseCache):
            return [
                collectors_mod.build_session_record(
                    machine_id=mid,
                    machine_name=mname,
                    tool="codex",
                    session_id="t1",
                    title="Healthy session",
                    updated_at=2000,
                ),
                collectors_mod.build_session_record(
                    machine_id=mid,
                    machine_name=mname,
                    tool="cursor",
                    session_id="c1",
                    title="Newer healthy session",
                    updated_at=5000,
                ),
            ]

        custom_collectors = (
            collectors_mod.CollectorEntry(name="broken", collect=broken_collector),
            collectors_mod.CollectorEntry(name="healthy", collect=healthy_collector),
        )
        with self.assertLogs("vibe_session_hub.collectors", level="WARNING"):
            sessions = collectors_mod.collect_all_local_sessions(
                "mac-test", "TestMac", collectors=custom_collectors
            )

        self.assertEqual([s["session_id"] for s in sessions], ["c1", "t1"])
        self.assertEqual(sessions[0]["tool_label"], "Cursor")
        self.assertEqual(sessions[1]["tool_label"], "Codex")
        self.assertEqual(sessions[0]["uid"], "mac-test:cursor:c1")

    def test_claude_family_collector_and_parse_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vsh-claude-") as td:
            root = Path(td)
            sessions_root = root / "sessions" / "org" / "proj"
            projects_root = root / "projects" / "my-proj"
            sessions_root.mkdir(parents=True)
            projects_root.mkdir(parents=True)

            (sessions_root / "local_sess01.json").write_text(
                json.dumps(
                    {
                        "sessionId": "local_sess01",
                        "cliSessionId": "cli-uuid-1",
                        "title": "Fix OAuth redirect",
                        "originCwd": "/tmp/my-repo",
                        "writtenBranches": ["fix/oauth"],
                        "createdAt": 1_700_000_000_000,
                        "lastActivityAt": 1_700_000_050_000,
                    }
                ),
                encoding="utf-8",
            )
            (projects_root / "cli-uuid-1.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "gitBranch": "fix/oauth",
                                "message": {
                                    "content": "Check https://github.com/org/repo/pull/404 for redirect bug"
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {"content": "Resolved redirect URI check."},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            cache = collectors_mod.FileParseCache(initial_entries={})
            first = collectors_mod.collect_claude_desktop_family(
                tool_id="claude-desktop",
                tool_label="Claude",
                sessions_root=root / "sessions",
                projects_root=root / "projects",
                machine_id="mac-01",
                machine_name="Mac01",
                cache=cache,
            )
            self.assertEqual(len(first), 1)
            rec = first[0]
            self.assertEqual(rec["uid"], "mac-01:claude-desktop:local_sess01")
            self.assertEqual(rec["prs"][0]["number"], "404")
            self.assertIn("fix/oauth", rec["branches"])
            self.assertEqual(len(cache.dirty_rows), 1)

            # Second run hits FileParseCache without re-opening the JSONL transcript file
            orig_open = Path.open

            def guarded_open(path_self: Path, *args, **kwargs):
                if path_self.suffix == ".jsonl":
                    raise AssertionError(f"Should hit FileParseCache for {path_self}")
                return orig_open(path_self, *args, **kwargs)

            with mock.patch.object(Path, "open", autospec=True, side_effect=guarded_open):
                second = collectors_mod.collect_claude_desktop_family(
                    tool_id="claude-desktop",
                    tool_label="Claude",
                    sessions_root=root / "sessions",
                    projects_root=root / "projects",
                    machine_id="mac-01",
                    machine_name="Mac01",
                    cache=cache,
                )
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["title"], "Fix OAuth redirect")
            self.assertEqual(second[0]["prs"][0]["number"], "404")


class TestP1DependencyInjectionAndHTTPServer(unittest.TestCase):
    def test_dual_isolated_stores_and_http_servers_without_monkeypatching(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vsh-di-") as td:
            root = Path(td)
            paths_a = config_mod.HubPaths.from_state_dir(
                root / "state-a", icloud_root=root / "missing-icloud-a"
            )
            paths_b = config_mod.HubPaths.from_state_dir(
                root / "state-b", icloud_root=root / "missing-icloud-b"
            )
            paths_a.state_dir.mkdir(parents=True, exist_ok=True)
            paths_b.state_dir.mkdir(parents=True, exist_ok=True)
            paths_a.machine_file.write_text(
                json.dumps({"machine_id": "mac-aaa", "machine_name": "AlphaMac", "created_at": 1}),
                encoding="utf-8",
            )
            paths_b.machine_file.write_text(
                json.dumps({"machine_id": "mac-bbb", "machine_name": "BetaMac", "created_at": 1}),
                encoding="utf-8",
            )

            store_a = storage_mod.SessionHubStore(local_only=True, paths=paths_a)
            store_b = storage_mod.SessionHubStore(local_only=True, paths=paths_b)
            srv_a = server_mod.create_http_server(store_a, host="127.0.0.1", port=0)
            srv_b = server_mod.create_http_server(store_b, host="127.0.0.1", port=0)
            ta = threading.Thread(target=srv_a.serve_forever, daemon=True)
            tb = threading.Thread(target=srv_b.serve_forever, daemon=True)
            ta.start()
            tb.start()

            try:
                port_a = srv_a.server_address[1]
                port_b = srv_b.server_address[1]

                # Add a manual link only to server A and change theme only on server B
                req_link = Request(
                    f"http://127.0.0.1:{port_a}/api/manual-link",
                    data=json.dumps(
                        {
                            "tool": "gemini-web",
                            "title": "Alpha isolated session",
                            "pr_number": "707",
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port_a}",
                    },
                    method="POST",
                )
                with urlopen(req_link, timeout=2) as resp:
                    link_res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(link_res["ok"])
                self.assertEqual(link_res["entry"]["tool_label"], "Gemini 网页版")

                req_theme = Request(
                    f"http://127.0.0.1:{port_b}/api/preferences",
                    data=json.dumps({"theme": "forest-sage"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Vibe-Hub": "1",
                        "Host": f"127.0.0.1:{port_b}",
                    },
                    method="POST",
                )
                with urlopen(req_theme, timeout=2) as resp:
                    pref_res = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(pref_res["preferences"]["theme"], "forest-sage")

                # Verify complete isolation across both concurrent servers
                with urlopen(f"http://127.0.0.1:{port_a}/api/facets", timeout=2) as resp:
                    facets_a = json.loads(resp.read().decode("utf-8"))
                with urlopen(f"http://127.0.0.1:{port_b}/api/facets", timeout=2) as resp:
                    facets_b = json.loads(resp.read().decode("utf-8"))

                self.assertEqual(facets_a["current_machine"]["machine_name"], "AlphaMac")
                self.assertEqual(facets_a["total"], 1)
                self.assertEqual(facets_a["preferences"]["theme"], "prism-aurora")
                self.assertIn("tool_catalog", facets_a)
                self.assertIn("codex", facets_a["tool_catalog"]["tools"])

                self.assertEqual(facets_b["current_machine"]["machine_name"], "BetaMac")
                self.assertEqual(facets_b["total"], 0)
                self.assertEqual(facets_b["preferences"]["theme"], "forest-sage")

                # Verify /api/tools endpoint
                with urlopen(f"http://127.0.0.1:{port_a}/api/tools", timeout=2) as resp:
                    tools_res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(tools_res["ok"])
                self.assertIn("manual_link_options", tools_res)

                # Verify ES module files are served with valid JavaScript MIME type
                for mod_name in ("main.js", "state.js", "api.js", "utils.js", "theme.js", "render.js"):
                    with urlopen(f"http://127.0.0.1:{port_a}/js/{mod_name}", timeout=2) as resp:
                        self.assertEqual(resp.status, 200)
                        ctype = resp.headers.get("Content-Type", "")
                        self.assertIn("javascript", ctype)

                # Verify index.html references module entry point
                with urlopen(f"http://127.0.0.1:{port_a}/", timeout=2) as resp:
                    index_html = resp.read().decode("utf-8")
                self.assertIn('type="module" src="/js/main.js"', index_html)

                # Verify path traversal protection returns 404
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(f"http://127.0.0.1:{port_a}/..%2fhub/server.py", timeout=2)
                self.assertEqual(ctx.exception.code, 404)
            finally:
                srv_a.shutdown()
                srv_a.server_close()
                srv_b.shutdown()
                srv_b.server_close()
                store_a.close()
                store_b.close()

    def test_configure_logging_is_idempotent(self) -> None:
        pkg_logger = logging.getLogger("vibe_session_hub")
        config_mod.configure_logging(verbose=False)
        first_count = len(pkg_logger.handlers)
        config_mod.configure_logging(verbose=True)
        self.assertEqual(len(pkg_logger.handlers), first_count)
        self.assertGreaterEqual(first_count, 1)


if __name__ == "__main__":
    unittest.main()
