#!/bin/bash
# ============================================================
# 《羊了个羊》GUI Agent · 双击运行（macOS）
# ============================================================
#
# 用法：
#   1) 在 Finder 里双击本文件（首次若被拦，右键 → 打开）
#   2) 或在终端执行：bash run.command
#
# 指定解释器：
#   PYTHON=/path/to/python bash run.command

cd "$(dirname "$0")" || exit 1

echo "==== 《羊了个羊》GUI Agent · 启动器 ===="
echo "工作目录：$(pwd)"
echo

# Agent 运行必需的第三方库：只挑「全都齐」的解释器来跑
DEPS_PROBE='import importlib.util as u,sys
miss=[m for m in ("tkinter","openai","pyautogui","PIL","Quartz") if u.find_spec(m) is None]
sys.exit(1 if miss else 0)'

# 只要求能开窗口（tkinter）；缺运行库时让 run.py 去给安装提示
TK_PROBE='import tkinter'

CANDIDATES=""
if [ -n "$PYTHON" ] && [ -x "$PYTHON" ]; then
    CANDIDATES="$PYTHON"
else
    # 依次尝试：项目内虚拟环境 → conda 环境 → 系统解释器。
    # conda 环境通常装好了全部依赖，排在系统 Python 前面。
    CANDIDATES="./.venv/bin/python ./venv/bin/python"
    for env_dir in "$HOME"/miniconda3/envs/*/bin/python \
                   "$HOME"/anaconda3/envs/*/bin/python \
                   "$HOME"/miniforge3/envs/*/bin/python; do
        [ -x "$env_dir" ] && CANDIDATES="$CANDIDATES $env_dir"
    done
    CANDIDATES="$CANDIDATES python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"
fi

PY=""
PY_FALLBACK=""
for c in $CANDIDATES; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "$TK_PROBE" >/dev/null 2>&1; then
            if "$c" -c "$DEPS_PROBE" >/dev/null 2>&1; then
                # 依赖全齐：直接用它，不再找别的
                PY="$c"
                break
            fi
            # 只能开窗口、缺运行库：先记下来当备选，
            # 万一后面没有更好的，就用它开窗口给安装提示
            [ -z "$PY_FALLBACK" ] && PY_FALLBACK="$c"
        fi
    fi
done

if [ -z "$PY" ]; then
    PY="$PY_FALLBACK"
fi

if [ -z "$PY" ]; then
    echo "❌ 没找到带 tkinter 的 Python 3，无法打开配置窗口。"
    echo
    echo "   补救办法（任选其一）："
    echo "     - 安装 Python 3.11 后重跑本文件"
    echo "     - 指定解释器：PYTHON=/path/to/python bash run.command"
    echo "     - 不开窗口直接跑：python3 run.py --no-gui"
    echo
    read -r -p "按回车键关闭此窗口…" _
    exit 1
fi

echo "使用解释器：$PY"
"$PY" -c "$DEPS_PROBE" >/dev/null 2>&1 || {
    echo
    echo "⚠️ 这个 Python 缺少运行库（openai / pyautogui / Pillow / pyobjc），"
    echo "   窗口里会给出安装命令；装好后重新打开本文件即可。"
    echo
    echo "   更省事的办法：建一个环境并装好依赖，再双击本文件——"
    echo "     conda create -n game_agent python=3.11 -y"
    echo "     conda run -n game_agent pip install -r requirements.txt"
}
echo

"$PY" run.py
RC=$?

echo
if [ "$RC" -ne 0 ]; then
    echo "⚠️ 程序退出码：$RC"
fi

read -r -p "按回车键关闭此窗口…" _
exit "$RC"
