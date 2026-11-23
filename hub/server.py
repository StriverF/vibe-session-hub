from __future__ import annotations

import argparse
import html
import json
import logging
import mimetypes
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    configure_logging,
    get_machine_info,
    get_preferences,
    resolve_configured_port,
    update_machine_name,
    update_preferences,
)
from .launcher import (
    APPROVED_DEEP_LINK_SCHEMES,
    build_native_deep_link,
    copy_to_clipboard,
    open_verified_session_locally,
)
from .storage import SessionHubStore
from .tool_registry import export_client_tool_catalog

logger = logging.getLogger("vibe_session_hub.server")

STATIC_DIR = (Path(__file__).resolve().parent.parent / "static").resolve()


def is_path_within(candidate: Path, base_dir: Path) -> bool:
    """Safely verify that `candidate` resolves strictly inside `base_dir`."""
    try:
        resolved_base = base_dir.resolve()
        resolved_candidate = candidate.resolve()
    except (OSError, ValueError):
        return False
    if hasattr(resolved_candidate, "is_relative_to"):
        return resolved_candidate.is_relative_to(resolved_base)
    try:
        resolved_candidate.relative_to(resolved_base)
        return True
    except ValueError:
        return False


def resolve_static_file(
    request_path: str, static_dir: Path = STATIC_DIR
) -> Path | None:
    """Decode and validate a static asset request path against `static_dir`.

    Prevents directory traversal (`../`, `%2e%2e%2f`), sibling prefix collisions
    (`/static_secret`), NUL bytes, backslash tricks, and symlink escapes.
    """
    try:
        decoded = unquote(request_path)
    except Exception:
        return None
    if "\x00" in decoded or "\\" in decoded:
        return None

    if decoded in ("", "/"):
        rel = "index.html"
    else:
        rel = decoded.lstrip("/")
        if not rel or Path(rel).is_absolute():
            return None

    try:
        base = static_dir.resolve()
        candidate = (base / rel).resolve()
    except (OSError, ValueError):
        return None

    if candidate == base or not is_path_within(candidate, base):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _render_jump_html(
    verified_uid: str,
    title: str,
    subtitle: str,
    deep_link: str = "",
) -> bytes:
    parsed_dl = urlparse(deep_link) if deep_link else None
    safe_dl = (
        deep_link
        if (
            parsed_dl
            and parsed_dl.scheme in (APPROVED_DEEP_LINK_SCHEMES | {"https", "http"})
        )
        else ""
    )
    badge_color = "#10b981" if verified_uid else "#f59e0b"
    status_text = (
        "已找到已验证的索引会话，点击下方按钮在桌面端打开"
        if verified_uid
        else "未在当前设备索引中找到该会话（请先点同步索引）"
    )
    open_btn_html = (
        f'<button id="confirmOpenBtn" class="btn" data-uid="{html.escape(verified_uid)}">在桌面客户端中打开会话</button>'
        if verified_uid
        else (
            f'<a class="btn" href="{html.escape(safe_dl)}">通过客户端协议打开</a>'
            if safe_dl
            else ""
        )
    )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Vibe Session Hub · 会话跳转确认</title>
  <link rel="icon" type="image/png" href="/icons/logo.png" />
  <style>
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #0b0c10; color: #f3f4f8; display: grid; place-items: center; min-height: 100vh;
    }}
    .card {{
      max-width: 520px; width: calc(100% - 40px); background: #12131a;
      border: 1px solid #252838; border-radius: 16px; padding: 28px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.48);
    }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: {badge_color}; margin-right: 8px; }}
    .status {{ font-size: 13px; color: #a5a8b8; display: flex; align-items: center; margin-bottom: 12px; }}
    h1 {{ font-size: 18px; margin: 0 0 8px; line-height: 1.4; }}
    p {{ font-size: 14px; color: #a5a8b8; margin: 0 0 20px; word-break: break-all; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .btn {{
      text-decoration: none; font-size: 13px; font-weight: 500; padding: 9px 15px;
      border-radius: 9px; background: linear-gradient(135deg, #5668f8 0%, #8b5cf6 52%, #d946ef 100%);
      color: #fff; border: none; cursor: pointer; box-shadow: 0 3px 14px rgba(139, 92, 246, 0.28);
    }}
    .btn.secondary {{ background: #1a1c26; color: #e4e4e7; border: 1px solid #252838; box-shadow: none; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="status"><span class="dot"></span><span id="statusLabel">{html.escape(status_text)}</span></div>
    <h1>{html.escape(title)}</h1>
    <p id="subText">{html.escape(subtitle)}</p>
    <div class="actions">
      {open_btn_html}
      <a class="btn secondary" href="/">返回搜索面板</a>
    </div>
  </div>
  <script>
    const btn = document.getElementById("confirmOpenBtn");
    if (btn) {{
      btn.addEventListener("click", async () => {{
        btn.disabled = true;
        const res = await fetch("/api/open", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "X-Vibe-Hub": "1" }},
          body: JSON.stringify({{ uid: btn.dataset.uid }})
        }});
        const data = await res.json();
        document.getElementById("subText").textContent = data.message || "已发送打开请求";
        btn.disabled = false;
      }});
    }}
  </script>
</body>
</html>"""
    return page.encode("utf-8")


class HubHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying an injected SessionHubStore instance."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        store: SessionHubStore,
    ) -> None:
        self.store = store
        super().__init__(server_address, handler_class)


class HubRequestHandler(BaseHTTPRequestHandler):
    store: SessionHubStore

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _get_store(self) -> SessionHubStore:
        injected = getattr(self.server, "store", None)
        if injected is not None:
            return injected
        return self.store

    def _get_machine_info(self) -> dict[str, Any]:
        store = self._get_store()
        if getattr(store, "_has_explicit_paths", False):
            return get_machine_info(machine_file=store.paths.machine_file)
        return get_machine_info()

    def _update_machine_name(self, new_name: str) -> dict[str, Any]:
        store = self._get_store()
        if getattr(store, "_has_explicit_paths", False):
            return update_machine_name(new_name, machine_file=store.paths.machine_file)
        return update_machine_name(new_name)

    def _get_preferences(self) -> dict[str, str]:
        store = self._get_store()
        if getattr(store, "_has_explicit_paths", False):
            return get_preferences(preferences_file=store.paths.preferences_file)
        return get_preferences()

    def _update_preferences(self, theme: str | None) -> dict[str, Any]:
        store = self._get_store()
        if getattr(store, "_has_explicit_paths", False):
            return update_preferences(
                theme=theme, preferences_file=store.paths.preferences_file
            )
        return update_preferences(theme=theme)

    def _get_server_info(self) -> dict[str, Any]:
        addr = getattr(self.server, "server_address", (DEFAULT_HOST, DEFAULT_PORT))
        host = str(addr[0] or DEFAULT_HOST) if isinstance(addr, tuple) and addr else DEFAULT_HOST
        port = int(getattr(self.server, "server_port", DEFAULT_PORT) or DEFAULT_PORT)
        return {
            "host": host,
            "port": port,
            "base_url": f"http://{host}:{port}",
            "version": __version__,
        }

    def _verify_same_origin_post(self) -> bool:
        if self.headers.get("X-Vibe-Hub") != "1":
            return False

        raw_host = (self.headers.get("Host") or "").strip()
        if not raw_host:
            return False
        try:
            parsed_host = urlparse(f"http://{raw_host}")
        except Exception:
            return False
        if (
            parsed_host.hostname not in ("127.0.0.1", "localhost")
            or parsed_host.username is not None
            or parsed_host.password is not None
            or parsed_host.path not in ("", "/")
        ):
            return False
        server_port = int(getattr(self.server, "server_port", 0) or 0)
        try:
            host_port = parsed_host.port
        except ValueError:
            return False
        if server_port and host_port is not None and host_port != server_port:
            return False

        raw_origin = (self.headers.get("Origin") or "").strip()
        if raw_origin:
            try:
                parsed_origin = urlparse(raw_origin)
                origin_port = parsed_origin.port
            except Exception:
                return False
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname not in ("127.0.0.1", "localhost")
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in ("", "/")
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                return False
            if server_port and (origin_port or 80) != server_port:
                return False
        return True

    def _send_json(self, payload: Any, status: int = 200, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 256_000:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            store = self._get_store()
            health = store.get_sync_health()
            server_info = self._get_server_info()
            self._send_json(
                {
                    "ok": True,
                    "version": __version__,
                    "port": server_info["port"],
                    "server": server_info,
                    "machine": self._get_machine_info(),
                    **health,
                },
                head_only=True,
            )
            return
        if path.startswith("/api/"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed")
            return
        self._serve_static(path, head_only=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path.startswith("/api/"):
            try:
                self._handle_api_get(path, qs)
            except Exception as exc:
                logger.exception("Unhandled error in GET %s", path)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/jump":
            self._handle_jump(qs)
            return

        self._serve_static(path)

    def _handle_api_get(self, path: str, qs: dict[str, list[str]]) -> None:
        store = self._get_store()

        if path == "/api/health":
            health = store.get_sync_health()
            server_info = self._get_server_info()
            self._send_json(
                {
                    "ok": True,
                    "version": __version__,
                    "port": server_info["port"],
                    "server": server_info,
                    "machine": self._get_machine_info(),
                    **health,
                }
            )
            return

        if path == "/api/tools":
            self._send_json({"ok": True, **export_client_tool_catalog()})
            return

        if path == "/api/facets":
            facets = store.get_facets()
            facets["preferences"] = self._get_preferences()
            facets["tool_catalog"] = export_client_tool_catalog()
            facets["server"] = self._get_server_info()
            self._send_json(facets)
            return

        if path == "/api/preferences":
            self._send_json({"ok": True, "preferences": self._get_preferences()})
            return

        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            tool = (qs.get("tool") or [""])[0]
            machine_id = (qs.get("machine_id") or [""])[0]
            only_pr = (qs.get("only_pr") or ["0"])[0] == "1"
            remote_pr = (qs.get("remote_pr") or ["0"])[0] == "1"
            res = store.search(
                query=q,
                tool=tool,
                machine_id=machine_id,
                only_with_pr=only_pr,
                resolve_remote_pr=remote_pr,
            )
            self._send_json(res)
            return

        self._send_json({"ok": False, "error": "Not Found"}, status=404)

    def _handle_jump(self, qs: dict[str, list[str]]) -> None:
        store = self._get_store()
        uid = (qs.get("uid") or [""])[0].strip()
        tool = (qs.get("tool") or [""])[0].strip()
        sid = (qs.get("id") or [""])[0].strip()

        session = store.get_session_by_uid(uid) if uid else None
        if session is None and tool and sid:
            minfo = self._get_machine_info()
            local_uid = f"{minfo['machine_id']}:{tool}:{sid}"
            session = store.get_session_by_uid(local_uid)

        if session is not None:
            dl = build_native_deep_link(
                session.get("tool", ""),
                session.get("session_id", ""),
                session.get("external_url", ""),
            )
            body = _render_jump_html(
                verified_uid=str(session["uid"]),
                title=session.get("title") or session.get("session_id") or "打开会话",
                subtitle=f"{session.get('tool_label', '')} · {session.get('session_id', '')}",
                deep_link=dl,
            )
        else:
            dl = build_native_deep_link(tool, sid)
            body = _render_jump_html(
                verified_uid="",
                title=f"{tool} · {sid}" if tool else "未找到会话",
                subtitle="若刚从另一台电脑创建该会话，请返回主页点击「同步索引」后再试。",
                deep_link=dl,
            )

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        file_path = resolve_static_file(path, static_dir=STATIC_DIR)
        if file_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        ctype, _ = mimetypes.guess_type(str(file_path))
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{ctype or 'application/octet-stream'}; charset=utf-8"
            if (ctype or "").startswith(("text/", "application/javascript"))
            else (ctype or "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_POST(self) -> None:
        if not self._verify_same_origin_post():
            self._send_json({"ok": False, "error": "Forbidden"}, status=403)
            return

        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        try:
            self._handle_api_post(path, body)
        except Exception as exc:
            logger.exception("Unhandled error in POST %s", path)
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def _handle_api_post(self, path: str, body: dict[str, Any]) -> None:
        store = self._get_store()

        if path == "/api/sync":
            bridge_synced = bool(body.get("bridge_synced", False))
            skip_local = bool(body.get("skip_local", bridge_synced))
            skip_icloud = bool(body.get("skip_icloud", bridge_synced))
            force_local = bool(body.get("force_local", not skip_local))
            force_reindex = bool(body.get("force_reindex", False))
            res = store.sync_now(
                force_local=force_local,
                force_reindex=force_reindex,
                skip_local=skip_local,
                skip_icloud=skip_icloud,
                bridge_synced=bridge_synced,
            )
            self._send_json(res)
            return

        if path == "/api/open":
            uid = str(body.get("uid") or "").strip()
            session = store.get_session_by_uid(uid) if uid else None
            if session is None:
                self._send_json(
                    {"ok": False, "message": "拒绝打开：未在已验证的索引会话中找到该记录"},
                    status=404,
                )
                return
            res = open_verified_session_locally(session)
            self._send_json(res)
            return

        if path == "/api/manual-link":
            entry = store.add_manual_link(body)
            self._send_json({"ok": True, "entry": entry})
            return

        if path == "/api/machine-name":
            new_name = str(body.get("machine_name") or "").strip()
            info = self._update_machine_name(new_name)
            store.sync_now(force_local=True)
            self._send_json({"ok": True, "machine": info})
            return

        if path == "/api/copy":
            copy_to_clipboard(str(body.get("text") or ""))
            self._send_json({"ok": True})
            return

        if path == "/api/preferences":
            theme = str(body.get("theme") or "").strip() or None
            prefs = self._update_preferences(theme=theme)
            self._send_json({"ok": True, "preferences": prefs})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def create_http_server(
    store: SessionHubStore,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> HubHTTPServer:
    """Create a ThreadingHTTPServer bound to the given SessionHubStore without global state."""
    return HubHTTPServer((host, port), HubRequestHandler, store=store)


def _background_sync_loop(store: SessionHubStore, interval_sec: int = 90) -> None:
    while True:
        try:
            store.sync_now(force_local=True)
        except Exception as exc:
            logger.warning("Background sync iteration failed: %s", exc)
        time.sleep(interval_sec)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vibe Session Hub local server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--open", dest="open_browser", action="store_true")
    parser.add_argument("--sync-once", dest="sync_only", action="store_true")
    parser.add_argument("--local-only", dest="local_only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--cwd", type=str, default="", help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging(verbose=args.verbose)

    store = SessionHubStore(local_only=args.local_only)
    info = store.sync_now(force_local=True)
    if args.sync_only:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        store.close()
        return

    listen_port = resolve_configured_port(
        cli_port=args.port,
        preferences_file=store.paths.preferences_file,
    )
    HubRequestHandler.store = store
    bg = threading.Thread(target=_background_sync_loop, args=(store, 90), daemon=True)
    bg.start()

    server = create_http_server(store, host=DEFAULT_HOST, port=listen_port)
    url = f"http://{DEFAULT_HOST}:{server.server_port}"
    print(
        f"Vibe Session Hub v{__version__} listening on {url} ({info['total_sessions']} sessions indexed)",
        flush=True,
    )
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
