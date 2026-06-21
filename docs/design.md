# 豆包语音输入法（Linux / Wayland）设计文档

> 状态：草案 v0.1 · 待讨论
> 目标平台：Ubuntu 25.04 / GNOME 48 / Wayland / PipeWire
> 参考项目：`doubao-murmur`（MIT，复用其豆包 ASR 逆向逻辑）

---

## 1. 目标与范围

做一个**全局语音输入法**：在任意应用的任意输入框里，**按住右 Ctrl 说话，松开就把识别出的文字输入到当前输入框**。

核心交互一句话：**push-to-hold（按住说话）→ release（松开输入）**。

非目标（v1 不做）：
- 多语言切换 UI（先固定中文）
- 软键盘 / 手柄输入（参考项目里的 SteamOS 专属功能，砍掉）
- 标点/格式后处理、自定义词库

---

## 2. 设计取舍总览：复用什么，自研什么

参考项目 `doubao-murmur/linux/` 已经是一套 **Python 3 + GTK4 + WebKitGTK** 的完整实现，它趟平了豆包 Web 版语音识别的逆向。豆包相关逻辑**几乎原样复用**，我们把全部精力放在三处差异上。

| 层 | 参考项目做法 | 本项目 | 复用度 |
|---|---|---|---|
| 登录 / 凭证提取 | WebKitGTK 加载 doubao.com，JS 拦截 `/alice/profile/self` 判登录态，提取 cookies + localStorage | 同 | **直接复用** |
| ASR WebSocket | cookies+device_id+web_id 拼 `wss://ws-samantha.doubao.com/...`，发 16k PCM，收 `event=result`→`result.Text` | 同 | **直接复用** |
| 音频采集 | sounddevice 16kHz/mono/int16 | 同 | **直接复用** |
| 凭证持久化 / 常量 | `params_store.py` / `config.py` | 同 | **直接复用** |
| **录音状态机** | toggle：按一下开始、再按一下停止 | **push-to-hold：按下开始、抬起停止** | **改语义** |
| **全局触发** | X11 XRecord（Wayland 下失效）+ 屏幕按钮兜底 | **evdev 监听右 Ctrl 的 down/up** | **重写** |
| **文本注入** | xdotool（X11）/ wl-copy | **wl-copy 写剪贴板 + uinput 模拟 Ctrl+V** | **重写** |
| **界面** | 悬浮窗 + 托盘 + 软键盘 | **全新设计**（见 §6） | **重做** |

> 复用的豆包代码以 vendored 形式纳入本仓库，保留 `doubao-murmur` 的 MIT 版权声明与出处。

### 2.1 技术栈选型理由：为什么 Python 而非 C++

**决定性理由：风险最高、最有价值的那部分（豆包逆向）在 Python 里已验证可用。**
项目真正的难点不是写代码，而是豆包 Web 版语音识别的逆向（WSS 参数拼装、JS 拦截登录态、cookies+localStorage 提取 device_id/web_id、`result.Text` 协议、认证失效识别）。`doubao-murmur/linux/` 已用 Python 趟平并验证。选 Python = vendored 直接复用；选 C++ = 把这段最脆弱（豆包随时改接口）、最易踩坑的逆向从零重写，用最高风险换几乎为零的收益。

**这个项目没有 C++ 能发挥的"热路径"。** 全是胶水 + 等 IO：ASR 计算在豆包服务端；音频 16kHz/mono、RMS 是 int16 块求均方根，毫秒级用不上；真正的延迟瓶颈是到豆包的网络往返，与语言无关；注入前的 ~50ms 是故意 sleep 等剪贴板就绪，不是语言慢。Python 的解释开销在此测不出来。

**C++ 的优势在本项目不成立：**
- 单一静态二进制 / 无解释器依赖：❌ GTK4/WebKitGTK 本身是 C 库，C++ 版同样要 `Depends: libgtk-4, libwebkit2gtk`；`.deb` 用 apt 依赖照样干净。
- 更省内存：⚠️ 略胜，但 WebKit 只在登录时加载、提完凭证即销毁，常驻态只剩 GTK+asyncio，占用本就不大。
- 实时音频 / 启动更快：❌ 本项目无实时约束，且为常驻服务、只启动一次。

