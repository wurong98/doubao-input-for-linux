# X11 / GTK3 移植说明

> 分支：`feat/x11-gtk3-port`
> 目标环境：Ubuntu 20.04 LTS · GNOME on **X11** · Python 3.8
> 原项目设计目标：Ubuntu 25.04 / GNOME 48 / **Wayland** / Python 3.11+

本文档记录从原 GTK4 + Wayland 版本，**降级到 GTK3 + WebKit2 4.0 + X11** 在 Ubuntu 20.04 上跑起来所做的全部改动，以及为什么这么改。原设计文档参见 [`docs/design.md`](design.md)，本文档**只覆盖偏离原设计的部分**。

---

## 1. 为什么有这个分支

CLAUDE.md 把项目目标定在 **GNOME / Wayland / Python 3.11+**。但目标机器实际是：

| 维度 | 项目要求 | 当前机器 |
|---|---|---|
| Python | `>=3.11` | **3.8.10** |
| GUI 工具包 | GTK 4 | **GTK 3**（系统无 `gir1.2-gtk-4.0`） |
| 浏览器嵌入 | WebKit 6 / WebKit2 4.1 | **WebKit2 4.0** |
| 桌面会话 | Wayland | **X11**（`XDG_SESSION_TYPE=x11`） |
| 剪贴板 | `wl-copy` | 无 wl-clipboard，有 `xclip` |
| 输入组 | 假设已有 | **当时不在 `input` 组** |

升级 Ubuntu 是大动作，于是选 **C 路线**：改造代码让它在当前系统上跑，参考 `/home/liudf/tools/zhipu-asr` 的 X11 思路。

---

## 2. 改动总览

```
 pyproject.toml                          |  +/-7    Python 版本 / websockets 上限
 src/doubao_input/app.py                 |  +66    GTK4→3、托盘、目标窗口、hold/release
 src/doubao_input/doubao/login_window.py | rewrite WebKit6→WebKit2 4.0
 src/doubao_input/inject/injector.py     |  +180   按窗口类型选粘贴策略（xdotool type / Ctrl+V）
 src/doubao_input/trigger/evdev_ptt.py   |  +23    诊断日志
 src/doubao_input/ui/control_window.py   | rewrite GTK4→3
 src/doubao_input/ui/overlay.py          | rewrite GTK4 Picture/Texture → GTK3 DrawingArea+cairo
 src/doubao_input/ui/tray.py             | NEW     AppIndicator3 系统托盘
 start.sh                                |  +63    自举 venv、libffi 适配、杀僵尸进程
 tests/probe/P6_asr.py                   |  +/-1   GTK 版本号
```

---

## 3. 按模块说明

### 3.1 `pyproject.toml`

- `requires-python = ">=3.11"` → `">=3.8"`。
- `websockets>=12` → `"websockets>=10,<12"`。websockets 12+ 砍掉了 Python 3.8 支持。
- `dependencies` 没动 sounddevice / evdev 的 minor，但实际安装版本是 `start.sh` 里 pin 死的（见 §3.9）。

### 3.2 GTK4 → GTK3：UI 三件套

`app.py` / `control_window.py` / `overlay.py` 的 `gi.require_version("Gtk", "4.0")` 全部改 `"3.0"`，并按 GTK3 的 API 重写。三类典型差异：

| GTK4 | GTK3 |
|---|---|
| `window.set_child(box)` | `window.add(box)` |
| `box.append(child)` | `box.pack_start(child, expand, fill, padding)` |
| `window.connect("close-request", h)` | `window.connect("delete-event", h)`，返回 `True` 拦截关闭 |
| widget 默认可见 | widget 默认不可见，必须 `window.show_all()` |
| `Gdk.Display.get_monitors()`（GListModel） | `Gdk.Display.get_monitor(i)` / `get_primary_monitor()` |
| `Gtk.Picture` + `Gdk.Texture` | `Gtk.DrawingArea` + `draw` 信号 + cairo |

#### 3.2.1 `control_window.py`：完全重写

去除了 GTK4 专属的 `Snapshot` 截图调试代码（约 30 行），主体改成 `Gtk.Box.pack_start` + `Gtk.Window.add`。窗口默认可见性差异要靠 `show_all()` 补齐。

