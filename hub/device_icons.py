"""Extract, trim, and cache native macOS hardware icons for device types.

On macOS, Apple ships photorealistic hardware ``.icns`` icons inside
``/System/Library/CoreServices/CoreTypes.bundle`` (the same icons shown in
Finder, AirDrop, and About This Mac).  This module renders them to crisp PNGs
at runtime via ``/usr/bin/sips``, trims excess transparent margins using pure
Python stdlib (no Pillow dependency), and caches the bytes in memory so no
proprietary Apple image assets need to be committed to the repository.
"""

from __future__ import annotations

import logging
import platform
import struct
import subprocess
import tempfile
import threading
import zlib
from pathlib import Path

from .config import VALID_MACHINE_TYPES

logger = logging.getLogger("vibe_session_hub.device_icons")

_CORETYPES_ROOT = Path("/System/Library/CoreServices/CoreTypes.bundle/Contents")
_CORETYPES_RES = _CORETYPES_ROOT / "Resources"
_CORETYPES_LIB = _CORETYPES_ROOT / "Library"

# Strict allowlist of servable device icon keys
ALLOWED_DEVICE_ICON_TYPES: frozenset[str] = frozenset(VALID_MACHINE_TYPES)

# Ordered candidate .icns paths per device type, preferring distinct, high-contrast
# photorealistic icons across macOS 13..26 (with fallbacks for older macOS versions).
_ICON_CANDIDATES: dict[str, tuple[Path, ...]] = {
    "mac-studio": (
        _CORETYPES_RES / "com.apple.macstudio.icns",
        _CORETYPES_LIB / "CoreTypes-0021.bundle" / "Contents" / "Resources" / "com.apple.macmini-2024.icns",
        _CORETYPES_RES / "com.apple.macmini-2020.icns",
    ),
    "mac-mini": (
        _CORETYPES_LIB / "CoreTypes-0021.bundle" / "Contents" / "Resources" / "com.apple.macmini-2024.icns",
        _CORETYPES_RES / "com.apple.macmini-2020.icns",
        _CORETYPES_RES / "com.apple.macmini-2018.icns",
        _CORETYPES_RES / "com.apple.macmini-unibody.icns",
        _CORETYPES_RES / "com.apple.macmini.icns",
    ),
    "macbook-pro": (
        _CORETYPES_RES / "com.apple.macbookpro-16-2021-silver.icns",
        _CORETYPES_RES / "com.apple.macbookpro-14-2021-silver.icns",
        _CORETYPES_RES / "com.apple.macbookpro-16-silver.icns",
        _CORETYPES_RES / "com.apple.macbookpro-15-retina-display.icns",
        _CORETYPES_RES / "com.apple.macbookpro-13-unibody.icns",
    ),
    "macbook-air": (
        # Warm gold wedge Air provides immediate visual distinction from silver MacBook Pro
        _CORETYPES_RES / "com.apple.macbookair-2018-gold.icns",
        _CORETYPES_LIB / "CoreTypes-0009.bundle" / "Contents" / "Resources" / "com.apple.macbookair-15-2023-starlight.icns",
        _CORETYPES_RES / "com.apple.macbookair-13-2022-stardust.icns",
        _CORETYPES_RES / "com.apple.macbookair-13-2022-silver.icns",
        _CORETYPES_RES / "com.apple.macbookair-13-unibody.icns",
        _CORETYPES_RES / "com.apple.macbookair.icns",
    ),
    "macbook": (
        _CORETYPES_RES / "com.apple.macbook-retina-silver.icns",
        _CORETYPES_RES / "com.apple.macbook-unibody.icns",
        _CORETYPES_RES / "com.apple.macbook-white.icns",
    ),
    "imac": (
        # Classic vivid-blue Retina display + brushed aluminum chin pops on both dark & light themes
        _CORETYPES_RES / "com.apple.imac-unibody-27.icns",
        _CORETYPES_RES / "com.apple.imac-unibody-21.icns",
        _CORETYPES_LIB / "CoreTypes-0020.bundle" / "Contents" / "Resources" / "com.apple.imac-2024-blue.icns",
        _CORETYPES_RES / "com.apple.imac-2021-blue.icns",
    ),
    "mac-pro": (
        _CORETYPES_RES / "com.apple.macpro-2019.icns",
        _CORETYPES_RES / "com.apple.macpro-cylinder.icns",
        _CORETYPES_RES / "com.apple.macpro.icns",
    ),
    "mac": (
        _CORETYPES_RES / "com.apple.studio-display.icns",
        _CORETYPES_RES / "com.apple.imac-unibody-27.icns",
    ),
    "shared": (
        _CORETYPES_RES / "GenericNetworkIcon.icns",
    ),
}