**两个现实因素：**
- PyGObject 是同一套 C 库（GTK4、WebKitGTK）的绑定，功能不打折。
- 项目本质是"逐层试 Wayland 通不通"（见 §9 探针），大量试错正合 Python 快速迭代；`python-evdev` 已封装 `/dev/uinput` 的 `UInput`，C++ 需自己写 ioctl/struct。

**结论：** 瓶颈在网络与豆包接口、不在语言；最大风险是逆向的脆弱性，而它在 Python 里已验证可用。选 Python 是拿"复用已验证代码"对冲核心风险。

> 分发洁净度（不污染系统 Python）在 §7 解决：`.deb` 用 venv 打包或 PyInstaller 冻结成单目录，保留复用优势又不裸跑系统 Python。

---

## 3. 为什么这套技术选型在 GNOME Wayland 上成立

三条结论，都已在本机实测验证：

1. **全局按键监听只能用 evdev。** XRecord 在 Wayland 拿不到真实按键（参考项目源码自己承认）；GNOME 也不开放全局快捷键 API 给第三方常驻进程。`/dev/input/event*`（evdev）是唯一能拿到**物理按键 down/up**的途径，而 push-to-hold 恰恰需要 down 和 up 两个事件。

2. **文本注入只能走 uinput。** xdotool 注入不到原生 Wayland 应用；`wtype` 依赖的 virtual-keyboard 协议 **GNOME Mutter 不实现**（本机 wtype 已装但在 GNOME 上无效）。`/dev/uinput` 内核级虚拟输入设备是唯一全兼容方案。中文无法逐键"打字"，所以注入路径固定为 **剪贴板 + 模拟 Ctrl+V**。

3. **权限零障碍。** 本机当前用户**已在 `input` 组**，`/dev/input/event*` 与 `/dev/uinput` 均为 `root:input rw` —— evdev 读和 uinput 写**都不需要 sudo**。这条链路开箱即通。

---

## 4. 端到端时序（push-to-hold）

```
按下右 Ctrl (evdev KEY_RIGHTCTRL value=1)
   │  （忽略 autorepeat value=2）
   ├─► 显示悬浮窗（聆听中…）
   ├─► audio_capture.start()  ──16k PCM──► asr_client.send_audio()（连接前先缓冲）
   └─► asr_client.connect()（用缓存凭证；失效则提示重新登录）
            │
            └─◄ event=result → 悬浮窗实时刷新文字

松开右 Ctrl (evdev KEY_RIGHTCTRL value=0)
   ├─► 若按住时长 < 150ms：视为误触，取消、不注入
   ├─► audio_capture.stop()
   ├─► asr_client.finish_sending()   （保持连接等末尾结果）
   │        │
   │        ├─◄ 收到最终 result / finish 事件
   │        └─◄ 或 1s 安全超时
   ▼
完成：
   ├─► wl-copy 写剪贴板
   ├─► 短延迟（确保右 Ctrl 物理键已抬起）
   ├─► uinput 注入 Ctrl+V   →  文字落入当前输入框
   └─► 隐藏悬浮窗，回到 idle
```

关键约束：
- evdev 监听是**被动**的，不 grab 键盘 —— 右 Ctrl 事件仍会传给前台应用。右 Ctrl 单独按几乎无副作用，故可接受（不做整设备独占）。
- 末尾结果是异步的，必须在 key-up 后短暂等待（安全超时兜底），否则会丢最后几个字。参考项目的状态机已实现此逻辑，照搬。
- 注入前确保物理右 Ctrl 已抬起，避免与注入的 Left Ctrl+V 串键。

---

## 5. 组件分解

