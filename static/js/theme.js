import { saveThemePreference } from "./api.js";
import { THEME_OPTIONS, els, state } from "./state.js";
import { escapeHtml, showToast } from "./utils.js";

export function applyThemeLocally(themeId) {
  if (!THEME_OPTIONS.some((t) => t.id === themeId)) return;
  state.theme = themeId;
  document.documentElement.dataset.theme = themeId;
  try {
    localStorage.setItem("vibe_hub_theme", themeId);
  } catch (_) {}
  renderAppearanceOptions();
}

export function renderAppearanceOptions() {
  if (!els.themeGrid) return;
  els.themeGrid.innerHTML = THEME_OPTIONS.map((t) => {
    const sel = state.theme === t.id ? "selected" : "";
    return `
      <button type="button" class="theme-option ${sel}" data-theme-id="${escapeHtml(t.id)}">
        <div class="theme-preview-strip" style="background:${t.bg}">
          <div class="theme-preview-bars">
            <span style="background:${t.text};width:85%"></span>
            <span style="background:${t.muted};width:60%"></span>
          </div>
          <span class="theme-preview-pill" style="background:${t.pill || t.accent}"></span>
        </div>
        <div class="theme-opt-name">${escapeHtml(t.name)}</div>
        <div class="theme-opt-desc">${escapeHtml(t.desc)}</div>
      </button>
    `;
  }).join("");
}

export async function selectTheme(themeId) {
  applyThemeLocally(themeId);
  const found = THEME_OPTIONS.find((t) => t.id === themeId);
  await saveThemePreference(themeId);
  if (found) showToast(`已切换为「${found.name}」主题`);
}

export function openAppearanceModal() {
  renderAppearanceOptions();
  els.appearanceModal.classList.remove("hidden");
}

export function closeAppearanceModal() {
  els.appearanceModal.classList.add("hidden");
}
