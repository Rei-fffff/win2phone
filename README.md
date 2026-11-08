# Win2Phone — 把安卓手机做成 Windows 的第二块屏

目标：**手机是一块真正的 Windows 扩展显示器**，可以从主屏把任意窗口拖过去。
WiFi / USB 双通道、横竖屏、触控回传、以及「虚拟屏摆在主屏哪一侧」都可切换。

> 状态：**M1 已跑通**。虚拟显示器驱动已安装，手机显示的是真正的第三块屏
> （1080×2400 / 2400×1080 原生比例，1:1 无变形），鼠标光标已合成进画面，
> 触控坐标按物理像素映射，断开保留虚拟屏、退出 host 时才摘除。
>
> 启动方式：双击工程根目录的 **`start.bat`**（详见文末 M1 小节）。

## 需求定稿

| 需求 | 实现归属 |
|---|---|
| 手机完全是副屏、窗口能拖过去 | 虚拟显示器驱动 + Win+P「扩展」 |
| 一个窗口占满 / 多个窗口并排 | **Windows 自带**（双击标题栏最大化、Win+方向键、Snap Layouts），本项目不实现 |
| 选择「从哪里拖过去」 | 本项目控制通道的 `side` 消息，动态调整虚拟屏相对主屏的位置 |
| WiFi / USB 切换 | 手机端只换目标地址；USB 走 `adb reverse` |
| 横 / 竖屏 | 改虚拟屏分辨率 + 重配编码器（M0 留接口，M1 完善自动重建） |
| 触控 | 手机 `onTouch` → 归一化坐标 → `SendInput` 注入 |

## 架构

```
Windows                                       Android
┌──────────────────────┐                      ┌──────────────────────┐
│ Virtual Display (VDD)│  H.264 Annex-B NAL   │ MediaCodec 硬解       │
│  ↓ mss 抓屏 (BGRA)   │ ── TCP 19010 ───────▶ │  ↓ SurfaceView 全屏   │
│ FFmpeg NVENC / x264  │                      │ onTouch → JSON        │
│ SendInput 注入        │ ◀── TCP 19011 ────── │ 悬浮窗 / 设置按钮     │
│ display.py 位置切换   │   控制消息           │                      │
└──────────────────────┘                      └──────────────────────┘
```

帧协议见 [`protocol.md`](protocol.md)。

## 前置条件（只在你要真跑时才做）

1. **装虚拟显示器驱动**（唯一必须借的现成组件）
   ```
   winget install --id=VirtualDrivers.Virtual-Display-Driver -e
   ```
   项目主页 <https://github.com/VirtualDrivers/Virtual-Display-Driver>（MIT，IddCx 1.10，已签名，用户态）。
   装完到「设置 → 系统 → 显示」确认多出一块屏，**Win+P 选「扩展」**。
   注意：更新显卡驱动前先卸载 VDD，否则可能黑屏（项目 README 有说明）。
2. **Windows 端**：Python 3.10+、FFmpeg 在 PATH（NVENC 需 N 卡；无 N 卡自动回退 libx264）
   ```
   cd pc
   pip install -r requirements.txt
   python -c "import win32api, mss; print('ok')"
   ```
3. **Android 端**：Android Studio 打开 `android/` 目录即可（minSdk 26，Kotlin + MediaCodec，无 GMS 依赖）。
   手机需已开启 USB 调试并授权（这台机器已完成）。

## 运行

**WiFi 模式**：PC 和手机同一局域网，PC 端先起服务：

```bash
cd pc
python host.py --monitor 2 --device 1 --codec auto --side right
```

`--monitor` 是 mss 的显示器序号（0=全部，1=主屏，2=第二块屏），`--device` 是 pywin32 枚举序号
（0=主屏，1=虚拟屏）。先用 `python host.py --list` 打印实际列表再填。

手机端填 PC 的局域网 IP，点 Connect。

**USB 模式**（推荐，延迟更低且不受 WiFi 干扰）：

```bash
adb reverse tcp:19010 tcp:19010
adb reverse tcp:19011 tcp:19011
```

手机端填 `127.0.0.1` 即可 —— 数据走 USB，**不需要 root、不需要 USB 网络共享**。
两种模式只是地址不同，协议完全一样。

切换摆放位置（手机从左搬到右）：手机端点「右 / 左 / 上 / 下」，或 PC 端
`--side left/top/bottom`。控制消息会调用 `ChangeDisplaySettingsEx` 重排显示器，
鼠标就会从对应边缘穿过去。

