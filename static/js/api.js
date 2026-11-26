import { showToast } from "./utils.js";

export async function postApi(url, payload = {}) {
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

export async function fetchFacets() {
  const res = await fetch("/api/facets");
  return res.json();
}

export async function fetchSearch({ query, tool, machineId, onlyPr, remotePr }) {
  const params = new URLSearchParams({
    q: query,
    tool,
    machine_id: machineId,
    only_pr: onlyPr ? "1" : "0",
    remote_pr: remotePr ? "1" : "0",
  });
  const res = await fetch(`/api/search?${params.toString()}`);
  return res.json();
}

export async function copyText(text, successMsg) {
  try {
    await postApi("/api/copy", { text });
  } catch {
    try {
      await navigator.clipboard.writeText(text);
    } catch {}
  }
  showToast(successMsg || "已复制到剪贴板");
}

export function syncNow(payload = {}) {
  return postApi("/api/sync", payload);
}

export function updateMachineName(machineName) {
  return postApi("/api/machine-name", { machine_name: machineName });
}

export function saveThemePreference(themeId) {
  return postApi("/api/preferences", { theme: themeId });
}

const pendingNativeLauncher = new Map();
let nativeLauncherSeq = 0;

if (typeof window !== "undefined") {
  window.__vibeHubResolveLauncher = (requestId, payload) => {
    const entry = pendingNativeLauncher.get(String(requestId));
    if (!entry) return;
    pendingNativeLauncher.delete(String(requestId));
    clearTimeout(entry.timer);
    entry.resolve(payload);
  };
}

function getNativeLauncherBridge() {
  if (typeof window === "undefined" || !window.__VIBE_HUB_NATIVE__) {
    return null;
  }
  return window.webkit?.messageHandlers?.vibeHubLauncher || null;
}

function invokeNativeAntigravityOpen(payload) {
  const bridge = getNativeLauncherBridge();
  if (!bridge) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    const requestId = `native_${++nativeLauncherSeq}`;
    const timer = setTimeout(() => {
      pendingNativeLauncher.delete(requestId);
      resolve(null);
    }, 20000);
    pendingNativeLauncher.set(requestId, { resolve, timer });
    try {
      bridge.postMessage({
        action: "openAntigravity",
        requestId,
        sessionId: String(payload?.session_id || ""),
        title: String(payload?.title || ""),
        cwd: String(payload?.cwd || ""),
      });
    } catch {
      pendingNativeLauncher.delete(requestId);
      clearTimeout(timer);
      resolve(null);
    }
  });
}

export async function openSession(uid) {
  const hasNativeBridge = Boolean(getNativeLauncherBridge());
  const res = await postApi("/api/open", {
    uid,
    defer_native_ui: hasNativeBridge,
  });
  if (res && res.action === "native_antigravity_open" && res.native_payload) {
    const nativeRes = await invokeNativeAntigravityOpen(res.native_payload);
    if (nativeRes && typeof nativeRes === "object") {
      return nativeRes;
    }
    return postApi("/api/open", { uid, defer_native_ui: false });
  }
  return res;
}

export function addManualLink(payload) {
  return postApi("/api/manual-link", payload);
}

export async function fetchUpdateStatus() {
  const res = await fetch("/api/update/status", { cache: "no-store" });
  return res.json();
}

export function triggerUpdateCheck() {
  return postApi("/api/update/check", {});
}

export async function fetchChangelog({ remote = false } = {}) {
  const url = remote ? "/api/update/changelog?remote=1" : "/api/update/changelog";
  const res = await fetch(url, { cache: "no-store" });
  return res.json();
}

