"""Virtual display management for Win2Phone.

Everything here is user-mode display configuration (EnumDisplayDevices /
EnumDisplaySettings / ChangeDisplaySettingsEx / SetDisplayConfig) — no driver
installation and no elevation required.

Facts learned on the real machine (2026-11-08), each one cost a debugging round:

  * The VDD appears as a display device whose DeviceString is
    "Virtual Display Driver". Its GDI name (``\\\\.\\DISPLAYn``) is **unstable** —
    it went from DISPLAY9 to DISPLAY10 after one device restart — so never
    persist an index; always re-resolve by DeviceString.
  * ``EnumDisplayDevices`` also reports dormant GPU outputs (``\\\\.\\DISPLAY2``..)
    that have no monitor attached. ``EnumDisplaySettings`` raises for those, so
    "usable" == "EnumDisplaySettings succeeds". Picking such an entry by index
    is what made every display call fail with "handle is invalid".
  * A VDD device node can exist while the display is *detached* from the
    desktop. Attach/detach is SetDisplayConfig, the same mechanism as Win+P, and
    works without admin rights.
  * ``QueryDisplayConfig`` returns 87 (ERROR_INVALID_PARAMETER) on this machine
    for every flag/buffer combination tried, even with provably correct struct
    sizes (72/64 bytes). So topology is driven with the SDC_TOPOLOGY_*
    shortcuts instead of hand-editing path arrays.
"""
from __future__ import annotations

import ctypes

import win32api
import win32con

CDS_UPDATEREGISTRY = 0x00000001
CDS_NORESET = 0x10000000

VDD_NAME_HINT = "virtual display"

# SetDisplayConfig flags (wingdi.h).
# QDC_ALL_PATHS (0x1) looks like the "safer" choice but is a trap: with it the
# topology switch returns ERROR_SUCCESS and does nothing. QDC_ONLY_ACTIVE_PATHS
# actually applies. Verified by cycling detach/attach against each flag.
QDC_ONLY_ACTIVE_PATHS = 0x00000002
SDC_TOPOLOGY_INTERNAL = 0x00000001   # show only on the internal panel
SDC_TOPOLOGY_EXTEND = 0x00000004     # extend onto every available display
SDC_APPLY = 0x00000080
ERROR_SUCCESS = 0

UINT32 = ctypes.c_uint32

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetDisplayConfig.argtypes = [
    UINT32, ctypes.c_void_p, UINT32, ctypes.c_void_p, UINT32]
_user32.SetDisplayConfig.restype = ctypes.c_long


# ------------------------------------------------------------------ enumerate
def devices() -> list[dict]:
    """Every display output Windows reports, usable or not."""
    out: list[dict] = []
    i = 0
    while True:
        try:
            d = win32api.EnumDisplayDevices(None, i)
        except Exception:
            break
        if not d.DeviceName:
            break
        name = d.DeviceName
        w = h = x = y = None
        try:
            dm = win32api.EnumDisplaySettings(name, win32con.ENUM_CURRENT_SETTINGS)
            w, h = dm.PelsWidth, dm.PelsHeight
            x, y = getattr(dm, "Position_x", None), getattr(dm, "Position_y", None)
        except Exception:
            pass
        out.append({
            "index": i,
            "name": name,
            "string": d.DeviceString,
            "usable": w is not None,
            "width": w, "height": h, "left": x, "top": y,
        })
        i += 1
    return out


def usable_devices() -> list[dict]:
    return [d for d in devices() if d["usable"]]


def find_virtual() -> dict | None:
    """Locate the VDD by DeviceString, regardless of its current DISPLAYn name."""
    for d in devices():
        if VDD_NAME_HINT in (d["string"] or "").lower():
            return d
    return None


def primary() -> dict | None:
    """The primary display; its origin anchors every relative position."""
    usable = usable_devices()
    for d in usable:
        if d["left"] == 0 and d["top"] == 0:
            return d
    return usable[0] if usable else None


def geometry(device: str) -> tuple[int, int, int, int] | None:
    """(width, height, left, top), or None when the display is detached."""
    try:
        dm = win32api.EnumDisplaySettings(device, win32con.ENUM_CURRENT_SETTINGS)
    except Exception:
        return None
    return (dm.PelsWidth, dm.PelsHeight,
            getattr(dm, "Position_x", 0), getattr(dm, "Position_y", 0))


def modes(device: str) -> list[tuple[int, int, int]]:
    """All (width, height, refresh) modes the display advertises."""
    seen: set[tuple[int, int, int]] = set()
    i = 0
    while i < 4000:
        try:
            dm = win32api.EnumDisplaySettings(device, i)
        except Exception:
            break
        seen.add((dm.PelsWidth, dm.PelsHeight, getattr(dm, "DisplayFrequency", 0)))
        i += 1
    return sorted(seen)


# --------------------------------------------------------- attach / detach
def is_attached(device: str) -> bool:
    return geometry(device) is not None


def attach() -> bool:
    """Extend the desktop onto every available display (Win+P -> Extend).

    Idempotent, and it re-activates a VDD that was detached earlier.
    Caveat: this is the "extend to all" shortcut, so any other display that is
    present but currently off will be switched on as well.
    """
    rc = _user32.SetDisplayConfig(
        0, None, 0, None, QDC_ONLY_ACTIVE_PATHS | SDC_TOPOLOGY_EXTEND | SDC_APPLY)
    if rc != ERROR_SUCCESS:
        raise OSError(ctypes.get_last_error(), f"SetDisplayConfig(EXTEND) rc={rc}")
    return True


