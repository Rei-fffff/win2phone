"""Win2Phone 控制台 — 一个进程、一个页面。

原来要开两个终端加一个网页：一个跑主机、一个跑控制面板、再开浏览器。
现在这些都收进本进程：

    app.py
      ├─ host.Host          视频流 + 控制通道（19010 / 19011）
      └─ ThreadingHTTPServer 控制台页面（19012）

页面里能做的事：看状态、调摆放位置与方向、修 USB 端口映射、看实时日志、退出。
日志通过替换 sys.stdout / sys.stderr 收集，所以 host 里原有的 print 全部会出现在页面上，
同时写一份到 pc/app.log，程序没起来时可以直接看这个文件。

    pythonw app.py          # 无控制台窗口（start.bat 用这个）
    python  app.py          # 带控制台，排错时用
    python  app.py --no-browser
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------- log plumbing
class LogBuffer:
    """Ring buffer + optional file sink, fed by every print() in this process."""

    def __init__(self, path: str | None = None, cap: int = 1200) -> None:
        self.lines: deque[str] = deque(maxlen=cap)
        self.seq = 0
        self.lock = threading.Lock()
        self._partial = ""
        self._file = None
        if path:
            try:
                # Append, never truncate: a second instance must not wipe the
                # log of the one that is already running.
                self._file = open(path, "a", encoding="utf-8", buffering=1)
                self._file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                                 f"pid {os.getpid()} =====\n")
            except OSError:
                self._file = None

    def feed(self, text: str) -> None:
        with self.lock:
            if self._file is not None:
                try:
                    self._file.write(text)
                except Exception:
                    pass
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self.lines.append(line)
                self.seq += 1

    def since(self, after: int) -> tuple[list[str], int]:
        with self.lock:
            total = self.seq
            first_seq = total - len(self.lines)
            start = max(0, after - first_seq)
            return list(self.lines)[start:], total


class Tee(io.TextIOBase):
    def __init__(self, stream, buf: LogBuffer) -> None:
        self._stream = stream
        self._buf = buf

    def write(self, s: str) -> int:
        if self._stream is not None:
            try:
                self._stream.write(s)
            except Exception:
                pass
        self._buf.feed(s)
        return len(s)

    def flush(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except Exception:
                pass


LOG = LogBuffer(os.path.join(BASE, "app.log"))
sys.stdout = Tee(sys.stdout, LOG)
sys.stderr = Tee(sys.stderr, LOG)

# host import must come after the tee is installed so its startup lines are
# captured; host also enables DPI awareness before importing mss.
import host as host_mod                     # noqa: E402
from config import SCALES, parse_args       # noqa: E402


# ---------------------------------------------------------------------- adb
_ADB_CACHE: dict = {"path": None, "ts": 0.0}


def adb_path() -> str | None:
    now = time.time()
    if _ADB_CACHE["path"] and now - _ADB_CACHE["ts"] < 120:
        return _ADB_CACHE["path"]
    found = shutil.which("adb")
    if not found:
        candidates = []
        for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            root = os.environ.get(env)
            if root:
                candidates.append(os.path.join(root, "platform-tools", "adb.exe"))
        candidates += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Android", "Sdk", "platform-tools", "adb.exe"),
            r"D:\Program Files\Android\platform-tools\adb.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                found = c
                break
    _ADB_CACHE.update(path=found, ts=now)
    return found


def _adb(args: list[str], timeout: int = 8) -> tuple[int | None, str]:
    exe = adb_path()
    if not exe:
        return None, "未找到 adb（把 platform-tools 加进 PATH，或设置 ANDROID_HOME）"
    try:
        r = subprocess.run(
            [exe, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return None, f"adb {' '.join(args)} 超时"
    except Exception as exc:
        return None, str(exc)


def adb_devices() -> tuple[list[dict], str]:
    rc, out = _adb(["devices"])
    if rc is None:
        return [], out
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devs.append({"serial": parts[0], "state": parts[1]})
    return devs, out


def adb_rules() -> list[str]:
    _, out = _adb(["reverse", "--list"])
    return [ln.strip() for ln in out.splitlines() if "tcp:" in ln]


def ensure_reverse(ports, tries: int = 3) -> dict:
    """(Re)create adb reverse rules and verify them. Idempotent."""
    devs, _ = adb_devices()
    if not devs:
        return {"ok": False, "devices": [], "rules": [],
                "message": "没有检测到设备：插好 USB 线，并在手机上允许 USB 调试"}
    bad = [d for d in devs if d["state"] != "device"]
    if bad:
        state = bad[0]["state"]
        hint = {"unauthorized": "在手机上点「允许 USB 调试」",
                "offline": "重新插拔一次 USB 线",
                "no": "换一根能传数据的线"}.get(state, "重新插拔 USB 线")
        return {"ok": False, "devices": devs, "rules": [],
                "message": f"设备 {bad[0]['serial']} 状态为 {state}：{hint}"}
    rules: list[str] = []
    for _ in range(tries):
        for p in ports:
            _adb(["reverse", f"tcp:{p}", f"tcp:{p}"], timeout=6)
        rules = adb_rules()
        if all(any(f"tcp:{p}" in r for r in rules) for p in ports):
            return {"ok": True, "devices": devs, "rules": rules,
                    "message": "USB 端口映射就绪"}
        time.sleep(0.4)
    return {"ok": False, "devices": devs, "rules": rules,
            "message": "adb reverse 未生效，重新插拔 USB 线后再试"}


# ---------------------------------------------------------------------- app
class App:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.host: host_mod.Host | None = None
        self.host_error = ""
        self.lock = threading.Lock()
        self.started = time.time()
        self.adb = {"ok": False, "devices": [], "rules": [],
                    "message": "尚未检测", "ts": 0.0}
        self.adb_lock = threading.Lock()

    # ---- host lifecycle --------------------------------------------------
    def start_host(self, detach_first: bool = False) -> tuple[bool, str]:
        with self.lock:
            if self.host is not None and self.host.running:
                if not detach_first:
                    return True, ""
                try:
                    self.host.stop(detach=False)
                except Exception:
                    pass
                self.host = None
            elif self.host is not None:
                try:
                    self.host.stop(detach=False)
                except Exception:
                    pass
                self.host = None
            h = host_mod.Host(self.cfg)
            try:
                h.start()
            except Exception as exc:
                h.error = str(exc)
                self.host_error = str(exc)
                print(f"[error] 主机启动失败：{exc}")
                try:
                    h.stop()
                except Exception:
                    pass
                return False, str(exc)
            self.host = h
            self.host_error = ""
            return True, ""

    def stop_host(self) -> None:
        with self.lock:
            if self.host is not None:
                try:
                    self.host.stop()
                except Exception as exc:
                    print(f"[error] 停止主机失败：{exc}")
                self.host = None

    # ---- adb snapshot ----------------------------------------------------
    def refresh_adb(self, repair: bool = False) -> dict:
        with self.adb_lock:
            ports = (self.cfg.video_port, self.cfg.control_port)
            if repair:
                res = ensure_reverse(ports)
            else:
                devs, msg = adb_devices()
                rules = adb_rules() if devs else []
                have = all(any(f"tcp:{p}" in r for r in rules) for p in ports)
                if have:
                    text = "USB 端口映射就绪"
                elif not devs:
                    text = msg or "未检测到设备"
                else:
                    text = "端口映射缺失（空闲时会自动重建）"
                res = {"ok": have, "devices": devs, "rules": rules,
                       "message": text}
            res["ts"] = time.time()
            self.adb = res
            return res

    def watchdog(self, stop_event: threading.Event) -> None:
        """Keep adb reverse alive while idle, and refresh the adb snapshot."""
        idle_rounds = 0
        while not stop_event.wait(5):
            try:
                h = self.host
                busy = False
                if h is not None and h.hub is not None:
                    with h.hub.lock:
                        busy = h.hub.video_clients > 0
                if busy:
                    idle_rounds = 0
                    continue
                snap = self.refresh_adb(repair=False)
                ports = (self.cfg.video_port, self.cfg.control_port)
                missing = [p for p in ports
                           if not any(f"tcp:{p}" in r for r in snap["rules"])]
                if not missing:
                    idle_rounds = 0
                    continue
                # Only repair when a device is actually attached; otherwise the
                # user is on WiFi and there is nothing to forward.
                if not any(d["state"] == "device" for d in snap["devices"]):
                    continue
                idle_rounds += 1
                if idle_rounds % 2:        # try at most every ~10 s
                    continue
                print(f"[adb] 端口映射缺失 {missing}，自动重建…")
                fixed = self.refresh_adb(repair=True)
                print(f"[adb] {fixed['message']}")
            except Exception as exc:
                print(f"[adb] watchdog: {exc}")

    # ---- status payload --------------------------------------------------
    def payload(self) -> dict:
        h = self.host
        out = {
            "host": h.info() if h is not None else None,
            "display": h.status() if h is not None else {},
            "error": self.host_error,
            "adb": dict(self.adb),
            "uptime": int(time.time() - self.started),
        }
        return out


# --------------------------------------------------------------------- page
HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Win2Phone</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:22px 20px 60px;background:#f5f7fa;color:#1f2933;
     font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;font-size:14px}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
h1{font-size:19px;margin:0;font-weight:600}
.pill{font-size:12px;padding:4px 10px;border-radius:20px;background:#eceff1;color:#546e7a}
.pill.ok{background:#e6f4ea;color:#1d7a3c}
.pill.warn{background:#fff4e5;color:#a15c00}
.pill.bad{background:#fdecea;color:#b3261e}
.spacer{flex:1}
button{font-family:inherit;font-size:13px;border:1px solid #cfd8e3;background:#fff;
       color:#263238;border-radius:8px;padding:7px 14px;cursor:pointer}
button:hover{border-color:#1976d2;color:#1565c0}
button.danger:hover{border-color:#c62828;color:#c62828}
button.primary{background:#1976d2;border-color:#1976d2;color:#fff}
button.primary:hover{background:#1565c0;color:#fff}
.card{background:#fff;border:1px solid #e1e7ee;border-radius:12px;
      margin-bottom:12px;box-shadow:0 1px 2px #0000000a}
details.card>summary{cursor:pointer;padding:13px 16px;font-weight:600;color:#37474f;
      list-style:none;display:flex;align-items:center;gap:8px}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::before{content:"▸";color:#90a4ae;transition:transform .15s}
details.card[open]>summary::before{transform:rotate(90deg)}
details.card>summary+*{margin:0 16px 14px}
.card.plain{padding:14px 16px}
h2{font-size:13px;margin:0 0 10px;color:#546e7a;font-weight:600}
.kv{display:grid;grid-template-columns:88px 1fr;gap:6px 10px;font-size:13px}
.kv .k{color:#90a4ae}
.kv .v{color:#263238;word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(3,80px);grid-template-rows:repeat(3,46px);
      gap:7px;justify-content:center}
.grid button{padding:0;font-size:15px}
.grid button.on{background:#1976d2;border-color:#1976d2;color:#fff}
.center{display:flex;align-items:center;justify-content:center;background:#eceff1;
        border:1px dashed #b0bec5;border-radius:8px;font-size:12px;color:#607d8b}
.row{display:flex;gap:10px}
.row button{flex:1;padding:11px}
.row button.on{background:#1976d2;border-color:#1976d2;color:#fff}
#scale-row button{padding:9px 6px;line-height:1.3}
#scale-row .pct{display:block;font-size:15px}
#scale-row small{display:block;color:#90a4ae;font-size:11px;margin-top:2px}
#scale-row button.on small{color:#cfe3f7}
.alert{background:#fdecea;border:1px solid #f5c6c2;color:#b3261e;border-radius:10px;
       padding:11px 14px;margin-bottom:12px;font-size:13px;white-space:pre-wrap}
.hidden{display:none}
pre#log{background:#0f1720;color:#c9d6e2;border-radius:8px;padding:10px;height:280px;
        overflow:auto;font-size:12px;line-height:1.55;margin:0;white-space:pre-wrap}
.tag{font-size:12px;color:#78909c;margin-left:6px;font-weight:400}
.hint{font-size:12px;color:#90a4ae;margin-top:8px}
</style></head><body>

<header>
  <h1>Win2Phone</h1>
  <span class="pill" id="pill">连接中…</span>
  <span class="spacer"></span>
  <button id="btn-restart">重启主机</button>
  <button id="btn-quit" class="danger">退出</button>
</header>

<div id="alert" class="alert hidden"></div>

<div class="card plain">
  <h2>状态</h2>
  <div class="kv">
    <div class="k">手机</div><div class="v" id="v-phone">—</div>
    <div class="k">虚拟屏</div><div class="v" id="v-vdd">—</div>
    <div class="k">分辨率</div><div class="v" id="v-res">—</div>
    <div class="k">编码器</div><div class="v" id="v-codec">—</div>
    <div class="k">提示</div><div class="v" id="v-note">—</div>
  </div>
</div>

<details class="card" open>
  <summary>① 手机摆放位置</summary>
  <div class="grid">
    <span></span><button data-side="top" style="grid-area:1/2">上</button><span></span>
    <button data-side="left" style="grid-area:2/1">左</button>
    <span class="center" style="grid-area:2/2">主屏</span>
    <button data-side="right" style="grid-area:2/3">右</button>
    <span></span><button data-side="bottom" style="grid-area:3/2">下</button><span></span>
  </div>
  <div class="hint">决定鼠标从哪条边穿到手机；改的是 Windows 里虚拟屏的实际位置。</div>
</details>

<details class="card" open>
  <summary>② 屏幕方向</summary>
  <div class="row">
    <button data-orientation="landscape">横屏 ▭</button>
    <button data-orientation="portrait">竖屏 ▯</button>
  </div>
  <div class="hint">切换会改虚拟屏分辨率，手机会自动旋转并重连，约 1 秒中断。</div>
</details>

<details class="card" open id="sec-scale">
  <summary>③ 分辨率 / 字号 <span class="tag" id="scale-tag"></span></summary>
  <div class="row" id="scale-row">
    <button data-scale="100"><span class="pct">100%</span><small></small></button>
    <button data-scale="75"><span class="pct">75%</span><small></small></button>
    <button data-scale="67"><span class="pct">67%</span><small></small></button>
    <button data-scale="50"><span class="pct">50%</span><small></small></button>
  </div>
  <div class="hint">档位越低，桌面元素相对越大、字越好读。长宽比固定 9:20（横屏 20:9），
    手机端始终 1:1 铺满，<b>任何一档都不会拉伸</b>。切换约 1 秒中断。</div>
</details>

<details class="card" id="sec-adb">
  <summary>④ USB 连接 <span class="tag" id="adb-tag"></span></summary>
  <div class="kv">
    <div class="k">adb</div><div class="v" id="v-adbpath">—</div>
    <div class="k">设备</div><div class="v" id="v-adbdev">—</div>
    <div class="k">端口映射</div><div class="v" id="v-adbrule">—</div>
  </div>
  <div class="row" style="margin-top:12px">
    <button class="primary" id="btn-adbfix">重建端口映射</button>
  </div>
  <div class="hint">USB 模式靠 adb reverse 把手机侧 19010/19011 映射到电脑。
    长时间待机或重插线后映射可能丢失，这里可以手动重建（空闲时也会自动修复）。</div>
</details>

<details class="card" id="sec-log">
  <summary>⑤ 日志 <span class="tag">点击展开</span></summary>
  <pre id="log"></pre>
</details>

<script>
let logSeq = 0, lastStatus = null;

// 与 config.py 的 SCALES 保持一致：只缩像素数，比例恒为 9:20
const BASE_SIZE = { portrait: [1080, 2400], landscape: [2400, 1080] };
const SCALE_RATIO = { '100': [1, 1], '75': [3, 4], '67': [2, 3], '50': [1, 2] };

function refreshScaleLabels(orient){
  const b = BASE_SIZE[orient] || BASE_SIZE.portrait;
  document.querySelectorAll('[data-scale]').forEach(function(btn){
    const r = SCALE_RATIO[btn.dataset.scale] || [1, 1];
    const w = Math.floor(b[0] * r[0] / r[1]);
    const h = Math.floor(b[1] * r[0] / r[1]);
    const small = btn.querySelector('small');
    if (small) small.textContent = w + '×' + h;
  });
}

function el(id){ return document.getElementById(id); }
function setPill(cls, text){ el('pill').className = 'pill ' + cls; el('pill').textContent = text; }

function paint(s){
  lastStatus = s;
  const host = s.host || {};
  const d = s.display || {};
  const adb = s.adb || {};

  if (s.error || (host.error && !host.running)) {
    el('alert').classList.remove('hidden');
    el('alert').textContent = '主机未运行：' + (s.error || host.error);
  } else {
    el('alert').classList.add('hidden');
  }

  if (host.running) {
    const n = host.video_clients || 0;
    setPill(n > 0 ? 'ok' : 'warn', n > 0 ? '手机已连接' : '等待手机连接');
    el('v-phone').textContent = n > 0 ? ('已连接（' + n + ' 路视频）') : '未连接';
  } else {
    setPill('bad', '主机未运行');
    el('v-phone').textContent = '—';
  }

  el('v-vdd').textContent = host.vdd || (host.debug_monitor != null ? '调试模式 mss[' + host.debug_monitor + ']' : '—');
  el('v-res').textContent = d.width ? (d.width + '×' + d.height + '  @ (' + d.left + ',' + d.top + ')') : '—';
  el('v-codec').textContent = host.codec ? (host.codec + ' / ' + host.fps + ' fps / ' + host.bitrate) : '—';
  el('v-note').textContent = d.note || (d.display_applied === false ? '虚拟屏设置未生效' : '正常');

  document.querySelectorAll('[data-side]').forEach(function(b){
    b.classList.toggle('on', b.dataset.side === d.side);
  });
  document.querySelectorAll('[data-orientation]').forEach(function(b){
    b.classList.toggle('on', b.dataset.orientation === d.orientation);
  });
  document.querySelectorAll('[data-scale]').forEach(function(b){
    b.classList.toggle('on', b.dataset.scale === (d.scale || '100'));
  });
  el('scale-tag').textContent = d.scale ? ('当前 ' + d.scale + '%') : '';
  refreshScaleLabels(d.orientation);

  el('v-adbpath').textContent = s.adbpath || '（见日志）';
  el('v-adbdev').textContent = (adb.devices && adb.devices.length)
      ? adb.devices.map(function(x){ return x.serial + ' [' + x.state + ']'; }).join(', ')
      : '未检测到';
  el('v-adbrule').textContent = (adb.rules && adb.rules.length) ? adb.rules.join('   ') : '无';
  el('adb-tag').textContent = adb.message || '';
  el('adb-tag').style.color = adb.ok ? '#1d7a3c' : '#a15c00';
}

async function jget(u){
  const r = await fetch(u, {cache: 'no-store'});
  return r.json();
}

async function tick(){
  try {
    const s = await jget('/api/status');
    paint(s);
    if (el('sec-log').open) await pullLog();
  } catch (e) {
    setPill('bad', '与控制台断开');
  }
}

async function pullLog(){
  try {
    const r = await jget('/api/logs?after=' + logSeq);
    if (r.lines && r.lines.length) {
      const box = el('log');
      box.textContent += r.lines.join('\n') + '\n';
      logSeq = r.total;
      if (box.textContent.length > 200000) box.textContent = box.textContent.slice(-120000);
      box.scrollTop = box.scrollHeight;
    }
  } catch (e) {}
}

document.querySelectorAll('[data-side]').forEach(function(b){
  b.onclick = async function(){
    try { await jget('/api/cmd?type=side&value=' + b.dataset.side); } catch (e) {}
    tick();
  };
});
document.querySelectorAll('[data-orientation]').forEach(function(b){
  b.onclick = async function(){
    try { await jget('/api/cmd?type=orientation&value=' + b.dataset.orientation); } catch (e) {}
    setTimeout(tick, 1500);
  };
});

document.querySelectorAll('[data-scale]').forEach(function(b){
  b.onclick = async function(){
    const row = el('scale-row');
    row.querySelectorAll('button').forEach(function(x){ x.disabled = true; });
    try { await jget('/api/cmd?type=scale&value=' + b.dataset.scale); } catch (e) {}
    row.querySelectorAll('button').forEach(function(x){ x.disabled = false; });
    setTimeout(tick, 1800);
  };
});

el('btn-adbfix').onclick = async function(){
  this.disabled = true; this.textContent = '重建中…';
  try { await jget('/api/adb/fix'); } catch (e) {}
  this.disabled = false; this.textContent = '重建端口映射';
  tick();
};

el('btn-restart').onclick = async function(){
  if (!confirm('重启主机会短暂中断画面，继续？')) return;
  this.disabled = true; this.textContent = '重启中…';
  try { await jget('/api/host/restart'); } catch (e) {}
  this.disabled = false; this.textContent = '重启主机';
  setTimeout(tick, 800);
};

el('btn-quit').onclick = async function(){
  if (!confirm('退出会停止投屏并移除虚拟显示器，确定？')) return;
  try { await jget('/api/quit'); } catch (e) {}
  document.body.innerHTML =
    '<div style="padding:60px;text-align:center;color:#78909c">' +
    '已退出，虚拟显示器已移除。可以关闭这个页面。</div>';
};

setInterval(tick, 1000);
tick();
</script>
</body></html>"""


