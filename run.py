#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《羊了个羊》GUI Agent · 一键启动器

双击 run.command（或在终端执行 python run.py）会打开一个配置窗口：
填好「模型服务地址 / 模型 ID / API Key」，点「开始运行」即可。

填过的内容会记在同目录的 config.json，下次打开窗口自动带出来。
config.json 已在 .gitignore 里（里面可能有你的 API Key，不要提交到仓库）。

不开窗口、直接用已保存配置启动：
    python run.py --no-gui
"""

import argparse
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox

    TK_AVAILABLE = True
except Exception:                                    # pragma: no cover
    TK_AVAILABLE = False


# ============================================================
# 1. 路径与默认值
# ============================================================

HERE = Path(__file__).resolve().parent
AGENT_PATH = HERE / "Game-agent-fast.py"
CONFIG_PATH = HERE / "config.json"

DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "qwen3-vl-30b-a3b-instruct-mlx@5bit",
    "api_key": "lm-studio",
}

# 运行 Agent 必需的第三方库（缺哪个就在窗口里提示装哪个）
REQUIRED_MODULES = {
    "pyautogui": "pyautogui",
    "PIL": "Pillow",
    "openai": "openai",
    "Quartz": "pyobjc-framework-Quartz",
}


# ============================================================
# 2. 配置读写
# ============================================================

def load_config():
    """读取 config.json；不存在或坏掉就返回默认值。"""

    cfg = dict(DEFAULT_CONFIG)

    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

            if isinstance(loaded, dict):
                for key in DEFAULT_CONFIG.keys():
                    value = loaded.get(key)

                    if isinstance(value, str) and value.strip():
                        cfg[key] = value.strip()
    except Exception as exc:
        print(f"[启动器] 读取 config.json 失败，使用默认值：{exc}")

    return cfg


def save_config(cfg):
    """把配置写回 config.json（里面有 Key，所以设成仅本人可读）。"""

    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(CONFIG_PATH, 0o600)

        return True
    except Exception as exc:
        print(f"[启动器] 保存 config.json 失败：{exc}")

        return False


def normalize_base_url(url):
    """
    把用户随手填的地址整理成 OpenAI 客户端能用的形式：
      127.0.0.1:1234      -> http://127.0.0.1:1234/v1
      http://host/v1/     -> http://host/v1
      https://host/openai/v1 -> 原样保留
    """

    url = (url or "").strip()

    if not url:
        return url

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url

    url = url.rstrip("/")

    # 已经有了版本段（/v1、/v1beta…）就不再补
    if not re.search(r"/v\d", url, re.IGNORECASE):
        url += "/v1"

    return url


def missing_dependencies():
    """返回缺失的依赖列表 [(import 名, pip 包名), ...]。"""

    missing = []

    for module_name, package_name in REQUIRED_MODULES.items():
        try:
            found = importlib.util.find_spec(module_name) is not None
        except Exception:
            found = False

        if not found:
            missing.append((module_name, package_name))

    return missing


# ============================================================
# 3. 模型服务
# ============================================================

def make_client(base_url, api_key, timeout):
    """按 OpenAI 兼容协议建一个客户端。"""

    try:
        from openai import OpenAI
    except ModuleNotFoundError:
        raise RuntimeError(
            "当前 Python 没装 openai 库。"
            "请在终端执行：pip install -r requirements.txt，"
            "然后换一个装好依赖的 Python 重新打开本启动器。"
        )

    return OpenAI(
        base_url=base_url,
        api_key=api_key or "lm-studio",
        timeout=timeout,
        max_retries=0,
    )


def _local_service_hint(url):
    if "127.0.0.1" in url or "localhost" in url:
        return "（本地地址：请确认本地模型服务已启动，并已打开 OpenAI 兼容接口）"

    return ""


def _ids_from_models(response):
    ids = []

    for item in getattr(response, "data", None) or []:
        model_id = getattr(item, "id", None)

        if model_id is None and isinstance(item, dict):
            model_id = item.get("id")

        if model_id:
            ids.append(str(model_id))

    return ids


def test_connection(cfg, timeout=15):
    """
    探测模型服务。返回 (ok: bool, message: str)。

    ok=False 表示"现在这样启动会失败"，窗口里会提示先改正。
    """

    base_url = cfg["base_url"]
    model = cfg["model"]

    try:
        client = make_client(base_url, cfg["api_key"], timeout)
    except Exception as exc:
        return False, f"无法创建客户端：{type(exc).__name__}: {exc}"

    # 先走 /models（瞬时返回，不加载模型）
    try:
        ids = _ids_from_models(client.models.list())
    except Exception as first_exc:
        # 有些服务不实现 /models，那就发一次最小对话来探测
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )

            return True, f"连接成功，模型「{model}」有响应。"
        except Exception as exc:
            hint = _local_service_hint(base_url)

            return False, (
                f"连接失败：{type(exc).__name__}: {exc} {hint}"
            )

    if not ids:
        return True, "连接成功（服务没有返回模型列表，无法核对模型 ID）。"

    if model in ids:
        return True, f"连接成功，模型「{model}」已就绪。"

    shown = "、".join(ids[:8]) + ("…" if len(ids) > 8 else "")

    return False, (
        f"连接成功，但服务里没有模型「{model}」。"
        f"当前可用：{shown}。请把「模型 ID」改成上面列出的其中一个。"
    )


# ============================================================
# 4. 加载 Agent 并套用配置
# ============================================================

def load_agent_module():
    """
    加载 Game-agent-fast.py。

    它结尾是 if __name__ == "__main__": main()，
    所以这里 import 进来不会自己跑起来，配置覆盖完再手动调 main()。
    """

    if not AGENT_PATH.exists():
        raise FileNotFoundError(f"找不到 Agent 文件：{AGENT_PATH}")

    spec = importlib.util.spec_from_file_location(
        "game_agent_agent", AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)

    sys.modules["game_agent_agent"] = module
    spec.loader.exec_module(module)

    return module


def apply_config(module, cfg):
    """
    把窗口里填的参数套到 Agent 上。

    Agent 的 BASE_URL / MODEL / client 都是模块级变量，
    直接覆盖即可——不用改它的源码。
    """

    module.BASE_URL = cfg["base_url"]
    module.MODEL = cfg["model"]
    module.client = make_client(
        cfg["base_url"],
        cfg["api_key"],
        getattr(module, "REQUEST_TIMEOUT", 120),
    )


def launch_agent(cfg):
    """加载 Agent、套用配置、开跑。返回进程退出码。"""

    print("=" * 55)
    print("羊了个羊 GUI Agent · 启动器")
    print(f"服务地址：{cfg['base_url']}")
    print(f"模型 ID ：{cfg['model']}")
    print("=" * 55)

    try:
        module = load_agent_module()
    except Exception as exc:
        print(f"\n[启动器] 无法加载 Agent：{type(exc).__name__}: {exc}")

        return 1

    try:
        apply_config(module, cfg)
    except Exception as exc:
        print(f"\n[启动器] 配置无效：{type(exc).__name__}: {exc}")

        return 1

    try:
        module.main()
    except SystemExit as exc:
        code = exc.code

        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except KeyboardInterrupt:
        print("\n[启动器] 已中断")

        return 130

    return 0


# ============================================================
# 5. 配置窗口
# ============================================================

class LauncherApp:
    """收集配置的小窗口。关掉窗口 = 不启动。"""

    def __init__(self, cfg, missing):
        self.start_cfg = None
        self._missing = missing
        self._queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("羊了个羊 GUI Agent · 启动器")
        self.root.resizable(False, False)

        self.var_url = tk.StringVar(value=cfg["base_url"])
        self.var_model = tk.StringVar(value=cfg["model"])
        self.var_key = tk.StringVar(value=cfg["api_key"])
        self.var_status = tk.StringVar(value="")

        self._build()

    # ---------- 界面 ----------

    def _build(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.grid(row=0, column=0, sticky="nsew")

        ttk.Label(outer, text="模型服务地址").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.entry_url = ttk.Entry(outer, textvariable=self.var_url, width=44)
        self.entry_url.grid(row=0, column=1, sticky="we", pady=(0, 8), padx=(10, 0))

        ttk.Label(outer, text="模型 ID").grid(
            row=1, column=0, sticky="w", pady=(0, 8)
        )
        self.entry_model = ttk.Entry(
            outer, textvariable=self.var_model, width=44
        )
        self.entry_model.grid(row=1, column=1, sticky="we", pady=(0, 8), padx=(10, 0))

        ttk.Label(outer, text="API Key").grid(row=2, column=0, sticky="w")
        self.entry_key = ttk.Entry(outer, textvariable=self.var_key, width=44)
        self.entry_key.grid(row=2, column=1, sticky="we", padx=(10, 0))

        ttk.Label(
            outer,
            text="本地服务不需要真的 Key，随便填即可；用云端服务请填自己的 Key。",
            foreground="#6b6b6b",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, columnspan=2, sticky="we", pady=(14, 0))
        self.btn_test = ttk.Button(
            buttons, text="测试连接", command=self.on_test
        )
        self.btn_test.pack(side="left")
        self.btn_start = ttk.Button(
            buttons, text="开始运行", command=self.on_start
        )
        self.btn_start.pack(side="right")

        ttk.Separator(outer, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="we", pady=14
        )

        ttk.Label(
            outer,
            textvariable=self.var_status,
            wraplength=430,
            justify="left",
            foreground="#1a6b3c",
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        ttk.Label(
            outer,
            text=(
                "点「开始运行」后本窗口关闭，Agent 接管鼠标开始操作。\n"
                "紧急停止：把鼠标移到屏幕左上角，或按 Ctrl+C。"
            ),
            wraplength=430,
            justify="left",
            foreground="#8a5a00",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))

        if self._missing:
            names = "、".join(package for _, package in self._missing)
            self.btn_test.state(["disabled"])
            self.btn_start.state(["disabled"])
            self.var_status.set(
                f"缺少依赖：{names}。\n"
                f"先在终端执行：pip install -r requirements.txt\n"
                f"（或 pip install {names}），"
                f"再用装好依赖的 Python 重新打开本窗口。"
            )
        else:
            self.var_status.set("填写上面的信息，建议先点「测试连接」确认可用。")

        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(
            f"+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 3)}"
        )

        self.root.lift()
        self.root.focus_force()
        self.root.after(120, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 交互 ----------

    def on_close(self):
        self.start_cfg = None
        self.root.quit()

    def _collect(self):
        """读窗口里的值；不合法就弹提示并返回 None。"""

        base_url = normalize_base_url(self.var_url.get())
        model = self.var_model.get().strip()
        api_key = self.var_key.get().strip() or "lm-studio"

        if not base_url:
            messagebox.showerror("还差一项", "请填写模型服务地址。")

            return None

        if not model:
            messagebox.showerror("还差一项", "请填写模型 ID。")

            return None

        # 把整理后的地址回填到输入框，让用户看得见被改成了什么
        self.var_url.set(base_url)

        return {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
        }

    def _set_busy(self, busy):
        state = ["disabled"] if busy else ["!disabled"]

        # 缺依赖时两个按钮都不能点：
        # 「开始运行」跑不起来，「测试连接」也只会报 import 错误
        if self._missing:
            self.btn_test.state(["disabled"])
            self.btn_start.state(["disabled"])

            return

        self.btn_test.state(state)
        self.btn_start.state(state)

    def on_test(self):
        cfg = self._collect()

        if cfg is None:
            return

        self.var_status.set("正在连接模型服务…")
        self._set_busy(True)

        threading.Thread(
            target=self._test_worker, args=(cfg,), daemon=True
        ).start()

    def _test_worker(self, cfg):
        try:
            ok, message = test_connection(cfg)
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"

        self._queue.put((ok, message))

    def _drain_queue(self):
        """在 Tk 主线程里更新界面（跨线程直接改控件不安全）。"""

        try:
            while True:
                ok, message = self._queue.get_nowait()
                self.var_status.set(("✅ " if ok else "⚠️ ") + message)
                self._set_busy(False)
        except queue.Empty:
            pass

        self.root.after(120, self._drain_queue)

    def on_start(self):
        cfg = self._collect()

        if cfg is None:
            return

        if self._missing:
            names = "、".join(package for _, package in self._missing)
            messagebox.showerror(
                "缺少依赖",
                f"还没安装：{names}\n\n"
                "请在终端执行：\npip install -r requirements.txt",
            )

            return

        save_config(cfg)
        self.start_cfg = cfg
        self.root.quit()

    # ---------- 生命周期 ----------

    def run(self):
        self.root.mainloop()
        self.root.destroy()


def run_gui(cfg):
    """打开配置窗口，返回用户确认的配置（关窗口则返回 None）。"""

    app = LauncherApp(cfg, missing_dependencies())
    app.run()

    return app.start_cfg


# ============================================================
# 6. 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="《羊了个羊》GUI Agent 一键启动器"
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="不开配置窗口，直接用 config.json（或默认值）启动",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.no_gui:
        return launch_agent(cfg)

    if not TK_AVAILABLE:
        print(
            "[启动器] 当前 Python 没有 tkinter，无法打开配置窗口。\n"
            "         可用 --no-gui 直接启动，或换一个带 tkinter 的 Python 3。"
        )

        return 1

    cfg = run_gui(cfg)

    if cfg is None:
        print("[启动器] 已取消，未启动 Agent。")

        return 0

    # 窗口销毁后稍等一下，确保它真的从屏幕上消失，
    # 否则它会被截进游戏画面里
    time.sleep(0.4)

    return launch_agent(cfg)


if __name__ == "__main__":
    sys.exit(main())
