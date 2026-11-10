"""Win2Phone host — makes an Android phone a real extended Windows display.

The phone is not mirrored: a Virtual Display Driver (VDD) monitor is created,
sized to the phone's own aspect ratio, positioned on the chosen edge of the
primary display, and only *that* monitor is streamed. Windows treats it as a
normal second screen, so windows can be dragged onto it.

Pipeline
    mss grab(VDD) -> composite mouse cursor -> FFmpeg H.264 -> TCP 19010
    control (JSON lines) on TCP 19011, shared by the phone app and the console

This module exposes the :class:`Host` object so ``app.py`` can run it inside a
single process next to the web console; ``python host.py`` still works standalone.

Design notes that are not obvious from the code:
  * DPI awareness must be enabled before mss is imported, otherwise every
    coordinate (cursor, touch, monitor rects) is silently virtualised.
  * The VDD's \\\\.\\DISPLAYn name changes across device restarts, so it is
    always re-resolved by DeviceString, never by index.
  * Listening sockets use SO_EXCLUSIVEADDRUSE. On Windows plain SO_REUSEADDR
    lets a *second* instance bind the same port and silently steal connections,
    which looks like "the phone connects only sometimes".
  * mss/BitBlt never captures the cursor on an IddCx display, so we draw it.
  * The virtual display stays attached while the host runs (so a phone that
    reconnects does not make Windows re-enumerate displays) and is detached
    when the host stops.

See README.md and protocol.md.
"""
from __future__ import annotations

import ctypes


