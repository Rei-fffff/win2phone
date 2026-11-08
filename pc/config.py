"""Win2Phone host configuration."""
from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class Config:
    video_port: int = 19010
    control_port: int = 19011
    monitor_index: int = 2      # mss: 0=all monitors, 1=primary, 2=second
    device_index: int = 1       # pywin32 EnumDisplayDevices index of the virtual display
    fps: int = 30
    bitrate: str = "8M"
    codec: str = "auto"         # auto | nvenc | x264
    side: str = "none"          # none | right | left | top | bottom
    scale: str = ""             # optional WxH downscale, e.g. 1280x800
    ffmpeg: str = "ffmpeg"
    list_only: bool = False


def parse_args() -> Config:
    p = argparse.ArgumentParser(description="Win2Phone Windows host (M0)")
    p.add_argument("--video-port", type=int, default=19010)
    p.add_argument("--control-port", type=int, default=19011)
    p.add_argument("--monitor", type=int, default=2,
                   help="mss monitor index; use --list to inspect")
    p.add_argument("--device", type=int, default=1,
                   help="pywin32 display device index for side/resolution changes")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="8M")
    p.add_argument("--codec", choices=["auto", "nvenc", "x264"], default="auto")
    p.add_argument("--side", choices=["none", "right", "left", "top", "bottom"],
                   default="none",
                   help="'none' skips display repositioning (use while testing on the primary monitor)")
    p.add_argument("--scale", default="",
                   help="optional WxH downscale before encoding, e.g. 1280x800; "
                        "handy when testing against the physical primary monitor")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--list", dest="list_only", action="store_true",
                   help="print monitor/device list and exit")
    a = p.parse_args()
    return Config(
        video_port=a.video_port, control_port=a.control_port,
        monitor_index=a.monitor, device_index=a.device,
        fps=a.fps, bitrate=a.bitrate, codec=a.codec, side=a.side,
        scale=a.scale, ffmpeg=a.ffmpeg, list_only=a.list_only,
    )
