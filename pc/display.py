"""Virtual display management: enumerate, reposition, resize.

Uses pywin32's PyDEVMODE + ChangeDisplaySettingsEx. Everything here is a
*user-mode display settings* change, not a driver installation.

NOTE: PyDEVMODE attribute names differ slightly between pywin32 releases.
If `Position_x` / `Position_y` / `Fields` raise AttributeError, run
`python -c "import win32api,win32con;d=win32api.EnumDisplaySettings(None,win32con.ENUM_CURRENT_SETTINGS);print(dir(d))"`
and adapt the three setters below.
"""
from __future__ import annotations

import win32api
import win32con

CDS_NORESET = 0x10000000
CDS_UPDATEREGISTRY = 0x00000001


def devices() -> list[dict]:
    """Enumerate display devices in the same order Windows reports them."""
    out: list[dict] = []
    i = 0
    while True:
        try:
            d = win32api.EnumDisplayDevices(None, i)
        except Exception:
            break
        if not d.DeviceName:
            break
        out.append({
            "index": i,
            "name": d.DeviceName,
            "string": d.DeviceString,
            "state": d.StateFlags,
        })
        i += 1
    return out


def devmode(device: str):
    return win32api.EnumDisplaySettings(device, win32con.ENUM_CURRENT_SETTINGS)


def primary_size() -> tuple[int, int]:
    dm = devmode(win32api.EnumDisplayDevices(None, 0).DeviceName)
    return dm.PelsWidth, dm.PelsHeight


def current_geometry(device: str) -> tuple[int, int, int, int]:
    dm = devmode(device)
    return dm.PelsWidth, dm.PelsHeight, getattr(dm, "Position_x", 0), getattr(dm, "Position_y", 0)


def _apply(device: str, dm) -> None:
    # Stage the change, then commit globally (two-call pattern required by Win32).
    win32api.ChangeDisplaySettingsEx(device, dm, CDS_UPDATEREGISTRY | CDS_NORESET)
    win32api.ChangeDisplaySettingsEx(None, None, 0)


def set_position(device: str, x: int, y: int) -> tuple[int, int]:
    """Move this display relative to the primary one."""
    dm = devmode(device)
    dm.Position_x = x
    dm.Position_y = y
    dm.Fields = win32con.DM_POSITION
    _apply(device, dm)
    return x, y


def set_side(device: str, side: str) -> tuple[int, int]:
    """Place the virtual display on a chosen edge of the primary display.

    This is what determines which screen edge the mouse crosses onto the phone.
    """
    pw, ph = primary_size()
    w, h, _, _ = current_geometry(device)
    positions = {
        "right": (pw, 0),
        "left": (-w, 0),
        "top": (0, -h),
        "bottom": (0, ph),
    }
    if side not in positions:
        raise ValueError(f"unknown side: {side}")
    return set_position(device, *positions[side])


def set_resolution(device: str, width: int, height: int, freq: int = 60) -> tuple[int, int]:
    """Portrait/landscape switch. Caller must rebuild the encoder afterwards."""
    dm = devmode(device)
    dm.PelsWidth = width
    dm.PelsHeight = height
    dm.DisplayFrequency = freq
    dm.Fields = (win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
                 | win32con.DM_DISPLAYFREQUENCY)
    _apply(device, dm)
    return width, height


def describe() -> str:
    lines = ["Display devices (pywin32 order):"]
    for d in devices():
        try:
            w, h, x, y = current_geometry(d["name"])
            geo = f"{w}x{h} @ ({x},{y})"
        except Exception as exc:
            geo = f"<unavailable: {exc}>"
        lines.append(f"  [{d['index']}] {d['name']}  {d['string']}  {geo}")
    return "\n".join(lines)