关闭窗口（×）走 `delete-event` 返回 `True`：窗口隐藏不销毁，进程留在后台，配合后面加入的托盘形成"主窗口 = 入口、托盘 = 常驻"的格局。

#### 3.2.2 `overlay.py`：cairo 重写

GTK4 版本用 `Gtk.Picture` 显示一张 `Gdk.Texture`（cairo 渲染好之后转成纹理上传），GTK3 没有 `Gdk.Texture` 的简洁接口。改用：

- 容器：`Gtk.Overlay`，底层 `Gtk.DrawingArea` 直接接 `"draw"` 信号画**圆角面板 + 波形 bars**，上层叠一个 `Gtk.Label` 显示文字（文字不参与呼吸动画的颜色衰减）。
- 窗口透明：必须三件套——`Gtk.Window(type=POPUP)` + 屏幕 RGBA visual + `set_app_paintable(True)`，否则圆角外露白底。
- 呼吸/敲击流光：仍走 `Gtk.Window.set_opacity()` 动整体透明度，GNOME on X11 下 Mutter 是合成器，能识别。
- 监视器枚举：GTK3 是 `display.get_monitor(i)`，不再有 `get_monitors()` 这个 GListModel。

视觉规范（圆角、呼吸 0.78-0.98、敲击跳 1.0 持续 220ms）与原 GTK4 版本严格一致。

#### 3.2.3 `app.py`：GTK 版本号 + 多处行为修正

主要改动：
- `gi.require_version("Gtk", "4.0")` → `"3.0"`，**仅一行**。GTK Application API 在 3/4 之间兼容性较好。
- `Gio_APPLICATION_NON_UNIQUE()` 之前返回 `FLAGS_NONE`（实际就是默认 unique）——**和函数名相反**。改成真正返回 `Gio.ApplicationFlags.NON_UNIQUE`。这个 bug 直接导致"start.sh 启动后 PID 立刻消失"的怪现象：上一次没退干净的实例会通过 D-Bus 把新启动当成"二次激活"，新进程立即退出。
- `_on_activate` 里加 `self.hold()`：托盘场景下用户可能把所有窗口都关掉了进程还得活着，hold 计数 > 0 才能阻止 GtkApplication 自动退出。
- `_quit()` 配对 `self.release()`。
- 新增 `_target_window` 字段和 `_capture_active_window()`：在右 Ctrl 按下瞬间用 `xdotool getactivewindow` 记下当前焦点窗口，供 `_do_paste` 选粘贴策略用（详见 §3.6）。
- 拼装托盘（§3.5）。

### 3.3 `login_window.py`：WebKit2 4.0 重写

原代码尝试 WebKit 6 → WebKit2 4.1 两段 try/except；这台机器只有 **WebKit2 4.0**。两套 API 在 JS 求值上不兼容：

| WebKit 6 / 4.1 | WebKit2 4.0 |
|---|---|
| `webview.evaluate_javascript(js, ...)` + `evaluate_javascript_finish` | `webview.run_javascript(js, cancel, cb, ud)` + `run_javascript_finish` |
| 结果是 `JSC.Value`（直接 `to_string()`） | 结果是 `WebKitJavascriptResult.get_js_value() -> JSC.Value` |
| Cookies: `webview.get_network_session().get_cookie_manager()` | Cookies: `webview.get_context().get_cookie_manager()` |
| `close-request` | `delete-event` |

#### 3.3.1 一个隐蔽的 bug：双重 JSON 编码

`extract_local_storage` 让 JS 返回 `JSON.stringify({device_id_raw: ..., tea_cache_raw: ...})`——这是一个**JS 字符串**。原 `_js_result_to_string` 直接对 `JSC.Value` 调 `to_json(0)`，**又给字符串外面套了一层 JSON 引号**：

```
JS 端返回:                  {"device_id_raw": "...", "tea_cache_raw": "..."}
to_json 之后变成:        "\"{\\\"device_id_raw\\\": \\\"...\\\", ...}\""
json.loads 一次只剥一层引号, 得到的还是 str, 不是 dict.
后续 data.get("device_id_raw") → AttributeError: 'str' object has no attribute 'get'
```

修复：先 `value.is_string()` 判断；如果是字符串直接 `to_string()` 拿原文，不再 `to_json` 二次编码。