# --------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    app: App = None          # set in main()

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, obj: dict) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:                       # noqa: N802
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        app = self.app

        if path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            payload = app.payload()
            exe = adb_path()
            payload["adbpath"] = exe or "未找到 adb"
            self._json(payload)
            return

        if path == "/api/logs":
            try:
                after = int(q.get("after", ["0"])[0])
            except ValueError:
                after = 0
            lines, total = LOG.since(after)
            self._json({"lines": lines, "total": total})
            return

        if path == "/api/cmd":
            kind = q.get("type", [""])[0]
            value = q.get("value", [""])[0]
            h = app.host
            if h is None or h.hub is None:
                self._json({"ok": False, "error": "主机未运行"})
                return
            try:
                if kind == "side" and value in host_mod.ControlHub.SIDES:
                    note = h.hub.set_side(value)
                elif kind == "orientation" and value in ("portrait", "landscape"):
                    note = h.hub.set_orientation(value)
                elif kind == "scale" and value in SCALES:
                    note = h.hub.set_scale(value)
                else:
                    self._json({"ok": False, "error": "参数无效"})
                    return
                self._json({"ok": True, "note": note, "status": h.status()})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
            return

        if path == "/api/adb/fix":
            self._json(app.refresh_adb(repair=True))
            return

        if path == "/api/host/restart":
            ok, err = app.start_host(detach_first=True)
            self._json({"ok": ok, "error": err})
            return

        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


