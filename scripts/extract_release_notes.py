#!/usr/bin/env python3
"""Generate structured GitHub Release notes for a tagged Vibe Session Hub version."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SECTION_RE = re.compile(r"^##\s+\[?v?([^\s\]\n]+)\]?(.*)$", re.MULTILINE)


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """Extract the markdown body for `version` (matching `[1.0.0]` or `[v1.0.0]`)."""
    target = version.lstrip("vV").strip()
    matches = list(SECTION_RE.finditer(changelog_text))
    for idx, m in enumerate(matches):
        heading_ver = m.group(1).lstrip("vV").strip()
        if heading_ver == target:
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(changelog_text)
            return changelog_text[start:end].strip()
    return ""


def collect_git_commit_highlights(tag: str, repo_root: Path) -> str:
    """Fallback commit list since previous tag when CHANGELOG has no explicit entry."""
    try:
        tags_out = subprocess.check_output(
            ["git", "tag", "--sort=-v:refname"],
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        tags = [t.strip() for t in tags_out.splitlines() if t.strip()]
        prev_tag = ""
        for idx, t in enumerate(tags):
            if t == tag and idx + 1 < len(tags):
                prev_tag = tags[idx + 1]
                break
        if not prev_tag and tags and tags[0] != tag:
            prev_tag = tags[0]
        rev_range = f"{prev_tag}..{tag}" if prev_tag else tag
        log_out = subprocess.check_output(
            ["git", "log", "--no-merges", "--pretty=format:- %s (`%h`)", rev_range],
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return log_out
    except Exception:
        return ""


def build_release_notes(
    version: str,
    tag: str,
    changelog_path: Path,
    dmg_name: str,
    sha256_line: str = "",
    signed_notarized: bool = False,
    repo_root: Path | None = None,
) -> str:
    clean_ver = version.lstrip("vV").strip()
    changelog_body = ""
    if changelog_path.is_file():
        changelog_body = extract_changelog_section(
            changelog_path.read_text(encoding="utf-8"), clean_ver
        )

    parts: list[str] = [f"## Vibe Session Hub {tag}"]

    if changelog_body:
        parts.append(changelog_body)
    else:
        commits = collect_git_commit_highlights(
            tag, repo_root or changelog_path.parent
        )
        if commits:
            parts.append("### What's Changed\n\n" + commits)
        else:
            parts.append(f"Release `{tag}` of Vibe Session Hub.")

    install_lines = [
        "### 下载与安装（macOS 13+ Universal · Apple Silicon & Intel）",
        "",
        f"1. 在下方 **Assets** 下载 `{dmg_name}` 并双击挂载；",
        "2. 将 **`Vibe Session Hub.app`** 拖入 **`Applications`** 文件夹；",
        "3. 双击打开即可使用（内置完整后端与前端，零第三方依赖）；",
        "4. 如需在关闭窗口后保持后台增量索引与跨机跳转服务运行，点击顶部菜单栏 **`Vibe Session Hub → 安装 / 更新开机后台服务`**。",
    ]

    if not signed_notarized:
        install_lines.extend(
            [
                "",
                "> **首次打开提示**：当前安装包为开源 Ad-hoc 签名构建。若 macOS Gatekeeper 提示无法验证开发者，在终端执行一次即可：",
                "> ```bash",
                '> xattr -cr "/Applications/Vibe Session Hub.app"',
                "> ```",
            ]
        )

    parts.append("\n".join(install_lines))

    if sha256_line.strip():
        parts.append(
            "### 校验和（SHA-256）\n\n```text\n" + sha256_line.strip() + "\n```"
        )

    return "\n\n".join(parts).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build GitHub Release notes markdown")
    parser.add_argument("--version", required=True, help="Release version (e.g. 1.0.0)")
    parser.add_argument("--tag", default="", help="Git tag name (default: v<version>)")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--dmg-name", required=True)
    parser.add_argument("--sha256-file", default="")
    parser.add_argument("--signed-notarized", action="store_true")
    parser.add_argument("--output", default="", help="Write markdown to file instead of stdout")
    args = parser.parse_args(argv)

    clean_ver = args.version.lstrip("vV").strip()
    tag = args.tag.strip() or f"v{clean_ver}"
    sha_text = ""
    if args.sha256_file:
        sha_path = Path(args.sha256_file)
        if sha_path.is_file():
            sha_text = sha_path.read_text(encoding="utf-8").strip()

    notes = build_release_notes(
        version=clean_ver,
        tag=tag,
        changelog_path=Path(args.changelog),
        dmg_name=args.dmg_name,
        sha256_line=sha_text,
        signed_notarized=args.signed_notarized,
    )
    if args.output:
        Path(args.output).write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
