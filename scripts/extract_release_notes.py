#!/usr/bin/env python3
"""Generate structured GitHub Release notes for a tagged Vibe Session Hub version."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import re
import subprocess
import sys
from pathlib import Path

SECTION_RE = re.compile(r"^##\s+\[?v?([^\s\]\n]+)\]?(.*)$", re.MULTILINE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")


def _parse_semver(raw: str) -> tuple[tuple[int, ...], tuple[str, ...]] | None:
    text = str(raw or "").strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    if "+" in text:
        text = text.split("+", 1)[0]
    if not text:
        return None
    if "-" in text:
        core_part, pre_part = text.split("-", 1)
    else:
        core_part, pre_part = text, ""
    raw_nums = core_part.split(".")
    if not raw_nums or any(not p.isdigit() for p in raw_nums):
        return None
    nums = tuple(int(p) for p in raw_nums)
    pre = tuple(p for p in pre_part.split(".") if p) if pre_part else ()
    return nums, pre


def compare_semver(lhs: str, rhs: str) -> int:
    a = _parse_semver(lhs)
    b = _parse_semver(rhs)
    if a is None or b is None:
        al = str(lhs or "").strip()
        bl = str(rhs or "").strip()
        if al == bl:
            return 0
        return -1 if al < bl else 1

    a_nums, a_pre = a
    b_nums, b_pre = b
    for i in range(max(len(a_nums), len(b_nums))):
        av = a_nums[i] if i < len(a_nums) else 0
        bv = b_nums[i] if i < len(b_nums) else 0
        if av != bv:
            return -1 if av < bv else 1

    if not a_pre and not b_pre:
        return 0
    if not a_pre:
        return 1
    if not b_pre:
        return -1

    for ap, bp in zip(a_pre, b_pre):
        if ap == bp:
            continue
        if ap.isdigit() and bp.isdigit():
            return -1 if int(ap) < int(bp) else 1
        if ap.isdigit():
            return -1
        if bp.isdigit():
            return 1
        return -1 if ap < bp else 1

    if len(a_pre) == len(b_pre):
        return 0
    return -1 if len(a_pre) < len(b_pre) else 1


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


def extract_unreleased_section(changelog_text: str) -> str:
    """Extract the markdown body under `## [Unreleased]` if present."""
    matches = list(SECTION_RE.finditer(changelog_text))
    for idx, m in enumerate(matches):
        if m.group(1).strip().lower() == "unreleased":
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(changelog_text)
            return changelog_text[start:end].strip()
    return ""


def _has_meaningful_notes(body: str) -> bool:
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return True
    return False


def _latest_released_version_in_changelog(changelog_text: str) -> str:
    latest = ""
    for m in SECTION_RE.finditer(changelog_text):
        ver = m.group(1).lstrip("vV").strip()
        if ver.lower() == "unreleased" or not SEMVER_RE.match(ver):
            continue
        if not latest or compare_semver(ver, latest) > 0:
            latest = ver
    return latest


def _git_tag_exists(tag: str, repo_root: Path) -> bool:
    if not tag:
        return False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _find_previous_tag(tag: str, repo_root: Path) -> str:
    try:
        tags_out = subprocess.check_output(
            ["git", "tag", "--sort=-v:refname"],
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        tags = [t.strip() for t in tags_out.splitlines() if t.strip()]
    except Exception:
        return ""

    for idx, t in enumerate(tags):
        if t == tag and idx + 1 < len(tags):
            return tags[idx + 1]
    for t in tags:
        if t != tag and compare_semver(t, tag) < 0:
            return t
    if tags and tags[0] != tag:
        return tags[0]
    return ""


def _changelog_modified_since_tag(
    prev_tag: str,
    tag: str,
    changelog_path: Path,
    repo_root: Path,
) -> bool | None:
    if not prev_tag:
        return None
    try:
        rel_changelog = changelog_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None

    try:
        if _git_tag_exists(tag, repo_root):
            diff_args = ["git", "diff", "--name-only", f"{prev_tag}..{tag}", "--", str(rel_changelog)]
        else:
            diff_args = ["git", "diff", "--name-only", prev_tag, "--", str(rel_changelog)]
        out = subprocess.check_output(
            diff_args,
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except Exception:
        return None


def _resolve_release_date(tag: str, repo_root: Path, explicit_date: str = "") -> str:
    if explicit_date.strip():
        return explicit_date.strip()
    if _git_tag_exists(tag, repo_root):
        try:
            tag_date = subprocess.check_output(
                ["git", "log", "-1", "--format=%cs", f"refs/tags/{tag}"],
                cwd=str(repo_root),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", tag_date):
                return tag_date
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def collect_git_commit_highlights(tag: str, repo_root: Path) -> str:
    """Fallback commit list since previous tag when CHANGELOG has no explicit entry."""
    try:
        prev_tag = _find_previous_tag(tag, repo_root)
        target_ref = tag if _git_tag_exists(tag, repo_root) else "HEAD"
        rev_range = f"{prev_tag}..{target_ref}" if prev_tag else target_ref
        log_out = subprocess.check_output(
            ["git", "log", "--no-merges", "--pretty=format:- %s (`%h`)", rev_range],
            cwd=str(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return log_out
    except Exception:
        return ""


def resolve_release_body(
    version: str,
    tag: str,
    changelog_path: Path,
    repo_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve release notes body and its source: 'version' | 'unreleased' | 'git' | 'fallback'."""
    clean_ver = version.lstrip("vV").strip()
    root = (repo_root or changelog_path.parent).resolve()

    changelog_text = ""
    if changelog_path.is_file():
        changelog_text = changelog_path.read_text(encoding="utf-8")

    if changelog_text:
        explicit = extract_changelog_section(changelog_text, clean_ver)
        if explicit:
            return explicit, "version"

        unreleased = extract_unreleased_section(changelog_text)
        if _has_meaningful_notes(unreleased):
            latest_in_cl = _latest_released_version_in_changelog(changelog_text)
            if not latest_in_cl or compare_semver(clean_ver, latest_in_cl) > 0:
                prev_tag = _find_previous_tag(tag, root)
                can_promote = True
                if prev_tag and latest_in_cl and compare_semver(prev_tag, latest_in_cl) > 0:
                    modified = _changelog_modified_since_tag(prev_tag, tag, changelog_path, root)
                    if modified is False:
                        can_promote = False
                if can_promote:
                    return unreleased, "unreleased"

    commits = collect_git_commit_highlights(tag, root)
    if commits:
        return "### 本次更新\n\n" + commits, "git"

    return f"- 发布 Vibe Session Hub `{tag}` 版本。", "fallback"


