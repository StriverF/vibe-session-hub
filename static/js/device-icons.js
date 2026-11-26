const VALID_DEVICE_TYPES = new Set([
  "macbook-pro",
  "macbook-air",
  "macbook",
  "mac-studio",
  "mac-mini",
  "imac",
  "mac-pro",
  "mac",
  "shared",
]);

export const DEVICE_TYPE_LABELS = {
  "macbook-pro": "MacBook Pro",
  "macbook-air": "MacBook Air",
  macbook: "MacBook",
  "mac-studio": "Mac Studio",
  "mac-mini": "Mac mini",
  imac: "iMac",
  "mac-pro": "Mac Pro",
  mac: "Mac",
  shared: "跨设备共享",
};

const NAME_PATTERNS = [
  [/(?:^|[^a-z])mac\s*[-_]?\s*studio(?:[^a-z]|$)/i, "mac-studio"],
  [/(?:^|[^a-z])mac\s*[-_]?\s*mini(?:[^a-z]|$)/i, "mac-mini"],
  [/(?:^|[^a-z])(?:mac\s*[-_]?\s*book\s*[-_]?\s*pro|mbp)(?:[^a-z]|$)/i, "macbook-pro"],
  [/(?:^|[^a-z])(?:mac\s*[-_]?\s*book\s*[-_]?\s*air|mba)(?:[^a-z]|$)/i, "macbook-air"],
  [/(?:^|[^a-z])imac(?:[^a-z]|$)/i, "imac"],
  [/(?:^|[^a-z])mac\s*[-_]?\s*pro(?:[^a-z]|$)/i, "mac-pro"],
  [/(?:^|[^a-z])mac\s*[-_]?\s*book(?:[^a-z]|$)/i, "macbook"],
];

const ICON_ASSET_VERSION = "2";

// Capture-phase image error handler so non-macOS / offline renders fall back
// cleanly to the realistic multi-tone SVG without inline event attributes.
if (typeof document !== "undefined") {
  document.addEventListener(
    "error",
    (ev) => {
      const target = ev.target;
      if (target && target.classList && target.classList.contains("device-icon-img")) {
        const wrap = target.closest(".device-icon");
        if (wrap) wrap.classList.add("is-fallback");
      }
    },
    true
  );
}

function inferFromText(text) {
  const cleaned = String(text || "").trim();
  if (!cleaned) return "";
  for (const [re, type] of NAME_PATTERNS) {
    if (re.test(cleaned)) return type;
  }
  return "";
}

export function resolveDeviceType(machineType, machineName = "", machineId = "") {
  if (String(machineId || "").trim().toLowerCase() === "shared") {
    return "shared";
  }
  const normalized = String(machineType || "").trim().toLowerCase();
  if (VALID_DEVICE_TYPES.has(normalized) && normalized !== "mac") {
    return normalized;
  }
  const inferred = inferFromText(machineName);
  if (inferred) return inferred;
  if (VALID_DEVICE_TYPES.has(normalized)) return normalized;
  return "mac";
}

export function getDeviceTypeLabel(machineType, machineName = "", machineId = "") {
  const type = resolveDeviceType(machineType, machineName, machineId);
  return DEVICE_TYPE_LABELS[type] || "Mac";
}

export function getDeviceIconUrl(machineType, machineName = "", machineId = "") {
  const type = resolveDeviceType(machineType, machineName, machineId);
  return `/api/device-icons/${type}.png?v=${ICON_ASSET_VERSION}`;
}

