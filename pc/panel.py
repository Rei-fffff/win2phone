"""Win2Phone PC control panel.

Lets the operator choose where the phone sits relative to the PC monitor
(mouse crossing edge) and whether the phone is landscape or portrait.

Three front-ends, all talking to host.py's control port (default 19011):

    python panel.py                 # native tkinter window, if tkinter exists
    python panel.py --web           # local web UI on 127.0.0.1:19012 (stdlib only)
    python panel.py --no-gui --side left --orientation portrait   # headless check

This project's embedded Python has no tkinter, so --web is the default here.
start-panel.bat launches the web UI and opens the browser.
"""
from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import tkinter as tk
except ImportError:  # embedded Python builds often omit Tcl/Tk
    tk = None

SIDES = ("top", "left", "right", "bottom")
SIDE_LABELS = {"top": "上", "left": "左", "right": "右", "bottom": "下"}


def send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


# ------------------------------------------------------------------ headless
def headless(host: str, port: int, side: str | None, orientation: str | None) -> int:
    s = socket.create_connection((host, port), timeout=3)
    s.settimeout(5)
    f = s.makefile("rwb")

    def talk(obj: dict) -> dict:
        f.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline()
        if not line:
            raise RuntimeError("host closed the connection")
        return json.loads(line.decode("utf-8", "replace"))

    hello = talk({"type": "hello", "client": "panel"})
    print("hello:", json.dumps(hello, ensure_ascii=False))
    if side:
        print("side:", json.dumps(talk({"type": "side", "value": side}),
                                 ensure_ascii=False))
    if orientation:
        print("orientation:", json.dumps(
            talk({"type": "orientation", "value": orientation}), ensure_ascii=False))
    print("status:", json.dumps(talk({"type": "query"}), ensure_ascii=False))
    s.close()
    return 0


# --------------------------------------------------------------- web front-end
HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Win2Phone 控制面板</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:28px;background:#f5f7fa;color:#1f2933;
     font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