### 3.4 `injector.py`：按窗口类型选粘贴策略（**重要**）

原 injector 一律走 "wl-copy/xclip → uinput Ctrl+V"。这套对 gedit 这种 GTK 应用没问题；但是：

- **GNOME Terminal / kitty / alacritty 等终端**：Ctrl+V 不是粘贴快捷键，需要 `Ctrl+Shift+V`。
- **终端里跑的 TUI（claude-code、less 等）**：连 `Ctrl+Shift+V` 都可能被拦截或字符走丢。
- **VSCode 编辑器主区域**：剪贴板路径可工作，但带 IME 的 CJK 偶发字符丢失。

参考 `/home/liudf/tools/zhipu-asr/asr_engine.py` 的做法，按目标窗口的 `WM_CLASS` 和标题分类，对终端类和 VSCode 类**走 `xdotool type` 逐字符合成 KeyPress/KeyRelease 事件**（X11 下 xdotool 通过临时绑定 keysym 支持 Unicode/CJK），完全不发 Ctrl+V。

骨架：

```
Injector.inject(text, target_window):
    if target_window 给了 + xdotool 可用:
        kind = _classify_window(target_window):
            xprop WM_CLASS + xdotool getwindowname
            → "type"  (终端 or WM_CLASS 含 'code')
            → "paste" (其他)
        if kind == "type":
            xdotool windowfocus <id>
            sleep 0.15 等焦点稳定
            xdotool keyup ctrl shift alt   # 释放残留修饰符
            xdotool type --window <id> --clearmodifiers --delay 1 -- text
        elif kind == "paste":
            wl-copy/xclip + windowfocus + uinput Ctrl+V
    else:
        老路径，无差别走 wl-copy/xclip + uinput Ctrl+V
```

`_is_terminal_window` 内置测试（在 inject 模块顶部 _TERMINAL_INDICATORS 维护），覆盖 gnome-terminal-server、Alacritty、kitty、xfce4-terminal、tilix、urxvt、PuTTY 等；并对 VSCode 内嵌终端做特判（WM_CLASS=code 且窗口标题含 bash/zsh/python/...）。

CLAUDE.md 的"injecting while our window is focused"那条注意事项仍然成立——`app.py:_do_paste` 在调 inject 前**先把 overlay + 控制窗口都 hide**，inject 完之后 600ms 再恢复控制窗口，避免 uinput Ctrl+V 落到我们自己的 widget 里。

### 3.5 新增 `ui/tray.py`：AppIndicator3 托盘

zhipu-asr 用 PySide6 的 `QSystemTrayIcon`，**这个不能搬过来**——`QApplication` 和 `GApplication` 不能在同一进程里共存。GTK 世界用 `AppIndicator3`，Ubuntu 20.04 自带 `gir1.2-appindicator3-0.1` + `ubuntu-appindicators@ubuntu.com` GNOME Shell 扩展。

托盘菜单：

- 显示主窗口（同时绑定为 `set_secondary_activate_target`，支持中键单击直接唤起）
- 检查麦克风
- ─────
- 退出

图标用 `src/doubao_input/resources/logo-128.png` 的绝对路径，零安装即可显示。CLAUDE.md 强调"GNOME 48 has no StatusNotifierItem by default — `ui.tray` must degrade silently, not fail"——所以 import `AppIndicator3` 失败 / `Indicator.new` 抛异常都被吞掉，主流程继续。

状态切换：录音中 → `IndicatorStatus.ATTENTION`，结束 → `ACTIVE`（在支持 ATTENTION 图标的 indicator 主题下可视；不支持也不影响功能）。

### 3.6 `evdev_ptt.py`：更明显的诊断日志

只加了两条日志，没改逻辑：

- 启动时打印"`evdev scan: N total device(s) under /dev/input/event*`"，N=0 时**显式提示**"this process is not in the 'input' group"以及修复方式。
- 扫到设备但都没有 KEY_RIGHTCTRL 时，警告"`evdev: N device(s) visible but none expose KEY_RIGHTCTRL`"。

这些日志是给本地排查"右 Ctrl 不响应"用的——绝大多数情况下原因都是 input 组成员关系没生效（见 §4）。

### 3.7 `start.sh`：自举 + 适配 + 杀僵尸

