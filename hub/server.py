from __future__ import annotations

import html
import json
import mimetypes
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    get_machine_info,
    get_preferences,
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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


class HubRequestHandler(BaseHTTPRequestHandler):
    store: SessionHubStore

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _verify_same_origin_post(self) -> bool:
        if self.headers.get("X-Vibe-Hub") != "1":
            return False
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = (self.headers.get("Origin") or "").lower()
        if origin and not (
            origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
        ):
            return False
        return True

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 256_000:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            health = self.store.get_sync_health()
            self._send_json(
                {
                    "ok": True,
                    "machine": get_machine_info(),
                    **health,
                }
            )
            return

        if path == "/api/facets":
            facets = self.store.get_facets()
            facets["preferences"] = get_preferences()
            self._send_json(facets)
            return

        if path == "/api/preferences":
            self._send_json({"ok": True, "preferences": get_preferences()})
            return

        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            tool = (qs.get("tool") or [""])[0]
            machine_id = (qs.get("machine_id") or [""])[0]
            only_pr = (qs.get("only_pr") or ["0"])[0] == "1"
            remote_pr = (qs.get("remote_pr") or ["0"])[0] == "1"
            res = self.store.search(
                query=q,
                tool=tool,
                machine_id=machine_id,
                only_with_pr=only_pr,
                resolve_remote_pr=remote_pr,
            )
            self._send_json(res)
            return

        if path == "/jump":
            uid = (qs.get("uid") or [""])[0].strip()
            tool = (qs.get("tool") or [""])[0].strip()
            sid = (qs.get("id") or [""])[0].strip()

            session = self.store.get_session_by_uid(uid) if uid else None
            if session is None and tool and sid:
                minfo = get_machine_info()
                local_uid = f"{minfo['machine_id']}:{tool}:{sid}"
                session = self.store.get_session_by_uid(local_uid)

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
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        file_path = (STATIC_DIR / rel).resolve()
        if not str(file_path).startswith(str(STATIC_DIR)) or not file_path.is_file():
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

        if path == "/api/sync":
            bridge_synced = bool(body.get("bridge_synced", False))
            skip_local = bool(body.get("skip_local", bridge_synced))
            skip_icloud = bool(body.get("skip_icloud", bridge_synced))
            force_local = bool(body.get("force_local", not skip_local))
            force_reindex = bool(body.get("force_reindex", False))
            res = self.store.sync_now(
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
            session = self.store.get_session_by_uid(uid) if uid else None
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
            entry = self.store.add_manual_link(body)
            self._send_json({"ok": True, "entry": entry})
            return

        if path == "/api/machine-name":
            new_name = str(body.get("machine_name") or "").strip()
            info = update_machine_name(new_name)
            self.store.sync_now(force_local=True)
            self._send_json({"ok": True, "machine": info})
            return

        if path == "/api/copy":
            copy_to_clipboard(str(body.get("text") or ""))
            self._send_json({"ok": True})
            return

        if path == "/api/preferences":
            theme = str(body.get("theme") or "").strip() or None
            prefs = update_preferences(theme=theme)
            self._send_json({"ok": True, "preferences": prefs})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def _background_sync_loop(store: SessionHubStore, interval_sec: int = 90) -> None:
    while True:
        try:
            store.sync_now(force_local=True)
        except Exception:
            pass
        time.sleep(interval_sec)


def main() -> None:
    port = DEFAULT_PORT
    for i, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    open_browser = "--open" in sys.argv
    sync_only = "--sync-once" in sys.argv
    local_only = "--local-only" in sys.argv

    store = SessionHubStore(local_only=local_only)
    info = store.sync_now(force_local=True)
    if sync_only:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    HubRequestHandler.store = store
    bg = threading.Thread(target=_background_sync_loop, args=(store, 90), daemon=True)
    bg.start()

    server = ThreadingHTTPServer((DEFAULT_HOST, port), HubRequestHandler)
    url = f"http://{DEFAULT_HOST}:{port}"
    print(
        f"Vibe Session Hub listening on {url} ({info['total_sessions']} sessions indexed)",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