def detach() -> bool:
    """Drop every non-internal display, i.e. remove the VDD from the desktop.

    The device node stays installed, so the next attach() is instant. Windows
    pulls windows that lived on the removed display back onto the remaining one
    instead of stranding them off-screen.

    Caveat: "internal only" also drops a *real* external monitor if one is
    plugged in. Fine for the laptop + VDD setup this project targets.
    """
    rc = _user32.SetDisplayConfig(
        0, None, 0, None, QDC_ONLY_ACTIVE_PATHS | SDC_TOPOLOGY_INTERNAL | SDC_APPLY)
    if rc != ERROR_SUCCESS:
        raise OSError(ctypes.get_last_error(), f"SetDisplayConfig(INTERNAL) rc={rc}")
    return True


# --------------------------------------------------------------- modification
def _apply(device: str, dm) -> None:
    """Stage, then commit (the two-call pattern Win32 requires)."""
    rc = win32api.ChangeDisplaySettingsEx(
        device, dm, CDS_UPDATEREGISTRY | CDS_NORESET)
    if rc != 0:
        raise OSError(f"ChangeDisplaySettingsEx(stage) returned {rc}")
    rc = win32api.ChangeDisplaySettingsEx(None, None, 0)
    if rc != 0:
        raise OSError(f"ChangeDisplaySettingsEx(commit) returned {rc}")


def set_mode(device: str, width: int, height: int, freq: int = 0) -> tuple[int, int, int]:
    """Switch to an advertised mode, preferring an exact (w,h) match.

    The mode must exist in the driver's mode list — VDD builds that list from
    vdd_settings.xml — otherwise Windows silently keeps the old one.
    """
    candidates = modes(device)
    if not candidates:
        raise RuntimeError(f"{device} is detached (no modes)")
    exact = [m for m in candidates if m[0] == width and m[1] == height]
    if not exact:
        near = sorted(candidates, key=lambda m: abs(m[0] - width) + abs(m[1] - height))[:4]
        raise ValueError(
            f"{width}x{height} is not advertised; closest: "
            + ", ".join(f"{m[0]}x{m[1]}@{m[2]}" for m in near))
    pick = next((m for m in exact if freq and m[2] == freq), None)
    if pick is None:
        pick = sorted(exact, key=lambda m: (m[2] != 60, -m[2]))[0]
    dm = win32api.EnumDisplaySettings(device, win32con.ENUM_CURRENT_SETTINGS)
    dm.PelsWidth, dm.PelsHeight = pick[0], pick[1]
    dm.DisplayFrequency = pick[2]
    dm.Fields = (win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
                 | win32con.DM_DISPLAYFREQUENCY)
    _apply(device, dm)
    return pick


def set_position(device: str, x: int, y: int) -> tuple[int, int]:
    dm = win32api.EnumDisplaySettings(device, win32con.ENUM_CURRENT_SETTINGS)
    dm.Position_x = x
    dm.Position_y = y
    dm.Fields = win32con.DM_POSITION
    _apply(device, dm)
    return x, y


def set_side(device: str, side: str) -> tuple[int, int]:
    """Place the display on a chosen edge of the primary display.

    Uses the primary's real origin (a laptop panel is not always at 0,0) and the
    virtual display's *current* size, so it stays correct after a portrait /
    landscape switch.
    """
    if side not in ("right", "left", "top", "bottom"):
        raise ValueError(f"unknown side: {side}")
    pri = primary()
    if pri is None:
        raise RuntimeError("no primary display found")
    geo = geometry(device)
    if geo is None:
        raise RuntimeError(f"{device} is detached")
    w, h = geo[0], geo[1]
    px, py, pw, ph = pri["left"], pri["top"], pri["width"], pri["height"]
    positions = {
        "right": (px + pw, py),
        "left": (px - w, py),
        "top": (px, py - h),
        "bottom": (px, py + ph),
    }
    return set_position(device, *positions[side])


def place(device: str, side: str, width: int, height: int) -> tuple[int, int, int, int]:
    """Resize, then anchor to the requested edge. Returns the final geometry."""
    set_mode(device, width, height)
    set_side(device, side)
    return geometry(device)


# --------------------------------------------------------------------- report
def describe() -> str:
    lines = ["Display devices (pywin32 order):"]
    for d in devices():
        if d["usable"]:
            tag = " <-- virtual (VDD)" if VDD_NAME_HINT in (d["string"] or "").lower() else ""
            lines.append(f"  [{d['index']}] {d['name']}  {d['string']}  "
                         f"{d['width']}x{d['height']} @ ({d['left']},{d['top']}){tag}")
        else:
            lines.append(f"  [{d['index']}] {d['name']}  {d['string']}  (dormant)")
    v = find_virtual()
    if v is None:
        lines.append("VDD: not found — install VirtualDrivers.Virtual-Display-Driver")
        return "\n".join(lines)
    if v["usable"]:
        lines.append(f"VDD: {v['name']} {v['width']}x{v['height']} @ ({v['left']},{v['top']})")
        ms = modes(v["name"])
        lines.append("  modes: " + ", ".join(f"{w}x{h}@{f}" for w, h, f in ms[:12]))
    else:
        lines.append(f"VDD: {v['name']} (detached from the desktop)")
    return "\n".join(lines)
