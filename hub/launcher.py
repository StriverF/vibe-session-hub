from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .tool_registry import APPROVED_DEEP_LINK_SCHEMES, get_tool_spec

logger = logging.getLogger("vibe_session_hub.launcher")

SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bundle_id(tool_id: str) -> str:
    spec = get_tool_spec(tool_id)
    return spec.bundle_id if spec else ""


def _is_safe_http_url(url: str) -> bool:
    if not url or url.startswith("-"):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("https", "http") and bool(parsed.netloc)
    except Exception:
        return False


def _is_safe_session_id(session_id: str) -> bool:
    return bool(session_id and SAFE_SESSION_ID_RE.match(session_id))


def _resolve_existing_dir(cwd: str) -> str:
    if not cwd or not os.path.isabs(cwd) or cwd.startswith("-"):
        return ""
    try:
        p = Path(cwd).resolve()
        return str(p) if p.is_dir() else ""
    except OSError:
        return ""


def _run_open(args: list[str]) -> tuple[bool, str]:
    try:
        subprocess.run(args, check=True, timeout=5)
        return True, "ok"
    except Exception as exc:
        logger.debug("Command %s failed: %s", args, exc)
        return False, str(exc)


def _find_send_url_helper() -> str | None:
    candidates = [
        PROJECT_ROOT / "macos" / "bin" / "send_url_to_pid",
        PROJECT_ROOT.parent
        / "Vibe Session Hub.app"
        / "Contents"
        / "MacOS"
        / "send_url_to_pid",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _find_process_pid(matcher: Callable[[str], bool]) -> int | None:
    try:
        out = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,command="], text=True, timeout=2
        )
    except Exception as exc:
        logger.debug("ps process scan failed: %s", exc)
        return None
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        if matcher(cmd):
            try:
                return int(pid_str)
            except ValueError:
                continue
    return None


def _find_claude_magpie_main_pid() -> int | None:
    target_ud = os.path.expanduser("~/Library/Application Support/Claude-Magpie-3p")
    return _find_process_pid(
        lambda cmd: f"--user-data-dir={target_ud}" in cmd
        and "/Contents/MacOS/Claude" in cmd
        and "--type=" not in cmd
    )


def _find_official_claude_main_pid() -> int | None:
    return _find_process_pid(
        lambda cmd: cmd.startswith("/Applications/Claude.app/Contents/MacOS/Claude")
        and "--user-data-dir=" not in cmd
        and "--type=" not in cmd
    )


def _find_cursor_main_pid() -> int | None:
    return _find_process_pid(
        lambda cmd: "/Cursor.app/Contents/MacOS/Cursor" in cmd and "--type=" not in cmd
    )


def _send_url_to_pid(pid: int, deep_link: str) -> tuple[bool, str]:
    helper = _find_send_url_helper()
    if not helper:
        return False, "未找到 send_url_to_pid 辅助程序，请重新运行 install.sh"
    try:
        res = subprocess.run(
            [helper, str(pid), deep_link],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return True, "ok"
        return False, (res.stderr or res.stdout or f"exit {res.returncode}").strip()
    except Exception as exc:
        logger.debug("send_url_to_pid failed for pid=%s: %s", pid, exc)
        return False, str(exc)


def _resolve_cursor_local_deep_link(session_id: str) -> str:
    if not _is_safe_session_id(session_id):
        return ""
    if session_id.startswith("bc-"):
        return f"cursor://anysphere.cursor-deeplink/background-agent?bcId={session_id}"

    gs_db = Path(
        os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
    )
    if not gs_db.is_file():
        return f"cursor://anysphere.cursor-deeplink/agent?id={session_id}"

    quoted = quote(str(gs_db))
    con: sqlite3.Connection | None = None
    try:
        try:
            con = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=1.5)
        except Exception:
            con = sqlite3.connect(f"file:{quoted}?immutable=1", uri=True, timeout=1.5)

        has_headers = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'composerHeaders'"
        ).fetchone()
        if not has_headers:
            return f"cursor://anysphere.cursor-deeplink/agent?id={session_id}"

        target_id = session_id
        for _ in range(5):
            row = con.execute(
                "SELECT value FROM composerHeaders WHERE composerId = ?",
                (target_id,),
            ).fetchone()
            if row and row[0]:
                raw = (
                    row[0]
                    if isinstance(row[0], str)
                    else row[0].decode("utf-8", "ignore")
                )
                try:
                    jd = json.loads(raw)
                except Exception:
                    jd = {}
                parent_id = str(jd.get("subagentParentId") or "").strip()
                if (
                    parent_id
                    and _is_safe_session_id(parent_id)
                    and parent_id != target_id
                ):
                    target_id = parent_id
                    continue
                return f"cursor://anysphere.cursor-deeplink/agent?id={target_id}"

            kv_row = con.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{target_id}",),
            ).fetchone()
            if kv_row and kv_row[0]:
                raw_kv = (
                    kv_row[0]
                    if isinstance(kv_row[0], str)
                    else kv_row[0].decode("utf-8", "ignore")
                )
                try:
                    kv_jd = json.loads(raw_kv)
                except Exception:
                    kv_jd = {}
                sub_info = kv_jd.get("subagentInfo")
                parent_id = ""
                if isinstance(sub_info, dict):
                    parent_id = str(sub_info.get("parentComposerId") or "").strip()
                if not parent_id:
                    parent_id = str(kv_jd.get("subagentParentId") or "").strip()
                if (
                    parent_id
                    and _is_safe_session_id(parent_id)
                    and parent_id != target_id
                ):
                    target_id = parent_id
                    continue
            break

        # Legacy pre-Agents-Window IDE workspace sessions not present in composerHeaders:
        # return empty so caller opens the workspace folder directly.
        return ""
    except Exception as exc:
        logger.debug("Cursor deep-link lookup fallback for %s: %s", session_id, exc)
        return f"cursor://anysphere.cursor-deeplink/agent?id={session_id}"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def build_native_deep_link(tool: str, session_id: str, external_url: str = "") -> str:
    if external_url:
        return external_url if _is_safe_http_url(external_url) else ""
    if not _is_safe_session_id(session_id):
        return ""
    if tool in ("claude-magpie", "claude-desktop"):
        sid = session_id if session_id.startswith("local_") else f"local_{session_id}"
        return f"claude://claude.ai/epitaxy/{sid}"
    if tool == "codex":
        return f"codex://threads/{session_id}"
    if tool == "grok-bot":
        return f"grokbot://app/v1/agent?id={session_id}"
    if tool == "cursor":
        if session_id.startswith("bc-"):
            return f"cursor://anysphere.cursor-deeplink/background-agent?bcId={session_id}"
        return f"cursor://anysphere.cursor-deeplink/agent?id={session_id}"
    if tool == "gemini":
        if session_id.startswith("c_"):
            return f"googlegemini://conversation/{session_id}"
        return ""
    return ""