h1{font-size:20px;margin:0 0 4px}
.sub{font-size:13px;color:#6b7785;margin-bottom:20px}
.card{background:#fff;border:1px solid #e1e7ee;border-radius:12px;
      padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 3px #0000000d}
h2{font-size:14px;margin:0 0 14px;color:#37474f}
.grid{display:grid;grid-template-columns:repeat(3,88px);
      grid-template-rows:repeat(3,52px);gap:8px;justify-content:center}
.grid button{border:1px solid #cfd8e3;background:#f0f3f7;border-radius:8px;
             font-size:16px;cursor:pointer;color:#263238}
.grid button:hover{border-color:#1976d2}
.grid button.on{background:#1976d2;color:#fff;border-color:#1976d2}
.primary-cell{display:flex;align-items:center;justify-content:center;
              background:#eceff1;border:1px dashed #b0bec5;border-radius:8px;
              font-size:13px;color:#607d8b}
.row{display:flex;gap:12px}
.row button{flex:1;padding:14px;border:1px solid #cfd8e3;background:#f0f3f7;
            border-radius:8px;font-size:15px;cursor:pointer;color:#263238}
.row button.on{background:#1976d2;color:#fff;border-color:#1976d2}
.kv{display:grid;grid-template-columns:110px 1fr;gap:6px 10px;font-size:13px}
.kv .k{color:#78909c}
.kv .v{color:#263238;word-break:break-all}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
     background:#c0392b;margin-right:7px;vertical-align:middle}
.dot.ok{background:#2e7d32}
#msg{font-size:12px;color:#8a6d3b;min-height:16px;margin-top:8px}
</style></head><body>
<h1>Win2Phone 控制面板</h1>
<div class="sub"><span id="dot" class="dot"></span><span id="conn">正在连接主机…</span>
&nbsp;·&nbsp; 控制端口 19011</div>

<div class="card">
 <h2>① 手机摆在主屏的哪一侧（鼠标从哪条边穿过去）</h2>
 <div class="grid">
  <span></span><button data-side="top" style="grid-area:1/2">上</button><span></span>
  <button data-side="left" style="grid-area:2/1">左</button>
  <span class="primary-cell" style="grid-area:2/2">主屏</span>
  <button data-side="right" style="grid-area:2/3">右</button>
  <span></span><button data-side="bottom" style="grid-area:3/2">下</button><span></span>
 </div>
</div>

<div class="card">
 <h2>② 手机屏幕方向</h2>
 <div class="row">
  <button data-orientation="landscape">横屏 ▭</button>
  <button data-orientation="portrait">竖屏 ▯</button>
 </div>
</div>

<div class="card">
 <h2>③ 当前状态（来自主机）</h2>
 <div class="kv">
  <div class="k">摆放位置</div><div class="v" id="v-side">—</div>
  <div class="k">屏幕方向</div><div class="v" id="v-orient">—</div>
  <div class="k">编码分辨率</div><div class="v" id="v-res">—</div>
  <div class="k">编码器 / 帧率</div><div class="v" id="v-codec">—</div>
  <div class="k">提示</div><div class="v" id="v-note">—</div>
 </div>
 <div id="msg"></div>
</div>

<script>
function paint(j){
  document.querySelectorAll('[data-side]').forEach(function(b){
    b.classList.toggle('on', b.dataset.side===j.side);
  });
  document.querySelectorAll('[data-orientation]').forEach(function(b){
    b.classList.toggle('on', b.dataset.orientation===j.orientation);
  });
  document.getElementById('v-side').textContent =
    ({top:'上',left:'左',right:'右',bottom:'下'}[j.side]||j.side||'—')+' ('+(j.side||'—')+')';
  document.getElementById('v-orient').textContent =
    (j.orientation==='portrait'?'竖屏':'横屏')+' ('+(j.orientation||'—')+')';
  document.getElementById('v-res').textContent =
    (j.width||'?')+'×'+(j.height||'?')+'（源 '+(j.source_width||'?')+'×'+
    (j.source_height||'?')+'，scale '+(j.scale||'none')+'）';
  document.getElementById('v-codec').textContent =
    (j.codec||'?')+' / '+(j.fps||'?')+' fps';
  document.getElementById('v-note').textContent = j.note || '正常';
}
function connected(ok,text){
  document.getElementById('dot').className = 'dot'+(ok?' ok':'');
  document.getElementById('conn').textContent = text;
}
async function call(url){
  try{
    const r=await fetch(url); const j=await r.json();
    if(j.error){document.getElementById('msg').textContent='错误：'+j.error;connected(false,'主机异常');return;}
    connected(true,'已连接主机'); paint(j);
    document.getElementById('msg').textContent='';
  }catch(e){connected(false,'未连接主机（2 秒后重试）');
            document.getElementById('msg').textContent='无法连接主机：'+e;}
}
document.querySelectorAll('[data-side]').forEach(function(b){
  b.onclick=function(){call('/api/cmd?type=side&value='+b.dataset.side);};
});
document.querySelectorAll('[data-orientation]').forEach(function(b){
  b.onclick=function(){call('/api/cmd?type=orientation&value='+b.dataset.orientation);};
});
setInterval(function(){call('/api/status');},2000);
call('/api/status');
</script>
</body></html>"""


def control_request(host: str, port: int, cmd: dict) -> dict:
    """Stateless request: one short TCP connection per command."""
    s = socket.create_connection((host, port), timeout=2)
    s.settimeout(4)
    try:
        f = s.makefile("rwb")
        f.write((json.dumps({"type": "hello", "client": "panel"},
                            ensure_ascii=False) + "\n").encode("utf-8"))
        f.flush()
        f.readline()                     # hello reply
        f.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline()
        if not line:
            raise RuntimeError("host closed the connection")
        return json.loads(line.decode("utf-8", "replace"))
    finally:
        s.close()


def web_mode(host: str, port: int, web_port: int, open_browser: bool) -> int:
    outer_host, outer_port = host, port

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path == "/":
                self.reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if u.path in ("/api/status", "/api/cmd"):
                if u.path == "/api/status":
                    cmd = {"type": "query"}
                else:
                    q = parse_qs(u.query)
                    kind = q.get("type", [""])[0]
                    value = q.get("value", [""])[0]
                    if kind == "side" and value not in SIDES:
                        self.reply(200, json.dumps({"error": "bad side"}).encode(),
                                   "application/json")
                        return
                    if kind == "orientation" and value not in ("landscape", "portrait"):
                        self.reply(200, json.dumps({"error": "bad orientation"}).encode(),
                                   "application/json")
                        return
                    if kind not in ("side", "orientation"):
                        self.reply(200, json.dumps({"error": "bad type"}).encode(),
                                   "application/json")
                        return
                    cmd = {"type": kind, "value": value}
                try:
                    data = control_request(outer_host, outer_port, cmd)
                except Exception as exc:
                    data = {"error": str(exc)}
                self.reply(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self.reply(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    url = f"http://127.0.0.1:{web_port}/"
    print(f"Win2Phone 控制面板：{url}", flush=True)
    print(f"控制主机：{host}:{port}（Ctrl+C 退出）", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


# ------------------------------------------------------------ tkinter window
class Panel:
    def __init__(self, root, host: str, port: int):
        self.root = root
        self.host = host
        self.port = port
        self.q: queue.Queue = queue.Queue()
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.side = "right"
        self.orientation = "landscape"

        root.title(f"Win2Phone 控制面板 — {host}:{port}")
        root.geometry("460x560")
        root.minsize(400, 500)

        self.conn_label = tk.Label(root, text="● 未连接主机", fg="#c0392b",
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.conn_label.pack(anchor="w", padx=14, pady=(12, 4))
        self._build_side_section()
        self._build_orientation_section()
        self._build_status_section()
        self._build_log()
        self._paint_selection()
        root.after(80, self._drain_queue)
        threading.Thread(target=self._network_loop, daemon=True).start()

    def _section(self, text: str) -> "tk.Frame":
        outer = tk.Frame(self.root)
        outer.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(outer, text=text, font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w")
        return outer

    def _build_side_section(self) -> None:
        sec = self._section("① 手机摆在主屏的哪一侧（决定鼠标从哪条边穿过去）")
        grid = tk.Frame(sec)
        grid.pack(pady=8)
        self.side_buttons = {}
        for side, (r, c) in {"top": (0, 1), "left": (1, 0), "right": (1, 2),
                             "bottom": (2, 1)}.items():
            b = tk.Button(grid, text=SIDE_LABELS[side], width=8, height=2,
                          font=("Microsoft YaHei UI", 11),
                          command=lambda s=side: self.command("side", s))
            b.grid(row=r, column=c, padx=5, pady=5)
            self.side_buttons[side] = b
        tk.Label(grid, text="主屏", width=8, height=2, relief="groove",
                 bg="#eceff1").grid(row=1, column=1, padx=5, pady=5)

    def _build_orientation_section(self) -> None:
        sec = self._section("② 手机屏幕方向")
        row = tk.Frame(sec)
        row.pack(pady=8)
        self.orient_buttons = {}
        for key, label in (("landscape", "横屏 ▭"), ("portrait", "竖屏 ▯")):
            b = tk.Button(row, text=label, width=12, height=2,
                          font=("Microsoft YaHei UI", 11),
                          command=lambda v=key: self.command("orientation", v))
            b.pack(side="left", padx=8)
            self.orient_buttons[key] = b

    def _build_status_section(self) -> None:
        sec = self._section("③ 当前状态（来自主机）")
        box = tk.Frame(sec, relief="groove", bd=1)
        box.pack(fill="x", pady=8)
        self.status_labels = {}
        for i, (key, label) in enumerate([
            ("side", "摆放位置"), ("orientation", "屏幕方向"),
            ("resolution", "编码分辨率"), ("codec", "编码器 / 帧率"),
            ("note", "提示"),
        ]):
            tk.Label(box, text=label + "：", anchor="w", width=12).grid(
                row=i, column=0, sticky="w", padx=(8, 0), pady=2)
            lab = tk.Label(box, text="—", anchor="w", wraplength=320,
                           justify="left", fg="#37474f")
            lab.grid(row=i, column=1, sticky="w", padx=(0, 8), pady=2)
            self.status_labels[key] = lab

    def _build_log(self) -> None:
        sec = self._section("日志")
        frame = tk.Frame(sec)
        frame.pack(fill="both", expand=True, pady=8)
        scroll = tk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(frame, height=8, state="disabled",
                           yscrollcommand=scroll.set, bg="#fafafa")
        self.log.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.log.yview)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _paint_selection(self) -> None:
        for side, b in self.side_buttons.items():
            on = side == self.side
            b.configure(bg="#1976d2" if on else "#f0f0f0",
                        fg="white" if on else "black",
                        relief="sunken" if on else "raised")
        for key, b in self.orient_buttons.items():
            on = key == self.orientation
            b.configure(bg="#1976d2" if on else "#f0f0f0",
                        fg="white" if on else "black",
                        relief="sunken" if on else "raised")

    def _network_loop(self) -> None:
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=2)
                s.settimeout(None)
                with self.lock:
                    self.sock = s
                self.q.put(("connected", None))
                send_line(s, {"type": "hello", "client": "panel"})
                with s.makefile("rb") as f:
                    for raw in f:
                        try:
                            self.q.put(("msg", json.loads(raw.decode("utf-8", "replace"))))
                        except json.JSONDecodeError:
                            continue
            except Exception as exc:
                self.q.put(("disconnected", str(exc)))
            finally:
                with self.lock:
                    if self.sock is not None:
                        try:
                            self.sock.close()
                        except OSError:
                            pass
                    self.sock = None
            time.sleep(2)

    def command(self, kind: str, value: str) -> None:
        with self.lock:
            s = self.sock
        if s is None:
            self._log("尚未连接主机，命令未发送")
            return
        try:
            send_line(s, {"type": kind, "value": value})
            self._log(f"发送 {kind} = {value}")
        except OSError as exc:
            self._log(f"发送失败：{exc}")

    def _drain_queue(self) -> None:
        try:
            while True:
                event, payload = self.q.get_nowait()
                if event == "connected":
                    self.conn_label.configure(text="● 已连接主机", fg="#2e7d32")
                    self._log("已连接主机")
                elif event == "disconnected":
                    self.conn_label.configure(text="● 未连接主机（2 秒后重试）",
                                              fg="#c0392b")
                elif event == "msg":
                    self._apply(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def _apply(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind in ("hello", "status"):
            if obj.get("side") in SIDES:
                self.side = obj["side"]
            if obj.get("orientation") in ("landscape", "portrait"):
                self.orientation = obj["orientation"]
            self._paint_selection()
            w, h = obj.get("width", "?"), obj.get("height", "?")
            sw, sh = obj.get("source_width", "?"), obj.get("source_height", "?")
            self.status_labels["side"].configure(
                text=f"{SIDE_LABELS.get(self.side, self.side)}（{self.side}）")
            self.status_labels["orientation"].configure(
                text=("横屏" if self.orientation == "landscape" else "竖屏")
                     + f"（{self.orientation}）")
            self.status_labels["resolution"].configure(
                text=f"{w}×{h}（源 {sw}×{sh}）")
            self.status_labels["codec"].configure(
                text=f"{obj.get('codec', '?')} / {obj.get('fps', '?')} fps")
            self.status_labels["note"].configure(text=obj.get("note") or "正常")
            self._log(f"状态 {kind}: {self.side} / {self.orientation} / {w}x{h}")
        elif kind == "error":
            self._log("错误：" + str(obj.get("message")))


# ------------------------------------------------------------------------ Main
def main() -> int:
    ap = argparse.ArgumentParser(description="Win2Phone PC control panel")
    ap.add_argument("--host", default="127.0.0.1", help="host.py control address")
    ap.add_argument("--port", type=int, default=19011)
    ap.add_argument("--web-port", type=int, default=19012)
    ap.add_argument("--web", action="store_true", help="force the local web UI")
    ap.add_argument("--no-browser", action="store_true",
                    help="web UI: do not auto-open the browser")
    ap.add_argument("--no-gui", action="store_true",
                    help="headless check: send commands and print replies, then exit")
    ap.add_argument("--side", choices=SIDES)
    ap.add_argument("--orientation", choices=("landscape", "portrait"))
    a = ap.parse_args()

    if a.no_gui:
        return headless(a.host, a.port, a.side, a.orientation)
    if a.web or tk is None:
        if tk is None and not a.web:
            print("tkinter 不可用，自动使用网页界面（--web）", flush=True)
        return web_mode(a.host, a.port, a.web_port, not a.no_browser)

    root = tk.Tk()
    Panel(root, a.host, a.port)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
