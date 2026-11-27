import { fetchChangelog, fetchUpdateStatus, triggerUpdateCheck } from "./api.js";
import { els, state } from "./state.js";
import { escapeHtml, showToast } from "./utils.js";

const DEFAULT_RELEASES_URL = "https://github.com/StriverF/vibe-session-hub/releases";

const INSTALL_HINT = "在后台下载并安装，期间不影响使用；装好后再决定何时重启";
const RESTART_HINT = "重启 Vibe Session Hub 以完成更新；不重启的话，下次打开时生效";

let pendingManualCheckToast = false;
let autoBrowserCheckTimer = null;
let userExpandedAllNotes = false;
let remoteChangelogInFlight = false;
let remoteChangelogFetchedKey = "";

export function isNativeUpdaterAvailable() {
  return Boolean(
    typeof window !== "undefined" &&
      window.__VIBE_HUB_NATIVE__ &&
      window.webkit?.messageHandlers?.vibeHubUpdater
  );
}

function postNativeAction(action) {
  if (!isNativeUpdaterAvailable()) return false;
  try {
    window.webkit.messageHandlers.vibeHubUpdater.postMessage({ action });
    return true;
  } catch (_) {
    return false;
  }
}

function defaultUpdateState() {
  return {
    mode: isNativeUpdaterAvailable() ? "native" : "browser",
    current_version: state.serverVersion || "",
    can_self_update: false,
    cannot_update_reason: isNativeUpdaterAvailable() ? "" : "browser_mode",
    status: "idle",
    checking: false,
    latest_confirmed: false,
    percent: 0,
    downloaded_bytes: 0,
    total_bytes: 0,
    error: "",
    check_error: "",
    checked_at: 0,
    update: null,
    just_updated: null,
    updated_from: "",
    updated_notes: "",
    updated_published_at: "",
  };
}

function getUpdateInfo() {
  return state.updateInfo || defaultUpdateState();
}

function normalizeVersion(ver) {
  return String(ver || "").trim().replace(/^v/i, "");
}

function parseSemver(raw) {
  let text = normalizeVersion(raw);
  const plusIdx = text.indexOf("+");
  if (plusIdx >= 0) text = text.slice(0, plusIdx);
  if (!text) return null;
  const dashIdx = text.indexOf("-");
  const corePart = dashIdx >= 0 ? text.slice(0, dashIdx) : text;
  const prePart = dashIdx >= 0 ? text.slice(dashIdx + 1) : "";
  const rawNums = corePart.split(".");
  if (!rawNums.length || rawNums.some((p) => !/^\d+$/.test(p))) return null;
  return {
    nums: rawNums.map((p) => Number.parseInt(p, 10)),
    pre: prePart ? prePart.split(".").filter(Boolean) : [],
  };
}

function compareSemver(lhs, rhs) {
  const a = parseSemver(lhs);
  const b = parseSemver(rhs);
  if (!a || !b) {
    const al = normalizeVersion(lhs);
    const bl = normalizeVersion(rhs);
    if (al === bl) return 0;
    return al < bl ? -1 : 1;
  }
  const maxLen = Math.max(a.nums.length, b.nums.length);
  for (let i = 0; i < maxLen; i += 1) {
    const av = i < a.nums.length ? a.nums[i] : 0;
    const bv = i < b.nums.length ? b.nums[i] : 0;
    if (av !== bv) return av < bv ? -1 : 1;
  }
  if (!a.pre.length && !b.pre.length) return 0;
  if (!a.pre.length) return 1;
  if (!b.pre.length) return -1;
  const minPre = Math.min(a.pre.length, b.pre.length);
  for (let i = 0; i < minPre; i += 1) {
    const ap = a.pre[i];
    const bp = b.pre[i];
    if (ap === bp) continue;
    const aNum = /^\d+$/.test(ap);
    const bNum = /^\d+$/.test(bp);
    if (aNum && bNum) return Number.parseInt(ap, 10) < Number.parseInt(bp, 10) ? -1 : 1;
    if (aNum) return -1;
    if (bNum) return 1;
    return ap < bp ? -1 : 1;
  }
  if (a.pre.length === b.pre.length) return 0;
  return a.pre.length < b.pre.length ? -1 : 1;
}

function formatVersionTag(ver) {
  const clean = normalizeVersion(ver);
  return clean ? `v${clean}` : "";
}

function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (!Number.isFinite(num) || num <= 0) return "";
  if (num < 1024) return `${num} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
  return `${(num / (1024 * 1024)).toFixed(1)} MB`;
}

function formatReleaseDate(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  const match = s.match(/^(\d{4}-\d{2}-\d{2})/);
  if (match) return match[1];
  const dt = new Date(s);
  if (Number.isNaN(dt.getTime())) return "";
  return dt.toISOString().slice(0, 10);
}

function formatInlineMarkdown(raw) {
  let safe = escapeHtml(raw);
  safe = safe.replace(/`([^`]+)`/g, "<code>$1</code>");
  safe = safe.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return safe;
}

