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


# 分辨率档位。只缩放像素数量，长宽比永远保持手机原生的 9:20，
# 所以手机端任何一档都是 1:1 铺满，不会出现拉伸或黑边。
# 1080x2400 / 2400x1080 能被这三个分数整除，结果全是整数像素。
# 分辨率越低 → 同样的桌面用更少的像素排 → 字越大（虚拟屏 DPI 恒为 100%）。
SCALES: dict[str, tuple[int, int]] = {
    "100": (1, 1),
    "75": (3, 4),
    "67": (2, 3),
    "50": (1, 2),
}
SCALE_CHOICES = tuple(SCALES)
SCALE_LABEL = {"100": "100%", "75": "75%", "67": "67%", "50": "50%"}


@dataclass
class Config:
    video_port: int = 19010
    control_port: int = 19011
    web_port: int = 19012
    fps: int = 30
    bitrate: str = "14M"
    codec: str = "auto"            # auto | nvenc | x264
    side: str = "right"            # right | left | top | bottom
    scale: str = "67"              # 100 | 75 | 67 | 50（像素档位，比例不变）
    portrait: tuple[int, int] = (1080, 2400)
    landscape: tuple[int, int] = (2400, 1080)
    ffmpeg: str = ""               # empty -> resolve from imageio-ffmpeg
    monitor: int | None = None     # debug: capture a physical monitor, no VDD
    list_only: bool = False
    browser: bool = True           # app.py: open the page automatically
    detach_on_exit: bool = True    # remove the virtual display when stopping


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        description="Win2Phone — turns an Android phone into a Windows display")
    p.add_argument("--video-port", type=int, default=19010)
    p.add_argument("--control-port", type=int, default=19011)
    p.add_argument("--web-port", type=int, default=19012,
                   help="port of the control console (app.py)")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="14M",
                   help="x264/nvenc target bitrate, e.g. 14M")
    p.add_argument("--codec", choices=["auto", "nvenc", "x264"], default="auto")
    p.add_argument("--side", choices=["right", "left", "top", "bottom"],
                   default="right",
                   help="where the virtual display sits relative to the primary one")
    p.add_argument("--scale", choices=SCALE_CHOICES, default="67",
                   help="pixel scale of the virtual display; the 9:20 aspect ratio "
                        "never changes, so nothing is stretched. Lower = bigger text")
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
    p.add_argument("--no-browser", dest="browser", action="store_false",
                   help="do not open the console page automatically")
    p.add_argument("--keep-display", dest="detach_on_exit", action="store_false",
                   help="leave the virtual display attached after exit")
    p.add_argument("--list", dest="list_only", action="store_true",
                   help="print display information and exit")
    a = p.parse_args(argv)
    return Config(
        video_port=a.video_port, control_port=a.control_port,
        web_port=a.web_port, fps=a.fps, bitrate=a.bitrate, codec=a.codec,
        side=a.side, scale=a.scale, portrait=a.portrait, landscape=a.landscape,
        ffmpeg=a.ffmpeg, monitor=a.monitor, list_only=a.list_only,
        browser=a.browser, detach_on_exit=a.detach_on_exit,
    )