```
src/doubao_input/
├── __main__.py            入口
├── app.py                 GtkApplication 生命周期 + 组件编排
├── config.py              [复用] 豆包固定参数、路径、超时
├── app_state.py           [复用] 可观察状态（GObject signals）
│
├── doubao/                ← 复用层（vendored，少改）
│   ├── asr_client.py       [复用] WSS 客户端
│   ├── audio_capture.py    [复用] sounddevice 采集
│   ├── params_store.py     [复用] 凭证持久化
│   └── transcription.py    [改] 状态机：toggle → push-to-hold
│
├── trigger/               ← 自研：触发
│   └── evdev_ptt.py        监听右 Ctrl 的 down/up，marshal 到 GTK 主线程
│
├── inject/                ← 自研：注入
│   └── injector.py         wl-copy + evdev.UInput 模拟 Ctrl+V
│
├── login/                 ← 复用层
│   ├── login_window.py     [复用] WebKitGTK 登录 + 凭证提取
│   └── resources/
│       ├── inject-websocket.js   [复用] 登录态拦截
│       └── inject-dom.js         [复用]
│
└── ui/                    ← 自研：界面（全新设计）
    ├── overlay.py          按住时的悬浮窗
    ├── control_window.py   控制/状态窗口（登录、帮助、退出）
    └── tray.py             可选 AppIndicator 托盘
```

### 5.1 `trigger/evdev_ptt.py`（自研核心）
- 用 `python3-evdev` 库（比参考项目里裸 `struct.unpack` 干净）。
- 启动时枚举所有带 `KEY_RIGHTCTRL` 能力的键盘设备，全部监听（应对多键盘/外接键盘）。
- `KEY_RIGHTCTRL` value=1→`on_press`，value=0→`on_release`，value=2（autorepeat）忽略。
- 后台线程读事件，经 `GLib.idle_add` 投递到 GTK 主线程。
- 防误触：记录按下时刻，松开时若 <150ms 走"取消"分支。
- 设备热插拔：监听失败/设备消失时重新枚举（v1 可先简单，定时重扫）。

> **✅ 验证（先于一切，最高风险）**
> - **探针**：写一个最小脚本，枚举键盘设备并打印右 Ctrl 的 `value=1/0` 事件。
> - **期望**：当前用户（已在 `input` 组）**免 sudo** 即可读到事件；按下打印 press、松开打印 release、长按只打印一次 press（autorepeat 被忽略）。
> - **判定**：若收不到事件 → evdev 路线不通，需回到 §3 重新评估；这是整个项目的地基，必须最先打通。
> - 旁证命令：`evtest`（apt `evtest`）选中键盘设备，按右 Ctrl 应看到 `EV_KEY KEY_RIGHTCTRL value 1/0`。

### 5.2 `inject/injector.py`（自研核心）
- 进程启动时创建一个常驻的 `evdev.UInput` 虚拟键盘（含 LEFTCTRL/LEFTSHIFT/V 等 keycap）。
- 注入序列：`wl-copy <text>` → `time.sleep(~50ms)` → UInput 发 `Ctrl down, V down, V up, Ctrl up`。
- 终端类应用需要 `Ctrl+Shift+V`：Wayland 下拿不到前台窗口类名（xdotool 不可靠）。**已定：v1 一律默认 Ctrl+V**；终端里若粘贴失败，文字仍在剪贴板可手动 `Ctrl+Shift+V`。终端模式作为后续可配置开关，不进 v1。
- 剪贴板会被覆盖（与参考项目一致，可接受）；保存/恢复剪贴板留作后续可选项。

> **✅ 验证（第二高风险：Wayland 注入是否真能落字）**
> - **探针**：最小脚本——`wl-copy "测试注入123"` 后用 `evdev.UInput` 发一次 Ctrl+V。
> - **手测步骤**：先把光标放进一个 GTK 文本框 / 浏览器输入框 / `gedit`，再运行探针，观察文字是否落入。
> - **期望**：免 sudo 创建 UInput 成功；"测试注入123" 出现在焦点输入框里。
> - **分项判定**：
>   - 剪贴板单独验证：`wl-copy "abc" && wl-paste` 应回显 `abc`。
>   - UInput 单独验证：用 `evtest` 看是否多出一个虚拟键盘设备、Ctrl+V 是否被系统识别。
>   - 中文确认：必须用**剪贴板路径**验证中文（逐键注入打不出中文，是预期的，不算 bug）。
> - **判定**：若注入不到原生 Wayland 应用 → 复核是否误用了 wtype/xdotool；uinput 是内核级，理论上对所有应用有效。

