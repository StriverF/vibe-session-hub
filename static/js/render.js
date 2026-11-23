import { copyText } from "./api.js";
import { canOpenRemoteDirectly, els, state } from "./state.js";
import {
  buildCrossMacJumpUrl,
  escapeHtml,
  formatTime,
  highlightText,
  renderToolLogo,
  shortenPath,
} from "./utils.js";

export function renderManualLinkToolOptions() {
  if (!els.linkToolSelect) return;
  const options = state.toolCatalog?.manual_link_options || [];
  if (!options.length) return;
  const prev = els.linkToolSelect.value;
  els.linkToolSelect.innerHTML = options
    .map(
      (opt) =>
        `<option value="${escapeHtml(opt.value)}">${escapeHtml(opt.label)}</option>`
    )
    .join("");
  if (prev && options.some((opt) => opt.value === prev)) {
    els.linkToolSelect.value = prev;
  }
}

export function renderFilterPills() {
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

export function renderPrBanner(onFetchRemotePr) {
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
    if (btn && onFetchRemotePr) btn.onclick = () => onFetchRemotePr();
  }
}

export function renderList() {
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

      const snippetSource =
        item.matched_snippet || item.first_prompt || item.summary || "";
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

export function renderDetail({ onOpenSession, onOpenLinkModal }) {
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

  const canTryOpenRemoteDirectly = canOpenRemoteDirectly(item);

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
  if (openBtn) openBtn.onclick = () => onOpenSession(item);

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
      const prStr = (item.prs || [])
        .map((p) => `#${p.number} (${p.url || ""})`)
        .join(", ");
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
    bindBtn.onclick = () => onOpenLinkModal(item);
  }
}
