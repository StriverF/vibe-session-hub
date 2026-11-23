"""Vibe Session Hub package."""

from __future__ import annotations

from pathlib import Path


def _load_version() -> str:
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        raw = version_file.read_text(encoding="utf-8").strip()
        if raw:
            return raw.lstrip("vV")
    except OSError:
        pass
    return "1.0.0"


def _load_build_id() -> str:
    build_file = Path(__file__).resolve().parent.parent / "BUILD_ID"
    try:
        return build_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__version__ = _load_version()
__build_id__ = _load_build_id()