### 5.3 `doubao/transcription.py`（改语义）
参考项目的 `handle_toggle()` 改为两个入口：
- `handle_press()`：等价于原 `_start_recording()`。
- `handle_release()`：等价于原 `_stop_recording()`（含 finish_sending + 安全超时 + 完成注入）。
其余状态流转、认证失效处理、安全超时逻辑**原样保留**。

> **✅ 验证（状态机逻辑，可纯单元测试、无需真硬件）**
> - 用假的 `audio_capture` / `asr_client`（mock）驱动状态机，断言：
>   - `press → release` 且**有最终结果** → 触发一次注入，回到 idle。
>   - `press → release` 但**结果迟到** → 1s 安全超时后用当前文本注入。
>   - 按住 <150ms → 走取消分支，**不**注入。
>   - 认证失效事件 → 清凭证、置未登录、弹重登。
> - 这一层不碰 Wayland/硬件，适合写成 pytest 回归用例，锁住交互语义。

---

## 6. 界面重新设计

设计原则：**够用即隐身**。语音输入是瞬时动作，UI 只在两个时刻出现。

### 6.1 按住悬浮窗（主角）—— 风格：**波形胶囊**（已选定）
- 形态：屏幕**顶部居中**一颗圆角"胶囊"，半透明深色，不抢焦点（`set_can_focus(False)`）。
- 内容：左侧 🎤 + **实时音量波形**，右侧实时识别文字。
  - 无文字时：波形占满胶囊，随麦克风音量跳动 —— 让用户确信"确实在听"。
  - 有文字时：波形缩到左侧一小段，腾出空间给识别文字（随长度增高，最多 N 行后滚动）。
  - 没收到声音时波形压平，作为"麦克风没拾到音"的即时提示。
- 启动瞬间（连接中）可先显示一个轻量 loading 态，连上后转为波形。
- 生命周期：右 Ctrl 按下即刻出现，松开注入后淡出。
- Wayland 定位：GNOME 由合成器决定窗口位置，`present()` 后大致居中即可；精确坐标不强求（无 layer-shell，不依赖）。

**音量数据来源**：在 `audio_capture` 的 PCM 回调里顺手算一次 RMS（int16 块的均方根，开销极小），经 `GLib.idle_add` 喂给悬浮窗的 `GtkDrawingArea` 驱动波形重绘。波形条用最近若干帧 RMS 做滚动柱状图。

> **✅ 验证（隐藏风险：悬浮窗绝不能抢焦点）**
> - **为什么关键**：若胶囊窗口抢走键盘焦点，松开右 Ctrl 后"当前输入框"已不是原来那个，注入就落错地方——整个闭环作废。
> - **手测**：焦点放进 gedit → 弹出悬浮窗 → 确认 gedit 标题栏仍显示为活动窗口、光标仍在 gedit 里闪。
> - **期望**：悬浮窗显示但 gedit 保持输入焦点；`set_can_focus(False)` 生效。
> - **判定**：若 GNOME 仍把焦点给了悬浮窗，需考虑用 override-redirect/不接受输入的窗口类型，或最坏情况下注入前先记录并不依赖"当前焦点"（备选方案）。

> 与参考项目最大不同：**没有常驻的屏幕 PTT 按钮**（物理键即触发器），界面更干净。

### 6.2 控制 / 状态窗口
- 单实例应用：再次启动即 `present()` 这个窗口（systemd 常驻 + 命令行唤起）。
- 内容：登录状态、"登录豆包"按钮、使用说明（右 Ctrl 按住说话）、退出。
- 登录窗口复用 `login_window.py` 的 WebKitGTK 实现。

### 6.3 托盘（可选，降级友好）
- GNOME 48 默认不显示 StatusNotifierItem 托盘，需 AppIndicator 扩展。
- 策略：检测到 SNI 可用就显示 🎤 托盘；否则**静默降级**，功能不受影响，引导用户用控制窗口。