def stamp_changelog_for_release(
    changelog_text: str,
    version: str,
    body: str,
    release_date: str,
    *,
    consumed_unreleased: bool = False,
) -> str:
    """Ensure the bundled CHANGELOG.md contains an explicit heading for `version`."""
    clean_ver = version.lstrip("vV").strip()
    if not clean_ver:
        return changelog_text

    if extract_changelog_section(changelog_text, clean_ver):
        return changelog_text

    clean_body = str(body or "").strip()
    if not clean_body:
        clean_body = f"- 发布 Vibe Session Hub `v{clean_ver}` 版本。"

    stamped_block = f"## [{clean_ver}] - {release_date}\n\n{clean_body}\n"
    matches = list(SECTION_RE.finditer(changelog_text))

    unreleased_idx: int | None = None
    first_ver_idx: int | None = None
    for idx, m in enumerate(matches):
        if m.group(1).strip().lower() == "unreleased":
            if unreleased_idx is None:
                unreleased_idx = idx
        elif first_ver_idx is None:
            first_ver_idx = idx

    if consumed_unreleased and unreleased_idx is not None:
        m = matches[unreleased_idx]
        sec_end = matches[unreleased_idx + 1].start() if unreleased_idx + 1 < len(matches) else len(changelog_text)
        prefix = changelog_text[: m.start()]
        suffix = changelog_text[sec_end:].lstrip("\n")
        rebuilt = f"{prefix}## [Unreleased]\n\n{stamped_block}"
        if suffix:
            rebuilt = f"{rebuilt}\n{suffix}"
        return rebuilt.rstrip() + "\n"

    if first_ver_idx is not None:
        insert_at = matches[first_ver_idx].start()
        prefix = changelog_text[:insert_at].rstrip()
        suffix = changelog_text[insert_at:]
        if prefix:
            return f"{prefix}\n\n{stamped_block}\n{suffix}".rstrip() + "\n"
        return f"{stamped_block}\n{suffix}".rstrip() + "\n"

    base = changelog_text.rstrip()
    if base:
        return f"{base}\n\n{stamped_block}"
    return f"# 更新日志\n\n{stamped_block}"


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
    body, _source = resolve_release_body(
        version=clean_ver,
        tag=tag,
        changelog_path=changelog_path,
        repo_root=repo_root,
    )

    parts: list[str] = [f"## Vibe Session Hub {tag}", body]

    install_lines = [
        "### 下载与安装（macOS 13+ Universal · Apple Silicon & Intel）",
        "",
        f"1. 在下方 **Assets** 下载 `{dmg_name}` 并双击挂载；",
        "2. 将 **`Vibe Session Hub.app`** 拖入 **`Applications`** 文件夹；",
        "3. 双击打开即可使用（内置完整后端与前端，零第三方依赖）；",
        "4. 如需在关闭窗口后保持后台增量索引与跨机跳转服务运行，点击顶部菜单栏 **`Vibe Session Hub → 安装 / 更新开机后台服务`**。",
    ]

    if signed_notarized:
        install_lines.extend(
            [
                "",
                "> **安全认证**：当前安装包已使用 Apple Developer ID 签名并通过 Apple 官方公证（Notarized），下载后可直接双击安装运行。",
            ]
        )
    else:
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
    parser.add_argument("--repo-root", default="", help="Git repository root for fallback commit lookup")
    parser.add_argument("--date", default="", help="Release date YYYY-MM-DD for --bundle-changelog")
    parser.add_argument("--bundle-changelog", default="", help="Write release-stamped CHANGELOG.md to path")
    parser.add_argument("--dmg-name", default="")
    parser.add_argument("--sha256-file", default="")
    parser.add_argument("--signed-notarized", action="store_true")
    parser.add_argument("--output", default="", help="Write markdown to file instead of stdout")
    args = parser.parse_args(argv)

    clean_ver = args.version.lstrip("vV").strip()
    tag = args.tag.strip() or f"v{clean_ver}"
    changelog_path = Path(args.changelog)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else changelog_path.resolve().parent

    if args.bundle_changelog:
        changelog_text = changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
        body, source = resolve_release_body(
            version=clean_ver,
            tag=tag,
            changelog_path=changelog_path,
            repo_root=repo_root,
        )
        release_date = _resolve_release_date(tag, repo_root, args.date)
        stamped = stamp_changelog_for_release(
            changelog_text,
            clean_ver,
            body,
            release_date,
            consumed_unreleased=(source == "unreleased"),
        )
        Path(args.bundle_changelog).write_text(stamped, encoding="utf-8")
        if not args.dmg_name and not args.output:
            return 0

    if not args.dmg_name:
        parser.error("--dmg-name is required unless only --bundle-changelog is requested")

    sha_text = ""
    if args.sha256_file:
        sha_path = Path(args.sha256_file)
        if sha_path.is_file():
            sha_text = sha_path.read_text(encoding="utf-8").strip()

    notes = build_release_notes(
        version=clean_ver,
        tag=tag,
        changelog_path=changelog_path,
        dmg_name=args.dmg_name,
        sha256_line=sha_text,
        signed_notarized=args.signed_notarized,
        repo_root=repo_root,
    )
    if args.output:
        Path(args.output).write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
