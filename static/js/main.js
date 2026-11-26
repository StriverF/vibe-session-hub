import {
  addManualLink,
  copyText,
  fetchFacets,
  fetchSearch,
  openSession,
  syncNow,
  updateMachineName,
} from "./api.js";
import {
  renderDetail,
  renderFilterPills,
  renderList,
  renderManualLinkToolOptions,
  renderPrBanner,
} from "./render.js";
import { els, state, updateToolCatalog } from "./state.js";
import {
  applyThemeLocally,
  closeAppearanceModal,
  openAppearanceModal,
  renderAppearanceOptions,
  selectTheme,
  syncNativeWindowAppearance,
} from "./theme.js";
import {
  bindUpdaterEvents,
  closeUpdateModal,
  initUpdater,
  renderUpdateControl,
} from "./updater.js";
import { getDeviceTypeLabel, renderDeviceIcon } from "./device-icons.js";
import { buildCrossMacJumpUrl, showToast } from "./utils.js";

async function loadFacets() {
  const data = await fetchFacets();
  state.facets = data;
  if (data.server?.base_url) {
    state.serverBaseUrl = data.server.base_url;
  }
  if (data.server?.version) {
    state.serverVersion = data.server.version;
    renderUpdateControl();
  }
  if (data.tool_catalog) {
    updateToolCatalog(data.tool_catalog);
    renderManualLinkToolOptions();
  }
  if (data.preferences?.theme) {
    applyThemeLocally(data.preferences.theme);
  }
  const curMachine = data.current_machine || {};
  els.currentMachineName.textContent = `${curMachine.machine_name} (本机)`;
  if (els.currentMachineIcon) {
    els.currentMachineIcon.innerHTML = renderDeviceIcon(
      curMachine.machine_type,
      curMachine.machine_name,
      "device-icon-sm",
      { machineId: curMachine.machine_id }
    );
  }
  const modelLabel =
    curMachine.machine_model ||
    getDeviceTypeLabel(
      curMachine.machine_type,
      curMachine.machine_name,
      curMachine.machine_id
    );
  if (els.machineBadgeBtn) {
    els.machineBadgeBtn.title = modelLabel
      ? `${modelLabel} · 点击修改当前电脑显示名称`
      : "点击修改当前电脑显示名称";
  }
  els.prTotalCount.textContent = data.with_pr_count;
  renderFilterPills();
}

function renderSelectedDetail() {
  renderDetail({
    onOpenSession: triggerOpenSession,
    onOpenLinkModal: openLinkModal,
  });
}

async function runSearch(remotePr = false) {
  const data = await fetchSearch({
    query: state.query,
    tool: state.tool,
    machineId: state.machineId,
    onlyPr: state.onlyPr,
    remotePr,
  });
  state.items = data.items || [];
  state.resolvedPr = data.resolved_pr || null;
  state.selectedIndex = state.items.length ? 0 : -1;

  els.syncMeta.textContent = `共匹配 ${state.items.length} 条 · ${data.elapsed_ms}ms`;
  renderPrBanner(() => runSearch(true));
  renderList();
  renderSelectedDetail();
}

