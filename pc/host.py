"""Win2Phone Windows host — M0.2.

Capture a display -> FFmpeg H.264 -> TCP NAL stream (port 19010).
A second TCP port (19011) carries newline-delimited JSON control messages.
Multiple control clients may connect at once (phone App + PC control panel);
side / orientation changes are broadcast and can restart the video session.

    python host.py --list
    python host.py --monitor 1 --scale 1280x800 --codec auto

See README.md and protocol.md.
"""
from __future__ import annotations

import ctypes
import json
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

import mss
import win32api

import display as disp
from config import parse_args

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


def inject_touch(monitor: dict, nx: float, ny: float, action: str) -> None:
    """Map normalized phone coords -> absolute desktop coords -> mouse event."""
    vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    tx = monitor["left"] + max(0.0, min(1.0, nx)) * monitor["width"]
    ty = monitor["top"] + max(0.0, min(1.0, ny)) * monitor["height"]
    ax = int((tx - vx) * 65535 / max(vw - 1, 1))
    ay = int((ty - vy) * 65535 / max(vh - 1, 1))
    ax, ay = max(0, min(65535, ax)), max(0, min(65535, ay))
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    if action == "down":
        _send_mouse(move, ax, ay)
        _send_mouse(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, ax, ay)
    elif action == "up":
        _send_mouse(move, ax, ay)
        _send_mouse(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, ax, ay)
    else:
        _send_mouse(move, ax, ay)


# ------------------------------------------------------------ Annex-B framing
def split_annexb(buf: bytearray):
    """Yield complete NAL units, retaining the trailing incomplete one."""
    while True:
        first = buf.find(b"\x00\x00\x01")
        if first < 0:
            return
        second = buf.find(b"\x00\x00\x01", first + 3)
        if second < 0:
            return
        nal = bytes(buf[first:second])
        if nal.endswith(b"\x00"):   # leading zero belonging to next 4-byte start code
            nal = nal[:-1]
        yield nal
        del buf[:second]


# ------------------------------------------------------------------- Encoding
def pick_codec(cfg) -> str:
    """Probe NVENC by actually encoding frames (a listed encoder can still fail)."""
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
            capture_output=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout:
            return "nvenc"
    except Exception:
        pass
    return "x264"


def effective_size(cfg, w: int, h: int) -> tuple[int, int]:
    """Encoded resolution (after optional --scale). Reported in hello/status."""
    if cfg.scale:
        try:
            sw, sh = (int(x) for x in cfg.scale.lower().split("x"))
            if sw > 0 and sh > 0:
                return sw, sh
        except ValueError:
            pass
    return w, h


def ffmpeg_cmd(cfg, w: int, h: int) -> list[str]:
    sw, sh = effective_size(cfg, w, h)
    vf = ["-vf", f"scale={sw}:{sh}:flags=fast_bilinear"] if (sw, sh) != (w, h) else []
    if cfg.codec == "nvenc":
        # FFmpeg 9.x rejects `-rc cbr-ld-h264` ("Undefined constant 'ld-h264'").
        venc = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    else:
        venc = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
    return [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgra",
        "-s", f"{w}x{h}", "-r", str(cfg.fps), "-i", "pipe:0",
        "-an", *vf, *venc, "-b:v", cfg.bitrate, "-g", str(cfg.fps),
        # B-frames force reordering in MediaCodec; forbidden for a live desktop.
        "-bf", "0",
        "-pix_fmt", "yuv420p", "-f", "h264", "pipe:1",
    ]


