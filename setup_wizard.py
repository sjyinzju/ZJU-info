#!/usr/bin/env python3
"""ZJU Info Agent — 首次部署向导

用法:
    python setup_wizard.py                   交互式配置
    python setup_wizard.py --schedule 08:00  仅注册 Windows 定时任务
    python setup_wizard.py --unschedule      删除已注册的定时任务
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
TASK_NAME = "ZJU_Info_Agent"


def register_windows_task(run_time: str = "08:00") -> bool:
    """向 Windows 系统静默注册每日定时任务。

    使用 schtasks 注册用户级每日任务：
      - 名称: ZJU_Info_Agent
      - 触发: 每天 run_time (北京时间)
      - 执行: pythonw.exe <项目根>/run_once.py
      - 静默: 无终端窗口 (pythonw.exe + CREATE_NO_WINDOW)

    Args:
        run_time: 24 小时制时间字符串，如 "08:00"、"20:30"

    Returns:
        bool: 注册成功返回 True，失败返回 False
    """
    # ── 1. 验证时间格式 ────────────────────────────────────────
    try:
        parts = run_time.strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        run_time = f"{hour:02d}:{minute:02d}"  # 标准化为 HH:MM
    except (ValueError, IndexError):
        print(f"[ERROR] 时间格式无效: '{run_time}'，应为 HH:MM（如 08:00）")
        return False

    # ── 2. 获取 pythonw.exe 路径 ───────────────────────────────
    pythonw_path = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw_path.exists():
        print(f"[ERROR] pythonw.exe 未找到: {pythonw_path}")
        print(f"        预期路径与 python.exe 同目录，请检查 Python 安装")
        return False

    # ── 3. 获取 run_once.py 绝对路径 ───────────────────────────
    script_path = PROJECT_ROOT / "run_once.py"
    if not script_path.exists():
        print(f"[ERROR] run_once.py 未找到: {script_path}")
        return False

    # ── 4. 构造 schtasks 命令 ─────────────────────────────────
    # 双引号包裹路径，防止空格导致命令行解析失败
    exec_cmd = f'"{pythonw_path}" "{script_path}"'
    working_dir = str(PROJECT_ROOT)

    schtasks_cmd = [
        "schtasks",
        "/create",
        "/tn", TASK_NAME,
        "/tr", exec_cmd,
        "/sc", "daily",
        "/st", run_time,
        "/f",           # 覆盖已有任务（幂等）
    ]

    # ── 5. 静默执行注册 ────────────────────────────────────────
    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=working_dir,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"[OK] 定时任务已注册")
            print(f"     任务名: {TASK_NAME}")
            print(f"     时间:   每天 {run_time} (北京时间)")
            print(f"     执行:   pythonw.exe run_once.py")
            print(f"     目录:   {working_dir}")
            print(f"     提示:   运行 'python setup_wizard.py --unschedule' 可删除")
            return True
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            print(f"[ERROR] schtasks 返回码 {result.returncode}: {error_msg}")
            return False

    except FileNotFoundError:
        print("[ERROR] schtasks.exe 未找到 — 系统可能不完整")
        return False
    except subprocess.TimeoutExpired:
        print("[ERROR] schtasks 命令超时")
        return False
    except Exception as e:
        print(f"[ERROR] 注册定时任务异常: {e}")
        return False


def unregister_windows_task() -> bool:
    """删除已注册的 ZJU_Info_Agent 定时任务"""
    schtasks_cmd = [
        "schtasks",
        "/delete",
        "/tn", TASK_NAME,
        "/f",
    ]
    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
        if result.returncode == 0:
            print(f"[OK] 定时任务 '{TASK_NAME}' 已删除")
            return True
        else:
            print(f"[INFO] 未找到定时任务 '{TASK_NAME}'（可能从未注册过）")
            return False
    except Exception as e:
        print(f"[ERROR] 删除定时任务异常: {e}")
        return False


def _load_schedule_time() -> str:
    """从 config.yaml 读取调度时间，默认 08:00"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.config_loader import load_config
        config = load_config()
        times = config.schedule.times
        return times[0] if times else "08:00"
    except Exception:
        return "08:00"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ZJU Info Agent — 部署向导")
    parser.add_argument("--schedule", "-s", nargs="?", const="__from_config__",
                        help="注册 Windows 定时任务（默认从 config.yaml 读取时间，也可指定如 08:00）")
    parser.add_argument("--unschedule", "-u", action="store_true",
                        help="删除已注册的定时任务")
    args = parser.parse_args()

    if args.unschedule:
        unregister_windows_task()
        return

    if args.schedule is not None:
        if args.schedule == "__from_config__":
            run_time = _load_schedule_time()
            print(f"[INFO] 从 config.yaml 读取调度时间: {run_time}")
        else:
            run_time = args.schedule
        register_windows_task(run_time)
        return

    # 默认：交互式模式
    print("=" * 56)
    print("  ZJU Info Agent — 部署向导")
    print("=" * 56)
    print()
    print("  用法:")
    print(f"    python setup_wizard.py --schedule      注册定时任务")
    print(f"    python setup_wizard.py --schedule 20:00 指定时间注册")
    print(f"    python setup_wizard.py --unschedule    删除定时任务")
    print()


if __name__ == "__main__":
    main()