原版只有一行 `PYTHONPATH=src .venv/bin/python -m doubao_input`，假设 venv 已经搭好。新版做的事：

1. **首次 `./start.sh` 时自动建 venv**（`--system-site-packages`，让 GTK3/WebKit2/cairo 的 PyGObject 绑定直接复用，省下编译时间）。
2. **`export PYTHONNOUSERSITE=1`**：屏蔽 `~/.local/lib/python3.8/site-packages/`，避免那里旧版的 sounddevice/cffi 覆盖 venv 内的版本（详见 §4 的 ffi 问题）。
3. **从源码装 `cffi==1.15.1`**（`--no-binary :all:`）：PyPI 的 cffi wheel 把 libffi 3.4 静态打包进 `.so`，跟系统 libffi 3.3 不兼容；从源码装才会动态链接到系统 `libffi.so.7`。详细见 §4。
4. **pin `sounddevice==0.4.7`**：0.5.x 在新版 cffi 上同样会撞 ABI。
5. **启动前杀残留实例**：用 `pgrep -f 'doubao_input'` 把上一次没退干净的进程清理掉，否则 `GApplication` 的单实例机制会把新启动当成二次激活并立刻退出（即使 NON_UNIQUE 也建议清理）。
6. **后台启动 + 日志**：默认 `nohup ... > $HOME/.local/share/doubao-input/doubao-input.log 2>&1 &`；`--debug` 走前台 unbuffered 输出。
7. **存活检查**：fork 后 sleep 1，再 `kill -0` 确认进程没在启动期崩。

---

## 4. 这台机器上踩过的坑（按出现顺序）

### 4.1 venv 用了系统 user-site，被旧 sounddevice 污染

`pyenv` 起的 venv 默认会暴露 `~/.local/lib/python3.8/site-packages/`。那里有一份 `sounddevice 0.5.5` 是另一个项目装的，加载时崩 `ffi_prep_closure: bad user_data`。**修法**：`PYTHONNOUSERSITE=1`（已写进 start.sh）。

### 4.2 cffi wheel 静态打包 libffi 3.4，与系统 3.3 ABI 不一致

`PYTHONNOUSERSITE=1` 解决了 user-site 之后，PortAudio 回调创建还是崩同样的错。根因：

- venv 装的 cffi（无论 1.15 还是 1.17）的 wheel 都是**静态打包 libffi 3.4**。
- `PyGObject` import 时会把系统的 `libffi.so.7`（3.3）加载到进程里。
- `cffi.ffi.callback()` 调到的是进程内已经存在的 libffi 3.3 实现，**但 cffi 编译时按 3.4 的内部结构布局算的偏移**——不匹配，抛 `bad user_data`。

**修法**：`pip install --force-reinstall --no-binary :all: cffi==1.15.1`，让 cffi 从源码编译、动态链接系统 libffi 3.3。`ldd` 验证 `_cffi_backend.cpython-38-...so` 出现 `libffi.so.7 => /lib/x86_64-linux-gnu/libffi.so.7`。

### 4.3 right-Ctrl 按了没反应：`input` 组成员关系

> `sudo usermod -aG input liudf` **只改 `/etc/group`，不影响活着的进程**。

进程的"补充组"是 fork+exec 那一刻从父进程继承的。你打开 shell 时还不在 input 组，所以这个 shell 以及它 fork 的所有子进程（包括 `./start.sh` 里的 python）都看不到 input 组。`evdev.list_devices()` 返回空列表，PTT 监听不到任何按键。

三种处理（按方便度排）：

1. **彻底**：注销 GNOME 会话重新登录。下次起的所有进程都在 input 组里，`./start.sh` 直接 work。
2. **短期**：每次 `sg input -c './start.sh'`。`sg` 起一个临时 setgroups 过的子 shell，子进程能看到 input 组。
3. **代码兜底**（可选，未实现）：`start.sh` 检测自己不在 input 组就 `exec sg input -c "$0 $@"`。

诊断办法：日志里看到 `evdev scan: 0 total device(s)` 或者 `EvdevPtt failed to start` 就是这个问题。

### 4.4 `GApplication` 二次激活，start.sh 看到 PID 立刻消失