# ------------------------------------------------------------ Shared state hub
class ControlHub:
    """Shared state for all control clients and the video server.

    The phone App and the PC control panel both connect to 19011. Any client may
    change side/orientation; the resulting state is broadcast to everyone so the
    phone can rotate its Activity and the panel can refresh its buttons.
    """

    SIDES = ("right", "left", "top", "bottom")

    def __init__(self, cfg, device, mon, w: int, h: int):
        self.cfg = cfg
        self.device = device
        self.mon = mon
        self.src_w, self.src_h = w, h
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.clients: dict[socket.socket, str] = {}   # conn -> role
        self.side = cfg.side if cfg.side in self.SIDES else "right"
        self.orientation = "landscape"
        self.fixed_scale = cfg.scale or ""
        self.display_note = ""
        self.display_applied = False
        self.restart = threading.Event()
        self.apply_scale()

    # ---- display geometry -------------------------------------------------
    def apply_scale(self) -> None:
        """Rotate the --scale box when the phone is in portrait."""
        if not self.fixed_scale:
            self.cfg.scale = ""
            return
        try:
            sw, sh = (int(x) for x in self.fixed_scale.lower().split("x"))
        except ValueError:
            sw, sh = 1280, 800
        want_portrait = self.orientation == "portrait"
        if want_portrait and sw > sh:
            sw, sh = sh, sw
        if not want_portrait and sh > sw:
            sw, sh = sh, sw
        self.cfg.scale = f"{sw}x{sh}"

    def set_side(self, value: str) -> str:
        """Try to move the virtual display; always record the requested side."""
        if value not in self.SIDES:
            return f"unknown side: {value}"
        self.side = value
        try:
            x, y = disp.set_side(self.device, value)
            self.display_applied = True
            self.display_note = f"Windows 虚拟屏已移动到 ({x},{y})"
            print(f"[control] side -> {value} @ ({x},{y})")
        except Exception as exc:
            self.display_applied = False
            self.display_note = (
                "位置已记录，但 Windows 显示器设置未生效："
                "尚未安装虚拟显示器驱动（VDD）或 --device 指向了不存在的显示器"
            )
            print(f"[control] side -> {value} (not applied: {exc})")
        return self.display_note

    def set_orientation(self, value: str) -> str:
        if value not in ("portrait", "landscape"):
            return f"unknown orientation: {value}"
        self.orientation = value
        self.apply_scale()
        ew, eh = effective_size(self.cfg, self.src_w, self.src_h)
        try:
            disp.set_resolution(self.device, ew, eh)
            self.display_applied = True
            self.display_note = f"虚拟屏分辨率 -> {ew}x{eh}"
        except Exception as exc:
            self.display_applied = False
            self.display_note = (
                f"编码分辨率改为 {ew}x{eh}；Windows 显示器设置未生效"
                "（尚未安装 VDD）"
            )
            print(f"[control] resolution {ew}x{eh} (display not applied: {exc})")
        # Force the video session to restart so the new scale takes effect.
        self.restart.set()
        print(f"[control] orientation -> {value}, scale={self.cfg.scale or 'none'}, "
              "video session will restart")
        return self.display_note

    # ---- status & messaging ----------------------------------------------
    def status(self, msg_type: str = "status") -> dict:
        ew, eh = effective_size(self.cfg, self.src_w, self.src_h)
        return {
            "type": msg_type,
            "side": self.side,
            "orientation": self.orientation,
            "width": ew,
            "height": eh,
            "source_width": self.src_w,
            "source_height": self.src_h,
            "scale": self.cfg.scale or "none",
            "fps": self.cfg.fps,
            "codec": self.cfg.codec,
            "display_applied": self.display_applied,
            "note": self.display_note,
        }

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