// Multi-tone metallic hardware SVGs used only as graceful fallback when native
// macOS CoreTypes PNGs are unavailable (e.g., non-macOS test environments).
const DEVICE_FALLBACK_SVGS = {
  "mac-studio": `
    <rect x="2.2" y="3.5" width="19.6" height="11.6" rx="2.2" fill="#D4D8DF" stroke="#9EA5B2" stroke-width="0.9"/>
    <rect x="2.8" y="4.1" width="18.4" height="2.4" rx="1.2" fill="#ECEFF4" fill-opacity="0.75"/>
    <rect x="4.6" y="15.1" width="14.8" height="2.0" rx="0.8" fill="#5C626E"/>
    <rect x="5.4" y="10.8" width="1.1" height="2.3" rx="0.5" fill="#2A2E35"/>
    <rect x="7.5" y="10.8" width="1.1" height="2.3" rx="0.5" fill="#2A2E35"/>
    <rect x="9.8" y="11.4" width="3.8" height="1.1" rx="0.5" fill="#2A2E35"/>
    <circle cx="18.3" cy="12.0" r="0.7" fill="#FFFFFF"/>
  `,

  "mac-mini": `
    <path d="M4.2 6.8C4.8 5.6 6.0 5.0 7.5 5.0H16.5C18.0 5.0 19.2 5.6 19.8 6.8L21.0 9.0H3.0L4.2 6.8Z" fill="#E3E6EC"/>
    <circle cx="12.0" cy="7.0" r="1.25" fill="#2D3138"/>
    <rect x="2.6" y="8.8" width="18.8" height="5.6" rx="1.6" fill="#C9CED7" stroke="#969DAA" stroke-width="0.85"/>
    <rect x="5.6" y="11.0" width="1.0" height="1.8" rx="0.45" fill="#2E323A"/>
    <rect x="7.4" y="11.0" width="1.0" height="1.8" rx="0.45" fill="#2E323A"/>
    <circle cx="17.8" cy="11.9" r="0.65" fill="#FFFFFF"/>
    <rect x="5.0" y="14.4" width="14.0" height="1.4" rx="0.7" fill="#5A606B"/>
  `,

  "macbook-pro": `
    <rect x="3.2" y="2.4" width="17.6" height="11.2" rx="1.4" fill="#181C24" stroke="#A7AEBC" stroke-width="0.85"/>
    <rect x="4.1" y="3.3" width="15.8" height="9.4" rx="0.7" fill="#4BA3F2"/>
    <rect x="10.3" y="3.2" width="3.4" height="1.15" rx="0.4" fill="#181C24"/>
    <rect x="1.2" y="13.7" width="21.6" height="2.6" rx="0.8" fill="#CFD4DC" stroke="#99A1AE" stroke-width="0.8"/>
    <rect x="9.6" y="13.7" width="4.8" height="0.9" rx="0.4" fill="#8B93A1"/>
    <rect x="3.0" y="16.4" width="2.4" height="1.0" rx="0.45" fill="#3D424D"/>
    <rect x="18.6" y="16.4" width="2.4" height="1.0" rx="0.45" fill="#3D424D"/>
  `,

  "macbook-air": `
    <rect x="3.8" y="3.2" width="16.4" height="10.4" rx="1.3" fill="#1B1F27" stroke="#D8BE96" stroke-width="0.85"/>
    <rect x="4.6" y="4.0" width="14.8" height="8.8" rx="0.6" fill="#59B2F8"/>
    <path d="M1.4 13.9H22.6L20.8 15.8H3.2L1.4 13.9Z" fill="#DFC398" stroke="#B79A6D" stroke-width="0.75"/>
  `,

  macbook: `
    <rect x="3.6" y="3.0" width="16.8" height="10.6" rx="1.3" fill="#1A1E26" stroke="#AEB5C2" stroke-width="0.85"/>
    <rect x="4.5" y="3.8" width="15.0" height="9.0" rx="0.6" fill="#4EA6F4"/>
    <rect x="1.8" y="13.9" width="20.4" height="2.0" rx="0.9" fill="#D2D7DF"/>
  `,

  imac: `
    <rect x="2.6" y="2.0" width="18.8" height="12.2" rx="1.3" fill="#1A1E26" stroke="#9EA5B2" stroke-width="0.85"/>
    <rect x="3.5" y="2.9" width="17.0" height="8.2" rx="0.5" fill="#4DA6F5"/>
    <path d="M2.9 11.4H21.1V13.1C21.1 13.65 20.65 14.1 20.1 14.1H3.9C3.35 14.1 2.9 13.65 2.9 13.1V11.4Z" fill="#D5D9E0"/>
    <circle cx="12.0" cy="12.75" r="0.65" fill="#2A2E36"/>
    <path d="M10.1 14.2L9.3 17.4H14.7L13.9 14.2Z" fill="#BFC5CF"/>
    <rect x="7.4" y="17.1" width="9.2" height="1.2" rx="0.6" fill="#D5D9E0"/>
  `,

  "mac-pro": `
    <path d="M7.6 3.5V2.2C7.6 1.75 8.0 1.4 8.5 1.4H15.5C16.0 1.4 16.4 1.75 16.4 2.2V3.5" stroke="#B8BFC9" stroke-width="1.35" stroke-linecap="round"/>
    <rect x="5.8" y="3.5" width="12.4" height="13.2" rx="1.4" fill="#D5D9E1" stroke="#9DA4B0" stroke-width="0.9"/>
    <circle cx="12.0" cy="10.0" r="1.45" fill="#23272F"/>
    <path d="M7.8 16.7V18.4M16.2 16.7V18.4" stroke="#A6ADB9" stroke-width="1.5" stroke-linecap="round"/>
  `,

  mac: `
    <rect x="2.8" y="2.4" width="18.4" height="11.8" rx="1.3" fill="#1A1E26" stroke="#A4ACB8" stroke-width="0.85"/>
    <rect x="3.7" y="3.3" width="16.6" height="10.0" rx="0.5" fill="#4BA3F2"/>
    <rect x="10.3" y="14.3" width="3.4" height="3.0" fill="#C4C9D2"/>
    <rect x="7.8" y="17.1" width="8.4" height="1.2" rx="0.6" fill="#D5D9E0"/>
  `,

  shared: `
    <circle cx="12" cy="10" r="7.4" fill="#2563EB" stroke="#93C5FD" stroke-width="0.9"/>
    <ellipse cx="12" cy="10" rx="3.4" ry="7.2" stroke="#DBEAFE" stroke-width="0.95" stroke-opacity="0.8"/>
    <path d="M4.8 10H19.2M6.2 6.8H17.8M6.2 13.2H17.8" stroke="#DBEAFE" stroke-width="0.9" stroke-opacity="0.75"/>
  `,
};

export function renderDeviceIcon(
  machineType,
  machineName = "",
  sizeClass = "device-icon-sm",
  options = {}
) {
  const type = resolveDeviceType(machineType, machineName, options.machineId || "");
  const label = DEVICE_TYPE_LABELS[type] || "Mac";
  const imgUrl = `/api/device-icons/${type}.png?v=${ICON_ASSET_VERSION}`;
  const fallbackBody = DEVICE_FALLBACK_SVGS[type] || DEVICE_FALLBACK_SVGS.mac;
  return `<span class="device-icon ${sizeClass}" data-device-type="${type}" aria-hidden="true" title="${label}"><img class="device-icon-img" src="${imgUrl}" alt="" decoding="async" /><svg class="device-icon-fallback" viewBox="0 0 24 20" fill="none" width="100%" height="100%">${fallbackBody}</svg></span>`;
}
