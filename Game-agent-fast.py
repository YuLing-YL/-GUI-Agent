import os
import re
import sys
import json
import base64
import shutil
import tempfile
import subprocess
import gc
import time
from pathlib import Path

import pyautogui
from openai import OpenAI


# ============================================================
# 1. 基础配置
# ============================================================

BASE_URL = "http://127.0.0.1:1234/v1"
MODEL = "qwen3-vl-30b-a3b-instruct-mlx@5bit"

# 单次模型请求的超时时间
REQUEST_TIMEOUT = 120

# 【修改点·需求4】固定 Qwen3-VL 推理参数
# 这三项写死为常量，每次请求都显式传入，避免运行中漂移。
#   temperature=0.25：比原来的 0.1 略高，让"预判式思考"有少量多样性；
#   top_p=0.7：收紧候选词分布，保证坐标数字不会乱飘；
#   max_tokens=700：【截断修复】原为 350，模型偶发"思考"啰嗦时
#     会在写出"点击坐标：x,y"之前就把额度用尽，导致坐标行整行丢失、
#     本步被误判为格式错误。新 System Prompt 已要求"思考"只写简短
#     预判，700 足以容纳一行思考 + 坐标行，仍是有界输出。
MODEL_TEMPERATURE = 0.25
MODEL_TOP_P = 0.7
MODEL_MAX_TOKENS = 700

# 鼠标操作安全设置：鼠标移动到屏幕左上角可触发紧急停止
pyautogui.FAILSAFE = True

# 鼠标点击后的等待时间
CLICK_DELAY = 0.3

# 是否要求用户确认后才点击
REQUIRE_CONFIRMATION = False

# 是否开启遮挡检查（强烈建议开启）
# 该游戏里被上层压住的砖块会露出一小截图案，但无法点击。
# 开启后会用像素规则验证点击点是否位于完整卡面上，
# 不在完整卡面上的一律拒绝点击。
CHECK_OCCLUSION = True

# 是否把本地检测出的"可点击砖块中心坐标"告诉模型
# 开启后模型只能从这些坐标里选，能从根本上避免选到被压住的砖块
CLICKABLE_HINT_ENABLED = True

# 一轮内最多询问模型的次数
# 模型选到不可点击位置时，会带上失败原因立刻重新询问
RECLICK_ATTEMPTS = 3

# 截图 JPEG 质量
IMAGE_QUALITY = 85

# 【性能优化 A1】发给模型的图片缩放比例
# 1.0 = 原尺寸；0.5 = 长宽各缩一半，像素数降到 1/4。
# 模型处理图片的时间与像素数成正比，实测单轮请求
# 由约 6.0 秒降到约 3.4 秒。
# 注意：只影响发给模型的那张图。本地遮挡检查用的
# debug_last.png 仍保存原尺寸，模型坐标会按同一比例
# 换算回来，坐标清单也会按同一比例缩给模型，
# 保证模型看到的数字和图片完全一致。
MODEL_IMAGE_SCALE = 0.5

# 是否保存坐标调试图
# True 时会在项目目录生成：
#   debug_last.png  —— 本轮的原尺寸分析图（同时也是发给模型的底图）
#   debug_click.png —— 在截图上标出实际点击位置，用于核对坐标映射
DEBUG_ENABLED = True

# 是否只把游戏窗口区域发给模型（强烈建议开启）
# 游戏窗口通常只占屏幕一小部分，全屏截图会让砖块太小，
# 模型无法准确定位。开启后自动裁剪出游戏窗口。
CROP_TO_GAME_WINDOW = True

# 游戏窗口标题关键词
GAME_WINDOW_KEYWORD = "羊了个羊"

PROJECT_DIR = Path(__file__).resolve().parent
DEBUG_LAST_PATH = PROJECT_DIR / "debug_last.png"
DEBUG_CLICK_PATH = PROJECT_DIR / "debug_click.png"
LEVEL_END_PATH = PROJECT_DIR / "level_end.png"
LOG_PATH = PROJECT_DIR / "agent_log.txt"

# ============================================================
# 关卡结束自动停止
# ============================================================
#
# 通关（或本关失败）后游戏会弹出结算界面，此时继续点击
# 只会在弹窗上乱点。开启后按下面的规则自动停止程序：
#   - 连续 LEVEL_END_CONFIRM_ROUNDS 轮都判定本关已结束
#   - 判定依据（任一满足，且需连续确认）：
#       a) 棋盘上检测不到任何可点击砖块（通关后棋盘清空）
#       b) 草地占比低于 GRASS_RATIO_MIN（弹窗盖住了画面）
#   - 确认结束的那一轮不会点击，避免误触弹窗按钮
STOP_AFTER_LEVEL_END = True

# 需要连续确认多少轮才判定结束（防止动画瞬时状态误判）
LEVEL_END_CONFIRM_ROUNDS = 3

# 草地占比阈值：正常对局实测约 90%，
# 被弹窗覆盖时会骤降，取 50% 留足余量
GRASS_RATIO_MIN = 0.5

# ============================================================
# 本局结束后交给独立脚本开启下一局（连续采集多局）
# ============================================================
#
# 【职责拆分】本 Agent 只负责**玩游戏**：看棋盘、选一块砖、点下去。
# 一局结束之后"找按钮重新开始"是另一件事，已拆到独立脚本：
#
#     restart_game.py —— 找到「开始下一局」的按钮 -> 点击 -> 验证新局
#
# 它先做本地像素识别（复活弹窗红字 / 结算页白按钮，不烧模型调用），
# 认不出来再截图问模型；点完自己验证新一局是否真的开出来，
# 然后用退出码把结果告诉本 Agent：
#     0 = 已成功开启新一局
#     2 = 推进不了（需要人工处理）
#
# 本 Agent 只做两件事：判定本局结束时以子进程方式运行它，
# 再按它的退出码决定"继续采集下一局"还是"停机等人工处理"。
AUTO_RESTART_ON_FAIL = True

# 最多自动采集多少局：0 = 不限，一直采到手动 Ctrl+C；
# 设成比如 30 就是采满 30 局自动停（推荐，方便统计采集进度）
RESTART_MAX_EPISODES = 0

# 【配置位置提醒】下面这些参数现在都在 restart_game.py 里，
# 本 Agent 已经不再使用，需要调就改那个脚本：
#   RESTART_POPUP_WAIT     每次点击/验证之间的等待秒数
#   ADVANCE_VERIFY_POLLS   验证"新局是否加载出来"的轮询次数
#   NEW_ROUND_GRASS_MIN    判定新局已开始的草地占比门槛
#   ADVANCE_MAX_TRIES      一次推进最多尝试几轮
#   ADVANCE_MODEL_MAX_ASKS 一次推进最多问模型几次
#   MODEL_NO_BUTTON_LIMIT  模型几次说"没按钮"就收手
#   LOCAL_MISS_LIMIT       本地按钮几次点不中就改问模型

# 重开脚本的文件名与退出码约定
# （debug_restart.png 等留档文件由 restart_game.py 自己负责）
RESTART_SCRIPT_NAME = "restart_game.py"
RESTART_EXIT_CANNOT_ADVANCE = 2     # 脚本判定"推进不了"，需要人工处理

# ============================================================
# 禁止使用道具 + 卡槽填满时主动结束本局
# ============================================================
#
# 规则（用户指定）：
#   1. 本 Agent **不允许使用道具**（移出 / 撤回 / 洗牌）：
#      - 卡槽下方的道具区一律拒绝点击；
#      - 提示词里也明确告诉模型不要点道具。
#   2. 卡槽只剩最后一个空位、且棋盘上没有"点了能三连/凑对"的砖块时，
#      不再无限等待（以前等的是"人工用道具腾位置"，现在不允许用道具了），
#      直接**主动点一块砖把卡槽填满**，让游戏弹出失败结算，
#      再由自动推进流程开下一把。
#      之所以必须点：游戏只有"点击填满卡槽"才会弹失败结算，
#      不点的话画面停在残局、根本没有"重新挑战"按钮可点。
FORBID_PROPS = True

# 单局内最多"主动结束"几次（防止点击没生效时反复空转）
FORCED_END_MAX_PER_ROUND = 3

# 【空转熔断】卡槽将满又推进不了时，最多连续干等多少轮就停机。
# 正常情况下主动结束一次就会填满卡槽、弹出失败结算；
# 连续等这么多轮还停在死局，说明点击根本没生效
# （窗口被挪动 / 有未知遮挡 / 预算被耗尽），再等只是烧时间。
# 按 ~4 秒/轮算，30 轮约 2 分钟。停机前轨迹会由退出兜底抢救保存。
STALL_STOP_ROUNDS = 30

# ============================================================
# 认不出的界面：交给重开脚本处理
# ============================================================
#
# 连续多轮既读不到棋盘砖、也读不到卡槽，且不是通关/失败弹窗时，
# 判定为"程序不认识的界面"（活动弹窗、次数用尽、改版结算页、
# 外部遮挡等）。本 Agent 不再自己想办法，直接调用 restart_game.py，
# 由它截图、找按钮、问模型、验证新局。
UNKNOWN_SCREEN_ROUNDS = 5       # 连续多少轮读不到砖块就求助重开脚本
UNKNOWN_SCREEN_MAX_ASKS = 5     # 单局最多求助几次，避免死循环

# 画面指纹、提示词、留档路径（debug_unknown.png）等实现细节
# 全部在 restart_game.py 里，本 Agent 只负责把它叫起来。

# 【已移除】死局自动停止（STUCK_STOP_ROUNDS）：
# 本地判定"无路可走"曾两次误停掉还能继续的局（可见副本/峰值前瞻都不可靠）。
# 现在失败与否只由游戏结算弹窗判定；卡槽将满时先由主循环尝试"主动结束"，
# 实在推进不了才在 STALL_STOP_ROUNDS 轮后熔断停机（见 FORCED_END_MAX_PER_ROUND
# 下方的说明），不会无限干等。

# 最近一次成功裁剪的游戏窗口区域（逻辑坐标）
# None 表示没有裁剪，使用的是全屏截图
LAST_WINDOW_RECT = None

# 最近一轮检测到的可点击砖块数量
LAST_CLICKABLE_COUNT = 0

# 最近一轮检测到的卡槽砖块数量（用于区分通关和失败）
LAST_TRAY_COUNT = 0

# 最近一轮消除优先级分组的结果（均为原尺寸分析图坐标）：
# LAST_PRIORITY_TOP：当前最高优先级非空组的坐标集合
# LAST_PRIORITY_ORDER：同一组坐标的有序列表（本地已按优先级排好），
#                      供模型连续不配合时"本地兜底"按序挑选
# LAST_MUST_WAIT：卡槽将满且没有可消除选择时为 True，此时必须等待
LAST_PRIORITY_TOP = set()
LAST_PRIORITY_ORDER = []

# 【卡槽紧张·强制消除】本轮"点下去立即三连消除"的砖块坐标集合
# （原尺寸分析图坐标）。卡槽紧张时 check_priority_choice 用它做强制校验。
LAST_ELIMINATE = set()
LAST_MUST_WAIT = False

# 必须等待时的具体原因（写日志 / 反馈给模型 / 失败归因都用它）
LAST_WAIT_REASON = ""

# 【卡槽槽位硬保护】本轮"点完之后卡槽的格数"信息：
#   LAST_CLICK_KIND：{(x, y): "eliminate"/"pair"/"new"}
#     eliminate = 卡槽已有 2 个同款，点完立即三连，净减 2 格
#     pair      = 卡槽已有 1 个同款，点完凑成一对，净加 1 格
#     new       = 全新图案，净加 1 格
#   LAST_TRAY_SAFE   ：本轮"点了不会撑爆卡槽"的坐标集合
#   LAST_TRAY_DANGER ：本轮"点了会把卡槽填满且不消除"的坐标集合（禁止）
LAST_CLICK_KIND = {}
LAST_TRAY_SAFE = set()
LAST_TRAY_DANGER = set()

# 本轮每块棋盘砖的包围盒 + 点击类型：
#   [(min_x, min_y, max_x, max_y, "eliminate"/"pair"/"new"), ...]
# 用来兜住"模型给出的坐标落在这块砖的卡面里、但不在正中心"的情况——
# 只按中心做容差匹配会漏判，导致把危险砖当成安全砖放过去。
LAST_CLICK_BBOX = []

# 【DPO 格式改造】本轮候选清单快照（写入轨迹 JSON 的 candidates 字段）：
#   [{"analysis_xy": [ax, ay],   # 分析图（原尺寸）坐标
#     "model_xy": [mx, my],      # 模型看到的缩放图坐标
#     "group": 1/2/3,            # 1=立即三连 2=与卡槽凑对 3=全新图案
#     "kind": "eliminate"/"pair"/"new",
#     "tray_n": 卡槽内同图案数量,
#     "board_n": 棋盘上同图案可见数量,
#     "unlock": 解放分（正下方被压砖块露出面积）,
#     "safe": 是否允许选择}, ...]
LAST_CANDIDATES = []

# 上一次点击执行完之后卡槽的格数（估算）。
# 一旦 >= TRAY_CAPACITY，说明刚把卡槽填满且没消除，本关必然失败，
# 这条证据用来把"卡槽满导致失败"和"全部消除通关"区分开。
LAST_TRAY_AFTER_CLICK = -1

# 【通关/失败归因】结算弹窗会把整屏压暗，那一帧卡槽和棋盘都检测不到
# （实测弹窗帧读到的是 卡槽 0/7），所以不能拿弹窗帧做判定，
# 必须记住弹窗出现之前最后一帧"正常对局画面"的状态。
#   LAST_STABLE_VALID：是否已经记录过稳定状态
#   LAST_STABLE_TRAY ：稳定帧的卡槽占用格数
#   LAST_STABLE_BOARD：稳定帧的棋盘剩余可点击砖块数
LAST_STABLE_VALID = False
LAST_STABLE_TRAY = 0
LAST_STABLE_BOARD = 0

# 【防误判】本局是否出现过"正常对局画面"（棋盘或卡槽非空）。
# 用途：点完「重新挑战」后新局还在加载时棋盘是空的、卡槽 0/7，
# 若此时弹窗残留（淡出动画）会被误判成"棋盘清空=通关"；
# 程序刚启动时屏幕上残留上一局的失败弹窗同理。
# 只有真正玩过（见过非空棋盘/卡槽）才允许归因通关/失败。
LAST_SAW_GAMEPLAY = False

# 【禁止使用道具】卡槽下方"道具区"（移出/撤回/洗牌）的上边界 y 值
# （原尺寸分析图坐标，0 表示未知）。由 build_ranked_hint 每轮刷新：
# 取卡槽砖下沿再往下留一点余量。落在该 y 以下的点击一律拒绝。
LAST_TOOLBAR_TOP = 0

# 【主动结束本局】本局已经"主动点砖结束"过几次（防重复空转）
LAST_FORCED_END_COUNT = 0

# 最近一轮分析图的尺寸（未知界面兜底等场景要用它做坐标换算）
LAST_ANALYSIS_SIZE = None


# ============================================================
# 1.1 对局轨迹自动保存（离线 DPO / SFT 训练语料）
# ============================================================
#
# 【DPO 格式改造】原来的轨迹只存 model_output / think / click / result /
# tray_count，缺三样离线训练必需的东西：
#   1) 每一步的截图  -> DPO 是多模态偏好数据，必须能还原"同一张图"
#   2) 完整 prompt   -> 训练与推理必须用同一段提示词
#   3) 候选清单快照  -> 才能对"同一张图 + 同一 prompt"构造
#                       chosen（长期最优）/ rejected（贪心坏动作）
# 现在每步额外记录：frame / prompt / candidates / tray_before / tray_after，
# 顶层额外记录 system_prompt / schema_version / trace_id / stop_kind。
#
# 目录结构：
#   traces/trace_YYYYmmdd_HHMMSS.json      一局一份轨迹
#   traces/frames/<trace_id>/step_0001.jpg 每一步发给模型的原始帧
TRACE_ENABLED = True
TRACE_DIR = PROJECT_DIR / "traces"
TRACE_FRAMES_DIRNAME = "frames"

# 轨迹数据结构版本号：预处理脚本据此判断字段是否可用
TRACE_SCHEMA_VERSION = 2

# 本局轨迹 id（一局内所有帧共用一个目录）
TRACE_ID = ""
# 本局帧图目录
TRACE_FRAMES_DIR = None
# 本局所有步骤记录（每局开始时清空）
TRACE_STEPS = []
# 本局已记录到第几步
TRACE_STEP_NO = 0

# ---- 供 record_step 取用的"本轮快照"（每轮由 ask_ai 刷新）----
# 本轮发给模型的完整 user 文本
LAST_STEP_PROMPT = ""
# 本轮候选清单快照（build_ranked_hint 生成）
LAST_STEP_CANDIDATES = []
# 本轮帧图在 traces/ 下的相对路径
LAST_STEP_FRAME = None
# 本轮图片尺寸（分析图 / 模型图）
LAST_STEP_IMAGE_SIZE = None
LAST_STEP_MODEL_IMAGE_SIZE = None


def start_trace():
    """
    开始新一局：清空轨迹缓存并生成本局 trace_id 与帧目录。
    """

    global TRACE_STEPS
    global TRACE_STEP_NO
    global TRACE_ID
    global TRACE_FRAMES_DIR

    TRACE_STEPS = []
    TRACE_STEP_NO = 0
    TRACE_ID = time.strftime("%Y%m%d_%H%M%S")

    if TRACE_ENABLED:
        TRACE_FRAMES_DIR = (
            TRACE_DIR / TRACE_FRAMES_DIRNAME / TRACE_ID
        )
        try:
            TRACE_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"[警告] 创建帧目录失败：{exc}")
            TRACE_FRAMES_DIR = None


def save_step_frame(screenshot_path):
    """
    【DPO 格式改造】把本轮发给模型的临时截图复制到轨迹帧目录。

    临时截图在每个请求结束后就被删除，所以必须在这里留档，
    否则事后无法还原"当时的画面"，偏好数据也就无从构造。

    返回：相对于 TRACE_DIR 的路径字符串（posix 风格）；
          失败或未开启时返回 None。
    """

    if not TRACE_ENABLED or not screenshot_path:
        return None

    if TRACE_FRAMES_DIR is None:
        return None

    try:
        temp_path = Path(screenshot_path)

        if not temp_path.exists():
            return None

        TRACE_STEP_NO_PENDING = TRACE_STEP_NO + 1
        target_name = f"step_{TRACE_STEP_NO_PENDING:04d}.jpg"
        target_path = TRACE_FRAMES_DIR / target_name

        shutil.copyfile(temp_path, target_path)

        return (
            Path(TRACE_FRAMES_DIRNAME)
            / TRACE_ID
            / target_name
        ).as_posix()

    except Exception as exc:
        print(f"[警告] 保存轨迹帧失败：{exc}")
        return None


