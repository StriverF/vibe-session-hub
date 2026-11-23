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

export function openSession(uid) {
  return postApi("/api/open", { uid });
}

export function addManualLink(payload) {
  return postApi("/api/manual-link", payload);
}