## 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 抓虚拟屏 → H.264 → TCP → Android 解码显示；控制通道 + 触控 + side 切换 | 源码已写，未联调 |
| M1 | 横竖屏自动重建编码器与解码器、SPS/PPS 解析宽高、letterbox 下的触控坐标修正 | 待做 |
| M2 | 悬浮窗 / 自由窗口模式、USB/WiFi 一键切换 UI、码率自适应 | 待做 |
| M3 | UDP + 抖动缓冲、硬件光标、音频转发 | 待做 |

## 已知简化（M0 故意没做的）

- ~~分辨率靠两端约定~~ → **M0.1 已改为从 SPS 真实解析宽高**（并读 `hello` 作兜底）
- 切换横竖屏后需要手动重连一次（编码器重启 + 解码器重建）；M0.1 已能在 Surface 重建后自动重建解码器，但 PC 端编码器仍需重启
- 触控按 SurfaceView 尺寸归一化，未处理画面 letterbox 黑边
- 单客户端、TCP 传输、无鉴权：只在可信局域网 / `adb reverse` 下使用，别把 19010/19011 暴露到公网
- 输入注入用鼠标事件（`SendInput`），未做 Windows 原生多点触控注入

## 2026-11-08 调试记录（USB 优先）

- **端口改为 19010 / 19011**：`127.0.0.1:9010` 被 Logitech G HUB
  (`lghub_agent.exe`) 占用，回环自测会连错进程。不要动那个进程。
- `adb reverse tcp:19010 tcp:19010` 与 `tcp:19011 tcp:19011` 已建立。
- PC 依赖装在项目内 `pc/.venv`：`mss 9.0.2`、`pywin32 312`、
  `imageio-ffmpeg 0.8.1`（自带 `ffmpeg-win-x86_64-v9.0.2.exe`，含 NVENC 与 libx264）。
  本机 pip 26 的 index 解析异常（代理 502 / "from versions: none"），
  故用 `fetch_wheels.py` 从清华镜像直取 wheel，再
  `--no-index --find-links .wheels` 离线安装。
- **FFmpeg 9.0.2 不认 `-rc cbr-ld-h264`**，已移除；`auto` 改为实际编码探测，
  实测选中 `h264_nvenc`，失败自动回退 `libx264`。
- 调试阶段用 `--monitor 1` 抓主屏（2560×1600）、`--side none` 跳过显示器重排，
  **不装 VDD** 也能验证传输链路；虚拟显示器驱动留到画面打通后再装。
- `python selftest.py` 回环自测：**PASS**（30 个 NAL / 459,628 B，含 SPS/PPS/IDR/non-IDR）。

PC 端启动（两个终端）：

```bash
cd pc
.venv/Scripts/python.exe -u host.py --monitor 1 --codec auto --fps 10 --bitrate 3M \
  --ffmpeg ".venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v9.0.2.exe"
.venv/Scripts/python.exe selftest.py
```

Android Studio 装在 `D:\Program Files\Android\Android Studio`，SDK 在
`D:\Program Files\Android\SDK`（向导用了自定义路径）；`android/local.properties`
已指向它，APK 已编译并安装到手机。SDK 目前只有 `android-37.0` / `build-tools 36.0.0`，
项目 `compileSdk 35` 若提示缺平台，在 AS 里点 Install 补装即可。

### 会话管理 bug 修复（14:45，反复「连不上」的根因）

- 现象：App 反复连接失败；PC 端 `19010` 出现 `CLOSE_WAIT`，
  主机日志里 `[video] client connected` 只出现一次，而 control 端口每次都连上。
- 原因：客户端断开后 `serve_stream()` 没有退出——pump 线程停止消费 FFmpeg stdout，
  管道写满后抓屏主循环永久阻塞，`video_server` 再也无法 accept 新连接。
- 修复：加入 `alive` Event（pump 发送失败即 clear）、主循环检查 alive 并在
  `finally` 里 terminate FFmpeg、`listen(5)`、会话结束打印日志。
- 验证：连续 3 次 `selftest.py` 全部 PASS，日志显示 3 次
  `client connected` → `session closed, waiting for next client`。

### 黑屏修复（14:57，M0.1）

- 现象：App 显示「已连接」但纯黑；logcat 里刷屏
  `MediaCodec: Invalid to call at Released state; only valid in executing state`。
- 根因 1：App 写死 `1080×1920` 配置解码器，而 PC 抓的是 `2560×1600`，
  尺寸不符导致 MTK C2 解码器不输出帧。
- 根因 2：反复点「连接」后旧的输出线程仍持有已 release 的 MediaCodec，
  继续 `dequeueOutputBuffer` 报错，且新解码器的输出线程可能未正常工作。