def record_step(
    model_output,
    think_text,
    click_coord,
    result,
    tray_count,
    attempt=1,
    feedback=None,
):
    """
    记录一局中的一步。

    参数：
        model_output：Qwen3-VL 本次输出的原始文本
        think_text  ：从输出里正则提取出的"思考"内容
        click_coord ：选择点击的坐标 [x, y]（分析图坐标）；
                      wait / 未点击时为 None
        result      ：操作结果（clicked / wait / blocked: 原因）
        tray_count  ：该步决策时的卡槽占用格数
        attempt     ：本轮是第几次询问模型（1 为首次）
        feedback    ：本次询问是否带了"上一步被拒"的反馈

    【DPO 格式改造】只有"首次询问且无反馈"的那一步才挂载
    frame / prompt / candidates —— 重试轮的画面对应的是带纠正提示的
    上下文，不能作为偏好数据里的"同一 prompt"样本。
    """

    global TRACE_STEP_NO
    global LAST_STEP_PROMPT
    global LAST_STEP_CANDIDATES
    global LAST_STEP_FRAME
    global LAST_STEP_IMAGE_SIZE
    global LAST_STEP_MODEL_IMAGE_SIZE

    if not TRACE_ENABLED:
        return

    TRACE_STEP_NO += 1

    is_clean_step = attempt == 1 and not feedback

    # 只有"真的点下去了"才有"点击后卡槽格数"，
    # 等待/被拒的步沿用上一步的旧值会污染训练标签，这里显式置空
    clicked_now = isinstance(result, str) and result.startswith(
        "clicked"
    )

    step_record = {
        "step": TRACE_STEP_NO,
        "model_output": model_output,
        "think": think_text,
        "click": click_coord,
        "result": result,
        "tray_count": tray_count,
        "tray_before": tray_count,
        "tray_after": (
            LAST_TRAY_AFTER_CLICK
            if clicked_now and LAST_TRAY_AFTER_CLICK >= 0
            else None
        ),
        "tray_capacity": TRAY_CAPACITY,
        "attempt": attempt,
        "feedback": feedback,
    }

    if is_clean_step:
        step_record.update(
            {
                "frame": LAST_STEP_FRAME,
                "image_size": LAST_STEP_IMAGE_SIZE,
                "model_image_size": LAST_STEP_MODEL_IMAGE_SIZE,
                "prompt": LAST_STEP_PROMPT,
                "candidates": LAST_STEP_CANDIDATES,
            }
        )
    else:
        step_record.update(
            {
                "frame": None,
                "image_size": None,
                "model_image_size": None,
                "prompt": None,
                "candidates": None,
            }
        )

    TRACE_STEPS.append(step_record)

    # 快照用完即清，避免下一步误用上一帧的数据
    LAST_STEP_PROMPT = ""
    LAST_STEP_CANDIDATES = []
    LAST_STEP_FRAME = None


def save_trace(cleared, fail_reason, stop_kind=None):
    """
    把本局完整轨迹写成 JSON 文件（离线训练语料）。

    返回写出的文件路径；失败或未开启时返回 None。
    """

    if not TRACE_ENABLED:
        return None

    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)

        trace_id = TRACE_ID or time.strftime("%Y%m%d_%H%M%S")
        path = TRACE_DIR / f"trace_{trace_id}.json"

        # 【DPO 格式改造】顶层补齐训练侧必需字段：
        #   system_prompt  -> 训练时用完全相同的系统提示词
        #   schema_version -> 预处理脚本据此校验字段完整性
        #   frames_dir     -> 帧图目录（相对 traces/）
        #   stop_kind      -> cleared / failed / covered ...
        payload = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": trace_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": MODEL,
            "system_prompt": SYSTEM_PROMPT,
            "infer_params": {
                "temperature": MODEL_TEMPERATURE,
                "top_p": MODEL_TOP_P,
                "max_tokens": MODEL_MAX_TOKENS,
            },
            "tray_capacity": TRAY_CAPACITY,
            "image_scale": MODEL_IMAGE_SCALE,
            "frames_dir": (
                f"{TRACE_FRAMES_DIRNAME}/{trace_id}"
            ),
            "stop_kind": stop_kind,
            "total_steps": len(TRACE_STEPS),
            "cleared": bool(cleared),
            "fail_reason": fail_reason,
            "steps": TRACE_STEPS,
        }

        with open(path, "w", encoding="utf-8") as trace_file:
            json.dump(payload, trace_file, ensure_ascii=False, indent=2)

        print(f"[轨迹] 本局轨迹已保存：{path}")
        return path

    except Exception as exc:
        print(f"[警告] 保存对局轨迹失败：{exc}")
        return None


def reset_game_context():
    """
    【修改点·需求5】一局结束后清空对话上下文与轨迹缓存。

    说明：ask_ai 每次请求都新建 messages 列表、不携带任何历史消息，
    所以"对话上下文"本身不会跨轮累积；这里再把轨迹缓存释放掉，
    保证多局串行采样时内存不会持续上涨。
    """

    global TRACE_STEPS
    global TRACE_STEP_NO
    global TRACE_ID
    global TRACE_FRAMES_DIR
    global LAST_STEP_PROMPT
    global LAST_STEP_CANDIDATES
    global LAST_STEP_FRAME
    global LAST_STEP_IMAGE_SIZE
    global LAST_STEP_MODEL_IMAGE_SIZE

    TRACE_STEPS = []
    TRACE_STEP_NO = 0
    TRACE_ID = ""
    TRACE_FRAMES_DIR = None
    LAST_STEP_PROMPT = ""
    LAST_STEP_CANDIDATES = []
    LAST_STEP_FRAME = None
    LAST_STEP_IMAGE_SIZE = None
    LAST_STEP_MODEL_IMAGE_SIZE = None

    gc.collect()


# ============================================================
# 1.2 退出兜底：抢救"被中断的那一局"
# ============================================================
#
# 轨迹 JSON 只在 finish_level() 里落盘，于是采集期有个漏洞：
# 按 Ctrl+C 收工 / 崩溃 / 鼠标撞左上角触发安全机制时，main 直接跳出循环，
# 当前这一局的 JSON 不会写出 —— 但每一步的帧图早就复制进
# traces/frames/<trace_id>/ 了，结果是"有图没 JSON"的孤儿帧，
# trace_to_dataset 会整份跳过，整局采集白费。
#
# 所以 main 退出前统一收尾：
#   - 本局有完整步   -> 按"被中断"落盘一份可用轨迹（stop_kind=interrupted）
#   - 本局一步都没有 -> 只剩一个空帧目录，清掉它别留垃圾
#
# 注意：interrupted 局 cleared=False，SFT 默认不吃（要加
# --include-uncleared-sft），但 DPO 不要求通关，照样能用。
INTERRUPT_TRACE_REASON = "采集被中断（Ctrl+C / 异常退出），本局未打完"
INTERRUPT_STOP_KIND = "interrupted"


def cleanup_unsaved_frame_dir():
    """
    清理本局留下的、没有任何可用步的帧目录。

    只删"完全空的"目录；里面还有帧时就**不删**，只打印提示：
    帧是先复制、后记录的，极端情况下会剩下一两张配不上模型输出的帧，
    宁可留着交给人工判断，也不替用户丢数据。
    """

    if TRACE_FRAMES_DIR is None or not TRACE_FRAMES_DIR.exists():
        return

    try:
        leftovers = sorted(TRACE_FRAMES_DIR.iterdir())

        if not leftovers:
            TRACE_FRAMES_DIR.rmdir()
            print(f"[轨迹] 已清理空帧目录：{TRACE_FRAMES_DIR}")
            return

        print(
            f"[轨迹] 本局没有可训练步，保留帧目录"
            f"（{len(leftovers)} 张未入集帧）：{TRACE_FRAMES_DIR}"
        )

    except Exception as exc:
        print(f"[警告] 清理帧目录失败：{exc}")


def finalize_interrupted_episode():
    """
    退出前的收尾兜底：把当前局的轨迹抢救下来，别让整局采集白费。

    任何退出路径都可以安全重复调用（atexit 里也挂了它）：
    正常打完的局已经在 finish_level 里存过并 reset 过，这里会因为
    TRACE_STEPS 为空而直接空转，不会写出第二份同名 JSON。

    返回 True 表示确实抢救了一局，False 表示没有可抢救的内容。
    """

    if not TRACE_STEPS:
        cleanup_unsaved_frame_dir()
        return False

    step_count = len(TRACE_STEPS)

    print(
        f"\n[轨迹] 本局未正常结束（已记录 {step_count} 步），"
        "退出前先抢救保存"
    )
    append_log(f"轨迹 | 中途退出，抢救保存本局 {step_count} 步")

    path = save_trace(
        cleared=False,
        fail_reason=INTERRUPT_TRACE_REASON,
        stop_kind=INTERRUPT_STOP_KIND,
    )

    # 存完即清空：保证重复调用（main 收尾 + atexit）不会重复落盘
    reset_game_context()

    return path is not None


# ============================================================
# 2. 初始化 LM Studio 客户端
# ============================================================

client = OpenAI(
    base_url=BASE_URL,
    api_key="lm-studio",
    timeout=REQUEST_TIMEOUT,
    max_retries=0,
)


# ============================================================
# 3. 系统提示词
# ============================================================
# 【修改点·本轮】System Prompt 替换为用户指定版本。
# 与上一版的差异：
#   - 决策规则改为用户给出的 4 条（首要目标 / 风险规避 / 槽位管控 /
#     四步决策思考流程），措辞与优先级顺序完全按用户给的来；
#   - 明确"不要输出多个候选方案"，从源头压掉啰嗦输出（配合截断修复）；
#   - "思考"要求写成"操作收益、底层通路风险、槽位变化"三要素。
# 说明：坐标必须取自用户消息里的坐标清单、无动作时输出
# "点击坐标：wait" 这两条运行期约束，仍由 ask_ai 的用户消息下发
# （每次请求都会带上），因此这里不重复写进 System Prompt。

SYSTEM_PROMPT = """
你是羊了个羊游戏决策Agent。你能看到游戏截图，禁止使用贪心策略，不要看到能三消就立刻点击。
【核心博弈规则，严格遵守优先级顺序】
1. 首要目标：优先选择执行后可以掀开上层方块、释放底层被压住方块的消除动作。
2. 风险规避：如果某个三消操作会永久封死底层方块通路，哪怕当前可以消除，也必须放弃该操作。
3. 槽位管控：时刻关注槽位数量，尽量维持低位，避免槽位逼近7个上限；禁止让槽位堆满。
4. 决策思考流程（必须按这个顺序分析）：
① 列出当前棋盘所有合法可三消的候选操作；
② 对每一个候选操作，预判执行后的棋盘变化：会暴露哪些新方块、会不会堵住底层通路、槽位增减情况；
③ 评估每个操作长期收益，选择长期收益最高的动作，不是当前消除最快的动作。
【强制输出格式，不允许增加额外文字，不允许解释格式】
思考：<简短预判分析，重点写：操作收益、底层通路风险、槽位变化>
点击坐标：x,y
约束：只输出上面两行，不要markdown，不要多余闲聊，不要输出多个候选方案。
"""


# ============================================================
# 4. 查找游戏窗口
# ============================================================

def find_game_window():
    """
    查找游戏窗口在屏幕上的位置（逻辑坐标）。

    返回：
        (left, top, width, height)
        找不到时返回 None。
    """

    try:
        import Quartz

        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )

        window_list = Quartz.CGWindowListCopyWindowInfo(
            options,
            Quartz.kCGNullWindowID,
        )

        for window in window_list:
            title = str(window.get("kCGWindowName") or "")

            if GAME_WINDOW_KEYWORD in title:
                bounds = window["kCGWindowBounds"]

                rect = (
                    int(bounds["X"]),
                    int(bounds["Y"]),
                    int(bounds["Width"]),
                    int(bounds["Height"]),
                )

                print(
                    f"[窗口] 已找到游戏窗口"
                    f"：位置 ({rect[0]}, {rect[1]})"
                    f"，大小 {rect[2]}x{rect[3]}"
                )

                return rect

    except Exception as exc:
        print(f"[警告] 查找游戏窗口失败：{exc}")

    return None


# ============================================================
# 5. 创建一次性临时截图
# ============================================================

def capture_screen():
    """
    每次重新截取屏幕，并保存到唯一的临时 JPEG 文件。

    如果开启了 CROP_TO_GAME_WINDOW 且能找到游戏窗口，
    则只保存游戏窗口区域，让模型看到更大的砖块画面。

    另外会按 MODEL_IMAGE_SCALE 生成一份缩放后的图发给模型
    （临时 JPEG），同时在 debug_last.png 里保留原尺寸的分析图。
    """

    from PIL import Image as PILImage

    global LAST_WINDOW_RECT

    screenshot = None
    rgb_screenshot = None
    screenshot_path = None

    try:
        # 1. 重新截取整个屏幕
        screenshot = pyautogui.screenshot()
        image_width, image_height = screenshot.size

        # 2. 转换成 JPEG 支持的 RGB 模式
        rgb_screenshot = screenshot.convert("RGB")

        # 3. 裁剪出游戏窗口区域
        LAST_WINDOW_RECT = None

        if CROP_TO_GAME_WINDOW:
            window = find_game_window()

            if window is not None:
                left, top, win_width, win_height = window

                screen_width, screen_height = pyautogui.size()

                # 全屏截图是物理像素，窗口坐标是逻辑点
                ratio = image_width / screen_width

                crop_box = (
                    max(0, round(left * ratio)),
                    max(0, round(top * ratio)),
                    min(image_width, round((left + win_width) * ratio)),
                    min(image_height, round((top + win_height) * ratio)),
                )

                crop_width = crop_box[2] - crop_box[0]
                crop_height = crop_box[3] - crop_box[1]

                if crop_width > 50 and crop_height > 50:
                    cropped_image = rgb_screenshot.crop(crop_box)

                    rgb_screenshot.close()
                    rgb_screenshot = cropped_image

                    image_width, image_height = cropped_image.size
                    LAST_WINDOW_RECT = window

                    print(
                        f"\n[截图] 已裁剪到游戏窗口区域"
                        f" | 分辨率：{image_width}x{image_height}"
                    )

                else:
                    print("[警告] 游戏窗口区域过小，改用全屏截图")

            else:
                print("[警告] 未找到游戏窗口，改用全屏截图")

        if LAST_WINDOW_RECT is None:
            print(
                f"\n[截图] 已生成最新全屏截图"
                f" | 分辨率：{image_width}x{image_height}"
            )

        # 4. 创建唯一的临时截图文件
        temp_file = tempfile.NamedTemporaryFile(
            prefix="game_agent_",
            suffix=".jpg",
            delete=False,
        )

        screenshot_path = temp_file.name
        temp_file.close()

        # 5. 保存发给模型的截图（按 MODEL_IMAGE_SCALE 缩放）
        #    缩放只作用于发给模型的这份，下面保存的分析图仍是原尺寸。
        model_image = rgb_screenshot

        if MODEL_IMAGE_SCALE != 1.0:
            model_width = max(
                1, round(image_width * MODEL_IMAGE_SCALE)
            )
            model_height = max(
                1, round(image_height * MODEL_IMAGE_SCALE)
            )

            model_image = rgb_screenshot.resize(
                (model_width, model_height),
                PILImage.LANCZOS,
            )

            print(
                f"[截图] 发给模型的图片已缩放："
                f"{image_width}x{image_height}"
                f" -> {model_width}x{model_height}"
            )

        try:
            model_image.save(
                screenshot_path,
                format="JPEG",
                quality=IMAGE_QUALITY,
            )
        finally:
            if model_image is not rgb_screenshot:
                model_image.close()

        # 6. 保存一份本轮分析图
        #    （遮挡检查的数据源，必须保持原尺寸，不能缩放）
        try:
            rgb_screenshot.save(DEBUG_LAST_PATH, format="PNG")
        except Exception as exc:
            print(f"[警告] 保存分析图失败：{exc}")

        return screenshot_path, image_width, image_height

    except Exception:
        # 如果截图或保存失败，删除不完整的临时文件
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
            except OSError:
                pass

        raise

    finally:
        # 只关闭已经成功创建的对象
        if rgb_screenshot is not None:
            rgb_screenshot.close()

        if screenshot is not None:
            screenshot.close()


# ============================================================
# 5. 读取并编码本轮截图
# ============================================================

def encode_image(screenshot_path):
    """
    读取指定的临时截图并转换为 Base64。

    每次都从当前临时文件读取，不缓存旧截图。
    """

    with open(screenshot_path, "rb") as file:
        image_bytes = file.read()

    try:
        image_base64 = base64.b64encode(
            image_bytes
        ).decode("utf-8")
    finally:
        del image_bytes

    return image_base64


# ============================================================
# 6. 请求 AI 分析当前画面
# ============================================================

