# 帧协议（M0）

两条独立 TCP 连接，手机端都是客户端。默认端口：视频 `19010`、控制 `19011`。
`adb reverse tcp:19010 tcp:19010` + `adb reverse tcp:19011 tcp:19011` 后，手机连接 `127.0.0.1` 即可走 USB。

控制端口**支持多个并发客户端**（手机 App + PC 控制面板各占一条连接），
每个连接由独立线程处理；任一客户端改动状态会广播给其余客户端。

## 1. 视频通道（PC → 手机，TCP 19010）

H.264 Annex-B 裸流，按 **NAL 单元**分包，每包前置 4 字节大端长度：

```
+-------------------+-------------------------+
| length (4B, >I)   | NAL (含 00 00 01 起始码) |
+-------------------+-------------------------+
```

- 每个 NAL 都以 `00 00 01` 开头（4 字节起始码的首个 `00` 归上一包尾，已剥离）
- NAL header 在 `nal[3]`，`type = nal[3] & 0x1F`
- 关键类型：`7`=SPS、`8`=PPS、`5`=IDR、`1`=非 IDR
- 手机端先缓存 SPS/PPS，**从 SPS 解析真实宽高**，凑齐后用 `csd-0/csd-1` 配置 MediaCodec
  （csd 为去掉起始码的 NAL payload），再喂 VCL NAL
- 每个 IDR 帧的输入缓冲里会重新带上 SPS+PPS（4 字节起始码归一），便于解码器随时起播
- `presentationTimeUs` 单调递增；编码器强制 `-bf 0`，无 B 帧重排序
- 单客户端；断开后 PC 端回到 accept 等待

## 2. 控制通道（双向，TCP 19011）

UTF-8，**一行一个 JSON 对象**（`\n` 分隔）。

### 手机 → PC

| type | 字段 | 说明 |
|---|---|---|
| `hello` | `client`(`android`/`panel`) | 握手并声明角色，PC 回复完整状态 |
| `query` | — | 主动拉取当前状态（面板刷新用） |
| `touch` | `action`(`down`/`move`/`up`), `x`, `y` | `x`/`y` 为 0..1 归一化坐标 |
| `side` | `value`(`right`/`left`/`top`/`bottom`) | 改虚拟屏相对主屏位置，决定鼠标从哪边穿过去 |
| `orientation` | `value`(`portrait`/`landscape`) | 切方向：改 `--scale` 尺寸 + 重启视频会话 |
| `mode` | `value`(`wifi`/`usb`) | 仅记录，地址由手机端决定 |

示例：

```json
{"type":"touch","action":"down","x":0.421,"y":0.337}
{"type":"side","value":"left"}
```

### PC → 手机

| type | 字段 | 说明 |
|---|---|---|
| `hello` / `status` | 见下 | 完整状态；`hello` 是握手的直接回复，`status` 是广播 + 命令回复 |
| `orientation` | `value` | 广播给手机端，手机据此旋转 Activity |
| `reconfigure` | `width`, `height`, `reason` | 分辨率变了，手机端应重建解码器后重连 |
| `error` | `message` | 控制命令失败原因 |

`hello` / `status` 的公共字段：

| 字段 | 说明 |
|---|---|
| `side` | `right`/`left`/`top`/`bottom`，决定鼠标从哪条边穿到手机 |
| `orientation` | `landscape`/`portrait` |
| `width`, `height` | `--scale` 之后的**编码**尺寸；手机端仅作 SPS 之外的兜底 |
| `source_width`, `source_height` | 采集源分辨率 |
| `scale` | 当前缩放设置，`none` 表示未缩放 |
| `fps`, `codec` | 帧率与实际编码器（`nvenc`/`x264`） |
| `display_applied` | Windows 显示器设置是否真的生效；`false` 通常是没装 VDD |
| `note` | 人类可读提示 |

> 广播时发送者被排除，只收一次直接回复，避免同一条消息重复到达。
> `orientation` 会触发主机断开当前视频会话；手机端应在 ~700 ms 后自动重连，
> 新码流的 SPS 尺寸会随之变化。

## 3. 坐标映射（PC 端 `SendInput`）

手机上报的归一化坐标先还原成虚拟屏内像素：

```
target_x = monitor.left + nx * monitor.width
target_y = monitor.top  + ny * monitor.height
```

再换算成 `MOUSEEVENTF_ABSOLUTE` 需要的 0..65535（相对**整个虚拟桌面**，不是单块屏）：

```
vx,vy,vw,vh = GetSystemMetrics(SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN /
                               SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN)
abs_x = (target_x - vx) * 65535 / (vw - 1)
abs_y = (target_y - vy) * 65535 / (vh - 1)
```

> 这正是「手机摆在左边还是右边」会影响手感的原因：`side` 改变的是 `monitor.left/top`，
> 上面的映射会自动跟着变，不需要手机端感知。

## 4. 安全边界

无鉴权、无加密。仅用于可信局域网或 `adb reverse` 的 USB 回环。
不要把 19010/19011 映射到公网，也不要在共享网络下长期开着。
