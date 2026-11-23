import { els, getToolIcon } from "./state.js";

let toastTimer = null;

export function showToast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 2800);
}

export function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderToolLogo(tool, sizeClass = "tool-logo-sm") {
  const src = getToolIcon(tool);
  if (src) {
    return `<img class="tool-logo ${sizeClass}" src="${escapeHtml(src)}" alt="" loading="lazy" />`;
  }
  return `<span class="tool-logo ${sizeClass} tool-logo-svg" aria-hidden="true">
    <svg viewBox="0 0 16 16" fill="none" width="100%" height="100%">
      <circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.4"/>
      <path d="M2.2 8H13.8M8 1.8C9.7 3.5 10.6 5.7 10.6 8C10.6 10.3 9.7 12.5 8 14.2C6.3 12.5 5.4 10.3 5.4 8C5.4 5.7 6.3 3.5 8 1.8Z" stroke="currentColor" stroke-width="1.2"/>
    </svg>
  </span>`;
}

export function highlightText(text, query) {
  const safe = escapeHtml(text);
  const terms = String(query || "")
    .trim()
    .split(/\s+/)
    .map((t) => t.replace(/^#/, ""))
    .filter((t) => t.length >= 2);
  if (!terms.length) return safe;
  const escapedTerms = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escapedTerms.join("|")})`, "gi");
  return safe.replace(re, "<mark>$1</mark>");
}

export function formatTime(ms) {
  if (!ms) return "";
  const diff = Date.now() - ms;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  const d = new Date(ms);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function shortenPath(p) {
  if (!p) return "";
  return p.replace(/^\/Users\/[^/]+/, "~");
}

export function buildCrossMacJumpUrl(item) {
  const params = new URLSearchParams({
    uid: item.uid,
    tool: item.tool,
    id: item.session_id,
  });
  return `http://127.0.0.1:17380/jump?${params.toString()}`;
}