def ask_ai(feedback=None):
    """
    每轮执行：
    1. 重新截图。
    2. 读取并编码截图。
    3. 本地检测可点击砖块，把坐标清单一起发给模型。
    4. 向模型发送本轮截图。
    5. 请求结束后删除截图文件。
    6. 清理图片相关临时数据。

    参数：
        feedback：上一次选择被拒绝的原因，会一并告诉模型

    返回：
        model_text: 模型返回的文本
        image_width: 本轮截图宽度
        image_height: 本轮截图高度
    """

    # 【DPO 格式改造】本轮留档用到的全局变量
    global LAST_STEP_PROMPT
    global LAST_STEP_CANDIDATES
    global LAST_STEP_FRAME
    global LAST_STEP_IMAGE_SIZE
    global LAST_STEP_MODEL_IMAGE_SIZE

    screenshot_path = None
    image_base64 = None

    image_width = 0
    image_height = 0

    try:
        # 每一轮都重新截屏
        screenshot_path, image_width, image_height = (
            capture_screen()
        )

        # 读取刚刚生成的截图
        image_base64 = encode_image(screenshot_path)

        # 本地检测本轮可点击的砖块并按消除优先级分组
        clickable_hint = build_ranked_hint(
            image_width, image_height
        )

        # 【修改点·需求2】提问文案改成新输出格式
        user_text = (
            "请分析这一次刚刚截取的游戏画面，给出下一步操作。"
            "严格按下面的格式输出（先完成推演，再给坐标），"
            "不要有多余文字：\n"
            "思考：<简短预判分析>\n"
            "点击坐标：x,y"
        )

        if clickable_hint:
            user_text += (
                "\n\n本轮可点击的砖块坐标清单如下（分组只表示短期收益"
                "参考，不强制按组号顺序；最终由你按长远收益决定，"
                "但 x、y 必须严格等于清单里的某一对，"
                "不允许使用清单以外的坐标）：\n"
                + clickable_hint
            )
        else:
            user_text += (
                "\n\n本轮没有检测到任何可点击的完整卡面，"
                "请输出 点击坐标：wait。"
            )

        # 【禁止使用道具】运行期硬约束：道具区（卡槽下方三个按钮）
        # 已在本地下线，提示词里同步说清楚，避免模型白给一个道具坐标
        if FORBID_PROPS:
            user_text += (
                "\n\n约束：本程序**禁止使用道具**，"
                "不要点击卡槽下方的「移出 / 撤回 / 洗牌」按钮，"
                "只能从上面的坐标清单里选砖块。"
            )

        if feedback:
            user_text += (
                "\n\n重要：你上一轮的选择或输出格式被判定为无效，"
                f"原因：{feedback}。"
                "请重新决策，并且必须严格输出两行："
                "第一行「思考：...」，第二行「点击坐标：x,y」，"
                "坐标只能从上面给出的清单里挑。"
                # 【截断修复】重问时明确要求压缩思考，
                # 避免再次因为写太长而把坐标行挤掉
                "「思考」只写一句话，不要列举多个候选方案、"
                "不要解释格式，避免输出过长导致坐标行被截断。"
            )

        print("[AI] 正在分析本轮最新截图……")

        # 【DPO 格式改造】只有"首次询问（不带反馈）"的那一轮才留档
        # frame / prompt / candidates：重试轮的画面对应的是带纠正说明的
        # 上下文，不能当作偏好数据里的"同一 prompt"样本。
        if feedback is None:
            LAST_STEP_PROMPT = user_text
            LAST_STEP_CANDIDATES = list(LAST_CANDIDATES)
            LAST_STEP_IMAGE_SIZE = [image_width, image_height]
            LAST_STEP_MODEL_IMAGE_SIZE = [
                round(image_width * MODEL_IMAGE_SCALE),
                round(image_height * MODEL_IMAGE_SCALE),
            ]
            LAST_STEP_FRAME = save_step_frame(screenshot_path)

        # 每次使用独立的消息列表，不携带旧截图或历史消息
        # 【修改点·需求5】对话上下文按"每次请求独立"处理，
        # 不跨轮、不跨局累积，避免内存持续上涨
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/jpeg;base64,"
                                    + image_base64
                                )
                            },
                        },
                    ],
                },
            ],
            # 【修改点·需求4】固定推理参数
            temperature=MODEL_TEMPERATURE,
            top_p=MODEL_TOP_P,
            max_tokens=MODEL_MAX_TOKENS,
        )

        choice = response.choices[0]
        model_text = choice.message.content

        if not model_text:
            raise RuntimeError("模型返回了空内容")

        # 【截断修复】识别"输出被 max_tokens 截断"的情况：
        # finish_reason == "length" 说明额度用尽，坐标行很可能被挤掉。
        # 这里只做记录与提示，后续重问由主流程按"输出格式错误"处理。
        if getattr(choice, "finish_reason", None) == "length":
            print(
                "[警告] 模型输出被长度上限截断（finish_reason=length），"
                "坐标行可能缺失，将按输出格式错误重问"
            )
            append_log(
                "截断 | finish_reason=length | 坐标行可能缺失"
            )

        return model_text, image_width, image_height

    finally:
        # 无论请求成功还是失败，都清理临时截图
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
                print("[清理] 本轮截图文件已删除")
            except OSError as exc:
                print(f"[警告] 删除截图失败：{exc}")

        # 清理 Base64 图片数据
        if image_base64 is not None:
            del image_base64

        # 请求结束后执行垃圾回收
        gc.collect()


# ============================================================
# 7. 解析模型返回的文本
# ============================================================
#
# 【修改点·需求2】输出格式由 JSON 改为：
#   思考：<简短预判分析>
#   点击坐标：x,y
# 下面新增 parse_model_output()，用正则提取"思考"内容和 x、y。

def parse_json(model_text):
    """
    旧版 JSON 解析（原链路使用）。

    【修改点·需求2】新链路输出的是"思考：/点击坐标："纯文本，
    本函数仅作历史保留，不再被主流程调用。
    """

    text = model_text.strip()

    # 移除可能出现的 Markdown 代码块
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取第一个 JSON 对象
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)

        if not match:
            raise ValueError(
                f"模型没有返回有效 JSON：\n{text}"
            )

        return json.loads(match.group(0))


# 【修改点·需求2】"点击坐标：..." 之后到行尾
# 【修改点·本轮·截断修复】解析时改为"取最后一次出现"，
# 不再用"思考:... 直到第一个 点击坐标"这种写法——模型啰嗦时
# 会在"思考"里顺带提到"点击坐标"，按第一次匹配会取错行。
CLICK_LINE_PATTERN = re.compile(r"点击坐标\s*[:：]\s*([^\n]*)")

# x,y 或 x，y（允许中英文逗号、空格、可选正负号）
COORD_PATTERN = re.compile(r"(-?\d+)\s*[,，]\s*(-?\d+)")

# 表示"本轮不点击"的关键词
WAIT_PATTERN = re.compile(
    r"wait|无|等待|none|null", re.IGNORECASE
)


def parse_model_output(model_text):
    """
    【修改点·需求2】解析"思考：/点击坐标："格式，正则提取 x、y。

    返回统一的操作字典（与旧 JSON 结构保持一致，方便下游复用）：
        {"action": "click"/"wait", "x": int|None, "y": int|None,
         "reason": 思考内容, "confidence": 1.0, "tile": "", "tray": []}

    输出格式不合格时返回 None，由主流程按"格式错误"重问。
    """

    if not model_text:
        return None

    text = model_text.strip()

    # 容忍偶发的 Markdown 代码块围栏
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    # 1. 必须存在"点击坐标："行，否则视为格式不合格。
    #    【截断修复】取最后一次出现：格式规定坐标行在最后，
    #    若"思考"里也提到了"点击坐标"，取第一次会取到思考中的那句话。
    click_matches = list(CLICK_LINE_PATTERN.finditer(text))

    if not click_matches:
        return None

    click_match = click_matches[-1]

    # 2. "思考"= 坐标行之前的所有内容，去掉"思考："前缀，
    #    并剔除思考里可能混入的"点击坐标：..."字样
    think_block = CLICK_LINE_PATTERN.sub("", text[: click_match.start()])

    think = re.sub(
        r"^\s*思考\s*[:：]", "", think_block.strip()
    ).strip()

    coord_text = click_match.group(1).strip()

    # 3. 提取坐标数字
    coord = COORD_PATTERN.search(coord_text)

    if not coord:
        # 没有数字：允许显式写 wait / 无 / 等待
        if WAIT_PATTERN.search(coord_text):
            return {
                "action": "wait",
                "x": None,
                "y": None,
                "reason": think or "本轮不点击",
                "confidence": 1.0,
                "tile": "",
                "tray": [],
            }

        return None

    # 4. 点击必须带"思考"，否则视为未完成强制思考流程
    if not think:
        return None

    return {
        "action": "click",
        "x": int(coord.group(1)),
        "y": int(coord.group(2)),
        "reason": think,
        "confidence": 1.0,
        "tile": "",
        "tray": [],
    }


# ============================================================
# 8. 检查模型返回的操作
# ============================================================

def model_coords_to_analysis(data):
    """
    把模型返回的坐标从"发给模型的缩放图"坐标系，
    换算回"本地原尺寸分析图"坐标系。

    模型看到的是缩放后的图片，它返回的 x、y 是那张图上的像素；
    而遮挡检查、鼠标坐标换算都基于原尺寸的分析图，
    所以解析出结果后必须先把坐标换算回来。

    该函数就地修改 data，不返回值。
    """

    if MODEL_IMAGE_SCALE == 1.0:
        return

    if data.get("action") != "click":
        return

    for key in ("x", "y"):
        value = data.get(key)

        if isinstance(value, (int, float)):
            data[key] = round(value / MODEL_IMAGE_SCALE)


def validate_action(data, image_width, image_height):
    """
    检查操作类型、坐标范围和置信度。

    返回：
        (True, "")：操作有效
        (False, 原因)：操作无效
    """

    action = data.get("action")

    if action in ("wait", "stop"):
        return True, ""

    if action != "click":
        print(f"[错误] 不支持的操作类型：{action}")
        return False, f"不支持的操作类型：{action}"

    # 防线：如果 reason 是提示词里的模板原句，说明模型
    # 只是在照抄示例，并没有真正分析画面，此时坐标不可信。
    reason = str(data.get("reason") or "").strip()

    if not reason or reason == "选择这个方块的原因":
        print(
            "[安全] 模型返回了模板占位内容，"
            "疑似未真正识别画面，本轮不点击"
        )
        return False, "返回了模板占位内容，未真正分析画面"

    x = data.get("x")
    y = data.get("y")

    if not isinstance(x, (int, float)):
        print("[错误] 模型返回的 x 坐标无效")
        return False, "x 坐标无效"

    if not isinstance(y, (int, float)):
        print("[错误] 模型返回的 y 坐标无效")
        return False, "y 坐标无效"

    if not (0 <= x < image_width):
        print("[错误] x 坐标超出截图范围")
        return False, "x 坐标超出画面范围"

    if not (0 <= y < image_height):
        print("[错误] y 坐标超出截图范围")
        return False, "y 坐标超出画面范围"

    confidence = data.get("confidence", 0)

    if not isinstance(confidence, (int, float)):
        confidence = 0

    if not 0 <= confidence <= 1:
        confidence = 0

    # 低置信度时拒绝点击
    if confidence < 0.5:
        print(
            f"[安全] 模型置信度过低：{confidence}"
            "，本轮不点击"
        )
        return False, f"置信度过低：{confidence}"

    return True, ""


# ============================================================
# 9. 砖块遮挡检测
# ============================================================
#
# 该游戏的画面特征（基于实测）：
#   - 可点击的砖块显示为一张完整的浅色（米白色）卡面，
#     约 95 x 115 像素，米白色实测约为 (247, 255, 210)
#   - 被上层压住的砖块只在卡面上方的灰色凹槽里露出
#     一小截图案（约 54 像素高），没有米白色卡面
#
# 判定方法：
#   1. 在点击点附近寻找米白色像素作为种子
#   2. 泛洪填充整个相连的米白区域，得到卡面包围盒
#   3. 点击点必须落在包围盒内，否则视为被压住或空白
#

# 以下阈值基于当前窗口大小（510x949）实测
# 在点击点多大范围内寻找米白卡面（级联半径，逐级放大重试）
# 不同关卡的砖块大小差别很大：小砖块关卡里卡面中心可能
# 整个落在图案上，离米白边框超过 30 像素，固定半径会漏判。
# 泛洪之后仍有"点击点必须落在卡面包围盒内"的判定兜底，
# 所以放大半径不会把邻居砖块误判成目标。
FACE_SEARCH_RADII = (30, 50, 70)
FACE_MIN_WIDTH = 60       # 完整卡面连通区域的最小宽度
FACE_MIN_HEIGHT = 80      # 完整卡面连通区域的最小高度
FACE_MAX_PIXELS = 60000   # 泛洪填充的像素数上限（防御异常大区域）
BBOX_MARGIN = 4           # 包围盒判定的宽容边际

# 【修复】卡槽排除：卡槽里已收集的砖块同样是完整米白卡面，
# 但它们挤在底部的深色卡槽面板里，左右两侧紧邻的是
# 其他卡面或面板；而棋盘砖块摆在草地上，左右至少
# 有一侧紧邻草地。用这个特征把卡槽砖块排除掉，
# 避免模型反复点击卡槽里的砖块导致卡死。
SIDE_SAMPLE_OFFSET = 20   # 在卡面左右两侧多远处采样背景

# 卡槽判定补充：卡槽面板固定在画面底部（实测卡槽砖中心
# y 约在画面高度 81% 处），而棋盘密集处左右没草地的砖块
# 最高也只到约 67%。只有"无草地 且 位于底部区域"的卡面
# 才认定为卡槽砖，避免把被邻居挤住的棋盘砖误判成卡槽砖。
TRAY_BAND_RATIO = 0.76    # 卡槽区域的最小高度占比（相对整幅画面）

# 【消除优先级】模型只看图数不清"卡槽里已有几个同款"，
# 所以由本地完成：把每块卡面的内部图案做成指纹并聚类，
# 统计卡槽中每种图案的数量，据此把可点击砖块分成三组：
#   第1组：卡槽里已有 2 个同款，点击立即三连消除（最高优先）
#   第2组：卡槽里已有 1 个同款，点击后凑成一对
#   第3组：全新图案，点击会占用一个卡槽空位（最低优先）
PRIORITY_ENFORCE = False       # 【修改点·需求1】默认关闭"贪心强校验"

# 【修改点·需求1】为什么默认关闭：
# 新的 System Prompt 已把策略决策交给 Qwen3-VL 单模型做长远推理，
# 并要求"为了不封死底层通路，可以放弃当前能立即三消的动作"。
# 而本开关原来的语义是"只要存在可立即消除的砖块，就只能点它"，
# 正好是要被禁用的贪心策略。若仍置 True，模型的长远选择会被本地
# 直接拒绝，新策略无法生效。改成 False 后：
#   - 遮挡检查（verify_click_on_face）仍然生效，安全底线不变；
#   - LAST_MUST_WAIT（卡槽将满必须等待）这条安全底线仍然生效；
#   - 只放开"必须优先选最高优先级组"这个贪心约束。
# 如需恢复旧行为，把这里改回 True 即可。
TRAY_CAPACITY = 7              # 卡槽容量

# ============================================================
# 解放被压砖块（明面上无可消除时的策略）
# ============================================================
#
# 明面上没有可消除/可配对的砖块时，不要随便点一块新图案，
# 而是优先点掉"正压着其他砖块"的砖，露出下方可能凑对的砖块。
#
# 安全前提（避免导致失败）：
#   - 只在卡槽已用格数 <= UNLOCK_MAX_TRAY_USED 时启用解放优先，
#     保证点掉一块解放砖之后卡槽还留有足够的周转空位。
UNLOCK_FIRST = True

# 砖块正下方露出图案面积达到多少像素才算"压着别的砖块"
# （实测一块完整露出的被压砖约 600~2600 像素）
UNLOCK_MIN_AREA = 600

# 砖块下边缘往下多大范围算"正下方"
UNLOCK_BAND_HEIGHT = 58

# 卡槽已用格数超过该值时不做解放优先（留足周转空位）
UNLOCK_MAX_TRAY_USED = TRAY_CAPACITY - 3
PATTERN_MATCH_THRESHOLD = 200  # 指纹距离低于该值视为同一图案
# 阈值依据：同图案实测 8~110（缩放/JPEG 压缩带来波动），
# 异图案最小 1166，取 200 两边余量都足够
FP_CELLS = 10                  # 指纹降采样网格边长
FP_INSET = 0.22                # 卡面内部取样比例（去掉边框和边缘图案）
PRIORITY_TOLERANCE = 4         # 优先级坐标校验的像素容差（缩放取整误差）


# ============================================================
# 卡槽槽位硬保护（修：卡槽填满 = 立即失败）
# ============================================================
#
# 《羊了个羊》的失败条件是"卡槽 7 格被填满且没有凑成三连"。
# 所以"点下去之后卡槽会变成几格"是硬约束，不是策略偏好：
#   卡槽 6/7 时点一块"不能立即三连"的砖 -> 变成 7/7 且不消除 -> 立即失败。
# 这跟贪心不贪心无关，任何策略下都不允许发生。
#
# 为什么必须单独做：关闭 PRIORITY_ENFORCE（不贪心）之后，
# 模型可能为了"解放底层"去点非三连的砖，正好踩中这条死亡线。
TRAY_HARD_GUARD = True

# 【卡槽紧张·强制"能消就先消"】
# 卡槽占用 >= TIGHT_TRAY_PRIORITY_THRESHOLD（默认 5/7）且本轮存在
# "点下去立即三连"的砖块时，**必须优先消除**：
#   消除会腾出 2 个空位，是唯一能把卡槽往回拉的走法；
#   此时去凑对/放新图案只会继续占用格子，把局面推向 6/7 死线。
# 卡槽宽松时不做这个强制，保留模型的长远规划空间
# （PRIORITY_ENFORCE 仍为 False，不给模型套全局贪心）。
#
# 依据：trace_20261014_110737 里模型反复在有可消除砖块时选了凑对
# （第 29/38/44/53/57/65/68/71 步），卡槽被一步步推到 6/7。
TIGHT_TRAY_PRIORITY = True
TIGHT_TRAY_PRIORITY_THRESHOLD = TRAY_CAPACITY - 2      # 5

# 【槽位硬保护·唯一规则】只禁止"数学上即死"的点击：
#   游戏规则是卡槽一旦满 7 格且这一下没有凑成三连 -> 立即失败。
#   因此只有一种点击是必死的：
#     卡槽 6/7 时点击"全新图案"（凑对和三连点下去都会立即消除，安全）。
#   其余一切前瞻（"之后还凑不凑得齐""可见副本够不够"）都不做——
#   实测教训：
#     1) 峰值公式把"先放新图案、再靠后续消除腾位"的活路算成必败
#        （例如 5/7 先拿白色、再点白下露出的粉色凑三连）；
#     2) "可见副本数"在叠放关卡里不可靠（同款大半压在下面，
#        看不见 != 不存在）。
#   这两条都曾把明明能继续的局面误判成死局而停机。
#   失败与否由游戏结算弹窗说了算，本地只拦"即死"这一种。