const SECTION_TITLE_MAP = {
  added: "新增",
  new: "新增",
  feature: "新增",
  features: "新增",
  新增: "新增",
  新功能: "新增",
  changed: "优化",
  improved: "优化",
  improvements: "优化",
  optimized: "优化",
  optimization: "优化",
  优化: "优化",
  变更: "优化",
  改进: "优化",
  fixed: "修复",
  fix: "修复",
  fixes: "修复",
  "bug fix": "修复",
  "bug fixes": "修复",
  bugfixes: "修复",
  修复: "修复",
  问题修复: "修复",
  security: "安全",
  安全: "安全",
  removed: "移除",
  deprecated: "移除",
  移除: "移除",
  废弃: "移除",
  "what's changed": "更新内容",
  "whats changed": "更新内容",
  本次更新: "更新内容",
  更新内容: "更新内容",
};

const INSTALL_SECTION_RE = /^###\s+(?:下载与安装|校验和|Download|Install|Checksum)/i;

function normalizeSectionTitle(rawTitle) {
  const cleaned = String(rawTitle || "").trim().replace(/^[*`]+|[*`]+$/g, "");
  if (!cleaned) return "";
  return SECTION_TITLE_MAP[cleaned.toLowerCase()] || cleaned;
}

function sectionKindClass(title) {
  switch (title) {
    case "新增":
      return "is-added";
    case "优化":
      return "is-changed";
    case "修复":
      return "is-fixed";
    case "安全":
      return "is-security";
    case "移除":
      return "is-removed";
    default:
      return "";
  }
}

function appendSectionItem(sections, title, item) {
  if (sections.length && sections[sections.length - 1].title === title) {
    sections[sections.length - 1].items.push(item);
  } else {
    sections.push({ title, items: [item] });
  }
}

function normalizeEntrySections(rawSections, fallbackItems = []) {
  const sections = [];
  if (Array.isArray(rawSections)) {
    for (const sec of rawSections) {
      if (!sec || typeof sec !== "object") continue;
      const title = normalizeSectionTitle(sec.title);
      const secItems = Array.isArray(sec.items)
        ? sec.items.map((x) => String(x || "").trim()).filter(Boolean)
        : [];
      for (const item of secItems) {
        appendSectionItem(sections, title, item);
      }
    }
  }
  if (!sections.length && fallbackItems.length) {
    sections.push({ title: "", items: [...fallbackItems] });
  }
  return sections;
}

function parseNotesSections(notes) {
  const bulletSections = [];
  const plainSections = [];
  let currentSection = "";
  let inCodeFence = false;
  for (const rawLine of String(notes || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith("```")) {
      inCodeFence = !inCodeFence;
      continue;
    }
    if (inCodeFence || !line || line.startsWith(">")) {
      continue;
    }
    if (line.startsWith("### ")) {
      if (INSTALL_SECTION_RE.test(line)) {
        break;
      }
      currentSection = normalizeSectionTitle(line.slice(4));
      continue;
    }
    if (line.startsWith("#")) {
      continue;
    }
    if (/^[-*•]\s+/.test(line)) {
      const cleaned = line.replace(/^[-*•]\s+/, "").trim();
      if (cleaned) appendSectionItem(bulletSections, currentSection, cleaned);
    } else {
      appendSectionItem(plainSections, currentSection, line);
    }
  }
  return bulletSections.length ? bulletSections : plainSections;
}

function parseNotesItems(notes) {
  return parseNotesSections(notes).flatMap((sec) => sec.items);
}

function describeCannotSelfUpdate(reason) {
  switch (reason) {
    case "dev_checkout":
      return "当前为源码开发版，不支持原地覆盖。请通过 git pull / install.sh 更新，或下载最新 DMG 安装。";
    case "ephemeral_location":
      return "当前应用正在只读镜像（DMG）或临时路径中运行，请先拖入「应用程序」文件夹后再使用一键升级。";
    case "not_writable":
      return "当前应用所在目录缺少写入权限，无法自动替换。请下载最新 DMG 手动覆盖安装。";
    case "browser_mode":
      return "当前在浏览器中查看；在桌面端 Vibe Session Hub.app 中打开即可支持一键下载与重启更新。";
    default:
      return "当前运行方式不支持自动原地替换，请前往 GitHub Release 下载最新 DMG 安装。";
  }
}

