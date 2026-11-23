const TOOL_ICON_MAP = {
  codex: "/icons/tools/codex.png",
  antigravity: "/icons/tools/antigravity.png",
  "claude-magpie": "/icons/tools/claude-magpie.png",
  "claude-desktop": "/icons/tools/claude-desktop.png",
  "grok-bot": "/icons/tools/grok-bot.png",
  "grok-web": "/icons/tools/grok-bot.png",
  cursor: "/icons/tools/cursor.png",
  gemini: "/icons/tools/gemini.png",
  "gemini-web": "/icons/tools/gemini.png",
  "chatgpt-web": "/icons/tools/chatgpt-web.png",
};

const THEME_OPTIONS = [
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

const state = {
  query: "",
  tool: "",
  machineId: "",
  onlyPr: false,
  facets: null,
  items: [],
  resolvedPr: null,
  selectedIndex: 0,
  searchTimer: null,
  theme: document.documentElement.dataset.theme || "obsidian-amber",
};

const els = {
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

let toastTimer = null;
function showToast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 2800);
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderToolLogo(tool, sizeClass = "tool-logo-sm") {
  const src = TOOL_ICON_MAP[tool];
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

function highlightText(text, query) {
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

function formatTime(ms) {
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

function shortenPath(p) {
  if (!p) return "";
  return p.replace(/^\/Users\/[^/]+/, "~");
}

function buildCrossMacJumpUrl(item) {
  const params = new URLSearchParams({
    uid: item.uid,
    tool: item.tool,
    id: item.session_id,
  });
  return `http://127.0.0.1:17380/jump?${params.toString()}`;
}

async function postApi(url, payload = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Vibe-Hub": "1",
    },
    body: JSON.stringify(payload),
  });
  return res.json();
}

async function copyText(text, successMsg) {
  try {
    await postApi("/api/copy", { text });
  } catch {
    try {
      await navigator.clipboard.writeText(text);
    } catch {}
  }
  showToast(successMsg || "已复制到剪贴板");
}

function applyThemeLocally(themeId) {
  if (!THEME_OPTIONS.some((t) => t.id === themeId)) return;
  state.theme = themeId;
  document.documentElement.dataset.theme = themeId;
  try {
    localStorage.setItem("vibe_hub_theme", themeId);
  } catch (_) {}
  renderAppearanceOptions();
}

function renderAppearanceOptions() {
  if (els.themeGrid) {
    els.themeGrid.innerHTML = THEME_OPTIONS.map((t) => {
      const sel = state.theme === t.id ? "selected" : "";
      return `
        <button type="button" class="theme-option ${sel}" data-theme-id="${escapeHtml(t.id)}">
          <div class="theme-preview-strip" style="background:${t.bg}">
            <div class="theme-preview-bars">
              <span style="background:${t.text};width:85%"></span>
              <span style="background:${t.muted};width:60%"></span>
            </div>
            <span class="theme-preview-pill" style="background:${t.accent}"></span>
          </div>
          <div class="theme-opt-name">${escapeHtml(t.name)}</div>
          <div class="theme-opt-desc">${escapeHtml(t.desc)}</div>
        </button>
      `;
    }).join("");
  }
}

async function selectTheme(themeId) {
  applyThemeLocally(themeId);
  const found = THEME_OPTIONS.find((t) => t.id === themeId);
  await postApi("/api/preferences", { theme: themeId });
  if (found) showToast(`已切换为「${found.name}」主题`);
}

async function loadFacets() {
  const res = await fetch("/api/facets");
  const data = await res.json();
  state.facets = data;
  if (data.preferences?.theme) {
    applyThemeLocally(data.preferences.theme);
  }
  els.currentMachineName.textContent = `${data.current_machine.machine_name} (本机)`;
  els.prTotalCount.textContent = data.with_pr_count;
  renderFilterPills();
}

function renderFilterPills() {
  if (!state.facets) return;
  const { machines, tools, total } = state.facets;

  const mHtml = [
    `<button class="filter-pill ${state.machineId === "" ? "active" : ""}" data-machine="">全部设备 (${total})</button>`,
    ...machines.map((m) => {
      const tag = m.is_local
        ? `💻 ${escapeHtml(m.machine_name)} · 本机`
        : `🖥 ${escapeHtml(m.machine_name)}`;
      const active = state.machineId === m.machine_id ? "active" : "";
      return `<button class="filter-pill ${active}" data-machine="${escapeHtml(m.machine_id)}">${tag} (${m.count})</button>`;
    }),
  ].join("");
  els.machineFilters.innerHTML = mHtml;

  const tHtml = [
    `<button class="filter-pill ${state.tool === "" ? "active" : ""}" data-tool="">全部工具</button>`,
    ...tools.map((t) => {
      const active = state.tool === t.tool ? "active" : "";
      return `<button class="filter-pill ${active}" data-tool="${escapeHtml(t.tool)}">
        ${renderToolLogo(t.tool, "tool-logo-xs")}
        <span>${escapeHtml(t.label)} (${t.count})</span>
      </button>`;
    }),
  ].join("");
  els.toolFilters.innerHTML = tHtml;
}

async function runSearch(remotePr = false) {
  const params = new URLSearchParams({
    q: state.query,
    tool: state.tool,
    machine_id: state.machineId,
    only_pr: state.onlyPr ? "1" : "0",
    remote_pr: remotePr ? "1" : "0",
  });
  const res = await fetch(`/api/search?${params.toString()}`);
  const data = await res.json();
  state.items = data.items || [];
  state.resolvedPr = data.resolved_pr || null;
  state.selectedIndex = state.items.length ? 0 : -1;

  els.syncMeta.textContent = `共匹配 ${state.items.length} 条 · ${data.elapsed_ms}ms`;
  renderPrBanner();
  renderList();
  renderDetail();
}

function renderPrBanner() {
  const q = state.query.trim();
  const looksLikePr = /^(?:pr\s*#?|pull\/|#)?(\d{2,7})$/i.test(q);
  if (!state.resolvedPr && !looksLikePr) {
    els.prBanner.classList.add("hidden");
    return;
  }
  els.prBanner.classList.remove("hidden");
  if (state.resolvedPr) {
    const p = state.resolvedPr;
    const branchHtml = p.branch ? ` · 分支 <code>${escapeHtml(p.branch)}</code>` : "";
    const linkHtml = p.url
      ? `<a href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer">在 GitHub 查看 PR #${escapeHtml(p.pr_number)} ↗</a>`
      : "";
    els.prBanner.innerHTML = `
      <div><strong>PR #${escapeHtml(p.pr_number)}</strong>：${escapeHtml(p.title || "已识别")}${branchHtml}</div>
      <div>${linkHtml}</div>
    `;
  } else {
    els.prBanner.innerHTML = `
      <div>正在按 PR 编号搜索本地索引，若该 PR 当时在网页端手动创建，可拉取 GitHub 分支反查会话：</div>
      <button id="fetchRemotePrBtn" class="btn btn-secondary">联网反查 GitHub PR 分支</button>
    `;
    const btn = document.getElementById("fetchRemotePrBtn");
    if (btn) btn.onclick = () => runSearch(true);
  }
}

function renderList() {
  if (!state.items.length) {
    els.resultsList.innerHTML = `<div class="empty-list">没有找到匹配的会话。试试搜其他关键词、分支名，或点击右上角「同步索引」。</div>`;
    return;
  }

  els.resultsList.innerHTML = state.items
    .map((item, idx) => {
      const selected = idx === state.selectedIndex ? "selected" : "";
      const locBadge = item.is_local
        ? `<span class="loc-badge loc-local">本机 · ${escapeHtml(item.machine_name)}</span>`
        : `<span class="loc-badge loc-remote">🖥 ${escapeHtml(item.machine_name)}</span>`;

      const prsHtml = (item.prs || [])
        .slice(0, 3)
        .map((p) => {
          const label = `PR #${escapeHtml(p.number)}`;
          return p.url
            ? `<a class="pr-pill" href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">${label}</a>`
            : `<span class="pr-pill">${label}</span>`;
        })
        .join("");

      const snippetSource = item.matched_snippet || item.first_prompt || item.summary || "";
      const branch = (item.branches || [])[0] || "";

      return `
        <div class="result-item ${selected}" data-index="${idx}">
          <div class="item-top">
            <span class="tool-tag tool-${escapeHtml(item.tool)}">
              ${renderToolLogo(item.tool, "tool-logo-sm")}
              <span>${escapeHtml(item.tool_label)}</span>
            </span>
            ${locBadge}
            ${prsHtml}
            <span class="item-time">${formatTime(item.updated_at)}</span>
          </div>
          <div class="item-title">${highlightText(item.title, state.query)}</div>
          ${snippetSource ? `<div class="item-sub">${highlightText(snippetSource, state.query)}</div>` : ""}
          <div class="item-foot">
            ${branch ? `<span class="mono-tag">🌿 ${escapeHtml(branch)}</span>` : ""}
            ${item.cwd ? `<span class="mono-tag">${escapeHtml(shortenPath(item.cwd))}</span>` : ""}
            ${item.match_reason ? `<span class="reason-tag">${escapeHtml(item.match_reason)}</span>` : ""}
          </div>
        </div>
      `;
    })
    .join("");
}

function renderDetail() {
  const item = state.items[state.selectedIndex];
  if (!item) {
    els.detailPane.innerHTML = `<div class="empty-detail">选择左侧会话查看详情与跳转操作</div>`;
    return;
  }

  const jumpUrl = buildCrossMacJumpUrl(item);
  const prsLinks = (item.prs || [])
    .map((p) =>
      p.url
        ? `<a class="pr-pill" href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer">PR #${escapeHtml(p.number)} ↗</a>`
        : `<span class="pr-pill">PR #${escapeHtml(p.number)}</span>`
    )
    .join(" ");

  const remoteBanner = !item.is_local
    ? `<div class="remote-notice">
         此会话保存在另一台电脑 <strong>「${escapeHtml(item.machine_name)}」</strong> 上。<br/>
         点击下方「复制跨机跳转链接」，通过 macOS 通用剪贴板在 <strong>${escapeHtml(item.machine_name)}</strong> 浏览器地址栏粘贴回车，即可直接唤起 ${escapeHtml(item.tool_label)} 对应会话。
       </div>`
    : "";

  const canTryOpenRemoteDirectly =
    item.tool === "grok-bot" || item.tool === "gemini" || Boolean(item.external_url);

  const primaryActionHtml = item.is_local
    ? `<button id="primaryOpenBtn" class="btn btn-primary btn-lg">
         ${renderToolLogo(item.tool, "tool-logo-btn")}
         <span>在 ${escapeHtml(item.tool_label)} 中打开会话 ↵</span>
       </button>
       <button id="copyJumpBtn" class="btn btn-secondary">复制跨机跳转链接</button>`
    : `<button id="copyJumpBtn" class="btn btn-primary btn-lg">复制跨机打开链接 ↵</button>
       ${item.deep_link ? `<button id="copyDeepLinkBtn" class="btn btn-secondary">复制原生协议 (${escapeHtml(item.deep_link.split(":")[0])}://)</button>` : ""}
       ${canTryOpenRemoteDirectly ? `<button id="primaryOpenBtn" class="btn btn-secondary">${renderToolLogo(item.tool, "tool-logo-btn")}<span>尝试在本机打开</span></button>` : ""}`;

  els.detailPane.innerHTML = `
    <div class="detail-card">
      <div class="detail-header">
        <div class="detail-badges">
          <span class="tool-tag tool-tag-lg tool-${escapeHtml(item.tool)}">
            ${renderToolLogo(item.tool, "tool-logo-md")}
            <span>${escapeHtml(item.tool_label)}</span>
          </span>
          <span class="loc-badge ${item.is_local ? "loc-local" : "loc-remote"}">
            ${item.is_local ? `💻 本机 · ${escapeHtml(item.machine_name)}` : `🖥 跨设备 · ${escapeHtml(item.machine_name)}`}
          </span>
          ${prsLinks}
        </div>
        <h1 class="detail-title">${highlightText(item.title, state.query)}</h1>
        ${remoteBanner}
        <div class="action-bar">
          ${primaryActionHtml}
          <button id="copyContextBtn" class="btn btn-ghost">复制上下文摘要</button>
          <button id="bindPrToSessionBtn" class="btn btn-ghost">+ 绑定 PR 号</button>
        </div>
      </div>

      <div class="meta-grid">
        <div class="meta-label">会话 ID</div>
        <div class="meta-val">${escapeHtml(item.session_id)}</div>
        ${item.branches && item.branches.length ? `<div class="meta-label">Git 分支</div><div class="meta-val">${escapeHtml(item.branches.join(", "))}</div>` : ""}
        ${item.cwd ? `<div class="meta-label">项目目录</div><div class="meta-val">${escapeHtml(item.cwd)}</div>` : ""}
        <div class="meta-label">最近活跃</div>
        <div class="meta-val">${new Date(item.updated_at || Date.now()).toLocaleString("zh-CN")}</div>
        <div class="meta-label">跨机链接</div>
        <div class="meta-val">${escapeHtml(jumpUrl)}</div>
      </div>

      ${item.matched_snippet ? `
        <div class="section-block">
          <h3>命中片段</h3>
          <div class="text-box">${highlightText(item.matched_snippet, state.query)}</div>
        </div>` : ""}

      ${item.first_prompt ? `
        <div class="section-block">
          <h3>初始提问</h3>
          <div class="text-box">${highlightText(item.first_prompt, state.query)}</div>
        </div>` : ""}

      ${item.summary ? `
        <div class="section-block">
          <h3>最新进展 / 回复摘要</h3>
          <div class="text-box">${highlightText(item.summary, state.query)}</div>
        </div>` : ""}
    </div>
  `;

  const openBtn = document.getElementById("primaryOpenBtn");
  if (openBtn) openBtn.onclick = () => triggerOpenSession(item);

  const copyJumpBtn = document.getElementById("copyJumpBtn");
  if (copyJumpBtn) {
    copyJumpBtn.onclick = () =>
      copyText(
        jumpUrl,
        `已复制跨机跳转链接！去「${item.machine_name}」浏览器粘贴回车即可打开`
      );
  }

  const copyDeepBtn = document.getElementById("copyDeepLinkBtn");
  if (copyDeepBtn) {
    copyDeepBtn.onclick = () =>
      copyText(item.deep_link, `已复制原生协议链接: ${item.deep_link}`);
  }

  const copyCtxBtn = document.getElementById("copyContextBtn");
  if (copyCtxBtn) {
    copyCtxBtn.onclick = () => {
      const prStr = (item.prs || []).map((p) => `#${p.number} (${p.url || ""})`).join(", ");
      const md = [
        `## 历史会话上下文（来自 ${item.machine_name} · ${item.tool_label}）`,
        `- 标题：${item.title}`,
        prStr ? `- 关联 PR：${prStr}` : "",
        item.branches?.length ? `- 分支：${item.branches.join(", ")}` : "",
        item.cwd ? `- 目录：${item.cwd}` : "",
        item.first_prompt ? `\n### 当时的提问\n${item.first_prompt}` : "",
        item.summary ? `\n### 当时的结论摘要\n${item.summary}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      copyText(md, "已复制会话上下文摘要，可直接粘贴到当前 Agent 继续提问");
    };
  }

  const bindBtn = document.getElementById("bindPrToSessionBtn");
  if (bindBtn) {
    bindBtn.onclick = () => openLinkModal(item);
  }
}

async function triggerOpenSession(item) {
  const data = await postApi("/api/open", { uid: item.uid });
  showToast(data.message || (data.ok ? "已唤起桌面客户端" : "打开失败"));
}

function triggerPrimaryForSelected() {
  const item = state.items[state.selectedIndex];
  if (!item) return;
  if (item.is_local) {
    triggerOpenSession(item);
  } else {
    copyText(
      buildCrossMacJumpUrl(item),
      `已复制跨机链接！去「${item.machine_name}」粘贴回车即可打开会话`
    );
  }
}

function openAppearanceModal() {
  renderAppearanceOptions();
  els.appearanceModal.classList.remove("hidden");
}

function closeAppearanceModal() {
  els.appearanceModal.classList.add("hidden");
}

function openLinkModal(targetSession = null) {
  els.manualLinkForm.reset();
  if (targetSession) {
    els.linkTargetUid.value = targetSession.uid;
    els.linkTargetHint.textContent = `正在为会话「${targetSession.title}」补充绑定 PR 编号、标题或备注`;
    els.linkTargetHint.classList.remove("hidden");
    els.standaloneFields.classList.add("hidden");
    els.standaloneTitleField.classList.remove("hidden");
  } else {
    els.linkTargetUid.value = "";
    els.linkTargetHint.classList.add("hidden");
    els.standaloneFields.classList.remove("hidden");
    els.standaloneTitleField.classList.remove("hidden");
  }
  els.linkModal.classList.remove("hidden");
  els.linkPrNumber.focus();
}

function closeLinkModal() {
  els.linkModal.classList.add("hidden");
}

els.openThemeModalBtn.addEventListener("click", openAppearanceModal);
els.closeAppearanceBtn.addEventListener("click", closeAppearanceModal);
els.doneAppearanceBtn.addEventListener("click", closeAppearanceModal);
els.appearanceModal.addEventListener("click", (e) => {
  if (e.target === els.appearanceModal) closeAppearanceModal();
});

els.themeGrid.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-theme-id]");
  if (!btn) return;
  selectTheme(btn.dataset.themeId);
});

els.searchInput.addEventListener("input", () => {
  state.query = els.searchInput.value;
  els.clearSearchBtn.classList.toggle("hidden", !state.query);
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => runSearch(false), 90);
});

els.clearSearchBtn.addEventListener("click", () => {
  els.searchInput.value = "";
  state.query = "";
  els.clearSearchBtn.classList.add("hidden");
  runSearch(false);
  els.searchInput.focus();
});

els.onlyPrCheckbox.addEventListener("change", () => {
  state.onlyPr = els.onlyPrCheckbox.checked;
  runSearch(false);
});

els.machineFilters.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-machine]");
  if (!btn) return;
  state.machineId = btn.dataset.machine;
  renderFilterPills();
  runSearch(false);
});

els.toolFilters.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tool]");
  if (!btn) return;
  state.tool = btn.dataset.tool;
  renderFilterPills();
  runSearch(false);
});

els.resultsList.addEventListener("click", (e) => {
  const row = e.target.closest(".result-item");
  if (!row) return;
  state.selectedIndex = Number(row.dataset.index);
  renderList();
  renderDetail();
});

els.resultsList.addEventListener("dblclick", (e) => {
  const row = e.target.closest(".result-item");
  if (!row) return;
  state.selectedIndex = Number(row.dataset.index);
  triggerPrimaryForSelected();
});

els.syncNowBtn.addEventListener("click", async () => {
  els.syncNowBtn.disabled = true;
  els.syncNowBtn.textContent = "同步中…";
  try {
    const info = await postApi("/api/sync", {});
    await loadFacets();
    await runSearch(false);
    showToast(`同步完成：共索引 ${info.total_sessions} 条会话 (${info.elapsed_ms}ms)`);
  } finally {
    els.syncNowBtn.disabled = false;
    els.syncNowBtn.textContent = "同步索引";
  }
});

els.machineBadgeBtn.addEventListener("click", async () => {
  const current = state.facets?.current_machine?.machine_name || "";
  const next = window.prompt("设置当前电脑的易读名称（例如 MacBook Pro 或 Mac Studio）：", current);
  if (!next || !next.trim() || next.trim() === current) return;
  await postApi("/api/machine-name", { machine_name: next.trim() });
  await loadFacets();
  await runSearch(false);
  showToast(`已将当前设备名称更新为「${next.trim()}」`);
});

els.openLinkModalBtn.addEventListener("click", () => openLinkModal(null));
els.closeModalBtn.addEventListener("click", closeLinkModal);
els.cancelModalBtn.addEventListener("click", closeLinkModal);
els.linkModal.addEventListener("click", (e) => {
  if (e.target === els.linkModal) closeLinkModal();
});

els.manualLinkForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const selOpt = els.linkToolSelect.options[els.linkToolSelect.selectedIndex];
  const payload = {
    target_uid: els.linkTargetUid.value,
    pr_number: els.linkPrNumber.value,
    tool: els.linkToolSelect.value,
    tool_label: selOpt ? selOpt.textContent : "外部会话",
    external_url: els.linkExternalUrl.value,
    title: els.linkTitle.value,
    note: els.linkNote.value,
  };
  await postApi("/api/manual-link", payload);
  closeLinkModal();
  await loadFacets();
  await runSearch(false);
  showToast("已保存并同步到 iCloud Drive");
});

window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    els.searchInput.focus();
    els.searchInput.select();
    return;
  }
  if (!els.appearanceModal.classList.contains("hidden")) {
    if (e.key === "Escape") closeAppearanceModal();
    return;
  }
  if (!els.linkModal.classList.contains("hidden")) {
    if (e.key === "Escape") closeLinkModal();
    return;
  }
  if (e.key === "Escape" && document.activeElement === els.searchInput && state.query) {
    els.clearSearchBtn.click();
    return;
  }
  if (e.key === "ArrowDown" && state.items.length) {
    e.preventDefault();
    state.selectedIndex = Math.min(state.items.length - 1, state.selectedIndex + 1);
    renderList();
    renderDetail();
    document.querySelector(".result-item.selected")?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "ArrowUp" && state.items.length) {
    e.preventDefault();
    state.selectedIndex = Math.max(0, state.selectedIndex - 1);
    renderList();
    renderDetail();
    document.querySelector(".result-item.selected")?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && state.items.length && document.activeElement === els.searchInput) {
    e.preventDefault();
    triggerPrimaryForSelected();
  }
});

(async function init() {
  renderAppearanceOptions();
  await loadFacets();
  await runSearch(false);
  els.searchInput.focus();
})();