async function triggerOpenSession(item) {
  const data = await openSession(item.uid);
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

function openLinkModal(targetSession = null) {
  renderManualLinkToolOptions();
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

function bindEvents() {
  bindUpdaterEvents();
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
    renderSelectedDetail();
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
      const info = await syncNow({});
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
    const next = window.prompt(
      "设置当前电脑的易读名称（例如 MacBook Pro 或 Mac Studio）：",
      current
    );
    if (!next || !next.trim() || next.trim() === current) return;
    await updateMachineName(next.trim());
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
    await addManualLink(payload);
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
    if (els.updateModal && !els.updateModal.classList.contains("hidden")) {
      if (e.key === "Escape") closeUpdateModal();
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
    if (
      e.key === "Escape" &&
      document.activeElement === els.searchInput &&
      state.query
    ) {
      els.clearSearchBtn.click();
      return;
    }
    if (e.key === "ArrowDown" && state.items.length) {
      e.preventDefault();
      state.selectedIndex = Math.min(state.items.length - 1, state.selectedIndex + 1);
      renderList();
      renderSelectedDetail();
      document
        .querySelector(".result-item.selected")
        ?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp" && state.items.length) {
      e.preventDefault();
      state.selectedIndex = Math.max(0, state.selectedIndex - 1);
      renderList();
      renderSelectedDetail();
      document
        .querySelector(".result-item.selected")
        ?.scrollIntoView({ block: "nearest" });
    } else if (
      e.key === "Enter" &&
      state.items.length &&
      document.activeElement === els.searchInput
    ) {
      e.preventDefault();
      triggerPrimaryForSelected();
    }
  });
}

const NATIVE_NO_DRAG_SELECTOR =
  'button, a, input, select, textarea, label, [role="button"], [contenteditable="true"], [data-no-drag]';

function initNativeWindowChrome() {
  const handler = window.webkit?.messageHandlers?.vibeHubWindow;
  if (!window.__VIBE_HUB_NATIVE__ || !handler) return;

  const post = (payload) => {
    try {
      handler.postMessage(payload);
    } catch (_) {}
  };

  const applyMetrics = (metrics) => {
    if (!metrics || typeof metrics !== "object") return;
    const root = document.documentElement;
    root.classList.add("native-macos");
    root.classList.toggle("native-fullscreen", Boolean(metrics.isFullScreen));
    if (typeof metrics.leftInset === "number") {
      root.style.setProperty("--native-titlebar-left", `${metrics.leftInset}px`);
    }
    if (typeof metrics.height === "number") {
      root.style.setProperty("--native-titlebar-height", `${metrics.height}px`);
    }
  };

  window.__vibeHubReceiveWindowMetrics = applyMetrics;
  if (window.__VIBE_HUB_NATIVE_METRICS__) {
    applyMetrics(window.__VIBE_HUB_NATIVE_METRICS__);
  }
  post({ action: "requestMetrics" });
  syncNativeWindowAppearance(state.theme);

  const topbar = document.querySelector(".topbar");
  if (!topbar) return;

  let dragCleanup = null;
  const clearPendingDrag = () => {
    if (dragCleanup) {
      dragCleanup();
      dragCleanup = null;
    }
  };

  topbar.addEventListener("dragstart", (e) => {
    e.preventDefault();
  });

  topbar.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target instanceof Element && e.target.closest(NATIVE_NO_DRAG_SELECTOR)) {
      return;
    }
    e.preventDefault();
    clearPendingDrag();

    if (e.detail === 2) {
      post({ action: "doubleClickTitlebar" });
      return;
    }
    if (e.detail !== 1) return;

    const startX = e.clientX;
    const startY = e.clientY;
    const onMove = (ev) => {
      if ((ev.buttons & 1) !== 1) {
        clearPendingDrag();
        return;
      }
      if (Math.hypot(ev.clientX - startX, ev.clientY - startY) >= 3) {
        clearPendingDrag();
        post({ action: "startDrag" });
      }
    };
    const onCancel = () => clearPendingDrag();

    window.addEventListener("mousemove", onMove, true);
    window.addEventListener("mouseup", onCancel, true);
    window.addEventListener("blur", onCancel);
    window.addEventListener("contextmenu", onCancel, true);
    dragCleanup = () => {
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("mouseup", onCancel, true);
      window.removeEventListener("blur", onCancel);
      window.removeEventListener("contextmenu", onCancel, true);
    };
  });
}

async function init() {
  initNativeWindowChrome();
  renderAppearanceOptions();
  renderManualLinkToolOptions();
  bindEvents();
  initUpdater();
  await loadFacets();
  await runSearch(false);
  els.searchInput.focus();
}

init();
