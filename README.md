# 豆包语音输入法 for Linux

> 在 Ubuntu / GNOME / Wayland 上,**按住右 Ctrl 说话,松开就把识别出的文字输入到当前输入框**。

本项目把豆包(Doubao)Web 版的语音识别能力做成一个常驻的全局语音输入法,
专为原生 Wayland / GNOME 设计,避开 xdotool 在 Wayland 下失效、`wtype` 依赖 virtual-keyboard 协议
(Mutter 不实现)等问题。复用 [doubao-murmur](https://github.com/yourname/doubao-murmur) 的豆包逆向逻辑
(登录、WebSocket ASR、音频采集),在其基础上重写触发、注入与 UI。

详细设计见 [`docs/design.md`](docs/design.md)。

---

## ✨ 特性

- **按住即说,松手即输** —— push-to-hold 交互,右 Ctrl 物理键触发。
- **GNOME / Wayland 原生可用** —— 用 `evdev` 监听 + `uinput` 模拟,不依赖 X11/XWayland。
- **中文 OK** —— 走剪贴板 + 模拟 Ctrl+V,不靠逐键输入。
- **波形胶囊悬浮窗** —— 屏幕顶部居中,按住时显示实时音量波形 + 实时识别文字。
- **会话常驻** —— `systemd --user` 自启;再次启动唤起控制窗口(单实例)。
- **凭证本地化** —— 登录一次,凭证写在 `~/.config/doubao-input/asr_params.json`,复用至下次。
- **零 sudo 运行** —— 仅需把用户加入 `input` 组(`.deb` 的 `postinst` 自动处理)。

---

## 📦 系统要求

| 组件 | 要求 |
|---|---|
| 操作系统 | Ubuntu 25.04 / GNOME 48 / Wayland(已在该环境实测) |
| Python | 3.13 |
| 音频 | PipeWire / PulseAudio(系统自带) |
| 用户组 | 当前用户需在 `input` 组(访问 `/dev/input/event*` 与 `/dev/uinput`) |

---

## 🛠️ 安装

### 1. 安装系统依赖

```bash
sudo apt install \
    python3-gi gir1.2-gtk-4.0 gir1.2-webkit-6.0 \
    python3-sounddevice python3-evdev python3-websockets \
    wl-clipboard
```

### 2. 加入 `input` 组(关键)

```bash
sudo usermod -aG input $USER
```

> ⚠️ **需要重新登录**才能生效。`/dev/uinput` 与 `/dev/input/event*` 都是 `root:input`,加入 `input` 组后免 sudo 即可读写。

### 3. 创建虚拟环境并安装 Python 依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 见下方"开发"
```

### 4. 跑起来

```bash
./start.sh
```

`start.sh` 等价于:

```bash
PYTHONPATH=src .venv/bin/python -m doubao_input
```

首次启动会弹出**登录窗口**,用 WebKitGTK 加载豆包网页;在 WebView 里登录成功后,
JS 拦截到登录态,自动从 `/alice/profile/self` 抓 cookies + 从 `localStorage` 抓 `device_id` / `web_id`,
写入 `~/.config/doubao-input/asr_params.json`,登录窗口自动关闭。

之后按下右 Ctrl 即可:

- **按下**:弹出波形胶囊悬浮窗,实时显示识别文字
- **松开**:等待 1s 安全超时(收齐末尾识别结果),把文字 `wl-copy` 写剪贴板 + `uinput` 发 Ctrl+V → 落入当前输入框
- **按住 <150ms**:视为误触,取消,不注入

---

## 🧑‍💻 开发

### 项目结构

```
src/doubao_input/
├── __main__.py            入口 (`python -m doubao_input`)
├── app.py                 GtkApplication 生命周期 + 组件编排
│
├── doubao/                ← 复用层 (vendored, 少量修改)
│   ├── asr_client.py       WSS 客户端
│   ├── audio_capture.py     sounddevice 16kHz/mono/int16 采集 + RMS 回调
│   ├── params_store.py      凭证持久化 (JSON)
│   ├── config.py            固定参数、路径、超时
│   ├── app_state.py         GObject 可观察状态
│   ├── login_window.py      WebKitGTK 登录 + JS 拦截凭证
│   └── host_tools.py        wl-copy / xclip 候选命令
│
├── trigger/evdev_ptt.py   ← 自研:右 Ctrl 物理键监听
├── inject/injector.py     ← 自研:wl-copy + uinput Ctrl+V
├── login/resources/        注入到 WebView 的 JS
└── ui/                    ← 自研:界面
    ├── overlay.py          顶部波形胶囊悬浮窗
    └── control_window.py   控制 / 登录入口窗口
```

### 复用与自研边界

`src/doubao_input/doubao/` 来自上游 `doubao-murmur` 的 Linux 端口(参见 [`NOTICE`](NOTICE))。
本项目相对上游的修改在每个文件顶部的 docstring 里有列出,主要是:

1. 包内导入从 `doubao_murmur.xxx` 改为 `doubao_input.doubao.xxx`
2. `audio_capture.py` 增加 `on_rms` 回调(驱动波形胶囊)
3. `transcription.py` 把上游的 `handle_toggle()` 拆成 `handle_press()` / `handle_release()`(toggle → push-to-hold)+ 150ms 防误触

`trigger/`、`inject/`、`ui/`、`app.py`、`__main__.py` 是本项目原创。

### 探针测试

`tests/probe/` 是验证豆包登录与 ASR 链路是否仍可用的最小探针(非产品代码):

```bash
# P5: 登录豆包,写凭证
.venv/bin/python tests/probe/P5_login.py

# P6: 用凭证连 WSS,录 5 秒,看是否返回 result.Text
.venv/bin/python tests/probe/P6_asr.py
```

每个高风险环节(依赖、evdev、uinput、音频、剪贴板、登录、ASR)在
[`docs/design.md`](docs/design.md) §9 都有验证方法。开发期应先跑通 P5/P6 再动产品代码。

### 日志

启动时 `INFO` 级别,关键事件都有日志:

```
on_activate: setup_done=False
build complete
EvdevPtt started on N device(s)
WS open, 开始录…
result: 今天天气怎么样
clipboard: wl-copy ok
paste: uinput ok (shift=False, gap=12ms)
```

调试:把 `logging.basicConfig(level=logging.DEBUG)` 改成 DEBUG 即可看到 evdev 设备扫描等细节。

---

## 📤 打包(计划中)

目标:`.deb` 包 + systemd user service 常驻。

- 程序装到 `/opt/doubao-input/` 或 `/usr/lib/doubao-input/`
- `debian/control` 的 `Depends:` 列出 apt 系统包
- `postinst` 自动 `usermod -aG input $SUDO_USER` 并提示重登
- 提供 `doubao-input.service` (`systemctl --user enable --now`)
- 提供 `doubao-input.desktop` 唤起控制窗口

详见 [`docs/design.md`](docs/design.md) §7。

---

## ⚠️ 已知限制

- **终端粘贴需 Ctrl+Shift+V**:Wayland 下拿不到前台窗口类名,v1 默认 Ctrl+V;终端里若粘贴失败,
  文字仍在剪贴板,可手动 Ctrl+Shift+V。终端模式作为后续可配置开关。
- **托盘在 GNOME 48 默认不可见**:GNOME 48 默认不显示 StatusNotifierItem 托盘,
  需 AppIndicator 扩展;否则静默降级,功能不受影响,引导用控制窗口。
- **复用即继承豆包接口变动风险**:豆包 Web 版接口一旦变更,ASR 会失效;凭证失效会触发自动重登。
- **剪贴板被覆盖**:每次粘贴覆盖系统剪贴板(与 `doubao-murmur` 一致)。

---

## 📄 许可证

MIT — 见 [`LICENSE`](LICENSE)。复用代码来自 [`doubao-murmur`](https://github.com/yourname/doubao-murmur)
(MIT),详见 [`NOTICE`](NOTICE)。