def copy_to_clipboard(text: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/pbcopy"],
            input=text[:32000].encode("utf-8"),
            check=False,
            timeout=2,
        )
    except Exception as exc:
        logger.debug("pbcopy failed: %s", exc)


def _open_claude_magpie(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    deep_link = build_native_deep_link("claude-magpie", session_id)
    if not deep_link:
        return {"ok": False, "message": "无效的会话 ID", "action": "blocked"}

    pid = _find_claude_magpie_main_pid()
    cold_started = False
    if pid is None:
        launcher_app = os.path.expanduser("~/Applications/Claude Magpie.app")
        if not os.path.isdir(launcher_app):
            launcher_app = "/Applications/Claude Magpie.app"
        if not os.path.isdir(launcher_app):
            return {"ok": False, "message": "未找到 Claude Magpie.app", "action": "error"}
        ok_launch, msg_launch = _run_open(["/usr/bin/open", "-a", launcher_app])
        if not ok_launch:
            return {"ok": False, "message": msg_launch, "action": "error"}
        cold_started = True
        for _ in range(28):
            time.sleep(0.25)
            pid = _find_claude_magpie_main_pid()
            if pid is not None:
                break
        if pid is not None:
            time.sleep(1.0)

    if pid is None:
        return {
            "ok": False,
            "message": "等待 Claude Magpie 主进程启动超时",
            "action": "error",
        }

    ok, msg = _send_url_to_pid(pid, deep_link)
    return {
        "ok": ok,
        "message": "已唤起 Claude Magpie 并定位到该会话" if ok else f"定位失败: {msg}",
        "action": "deep_link_pid",
        "target": deep_link,
        "pid": pid,
        "cold_started": cold_started,
    }


def _open_claude_desktop(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    deep_link = build_native_deep_link("claude-desktop", session_id)
    if not deep_link:
        return {"ok": False, "message": "无效的会话 ID", "action": "blocked"}
    pid = _find_official_claude_main_pid()
    if pid is not None:
        ok, _ = _send_url_to_pid(pid, deep_link)
        if ok:
            return {
                "ok": True,
                "message": "已唤起 Claude 官方桌面端并定位会话",
                "action": "deep_link_pid",
                "target": deep_link,
            }
    ok, msg = _run_open(
        ["/usr/bin/open", "-b", _bundle_id("claude-desktop"), deep_link]
    )
    return {
        "ok": ok,
        "message": "已唤起 Claude 官方桌面端并定位会话" if ok else msg,
        "action": "deep_link",
        "target": deep_link,
    }


def _open_bundle_deep_link(
    tool: str, session_id: str, success_message: str
) -> dict[str, Any]:
    deep_link = build_native_deep_link(tool, session_id)
    if not deep_link:
        return {"ok": False, "message": "无效的会话 ID", "action": "blocked"}
    ok, msg = _run_open(["/usr/bin/open", "-b", _bundle_id(tool), deep_link])
    return {
        "ok": ok,
        "message": success_message if ok else msg,
        "action": "deep_link",
        "target": deep_link,
    }


def _open_codex(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    return _open_bundle_deep_link("codex", session_id, "已唤起 Codex 桌面端并定位会话")


def _open_grok_bot(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    return _open_bundle_deep_link(
        "grok-bot", session_id, "已唤起 Grok Bot 桌面端并定位会话"
    )


def _open_antigravity(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    if title:
        copy_to_clipboard(title)
    target_dir = _resolve_existing_dir(cwd)
    cmd = ["/usr/bin/open", "-b", _bundle_id("antigravity")]
    if target_dir:
        cmd.extend(["--", target_dir])
    ok, msg = _run_open(cmd)
    return {
        "ok": ok,
        "message": "已打开 Antigravity 对应工作区（会话标题已复制，可直接粘贴定位）"
        if ok
        else msg,
        "action": "open_workspace",
        "target": target_dir or "Antigravity.app",
    }


def _open_cursor(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    bundle = _bundle_id("cursor")
    deep_link = _resolve_cursor_local_deep_link(session_id)
    if deep_link:
        was_running = _find_cursor_main_pid() is not None
        open_cmd = ["/usr/bin/open", "-b", bundle, deep_link]
        ok, msg = _run_open(open_cmd)
        if not ok and os.path.isdir("/Applications/Cursor.app"):
            open_cmd = ["/usr/bin/open", "-a", "/Applications/Cursor.app", deep_link]
            ok, msg = _run_open(open_cmd)
        if ok and not was_running:
            for _ in range(16):
                time.sleep(0.2)
                if _find_cursor_main_pid() is not None:
                    break
            time.sleep(1.8)
            _run_open(open_cmd)
        return {
            "ok": ok,
            "message": "已唤起 Cursor 并精准打开对应会话" if ok else msg,
            "action": "deep_link",
            "target": deep_link,
            "cold_started": not was_running,
        }
    if title:
        copy_to_clipboard(title)
    target_dir = _resolve_existing_dir(cwd)
    cmd = ["/usr/bin/open", "-b", bundle]
    if target_dir:
        cmd.extend(["--", target_dir])
    ok, msg = _run_open(cmd)
    if not ok and os.path.isdir("/Applications/Cursor.app"):
        fallback_cmd = ["/usr/bin/open", "-a", "/Applications/Cursor.app"]
        if target_dir:
            fallback_cmd.extend(["--", target_dir])
        ok, msg = _run_open(fallback_cmd)
    return {
        "ok": ok,
        "message": "已打开 Cursor 早期历史工作区（会话标题已复制，可在历史中粘贴定位）"
        if ok
        else msg,
        "action": "open_workspace",
        "target": target_dir or "Cursor.app",
    }


def _open_gemini(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    bundle = _bundle_id("gemini")
    deep_link = build_native_deep_link("gemini", session_id)
    if deep_link:
        ok, msg = _run_open(["/usr/bin/open", "-b", bundle, deep_link])
        if ok:
            return {
                "ok": True,
                "message": "已唤起 Gemini 桌面端并定位到该会话",
                "action": "deep_link",
                "target": deep_link,
            }
        web_id = session_id.removeprefix("c_")
        if _is_safe_session_id(web_id):
            web_url = f"https://gemini.google.com/app/{web_id}"
            ok_w, msg_w = _run_open(["/usr/bin/open", "--", web_url])
            return {
                "ok": ok_w,
                "message": "未检测到 Gemini 桌面端，已在浏览器打开该 Gemini 会话"
                if ok_w
                else msg_w,
                "action": "open_url",
                "target": web_url,
            }
    if title:
        copy_to_clipboard(title)
    ok, msg = _run_open(["/usr/bin/open", "-b", bundle])
    return {
        "ok": ok,
        "message": "已打开 Gemini 桌面端（会话标题已复制）" if ok else msg,
        "action": "open_app",
        "target": "Gemini.app",
    }


LauncherFn = Callable[[str, str, str], dict[str, Any]]

_TOOL_LAUNCHERS: dict[str, LauncherFn] = {
    "claude-magpie": _open_claude_magpie,
    "claude-desktop": _open_claude_desktop,
    "codex": _open_codex,
    "grok-bot": _open_grok_bot,
    "antigravity": _open_antigravity,
    "cursor": _open_cursor,
    "gemini": _open_gemini,
}


def open_verified_session_locally(session: dict[str, Any]) -> dict[str, Any]:
    tool = str(session.get("tool") or "")
    session_id = str(session.get("session_id") or "")
    cwd = str(session.get("cwd") or "")
    title = str(session.get("title") or "")
    external_url = str(session.get("external_url") or "")

    if external_url:
        if not _is_safe_http_url(external_url):
            return {
                "ok": False,
                "message": "仅允许打开 http/https 外部会话链接",
                "action": "blocked",
            }
        ok, msg = _run_open(["/usr/bin/open", "--", external_url])
        return {
            "ok": ok,
            "message": msg,
            "action": "open_url",
            "target": external_url,
        }

    handler = _TOOL_LAUNCHERS.get(tool)
    if handler is None:
        return {"ok": False, "message": f"不支持的工具类型: {tool}", "action": "none"}
    return handler(session_id, cwd, title)


__all__ = [
    "APPROVED_DEEP_LINK_SCHEMES",
    "SAFE_SESSION_ID_RE",
    "build_native_deep_link",
    "copy_to_clipboard",
    "open_verified_session_locally",
]