> 待讨论：悬浮窗的视觉风格（胶囊 / 极简条 / 波形动画）我可以出几版 mockup 给你选。

---

## 7. 打包与分发：`.deb`

面向最终目标（Ubuntu `.deb`）规划，开发期用 venv。

### 7.1 运行依赖（apt）
- `python3` (3.13)、`python3-gi`、`gir1.2-gtk-4.0`、`gir1.2-webkit-6.0`
- `python3-sounddevice`（或 venv 装 `sounddevice`）、`python3-evdev`、`python3-websockets`
- `wl-clipboard`（提供 `wl-copy`）
- PipeWire / PulseAudio（系统自带）
> 本机已确认缺 GTK4/WebKit6 的 typelib，需在依赖里列全。

### 7.2 `.deb` 结构
- 程序装到 `/opt/doubao-input/` 或 `/usr/lib/doubao-input/`。
- **分发洁净度（呼应 §2.1）**：两种打包路线择一——
  - **A. 系统包依赖**：`Depends:` 列出 `python3-gi`、`gir1.2-*`、`python3-evdev` 等，复用系统库，包体最小；
  - **B. 自带 venv / PyInstaller 单目录**：把纯 Python 依赖（websockets、sounddevice、evdev）冻进 `/opt/doubao-input/`，**不污染系统 Python**，版本可控；GTK/WebKit 这类 GI 库仍走系统（无法脱离）。
  - 倾向：先用 A（最省事）；若遇系统 Python 版本/包冲突再切 B。
- `debian/control` 列出上述 `Depends:`。
- 提供 **systemd user service**（`doubao-input.service`，`systemctl --user enable --now`），随登录会话常驻。
- 桌面入口 `doubao-input.desktop`（唤起控制窗口）。

### 7.3 `postinst`：权限自检（已定：走 `input` 组）
- 把当前用户加入 `input` 组（`usermod -aG input $SUDO_USER`），并提示需**重新登录**生效。
- 说明 evdev/uinput 依赖 `input` 组；不做需要 root 常驻的方案。
- 若已在 `input` 组（如本机）则跳过，仅提示。
> udev 规则方案（只授权 `/dev/uinput`）作为备选记录，v1 不采用。

---

## 8. 关键风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 豆包改页面/接口 | ASR 失效 | 复用即继承此风险；凭证失效已有自动重登流程 |
| 终端粘贴需 Ctrl+Shift+V | 终端里粘贴失败 | v1 默认 Ctrl+V；终端模式做成可配置；文字始终在剪贴板可手动粘 |
| 右 Ctrl 被前台应用消费 | 个别应用有副作用 | 右 Ctrl 单独按基本无害；键位可配置 |
| 末尾结果丢字 | 漏最后几个字 | key-up 后等末尾 result + 1s 安全超时（照搬参考项目） |
| 多键盘 / 热插拔 | 换键盘后失灵 | 监听全部键盘设备 + 定时重扫 |
| 无托盘（GNOME） | 找不到入口 | 降级到控制窗口 + .desktop 唤起 |

---

## 9. 测试与验证策略

### 9.1 验证哲学：探针先行（probe-first）
本项目的风险几乎全在"Wayland/GNOME 上某一层到底通不通"，而不是业务逻辑。所以**每个高风险环节先用一个最小独立探针验证可行，再写进系统**——避免把整套搭完才发现地基某层不工作。探针只为验证，不是产品代码（实现阶段再落地）。

下表按风险从高到低排序，**P0/P1 必须最先跑通**，否则架构需要回退重评。