def non_eliminate_survivable(tray_used, tray_n, board_n):
    """
    非消除点击（凑对 / 新图案）是否"即死"。

    【重要修正 2026-10-14】凑对（tray_n=1）**不等于**三连！
    卡槽里已有 1 个同款时点第 2 个，只是凑成"一对"，
    这一下既不会消除、也不释放槽位，照样占掉 1 格。
    所以凑对和新图案的占用规则完全一样：
        点下去卡槽变成 tray_used + 1 格，
        只有"没满 7 格"才安全 ——
        卡槽 6/7 时点下去就是 7/7 且不消除 = 立即失败。

    实测教训（trace_20261014_110737 第 79 步）：
    旧实现把凑对当成"点下去立即三连"直接放行（return True），
    结果卡槽 6/7 时 5 块凑对被标成 safe=True 写进提示和轨迹，
    模型一点就 7/7 当场失败。这是规则错误，不是策略偏好：
    任何策略下这一步都必死，而且错误的 safe 标签还会
    顺着 DPO 数据集污染训练样本。

    参数 board_n 仅为保持旧签名兼容，不参与判定
    （"可见副本数"在叠放关卡里不可靠：同款大半压在下面，
      看不见 != 不存在，曾据此把活路误判成死局）。
    """

    return tray_used + 1 < TRAY_CAPACITY


def is_grass_background(pixel):
    """
    判断一个像素是否是棋盘的草地背景。

    草地有两种色调（青绿约 (94,165,144) 和黄绿约 (123,159,52)），
    共同特征是绿色分量明显高于红色分量。
    米白卡面、白色遮挡窗、深色卡槽面板都不满足。
    """

    r, g, b = pixel[0], pixel[1], pixel[2]

    return g > 140 and g > r + 25


def sample_grass_ratio(image):
    """
    采样估算画面里草地背景的占比（0.0 ~ 1.0）。

    正常对局实测约 90%；结算弹窗 / 复活弹窗会把画面压暗或盖住，
    占比骤降到 50% 以下。detect_level_end 和"新局验证"共用这一个实现。
    """

    pixels = image.load()
    width, height = image.size

    grass = 0
    total = 0

    for y in range(0, height, 8):
        for x in range(0, width, 8):
            total += 1

            if is_grass_background(pixels[x, y]):
                grass += 1

    if not total:
        return 0.0

    return grass / total


def has_grass_beside(image, min_x, min_y, max_x, max_y):
    """
    检查一张卡面的左右两侧是否至少有一侧紧邻草地背景。

    棋盘砖块 -> True（左右有草地）
    卡槽砖块 -> False（左右是其他卡面或深色面板）
    """

    pixels = image.load()
    width, height = image.size

    center_y = (min_y + max_y) // 2

    for side_x in (
        min_x - SIDE_SAMPLE_OFFSET,
        max_x + SIDE_SAMPLE_OFFSET,
    ):
        for dy in (-40, -20, 0, 20, 40):
            sy = center_y + dy

            if 0 <= side_x < width and 0 <= sy < height:
                if is_grass_background(pixels[side_x, sy]):
                    return True

    return False


def is_cream_face(pixel):
    """判断一个像素是否属于卡面的米白色底。"""
    r, g, b = pixel[0], pixel[1], pixel[2]
    return r > 225 and g > 225 and b > 170 and (r - b) > 15


def find_face_component(image, seed_x, seed_y):
    """
    从种子像素出发，泛洪填充整个相连的米白卡面区域。

    返回：
        (min_x, min_y, max_x, max_y, members)
        members：该卡面包含的所有像素集合
    """

    pixels = image.load()
    width, height = image.size

    min_x = max_x = seed_x
    min_y = max_y = seed_y

    visited = set()
    stack = [(seed_x, seed_y)]

    while stack:
        cx, cy = stack.pop()

        if (cx, cy) in visited:
            continue

        visited.add((cx, cy))

        if cx < min_x:
            min_x = cx
        if cx > max_x:
            max_x = cx
        if cy < min_y:
            min_y = cy
        if cy > max_y:
            max_y = cy

        if len(visited) > FACE_MAX_PIXELS:
            break

        for nx, ny in (
            (cx + 1, cy),
            (cx - 1, cy),
            (cx, cy + 1),
            (cx, cy - 1),
        ):
            if 0 <= nx < width and 0 <= ny < height:
                if (nx, ny) not in visited:
                    if is_cream_face(pixels[nx, ny]):
                        stack.append((nx, ny))

    return min_x, min_y, max_x, max_y, visited