# ---------------------------------------------------------------- Video server
def serve_stream(cfg, conn: socket.socket, stop: threading.Event,
                 hub: ControlHub) -> None:
    """Serve exactly one client and always return when that client goes away.

    Without the `alive` flag, a disconnected client leaves the capture loop
    writing into an FFmpeg pipe nobody drains; the session then blocks forever
    and video_server never accepts another connection (CLOSE_WAIT on the port).
    `hub.restart` lets the control panel end this session on demand.
    """
    hub.restart.clear()
    cfg.codec = pick_codec(cfg)
    alive = threading.Event()
    alive.set()
    with mss.mss() as sct:
        if cfg.monitor_index >= len(sct.monitors):
            raise RuntimeError(
                f"monitor {cfg.monitor_index} not found; available: {sct.monitors}"
            )
        mon = sct.monitors[cfg.monitor_index]
        w, h = mon["width"], mon["height"]
        ew, eh = effective_size(cfg, w, h)
        print(f"[capture] monitor {cfg.monitor_index}: {w}x{h} -> {ew}x{eh} "
              f"@ ({mon['left']},{mon['top']})  codec={cfg.codec}")

        proc = subprocess.Popen(
            ffmpeg_cmd(cfg, w, h),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        chunks: queue.Queue = queue.Queue()

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
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        alive.clear()
                        return

        threading.Thread(target=pump, daemon=True).start()

        interval = 1.0 / cfg.fps
        try:
            while (not stop.is_set() and alive.is_set()
                   and not hub.restart.is_set()):
                t0 = time.time()
                frame = sct.grab(mon)
                proc.stdin.write(frame.bgra)
                proc.stdin.flush()
                remain = interval - (time.time() - t0)
                if remain > 0:
                    time.sleep(remain)
        except (BrokenPipeError, OSError) as exc:
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


def video_server(cfg, stop: threading.Event, hub: ControlHub) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", cfg.video_port))
    srv.listen(5)
    srv.settimeout(1.0)
    print(f"[video] listening on {cfg.video_port}")
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        print(f"[video] client connected {addr}")
        try:
            serve_stream(cfg, conn, stop, hub)
        except Exception as exc:
            print(f"[video] session ended: {exc}")
        finally:
            conn.close()
            if hub.restart.is_set():
                print("[video] session closed: applying new orientation/scale")
            else:
                print("[video] session closed, waiting for next client")
    srv.close()


# --------------------------------------------------------------- Control server
def handle(msg: dict, hub: ControlHub, conn: socket.socket) -> dict | None:
    kind = msg.get("type")
    if kind == "hello":
        hub.set_role(conn, str(msg.get("client", "")))
        return hub.status("hello")
    if kind == "query":
        return hub.status()
    if kind == "touch":
        inject_touch(hub.mon, float(msg.get("x", 0)), float(msg.get("y", 0)),
                     str(msg.get("action", "move")))
        return None
    if kind == "side":
        value = str(msg.get("value", ""))
        if value not in ControlHub.SIDES:
            return {"type": "error", "message": f"unknown side: {value}"}
        note = hub.set_side(value)
        st = hub.status()
        # The sender gets `st` as its direct reply; exclude it from the broadcast
        # so it does not receive the same message twice.
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


def control_server(cfg, stop: threading.Event, hub: ControlHub) -> None:
    devs = disp.devices()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", cfg.control_port))
    srv.listen(5)
    srv.settimeout(1.0)
    print(f"[control] listening on {cfg.control_port}; "
          f"device={hub.device}; clients can connect concurrently")
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=control_session, args=(hub, conn, addr, stop),
                         daemon=True).start()
    srv.close()


# ------------------------------------------------------------------------ Main
def main() -> int:
    cfg = parse_args()
    if cfg.list_only:
        with mss.mss() as sct:
            print("mss monitors:")
            for i, m in enumerate(sct.monitors):
                print(f"  [{i}] {m}")
        print(disp.describe())
        return 0

    print("Win2Phone host M0.2 — Ctrl+C to stop")
    print(disp.describe())
    # Resolve `auto` once so status/hello always report the real encoder.
    cfg.codec = pick_codec(cfg)
    print(f"[init] encoder = {cfg.codec}")

    devs = disp.devices()
    device = (devs[cfg.device_index]["name"] if cfg.device_index < len(devs)
              else (devs[-1]["name"] if devs else None))
    with mss.mss() as sct:
        mon = (sct.monitors[cfg.monitor_index]
               if cfg.monitor_index < len(sct.monitors) else sct.monitors[1])
        w, h = mon["width"], mon["height"]

    hub = ControlHub(cfg, device, mon, w, h)

    if cfg.side in ControlHub.SIDES:
        hub.set_side(cfg.side)
    else:
        print("[init] display repositioning skipped (--side none)")

    stop = threading.Event()
    threads = [
        threading.Thread(target=video_server, args=(cfg, stop, hub), daemon=True),
        threading.Thread(target=control_server, args=(cfg, stop, hub), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