def _enable_dpi_awareness() -> None:
    """Run before mss/pywin32 read any display geometry.

    A DPI-unaware process sees a scaled virtual desktop (2912x1920 instead of
    3640x2400 on this machine) and SetCursorPos/GetCursorInfo operate in
    virtualised coordinates — which breaks cursor compositing *and* touch
    injection. mss latches unaware mode when imported, so order matters.
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

import json                      # noqa: E402
import os                        # noqa: E402
import queue                     # noqa: E402
import socket                    # noqa: E402
import struct                    # noqa: E402
import subprocess                # noqa: E402
import sys                       # noqa: E402
import threading                 # noqa: E402
import time                      # noqa: E402

import mss                       # noqa: E402
import win32api                  # noqa: E402

import display as disp           # noqa: E402
from config import SCALES, Config, parse_args   # noqa: E402
from cursor import CursorCompositor     # noqa: E402

SIDE_LABEL = {"right": "右", "left": "左", "top": "上", "bottom": "下"}

# app.py runs under pythonw (no console); without this every ffmpeg launch
# would flash a console window on top of whatever the user is doing.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- Win32 input
PUL = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def _send_mouse(flags: int, x: int = 0, y: int = 0) -> None:
    inp = INPUT(0, MOUSEINPUT(x, y, 0, flags, 0, None))
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def inject_touch(mon: dict, nx: float, ny: float, action: str) -> None:
    """Map phone-normalised (0..1) coords onto this monitor, then click.

    ``mon`` must be the *current* rectangle: after a side or resolution change
    the old one points at empty desktop.
    """
    if not mon:
        return
    vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    tx = mon["left"] + max(0.0, min(1.0, nx)) * mon["width"]
    ty = mon["top"] + max(0.0, min(1.0, ny)) * mon["height"]
    ax = int((tx - vx) * 65535 / max(vw - 1, 1))
    ay = int((ty - vy) * 65535 / max(vh - 1, 1))
    ax, ay = max(0, min(65535, ax)), max(0, min(65535, ay))
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    if action == "down":
        _send_mouse(move, ax, ay)
        _send_mouse(MOUSEEVENTF_LEFTDOWN, ax, ay)
    elif action == "up":
        _send_mouse(MOUSEEVENTF_LEFTUP, ax, ay)
        _send_mouse(move, ax, ay)
    else:
        _send_mouse(move, ax, ay)


# ------------------------------------------------------------ Annex-B framing
def split_annexb(buf: bytearray):
    """Yield complete NAL units, keeping the trailing incomplete one in ``buf``."""
    while True:
        first = buf.find(b"\x00\x00\x01")
        if first < 0:
            return
        second = buf.find(b"\x00\x00\x01", first + 3)
        if second < 0:
            return
        nal = bytes(buf[first:second])
        if nal.endswith(b"\x00"):   # leading zero of the next 4-byte start code
            nal = nal[:-1]
        yield nal
        del buf[:second]


# ------------------------------------------------------------------- Encoding
def resolve_ffmpeg(given: str) -> str:
    if given:
        return given
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return "ffmpeg"


def pick_codec(cfg: Config) -> str:
    """Probe NVENC by actually encoding: a listed encoder can still fail."""
    if cfg.codec != "auto":
        return cfg.codec
    try:
        probe = [
            cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgra", "-s", "640x360",
            "-r", "10", "-i", "pipe:0", "-an",
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
            "-b:v", "1M", "-g", "10", "-pix_fmt", "yuv420p",
            "-f", "h264", "pipe:1",
        ]
        result = subprocess.run(
            probe, input=b"\x40" * (640 * 360 * 4 * 2),
            capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        if result.returncode == 0 and result.stdout:
            return "nvenc"
    except Exception:
        pass
    return "x264"


def ffmpeg_cmd(cfg: Config, w: int, h: int, codec: str) -> list[str]:
    if codec == "nvenc":
        # FFmpeg 9.x rejects `-rc cbr-ld-h264` ("Undefined constant 'ld-h264'").
        venc = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    else:
        venc = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
    return [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgra",
        "-s", f"{w}x{h}", "-r", str(cfg.fps), "-i", "pipe:0",
        "-an", *venc, "-b:v", cfg.bitrate, "-g", str(cfg.fps),
        # B-frames force reordering in MediaCodec; forbidden for a live desktop.
        "-bf", "0",
        "-pix_fmt", "yuv420p", "-f", "h264", "pipe:1",
    ]


# ------------------------------------------------------------ virtual display
class VirtualDisplay:
    """Wraps display.py with the attach/configure/resolve dance."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.device = ""

    def size_for(self, orientation: str) -> tuple[int, int]:
        """Display size for the current orientation × pixel scale.

        The scale only divides the pixel count (1/1, 3/4, 2/3, 1/2); the 9:20
        aspect ratio is preserved exactly, so the phone never has to stretch.
        """
        base = self.cfg.landscape if orientation == "landscape" else self.cfg.portrait
        num, den = SCALES.get(self.cfg.scale, (1, 1))
        return base[0] * num // den, base[1] * num // den

    def ensure(self) -> str:
        v = disp.find_virtual()
        if v is None:
            raise RuntimeError(
                "找不到虚拟显示器驱动（VDD）。先安装：\n"
                "  winget install --id=VirtualDrivers.Virtual-Display-Driver "
                "-e --source winget\n"
                "然后用管理员权限装一次驱动本体（见 README 的 VDD 小节）。")
        if not v["usable"]:
            print("[vdd] 虚拟屏存在但未接入桌面，正在扩展…")
            disp.attach()
            for _ in range(40):
                time.sleep(0.1)
                v = disp.find_virtual()
                if v and v["usable"]:
                    break
        if not v or not v["usable"]:
            raise RuntimeError("虚拟屏无法接入桌面（SetDisplayConfig EXTEND 未生效）")
        self.device = v["name"]
        print(f"[vdd] {self.device}  {v['width']}x{v['height']} "
              f"@ ({v['left']},{v['top']})")
        return self.device

    def configure(self, orientation: str, side: str) -> tuple[int, int, int, int]:
        w, h = self.size_for(orientation)
        try:
            geo = disp.place(self.device, side, w, h)
        except ValueError:
            # The requested scale is not in the driver's mode list (usually the
            # system copy of vdd_settings.xml is stale). Fall back to the
            # nearest mode with the same aspect ratio rather than stretching.
            modes = disp.modes(self.device)
            same = [m for m in modes
                    if abs(m[0] / m[1] - w / h) < 0.002] or modes
            if not same:
                raise
            pick = min(same, key=lambda m: abs(m[0] - w) + abs(m[1] - h))
            print(f"[vdd] {w}x{h} 不在驱动模式列表里，改用 {pick[0]}x{pick[1]}")
            geo = disp.place(self.device, side, pick[0], pick[1])
        print(f"[vdd] orientation={orientation} scale={self.cfg.scale} "
              f"{geo[0]}x{geo[1]} @ ({geo[2]},{geo[3]}) side={side}")
        return geo

    def monitor(self, timeout: float = 3.0) -> tuple[int, dict]:
        """The mss monitor entry that matches the virtual display exactly.

        Polled: right after a mode/position change Windows needs a moment before
        the new rectangle shows up in the monitor list.
        """
        deadline = time.time() + timeout
        geo = None
        seen = []
        while time.time() < deadline:
            geo = disp.geometry(self.device)
            if geo is None:
                raise RuntimeError("虚拟屏已被摘除")
            with mss.mss() as sct:
                seen = [dict(m) for m in sct.monitors[1:]]
                for i, m in enumerate(sct.monitors[1:], start=1):
                    # geometry() is (w, h, left, top) — keep that order.
                    if ((m["width"], m["height"], m["left"], m["top"]) == geo):
                        return i, dict(m)
            time.sleep(0.15)
        raise RuntimeError(f"mss 里没有与虚拟屏 {geo} 匹配的显示器；"
                           f"mss 当前为 {seen}")