`Gio_APPLICATION_NON_UNIQUE()` 这个函数的**名字说的是 NON_UNIQUE，实际返回的是 FLAGS_NONE**（也就是 unique）。后果：上一次启动没退干净的实例还在跑，**新的 `./start.sh` fork 出的 python 会把请求通过 D-Bus 转给已有实例、自己 1 秒内退出**，start.sh 的存活检查就报"启动失败"。

修复二选一（这里两个都做了）：

- `app.py`：让函数真的返回 `Gio.ApplicationFlags.NON_UNIQUE`。
- `start.sh`：启动前先杀残留实例。

### 4.5 控制窗口被发到第二屏外

`Gtk.Window.set_position(Gtk.WindowPosition.CENTER)` 在多显示器 + HiDPI 缩放下会算到错的屏幕。当前实现优先用 `display.get_primary_monitor()` 自己算坐标。**已知遗留**：第一次 `show()` 可能仍在副屏，再次激活后会回到主屏中心；用户可以拖到任意位置。

### 4.6 登录后凭证保存失败：双重 JSON 编码

见 §3.3.1。`'str' object has no attribute 'get'` 这条 traceback 出现在 `login_window.py` 解析 localStorage 时。

---

## 5. 运行流程

### 5.1 启动

```bash
# 第一次运行 (会创建 .venv, 装 cffi/sounddevice/evdev/websockets):
./start.sh

# 如果 shell 不在 input 组 (groups 看不到 input):
sg input -c './start.sh'

# 前台调试模式:
./start.sh --debug
```

日志写在 `~/.local/share/doubao-input/doubao-input.log`。

### 5.2 使用

1. **首次**：托盘菜单或控制窗口点「登录豆包」→ 嵌入 WebView 扫码 → 凭证写入 `~/.config/doubao-input/asr_params.json`。
2. **任意输入框聚焦** → 按住右 Ctrl 说话 → 松开 → 文字自动落入。
3. **关闭主窗口**：× 只是隐藏，进程继续在托盘运行。彻底退出走托盘菜单「退出」。

### 5.3 排错路径

| 现象 | 看这里 |
|---|---|
| `evdev PTT failed to start` | §4.3 - input 组 |
| 启动后 1 秒就死掉 | §4.4 - GApplication NON_UNIQUE |
| 检查麦克风崩 `ffi_prep_closure` | §4.1 / §4.2 - cffi / libffi |
| 登录后再次按右 Ctrl 还是要求登录 | §3.3.1 - 凭证没存进去 |
| 终端里粘贴丢字符 | §3.4 - 应该走 `xdotool type` 路径，看日志是不是 `strategy=type` |

---

## 6. 与原设计的偏离

- 设计目标是 **Wayland**；当前分支是 **X11 专用**：`xdotool` / `xprop` 都是 X11 工具，PTT 注入路径里假设了 X server。
- 设计目标是 **GTK4**；当前分支是 **GTK3**。
- 设计目标是 **WebKit 6**；当前分支是 **WebKit2 4.0**。
- 项目主分支 `master` 与 CLAUDE.md 仍按原设计目标维护；本分支 `feat/x11-gtk3-port` 是临时方案，**不建议合并回 master**。等机器升级到 Ubuntu 22.04+ 后回到主分支。

---

## 7. 验证状态

| 项 | 状态 | 备注 |
|---|---|---|
| Python 编译 (`python -m compileall src`) | ✅ | 全模块通过 |
| 主进程启动 + evdev / 托盘 / GTK3 窗口 | ✅ | 在 input 组的会话里 |
| 控制窗口「检查麦克风」 RMS 输出 | ✅ | sounddevice 0.4.7 + 源码 cffi 1.15.1 |
| 控制窗口「测试粘贴」 | ✅ | 通过 xclip + uinput |
| 完整流程：右 Ctrl → ASR → 粘贴到 gedit | ✅ | strategy=paste 路径 |
| 完整流程：右 Ctrl → ASR → 粘贴到 claude-code (终端 TUI) | ✅ | strategy=type 路径 |
| 完整流程：右 Ctrl → ASR → 粘贴到 VSCode | 未验证 | 预期走 strategy=type |
| `tests/probe/P5_login.py` | ❌ | 上游就有 `LoginWindow()` 缺参数的问题，未修 |
| `tests/probe/P6_asr.py` | 未跑 | 凭证已正常生效，主程序已验证 |