| 编号 | 验证目标 | 探针/方法 | 通过标准 |
|---|---|---|---|
| **P0** | 依赖齐备 | `python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('WebKit','6.0')"`；`import sounddevice, evdev, websockets` | 全部 import 成功（本机当前缺 GTK4/WebKit6 typelib，需先 apt 装齐） |
| **P1** | evdev 读右 Ctrl（地基） | 最小脚本枚举键盘、打印右 Ctrl down/up；旁证 `evtest` | **免 sudo**收到 `value=1/0`，长按只一次 press（见 §5.1） |
| **P2** | uinput 注入（地基） | `wl-copy` + UInput 发 Ctrl+V，注入到 gedit/浏览器 | **免 sudo**，中文经剪贴板路径正确落字（见 §5.2） |
| **P3** | 音频采集 + 音量 | `sounddevice` 开 16k/mono/int16 流，打印每块 RMS | 拿到稳定 int16 数据；出声时 RMS 明显上升、静音时接近 0（驱动 §6.1 波形） |
| **P4** | 剪贴板 | `wl-copy "abc" && wl-paste` | 回显 `abc` |
| **P5** | 豆包登录 + 凭证提取 | 跑复用的 `login_window`，WebKitGTK 登录豆包，提取 cookies + device_id + web_id | 登录态被 JS 拦截识别；`asr_params.json` 写出非空凭证 |
| **P6** | 豆包 ASR 链路（**外部依赖，易变**） | 用 P5 的凭证连 WSS，发一段 16k PCM，收 `event=result` | 拿到非空 `result.Text`；认证失效能被识别（验证 §8 风险） |

> P5/P6 依赖豆包线上接口，是唯一可能"今天通明天变"的环节，应**最先用复用代码验证一次**，确认逆向逻辑仍有效后再继续。

### 9.2 里程碑与验收

| 里程碑 | 内容 | 验收标准（怎么算这步过了） |
|---|---|---|
| **M0 环境就绪** | apt 装依赖、建 venv | P0、P4 通过 |
| **M1 豆包链路** | 搬入复用层，跑通登录+一次识别（临时手动触发） | P5、P6 通过：能登录、能把一句话识别成文字并打印 |
| **M2 输入闭环** | evdev 右 Ctrl + 状态机改 push-to-hold + uinput 注入 | P1、P2、P3 通过；**端到端冒烟**：在 gedit 里按住右 Ctrl 说一句、松开，文字落入光标处 |
| **M3 界面** | 波形胶囊悬浮窗 + 控制/登录窗口（§6） | 按住即现胶囊、波形随音量跳、实时文字刷新、松开淡出；二次启动唤起控制窗口 |
| **M4 打包** | systemd user service + `.deb` + postinst（input 组） | 全新机器上 `apt install ./xxx.deb` → 重登 → 开机自启 → 端到端可用 |
| **M5 打磨** | 防误触(<150ms)、设备热插拔、错误提示、终端可配置 | 各项对应单元/手测用例通过（误触取消、拔插键盘后仍可用、麦克风失败有提示） |

**状态机单测**（见 §5.3）随 M2 落地，作为锁交互语义的回归用例。

**端到端冒烟脚本（手测清单，每个里程碑回归一次）**：
1. 焦点放进 gedit → 按住右 Ctrl → 出现波形胶囊
2. 说"今天天气怎么样" → 胶囊实时显示文字、波形跳动
3. 松开右 Ctrl → 胶囊淡出、文字落入 gedit
4. 极短轻点右 Ctrl → 不应注入任何东西（防误触）
5. 删掉 `asr_params.json` 再触发 → 应弹出重新登录

---

## 10. 决策记录（已锁定）

| 项 | 决策 |
|---|---|
| 触发方式 | 物理键 + evdev（被动监听，不 grab） |
| 触发键 | **右 Ctrl**（down=开始，up=停止并注入；忽略 autorepeat） |
| 交互模型 | push-to-hold（按住说话，松开输入） |
| 文本注入 | `wl-copy` 写剪贴板 + `evdev.UInput` 模拟 **Ctrl+V**（终端模式后续再加） |
| 悬浮窗风格 | **波形胶囊**（🎤 + 实时音量波形 + 文字，顶部居中） |
| 权限方案 | 加入 **`input` 组**（postinst 自动处理，提示重登） |
| 分发 | `.deb` + systemd user service 常驻；开发期用 venv |
| 豆包逻辑 | vendored 复用 `doubao-murmur`（MIT），登录/凭证/WSS/音频不重写 |

下一步：定稿后开 **M1**（venv 装依赖 → 跑通豆包登录与一次识别）。