# ------------------------------------------------------------ shared state hub
class ControlHub:
    """State shared by the phone app, the console page and the video session."""

    SIDES = ("right", "left", "top", "bottom")

    def __init__(self, cfg: Config, vd: VirtualDisplay | None, mon: dict,
                 codec: str) -> None:
        self.cfg = cfg
        self.vd = vd
        self.mon = mon
        self.codec = codec
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.clients: dict[socket.socket, str] = {}
        self.side = cfg.side
        self.orientation = "portrait"
        self.note = "调试模式（--monitor）：不管理虚拟屏" if vd is None else ""
        self.applied = vd is not None
        self.restart = threading.Event()
        self.video_clients = 0
        self.last_error = ""
        self.last_session_end = 0.0

    # ---- state changes ----------------------------------------------------
    def refresh(self) -> None:
        if self.vd is None:
            return
        try:
            _, m = self.vd.monitor()
            self.mon = m
        except Exception as exc:
            print(f"[vdd] monitor refresh failed: {exc}")

    def set_side(self, value: str) -> str:
        if value not in self.SIDES:
            return f"unknown side: {value}"
        self.side = value
        if self.vd is None:
            return "调试模式：虚拟屏位置未管理"
        try:
            x, y = disp.set_side(self.vd.device, value)
            self.applied = True
            self.note = f"虚拟屏已移到主屏{SIDE_LABEL[value]}侧 ({x},{y})"
            print(f"[control] side -> {value} @ ({x},{y})")
        except Exception as exc:
            self.applied = False
            self.note = f"位置未应用：{exc}"
            print(f"[control] side -> {value} failed: {exc}")
        self.refresh()
        return self.note

    def set_orientation(self, value: str) -> str:
        if value not in ("portrait", "landscape"):
            return f"unknown orientation: {value}"
        self.orientation = value
        if self.vd is None:
            self.restart.set()
            return "调试模式：虚拟屏方向未管理"
        try:
            geo = self.vd.configure(value, self.side)
            self.applied = True
            self.note = f"虚拟屏 {geo[0]}x{geo[1]} @ ({geo[2]},{geo[3]})"
        except Exception as exc:
            self.applied = False
            self.note = f"方向切换失败：{exc}"
            print(f"[control] orientation -> {value} failed: {exc}")
        self.refresh()
        # Only the encoder/decoder need rebuilding; geometry already changed.
        self.restart.set()
        print(f"[control] orientation -> {value}; video session restarts")
        return self.note

    def set_scale(self, value: str) -> str:
        """Change the pixel scale (100/75/67/50). Aspect ratio is untouched."""
        if value not in SCALES:
            return f"unknown scale: {value}"
        self.cfg.scale = value
        if self.vd is None:
            self.restart.set()
            return "调试模式：分辨率未管理"
        try:
            geo = self.vd.configure(self.orientation, self.side)
            self.applied = True
            self.note = f"虚拟屏 {geo[0]}x{geo[1]}（{value}%）"
        except Exception as exc:
            self.applied = False
            self.note = f"分辨率切换失败：{exc}"
            print(f"[control] scale -> {value} failed: {exc}")
        self.refresh()
        self.restart.set()
        print(f"[control] scale -> {value}; video session restarts")
        return self.note

    # ---- status -----------------------------------------------------------
    def status(self, msg_type: str = "status") -> dict:
        return {
            "type": msg_type,
            "side": self.side,
            "orientation": self.orientation,
            "width": self.mon["width"],
            "height": self.mon["height"],
            "source_width": self.mon["width"],
            "source_height": self.mon["height"],
            "left": self.mon["left"],
            "top": self.mon["top"],
            "fps": self.cfg.fps,
            "bitrate": self.cfg.bitrate,
            "scale": self.cfg.scale,
            "codec": self.codec,
            "display_applied": self.applied,
            "note": self.note,
        }

    # ---- client bookkeeping ----------------------------------------------
    def register(self, conn: socket.socket, role: str = "") -> None:
        with self.lock:
            self.clients[conn] = role

    def set_role(self, conn: socket.socket, role: str) -> None:
        with self.lock:
            self.clients[conn] = role
        print(f"[control] client role = {role or 'unknown'}")

    def unregister(self, conn: socket.socket) -> None:
        with self.lock:
            self.clients.pop(conn, None)

    def send_to(self, conn: socket.socket, obj: dict) -> None:
        try:
            with self.write_lock:
                conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            self.unregister(conn)

    def broadcast(self, obj: dict, exclude: socket.socket | None = None) -> None:
        with self.lock:
            targets = list(self.clients)
        for conn in targets:
            if conn is exclude:
                continue
            self.send_to(conn, obj)


