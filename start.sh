#!/usr/bin/env bash
# 启动 doubao-input。首次会自动创建一个 venv（带 --system-site-packages，
# 这样 PyGObject / cairo / WebKit2 等系统级 Python 绑定可以直接复用，无须 pip）。
set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"
PY="${PYTHON:-python3}"

# 屏蔽 $HOME/.local/lib/pythonX.Y/site-packages: 那里可能装了与 venv 不兼容
# 的旧版 sounddevice / cffi (依赖于不同 libffi), 会让 RawInputStream 在
# `ffi_prep_closure: bad user_data` 上崩. PYTHONNOUSERSITE=1 让解释器在
# 解析 sys.path 时跳过 user-site, 保证麦克风和 evdev 都走 venv 的版本.
export PYTHONNOUSERSITE=1

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[start.sh] creating venv at $VENV_DIR (system-site-packages)…" >&2
    "$PY" -m venv --system-site-packages "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    # cffi 必须从源码装: 官方 wheel 把 libffi 3.4 静态打进去, 但 Ubuntu 20.04
    # 的 PortAudio/PyGObject 已经把 libffi.so.7 (3.3) 加载进了进程, ABI 不一致
    # 会让 sounddevice 的 RawInputStream 在创建 PortAudio 回调闭包时崩
    # (`ffi_prep_closure: bad user_data`). 从源码装时 cffi 会动态链接系统
    # libffi 3.3, 和进程里的版本一致.
    "$VENV_DIR/bin/pip" install --force-reinstall --no-binary :all: \
        "cffi==1.15.1"
    # sounddevice 0.4.7 是 Ubuntu 20.04 + 系统 libffi 3.3 上验证可用的版本;
    # 0.5.x 在新的 cffi 上能崩. websockets 11 是 py3.8 上仍维护的分支.
    # evdev 用 --force-reinstall 防止它被 user-site / dist-packages 覆盖.
    "$VENV_DIR/bin/pip" install --force-reinstall --no-deps \
        "websockets>=10,<12" \
        "sounddevice==0.4.7" \
        "evdev>=1.4"
fi

# 走 PulseAudio (Mutter on Ubuntu 20.04 默认是这套)
export ALSA_PLUGIN_DIR="${ALSA_PLUGIN_DIR:-/usr/lib/x86_64-linux-gnu/alsa-lib}"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

LOG_FILE="${LOG_FILE:-$HOME/.local/share/doubao-input/doubao-input.log}"
mkdir -p "$(dirname "$LOG_FILE")"

# 杀掉之前没退干净的实例 (比如 sg input 包起来的子 shell 吞掉了 kill 信号).
# 不杀的话, 我们用的是 GApplication, 新进程会被识别为已有实例的二次激活并立刻退出.
STALE_PIDS=$(pgrep -f 'doubao_input(\.|$| )' || true)
if [[ -n "$STALE_PIDS" ]]; then
    echo "[start.sh] killing stale instances: $STALE_PIDS" >&2
    kill $STALE_PIDS 2>/dev/null || true
    sleep 0.5
    kill -KILL $STALE_PIDS 2>/dev/null || true
fi

DEBUG_MODE=false
for arg in "$@"; do
    if [[ "$arg" == "--debug" ]]; then
        DEBUG_MODE=true
    fi
done

if $DEBUG_MODE; then
    exec "$VENV_DIR/bin/python" -u -m doubao_input
else
    nohup "$VENV_DIR/bin/python" -u -m doubao_input > "$LOG_FILE" 2>&1 &
    PID=$!
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "❌ 启动失败，查看日志：$LOG_FILE"
        tail -20 "$LOG_FILE"
        exit 1
    fi
    echo "✅ 已启动（PID $PID），日志：$LOG_FILE"
fi
