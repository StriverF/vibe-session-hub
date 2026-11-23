from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for each supported AI coding client / web source."""

    tool_id: str
    label: str
    icon: str = ""
    bundle_id: str = ""
    deep_link_scheme: str = ""
    allow_remote_open: bool = False
    manual_link_option: bool = False
    manual_link_label: str = ""
    manual_link_order: int = 100


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        tool_id="claude-magpie",
        label="Claude Magpie",
        icon="/icons/tools/claude-magpie.png",
        deep_link_scheme="claude",
    ),
    ToolSpec(
        tool_id="claude-desktop",
        label="Claude",
        icon="/icons/tools/claude-desktop.png",
        bundle_id="com.anthropic.claudefordesktop",
        deep_link_scheme="claude",
    ),
    ToolSpec(
        tool_id="codex",
        label="Codex",
        icon="/icons/tools/codex.png",
        bundle_id="com.openai.codex",
        deep_link_scheme="codex",
    ),
    ToolSpec(
        tool_id="cursor",
        label="Cursor",
        icon="/icons/tools/cursor.png",
        bundle_id="com.todesktop.230313mzl4w4u92",
        deep_link_scheme="cursor",
        manual_link_option=True,
        manual_link_label="Cursor",
        manual_link_order=40,
    ),
    ToolSpec(
        tool_id="gemini",
        label="Gemini",
        icon="/icons/tools/gemini.png",
        bundle_id="com.google.GeminiMacOS",
        deep_link_scheme="googlegemini",
        allow_remote_open=True,
        manual_link_option=True,
        manual_link_label="Gemini 桌面端",
        manual_link_order=50,
    ),
    ToolSpec(
        tool_id="grok-bot",
        label="Grok Bot",
        icon="/icons/tools/grok-bot.png",
        bundle_id="com.anysphere.sand",
        deep_link_scheme="grokbot",
        allow_remote_open=True,
    ),
    ToolSpec(
        tool_id="antigravity",
        label="Antigravity",
        icon="/icons/tools/antigravity.png",
        bundle_id="com.google.antigravity",
        deep_link_scheme="antigravity",
    ),
    ToolSpec(
        tool_id="gemini-web",
        label="Gemini 网页版",
        icon="/icons/tools/gemini.png",
        allow_remote_open=True,
        manual_link_option=True,
        manual_link_label="Gemini 网页版",
        manual_link_order=10,
    ),
    ToolSpec(
        tool_id="grok-web",
        label="Grok 网页版",
        icon="/icons/tools/grok-bot.png",
        allow_remote_open=True,
        manual_link_option=True,
        manual_link_label="Grok 网页版",
        manual_link_order=20,
    ),
    ToolSpec(
        tool_id="chatgpt-web",
        label="ChatGPT 网页版",
        icon="/icons/tools/chatgpt-web.png",
        allow_remote_open=True,
        manual_link_option=True,
        manual_link_label="ChatGPT 网页版",
        manual_link_order=30,
    ),
    ToolSpec(
        tool_id="web-link",
        label="外部会话链接",
        icon="",
        allow_remote_open=True,
        manual_link_option=True,
        manual_link_label="其他链接",
        manual_link_order=60,
    ),
)

TOOL_REGISTRY: dict[str, ToolSpec] = {spec.tool_id: spec for spec in TOOL_SPECS}

# Extra vendor scheme aliases accepted alongside registered primary deep-link schemes.
_EXTRA_DEEP_LINK_SCHEMES: frozenset[str] = frozenset({"comgooglegemini"})

APPROVED_DEEP_LINK_SCHEMES: frozenset[str] = (
    frozenset(spec.deep_link_scheme for spec in TOOL_SPECS if spec.deep_link_scheme)
    | _EXTRA_DEEP_LINK_SCHEMES
)


def get_tool_spec(tool_id: str) -> ToolSpec | None:
    return TOOL_REGISTRY.get(tool_id)


def get_tool_label(tool_id: str, default: str = "") -> str:
    spec = TOOL_REGISTRY.get(tool_id)
    if spec is not None:
        return spec.label
    return default or tool_id


def export_client_tool_catalog() -> dict[str, Any]:
    """Export frontend-consumable tool metadata from the single source of truth."""
    manual_opts = sorted(
        (spec for spec in TOOL_SPECS if spec.manual_link_option),
        key=lambda s: s.manual_link_order,
    )
    return {
        "tools": {
            spec.tool_id: {
                "label": spec.label,
                "icon": spec.icon,
                "allow_remote_open": spec.allow_remote_open,
            }
            for spec in TOOL_SPECS
        },
        "manual_link_options": [
            {
                "value": spec.tool_id,
                "label": spec.manual_link_label or spec.label,
            }
            for spec in manual_opts
        ],
    }
