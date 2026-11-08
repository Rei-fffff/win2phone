"""Composite the Windows mouse cursor into a captured frame.

Why this exists: ``mss`` captures through GDI ``BitBlt`` on the screen DC, and
the cursor is not part of that bitmap — it is drawn on a separate plane. On a
normal GPU-driven monitor the cursor still ends up in the capture, but on the
IddCx virtual display it does not (measured: moving the cursor across the
virtual display changed 0 pixels; the same move on the primary changed ~9.4 kB).
Flipping the VDD ``HardwareCursor`` setting either way did not help.

So we draw it ourselves: GetCursorInfo -> GetIconInfo -> GetDIBits, then alpha
blend the 32x32-ish sprite into the BGRA frame at the cursor's position.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

CURSOR_SHOWING = 0x00000001
DIB_RGB_COLORS = 0
BI_RGB = 0

SRCCOPY = 0x00CC0020


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hCursor", wt.HANDLE),
        ("ptScreenPos", wt.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask", wt.HBITMAP),
        ("hbmColor", wt.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wt.LONG),
        ("bmWidth", wt.LONG),
        ("bmHeight", wt.LONG),
        ("bmWidthBytes", wt.LONG),
        ("bmPlanes", wt.WORD),
        ("bmBitsPixel", wt.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


# Explicit prototypes: GDI handles are pointer-sized, and ctypes would otherwise
# marshal a Python int as a 32-bit C int and raise OverflowError.
_user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
_user32.GetCursorInfo.restype = wt.BOOL
_user32.GetIconInfo.argtypes = [wt.HANDLE, ctypes.POINTER(ICONINFO)]
_user32.GetIconInfo.restype = wt.BOOL
_user32.GetDC.argtypes = [wt.HWND]
_user32.GetDC.restype = wt.HDC
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.DeleteDC.argtypes = [wt.HDC]
_gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetObjectW.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HANDLE, wt.UINT, wt.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [wt.HANDLE]


class Cursor:
    """Cached cursor sprite: position, hotspot and a straight-alpha BGRA bitmap."""
    __slots__ = ("h", "x", "y", "hot_x", "hot_y", "w", "h_px", "pixels", "mono", "mask")

    def __init__(self):
        self.h = None
        self.x = self.y = 0
        self.hot_x = self.hot_y = 0
        self.w = self.h_px = 0
        self.pixels = b""
        self.mono = False
        self.mask = b""


def _dib_bits(hbm, w: int, h: int, bpp: int) -> bytes:
    """Read a GDI bitmap as top-down, tightly packed rows."""
    hdc = _user32.GetDC(None)
    mem = _gdi32.CreateCompatibleDC(hdc)
    try:
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h            # negative -> top-down rows
        bi.biPlanes = 1
        bi.biBitCount = bpp
        bi.biCompression = BI_RGB
        stride = ((w * bpp + 31) // 32) * 4
        buf = ctypes.create_string_buffer(stride * h)
        got = _gdi32.GetDIBits(mem, hbm, 0, h, buf,
                               ctypes.byref(bi), DIB_RGB_COLORS)
        if got == 0:
            return b""
        return buf.raw
    finally:
        _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(None, hdc)


def _load(hcursor) -> tuple[int, int, int, int, bytes, bool, bytes]:
    """Decode one HCURSOR into (w, h, hotx, hoty, bgra, mono, mask)."""
    ii = ICONINFO()
    if not _user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return 0, 0, 0, 0, b"", False, b""
    try:
        bmp = BITMAP()
        src = ii.hbmColor or ii.hbmMask
        if not src:
            return 0, 0, 0, 0, b"", False, b""
        _gdi32.GetObjectW(src, ctypes.sizeof(BITMAP), ctypes.byref(bmp))
        w, h = bmp.bmWidth, bmp.bmHeight
        if not (0 < w <= 512 and 0 < h <= 512):
            return 0, 0, 0, 0, b"", False, b""
        if ii.hbmColor:
            color = _dib_bits(ii.hbmColor, w, h, 32)
            if not color:
                return 0, 0, 0, 0, b"", False, b""
            # Some legacy cursors come back with an all-zero alpha channel;
            # treat those as fully opaque rather than invisible.
            has_alpha = any(color[i] for i in range(3, len(color), 4))
            if not has_alpha:
                color = bytes(
                    b for i in range(0, len(color), 4)
                    for b in (color[i], color[i + 1], color[i + 2], 255))
            return w, h, ii.xHotspot, ii.yHotspot, color, False, b""
        mask = _dib_bits(ii.hbmMask, w, h, 1)
        return w, h, ii.xHotspot, ii.yHotspot, b"", True, mask
    finally:
        for hbm in (ii.hbmColor, ii.hbmMask):
            if hbm:
                _gdi32.DeleteObject(hbm)


class CursorCompositor:
    """Draws the live cursor into a BGRA frame that covers one monitor."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple] = {}
        self._sprite = Cursor()
        self.drawn = 0

    def _sprite_for(self, hcursor) -> Cursor:
        key = int(hcursor or 0)
        if key in self._cache:
            data = self._cache[key]
        else:
            data = _load(hcursor)
            if len(self._cache) > 32:      # cursors are few; keep the cache tiny
                self._cache.clear()
            self._cache[key] = data
        s = self._sprite
        s.h = key
        s.w, s.h_px, s.hot_x, s.hot_y, s.pixels, s.mono, s.mask = data
        return s

    def draw(self, buf: bytearray, mon: dict) -> bool:
        """Blend the cursor into ``buf`` (BGRA, mon-sized). Returns True if drawn."""
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not _user32.GetCursorInfo(ctypes.byref(ci)):
            return False
        if not (ci.flags & CURSOR_SHOWING) or not ci.hCursor:
            return False

        s = self._sprite_for(ci.hCursor)
        if not s.w or not s.h_px:
            return False

        # Monitor-local top-left corner of the sprite.
        px = ci.ptScreenPos.x - s.hot_x - mon["left"]
        py = ci.ptScreenPos.y - s.hot_y - mon["top"]
        if (px + s.w <= 0 or py + s.h_px <= 0
                or px >= mon["width"] or py >= mon["height"]):
            return False

        stride = mon["width"] * 4
        sprite = s.pixels
        sw, sh = s.w, s.h_px

        # Clip against the monitor edges.
        x0 = max(0, px)
        y0 = max(0, py)
        x1 = min(mon["width"], px + sw)
        y1 = min(mon["height"], py + sh)
        if x0 >= x1 or y0 >= y1:
            return False

        for yy in range(y0, y1):
            sy = yy - py
            row = (yy * stride) + x0 * 4
            srow = (sy * sw + (x0 - px)) * 4
            for xx in range(x0, x1):
                if s.mono:
                    # 1bpp AND mask: 1 leaves the desktop alone, 0 inverts it.
                    idx = srow >> 3
                    bit = 0x80 >> (srow & 7)
                    srow += 1
                    if idx < len(s.mask) and not (s.mask[idx] & bit):
                        buf[row] ^= 0xFF
                        buf[row + 1] ^= 0xFF
                        buf[row + 2] ^= 0xFF
                    row += 4
                    continue
                a = sprite[srow + 3]
                if a:
                    if a == 255:
                        buf[row] = sprite[srow]
                        buf[row + 1] = sprite[srow + 1]
                        buf[row + 2] = sprite[srow + 2]
                        buf[row + 3] = 255
                    else:
                        ia = 255 - a
                        buf[row] = (sprite[srow] * a + buf[row] * ia) // 255
                        buf[row + 1] = (sprite[srow + 1] * a + buf[row + 1] * ia) // 255
                        buf[row + 2] = (sprite[srow + 2] * a + buf[row + 2] * ia) // 255
                        buf[row + 3] = 255
                srow += 4
                row += 4
        self.drawn += 1
        return True