- 修复（Android 端）：
  1. 内置 SPS 解析器（exp-Golomb + 裁剪），**用码流真实宽高**配置解码器；
     `hello` 回复的宽高只作兜底
  2. `csd-0/csd-1` 去掉 Annex-B 起始码（只留 NAL 头开始的 payload）
  3. 每个 IDR 前在输入缓冲里重发一次 SPS+PPS（4 字节起始码归一），兼容任意 C2/OMX 实现
  4. `presentationTimeUs` 改为单调递增，不再恒为 0
  5. 用 `generation` 计数绑定会话；`drainOutput` 校验 `decoder === d`，
     `surfaceDestroyed` 时释放解码器并在下一个 IDR 用新 Surface 重建
  6. `Socket` 加 3 s 连接超时 / 15 s 读超时，状态栏显示「帧数」和 4 秒无画面告警
  7. `SurfaceView.setZOrderOnTop(true)`，排除窗口背景盖住视频层的情况
  8. 「USB」按钮改为一键填 `127.0.0.1` 并直接连接
- 修复（PC 端）：`ffmpeg_cmd` 增加 `-bf 0`（B 帧会让解码端重排序）；
  新增可选 `--scale WxH`（`-vf scale=...`），`hello` 回报缩放后的实际尺寸。
- 验证：`py_compile` 通过；`gradlew :app:assembleDebug --offline` **BUILD SUCCESSFUL**；
  `adb install -r` Success；离线用同一算法解析真实 NVENC SPS 得到
  `profile 100 / level 50 → 2560×1600`，URL 无误。

> 注意：主机服务被手动停止后，`127.0.0.1:19010` 会直接拒绝连接，App 现在会明确报
> `连接失败 [ConnectException] ...` 而不是一直转「连接中…」。USB 连不上先确认服务在跑。

### 15:08 端到端打通（USB 模式，黑屏彻底解决）✅

真机联调结果：`adb reverse` → PC 抓屏/NVENC → USB → MediaCodec → SurfaceView 全链路出画，
手机截屏能看到完整 Windows 桌面（含任务栏与窗口），状态栏 `已连接 1280x800 · 帧 240`，
**丢帧计数 0**。

**最关键的一条发现：MTK 硬解不接受 2560×1600。**

| 编码尺寸 | `createDecoderByType` 首选 | 结果 |
|---|---|---|
| 2560×1600（主屏原尺寸） | `c2.mtk.avc.decoder` | 所有格式均 `BAD_VALUE` / `IllegalArgumentException`；最终回退 `c2.android.avc.decoder` 软解才出画，但约 5 帧/s 丢帧 |
| **1280×800**（`--scale`） | `c2.mtk.avc.decoder` | **裸格式一次配置成功，53 ms 出首帧，0 丢帧** |

- 因此 App 端 `avcDecoderNames()` 会枚举全部 AVC 解码器，按
  `解码器 × 格式(bare / csd-raw / csd-annexb)` 自动试，并缓存成功的组合；
  厂商实现之间的差异不再需要人工适配。
- 另修掉两个真 bug：① 配置失败时**泄漏 MediaCodec 实例**（每次重试占一个 C2 组件）；
  ② 配置重试无节流（每帧一次）。现在 200 ms 节流 + 失败立即 `release()`。
- **推荐启动命令**（USB 调试阶段）：

```bash
cd /d/Files/Apps/win2phone/pc
.venv/Scripts/python.exe -u host.py --monitor 1 --codec auto --fps 10 --bitrate 3M \
  --scale 1280x800 \
  --ffmpeg ".venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v9.0.2.exe"
```

手机端点 **「USB」**（一键填 `127.0.0.1` 并连接）即可。
装好 VDD 虚拟显示器后，把 `--monitor` 改成虚拟屏序号、`--side` 改成 right/left/…
并去掉 `--scale`（虚拟屏本身可以设成 1280×800 这类硬解友好的尺寸）。

> 验证截图：`w2p_usb_working.png`（工程根目录）。

## 15:30 M0.2 · 两级连接界面 + 悬浮断开 + PC 控制面板

### 手机端（三级页面，单 Activity 内切换）

| 页面 | 内容 |
|---|---|
| ① 连接方式 | 「USB 连接（推荐）」点击后**直接连** `127.0.0.1`；「网络 / WiFi 连接」进入 ② |
| ② 网络地址 | 输入 PC 局域网 IP（会记住上次输入）、「开始投屏」/「返回」 |
| ③ 投屏中 | **全屏画面 + 唯一一个半透明「断开」按钮浮在右上角**；返回键 = 断开回 ① |