_ICON_CACHE_LOCK = threading.Lock()
_ICON_CACHE: dict[str, bytes | None] = {}


def _read_png_rgba(data: bytes) -> tuple[int, int, list[bytearray]] | None:
    """Decode an 8-bit non-interlaced RGBA PNG into scanlines using stdlib only."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    idat = bytearray()
    width = height = bit_depth = color_type = interlace = 0
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            return None
        chunk = data[pos + 8 : end]
        pos = end + 4
        if ctype == b"IHDR" and len(chunk) >= 13:
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk[:13]
            )
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break

    if width <= 0 or height <= 0 or bit_depth != 8 or color_type != 6 or interlace != 0:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None

    bpp = 4
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        return None

    rows: list[bytearray] = []
    prev = bytearray(stride)
    idx = 0
    for _ in range(height):
        ftype = raw[idx]
        idx += 1
        row = bytearray(raw[idx : idx + stride])
        idx += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        else:
            return None
        rows.append(row)
        prev = row
    return width, height, rows


def _encode_png_rgba(width: int, height: int, rows: list[bytes]) -> bytes:
    def _chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def trim_png_transparent_margins(
    png_bytes: bytes,
    *,
    alpha_threshold: int = 18,
    pad_px: int = 3,
) -> bytes:
    """Crop excess transparent borders from an RGBA PNG while keeping a tiny pad."""
    decoded = _read_png_rgba(png_bytes)
    if decoded is None:
        return png_bytes
    w, h, rows = decoded
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        row = rows[y]
        for x in range(w):
            if row[x * 4 + 3] >= alpha_threshold:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < min_x or max_y < min_y:
        return png_bytes
    min_x = max(0, min_x - pad_px)
    min_y = max(0, min_y - pad_px)
    max_x = min(w - 1, max_x + pad_px)
    max_y = min(h - 1, max_y + pad_px)
    nw = max_x - min_x + 1
    nh = max_y - min_y + 1
    if nw == w and nh == h:
        return png_bytes
    cropped = [bytes(rows[y][min_x * 4 : (max_x + 1) * 4]) for y in range(min_y, max_y + 1)]
    return _encode_png_rgba(nw, nh, cropped)


def resolve_macos_icns_path(device_type: str) -> Path | None:
    """Return the first existing macOS CoreTypes .icns path for *device_type*."""
    key = str(device_type or "").strip().lower()
    if key not in ALLOWED_DEVICE_ICON_TYPES:
        return None
    for candidate in _ICON_CANDIDATES.get(key, ()):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _render_icns_to_png(icns_path: Path, max_dim: int = 128) -> bytes | None:
    """Convert a macOS .icns file to a trimmed PNG using /usr/bin/sips."""
    sips_bin = Path("/usr/bin/sips")
    if not sips_bin.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="vsh-icon-") as td:
        out_png = Path(td) / "icon.png"
        try:
            res = subprocess.run(
                [
                    str(sips_bin),
                    "-s",
                    "format",
                    "png",
                    "-Z",
                    str(max_dim),
                    str(icns_path),
                    "--out",
                    str(out_png),
                ],
                capture_output=True,
                timeout=4,
                check=False,
            )
        except Exception as exc:
            logger.debug("sips failed for %s: %s", icns_path, exc)
            return None
        if res.returncode != 0 or not out_png.is_file():
            return None
        try:
            raw = out_png.read_bytes()
        except OSError:
            return None
    return trim_png_transparent_margins(raw)


def get_device_icon_png(device_type: str) -> bytes | None:
    """Return cached photorealistic PNG bytes for *device_type* on macOS, or None."""
    key = str(device_type or "").strip().lower()
    if key not in ALLOWED_DEVICE_ICON_TYPES:
        return None
    with _ICON_CACHE_LOCK:
        if key in _ICON_CACHE:
            return _ICON_CACHE[key]

    data: bytes | None = None
    if platform.system() == "Darwin":
        icns_path = resolve_macos_icns_path(key)
        if icns_path is not None:
            data = _render_icns_to_png(icns_path)

    with _ICON_CACHE_LOCK:
        _ICON_CACHE[key] = data
    return data


def clear_device_icon_cache() -> None:
    """Clear the in-memory device icon cache (useful for tests)."""
    with _ICON_CACHE_LOCK:
        _ICON_CACHE.clear()