SERVER: ThreadingHTTPServer | None = None


def shutdown() -> None:
    time.sleep(0.3)
    app = getattr(Handler, "app", None)
    if app is not None:
        app.stop_host()
    if SERVER is not None:
        try:
            SERVER.shutdown()
        except Exception:
            pass
    print("[app] 已退出")
    os._exit(0)


# --------------------------------------------------------------------- main
def main() -> int:
    cfg = parse_args()
    if cfg.list_only:
        import mss
        import display as disp
        print(disp.describe())
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                print(f"  mss[{i}] {m}")
        return 0

    global SERVER
    app = App(cfg)
    Handler.app = app

    print(f"[app] pid {os.getpid()}，端口 视频 {cfg.video_port} / 控制 "
          f"{cfg.control_port} / 页面 {cfg.web_port}")

    srv = ThreadingHTTPServer(("127.0.0.1", cfg.web_port), Handler)
    SERVER = srv
    url = f"http://127.0.0.1:{cfg.web_port}/"
    print(f"[app] 控制台：{url}")
    print(f"[app] 日志文件：{os.path.join(BASE, 'app.log')}")

    threading.Thread(target=srv.serve_forever, daemon=True, name="w2p-http").start()

    ok, err = app.start_host()
    if not ok:
        print(f"[app] 主机未启动（页面里可以重试）：{err}")

    app.refresh_adb(repair=False)
    threading.Thread(target=app.watchdog, args=(threading.Event(),),
                     daemon=True, name="w2p-adb").start()

    if cfg.browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[app] 停止中…")
    finally:
        app.stop_host()
        try:
            srv.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
