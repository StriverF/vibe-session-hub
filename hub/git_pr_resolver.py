from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("vibe_session_hub.git_pr_resolver")

PR_QUERY_RE = re.compile(
    r"^(?:pr\s*#?|pull/|#)?(\d{2,7})$|"
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


def parse_pr_query(query: str) -> tuple[str | None, str | None]:
    q = query.strip()
    if not q:
        return None, None
    m = PR_QUERY_RE.search(q)
    if not m:
        m_qual = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{2,7})$", q)
        if m_qual:
            return m_qual.group(2), m_qual.group(1)
        return None, None
    if m.group(1):
        return m.group(1), None
    return m.group(3), m.group(2)


def resolve_pr_from_local_git(
    pr_number: str,
    candidate_repos: list[str],
    total_budget_sec: float = 2.5,
) -> dict[str, Any] | None:
    seen: set[str] = set()
    deadline = time.monotonic() + max(0.2, total_budget_sec)
    for repo_dir in candidate_repos:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        if not repo_dir or repo_dir in seen:
            continue
        seen.add(repo_dir)
        p = Path(repo_dir)
        if not p.exists():
            continue
        if not any((anc / ".git").exists() for anc in (p, *p.parents)):
            continue
        try:
            out = subprocess.check_output(
                [
                    "/usr/bin/git",
                    "-C",
                    str(p),
                    "log",
                    "--all",
                    "-n",
                    "1",
                    f"--grep=(#{pr_number})",
                    "--fixed-strings",
                    "--format=%H%x1f%s",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=min(1.5, remaining),
            ).strip()
            if not out:
                continue
            sha, subject = out.split("\x1f", 1)
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                files = []
            else:
                files_out = subprocess.check_output(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(p),
                        "show",
                        "--name-only",
                        "--format=",
                        sha,
                    ],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=min(1.5, remaining),
                ).strip()
                files = [f.strip() for f in files_out.splitlines() if f.strip()][:12]
            clean_title = re.sub(rf"\s*\(#{pr_number}\)\s*$", "", subject).strip()
            return {
                "pr_number": pr_number,
                "title": clean_title,
                "branch": "",
                "url": "",
                "repo_dir": str(p),
                "files": files,
                "source": "git_log",
                "resolved_at": int(time.time() * 1000),
            }
        except Exception as exc:
            logger.debug("git log PR lookup failed in %s: %s", p, exc)
            continue
    return None


def resolve_pr_via_gh(
    pr_number: str, repo_slug: str | None, candidate_repos: list[str]
) -> dict[str, Any] | None:
    gh_bins = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "gh"]
    gh_bin = next((b for b in gh_bins if b == "gh" or os.path.exists(b)), "gh")

    cwd = next(
        (r for r in candidate_repos if r and Path(r).is_dir()),
        os.path.expanduser("~"),
    )
    cmd = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--json",
        "number,title,headRefName,url,files",
    ]
    if repo_slug:
        cmd.extend(["--repo", repo_slug])

    try:
        out = subprocess.check_output(
            cmd,
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3.5,
        )
        data = json.loads(out)
        files = [
            f.get("path", "")
            for f in (data.get("files") or [])[:15]
            if isinstance(f, dict) and f.get("path")
        ]
        return {
            "pr_number": str(data.get("number") or pr_number),
            "title": data.get("title") or "",
            "branch": data.get("headRefName") or "",
            "url": data.get("url") or "",
            "repo_dir": cwd,
            "files": files,
            "source": "gh_cli",
            "resolved_at": int(time.time() * 1000),
        }
    except Exception as exc:
        logger.debug("gh pr view #%s failed: %s", pr_number, exc)
        return None