def find_board_and_tray_tiles(image):
    """
    一次扫描找出画面里所有完整卡面，并区分棋盘砖和卡槽砖。

    区分依据（位置）：卡槽面板固定在画面底部，
    卡面中心落在底部区域（TRAY_BAND_RATIO）以下的是卡槽砖，
    其余全部是棋盘砖。
    注意不能用"旁边有没有草地"区分——密集堆叠关卡里
    大部分棋盘砖被邻居挤住，左右根本没有草地。

    返回：
        (board_tiles, tray_tiles)
        每个元素是 {"center": (cx, cy), "bbox": (x0, y0, x1, y1)}
        均按从上到下、从左到右排序
    """

    pixels = image.load()
    width, height = image.size

    visited = set()
    board_tiles = []
    tray_tiles = []

    # 步长 4 采样即可命中卡面，泛洪填充本身是逐像素的
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            if (x, y) in visited:
                continue

            if not is_cream_face(pixels[x, y]):
                continue

            (
                min_x,
                min_y,
                max_x,
                max_y,
                members,
            ) = find_face_component(image, x, y)

            visited |= members

            face_width = max_x - min_x + 1
            face_height = max_y - min_y + 1

            if face_width < FACE_MIN_WIDTH:
                continue

            if face_height < FACE_MIN_HEIGHT:
                continue

            bbox = (min_x, min_y, max_x, max_y)
            center = ((min_x + max_x) // 2, (min_y + max_y) // 2)

            in_bottom_band = (
                center[1] >= height * TRAY_BAND_RATIO
            )

            if in_bottom_band:
                # 位于底部卡槽面板区域 -> 卡槽砖（已收集，不可再点）
                tray_tiles.append(
                    {"center": center, "bbox": bbox}
                )
            else:
                board_tiles.append(
                    {"center": center, "bbox": bbox}
                )

    board_tiles.sort(key=lambda t: (t["center"][1], t["center"][0]))
    tray_tiles.sort(key=lambda t: (t["center"][1], t["center"][0]))

    return board_tiles, tray_tiles


def find_clickable_tiles(image):
    """
    找出当前画面中所有可点击的完整卡面（即棋盘砖）。

    会排除底部卡槽里的砖块：它们虽然也是完整米白卡面，
    但已经收集进卡槽，无法再次点击。

    返回：
        [(center_x, center_y), ...]，按从上到下、从左到右排序
    """

    board_tiles, _ = find_board_and_tray_tiles(image)

    return [tile["center"] for tile in board_tiles]


def find_buried_regions(image, board_tiles, tray_tiles):
    """
    找出被上层压住的砖块露出的部分——画面上"没有完整卡面、
    但能看到图案"的区域。

    这些区域是"下方还有砖块被压着"的证据：
    哪块可点击砖块的正下方压着它们，点掉那块砖
    就能解放（部分或全部露出）下面的砖块。

    只在棋盘有效区域内找（所有砖块外扩一个砖宽），
    避免把背景上的小草涂鸦当成露出图案。

    返回：
        [bbox, ...]，bbox = (x0, y0, x1, y1)
    """

    from collections import deque

    pixels = image.load()
    width, height = image.size

    if not board_tiles:
        return []

    faces = [t["bbox"] for t in board_tiles]
    faces += [t["bbox"] for t in tray_tiles]

    x_lo = max(0, min(b[0] for b in faces) - 60)
    x_hi = min(width, max(b[2] for b in faces) + 60)
    y_lo = max(0, min(b[1] for b in faces) - 60)
    y_hi = min(
        int(height * TRAY_BAND_RATIO),
        max(b[3] for b in faces) + 60,
    )

    if x_lo >= x_hi or y_lo >= y_hi:
        return []

    step = 2
    gw = (x_hi - x_lo) // step + 1
    gh = (y_hi - y_lo) // step + 1

    # 先把所有完整卡面标记到网格上，避免逐像素遍历卡面
    face_grid = [bytearray(gw) for _ in range(gh)]

    for fx0, fy0, fx1, fy1 in faces:
        gx0 = max(0, (fx0 - 2 - x_lo) // step)
        gx1 = min(gw - 1, (fx1 + 2 - x_lo) // step)
        gy0 = max(0, (fy0 - 2 - y_lo) // step)
        gy1 = min(gh - 1, (fy1 + 2 - y_lo) // step)

        for gy in range(gy0, gy1 + 1):
            row = face_grid[gy]

            for gx in range(gx0, gx1 + 1):
                row[gx] = 1

    def is_pattern_pixel(p):
        """既不是米白卡面、也不是草地，而是有饱和度的图案色。"""

        if is_cream_face(p) or is_grass_background(p):
            return False

        r, g, b = p
        bright = max(r, g, b)

        return bright >= 60 and bright - min(r, g, b) >= 40

    mask = set()

    for gy in range(gh):
        frow = face_grid[gy]
        y = y_lo + gy * step

        for gx in range(gw):
            if frow[gx]:
                continue

            x = x_lo + gx * step

            if is_pattern_pixel(pixels[x, y]):
                mask.add((x, y))

    seen = set()
    regions = []

    for start in mask:
        if start in seen:
            continue

        queue = deque([start])
        seen.add(start)
        comp = []

        while queue:
            cx, cy = queue.popleft()
            comp.append((cx, cy))

            for dx in (-step, 0, step):
                for dy in (-step, 0, step):
                    n = (cx + dx, cy + dy)

                    if n in mask and n not in seen:
                        seen.add(n)
                        queue.append(n)

        # 40 个采样点 ≈ 160 像素，过滤碎屑噪点
        if len(comp) < 40:
            continue

        xs = [c[0] for c in comp]
        ys = [c[1] for c in comp]

        x0, y0 = min(xs), min(ys)
        x1, y1 = max(xs), max(ys)

        bw = x1 - x0 + step
        bh = y1 - y0 + step

        # 露出部分的合理形状：宽度至少小半个砖，高度 18~110
        if bw < 45 or bh < 18 or bh > 110:
            continue

        regions.append((x0, y0, x1, y1))

    return regions


def compute_unlock_scores(board_tiles, buried_regions):
    """
    给每块棋盘砖打"解放分"：它正下方压着多少露出图案区域。

    分数越高，说明这块砖压着的被压砖块越多/露出越多，
    点掉它越可能解放出下方可以凑对的砖块。

    返回：
        {砖块中心坐标: 解放分}
    """

    scores = {}

    for tile in board_tiles:
        x0, y0, x1, y1 = tile["bbox"]

        band = (
            x0 - 30,
            y1 + 2,
            x1 + 30,
            y1 + UNLOCK_BAND_HEIGHT,
        )

        area = 0

        for bx0, by0, bx1, by1 in buried_regions:
            ox = min(band[2], bx1) - max(band[0], bx0)
            oy = min(band[3], by1) - max(band[1], by0)

            if ox > 0 and oy > 0:
                area += ox * oy

        scores[tile["center"]] = area

    return scores


def tile_fingerprint(image, bbox):
    """
    计算一块卡面的图案指纹。

    取卡面内部区域（按 FP_INSET 去掉边框），
    降采样成 FP_CELLS x FP_CELLS 的小图，
    同一种图案渲染完全相同，指纹几乎一致；
    不同图案的指纹距离则大得多（实测差一个数量级以上）。

    【底边锚定修复】取样窗的高宽都按**卡面宽度**推算、并贴着
    bbox 底边放置。原因：棋盘砖的上沿会被上层砖压住一截，
    检测出的 bbox 只剩"下半张脸"（实测底行砖 98x90 vs 完整脸
    99x100），如果按 bbox 高度居中取样，同一种图案的指纹会整体
    错位（实测同款距离从 ~50 飙到 300~580，超过阈值导致全部
    误判成"新图案"、卡槽 6/7 时所有候选被禁、Agent 假死等待）。
    而砖块的**宽度**和**底边**永远不会被上方遮挡，按宽度定窗 +
    底边锚定后，无论上沿被压多少，取样窗始终覆盖图案的同一区域
    （实测同款距离全部回落到 81 以下）。
    """

    min_x, min_y, max_x, max_y = bbox

    face_width = max_x - min_x + 1
    face_height = max_y - min_y + 1

    inset = round(face_width * FP_INSET)

    # 正方形取样窗：边长按宽度推算（宽度不受上沿遮挡影响）
    window = face_width - 2 * inset

    # 水平居中 + 底边锚定（底边内缩去掉 3D 厚度边）
    x0 = min_x + inset
    x1 = max_x + 1 - inset
    y1 = max_y + 1 - inset
    y0 = y1 - window

    if (
        window <= 0
        or x1 <= x0
        or y0 < min_y           # 可见部分放不下取样窗（被压得太狠）
        or y1 > max_y + 1
    ):
        # 兜底：退回整卡面取样（老行为）
        x0, y0, x1, y1 = min_x, min_y, max_x + 1, max_y + 1

    from PIL import Image as PILImage

    region = image.crop((x0, y0, x1, y1))
    region = region.resize(
        (FP_CELLS, FP_CELLS), PILImage.LANCZOS
    )

    return region.tobytes()


def fingerprint_distance(a, b):
    """两个指纹的欧氏距离。"""

    total = 0

    for pa, pb in zip(a, b):
        diff = pa - pb
        total += diff * diff

    return total ** 0.5


def cluster_by_pattern(fingerprints):
    """
    把指纹聚类成若干图案组。

    贪心策略：每块砖依次尝试加入已有组，
    要求与组内所有成员的距离都小于阈值，否则另立新组。

    返回：
        labels[i] = 第 i 块砖所属的图案组编号
    """

    labels = []
    clusters = []

    for fingerprint in fingerprints:
        for cluster_id, members in enumerate(clusters):
            same = all(
                fingerprint_distance(fingerprint, fingerprints[j])
                < PATTERN_MATCH_THRESHOLD
                for j in members
            )

            if same:
                labels.append(cluster_id)
                members.append(len(labels) - 1)
                break
        else:
            labels.append(len(clusters))
            clusters.append([len(labels) - 1])

    return labels


def build_candidates_snapshot(
    groups,
    blocked,
    tile_pattern_meta,
    unlock_scores,
    tray_used,
    scale_point,
):
    """
    【DPO 格式改造】生成本轮候选清单快照并写入全局 LAST_CANDIDATES。

    这些字段是离线构造偏好数据的基础：只有知道"同一张图上有哪些
    可选动作、各自的长短期收益与风险"，才能确定性地构造
    chosen（长期最优）/ rejected（贪心坏动作）。

    参数：
        groups           ：((组号, 候选列表, 点击类型), ...)；
                           候选列表元素为 (中心坐标, 棋盘同款数量)
        blocked          ：被"即死点击"判定剔除的候选（safe=False）
        tile_pattern_meta：{中心坐标: (图案标签, 卡槽同款数, 棋盘同款数)}
        unlock_scores    ：{中心坐标: 解放分}
        tray_used        ：本轮卡槽已占用格数
        scale_point      ：分析图坐标 -> 模型图坐标 的换算函数
    """

    global LAST_CANDIDATES

    snapshot = []

    def append_items(group_no, items, kind, is_safe):
        for center, board_n in items:
            ax, ay = center
            label, tray_n, meta_board_n = tile_pattern_meta.get(
                center, (None, 0, board_n)
            )

            mx, my = scale_point(center)

            snapshot.append(
                {
                    "analysis_xy": [ax, ay],
                    "model_xy": [mx, my],
                    "group": group_no,
                    "kind": kind,
                    "tray_n": tray_n,
                    "board_n": meta_board_n,
                    "unlock": int(unlock_scores.get(center, 0)),
                    "safe": bool(is_safe),
                    "tray_used": tray_used,
                }
            )

    for group_no, items, kind in groups:
        append_items(group_no, items, kind, True)

    # 被即死判定剔除的候选也记下来（safe=False），
    # 数据集脚本可以用它们做"明确的负例"
    for center, board_n in blocked:
        label, tray_n, meta_board_n = tile_pattern_meta.get(
            center, (None, 0, board_n)
        )
        is_pair = tray_n == 1
        mx, my = scale_point(center)

        snapshot.append(
            {
                "analysis_xy": [center[0], center[1]],
                "model_xy": [mx, my],
                "group": 2 if is_pair else 3,
                "kind": "pair" if is_pair else "new",
                "tray_n": tray_n,
                "board_n": meta_board_n,
                "unlock": int(unlock_scores.get(center, 0)),
                "safe": False,
                "tray_used": tray_used,
            }
        )

    LAST_CANDIDATES = snapshot


def build_ranked_hint(image_width, image_height):
    """
    生成带消除优先级分组的坐标提示，并记录本地校验所需的状态。

    流程：
    1. 扫描出棋盘砖和卡槽砖。
    2. 对所有卡面做图案指纹聚类（棋盘和卡槽一起聚，
       保证"同一种图案"落在同一组）。
    3. 按卡槽中同款数量把棋盘砖分成三组：
       第1组可立即消除 > 第2组凑成一对 > 第3组新图案。
    4. 生成提示文本（坐标按 MODEL_IMAGE_SCALE 缩放到模型图），
       并把最高优先级组的原尺寸坐标存入 LAST_PRIORITY_TOP。

    返回：
        提示文本，检测不到棋盘砖或分析图不可用时返回 None
    """

    global LAST_CLICKABLE_COUNT
    global LAST_TRAY_COUNT
    global LAST_PRIORITY_TOP
    global LAST_PRIORITY_ORDER
    global LAST_ELIMINATE
    global LAST_MUST_WAIT
    global LAST_WAIT_REASON
    global LAST_CLICK_KIND
    global LAST_TRAY_SAFE
    global LAST_TRAY_DANGER
    global LAST_CLICK_BBOX
    global LAST_CANDIDATES
    global LAST_TOOLBAR_TOP
    global LAST_ANALYSIS_SIZE

    LAST_CLICKABLE_COUNT = 0
    LAST_TRAY_COUNT = 0
    LAST_PRIORITY_TOP = set()
    LAST_PRIORITY_ORDER = []
    LAST_ELIMINATE = set()
    LAST_MUST_WAIT = False
    LAST_WAIT_REASON = ""
    LAST_CLICK_KIND = {}
    LAST_TRAY_SAFE = set()
    LAST_TRAY_DANGER = set()
    LAST_CLICK_BBOX = []
    LAST_CANDIDATES = []
    LAST_ANALYSIS_SIZE = (image_width, image_height)

    if not CLICKABLE_HINT_ENABLED:
        return None

    if not DEBUG_LAST_PATH.exists():
        return None

    try:
        from PIL import Image as PILImage

        image = PILImage.open(DEBUG_LAST_PATH)
        image = image.convert("RGB")
    except Exception:
        return None

    if image.size != (image_width, image_height):
        image.close()
        return None

    try:
        board_tiles, tray_tiles = find_board_and_tray_tiles(image)

        LAST_CLICKABLE_COUNT = len(board_tiles)
        LAST_TRAY_COUNT = len(tray_tiles)

        # 【禁止使用道具】算出"道具区上边界"：
        # 卡槽砖的下沿再往下留 12px 余量，再往下（移出/撤回/洗牌）
        # 就是道具按钮，一律拒绝点击。
        # 卡槽为空（开局）时按画面高度的 86% 兜底——实测卡槽下沿
        # 约在 85%，道具按钮约在 87% 以下，这个位置正好卡在两者之间。
        if tray_tiles:
            LAST_TOOLBAR_TOP = (
                max(tile["bbox"][3] for tile in tray_tiles) + 12
            )
        else:
            LAST_TOOLBAR_TOP = round(image_height * 0.86)

        if not board_tiles:
            return None

        # 【自洽校验】提示里的坐标之后还要通过
        # check_tile_clickable 的遮挡检查。这里不预先过滤的话，
        # 会出现"提示推荐某砖块 -> 校验又拒绝它"的自相矛盾，
        # 模型被迫反复选同一个坐标导致死循环。
        verified_tiles = []

        for tile in board_tiles:
            ok, _ = check_tile_clickable(
                image, tile["center"][0], tile["center"][1]
            )

            if ok:
                verified_tiles.append(tile)

        board_tiles = verified_tiles

        if not board_tiles:
            return None

        # 解放分：每块棋盘砖正下方压着多少被压砖块的露出部分
        buried_regions = find_buried_regions(
            image, board_tiles, tray_tiles
        )
        unlock_scores = compute_unlock_scores(
            board_tiles, buried_regions
        )

        # 棋盘 + 卡槽一起做指纹聚类
        fingerprints = [
            tile_fingerprint(image, tile["bbox"])
            for tile in board_tiles + tray_tiles
        ]
    finally:
        image.close()

    labels = cluster_by_pattern(fingerprints)

    board_labels = labels[: len(board_tiles)]
    tray_labels = labels[len(board_tiles):]

    # 统计卡槽里每种图案的数量
    tray_count = {}
    for label in tray_labels:
        tray_count[label] = tray_count.get(label, 0) + 1

    # 统计棋盘上每种图案的可点击数量
    # （开局卡槽为空时，第1、2组必然为空，这是正常现象；
    #   但可以用棋盘同款数量给第3组排序，让开局也有引导）
    board_count = {}
    for label in board_labels:
        board_count[label] = board_count.get(label, 0) + 1

    # 按消除优先级把棋盘砖分成三组
    # 每个元素是 (中心坐标, 棋盘同款数量)
    group1 = []   # 卡槽已有 2 个同款，点击立即消除
    group2 = []   # 卡槽已有 1 个同款，点击凑成一对
    group3 = []   # 全新图案，占用卡槽空位

    # 【DPO 格式改造】center -> (图案标签, 卡槽同款数, 棋盘同款数)
    # 用于事后把"每块候选砖的图案与同款数"写进轨迹 JSON，
    # 让数据集脚本能在离线状态下重建 chosen/rejected 的判据
    tile_pattern_meta = {}

    for tile, label in zip(board_tiles, board_labels):
        tray_n = tray_count.get(label, 0)
        board_n = board_count.get(label, 0)
        item = (tile["center"], board_n)

        tile_pattern_meta[tile["center"]] = (
            label,
            tray_n,
            board_n,
        )

        if tray_n >= 2:
            group1.append(item)
        elif tray_n == 1:
            group2.append(item)
        else:
            group3.append(item)

    # ---------- 【卡槽槽位硬保护】----------
    # 记录每块候选砖点下去之后卡槽的变化类型
    for center, _ in group1:
        LAST_CLICK_KIND[center] = "eliminate"
    for center, _ in group2:
        LAST_CLICK_KIND[center] = "pair"
    for center, _ in group3:
        LAST_CLICK_KIND[center] = "new"

    tray_used = len(tray_tiles)

    # 被槽位保护剔除的候选项（写进提示里明确禁止，模型就不会去踩）
    slot_blocked = []

    if TRAY_HARD_GUARD:
        # 消除点击永远安全；凑对/新图案必须通过必败预判
        # （group2 的 tray_n=1，group3 的 tray_n=0）。
        safe1 = list(group1)
        safe2 = [
            i for i in group2
            if non_eliminate_survivable(tray_used, 1, i[1])
        ]
        safe3 = [
            i for i in group3
            if non_eliminate_survivable(tray_used, 0, i[1])
        ]

        slot_blocked = (
            [i for i in group2 if i not in safe2]
            + [i for i in group3 if i not in safe3]
        )

        group1, group2, group3 = safe1, safe2, safe3

    LAST_TRAY_SAFE = set(p for p, _ in group1 + group2 + group3)
    LAST_TRAY_DANGER = set(p for p, _ in slot_blocked)

    # 【卡槽紧张·强制消除】记录"点下去立即三连"的砖块（原尺寸坐标），
    # 供 check_priority_choice 在卡槽紧张时做强制校验
    LAST_ELIMINATE = set(p for p, _ in group1)

    # 每块砖的包围盒 + 点击类型，供"落点在卡面内但不在中心"的判定兜底
    LAST_CLICK_BBOX = [
        (
            tile["bbox"][0],
            tile["bbox"][1],
            tile["bbox"][2],
            tile["bbox"][3],
            LAST_CLICK_KIND.get(tile["center"], "new"),
        )
        for tile in board_tiles
    ]

    # ---------- 【DPO 格式改造】候选清单快照 ----------
    # 位置说明：必须在 scale_point 定义之后调用（它要用来换算模型坐标）。

    # 原尺寸分析图坐标 -> 模型看到的缩放图坐标
    def scale_point(point):
        x, y = point

        if MODEL_IMAGE_SCALE == 1.0:
            return x, y

        return (
            round(x * MODEL_IMAGE_SCALE),
            round(y * MODEL_IMAGE_SCALE),
        )

    # 把这一轮所有候选砖的完整判据写进 LAST_CANDIDATES，
    # 由 ask_ai 复制进轨迹 JSON。离线数据集脚本据此对
    # "同一张图 + 同一 prompt"构造 chosen / rejected：
    #   group=1 点下即三连（短期收益最高）
    #   group=2 与卡槽凑成一对（离三连差一块）
    #   group=3 全新图案（会占卡槽空位）
    #   unlock  = 解放分，正下方压着多少露出图案（越大越该先拿）
    #   safe    = 是否通过"即死点击"判定（False 表示禁止选）
    build_candidates_snapshot(
        groups=(
            (1, group1, "eliminate"),
            (2, group2, "pair"),
            (3, group3, "new"),
        ),
        blocked=slot_blocked,
        tile_pattern_meta=tile_pattern_meta,
        unlock_scores=unlock_scores,
        tray_used=len(tray_tiles),
        scale_point=scale_point,
    )

    def format_group(title, items):
        """items: [(中心坐标, 棋盘同款数量)]，只列坐标"""

        coords = "、".join(
            f"({scale_point(p)[0]}, {scale_point(p)[1]})"
            for p, _ in items
        )
        return f"【{title}】{coords}"

    def format_counted(title, items):
        """
        items: [(中心坐标, 棋盘同款数量)]
        按同款数量从多到少排列并分段标注，
        让模型在组内也知道优先选谁。
        """

        ordered = sorted(
            items,
            key=lambda t: (-t[1], t[0][1], t[0][0]),
        )

        segments = []

        i = 0
        while i < len(ordered):
            n = ordered[i][1]
            coords = []

            while i < len(ordered) and ordered[i][1] == n:
                x, y = scale_point(ordered[i][0])
                coords.append(f"({x}, {y})")
                i += 1

            segments.append(f"同款×{n}：{'、'.join(coords)}")

        return f"【{title}】" + "；".join(segments)

    lines = []

    if tray_tiles:
        free_slots = TRAY_CAPACITY - tray_used
        lines.append(
            f"卡槽现状：{tray_used}/{TRAY_CAPACITY} 格已占用，"
            f"只剩 {free_slots} 个空位。"
            f"卡槽被填满 7 格且没凑成三连 = 立即失败，"
            f"所以绝不能把最后一格填满而不消除。"
        )
    else:
        lines.append("卡槽现状：空（开局时第1、2组为空是正常的）")

    if group1:
        # 【修改点·需求1】去掉"最优先/必须选"的贪心措辞：
        # 分组只提供短期收益参考，是否执行由模型的远期推演决定
        lines.append(
            format_group(
                "第1组｜点击后立即三连消除（短期收益最高；"
                "若这步会永久封死底层通路，可以放弃它改做别的）",
                group1,
            )
        )

    if group2:
        lines.append(
            format_counted(
                "第2组｜点击后与卡槽中的同款凑成一对",
                group2,
            )
        )

    if group3:
        # 明面上无可消除（1、2组都空）且卡槽有余量时，
        # 第3组按"解放分"排序：先点掉正压着其他砖块的砖，
        # 露出下方可能凑对的砖块，而不是随便占用一个卡槽格
        unlock_mode = (
            UNLOCK_FIRST
            and not group1
            and not group2
            and len(tray_tiles) <= UNLOCK_MAX_TRAY_USED
        )

        if unlock_mode:
            def unlock_key(item):
                center, board_n = item
                score = unlock_scores.get(center, 0)

                if score >= UNLOCK_MIN_AREA:
                    return (0, -score, -board_n,
                            center[1], center[0])

                return (1, 0, -board_n, center[1], center[0])

            group3.sort(key=unlock_key)

            coords = "、".join(
                f"({scale_point(p)[0]}, {scale_point(p)[1]})"
                for p, _ in group3
            )

            lines.append(
                "【第3组｜全新图案，组内按'解放价值'从高到低排列："
                "排在前面的砖块正压着下方其他砖块，点掉它能露出"
                "下面可能凑成一对的砖块，请在组内优先选择靠前的】"
                + coords
            )
        else:
            lines.append(
                format_counted(
                    "第3组｜全新图案，会占用一个卡槽空位，"
                    "组内按棋盘同款数量从多到少排列，优先选同款多的",
                    group3,
                )
            )

    # 【卡槽槽位硬保护】被剔除的候选砖：
    # 只有"点下去卡槽立刻满 7 格且不凑成三连"（即 6/7 点新图案）
    # 才会被禁，其余候选一律放行，避免误伤还能走活的局面
    if slot_blocked:
        blocked_text = "、".join(
            f"({scale_point(p)[0]}, {scale_point(p)[1]})"
            for p, _ in slot_blocked
        )
        lines.append(
            f"【槽位保护·禁止选择】卡槽已占 {tray_used}/{TRAY_CAPACITY}，"
            f"以下坐标点下去会把卡槽填满 7 格且无法凑成三连、"
            f"立即失败，已全部禁止：{blocked_text}"
        )

    # 没有任何安全候选：卡槽 6/7 且棋盘上没有可立即三连/凑对的砖。
    # 此时置 LAST_MUST_WAIT，由主循环接手：
    #   - 先尝试"主动结束"（force_end_round，点一块被禁砖填满卡槽，
    #     让游戏弹出失败结算，再由自动推进开下一把）；
    #   - 结算弹窗出现后由 detect_level_end 判定成败；
    #   - 实在推进不了（预算用尽 / 点击没生效），空等超过
    #     STALL_STOP_ROUNDS 轮会熔断停机，绝不无限干等。
    if TRAY_HARD_GUARD and not LAST_TRAY_SAFE:
        LAST_MUST_WAIT = True
        LAST_WAIT_REASON = (
            f"卡槽已占用 {tray_used}/{TRAY_CAPACITY} 格，"
            "棋盘上没有可立即三连或凑对的砖块，"
            "点击其他砖块会把卡槽填满导致失败"
        )
        lines.append(
            f"警告：{LAST_WAIT_REASON}，必须输出 点击坐标：wait。"
        )
    elif tray_used >= TRAY_CAPACITY - 1:
        # 卡槽 6/7：本轮只保留了三连/凑对候选（新图案已被即死判定剔除），
        # 明确告诉模型这不是策略偏好，而是保命约束
        lines.append(
            "【保命约束】卡槽只剩 1 个空位，本轮只能点击"
            "“点击后立即三连消除或凑成一对”的坐标；不要为了解放底层"
            "去点别的砖块，那会直接填满卡槽导致失败。"
        )

    # 卡槽过半时强化"凑对优先于放新图案"的引导：
    # 一步放新图案只增加占用，凑对则距离三连只差一块
    # （卡槽紧张时不再这么引导：那时必须先消除，见下面的保命约束）
    if (
        tray_used >= 3
        and tray_used < TIGHT_TRAY_PRIORITY_THRESHOLD
        and group2
        and group3
    ):
        lines.append(
            f"【槽位管控】卡槽已有 {tray_used}/{TRAY_CAPACITY} 格："
            "第2组凑对后只差一块就能三连，第3组新图案只占用空位，"
            "优先从第2组选择。"
        )

    # 【卡槽紧张·强制消除】卡槽 >= 5/7 且存在可立即三连的砖块时，
    # 必须优先消除——消除腾出 2 格，是唯一能把卡槽往回拉的走法。
    # 实测教训：模型反复在有可消除砖块时去凑对，
    # 把卡槽一步步推到 6/7，最后无路可走（见 trace_20261014_110737）。
    if (
        TIGHT_TRAY_PRIORITY
        and tray_used >= TIGHT_TRAY_PRIORITY_THRESHOLD
        and group1
    ):
        lines.append(
            f"【保命约束·必须优先消除】卡槽已占 {tray_used}/{TRAY_CAPACITY}，"
            f"且第1组有 {len(group1)} 块可立即三连消除的砖。"
            "消除会腾出 2 个空位，是唯一能把卡槽往回拉的走法；"
            "本轮**必须从第1组选择**，不允许凑对或放新图案"
            "（那只会继续占格子，把卡槽推向填满即失败）。"
        )

    print(
        f"[优先级] 卡槽 {len(tray_tiles)}/{TRAY_CAPACITY}"
        f" | 第1组 {len(group1)} 块"
        f" | 第2组 {len(group2)} 块"
        f" | 第3组 {len(group3)} 块"
    )

    # 每轮把分组情况写进日志，方便回查分组是否合理
    append_log(
        f"分析 | 卡槽 {len(tray_tiles)}/{TRAY_CAPACITY}"
        f" | 1组 {len(group1)}"
        f" | 2组 {len(group2)}"
        f" | 3组 {len(group3)}"
    )

    # 记录最高优先级组（原尺寸坐标），供本地强制校验使用
    # LAST_PRIORITY_ORDER 保留本地排序，供"本地兜底"按序挑选
    if LAST_MUST_WAIT:
        top_group = []
    elif group1:
        top_group = group1
    elif group2:
        top_group = group2
    else:
        top_group = group3

    LAST_PRIORITY_TOP = set(p for p, _ in top_group)
    LAST_PRIORITY_ORDER = [p for p, _ in top_group]

    return "\n".join(lines)


def analysis_point_to_model(point):
    """
    原尺寸分析图坐标 -> 模型看到的缩放图坐标。

    用于把本地允许的坐标写进反馈文本，
    保证反馈里的数字和模型看到的图片一致。
    """

    x, y = point

    if MODEL_IMAGE_SCALE == 1.0:
        return x, y

    return (
        round(x * MODEL_IMAGE_SCALE),
        round(y * MODEL_IMAGE_SCALE),
    )


def detect_level_end(image_width, image_height):
    """
    判断这一关是否已经结束（通关/失败结算弹窗出现）。

    只在最近一轮的分析图上做只读判断，不点击任何东西。

    主信号是草地占比：正常对局中草地约占 90%（实测），
    而通关/失败结算弹窗会把整个画面压暗，草地占比骤降。
    注意不能用"检测不到砖块"当主信号——关卡开场动画
    （小羊出场、点击跳过）同样没有砖块，会误判成通关。

    【通关 / 失败归因】弹窗那一帧的画面已经被压暗，卡槽和棋盘都
    检测不到（实测弹窗帧读到的是"卡槽 0/7"），所以判定不能用
    弹窗帧的数字，必须用弹窗出现之前最后一帧正常画面的状态。
    证据链（按可靠性排序）：
      1. 上一次点击后卡槽已达 7 格 -> 槽位被填满，失败
      2. 弹窗前最后一帧卡槽已满 7 格 -> 槽位被填满，失败
      3. 弹窗前最后一帧棋盘已无可点击砖块 -> 全部消除，通关
      4. 以上都不成立 -> 按卡槽占用推断（低置信，描述里注明）

    返回：
        (kind, description)
        kind 为 "cleared" / "failed" / "covered" / None
    """

    # 【通关/失败归因】需要写入"弹窗前的最后稳定状态"
    global LAST_STABLE_VALID
    global LAST_STABLE_TRAY
    global LAST_STABLE_BOARD
    global LAST_SAW_GAMEPLAY

    if not DEBUG_LAST_PATH.exists():
        return None, ""

    try:
        from PIL import Image as PILImage

        image = PILImage.open(DEBUG_LAST_PATH)
        image = image.convert("RGB")
    except Exception:
        return None, ""

    try:
        if image.size != (image_width, image_height):
            return None, ""

        grass_ratio = sample_grass_ratio(image)

        if grass_ratio >= GRASS_RATIO_MIN:
            # 画面还是正常草地：对局进行中（或开场动画），不算结束。
            # 【通关/失败归因】把这一帧记下来当作"弹窗前的最后稳定状态"——
            # 弹窗出现后画面被压暗，卡槽和棋盘都读不到了。
            LAST_STABLE_VALID = True
            LAST_STABLE_TRAY = LAST_TRAY_COUNT
            LAST_STABLE_BOARD = LAST_CLICKABLE_COUNT

            # 【防误判】只要见过非空的棋盘/卡槽，本局就算真正玩过
            if LAST_CLICKABLE_COUNT > 0 or LAST_TRAY_COUNT > 0:
                LAST_SAW_GAMEPLAY = True

            return None, ""

        # ---------- 已经出现结算弹窗，开始归因 ----------

        # 【防误判】本局还没见过正常对局画面（棋盘和卡槽一直是空的）：
        #   - 程序启动时屏幕上残留上一局的结算弹窗；
        #   - 刚点完「重新挑战」，新局还在加载、弹窗淡出未消失。
        # 这两种情况棋盘/卡槽都读不到，绝不能据此判通关或判失败，
        # 统一按 covered 处理（finish_level 记为失败 -> 触发自动推进）。
        if not LAST_SAW_GAMEPLAY:
            return (
                "covered",
                "画面被弹窗覆盖，且本局尚未出现正常对局画面"
                "（疑似开局残留弹窗或重开加载过渡），不判通关/失败",
            )

        ratio = 100 * grass_ratio

        # 弹窗帧读到的是 0/0，所以优先用"弹窗前的稳定状态"；
        # 万一还没记录过稳定状态，才退回用当前帧的数字
        if LAST_STABLE_VALID:
            stable_tray = LAST_STABLE_TRAY
            stable_board = LAST_STABLE_BOARD
        else:
            stable_tray = LAST_TRAY_COUNT
            stable_board = LAST_CLICKABLE_COUNT

        evidence = (
            f"草地占比 {ratio:.0f}%"
            f" | 点击后卡槽 {LAST_TRAY_AFTER_CLICK}"
            f" | 弹窗前卡槽 {stable_tray}/{TRAY_CAPACITY}"
            f" | 弹窗前棋盘剩余 {stable_board} 块"
        )

        # 证据 1：上一次点击执行完之后卡槽已经满格 -> 槽位填满，失败
        if LAST_TRAY_AFTER_CLICK >= TRAY_CAPACITY:
            return (
                "failed",
                f"失败原因：卡槽被填满（{LAST_TRAY_AFTER_CLICK}/"
                f"{TRAY_CAPACITY}）且没有凑成三连。{evidence}",
            )

        # 证据 2：弹窗前的最后一帧卡槽就是满的 -> 失败
        if stable_tray >= TRAY_CAPACITY:
            return (
                "failed",
                f"失败原因：弹窗前卡槽已满（{stable_tray}/"
                f"{TRAY_CAPACITY}）且无法消除。{evidence}",
            )

        # 证据 3：弹窗前棋盘已经没有任何可点击砖块 -> 全部消除，通关
        if stable_board <= 0:
            return (
                "cleared",
                f"通关依据：弹窗前棋盘已无可点击砖块"
                f"（卡槽 {stable_tray}/{TRAY_CAPACITY}），"
                f"全部砖块已消除。{evidence}",
            )

        # 证据 4：信息不足，按卡槽占用推断（低置信）
        if stable_tray >= TRAY_CAPACITY - 1:
            return (
                "failed",
                f"失败原因（推断）：弹窗前卡槽已占 {stable_tray}/"
                f"{TRAY_CAPACITY}，处于填满边缘。{evidence}",
            )

        return (
            "cleared",
            f"通关依据（推断）：弹窗前棋盘还剩 {stable_board} 块、"
            f"卡槽仅 {stable_tray}/{TRAY_CAPACITY}，"
            f"占用很低时出现的结算界面通常是通关。{evidence}",
        )
    finally:
        image.close()

    return None, ""


def force_end_round():
    """
    卡槽只剩最后一个空位、棋盘上又没有"点了能三连/凑对"的砖块时，
    主动点一块砖把卡槽填满，让游戏弹出失败结算、进入下一把。

    为什么必须这么做：
        - 本程序**禁止使用道具**，所以没法像以前那样"等人工用
          移出/撤回/洗牌腾位置"；
        - 游戏只有"点击把卡槽填满"才会弹失败结算，干等的话画面
          停在残局，根本没有「重新挑战」按钮可点；
        - 既然这一局在数学上已经无法挽回（点任何一块都会满槽），
          不如立刻结束、把时间留给下一把。

    返回：
        True  = 已经点下去（本局接下来会弹失败结算）
        False = 连一块可点的砖都找不到（画面异常，交由上层继续等待）
    """

    global LAST_TRAY_AFTER_CLICK
    global LAST_FORCED_END_COUNT

    if not LAST_CANDIDATES:
        return False

    if not LAST_ANALYSIS_SIZE:
        return False

    image_width, image_height = LAST_ANALYSIS_SIZE

    # 从候选里挑一块没被保护的砖。既然点哪块都是满槽，
    # 就优先挑"解放分"最高的那块——纯属顺手，反正都会失败。
    candidates = [c for c in LAST_CANDIDATES if not c.get("safe")]

    if not candidates:
        return False

    target = max(candidates, key=lambda c: c.get("unlock", 0))
    ax, ay = target["analysis_xy"]

    LAST_FORCED_END_COUNT += 1

    print(
        f"\n[主动结束] 卡槽已占 {LAST_TRAY_COUNT}/{TRAY_CAPACITY}，"
        f"没有可三连/凑对的砖、且禁止使用道具 —— "
        f"主动点击 ({ax}, {ay}) 把卡槽填满，结束本局"
        f"（第 {LAST_FORCED_END_COUNT}/{FORCED_END_MAX_PER_ROUND} 次）"
    )

    append_log(
        f"主动结束 | 无安全走法且禁用道具，点击({ax}, {ay})结束本局"
        f"（第 {LAST_FORCED_END_COUNT} 次）"
    )

    screen_x, screen_y = screenshot_to_screen(
        ax, ay, image_width, image_height
    )

    pyautogui.click(x=screen_x, y=screen_y)

    # 这一击必然把卡槽填满（候选全是"新图案"），
    # 记下来供失败归因使用（证据 1：点击后卡槽 >= 7 → failed）
    LAST_TRAY_AFTER_CLICK = TRAY_CAPACITY

    # 记入轨迹，但用 feedback 标记为"主动结束"，
    # 数据集构建时不会把它当成模型的可训练决策帧
    record_step(
        "(主动结束本局)",
        "卡槽只剩 1 个空位且无安全走法，禁止使用道具，主动点砖结束本局",
        [ax, ay],
        "forced_end: 主动结束本局，等待失败结算",
        LAST_TRAY_COUNT,
        attempt=RECLICK_ATTEMPTS + 1,
        feedback="forced_end",
    )

    return True


def should_auto_restart(end_kind, episode_count):
    """
    判断本局结束后是否应该调用重开脚本、继续采集下一局。

    能走到这里的都是已经确认结束的局：
        "failed"  —— 失败结算页
        "cleared" —— 棋盘全部消除，通关
        "covered" —— 画面被弹窗盖住、本局有没有真玩过不确定
                     （开局残留弹窗 / 重开加载过渡）

    三种都交给 restart_game.py 处理：它自己找按钮、认不出就问模型，
    点完还会验证新局是否真的开出来，所以误点的风险已经被兜住，
    不需要"通关后一律停手"这种保守策略（那会打断连续采集）。

    AUTO_RESTART_ON_FAIL 这个变量名是历史沿用，
    它实际控制的是"所有结束后的自动重开"。

    达到 RESTART_MAX_EPISODES 上限时不再重开。
    """

    if not AUTO_RESTART_ON_FAIL:
        return False

    if end_kind not in ("failed", "cleared", "covered"):
        return False

    if RESTART_MAX_EPISODES > 0 and episode_count >= RESTART_MAX_EPISODES:
        return False

    return True


def finish_level(kind, description):
    """
    关卡结束时收尾：保存结束画面、写日志、打印结论。

    通关 / 失败的区分依据（详见 detect_level_end）：
        通关 = 棋盘砖块被全部消除（卡槽不会满）
        失败 = 卡槽被填满且没有凑成三连
    """

    # 【通关/失败归因】结束后重置归因状态，避免下一局沿用上一局的证据
    # ⚠️ 这四个都必须显式 global：2026-10-14 的死循环就是
    # LAST_FORCED_END_COUNT / LAST_SAW_GAMEPLAY 漏了声明，
    # 赋值只创建了局部变量，全局计数器跨局从不清零 ——
    # 主动结束预算在 3 局之内被耗尽，之后每局卡槽 6/7 死局都无限干等。
    global LAST_TRAY_AFTER_CLICK
    global LAST_STABLE_VALID
    global LAST_SAW_GAMEPLAY
    global LAST_FORCED_END_COUNT

    print("\n" + "=" * 55)
    print("[结束] 本关已结束，自动停止操作")
    print("=" * 55)
    print(f"判定类型：{kind}")
    print(f"判定依据：{description}")

    if kind == "cleared":
        print("结论：通关 🎉 —— 棋盘砖块已全部消除")
    elif kind == "failed":
        print("结论：失败 —— 卡槽被填满且没有凑成三连")
    elif kind == "stuck":
        print(
            "结论：失败 —— 卡槽将满且没有任何可消除的砖块，"
            "已无路可走"
        )
    else:
        print("结论：画面被弹窗覆盖，本局已结束（归因证据不足）")

    try:
        if DEBUG_LAST_PATH.exists():
            LEVEL_END_PATH.write_bytes(DEBUG_LAST_PATH.read_bytes())
            print(f"结束画面已保存：{LEVEL_END_PATH}")
    except Exception as exc:
        print(f"[警告] 保存结束画面失败：{exc}")

    append_log(f"结束 | 判定={kind} | {description}")

    # 【修改点·需求3】本局结束（通关/卡死）：
    # 判定是否通关并输出完整轨迹 JSON，供后续 DPO 样本采集。
    # 【通关/失败归因】只有 kind == "cleared" 才算通关；
    # failed / stuck / covered 一律按失败记录，并写入具体原因。
    cleared = kind == "cleared"

    fail_reason = "" if cleared else description

    print(
        f"本局结果：{'通关' if cleared else '未通关'}"
        f" | 写入轨迹：total_steps={len(TRACE_STEPS)}"
    )

    # 【防误判配套】一步都没记录的局（开局残留弹窗 / 重开加载过渡
    # 被判 covered 的"幽灵局"）没有训练价值，跳过落盘，
    # 避免数据集构建时出现一堆"没有任何可训练步"的坏轨迹。
    if not TRACE_STEPS:
        print(
            "[轨迹] 本局没有任何可训练步（幽灵局），跳过保存轨迹 JSON"
        )
        append_log("轨迹 | 本局 0 步，跳过保存")
        # 顺带清掉这次 start_trace 建出来的空帧目录，
        # 否则每遇到一次"幽灵局"就多留一个空目录
        cleanup_unsaved_frame_dir()
    else:
        save_trace(
            cleared=cleared,
            fail_reason=fail_reason,
            stop_kind=kind,
        )

    # 【修改点·需求5】本局结束，清空对话上下文 / 轨迹缓存，
    # 避免多局串行采样时内存持续上涨
    reset_game_context()

    # 归因状态清零（下一局重新累积"是否见过正常对局画面"的证据，
    # 否则上一局的痕迹会让新一局的加载过渡期被误判通关/失败）
    LAST_TRAY_AFTER_CLICK = -1
    LAST_STABLE_VALID = False
    LAST_SAW_GAMEPLAY = False
    # 【主动结束】计数按局重置
    LAST_FORCED_END_COUNT = 0


def lookup_click_kind(x, y):
    """
    查这一坐标点下去属于哪种卡槽变化类型：
    "eliminate"（三连净减 2 格）/ "pair" / "new"（净加 1 格）/ None。

    两级匹配：
    1. 先按砖块中心做容差匹配（正常情况：模型直接抄清单里的中心坐标）；
    2. 再按砖块包围盒匹配（兜住"坐标落在这块砖的卡面里但不在中心"，
       这种情况只按中心匹配会漏判，把危险砖误当安全砖）。
    落在多块砖的包围盒里时，取中心离点击点最近的那块。
    """

    for (kx, ky), kind in LAST_CLICK_KIND.items():
        if (
            abs(kx - x) <= PRIORITY_TOLERANCE
            and abs(ky - y) <= PRIORITY_TOLERANCE
        ):
            return kind

    best = None
    best_dist = None

    for min_x, min_y, max_x, max_y, kind in LAST_CLICK_BBOX:
        if not (min_x <= x <= max_x and min_y <= y <= max_y):
            continue

        cx = (min_x + max_x) // 2
        cy = (min_y + max_y) // 2
        dist = (cx - x) ** 2 + (cy - y) ** 2

        if best_dist is None or dist < best_dist:
            best, best_dist = kind, dist

    return best


def find_clicked_tile_center(x, y):
    """
    找到点击点对应的候选砖中心（原尺寸分析图坐标）。

    两级匹配（与遮挡校验同一思路）：
    1. 先按砖块中心做容差匹配；
    2. 再按砖块包围盒匹配（兜住"落点在卡面内但不在中心"）。
    落在多块砖的包围盒里时，取中心离点击点最近的那块。
    找不到返回 None。
    """

    for kx, ky in LAST_CLICK_KIND:
        if (
            abs(kx - x) <= PRIORITY_TOLERANCE
            and abs(ky - y) <= PRIORITY_TOLERANCE
        ):
            return (kx, ky)

    best = None
    best_dist = None

    for min_x, min_y, max_x, max_y, _kind in LAST_CLICK_BBOX:
        if not (min_x <= x <= max_x and min_y <= y <= max_y):
            continue

        cx = (min_x + max_x) // 2
        cy = (min_y + max_y) // 2
        dist = (cx - x) ** 2 + (cy - y) ** 2

        if best_dist is None or dist < best_dist:
            best, best_dist = (cx, cy), dist

    return best


def check_priority_choice(x, y):
    """
    本地校验：模型选的坐标是否可以安全点击。

    返回：
        (True, "")：允许点击
        (False, 原因)：拒绝并反馈给模型

    语义说明：
    原来这里是"贪心强校验"——只要存在可立即消除的砖块，
    就必须从最高优先级组里选，否则直接拒绝。这正好是被禁用的
    贪心策略，会让模型的"长远解放"选择永远无法执行。
    现在改成：
    1. 安全底线（不受开关影响）：
       1.1 槽位硬保护 + 必败预判——点下去会把卡槽推入必败状态
           （三连前槽位溢出，或危险区里可见同款凑不齐三连），
           与 build_ranked_hint 的剔除结果（LAST_TRAY_DANGER）完全一致；
       1.2 卡槽将满且无可消除时只能等待；
    2. 其余情况不再强制组顺序（PRIORITY_ENFORCE 默认 False），
       策略交给 System Prompt 里的长远推理。
    """

    # 1.1 【槽位硬保护 + 必败预判】点下去必败的砖块，
    #     与提示清单的"槽位保护·禁止选择"保持同一份判定结果。
    #     判定同时覆盖"中心容差命中"和"落点在卡面内"两种坐标误差。
    if TRAY_HARD_GUARD:
        blocked_by_slot = False
        tile_center = find_clicked_tile_center(x, y)

        if tile_center is not None:
            blocked_by_slot = tile_center in LAST_TRAY_DANGER
        elif LAST_TRAY_COUNT >= TRAY_CAPACITY - 1:
            # 认不出这块砖属于哪种图案，而卡槽只剩 1 个空位：
            # 赌错的代价是直接失败，所以按"不安全"处理
            blocked_by_slot = True

        if blocked_by_slot:
            allowed_pairs = [
                analysis_point_to_model(p)
                for p in LAST_PRIORITY_ORDER[:6]
            ]

            if allowed_pairs:
                allowed_text = "、".join(
                    f"({mx}, {my})" for mx, my in allowed_pairs
                )
                allowed_text = f"本轮只能从这些坐标里选：{allowed_text}"
            else:
                allowed_text = "本轮只能输出 点击坐标：wait"

            return False, (
                f"槽位保护：卡槽已有 {LAST_TRAY_COUNT}/{TRAY_CAPACITY} 格，"
                "点这块砖会把卡槽推入必败状态"
                "（凑齐三连前槽位溢出，或可见同款已凑不齐三连），"
                f"卡槽填满即失败。{allowed_text}"
            )

    # 1.2 安全底线：卡槽将满且没有任何可立即消除的砖块时，
    #     任何点击都会直接导致失败，这条硬性保留。
    if LAST_MUST_WAIT:
        if LAST_WAIT_REASON:
            return False, f"{LAST_WAIT_REASON}，必须输出 点击坐标：wait"

        return False, (
            "卡槽即将满且没有可立即消除的砖块，"
            "点击任何砖块都会导致失败，必须输出 点击坐标：wait"
        )

    # 2.【卡槽紧张·强制"能消就先消"】
    #     卡槽 >= 5/7 且本轮存在可立即三连的砖块时，必须优先消除：
    #     消除能腾出 2 格，是唯一能把卡槽往回拉的走法；
    #     此时凑对/放新图案只会继续占用格子，把局面推向 6/7 死线。
    #     这是"保命"级别的约束，不是贪心偏好，所以不受
    #     PRIORITY_ENFORCE（长远推演开关）影响。
    if (
        TIGHT_TRAY_PRIORITY
        and LAST_TRAY_COUNT >= TIGHT_TRAY_PRIORITY_THRESHOLD
        and LAST_ELIMINATE
    ):
        in_eliminate = any(
            abs(ex - x) <= PRIORITY_TOLERANCE
            and abs(ey - y) <= PRIORITY_TOLERANCE
            for ex, ey in LAST_ELIMINATE
        )

        if not in_eliminate:
            allowed_pairs = [
                analysis_point_to_model(p)
                for p in sorted(LAST_ELIMINATE)[:8]
            ]

            allowed_text = "、".join(
                f"({mx}, {my})" for mx, my in allowed_pairs
            )

            return False, (
                f"卡槽已占 {LAST_TRAY_COUNT}/{TRAY_CAPACITY}，"
                "而本轮有可以立即三连消除的砖块（消除能腾出 2 格空位）。"
                "卡槽紧张时必须先消除，不允许再凑对或放新图案占格子。"
                f"本轮只能从这些坐标里选：{allowed_text}"
            )

    # 3. 【修改点·需求1】贪心组顺序强校验：默认已关闭
    if not PRIORITY_ENFORCE:
        return True, ""

    if not LAST_PRIORITY_TOP:
        return True, ""

    for top_x, top_y in LAST_PRIORITY_TOP:
        if (
            abs(top_x - x) <= PRIORITY_TOLERANCE
            and abs(top_y - y) <= PRIORITY_TOLERANCE
        ):
            return True, ""

    # 拒绝时把允许的坐标直接列给模型，
    # 减少模型"不知道该选哪个"的反复违规
    allowed_pairs = [
        analysis_point_to_model(p)
        for p in LAST_PRIORITY_ORDER[:5]
    ]

    allowed_text = "、".join(
        f"({mx}, {my})" for mx, my in allowed_pairs
    )

    return False, (
        "没有遵守消除优先级：存在更高优先级的可消除砖块。"
        f"你只能从这些坐标中选择（本图坐标）：{allowed_text}"
    )


def pick_local_fallback():
    """
    模型连续不配合时的本地兜底选择。

    按本地排好的优先级顺序（LAST_PRIORITY_ORDER）逐个尝试，
    返回第一个能通过遮挡检查的坐标 (x, y)；都不可用时返回 None。

    该函数只读最近一轮的分析图，不做任何点击。
    """

    if not LAST_PRIORITY_ORDER:
        return None

    if not DEBUG_LAST_PATH.exists():
        return None

    try:
        from PIL import Image as PILImage

        image = PILImage.open(DEBUG_LAST_PATH)
        image = image.convert("RGB")
    except Exception:
        return None

    try:
        for x, y in LAST_PRIORITY_ORDER:
            ok, _ = check_tile_clickable(image, x, y)

            if ok:
                return (x, y)
    finally:
        image.close()

    return None


def check_tile_clickable(image, x, y):
    """
    检查模型选择的位置是否位于一张完整、可点击的卡面上。

    返回：
        (True, "")：可以点击
        (False, 原因)：不可点击
    """

    pixels = image.load()
    width, height = image.size

    if not (0 <= x < width and 0 <= y < height):
        return False, "坐标超出画面范围"

    # 1. 在点击点附近寻找米白色卡面像素作为种子。
    #    注意：部分关卡的砖块图案本身就是浅黄色（太阳、钻石、
    #    奖牌等），图案上的浅黄纹路同样能通过米白判定，
    #    会形成 1x1、3x10 这类孤立的假卡面碎片。
    #    所以不能拿到第一个种子就用，必须逐个种子泛洪，
    #    直到泛洪出满足最小尺寸的完整卡面为止。
    tried = set()

    min_x = min_y = max_x = max_y = 0
    face_found = False

    for radius in FACE_SEARCH_RADII:
        if face_found:
            break

        for dy in range(-radius, radius + 1, 2):
            if face_found:
                break

            for dx in range(-radius, radius + 1, 2):
                sx = x + dx
                sy = y + dy

                if not (0 <= sx < width and 0 <= sy < height):
                    continue

                if (sx, sy) in tried:
                    continue

                if not is_cream_face(pixels[sx, sy]):
                    continue

                (
                    cand_min_x,
                    cand_min_y,
                    cand_max_x,
                    cand_max_y,
                    members,
                ) = find_face_component(image, sx, sy)

                tried |= members

                cand_width = cand_max_x - cand_min_x + 1
                cand_height = cand_max_y - cand_min_y + 1

                if (
                    cand_width >= FACE_MIN_WIDTH
                    and cand_height >= FACE_MIN_HEIGHT
                ):
                    min_x, min_y = cand_min_x, cand_min_y
                    max_x, max_y = cand_max_x, cand_max_y
                    face_found = True
                    break

    if not face_found:
        return False, (
            "点击点周围没有完整卡面"
            "（被压住的砖块、卡槽或空白区域）"
        )

    face_width = max_x - min_x + 1
    face_height = max_y - min_y + 1

    # 3. 点击点必须落在卡面包围盒内
    inside_x = min_x - BBOX_MARGIN <= x <= max_x + BBOX_MARGIN
    inside_y = min_y - BBOX_MARGIN <= y <= max_y + BBOX_MARGIN

    if not (inside_x and inside_y):
        return False, (
            "点击点位于卡面之外，"
            "目标砖块被上层压住，只露出了一部分"
        )

    # 4. 卡槽里的砖块也会形成完整卡面，但它们固定在画面
    #    底部的卡槽面板里。用位置排除，避免反复点击卡槽里
    #    已收集的砖块导致卡死。
    #    注意不能用"旁边有没有草地"判断——密集堆叠关卡里
    #    大部分棋盘砖左右都是邻居，没有草地。
    if y >= height * TRAY_BAND_RATIO:
        return False, (
            "该位置在底部卡槽内，"
            "卡槽里已收集的砖块无法再次点击，"
            "只能点击棋盘上的砖块"
        )

    return True, ""


def verify_click_on_face(x, y, image_width, image_height):
    """
    加载本轮分析图并执行遮挡检查。

    分析图不可用时放行（不阻塞正常流程）。
    """

    if not CHECK_OCCLUSION:
        return True, ""

    if not DEBUG_LAST_PATH.exists():
        print("[警告] 找不到本轮分析图，跳过遮挡检查")
        return True, ""

    try:
        from PIL import Image as PILImage

        image = PILImage.open(DEBUG_LAST_PATH)
        image = image.convert("RGB")
    except Exception as exc:
        print(f"[警告] 读取分析图失败，跳过遮挡检查：{exc}")
        return True, ""

    if image.size != (image_width, image_height):
        image.close()
        print("[警告] 分析图尺寸与本轮截图不一致，跳过遮挡检查")
        return True, ""

    try:
        result = check_tile_clickable(image, int(x), int(y))
    finally:
        image.close()

    return result


# ============================================================
# 10. 将截图坐标转换成鼠标坐标
# ============================================================

import pyautogui

def screenshot_to_screen(x, y, screenshot_width, screenshot_height):
    """
    将模型输出的截图坐标转换为 PyAutoGUI 屏幕坐标。

    参数：
        x, y：模型输出的截图坐标
        screenshot_width：当前截图宽度
        screenshot_height：当前截图高度

    返回：
        screen_x, screen_y：实际鼠标坐标（逻辑点）

    两种情况：
    1. 截图被裁剪成了游戏窗口区域：
       模型坐标对应窗口内部的像素，
       先按窗口尺寸换算，再加上窗口在屏幕上的偏移。
    2. 截图是全屏：
       macOS Retina 屏幕上截图是物理像素（例如 3024×1964），
       pyautogui 鼠标坐标是逻辑点（例如 1512×982），
       两者只差一个缩放系数，换算后不能再额外乘倍数。
    """

    # 获取 PyAutoGUI 的屏幕尺寸（逻辑点）
    screen_width, screen_height = pyautogui.size()

    if LAST_WINDOW_RECT is not None:
        # 情况 1：截图是游戏窗口的裁剪图
        win_left, win_top, win_width, win_height = LAST_WINDOW_RECT

        scale_x = win_width / screenshot_width
        scale_y = win_height / screenshot_height

        screen_x = round(win_left + x * scale_x)
        screen_y = round(win_top + y * scale_y)

        scale_desc = (
            f"窗口偏移 ({win_left}, {win_top})，"
            f"窗口内缩放 ({scale_x:.4f}, {scale_y:.4f})"
        )

    else:
        # 情况 2：截图是全屏
        scale_x = screen_width / screenshot_width
        scale_y = screen_height / screenshot_height

        screen_x = round(x * scale_x)
        screen_y = round(y * scale_y)

        scale_desc = f"全屏缩放 ({scale_x:.4f}, {scale_y:.4f})"

    # 限制在屏幕有效范围内
    screen_x = max(0, min(screen_x, screen_width - 1))
    screen_y = max(0, min(screen_y, screen_height - 1))

    # 打印调试信息
    print(
        f"[坐标调试] 截图尺寸："
        f"{screenshot_width} × {screenshot_height}"
    )
    print(
        f"[坐标调试] 屏幕尺寸："
        f"{screen_width} × {screen_height}"
    )
    print(
        f"[坐标调试] 模型坐标：({x}, {y})"
    )
    print(f"[坐标调试] {scale_desc}")
    print(
        f"[坐标调试] 最终坐标："
        f"({screen_x}, {screen_y})"
    )
    print(
        "[坐标调试] 说明：模型坐标是发给模型的截图的像素，"
        "最终坐标是屏幕逻辑点，两者本就不是同一个数。"
    )

    if abs(scale_x - scale_y) > 0.02:
        print(
            "[警告] 横纵缩放比例不一致，"
            "请检查是否使用了多显示器或非整数缩放。"
        )

    # 注意：这里必须直接返回换算结果，不能乘 2
    return screen_x, screen_y


def save_debug_marker(screen_x, screen_y):
    """
    在调试图上标出实际点击位置，方便人工核对。

    调试图是发给模型的裁剪截图（或全屏截图），
    需要把鼠标逻辑坐标换算回调试图的像素坐标再标注。
    """

    if not DEBUG_LAST_PATH.exists():
        return

    try:
        from PIL import Image, ImageDraw

        with Image.open(DEBUG_LAST_PATH) as raw:
            image = raw.convert("RGB")

        if LAST_WINDOW_RECT is not None:
            win_left, win_top, win_width, win_height = LAST_WINDOW_RECT

            pixel_x = (screen_x - win_left) * image.width / win_width
            pixel_y = (screen_y - win_top) * image.height / win_height

        else:
            screen_width, screen_height = pyautogui.size()

            pixel_x = screen_x * image.width / screen_width
            pixel_y = screen_y * image.height / screen_height

        draw = ImageDraw.Draw(image)
        radius = 40

        draw.line(
            (pixel_x - radius, pixel_y, pixel_x + radius, pixel_y),
            fill=(255, 0, 0),
            width=6,
        )
        draw.line(
            (pixel_x, pixel_y - radius, pixel_x, pixel_y + radius),
            fill=(255, 0, 0),
            width=6,
        )
        draw.ellipse(
            (
                pixel_x - radius,
                pixel_y - radius,
                pixel_x + radius,
                pixel_y + radius,
            ),
            outline=(255, 0, 0),
            width=6,
        )

        image.save(DEBUG_CLICK_PATH, format="PNG")
        image.close()

        print(f"[调试] 点击位置标记图已保存：{DEBUG_CLICK_PATH}")

    except Exception as exc:
        print(f"[警告] 生成坐标标记图失败：{exc}")

def append_log(text):
    """
    把一轮的关键信息追加到日志文件，方便事后排查。

    时间戳带日期（YYYY-MM-DD HH:MM:SS）：日志是跨天追加的同一份文件，
    只写时分秒的话，几天的采集记录混在一起根本对不上是哪天，
    事后想把某一局的时间点和 traces/ 里的轨迹对齐就无从下手。
    """
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n"
            )
    except Exception:
        pass


# ============================================================
# 11. 执行点击
# ============================================================

def execute_action(data, image_width, image_height):
    """
    自动执行模型建议的操作。

    返回：
        (status, reason)
        status 为 "clicked" / "wait" / "blocked"
        reason 为 blocked 时的具体原因，用于反馈给模型重新选择
    """

    action = data.get("action")

    # 【槽位硬保护 + 失败归因】需要更新"这一击之后卡槽会变成几格"
    global LAST_TRAY_AFTER_CLICK

    print("\n" + "=" * 45)
    print("AI 分析结果")
    print("=" * 45)

    print("操作：", action)
    print("目标方块：", data.get("tile", "未知"))
    print("选择原因：", data.get("reason", "未提供"))
    print("置信度：", data.get("confidence", "未知"))
    print("卡槽：", data.get("tray", []))

    if action == "wait":
        print("[操作] AI 建议等待")
        time.sleep(1)
        return "wait", ""

    if action == "stop":
        print("[操作] AI 要求停止，结束程序")
        raise SystemExit(0)

    # 验证操作和坐标
    valid, invalid_reason = validate_action(
        data, image_width, image_height
    )

    if not valid:
        print(f"[安全] 本轮操作无效：{invalid_reason}")
        append_log(
            f"跳过 | action={action}"
            f" | 模型({data.get('x')}, {data.get('y')})"
            f" | {invalid_reason}"
        )
        time.sleep(1)
        return "blocked", invalid_reason

    x = data["x"]
    y = data["y"]

    # 【禁止使用道具】卡槽下方是"移出 / 撤回 / 洗牌"道具区。
    # 落在道具区的点击一律拒绝：本 Agent 不允许使用道具，
    # 而且这些按钮一点就可能改变局面、破坏数据可复现性。
    if FORBID_PROPS and LAST_TOOLBAR_TOP and y >= LAST_TOOLBAR_TOP:
        reason = (
            "该坐标位于卡槽下方的道具区（移出/撤回/洗牌），"
            "本程序禁止使用道具，请改选棋盘上的砖块坐标"
        )
        print(f"[道具禁用] 拒绝点击：{reason}")
        append_log(f"跳过(道具) | 模型({x}, {y}) | {reason}")
        time.sleep(1)
        return "blocked", reason

    # 遮挡检查：确认点击点位于完整卡面上（没有被上层压住）
    clickable, block_reason = verify_click_on_face(
        x, y, image_width, image_height
    )

    if not clickable:
        print(f"[安全] 目标不可点击：{block_reason}")
        append_log(
            f"跳过(遮挡) | 模型({x}, {y}) | {block_reason}"
        )
        time.sleep(1)
        return "blocked", block_reason

    # 消除优先级检查：存在可立即消除的砖块时，
    # 不允许模型去点优先级更低的砖块
    allowed, priority_reason = check_priority_choice(x, y)

    if not allowed:
        print(f"[优先级] 拒绝：{priority_reason}")
        append_log(
            f"跳过(优先级) | 模型({x}, {y})"
            f" | {priority_reason}"
        )
        time.sleep(1)
        return "blocked", priority_reason

    screen_x, screen_y = screenshot_to_screen(
    x, y, image_width, image_height
    )

    print(f"[自动操作] 截图坐标：({x}, {y})")
    print(f"[自动操作] 鼠标坐标：({screen_x}, {screen_y})")

    append_log(
        f"点击 | 模型({x}, {y})"
        f" -> 鼠标({screen_x}, {screen_y})"
        f" | tile={data.get('tile')}"
        f" | conf={data.get('confidence')}"
    )

    # 在调试图上标出实际点击位置
    if DEBUG_ENABLED:
        save_debug_marker(screen_x, screen_y)

    pyautogui.click(x=screen_x, y=screen_y)

    # 【槽位硬保护 + 失败归因】算一下这一击之后卡槽的格数：
    #   三连消除净减 2 格（先 +1 再 -3），其余净加 1 格。
    # 这个值 >= TRAY_CAPACITY 说明刚把卡槽填满且没消除，
    # 本关必然失败——弹窗出现时用它把"槽位满失败"和"消除完通关"区分开。
    click_kind = lookup_click_kind(x, y)

    if click_kind == "eliminate":
        LAST_TRAY_AFTER_CLICK = LAST_TRAY_COUNT - 2
    elif click_kind in ("pair", "new"):
        LAST_TRAY_AFTER_CLICK = LAST_TRAY_COUNT + 1
    else:
        LAST_TRAY_AFTER_CLICK = -1

    print(
        f"[槽位] 点击类型：{click_kind or '未知'}"
        f" | 卡槽 {LAST_TRAY_COUNT} -> {LAST_TRAY_AFTER_CLICK}"
        f"（上限 {TRAY_CAPACITY}）"
    )

    if LAST_TRAY_AFTER_CLICK >= TRAY_CAPACITY:
        print(
            f"[槽位] 危险：这一击后卡槽达 "
            f"{LAST_TRAY_AFTER_CLICK}/{TRAY_CAPACITY} "
            "且未凑成三连，本关即将失败"
        )

        append_log(
            f"槽位危险 | 点击后卡槽 {LAST_TRAY_AFTER_CLICK}"
            f"/{TRAY_CAPACITY} 且无三连，本关即将失败"
        )

    print("[自动操作] 点击完成")

    # 等待游戏动画和状态更新
    time.sleep(CLICK_DELAY)

    return "clicked", ""


# ============================================================
# 12. 主程序
# ============================================================


# ============================================================
# 12.1 单实例锁：同一时间只允许一个 Agent 进程操作游戏
# ============================================================
#
# 之前出现过多个 Agent 进程同时运行的事故：每个进程都在截图、
# 都在点击、都在判断弹窗，互相把对方的点击点到错误的界面上
# （甚至点出调试菜单），轨迹数据也全部作废。
# 用锁文件 + PID 存活检查保证全局只有一个实例。

AGENT_LOCK_PATH = PROJECT_DIR / "agent.lock"


def acquire_single_instance_lock():
    """
    尝试获取单实例锁。

    返回：
        (True, pid)  = 获取成功，可以运行
        (False, pid) = 已有其他实例在运行（pid 为对方的进程号）
    """

    if AGENT_LOCK_PATH.exists():
        try:
            other_pid = int(AGENT_LOCK_PATH.read_text().strip())

            if other_pid == os.getpid():
                # 锁文件记录的就是本进程：重复进入 main 的场景，放行
                return True, other_pid

            # 向该进程发 0 号信号：进程存在则不报错，不存在则抛异常
            os.kill(other_pid, 0)

            return False, other_pid
        except (ValueError, ProcessLookupError, TypeError):
            # 锁文件损坏 / 进程已退出：属残留锁，直接接管
            print(
                f"[锁] 发现残留锁文件（进程已不存在），接管："
                f"{AGENT_LOCK_PATH}"
            )
        except PermissionError:
            return False, -1

    AGENT_LOCK_PATH.write_text(str(os.getpid()))

    return True, os.getpid()


def release_single_instance_lock():
    """释放单实例锁（仅当锁文件记录的是本进程时才删除）。"""

    try:
        if AGENT_LOCK_PATH.exists() and AGENT_LOCK_PATH.read_text().strip() == str(os.getpid()):
            AGENT_LOCK_PATH.unlink()
    except OSError:
        pass


def run_restart_script(end_kind=""):
    """
    本局结束后，调用独立的 restart_game.py 完成"找按钮 + 开新局"。

    本 Agent 只负责玩游戏：一旦判定本局结束（失败 / 通关 / 画面被
    弹窗盖住），或遇到认不出来的界面，就把"重开"整件事交给
    restart_game.py —— 它在自己的进程里截图、找按钮（本地像素优先，
    认不出来问模型）、点击，并验证新一局是否真的开出来了。

    退出码约定（见 restart_game.py）：
        0 = 已成功开启新一局
        2 = 推进不了（认不出界面 / 点了没反应），需要人工处理
        其他 = 异常退出

    子进程的输出直接接到当前终端，方便旁观重开过程。

    返回：
        (True, "")           —— 新一局已经开出来，调用方可以继续采集
        (False, 停机原因)     —— 重开失败，调用方带原因停机
    """

    script_path = PROJECT_DIR / RESTART_SCRIPT_NAME

    if not script_path.exists():
        reason = f"找不到重开脚本 {script_path}，无法自动开启下一局"
        print(f"\n[重开] {reason}")
        append_log(f"重开 | {reason}")
        return False, reason

    cmd = [sys.executable, str(script_path)]

    if end_kind:
        cmd += ["--end-kind", str(end_kind)]

    print(
        f"\n[重开] 本局已结束（判定={end_kind or '未知'}），"
        f"交给独立脚本处理：{RESTART_SCRIPT_NAME}"
    )
    append_log(f"重开 | 调用 {RESTART_SCRIPT_NAME}（判定={end_kind or '未知'}）")

    try:
        result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        reason = f"启动重开脚本失败：{type(exc).__name__}: {exc}"
        print(f"\n[重开] {reason}")
        append_log(f"重开 | {reason}")
        return False, reason

    if result.returncode == 0:
        print(f"\n[重开] {RESTART_SCRIPT_NAME} 已开启新一局")
        append_log("重开 | 成功，新一局已开启")
        return True, ""

    if result.returncode == RESTART_EXIT_CANNOT_ADVANCE:
        reason = (
            f"{RESTART_SCRIPT_NAME} 无法开启下一局"
            "（认不出结束界面，或点了按钮却没能进入新局），需要人工处理"
        )
    else:
        reason = (
            f"{RESTART_SCRIPT_NAME} 异常退出"
            f"（退出码 {result.returncode}），需要人工检查"
        )

    print(f"\n[重开] {reason}")
    append_log(f"重开 | 失败：{reason}")

    return False, reason


def main():
    print("=" * 55)
    print("微信小游戏 GUI Agent")
    print("模式：全自动（单模型 Qwen3-VL：看图 + 推理）")
    print("截图：每轮截取游戏窗口区域")
    print("输出：思考 + 点击坐标（正则解析）")
    print("清理：每轮请求后删除临时截图")
    print("职责：只负责玩游戏；本局结束后自动调用重开脚本")
    print(f"      重开脚本：{RESTART_SCRIPT_NAME}")
    print("      找按钮 / 问模型 / 验证新局都是它的事")
    print("轨迹：每局结束自动保存 JSON（DPO 样本）")
    print("=" * 55)

    # 【单实例】拿到锁才允许开跑，防止多进程互相干扰
    locked, lock_pid = acquire_single_instance_lock()

    if not locked:
        print(
            f"\n[拒绝启动] 已有另一个 Agent 进程在运行"
            f"（PID {lock_pid}）。"
            "多个实例会互相乱点，请先关掉旧的再启动。"
        )
        sys.exit(2)

    import atexit

    # 【收尾兜底】再挂一道保险：即使 main 是被异常/提前 return 带走的、
    # 走不到循环后面那句 finalize，退出时也会补一次。
    # atexit 是 LIFO，所以它会先于锁释放执行。
    atexit.register(finalize_interrupted_episode)
    atexit.register(release_single_instance_lock)

    print("\n请确认：")
    print("1. 微信小游戏《羊了个羊》已经打开。")
    print("2. 游戏画面处于可见状态，没有被其他窗口遮挡。")
    print("3. LM Studio 已启动，并已加载 Qwen3-VL。")
    print("\n程序将在 3 秒后开始自动操作。")
    print("紧急停止：将鼠标移至屏幕左上角，或按 Ctrl+C。")

    time.sleep(3)

    round_number = 0

    # 【修改点·需求3】开始本局：初始化对局轨迹
    start_trace()

    # 【修改点·需求5】运行约束：
    # 本程序单线程串行运行，一个进程只采样一局，不做并行多局采样；
    # 每局结束后由 finish_level() 清空对话上下文与轨迹缓存，
    # 因此不存在多局并发或跨局长跑导致的内存上涨。

    # 连续判定"本关已结束"的轮数，用于过滤动画等瞬时状态
    end_confirm_rounds = 0
    # 卡槽将满、本地要求 wait 的连续等待轮数（仅用于日志提示，不再停机）
    wait_rounds = 0
    # 已确认本关结束，需要在外层循环退出
    stop_requested = False
    # 【自动推进】本进程已完成的局数（含第一局）与最近一局的结束类型
    episode_count = 1
    last_end_kind = None
    # 【未知界面兜底】连续读不到砖块的轮数 / 本局已求助模型的次数
    unknown_rounds = 0
    unknown_asks = 0

    while True:
        try:
            round_number += 1

            print(
                f"\n========== 第 {round_number} 轮 =========="
            )

            # 一轮内最多询问模型 RECLICK_ATTEMPTS 次：
            # 如果模型选到了不可点击的位置，带上失败原因重新询问，
            # 避免每轮都选到同一个被压住的砖块而卡死。
            feedback = None
            status = "blocked"
            data = None
            end_kind = None
            # 本轮内决定停机的具体原因（区别于"关卡结束"的停机）
            stop_reason = None

            for attempt in range(RECLICK_ATTEMPTS):
                if attempt > 0:
                    print(
                        f"\n[重试] 第 {attempt + 1} 次询问模型……"
                    )

                # 1. 截取当前屏幕
                # 2. 本地检测可点击砖块并随提问一起发出
                # 3. 请求结束后清理临时截图
                model_text, image_width, image_height = (
                    ask_ai(feedback)
                )

                # 【关卡结束检测·前置】通关/失败弹窗出现时，本轮不点击。
                # 必须放在"槽位保护等待"**之前**：等待轮原来直接 break，
                # 失败弹窗真出现了也检测不到，Agent 会对着弹窗永远等下去。
                if attempt == 0 and STOP_AFTER_LEVEL_END:
                    end_kind, end_desc = detect_level_end(
                        image_width, image_height
                    )

                    if end_kind:
                        end_confirm_rounds += 1

                        print(
                            f"\n[结束检测] {end_desc}"
                            f"（第 {end_confirm_rounds}/"
                            f"{LEVEL_END_CONFIRM_ROUNDS} 次确认）"
                        )

                        if (
                            end_confirm_rounds
                            >= LEVEL_END_CONFIRM_ROUNDS
                        ):
                            finish_level(end_kind, end_desc)
                            last_end_kind = end_kind
                            stop_requested = True
                            status = "wait"
                            break

                        time.sleep(1.5)
                        status = "wait"
                        break

                    end_confirm_rounds = 0

                # 【未知界面】连续多轮既读不到棋盘砖、也读不到卡槽，
                # 且上面没判定成通关/失败弹窗 —— 说明遇到了程序不认识的
                # 界面（活动弹窗、次数用尽、改版结算页、外部遮挡等）。
                # 本 Agent 不自己猜该点哪里，直接把重开脚本叫起来：
                # 由它截图、找按钮（认不出就问模型）、并验证新局是否开出。
                if attempt == 0 and not end_kind:
                    if LAST_CLICKABLE_COUNT == 0 and LAST_TRAY_COUNT == 0:
                        unknown_rounds += 1
                    else:
                        unknown_rounds = 0

                    if unknown_rounds >= UNKNOWN_SCREEN_ROUNDS:
                        if unknown_asks < UNKNOWN_SCREEN_MAX_ASKS:
                            unknown_asks += 1
                            unknown_rounds = 0

                            print(
                                f"\n[未知界面] 已连续 "
                                f"{UNKNOWN_SCREEN_ROUNDS} 轮读不到任何"
                                f"砖块（本局第 {unknown_asks}/"
                                f"{UNKNOWN_SCREEN_MAX_ASKS} 次求助重开脚本）"
                            )

                            restarted, restart_reason = run_restart_script(
                                "unknown_screen"
                            )

                            if restarted:
                                # 画面已经回到可玩状态：清零计数继续对局
                                unknown_asks = 0
                            else:
                                stop_reason = restart_reason
                                stop_requested = True

                            status = "wait"
                            break

                        # 本局求助次数已经用完，依然读不到任何砖块：
                        # 典型的"今日挑战次数已用完"这类界面，
                        # 只能收敛停机，否则就是无限空转。
                        stop_reason = (
                            f"未知界面无法推进：本局已求助重开脚本 "
                            f"{UNKNOWN_SCREEN_MAX_ASKS} 次，"
                            "画面仍然读不到任何砖块"
                            "（典型如「今日挑战次数已用完」，"
                            "需要人工处理）"
                        )
                        print(f"\n[停止] {stop_reason}")
                        append_log(f"停止 | {stop_reason}")
                        stop_requested = True
                        status = "wait"
                        break

                # 【槽位将满】卡槽只剩最后一个空位、棋盘上又没有可三连/
                # 凑对的砖块时，本局在数学上已经无法挽回：
                #   - 本程序禁止使用道具，不能等"人工腾位置"；
                #   - 与其干等，不如主动点砖填满卡槽，让游戏弹失败结算，
                #     再由自动推进流程开下一把（数据采集效率优先）。
                if attempt == 0 and LAST_MUST_WAIT:
                    wait_rounds += 1

                    wait_reason = LAST_WAIT_REASON or (
                        "卡槽将满且没有任何可消除的砖块"
                    )

                    if (
                        FORBID_PROPS
                        and LAST_FORCED_END_COUNT
                        < FORCED_END_MAX_PER_ROUND
                    ):
                        print(f"\n[主动结束] 触发条件：{wait_reason}")

                        if force_end_round():
                            wait_rounds = 0
                            time.sleep(1.5)
                            status = "wait"
                            break

                    # 【空转熔断】预算用尽（或连可点的砖都找不到）后，
                    # 不允许就这样永远等下去：2026-10-14 的死循环就是
                    # "计数器没清零 + 这里没有熔断"，卡槽 6/7 干等 41 轮。
                    if wait_rounds >= STALL_STOP_ROUNDS:
                        if (
                            LAST_FORCED_END_COUNT
                            >= FORCED_END_MAX_PER_ROUND
                        ):
                            why = (
                                f"主动结束预算已用完（已用 "
                                f"{LAST_FORCED_END_COUNT}/"
                                f"{FORCED_END_MAX_PER_ROUND}，"
                                "点完仍未见结算弹窗）"
                            )
                        else:
                            why = "棋盘上连可点填槽的砖都找不到"

                        stop_reason = (
                            f"卡槽 {LAST_TRAY_COUNT}/"
                            f"{TRAY_CAPACITY} 将满且无安全点击，"
                            f"{why}，已连续空等 {wait_rounds} 轮，"
                            "熔断停机（本局轨迹由退出兜底抢救保存）"
                        )
                        # 打印与记日志交给下方 stop_requested 统一处理
                        stop_requested = True
                        status = "wait"
                        break

                    # 兜底：连可点的砖都找不到（画面异常/被遮挡），
                    # 原地等待，下一轮仍会先检测弹窗
                    if wait_rounds % 10 == 1:
                        print(
                            f"\n[等待] {wait_reason}。"
                            "当前画面没有可点击的砖块，"
                            "持续等待（下一轮仍会检测结算弹窗），"
                            f"已连续等待 {wait_rounds} 轮"
                        )
                        append_log(
                            f"等待 | 卡槽 {LAST_TRAY_COUNT}/"
                            f"{TRAY_CAPACITY} 无安全点击，"
                            f"持续等待（第 {wait_rounds} 轮）"
                        )

                    time.sleep(1.5)
                    status = "wait"
                    break

                wait_rounds = 0

                print("\n[AI 原始返回]")
                print(model_text)

                # 4. 解析模型结果
                # 【修改点·需求2】改用"思考：/点击坐标："格式的正则解析
                data = parse_model_output(model_text)

                if data is None:
                    # 输出格式不合格：不点击，带上格式要求重问
                    status = "blocked"
                    reason = (
                        "输出格式不符合要求：必须严格只有两行，"
                        "第一行「思考：<简短预判分析>」，"
                        "第二行「点击坐标：x,y」，不能有多余文字。"
                        "「思考」只写一句话，不要列举多个候选方案，"
                        "避免输出过长导致坐标行被截断"
                    )

                    print(f"\n[格式] {reason}")
                    append_log(f"跳过(格式) | {reason}")

                    # 【修改点·需求3】记录这一步（格式错误也要入库）
                    # 【DPO 格式改造】带上 attempt / feedback，
                    # 只有首次无反馈的那一步才挂 frame+prompt+candidates
                    record_step(
                        model_text,
                        "",
                        None,
                        f"blocked: {reason}",
                        LAST_TRAY_COUNT,
                        attempt=attempt + 1,
                        feedback=feedback,
                    )
                else:
                    # 4.1 模型坐标是缩放图上的像素，换算回原尺寸
                    #     分析图坐标系，后续遮挡检查和鼠标换算都基于它
                    model_coords_to_analysis(data)

                    # 5. 自动执行操作
                    status, reason = execute_action(
                        data,
                        image_width,
                        image_height,
                    )

                    # 【修改点·需求3】记录这一步：
                    # 步号 / 模型原始输出 / 思考 / 点击坐标 / 结果 / 卡槽占用
                    if data.get("action") == "click":
                        step_click = [data.get("x"), data.get("y")]
                    else:
                        step_click = None

                    record_step(
                        model_text,
                        str(data.get("reason") or ""),
                        step_click,
                        f"{status}: {reason}" if reason else status,
                        LAST_TRAY_COUNT,
                        attempt=attempt + 1,
                        feedback=feedback,
                    )

                if status != "blocked":
                    break

                # 本轮没有任何可点击砖块时，不必反复追问
                if LAST_CLICKABLE_COUNT == 0:
                    print(
                        "[等待] 画面中没有检测到可点击的完整卡面，"
                        "2 秒后重新截图"
                    )
                    append_log("等待 | 画面中无可点击卡面")
                    time.sleep(2)
                    break

                feedback = reason

                if attempt == RECLICK_ATTEMPTS - 1:
                    print(
                        f"\n[放弃] 连续 {RECLICK_ATTEMPTS} 次选择"
                        f"都不可点击（{reason}）"
                    )

            # 已确认本局结束：交给重开脚本开下一局，继续采集
            if stop_requested:
                # 本轮里由未知界面分支给出的停机原因（"次数用尽、
                # 求助多次仍读不到砖块"）优先展示，不做推进尝试
                if stop_reason:
                    print(f"\n[停止] {stop_reason}")
                    append_log(f"停止 | {stop_reason}")
                    break

                # 【自动推进】failed（失败结算页）/ cleared（通关）/
                # covered（画面被弹窗盖住）本局都算结束，统一调用
                # restart_game.py：找按钮、点击、验证新局，都是它的活。
                if should_auto_restart(last_end_kind, episode_count):
                    print(
                        f"\n[自动推进] 第 {episode_count} 局结束"
                        f"（判定={last_end_kind}），"
                        f"交由 {RESTART_SCRIPT_NAME} 开启下一局"
                        f"（目标 {RESTART_MAX_EPISODES or '∞'} 局）"
                    )

                    restarted, restart_reason = run_restart_script(
                        last_end_kind
                    )

                    if restarted:
                        episode_count += 1
                        stop_requested = False
                        end_confirm_rounds = 0
                        wait_rounds = 0
                        unknown_rounds = 0
                        unknown_asks = 0

                        # 上一局轨迹已在 finish_level 里保存并清空，
                        # 这里为新一局重新初始化轨迹缓存
                        start_trace()

                        print(
                            f"\n[自动推进] 已开始第 {episode_count} 局"
                            f"（已完成 {episode_count - 1} 局）"
                        )
                        append_log(
                            f"推进 | 成功，开始第 {episode_count} 局"
                        )
                        continue

                    print(f"\n[停止] {restart_reason}")
                    append_log(f"停止 | {restart_reason}")
                    break

                print(
                    f"\n[停止] 连续 {LEVEL_END_CONFIRM_ROUNDS} 轮"
                    "确认本关已结束，程序自动退出"
                )
                break

            # 本地兜底：模型连续不配合（反复选不可点击的砖块、
            # 或无视优先级）时，不再原地打转，直接由本地从
            # 最高优先级组里挑一个坐标执行点击。
            # "该点哪块砖"本质是规则决策，本地已经算出答案，
            # 模型只是执行者，不配合时不值得反复追问。
            if (
                status == "blocked"
                and not LAST_MUST_WAIT
                and LAST_CLICKABLE_COUNT > 0
            ):
                fallback = pick_local_fallback()

                if fallback is not None:
                    fx, fy = fallback

                    print(
                        f"\n[兜底] 模型连续 {RECLICK_ATTEMPTS} 次"
                        f"未配合，本地直接点击最高优先级坐标 "
                        f"({fx}, {fy})"
                    )

                    append_log(
                        f"兜底点击 | 本地选择({fx}, {fy})"
                        f" | 模型连续不配合"
                    )

                    fallback_data = {
                        "action": "click",
                        "x": fx,
                        "y": fy,
                        "reason": "本地兜底：模型连续未给出可执行动作",
                        "confidence": 1.0,
                        "tile": "本地兜底",
                    }

                    status, reason = execute_action(
                        fallback_data,
                        image_width,
                        image_height,
                    )

                    # 【修改点·需求3】兜底动作也记入轨迹
                    # 【DPO 格式改造】attempt 传 RECLICK_ATTEMPTS+1 且
                    # feedback 非空，标明这不是"无反馈首轮"样本，
                    # 因此不挂 frame/prompt/candidates（兜底帧可能已过期）
                    record_step(
                        "(本地兜底)",
                        "本地兜底：模型连续未给出可执行动作",
                        [fx, fy],
                        f"{status}: {reason}" if reason else status,
                        LAST_TRAY_COUNT,
                        attempt=RECLICK_ATTEMPTS + 1,
                        feedback="local_fallback",
                    )
                else:
                    print(
                        "[兜底] 最高优先级组中没有可点击的坐标，"
                        "2 秒后重新截图"
                    )
                    time.sleep(2)

            # 6. 释放本轮不再需要的数据
            #    关卡结束检测会提前跳出本轮，此时 data 可能还没生成
            del model_text

            if data is not None:
                del data

            gc.collect()

            # 避免请求过于密集
            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n[退出] 收到 Ctrl+C，程序已停止")
            break

        except pyautogui.FailSafeException:
            print("\n[紧急停止] 鼠标触发安全机制，程序已停止")
            break

        except SystemExit:
            print("\n[退出] AI 建议停止操作")
            break

        except Exception as exc:
            print(
                f"\n[错误] 第 {round_number} 轮执行失败："
                f"{type(exc).__name__}: {exc}"
            )

            print("[恢复] 等待 2 秒后重新尝试")
            gc.collect()
            time.sleep(2)

    # 【收尾兜底】不论从哪条路跳出循环（Ctrl+C / 鼠标安全机制 /
    # SystemExit / 各类停机），都把当前局的轨迹抢救下来：
    # 正常打完的局在 finish_level 里已经存过，这里会自动空转。
    finalize_interrupted_episode()

# ============================================================
# 13. 程序入口
# ============================================================

if __name__ == "__main__":
    main()