function safeOpenExternalUrl(rawUrl) {
  const url = String(rawUrl || "").trim();
  if (!/^https:\/\//i.test(url)) return;
  window.open(url, "_blank", "noopener,noreferrer");
}

function buildCombinedChangelogEntries(info) {
  const entries = (state.changelogEntries || [])
    .map((e) => {
      const items = Array.isArray(e.items)
        ? e.items.map((x) => String(x || "").trim()).filter(Boolean)
        : [];
      const sections = normalizeEntrySections(e.sections, items);
      return {
        version: normalizeVersion(e.version),
        date: e.date || "",
        items: items.length ? items : sections.flatMap((sec) => sec.items),
        sections,
        next: false,
      };
    })
    .filter((e) => e.version);

  const upsertEntry = (version, date, notes, { markNext = false } = {}) => {
    const cleanVer = normalizeVersion(version);
    if (!cleanVer) return;
    const parsedSections = notes ? parseNotesSections(notes) : [];
    const parsedItems = parsedSections.flatMap((sec) => sec.items);
    const formattedDate = formatReleaseDate(date);
    const existing = entries.find((e) => compareSemver(e.version, cleanVer) === 0);
    if (existing) {
      if (markNext) existing.next = true;
      if (!existing.items.length && parsedItems.length) {
        existing.items = parsedItems;
        existing.sections = parsedSections;
      } else if (
        parsedSections.some((sec) => sec.title) &&
        !existing.sections.some((sec) => sec.title) &&
        parsedItems.length === existing.items.length
      ) {
        existing.sections = parsedSections;
      }
      if (!existing.date && formattedDate) existing.date = formattedDate;
      return;
    }
    if (parsedItems.length) {
      entries.push({
        version: cleanVer,
        date: formattedDate,
        items: parsedItems,
        sections: parsedSections,
        next: markNext,
      });
    }
  };

  // Merge persisted post-restart release notes from AppUpdater (survives dismissJustUpdated)
  const upgradedVer = normalizeVersion(
    info.just_updated?.to || (info.updated_notes ? info.current_version || state.serverVersion : "")
  );
  const upgradedNotes = info.just_updated?.notes || info.updated_notes || "";
  const upgradedDate = info.just_updated?.published_at || info.updated_published_at || "";
  if (upgradedVer && upgradedNotes) {
    upsertEntry(upgradedVer, upgradedDate, upgradedNotes, { markNext: false });
  }

  // Merge newly discovered available/downloading/ready release notes
  const up = info.update;
  if (up && up.version) {
    upsertEntry(up.version, up.published_at, up.notes, { markNext: true });
  }

  entries.sort((a, b) => compareSemver(b.version, a.version));
  return entries;
}

function loadChangelogEntries({ remote = false } = {}) {
  if (remote) {
    if (remoteChangelogInFlight) return;
    remoteChangelogInFlight = true;
  }
  fetchChangelog({ remote })
    .then((res) => {
      if (res && Array.isArray(res.entries)) {
        state.changelogEntries = res.entries;
        renderUpdateModal();
      }
    })
    .catch(() => {})
    .finally(() => {
      if (remote) {
        remoteChangelogInFlight = false;
      }
    });
}

function maybeRefreshRemoteChangelog(info) {
  const currentVer = normalizeVersion(info.current_version || state.serverVersion);
  const nextVer = normalizeVersion(info.update?.version);
  const entries = state.changelogEntries || [];
  const hasCurrent =
    !currentVer || entries.some((e) => compareSemver(e.version, currentVer) === 0);
  const hasNext =
    !nextVer || entries.some((e) => compareSemver(e.version, nextVer) === 0);
  const needsRemote = !entries.length || !hasCurrent || !hasNext || Boolean(nextVer);
  if (!needsRemote) return;

  const key = `${currentVer}|${nextVer}`;
  if (remoteChangelogFetchedKey === key || remoteChangelogInFlight) return;
  remoteChangelogFetchedKey = key;
  loadChangelogEntries({ remote: true });
}

export function renderUpdateControl() {
  if (!els.updateControl) return;
  const info = getUpdateInfo();
  const up = info.update;
  const currentTag = formatVersionTag(info.current_version || state.serverVersion);

  if (up && info.status === "available") {
    const nextTag = formatVersionTag(up.version);
    const ctaLabel = info.can_self_update ? "升级" : "下载";
    const ctaTip = info.can_self_update ? INSTALL_HINT : `查看 ${nextTag} 并下载安装包`;
    const notesTip = up.notes ? `查看 ${nextTag} 的更新内容` : `查看 ${nextTag} 更新详情`;
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-available">
        <button type="button" class="update-pill-info" data-update-action="open-notes" title="${escapeHtml(notesTip)}">
          <span class="update-dot update-dot-accent" aria-hidden="true"></span>
          <span class="update-pill-text">新版本 ${escapeHtml(nextTag)}</span>
        </button>
        <span class="update-pill-sep" aria-hidden="true"></span>
        <button type="button" class="update-pill-cta" data-update-action="start-or-modal" title="${escapeHtml(ctaTip)}">
          ${escapeHtml(ctaLabel)}
        </button>
      </div>
    `;
    return;
  }

  if (up && info.status === "downloading") {
    const pct = Math.max(0, Math.min(99, Number(info.percent || 0)));
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-progress">
        <button type="button" class="update-pill-info" data-update-action="open-modal" title="查看下载进度与更新内容">
          <span class="update-mini-bar" aria-hidden="true">
            <span class="update-mini-bar-fill" style="width:${pct}%"></span>
          </span>
          <span class="update-pill-text update-tabular">下载 ${pct}%</span>
        </button>
        <span class="update-pill-sep" aria-hidden="true"></span>
        <button type="button" class="update-pill-cta update-pill-cta-muted" data-update-action="cancel">取消</button>
      </div>
    `;
    return;
  }

  if (up && info.status === "installing") {
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-progress">
        <button type="button" class="update-pill-info" data-update-action="open-modal" title="正在校验安装包并准备替换">
          <span class="update-spinner" aria-hidden="true"></span>
          <span class="update-pill-text">正在安装…</span>
        </button>
      </div>
    `;
    return;
  }

  if (up && info.status === "ready") {
    const nextTag = formatVersionTag(up.version);
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-ready">
        <button type="button" class="update-pill-info" data-update-action="open-notes" title="${escapeHtml(nextTag)} 已安装就绪 · 点击查看更新内容">
          <span class="update-dot update-dot-ready" aria-hidden="true"></span>
          <span class="update-pill-text">${escapeHtml(nextTag)} 已就绪</span>
        </button>
        <span class="update-pill-sep" aria-hidden="true"></span>
        <button type="button" class="update-pill-cta update-pill-cta-strong" data-update-action="restart" title="${escapeHtml(RESTART_HINT)}">
          立即重启
        </button>
      </div>
    `;
    return;
  }

  if (up && info.status === "restarting") {
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-ready">
        <span class="update-pill-info">
          <span class="update-spinner" aria-hidden="true"></span>
          <span class="update-pill-text">正在重启…</span>
        </span>
      </div>
    `;
    return;
  }

  if (up && info.status === "failed") {
    const errTip = info.error ? `升级失败：${info.error}` : "升级失败，点击查看详情";
    els.updateControl.innerHTML = `
      <div class="update-pill update-pill-failed">
        <button type="button" class="update-pill-info" data-update-action="open-modal" title="${escapeHtml(errTip)}">
          <span class="update-dot update-dot-error" aria-hidden="true"></span>
          <span class="update-pill-text">升级失败</span>
        </button>
        <span class="update-pill-sep" aria-hidden="true"></span>
        <button type="button" class="update-pill-cta" data-update-action="retry" title="${escapeHtml(INSTALL_HINT)}">重试</button>
      </div>
    `;
    return;
  }

  if (info.just_updated) {
    const just = info.just_updated;
    if (just.ok) {
      const toTag = formatVersionTag(just.to || info.current_version);
      els.updateControl.innerHTML = `
        <div class="update-pill update-pill-done">
          <button type="button" class="update-pill-info" data-update-action="open-changelog-after-update" title="查看本次升级的更新内容">
            <span class="update-dot update-dot-ready" aria-hidden="true"></span>
            <span class="update-pill-text">已升级到 ${escapeHtml(toTag)}</span>
          </button>
          <span class="update-pill-sep" aria-hidden="true"></span>
          <button type="button" class="update-pill-cta" data-update-action="open-changelog-after-update">更新内容</button>
          <button type="button" class="update-pill-dismiss" data-update-action="dismiss-just-updated" title="知道了" aria-label="知道了">✕</button>
        </div>
      `;
    } else {
      els.updateControl.innerHTML = `
        <div class="update-pill update-pill-failed">
          <button type="button" class="update-pill-info" data-update-action="open-modal" title="上次升级未完成，仍运行旧版本">
            <span class="update-dot update-dot-error" aria-hidden="true"></span>
            <span class="update-pill-text">升级未完成，仍为 ${escapeHtml(currentTag || "旧版本")}</span>
          </button>
          <span class="update-pill-sep" aria-hidden="true"></span>
          <button type="button" class="update-pill-cta" data-update-action="dismiss-just-updated">知道了</button>
        </div>
      `;
    }
    return;
  }

  if (info.checking) {
    els.updateControl.innerHTML = `
      <button type="button" class="btn btn-ghost update-version-btn is-busy" data-update-action="open-modal" title="正在检查新版本…">
        <span class="update-spinner" aria-hidden="true"></span>
        <span>检查更新…</span>
      </button>
    `;
    return;
  }

  const label = currentTag || "检查更新";
  const tip = currentTag
    ? `当前版本 ${currentTag} · 点击检查新版本与更新日志`
    : "点击检查新版本与更新日志";
  els.updateControl.innerHTML = `
    <button type="button" class="btn btn-ghost update-version-btn" data-update-action="open-modal" title="${escapeHtml(tip)}">
      <svg class="update-btn-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <circle cx="8" cy="8" r="6.25" stroke="currentColor" stroke-width="1.35" />
        <path d="M8 10.8V5.4M8 5.4L5.6 7.7M8 5.4L10.4 7.7" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span>${escapeHtml(label)}</span>
    </button>
  `;
}

export function renderUpdateModal() {
  if (!els.updateModal || els.updateModal.classList.contains("hidden")) return;

  const info = getUpdateInfo();
  const up = info.update;
  const currentVer = String(info.current_version || state.serverVersion || "").replace(/^v/i, "");
  const currentTag = formatVersionTag(currentVer) || "—";

  if (els.updateModalSubtitle) {
    let sub = `当前版本 ${currentTag}`;
    if (info.mode === "native") {
      sub += info.can_self_update ? " · 支持应用内一键下载与重启更新" : " · 桌面客户端";
    } else {
      sub += " · 浏览器视图";
    }
    els.updateModalSubtitle.textContent = sub;
  }

  if (els.updateStatusCard) {
    els.updateStatusCard.innerHTML = renderStatusCardHtml(info, up, currentTag);
  }

  renderChangelogList(info, currentVer);
}

function renderStatusCardHtml(info, up, currentTag) {
  if (up && info.status === "downloading") {
    const pct = Math.max(0, Math.min(99, Number(info.percent || 0)));
    const doneStr = formatBytes(info.downloaded_bytes);
    const totalStr = formatBytes(info.total_bytes || up.dmg_size);
    const bytesMeta =
      doneStr && totalStr ? `${doneStr} / ${totalStr}` : doneStr || totalStr || "";
    return `
      <div class="update-card-main">
        <div class="update-card-title">正在下载新版本 ${escapeHtml(formatVersionTag(up.version))}</div>
        <div class="update-card-desc">
          <span class="update-tabular">进度 ${pct}%${bytesMeta ? ` · ${escapeHtml(bytesMeta)}` : ""}</span>
          <span> · 下载与校验完成后再由你决定何时重启</span>
        </div>
        <div class="update-progress-track" aria-hidden="true">
          <div class="update-progress-fill" style="width:${pct}%"></div>
        </div>
      </div>
      <div class="update-card-actions">
        <button type="button" class="btn btn-ghost" data-update-action="cancel">取消下载</button>
      </div>
    `;
  }

  if (up && info.status === "installing") {
    return `
      <div class="update-card-main">
        <div class="update-card-title">
          <span class="update-spinner" aria-hidden="true"></span>
          <span>正在校验并安装 ${escapeHtml(formatVersionTag(up.version))}…</span>
        </div>
        <div class="update-card-desc">正在校验 SHA-256 哈希、验证应用签名并准备原地替换，请稍候。</div>
      </div>
    `;
  }

  if (up && info.status === "ready") {
    const nextTag = formatVersionTag(up.version);
    return `
      <div class="update-card-main">
        <div class="update-card-title">
          <span class="update-dot update-dot-ready" aria-hidden="true"></span>
          <span>新版本 ${escapeHtml(nextTag)} 已准备就绪</span>
        </div>
        <div class="update-card-desc">${escapeHtml(RESTART_HINT)}</div>
      </div>
      <div class="update-card-actions">
        <button type="button" class="btn btn-primary" data-update-action="restart">立即重启并更新</button>
      </div>
    `;
  }

  if (up && info.status === "restarting") {
    return `
      <div class="update-card-main">
        <div class="update-card-title">
          <span class="update-spinner" aria-hidden="true"></span>
          <span>正在重启并切换到 ${escapeHtml(formatVersionTag(up.version))}…</span>
        </div>
        <div class="update-card-desc">应用将在替换完成后自动重新打开，后台服务也会同步切换至新版本。</div>
      </div>
    `;
  }

  if (up && info.status === "failed") {
    const nextTag = formatVersionTag(up.version);
    const errText = info.error || "发生未知错误，请稍后重试。";
    return `
      <div class="update-card-main">
        <div class="update-card-title">
          <span class="update-dot update-dot-error" aria-hidden="true"></span>
          <span>更新到 ${escapeHtml(nextTag)} 失败</span>
        </div>
        <div class="update-card-desc update-card-error">${escapeHtml(errText)}</div>
      </div>
      <div class="update-card-actions">
        ${
          info.can_self_update
            ? `<button type="button" class="btn btn-primary" data-update-action="retry">重试更新</button>`
            : ""
        }
        ${
          up.dmg_url
            ? `<button type="button" class="btn btn-secondary" data-update-action="download-dmg">手动下载 DMG</button>`
            : ""
        }
      </div>
    `;
  }

  if (up && info.status === "available") {
    const nextTag = formatVersionTag(up.version);
    const metaParts = [];
    const dateStr = formatReleaseDate(up.published_at);
    if (dateStr) metaParts.push(`发布于 ${dateStr}`);
    const sizeStr = formatBytes(up.dmg_size);
    if (sizeStr) metaParts.push(sizeStr);
    const metaLine = metaParts.join(" · ");

    if (info.can_self_update) {
      return `
        <div class="update-card-main">
          <div class="update-card-title">
            <span class="update-dot update-dot-accent" aria-hidden="true"></span>
            <span>发现新版本 ${escapeHtml(nextTag)}</span>
            ${metaLine ? `<span class="update-card-meta">${escapeHtml(metaLine)}</span>` : ""}
          </div>
          <div class="update-card-desc">${escapeHtml(INSTALL_HINT)}</div>
        </div>
        <div class="update-card-actions">
          <button type="button" class="btn btn-primary" data-update-action="start">下载并安装</button>
        </div>
      `;
    }

    return `
      <div class="update-card-main">
        <div class="update-card-title">
          <span class="update-dot update-dot-accent" aria-hidden="true"></span>
          <span>发现新版本 ${escapeHtml(nextTag)}</span>
          ${metaLine ? `<span class="update-card-meta">${escapeHtml(metaLine)}</span>` : ""}
        </div>
        <div class="update-card-desc">${escapeHtml(describeCannotSelfUpdate(info.cannot_update_reason))}</div>
      </div>
      <div class="update-card-actions">
        ${
          up.dmg_url
            ? `<button type="button" class="btn btn-primary" data-update-action="download-dmg">下载 DMG</button>`
            : `<button type="button" class="btn btn-primary" data-update-action="open-release-page">前往下载</button>`
        }
      </div>
    `;
  }

  // Idle / up-to-date / checking / check error / just updated
  let title = `当前版本 ${currentTag}`;
  let desc = "会自动定期检查 GitHub Release 新版本，也可以随时手动检查。";
  if (info.checking) {
    title = "正在检查新版本…";
    desc = "正在从 GitHub Releases 获取最新发布信息。";
  } else if (info.check_error) {
    title = "检查更新失败";
    desc = info.check_error;
  } else if (info.just_updated?.ok) {
    const fromTag = formatVersionTag(info.just_updated.from);
    const toTag = formatVersionTag(info.just_updated.to || currentTag);
    title = `已成功升级到 ${toTag}`;
    desc = fromTag ? `已从 ${fromTag} 平滑升级并重启完成。` : "已重启进入新版本。";
  } else if (info.latest_confirmed) {
    title = `已是最新版本 ${currentTag}`;
    desc = "目前没有比当前版本更新的正式发布。";
  } else if (!info.can_self_update && info.cannot_update_reason) {
    desc = describeCannotSelfUpdate(info.cannot_update_reason);
  }

  return `
    <div class="update-card-main">
      <div class="update-card-title">
        ${info.checking ? `<span class="update-spinner" aria-hidden="true"></span>` : ""}
        <span>${escapeHtml(title)}</span>
      </div>
      <div class="update-card-desc ${info.check_error ? "update-card-error" : ""}">${escapeHtml(desc)}</div>
    </div>
    <div class="update-card-actions">
      <button type="button" class="btn btn-secondary" data-update-action="check" ${info.checking ? "disabled" : ""}>
        ${info.checking ? "检查中…" : "检查更新"}
      </button>
    </div>
  `;
}

function renderChangelogList(info, currentVer) {
  if (!els.updateChangelogList) return;
  const all = buildCombinedChangelogEntries(info);
  let list = all;

  if (state.updateModalScope === "next") {
    const newerEntries = currentVer
      ? all.filter((e) => compareSemver(e.version, currentVer) > 0)
      : all.filter((e) => e.next);
    if (newerEntries.length) {
      list = newerEntries;
    } else if (all.length && all[0].next) {
      list = all.slice(0, 1);
    }
  } else if (state.updateModalScope === "since" && info.updated_from) {
    const fromClean = normalizeVersion(info.updated_from);
    const idx = all.findIndex((e) => compareSemver(e.version, fromClean) === 0);
    if (idx >= 0) {
      list = all.slice(0, idx + 1);
    } else {
      const newerThanFrom = all.filter((e) => compareSemver(e.version, fromClean) > 0);
      if (newerThanFrom.length) {
        list = newerThanFrom;
      }
    }
  }

  if (els.updateToggleAllNotesBtn) {
    els.updateToggleAllNotesBtn.classList.toggle("hidden", list.length >= all.length);
  }

  if (!list.length) {
    els.updateChangelogList.innerHTML = `<div class="update-changelog-empty">暂无更新日志记录，可前往 GitHub Release 查看历史版本。</div>`;
    return;
  }

  const fromClean = normalizeVersion(info.updated_from);
  els.updateChangelogList.innerHTML = list
    .map((entry) => {
      const tags = [];
      if (entry.next) {
        const nextLabel = info.status === "ready" ? "已装好，待重启" : "新版本";
        tags.push(`<span class="update-changelog-tag is-accent">${escapeHtml(nextLabel)}</span>`);
      }
      if (currentVer && compareSemver(entry.version, currentVer) === 0) {
        tags.push(`<span class="update-changelog-tag is-current">当前版本</span>`);
      }
      if (fromClean && compareSemver(entry.version, fromClean) === 0) {
        tags.push(`<span class="update-changelog-tag">升级前版本</span>`);
      }

      const sections = normalizeEntrySections(entry.sections, entry.items || []).filter(
        (sec) => sec.items.length
      );
      const hasTitledSection = sections.some((sec) => Boolean(sec.title));

      let bodyHtml = "";
      if (hasTitledSection) {
        bodyHtml = `<div class="update-changelog-groups">${sections
          .map((sec) => {
            const kindClass = sectionKindClass(sec.title);
            const headingHtml = sec.title
              ? `<div class="update-changelog-group-head"><span class="update-changelog-kind ${kindClass}">${escapeHtml(sec.title)}</span></div>`
              : "";
            const itemsHtml = sec.items
              .map(
                (item) => `
              <li class="update-changelog-item">${formatInlineMarkdown(item)}</li>
            `
              )
              .join("");
            return `
              <div class="update-changelog-group">
                ${headingHtml}
                <ul class="update-changelog-items">${itemsHtml}</ul>
              </div>
            `;
          })
          .join("")}</div>`;
      } else {
        const itemsHtml = (entry.items || [])
          .map(
            (item) => `
          <li class="update-changelog-item">${formatInlineMarkdown(item)}</li>
        `
          )
          .join("");
        bodyHtml = itemsHtml ? `<ul class="update-changelog-items">${itemsHtml}</ul>` : "";
      }

      return `
        <section class="update-changelog-entry">
          <div class="update-changelog-head">
            <span class="update-changelog-ver">${escapeHtml(formatVersionTag(entry.version))}</span>
            ${tags.join("")}
            ${entry.date ? `<span class="update-changelog-date">${escapeHtml(entry.date)}</span>` : ""}
          </div>
          ${bodyHtml}
        </section>
      `;
    })
    .join("");
}

export function applyUpdateSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  const prev = state.updateInfo;
  const prevUpdateVer = normalizeVersion(prev?.update?.version);
  state.updateInfo = { ...defaultUpdateState(), ...snapshot };
  const nextUpdateVer = normalizeVersion(state.updateInfo.update?.version);
  const newlyDiscovered = Boolean(nextUpdateVer && nextUpdateVer !== prevUpdateVer);

  if (newlyDiscovered && !userExpandedAllNotes && state.updateModalScope !== "since") {
    state.updateModalScope = "next";
  }

  renderUpdateControl();
  renderUpdateModal();

  if (newlyDiscovered || state.updateInfo.just_updated?.ok) {
    maybeRefreshRemoteChangelog(state.updateInfo);
  }

  if (pendingManualCheckToast && !state.updateInfo.checking) {
    pendingManualCheckToast = false;
    if (state.updateInfo.check_error) {
      showToast(`检查更新失败：${state.updateInfo.check_error}`);
    } else if (state.updateInfo.update && state.updateInfo.status === "available") {
      showToast(`发现新版本 ${formatVersionTag(state.updateInfo.update.version)}`);
    } else if (state.updateInfo.latest_confirmed) {
      showToast(`当前已是最新版本 ${formatVersionTag(state.updateInfo.current_version)}`);
    }
  } else if (
    prev &&
    prev.status !== "ready" &&
    state.updateInfo.status === "ready" &&
    state.updateInfo.update
  ) {
    showToast(`${formatVersionTag(state.updateInfo.update.version)} 已装好，点击「立即重启」即可生效`);
  }
}

export async function checkForUpdates(fromUser = true) {
  clearTimeout(autoBrowserCheckTimer);
  autoBrowserCheckTimer = null;
  if (isNativeUpdaterAvailable()) {
    if (fromUser) pendingManualCheckToast = true;
    postNativeAction("check");
    return;
  }

  const current = getUpdateInfo();
  if (current.checking) return;

  state.updateInfo = {
    ...current,
    checking: true,
    latest_confirmed: false,
    check_error: "",
  };
  renderUpdateControl();
  renderUpdateModal();

  try {
    const res = await triggerUpdateCheck();
    if (fromUser) pendingManualCheckToast = true;
    applyUpdateSnapshot(res);
  } catch (err) {
    const msg = err?.message || "网络请求失败";
    state.updateInfo = {
      ...getUpdateInfo(),
      checking: false,
      check_error: msg,
    };
    renderUpdateControl();
    renderUpdateModal();
    if (fromUser) {
      showToast(`检查更新失败：${msg}`);
    }
  }
}

export function startUpdateDownload() {
  if (isNativeUpdaterAvailable()) {
    postNativeAction("start");
    return;
  }
  openUpdateModal("next");
}

export function cancelUpdateDownload() {
  postNativeAction("cancel");
}

export function restartToApplyUpdate() {
  postNativeAction("restart");
}

export function dismissJustUpdated() {
  if (isNativeUpdaterAvailable()) {
    postNativeAction("dismissJustUpdated");
  }
  if (state.updateInfo) {
    state.updateInfo = { ...state.updateInfo, just_updated: null };
    renderUpdateControl();
    renderUpdateModal();
  }
}

export function openReleasePage() {
  if (isNativeUpdaterAvailable()) {
    postNativeAction("openReleasePage");
    return;
  }
  const url = getUpdateInfo().update?.html_url || DEFAULT_RELEASES_URL;
  safeOpenExternalUrl(url);
}

export function openDownloadURL() {
  if (isNativeUpdaterAvailable()) {
    postNativeAction("openDownloadURL");
    return;
  }
  const up = getUpdateInfo().update;
  safeOpenExternalUrl(up?.dmg_url || up?.html_url || DEFAULT_RELEASES_URL);
}

export function openUpdateModal(scope = "default") {
  if (!els.updateModal) return;
  const info = getUpdateInfo();
  userExpandedAllNotes = false;
  if (scope === "next") {
    state.updateModalScope = "next";
  } else if (scope === "since") {
    state.updateModalScope = "since";
  } else {
    state.updateModalScope = info.update && info.update.version ? "next" : "all";
  }
  els.updateModal.classList.remove("hidden");
  renderUpdateModal();
  maybeRefreshRemoteChangelog(info);

  if (!info.checked_at && !info.checking && !info.update) {
    checkForUpdates(false);
  }
}

export function closeUpdateModal() {
  if (!els.updateModal) return;
  els.updateModal.classList.add("hidden");
}

function handleActionClick(e) {
  const target = e.target.closest("[data-update-action]");
  if (!target) return;
  const action = target.dataset.updateAction;
  const info = getUpdateInfo();

  switch (action) {
    case "open-modal":
      openUpdateModal("default");
      break;
    case "open-notes":
      openUpdateModal("next");
      break;
    case "open-changelog-after-update":
      openUpdateModal("since");
      dismissJustUpdated();
      break;
    case "check":
      checkForUpdates(true);
      break;
    case "start-or-modal":
      if (info.can_self_update) {
        startUpdateDownload();
      } else {
        openUpdateModal("next");
      }
      break;
    case "start":
    case "retry":
      if (info.can_self_update) {
        startUpdateDownload();
      } else {
        openUpdateModal("next");
      }
      break;
    case "cancel":
      cancelUpdateDownload();
      break;
    case "restart":
      restartToApplyUpdate();
      break;
    case "dismiss-just-updated":
      dismissJustUpdated();
      break;
    case "download-dmg":
      openDownloadURL();
      break;
    case "open-release-page":
      openReleasePage();
      break;
    default:
      break;
  }
}

export function bindUpdaterEvents() {
  if (els.updateControl) {
    els.updateControl.addEventListener("click", handleActionClick);
  }
  if (els.updateStatusCard) {
    els.updateStatusCard.addEventListener("click", handleActionClick);
  }
  if (els.closeUpdateModalBtn) {
    els.closeUpdateModalBtn.addEventListener("click", closeUpdateModal);
  }
  if (els.doneUpdateModalBtn) {
    els.doneUpdateModalBtn.addEventListener("click", closeUpdateModal);
  }
  if (els.openReleasePageBtn) {
    els.openReleasePageBtn.addEventListener("click", openReleasePage);
  }
  if (els.updateToggleAllNotesBtn) {
    els.updateToggleAllNotesBtn.addEventListener("click", () => {
      userExpandedAllNotes = true;
      state.updateModalScope = "all";
      renderUpdateModal();
    });
  }
  if (els.updateModal) {
    els.updateModal.addEventListener("click", (e) => {
      if (e.target === els.updateModal) closeUpdateModal();
    });
  }
}

export async function initUpdater() {
  window.__vibeHubReceiveUpdateState = (snapshot) => {
    applyUpdateSnapshot(snapshot);
  };
  window.__vibeHubOpenUpdateModal = () => {
    openUpdateModal("default");
  };

  renderUpdateControl();

  // Load bundled local CHANGELOG.md in background
  loadChangelogEntries({ remote: false });

  if (isNativeUpdaterAvailable()) {
    postNativeAction("getState");
    return;
  }

  // Browser mode fallback: load cached status, then perform a single quiet check if never checked
  try {
    const status = await fetchUpdateStatus();
    applyUpdateSnapshot(status);
    if (!status?.checked_at) {
      clearTimeout(autoBrowserCheckTimer);
      autoBrowserCheckTimer = setTimeout(() => {
        checkForUpdates(false);
      }, 2500);
    }
  } catch (_) {}
}
