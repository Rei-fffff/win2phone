"""Win2Phone host configuration.

Defaults are chosen so that `python host.py` alone works on this machine:
the virtual display is found by name, ffmpeg by package lookup, and the two
modes match the phone's native 9:20 panel (1080x2400 / rotated 2400x1080).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass


def parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in text.lower().replace("*", "x").split("x"))
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"expected WxH, got {text!r}") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError(f"bad size {text!r}")
    return w, h


@dataclass
class Config:
    video_port: int = 19010
    control_port: int = 19011
    fps: int = 30
    bitrate: str = "14M"
    codec: str = "auto"            # auto | nvenc | x264
    side: str = "right"            # right | left | top | bottom
    portrait: tuple[int, int] = (1080, 2400)
    landscape: tuple[int, int] = (2400, 1080)
    ffmpeg: str = ""               # empty -> resolve from imageio-ffmpeg
    monitor: int | None = None     # debug: capture a physical monitor, no VDD
    list_only: bool = False


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        description="Win2Phone host — turns an Android phone into a Windows display")
    p.add_argument("--video-port", type=int, default=19010)
    p.add_argument("--control-port", type=int, default=19011)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="14M",
                   help="x264/nvenc target bitrate, e.g. 14M")
    p.add_argument("--codec", choices=["auto", "nvenc", "x264"], default="auto")
    p.add_argument("--side", choices=["right", "left", "top", "bottom"],
                   default="right",
                   help="where the virtual display sits relative to the primary one")
    p.add_argument("--portrait", type=parse_size, default=(1080, 2400),
                   help="virtual display mode when the phone is upright")
    p.add_argument("--landscape", type=parse_size, default=(2400, 1080),
                   help="virtual display mode when the phone is sideways")
    p.add_argument("--ffmpeg", default="",
                   help="ffmpeg path; empty = bundled imageio-ffmpeg binary, "
                        "then PATH")
    p.add_argument("--monitor", type=int, default=None,
                   help="debug: capture this mss monitor index and skip all "
                        "virtual-display management")
    p.add_argument("--list", dest="list_only", action="store_true",
                   help="print display information and exit")
    a = p.parse_args(argv)
    return Config(
        video_port=a.video_port, control_port=a.control_port,
        fps=a.fps, bitrate=a.bitrate, codec=a.codec, side=a.side,
        portrait=a.portrait, landscape=a.landscape,
        ffmpeg=a.ffmpeg, monitor=a.monitor, list_only=a.list_only,
    )
