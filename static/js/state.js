export const THEME_OPTIONS = [
  {
    id: "prism-aurora",
    name: "星环极光 · 默认",
    desc: "Logo 光谱暗黑 · 极光流彩",
    bg: "#0b0c10",
    surface: "#12131a",
    text: "#f3f4f8",
    muted: "#6f7387",
    accent: "#8b5cf6",
    pill: "linear-gradient(135deg, #38bdf8 0%, #8b5cf6 44%, #ec4899 74%, #fb923c 100%)",
  },
  {
    id: "obsidian-amber",
    name: "曜石暖金",
    desc: "沉稳黑曜 · 暖陶主色",
    bg: "#111113",
    surface: "#18181b",
    text: "#f3f2ee",
    muted: "#74726e",
    accent: "#d97757",
  },
  {
    id: "graphite-mono",
    name: "极简钛灰",
    desc: "无彩度碳黑 · 纯白高亮",
    bg: "#0c0c0e",
    surface: "#141417",
    text: "#f4f4f5",
    muted: "#71717a",
    accent: "#e4e4e7",
  },
  {
    id: "forest-sage",
    name: "松烟翡翠",
    desc: "深林暗绿 · 护眼翠色",
    bg: "#0c1210",
    surface: "#131b18",
    text: "#ecf3f0",
    muted: "#698076",
    accent: "#10b981",
  },
  {
    id: "espresso-copper",
    name: "浓缩咖啡",
    desc: "暖棕烘焙 · 古铜焦糖",
    bg: "#14110f",
    surface: "#1c1815",
    text: "#f5efe8",
    muted: "#827569",
    accent: "#ce8456",
  },
  {
    id: "plum-amethyst",
    name: "暮光紫晶",
    desc: "深邃暗夜 · 柔和紫晶",
    bg: "#110e17",
    surface: "#191522",
    text: "#f3effa",
    muted: "#7b7094",
    accent: "#9f7aea",
  },
  {
    id: "crimson-rose",
    name: "暗夜绯红",
    desc: "炭黑底色 · 雅致浆果红",
    bg: "#121012",
    surface: "#1a1619",
    text: "#f6eff2",
    muted: "#806f77",
    accent: "#e15b76",
  },
  {
    id: "warm-paper",
    name: "暖砂宣纸",
    desc: "护眼浅色 · 纸感暖米",
    bg: "#f5f1e8",
    surface: "#fcfaf5",
    text: "#1c1917",
    muted: "#878078",
    accent: "#c25e38",
  },
  {
    id: "mist-sage",
    name: "晨雾青瓷",
    desc: "清爽浅色 · 青瓷黛绿",
    bg: "#f1f4f2",
    surface: "#ffffff",
    text: "#131c18",
    muted: "#788a82",
    accent: "#0f766e",
  },
];

// Fallback catalog used only prior to /api/facets hydration; backend tool_registry.py is the source of truth.
const DEFAULT_TOOL_CATALOG = {
  tools: {
    codex: { label: "Codex", icon: "/icons/tools/codex.png", allow_remote_open: false },
    antigravity: { label: "Antigravity", icon: "/icons/tools/antigravity.png", allow_remote_open: false },
    "claude-magpie": { label: "Claude Magpie", icon: "/icons/tools/claude-magpie.png", allow_remote_open: false },
    "claude-desktop": { label: "Claude", icon: "/icons/tools/claude-desktop.png", allow_remote_open: false },
    "grok-bot": { label: "Grok Bot", icon: "/icons/tools/grok-bot.png", allow_remote_open: true },
    "grok-web": { label: "Grok 网页版", icon: "/icons/tools/grok-bot.png", allow_remote_open: true },
    cursor: { label: "Cursor", icon: "/icons/tools/cursor.png", allow_remote_open: false },
    gemini: { label: "Gemini", icon: "/icons/tools/gemini.png", allow_remote_open: true },
    "gemini-web": { label: "Gemini 网页版", icon: "/icons/tools/gemini.png", allow_remote_open: true },
    "chatgpt-web": { label: "ChatGPT 网页版", icon: "/icons/tools/chatgpt-web.png", allow_remote_open: true },
    "web-link": { label: "外部会话链接", icon: "", allow_remote_open: true },
  },
  manual_link_options: [
    { value: "gemini-web", label: "Gemini 网页版" },
    { value: "grok-web", label: "Grok 网页版" },
    { value: "chatgpt-web", label: "ChatGPT 网页版" },
    { value: "cursor", label: "Cursor" },
    { value: "gemini", label: "Gemini 桌面端" },
    { value: "web-link", label: "其他链接" },
  ],
};