# ---------------------------------------------------------------- video server
def serve_stream(cfg: Config, conn: socket.socket, stop: threading.Event,
                 hub: ControlHub) -> None:
    """Serve exactly one client and always return when it goes away.

    Without the ``alive`` flag a disconnected client leaves the capture loop
    writing into an FFmpeg pipe nobody drains; the session then blocks forever
    and the video server never accepts another connection.
    """
    hub.restart.clear()
    hub.refresh()

    if cfg.monitor is not None:
        with mss.mss() as sct:
            if cfg.monitor >= len(sct.monitors):
                raise RuntimeError(f"monitor {cfg.monitor} not found; "
                                   f"available: {len(sct.monitors)}")
            mon = dict(sct.monitors[cfg.monitor])
    else:
        _, mon = hub.vd.monitor()
        hub.mon = mon

    w, h = mon["width"], mon["height"]
    print(f"[capture] {w}x{h} @ ({mon['left']},{mon['top']}) codec={hub.codec}")

    proc = subprocess.Popen(
        ffmpeg_cmd(cfg, w, h, hub.codec),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    chunks: queue.Queue = queue.Queue()
    alive = threading.Event()
    alive.set()

    def read_stdout() -> None:
        while True:
            data = proc.stdout.read(65536)
            if not data:
                break
            chunks.put(data)
        chunks.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()

    buf = bytearray()

    def pump() -> None:
        while alive.is_set():
            data = chunks.get()
            if data is None:
                alive.clear()
                return
            buf.extend(data)
            for nal in split_annexb(buf):
                try:
                    conn.sendall(struct.pack(">I", len(nal)) + nal)
                except OSError:
                    alive.clear()
                    return

    threading.Thread(target=pump, daemon=True).start()

    compositor = CursorCompositor()
    interval = 1.0 / cfg.fps
    try:
        with mss.mss() as sct:
            while (not stop.is_set() and alive.is_set()
                   and not hub.restart.is_set()):
                t0 = time.time()
                frame = sct.grab(mon)
                # mss/BitBlt never sees the cursor on an IddCx display.
                compositor.draw(frame.raw, mon)
                proc.stdin.write(frame.raw)
                proc.stdin.flush()
                remain = interval - (time.time() - t0)
                if remain > 0:
                    time.sleep(remain)
    except (BrokenPipeError, OSError, mss.exception.ScreenShotError) as exc:
        print(f"[capture] stream stopped: {exc}")
    finally:
        alive.clear()
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if err.strip():
            print("[ffmpeg]", err.strip()[:500])


def video_server(srv: socket.socket, cfg: Config, stop: threading.Event,
                 hub: ControlHub) -> None:
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        print(f"[video] client connected {addr}")
        with hub.lock:
            hub.video_clients += 1
        try:
            serve_stream(cfg, conn, stop, hub)
        except Exception as exc:
            hub.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[video] session ended: {exc}")
        finally:
            with hub.lock:
                hub.video_clients = max(0, hub.video_clients - 1)
            hub.last_session_end = time.time()
            try:
                conn.close()
            except OSError:
                pass
            print("[video] session closed, waiting for next client")


# -------------------------------------------------------------- control server
def handle(msg: dict, hub: ControlHub, conn: socket.socket) -> dict | None:
    kind = msg.get("type")
    if kind == "hello":
        hub.set_role(conn, str(msg.get("client", "")))
        # Exactly one reply per command: the phone app applies `orientation`
        # from this status, and short-lived clients rely on the 1:1 pairing.
        return hub.status("hello")
    if kind == "query":
        return hub.status()
    if kind == "touch":
        try:
            inject_touch(hub.mon, float(msg.get("x", 0)), float(msg.get("y", 0)),
                         str(msg.get("action", "move")))
        except (TypeError, ValueError):
            pass
        return None
    if kind == "side":
        value = str(msg.get("value", ""))
        if value not in ControlHub.SIDES:
            return {"type": "error", "message": f"unknown side: {value}"}
        hub.set_side(value)
        st = hub.status()
        hub.broadcast(st, exclude=conn)
        return st
    if kind == "orientation":
        value = str(msg.get("value", ""))
        if value not in ("portrait", "landscape"):
            return {"type": "error", "message": f"unknown orientation: {value}"}
        hub.set_orientation(value)
        hub.broadcast({"type": "orientation", "value": value}, exclude=conn)
        st = hub.status()
        hub.broadcast(st, exclude=conn)
        return st
    if kind == "scale":
        value = str(msg.get("value", ""))
        if value not in SCALES:
            return {"type": "error", "message": f"unknown scale: {value}"}
        hub.set_scale(value)
        st = hub.status()
        hub.broadcast(st, exclude=conn)
        return st
    if kind == "mode":
        print(f"[control] transport mode = {msg.get('value')}")
        return None
    return {"type": "error", "message": f"unknown type: {kind}"}


def control_session(hub: ControlHub, conn: socket.socket, addr,
                    stop: threading.Event) -> None:
    hub.register(conn)
    print(f"[control] client connected {addr}")
    try:
        with conn.makefile("rb") as f:
            for raw in f:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reply = handle(msg, hub, conn)
                if reply:
                    hub.send_to(conn, reply)
    except Exception as exc:
        print(f"[control] session ended: {exc}")
    finally:
        hub.unregister(conn)
        try:
            conn.close()
        except OSError:
            pass
        print("[control] client disconnected")


def control_server(srv: socket.socket, hub: ControlHub,
                   stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=control_session, args=(hub, conn, addr, stop),
                         daemon=True).start()


# ------------------------------------------------------------------------ host
def _listen(port: int) -> socket.socket:
    """Bind a listening socket that a second instance cannot steal.

    Windows' SO_REUSEADDR means "allow binding an address already in use", so a
    stale host plus a fresh one can both listen and connections land on either
    process at random — which looks exactly like "the phone connects only
    sometimes". SO_EXCLUSIVEADDRUSE makes the second bind fail loudly instead.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
    except OSError as exc:
        srv.close()
        raise RuntimeError(
            f"端口 {port} 无法绑定：{exc}。多半是已经有一个 Win2Phone 主机在运行，"
            f"先关掉它（或换 --video-port / --control-port）。") from exc
    srv.listen(5)
    srv.settimeout(1.0)
    return srv


class Host:
    """Owns the display, the encoder loop and both TCP servers."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.vd: VirtualDisplay | None = None
        self.hub: ControlHub | None = None
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.sockets: list[socket.socket] = []
        self.running = False
        self.error = ""
        self._detached = threading.Event()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        cfg = self.cfg
        cfg.ffmpeg = resolve_ffmpeg(cfg.ffmpeg)
        print(f"[init] ffmpeg = {cfg.ffmpeg}")

        # Bind before touching the display: a port conflict must fail fast and
        # without a screen flicker.
        video_srv = _listen(cfg.video_port)
        try:
            control_srv = _listen(cfg.control_port)
        except Exception:
            video_srv.close()
            raise
        self.sockets = [video_srv, control_srv]
        print(f"[video] listening on {cfg.video_port}")
        print(f"[control] listening on {cfg.control_port}")

        cfg.codec = pick_codec(cfg)
        print(f"[init] encoder = {cfg.codec}")

        if cfg.monitor is not None:
            with mss.mss() as sct:
                if cfg.monitor >= len(sct.monitors):
                    raise RuntimeError(
                        f"monitor {cfg.monitor} 不存在，可用 0..{len(sct.monitors)-1}")
                mon = dict(sct.monitors[cfg.monitor])
            print(f"[init] 调试模式：抓 mss[{cfg.monitor}] {mon}")
        else:
            self.vd = VirtualDisplay(cfg)
            self.vd.ensure()
            self.vd.configure("portrait", cfg.side)
            _, mon = self.vd.monitor()

        self.hub = ControlHub(cfg, self.vd, mon, cfg.codec)
        self.threads = [
            threading.Thread(target=video_server,
                             args=(video_srv, cfg, self.stop_event, self.hub),
                             daemon=True, name="w2p-video"),
            threading.Thread(target=control_server,
                             args=(control_srv, self.hub, self.stop_event),
                             daemon=True, name="w2p-control"),
        ]
        for t in self.threads:
            t.start()
        self.running = True
        print("[init] 就绪，等待手机连接")

    def stop(self, detach: bool = True) -> None:
        self.running = False
        self.stop_event.set()
        for srv in self.sockets:
            try:
                srv.close()
            except OSError:
                pass
        self.sockets = []
        for t in self.threads:
            t.join(timeout=2)
        self.threads = []
        if detach:
            self.detach()
        print("[host] stopped")

    def detach(self) -> None:
        """Remove the virtual display once (safe to call repeatedly)."""
        if self.vd is None or self._detached.is_set() or not self.cfg.detach_on_exit:
            return
        self._detached.set()
        try:
            disp.detach()
            print("[vdd] 虚拟屏已摘除")
        except Exception as exc:
            print(f"[vdd] 摘除失败：{exc}")

    # ---- introspection ---------------------------------------------------
    def info(self) -> dict:
        out = {
            "running": self.running,
            "error": self.error,
            "video_port": self.cfg.video_port,
            "control_port": self.cfg.control_port,
            "fps": self.cfg.fps,
            "bitrate": self.cfg.bitrate,
            "codec": self.cfg.codec,
            "vdd": self.vd.device if self.vd else "",
            "debug_monitor": self.cfg.monitor,
        }
        if self.hub is not None:
            with self.hub.lock:
                out["video_clients"] = self.hub.video_clients
            out["last_session_end"] = self.hub.last_session_end
            out["last_error"] = self.hub.last_error
        return out

    def status(self) -> dict:
        if self.hub is None:
            return {}
        return self.hub.status()


# ------------------------------------------------------------------------ main
_CTRL_HANDLER = None      # keep the callback alive; Windows holds a raw pointer


def _install_close_handler(on_close) -> bool:
    """Run ``on_close`` when the console window is closed.

    Closing a console window does not raise KeyboardInterrupt — Windows sends
    CTRL_CLOSE_EVENT and then kills the process, so ``finally`` never runs and
    the virtual display would be left behind.
    """
    global _CTRL_HANDLER
    try:
        prototype = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        CTRL_CLOSE_EVENT = 2

        def _handler(ctrl_type: int) -> int:
            if ctrl_type == CTRL_CLOSE_EVENT:
                try:
                    on_close()
                except Exception:
                    pass
                return 1
            return 0        # let Ctrl+C stay a normal KeyboardInterrupt

        _CTRL_HANDLER = prototype(_handler)
        return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True))
    except Exception:
        return False


def main() -> int:
    cfg = parse_args()
    if cfg.list_only:
        print(disp.describe())
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                print(f"  mss[{i}] {m}")
        return 0

    print("Win2Phone host — Ctrl+C 退出")
    h = Host(cfg)
    try:
        h.start()
    except Exception as exc:
        h.error = str(exc)
        print(f"[error] {exc}")
        h.stop()
        return 2

    _install_close_handler(h.detach)
    print(f"[init] 手机端 USB 连接前先执行：\n"
          f"        adb reverse tcp:{cfg.video_port} tcp:{cfg.video_port}\n"
          f"        adb reverse tcp:{cfg.control_port} tcp:{cfg.control_port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[main] 停止中…")
    finally:
        h.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
