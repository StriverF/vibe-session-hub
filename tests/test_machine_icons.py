from __future__ import annotations

import json
import platform
import plistlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub import config as config_mod
from hub import device_icons as device_icons_mod
from hub import server as server_mod
from hub import storage as storage_mod


def _make_session(
    machine_id: str,
    session_id: str,
    title: str,
    *,
    tool: str = "codex",
    machine_name: str = "TestMac",
    machine_type: str = "",
    updated_at: int = 1_700_000_000_000,
) -> dict:
    rec = {
        "uid": f"{machine_id}:{tool}:{session_id}",
        "session_id": session_id,
        "tool": tool,
        "tool_label": "Codex",
        "machine_id": machine_id,
        "machine_name": machine_name,
        "title": title,
        "first_prompt": title,
        "summary": "",
        "cwd": "/tmp/demo",
        "branches": [],
        "prs": [],
        "created_at": updated_at - 1000,
        "updated_at": updated_at,
        "is_archived": False,
        "deep_link": f"codex://threads/{session_id}",
        "external_url": "",
        "search_text": title,
    }
    if machine_type:
        rec["machine_type"] = machine_type
    return rec


def _write_shard(path: Path, meta: dict, sessions: list[dict]) -> None:
    lines = [json.dumps(meta, ensure_ascii=False)]
    for s in sessions:
        lines.append(json.dumps(s, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestMachineTypeClassificationAndDetection(unittest.TestCase):
    def test_classify_from_product_name_hw_model_and_display_name(self) -> None:
        cases = [
            # Apple Silicon IORegistry product names
            ({"product_name": "Mac Studio (2023)"}, "mac-studio"),
            ({"product_name": "Mac Studio (2025)"}, "mac-studio"),
            ({"product_name": "Mac mini (2024)"}, "mac-mini"),
            ({"product_name": "MacBook Pro (16-inch, Nov 2024)"}, "macbook-pro"),
            ({"product_name": "MacBook Air (15-inch, M3, 2024)"}, "macbook-air"),
            ({"product_name": "iMac (24-inch, M4, 2024)"}, "imac"),
            ({"product_name": "Mac Pro (2023)"}, "mac-pro"),
            ({"product_name": "MacBook (Retina, 12-inch, 2017)"}, "macbook"),
            # Apple Silicon hw.model identifiers
            ({"machine_model": "Mac13,1"}, "mac-studio"),
            ({"machine_model": "Mac14,13"}, "mac-studio"),
            ({"machine_model": "Mac15,14"}, "mac-studio"),
            ({"machine_model": "Mac16,9"}, "mac-studio"),
            ({"machine_model": "Mac14,3"}, "mac-mini"),
            ({"machine_model": "Mac14,12"}, "mac-mini"),
            ({"machine_model": "Mac16,10"}, "mac-mini"),
            ({"machine_model": "Mac14,5"}, "macbook-pro"),
            ({"machine_model": "Mac15,6"}, "macbook-pro"),
            ({"machine_model": "Mac16,7"}, "macbook-pro"),
            ({"machine_model": "Mac14,2"}, "macbook-air"),
            ({"machine_model": "Mac15,13"}, "macbook-air"),
            ({"machine_model": "Mac16,12"}, "macbook-air"),
            ({"machine_model": "Mac15,4"}, "imac"),
            ({"machine_model": "Mac16,3"}, "imac"),
            ({"machine_model": "Mac14,8"}, "mac-pro"),
            # Intel & M1 descriptive hw.model identifiers
            ({"machine_model": "MacBookPro18,2"}, "macbook-pro"),
            ({"machine_model": "MacBookAir10,1"}, "macbook-air"),
            ({"machine_model": "Macmini9,1"}, "mac-mini"),
            ({"machine_model": "iMac21,1"}, "imac"),
            ({"machine_model": "MacPro7,1"}, "mac-pro"),
            ({"machine_model": "MacBook10,1"}, "macbook"),
            # Display names including CJK prefixes (no spaces before Mac)
            ({"machine_name": "Alan的Mac Studio"}, "mac-studio"),
            ({"machine_name": "AlanYin的MacBook Pro"}, "macbook-pro"),
            ({"machine_name": "我的MacBook Air"}, "macbook-air"),
            ({"machine_name": "客厅的mac mini"}, "mac-mini"),
            ({"machine_name": "办公室iMac"}, "imac"),
            ({"machine_name": "机房Mac Pro"}, "mac-pro"),
            ({"machine_name": "Alan的MBP"}, "macbook-pro"),
            ({"machine_name": "出差MBA-15"}, "macbook-air"),
            # Generic and shared fallbacks without false positives
            ({"machine_name": "AlphaMac"}, "mac"),
            ({"machine_name": "StudioWorkstation"}, "mac"),
            ({"machine_id": "shared", "machine_name": "跨设备共享"}, "shared"),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(config_mod.classify_machine_type(**kwargs), expected)

    def test_detect_hardware_model_uses_ioreg_for_future_models_and_falls_back_to_system_profiler(
        self,
    ) -> None:
        future_plist = plistlib.dumps(
            [{"product-name": b"Mac mini (2026)\x00"}]
        )
        with (
            mock.patch.object(config_mod, "_detect_sysctl_hw_model", return_value="Mac17,99"),
            mock.patch.object(config_mod.subprocess, "check_output", return_value=future_plist),
        ):
            model, mtype = config_mod._detect_hardware_model()
        self.assertEqual(model, "Mac mini (2026)")
        self.assertEqual(mtype, "mac-mini")

        # Fallback to system_profiler when sysctl+ioreg cannot classify
        with (
            mock.patch.object(config_mod, "_detect_sysctl_hw_model", return_value="Mac99,1"),
            mock.patch.object(config_mod, "_detect_ioreg_product_name", return_value=""),
            mock.patch.object(
                config_mod,
                "_detect_system_profiler_hardware",
                return_value=("Mac99,1", "MacBook Air"),
            ),
        ):
            model2, mtype2 = config_mod._detect_hardware_model()
        self.assertEqual(model2, "MacBook Air")
        self.assertEqual(mtype2, "macbook-air")

    def test_legacy_machine_json_backfills_hardware_type_and_survives_rename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vsh-mtype-") as td:
            mfile = Path(td) / "machine.json"
            mfile.write_text(
                json.dumps(
                    {
                        "machine_id": "mac-studio01",
                        "machine_name": "Alan的Mac Studio",
                        "created_at": 1_700_000_000_000,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(config_mod, "_MACHINE_INFO_CACHE", None),
                mock.patch.object(
                    config_mod,
                    "_get_cached_hardware_model",
                    return_value=("Mac Studio (2023)", "mac-studio"),
                ),
            ):
                info = config_mod.get_machine_info(machine_file=mfile)
                self.assertEqual(info["machine_type"], "mac-studio")
                self.assertEqual(info["machine_model"], "Mac Studio (2023)")

                # Persisted on disk for subsequent boots
                persisted = json.loads(mfile.read_text(encoding="utf-8"))
                self.assertEqual(persisted["machine_type"], "mac-studio")
                self.assertEqual(persisted["machine_model"], "Mac Studio (2023)")

                # Renaming to a custom non-descriptive name keeps true hardware machine_type
                renamed = config_mod.update_machine_name("主力工作站", machine_file=mfile)
                self.assertEqual(renamed["machine_name"], "主力工作站")
                self.assertEqual(renamed["machine_type"], "mac-studio")
                self.assertEqual(renamed["machine_model"], "Mac Studio (2023)")


class TestMachineTypesEndToEndAndFrontend(unittest.TestCase):
    def test_facets_search_and_shard_export_report_accurate_machine_types(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vsh-micons-") as td:
            root = Path(td)
            paths = config_mod.HubPaths.from_state_dir(
                root / "state", icloud_root=root / "no-icloud"
            )
            paths.ensure_dirs(local_only=True)
            # Local machine is a Mac Studio renamed to a generic name
            paths.machine_file.write_text(
                json.dumps(
                    {
                        "machine_id": "mac-studio-local",
                        "machine_name": "工作室主机",
                        "machine_type": "mac-studio",
                        "machine_model": "Mac Studio (2023)",
                        "created_at": 1_700_000_000_000,
                    }
                ),
                encoding="utf-8",
            )

            # Remote legacy shard (no machine_type in _meta) for MacBook Pro
            _write_shard(
                paths.nodes_dir / "mac-mbp-remote.jsonl",
                {
                    "_meta": True,
                    "machine_id": "mac-mbp-remote",
                    "machine_name": "AlanYin的MacBook Pro",
                    "exported_at": 1_700_000_001_000,
                },
                [
                    _make_session(
                        "mac-mbp-remote",
                        "mbp-1",
                        "MacBook Pro 远端会话",
                        machine_name="AlanYin的MacBook Pro",
                    )
                ],
            )
            # Remote Mac mini shard with explicit machine_type and custom name
            _write_shard(
                paths.nodes_dir / "mac-mini-remote.jsonl",
                {
                    "_meta": True,
                    "machine_id": "mac-mini-remote",
                    "machine_name": "家庭节点",
                    "machine_type": "mac-mini",
                    "machine_model": "Mac mini (2024)",
                    "exported_at": 1_700_000_002_000,
                },
                [
                    _make_session(
                        "mac-mini-remote",
                        "mini-1",
                        "Mac mini 远端会话",
                        machine_name="家庭节点",
                    )
                ],
            )
            # Remote MacBook Air shard
            _write_shard(
                paths.nodes_dir / "mac-air-remote.jsonl",
                {
                    "_meta": True,
                    "machine_id": "mac-air-remote",
                    "machine_name": "Alan的MacBook Air",
                    "machine_type": "macbook-air",
                    "exported_at": 1_700_000_003_000,
                },
                [
                    _make_session(
                        "mac-air-remote",
                        "air-1",
                        "MacBook Air 远端会话",
                        machine_name="Alan的MacBook Air",
                    )
                ],
            )
            # Regression check: iMac machine with mixed 'imac' and generic 'mac' rows must resolve to 'imac'
            _write_shard(
                paths.nodes_dir / "mac-imac-remote.jsonl",
                {
                    "_meta": True,
                    "machine_id": "mac-imac-remote",
                    "machine_name": "设计台",
                    "machine_type": "imac",
                    "exported_at": 1_700_000_004_000,
                },
                [
                    _make_session(
                        "mac-imac-remote",
                        "imac-1",
                        "iMac 远端会话",
                        machine_name="设计台",
                    )
                ],
            )
            # Add a legacy manual link on the same iMac machine_id without machine_type
            (paths.manual_links_dir / "mac-imac-remote-links.jsonl").write_text(
                json.dumps(
                    {
                        "link_id": "link_imac_legacy",
                        "target_uid": "",
                        "tool": "gemini-web",
                        "title": "旧版手动链接",
                        "machine_id": "mac-imac-remote",
                        "machine_name": "设计台",
                        "created_at": 1_700_000_005_000,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            local_sessions = [
                _make_session(
                    "mac-studio-local",
                    "studio-1",
                    "Mac Studio 本机会话",
                    machine_name="工作室主机",
                )
            ]
            store = storage_mod.SessionHubStore(local_only=True, paths=paths)
            try:
                with mock.patch.object(
                    storage_mod, "collect_all_local_sessions", return_value=local_sessions
                ):
                    store.sync_now(force_local=True, skip_icloud=True)
                    # Second sync with identical data should NOT rewrite local shard
                    exported_again = store._export_local_shard(
                        "mac-studio-local",
                        "工作室主机",
                        local_sessions,
                        machine_type="mac-studio",
                        machine_model="Mac Studio (2023)",
                    )
                    self.assertFalse(exported_again)

                # Verify exported shard _meta contains machine_type & machine_model
                own_shard = paths.nodes_dir / "mac-studio-local.jsonl"
                meta = json.loads(own_shard.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(meta["machine_type"], "mac-studio")
                self.assertEqual(meta["machine_model"], "Mac Studio (2023)")

                srv = server_mod.create_http_server(store, host="127.0.0.1", port=0)
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                try:
                    port = srv.server_address[1]
                    with urlopen(f"http://127.0.0.1:{port}/api/facets", timeout=2) as resp:
                        facets = json.loads(resp.read().decode("utf-8"))
                    by_id = {m["machine_id"]: m for m in facets["machines"]}
                    self.assertEqual(by_id["mac-studio-local"]["machine_type"], "mac-studio")
                    self.assertEqual(by_id["mac-studio-local"]["machine_model"], "Mac Studio (2023)")
                    self.assertTrue(by_id["mac-studio-local"]["is_local"])
                    self.assertEqual(by_id["mac-mbp-remote"]["machine_type"], "macbook-pro")
                    self.assertEqual(by_id["mac-mini-remote"]["machine_type"], "mac-mini")
                    self.assertEqual(by_id["mac-air-remote"]["machine_type"], "macbook-air")
                    self.assertEqual(by_id["mac-imac-remote"]["machine_type"], "imac")

                    with urlopen(f"http://127.0.0.1:{port}/api/search", timeout=2) as resp:
                        search_res = json.loads(resp.read().decode("utf-8"))
                    item_types = {it["session_id"]: it["machine_type"] for it in search_res["items"]}
                    self.assertEqual(item_types["studio-1"], "mac-studio")
                    self.assertEqual(item_types["mbp-1"], "macbook-pro")
                    self.assertEqual(item_types["mini-1"], "mac-mini")
                    self.assertEqual(item_types["air-1"], "macbook-air")
                    self.assertEqual(item_types["imac-1"], "imac")

                    # Verify device-icons.js is served properly
                    with urlopen(f"http://127.0.0.1:{port}/js/device-icons.js", timeout=2) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertIn("javascript", resp.headers.get("Content-Type", ""))

                    # Verify /api/device-icons/<type>.png blocks traversal & unknown keys
                    for bad in ("unknown.png", "../config.py", "mac-studio.svg", "mac-studio"):
                        with self.assertRaises(HTTPError) as ctx:
                            urlopen(f"http://127.0.0.1:{port}/api/device-icons/{bad}", timeout=2)
                        self.assertEqual(ctx.exception.code, 404)

                    if platform.system() == "Darwin":
                        icon_payloads = {}
                        for kind in (
                            "mac-studio",
                            "mac-mini",
                            "macbook-pro",
                            "macbook-air",
                            "imac",
                            "mac-pro",
                        ):
                            with urlopen(
                                f"http://127.0.0.1:{port}/api/device-icons/{kind}.png",
                                timeout=5,
                            ) as resp:
                                self.assertEqual(resp.status, 200)
                                self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                                body = resp.read()
                                self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
                                icon_payloads[kind] = body
                        # HEAD must also succeed for allowed device icons
                        head_req = Request(
                            f"http://127.0.0.1:{port}/api/device-icons/mac-studio.png",
                            method="HEAD",
                        )
                        with urlopen(head_req, timeout=2) as resp:
                            self.assertEqual(resp.status, 200)
                            self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                            self.assertEqual(
                                int(resp.headers.get("Content-Length") or "0"),
                                len(icon_payloads["mac-studio"]),
                            )
                        self.assertEqual(len(set(icon_payloads.values())), 6)
                finally:
                    srv.shutdown()
                    srv.server_close()
            finally:
                store.close()

    def test_trim_png_transparent_margins_crops_outer_alpha_border(self) -> None:
        # Build a 20x20 RGBA PNG with an opaque 6x4 block centered at (7..12, 8..11)
        rows = []
        for y in range(20):
            row = bytearray(20 * 4)
            if 8 <= y <= 11:
                for x in range(7, 13):
                    row[x * 4 : x * 4 + 4] = b"\xcc\xd0\xd8\xff"
            rows.append(bytes(row))
        src = device_icons_mod._encode_png_rgba(20, 20, rows)
        trimmed = device_icons_mod.trim_png_transparent_margins(src, alpha_threshold=16, pad_px=1)
        decoded = device_icons_mod._read_png_rgba(trimmed)
        self.assertIsNotNone(decoded)
        w, h, _ = decoded
        # 6 + 2*1 = 8, 4 + 2*1 = 6
        self.assertEqual((w, h), (8, 6))

    def test_frontend_render_uses_device_icons_not_is_local_emojis(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        render_js = (repo_root / "static" / "js" / "render.js").read_text(encoding="utf-8")
        self.assertNotIn("💻", render_js)
        self.assertNotIn("🖥", render_js)
        self.assertIn("renderDeviceIcon", render_js)

        node_bin = shutil.which("node")
        if not node_bin:
            return
        device_icons_url = (repo_root / "static" / "js" / "device-icons.js").as_uri()
        script = f"""
import {{ resolveDeviceType, renderDeviceIcon }} from {json.dumps(device_icons_url)};
const cases = [
  [["mac-studio", "工作室主机"], "mac-studio"],
  [["", "Alan的Mac Studio"], "mac-studio"],
  [["", "AlanYin的MacBook Pro"], "macbook-pro"],
  [["", "我的MacBook Air"], "macbook-air"],
  [["", "远程Mac mini"], "mac-mini"],
  [["", "办公室iMac"], "imac"],
  [["", "机房Mac Pro"], "mac-pro"],
  [["", "跨设备共享", "shared"], "shared"],
];
for (const [args, expected] of cases) {{
  const got = resolveDeviceType(...args);
  if (got !== expected) {{
    throw new Error(`Expected ${{expected}} for ${{JSON.stringify(args)}}, got ${{got}}`);
  }}
  const html = renderDeviceIcon(args[0], args[1], "device-icon-sm", {{ machineId: args[2] || "" }});
  if (
    !html.includes(`data-device-type="${{expected}}"`) ||
    !html.includes(`/api/device-icons/${{expected}}.png`) ||
    !html.includes("<svg")
  ) {{
    throw new Error(`Invalid device icon HTML for ${{expected}}: ${{html}}`);
  }}
}}
// Ensure Mac Studio, Mac mini, MacBook Pro, MacBook Air, iMac, Mac Pro produce distinct icons
const distinct = new Set([
  renderDeviceIcon("mac-studio"),
  renderDeviceIcon("mac-mini"),
  renderDeviceIcon("macbook-pro"),
  renderDeviceIcon("macbook-air"),
  renderDeviceIcon("imac"),
  renderDeviceIcon("mac-pro"),
]);
if (distinct.size !== 6) {{
  throw new Error(`Expected 6 distinct hardware icons, got ${{distinct.size}}`);
}}
console.log("OK");
"""
        res = subprocess.run(
            [node_bin, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertEqual(res.stdout.strip(), "OK")


if __name__ == "__main__":
    unittest.main()