export const state = {
  query: "",
  tool: "",
  machineId: "",
  onlyPr: false,
  facets: null,
  toolCatalog: DEFAULT_TOOL_CATALOG,
  items: [],
  resolvedPr: null,
  selectedIndex: 0,
  searchTimer: null,
  theme: document.documentElement.dataset.theme || "prism-aurora",
};

export function updateToolCatalog(catalog) {
  if (!catalog || typeof catalog !== "object") return;
  state.toolCatalog = {
    tools: {
      ...DEFAULT_TOOL_CATALOG.tools,
      ...(catalog.tools || {}),
    },
    manual_link_options:
      Array.isArray(catalog.manual_link_options) && catalog.manual_link_options.length
        ? catalog.manual_link_options
        : DEFAULT_TOOL_CATALOG.manual_link_options,
  };
}

export function getToolIcon(toolId) {
  return state.toolCatalog.tools?.[toolId]?.icon || "";
}

export function canOpenRemoteDirectly(item) {
  if (!item) return false;
  if (item.external_url) return true;
  return Boolean(state.toolCatalog.tools?.[item.tool]?.allow_remote_open);
}

export const els = {
  openThemeModalBtn: document.getElementById("openThemeModalBtn"),
  appearanceModal: document.getElementById("appearanceModal"),
  closeAppearanceBtn: document.getElementById("closeAppearanceBtn"),
  doneAppearanceBtn: document.getElementById("doneAppearanceBtn"),
  themeGrid: document.getElementById("themeGrid"),
  searchInput: document.getElementById("searchInput"),
  clearSearchBtn: document.getElementById("clearSearchBtn"),
  machineFilters: document.getElementById("machineFilters"),
  toolFilters: document.getElementById("toolFilters"),
  onlyPrCheckbox: document.getElementById("onlyPrCheckbox"),
  prTotalCount: document.getElementById("prTotalCount"),
  prBanner: document.getElementById("prBanner"),
  resultsList: document.getElementById("resultsList"),
  detailPane: document.getElementById("detailPane"),
  currentMachineName: document.getElementById("currentMachineName"),
  machineBadgeBtn: document.getElementById("machineBadgeBtn"),
  syncMeta: document.getElementById("syncMeta"),
  syncNowBtn: document.getElementById("syncNowBtn"),
  openLinkModalBtn: document.getElementById("openLinkModalBtn"),
  linkModal: document.getElementById("linkModal"),
  closeModalBtn: document.getElementById("closeModalBtn"),
  cancelModalBtn: document.getElementById("cancelModalBtn"),
  manualLinkForm: document.getElementById("manualLinkForm"),
  linkTargetUid: document.getElementById("linkTargetUid"),
  linkTargetHint: document.getElementById("linkTargetHint"),
  linkPrNumber: document.getElementById("linkPrNumber"),
  linkToolSelect: document.getElementById("linkToolSelect"),
  linkExternalUrl: document.getElementById("linkExternalUrl"),
  linkTitle: document.getElementById("linkTitle"),
  linkNote: document.getElementById("linkNote"),
  standaloneFields: document.getElementById("standaloneFields"),
  standaloneTitleField: document.getElementById("standaloneTitleField"),
  toast: document.getElementById("toast"),
};
