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

if [ -n "$PYTHON" ] && [ -x "$PYTHON" ]; then
    CANDIDATES="$PYTHON"
else
    CANDIDATES="./.venv/bin/python ./venv/bin/python python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"
fi

PY=""
for c in $CANDIDATES; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
        # 需要 tkinter 才能打开配置窗口
        if "$c" -c "import tkinter" >/dev/null 2>&1; then
            PY="$c"
            break
        fi
    fi
done

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
echo

"$PY" run.py
RC=$?

echo
if [ "$RC" -ne 0 ]; then
    echo "⚠️ 程序退出码：$RC"
fi

read -r -p "按回车键关闭此窗口…" _
exit "$RC"