状态提示改为 Toast + logcat（`Win2Phone`），投屏页不再有常驻状态栏或设置按钮。
`SurfaceView` 从 `setZOrderOnTop(true)` 改为 **`setZOrderMediaOverlay(true)`**——
否则 surface 会盖住悬浮按钮；实测 MTK 硬解仍正常。

新增能力：
- **控制通道常驻读线程**：接收 PC 下发的 `status` / `orientation`；收到方向广播后
  用 `requestedOrientation` 旋转 Activity（`AndroidManifest.xml` 已声明
  `configChanges`，不会重建 Activity 掉线）
- **视频断线自动重连**：PC 切换方向会重启编码器并断开视频会话，App 700 ms 后自动
  重连，控制通道保持不断开；实测 `portrait` 后 800×1280 新流自动出画
- 断开时把方向恢复为 `SCREEN_ORIENTATION_UNSPECIFIED`

### PC 控制面板（`pc/panel.py`）

| 前端 | 启动方式 | 适用 |
|---|---|---|
| **本地网页界面**（默认） | 双击 `pc/start-panel.bat`，或 `python panel.py --web`；浏览器开 `http://127.0.0.1:19012/` | **零额外依赖**，本项目嵌入式 Python 没有 tkinter，走这条 |
| 原生 tkinter 窗口 | 用带 tkinter 的 CPython 跑 `python panel.py` | 想要独立窗口时 |
| 命令行自检 | `python panel.py --no-gui --side left --orientation portrait` | 脚本化验证 |

界面提供：上/左/右/下四个摆放按钮、横屏/竖屏切换、实时状态（分辨率、编码器、
帧率、VDD 是否生效）与日志；每 2 秒自动刷新状态，主机没起时自动重连。

### 主机（`pc/host.py`）配套改造

- 控制通道改为**多客户端并发**：每个连接一个线程，手机 App 与 PC 面板可同时在线
- `ControlHub` 维护 `side` / `orientation` / `scale`，任何客户端改动都会
  **广播 `status` 给所有客户端**（发送者只收一次直接回复，避免重复）
- `orientation` 会把 `--scale 1280x800` 自动转成 `800x1280`，并置 `restart` 事件
  让视频会话重建；`side` 会尝试 `ChangeDisplaySettingsEx`，无 VDD 时仍记录选择并
  在 `note` 里明确提示未生效
- 新增 `query` 消息用于面板刷新；`hello` 回复现在直接带完整状态

真机验证记录：

| 操作 | 结果 |
|---|---|
| USB 按钮一键连接 | `control hello 1280x800` → `c2.mtk.avc.decoder/bare` → `first frame rendered` |
| 投屏页 UI dump | 只有 `SurfaceView` + 一个 `断开` Button（右上角半透明） |
| 面板 `side=left` | 手机日志 `control status ... side=left` |
| 面板 `orientation=portrait` | 主机重启会话 → App 自动重连 → `SPS -> 800x1280` → `first frame rendered 800x1280` |
| 点击悬浮断开 | 回到 ①，日志 `disconnected` / `session ended` |
| 网络页填 `127.0.0.1` 开始投屏 | `connect gen=5` → 正常出画 |

截图：`p1_connect.png`、`p2_address.png`、`p3_stream.png`、`p4_portrait.png`。

---

# M1 · 真扩展屏（2026-11-08）

M0 只是把主屏镜像到手机；M1 才真正做到「系统里多一块屏，窗口能拖过去」。
这一节记录**怎么用**和**踩过的坑**，坑都写清楚免得重犯。

## 怎么启动

**双击工程根目录的 `start.bat`。** 它做三件事：

1. `adb reverse tcp:19010/19011`（没插手机时报错也无所谓，WiFi 模式用不上）
2. 另开一个窗口起控制面板，自动打开 <http://127.0.0.1:19012/>
3. 在**当前窗口**里跑 `host.py`——这个窗口就是主机，**关掉它 = 停止并摘除虚拟屏**

手改参数时直接用命令行，默认值已经能跑：

```bash
cd pc
.venv/Scripts/python.exe -u host.py                 # 默认 30fps / 14M / 竖屏 / 右侧
.venv/Scripts/python.exe -u host.py --landscape 1800x810   # 带宽吃紧时降一档
.venv/Scripts/python.exe host.py --list             # 看显示器与 VDD 状态
```

## VDD 安装（一次性）

```powershell
winget install --id=VirtualDrivers.Virtual-Display-Driver -e --source winget
```

winget 只装「控制端 + 签名驱动包」，**驱动本体要单独装一次**（管理员）：

```powershell
# 参数按实际版本目录调整
$SRC="$env:LOCALAPPDATA\Microsoft\WinGet\Packages\VirtualDrivers.Virtual-Display-Driver_Microsoft.Winget.Source_8wekyb3d8bbwe"
mkdir C:\VirtualDisplayDriver -Force
copy pc\vdd_settings.xml C:\VirtualDisplayDriver\vdd_settings.xml
& "$SRC\Dependencies\devcon.exe" install "$SRC\SignedDrivers\x86\VDD\MttVDD.inf" Root\MttVDD
```

`pc/vdd_settings.xml` 里定义了 4 个模式，全部按手机原生 9:20 比例：
`1080x2400` / `2400x1080` / `810x1800` / `1800x810`。
**改完要重启虚拟显示器设备**才生效（`devcon restart Root\MttVDD`，管理员）。

> ⚠️ 更新显卡驱动前先卸载 VDD，否则可能黑屏（VDD 官方 README 有说明）。

## M1 改了什么

| 方面 | M0 | M1 |
|---|---|---|
| 画面来源 | 主屏镜像 | 真正的 VDD 虚拟屏，独立任务栏、窗口可拖 |
| 分辨率 | 固定 1280×800 缩放 | 手机原生 1080×2400 / 2400×1080，1:1 |
| 显示器定位 | 手填 `--device 1`（指向死设备） | 按 DeviceString 自动找 VDD，索引全部废弃 |
| 横竖屏 | 只改编码尺寸，画面被拉伸 | 真的改虚拟屏分辨率 + 重排位置，手机同步旋转 |
| 光标 | 画面里没有光标 | PC 端合成进帧，手机能看到箭头 |
| 触控 | 用手写的启动快照，切边后全偏 | 每次会话重新解析几何，物理像素映射 |
| 断开 | 无处理 | 保留虚拟屏；Ctrl+C 退出 host 才摘除 |
| 启动 | 两个终端 + 长参数 | `start.bat` |

## M1 踩的坑（都验证过，别再改回去）

| 现象 | 根因 | 结论 |
|---|---|---|
| 所有显示器操作报「句柄无效」 | `--device 1` 指向 `\\.\DISPLAY2`，这是**没有接显示器的潜伏输出**，`EnumDisplaySettings` 必失败 | 只认 `EnumDisplaySettings` 成功的设备 |
| 设备名写死就崩 | VDD 重启一次后名字从 `DISPLAY9` 变成 `DISPLAY10` | 永远按 `DeviceString == "Virtual Display Driver"` 重新查找 |
| `SetDisplayConfig` 返回 0 但没效果 | 用了 `QDC_ALL_PATHS`(0x1) | **必须用 `QDC_ONLY_ACTIVE_PATHS`(0x2)**，否则静默失败 |
| `QueryDisplayConfig` 恒返回 87 | 本机该 API 对所有 flag/缓冲区组合都失败（结构体尺寸已验证 72/64 正确） | 放弃手改 path 数组，改用 `SDC_TOPOLOGY_EXTEND / _INTERNAL` |
| 虚拟屏上抓不到鼠标光标 | mss 走 GDI `BitBlt`，光标在独立图层；虚拟屏上不合成（实测移动光标 0 像素变化，主屏 9.4KB）。VDD 的 `HardwareCursor` 开/关都一样 | 自己用 `GetCursorInfo`+`GetIconInfo`+`GetDIBits` 画 |
| 光标/触控坐标整体偏移 | 进程不是 DPI-aware，虚拟桌面被报成 2912×1920（实际 3640×2400） | **必须在 `import mss` 之前**调 `SetProcessDpiAwarenessContext(-4)` |
| 改完分辨率立刻查 mss 查不到 | Windows 需要一点时间才把新矩形反映到显示器枚举 | `monitor()` 里轮询 3 秒 |
| 面板命令回复错位 | `hello` 时多发了一条 `orientation` | 一条命令只回一条消息；方向由 App 从 `status` 里读 |

## 仍未做

- 触控只映射到 `SendInput` 鼠标事件，没有 Windows 原生多点触控
- 无鉴权、无加密、TCP 单客户端：只在可信局域网或 `adb reverse` 下用
- 断开虚拟屏用的是「只保留内置屏」的拓扑，**如果接了真实外接显示器会被一起摘掉**

> 仍未完成：VDD 驱动安装、Win+P「扩展」、触控坐标与 `side` 摆放位置在 Windows 侧的
> 实机生效验证。
