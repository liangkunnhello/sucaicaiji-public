#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""浏览器调试端口(CDP)保障：先确认 9223 通，不通则自动拉起 onion.exe 并等待就绪。

用法：
    from cdp_guard import ensure_cdp
    ensure_cdp()   # 不通会自动启动账号管家并等待，就绪后才返回
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

CDP_ENDPOINT = os.environ.get("MATERIAL_DOWNLOADER_CDP_ENDPOINT", "http://127.0.0.1:9223").rstrip("/")
WAIT_SECONDS = max(1, int(os.environ.get("CDP_WAIT_SECONDS", "60")))
EXISTING_PROCESS_WAIT_SECONDS = max(
    0, int(os.environ.get("CDP_EXISTING_PROCESS_WAIT_SECONDS", "20"))
)
_LAST_READY_ENDPOINT = None

# onion.exe（账号管家）常见安装位置，找到第一个存在的就用
ONION_CANDIDATES = [
    r"C:\Program Files\Oray\OrayOnion\onion.exe",
    r"C:\Program Files (x86)\Oray\OrayOnion\onion.exe",
    r"C:\Program Files\onion\onion.exe",
    r"C:\Program Files (x86)\onion\onion.exe",
    r"C:\Program Files\OrayOnion\onion.exe",
    r"C:\Program Files (x86)\OrayOnion\onion.exe",
    r"D:\onion\onion.exe",
    r"D:\OrayOnion\onion.exe",
    r"D:\Program Files\onion\onion.exe",
    r"D:\Program Files\Oray\OrayOnion\onion.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\onion\onion.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\onion\onion.exe"),
    os.path.expandvars(r"%USERPROFILE%\Desktop\onion.exe"),
]


def _endpoint_candidates():
    """Return safe local endpoint variants without changing the public default."""
    configured = CDP_ENDPOINT or "http://127.0.0.1:9223"
    candidates = [configured]
    try:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(configured)
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            for host in ("127.0.0.1", "localhost"):
                candidate = urlunsplit(
                    (
                        parsed.scheme or "http",
                        f"{host}:{parsed.port or 9223}",
                        parsed.path.rstrip("/"),
                        "",
                        "",
                    )
                ).rstrip("/")
                if candidate not in candidates:
                    candidates.append(candidate)
    except Exception:
        pass
    return candidates


def _probe_endpoint(endpoint, timeout=2):
    """Probe ``/json/version`` and return a diagnostic dictionary."""
    url = f"{endpoint.rstrip('/')}/json/version"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        websocket = payload.get("webSocketDebuggerUrl")
        if not websocket:
            return {
                "ready": False,
                "endpoint": endpoint,
                "error": "响应缺少 webSocketDebuggerUrl",
            }
        return {
            "ready": True,
            "endpoint": endpoint,
            "websocket": websocket,
            "browser": payload.get("Browser") or payload.get("browser"),
        }
    except Exception as error:
        return {"ready": False, "endpoint": endpoint, "error": str(error)}


def probe_cdp(timeout=2):
    """Probe all local CDP endpoint variants and retain the working one."""
    global _LAST_READY_ENDPOINT
    _LAST_READY_ENDPOINT = None
    diagnostics = []
    for endpoint in _endpoint_candidates():
        result = _probe_endpoint(endpoint, timeout=timeout)
        diagnostics.append(result)
        if result.get("ready"):
            _LAST_READY_ENDPOINT = endpoint
            return result
    return {
        "ready": False,
        "endpoint": CDP_ENDPOINT,
        "diagnostics": diagnostics,
        "error": "；".join(
            f"{item.get('endpoint')}: {item.get('error')}" for item in diagnostics
        ),
    }


def cdp_ready(timeout=2):
    """9223 调试端口是否可用。"""
    return bool(probe_cdp(timeout=timeout).get("ready"))


def get_cdp_endpoint():
    """Return the endpoint that most recently answered successfully."""
    return _LAST_READY_ENDPOINT or CDP_ENDPOINT


def _running_onion_processes():
    """读取账号管家主进程及命令行。

    Chromium 会创建多个同名 ``onion.exe`` 子进程。旧实现取 WMI 返回的
    第一条记录，可能拿到 renderer，导致 PID/命令行诊断不准确；这里优先
    选择不带 ``--type=`` 的浏览器主进程。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='onion.exe'\" | "
             "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=10,
        ).stdout
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            text = out.decode("gbk", errors="replace").strip()
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = [payload]
        processes = [
            item for item in payload
            if isinstance(item, dict) and item.get("ExecutablePath")
        ]
        processes.sort(
            key=lambda item: (
                "--type=" in str(item.get("CommandLine") or ""),
                str(item.get("ProcessId") or ""),
            )
        )
        return processes
    except Exception:
        return []


def _running_onion_info():
    processes = _running_onion_processes()
    return processes[0] if processes else None


def _running_onion_path():
    """从正在运行的主进程里找 onion.exe 的完整路径。"""
    info = _running_onion_info()
    path = info.get("ExecutablePath") if info else None
    return path if path and Path(path).exists() else None


def _debug_port_from_command_line(command_line):
    match = re.search(
        r"--remote-debugging-port(?:=|\s+)(\d+)",
        str(command_line or ""),
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _where_onion():
    """按 PATH 查找 onion.exe。"""
    try:
        out = subprocess.run(["where", "onion.exe"], capture_output=True, timeout=5).stdout.decode("gbk", errors="replace").strip()
        if out:
            return out.splitlines()[0].strip()
    except Exception:
        pass
    return None


def find_onion():
    """定位 onion.exe：运行中的进程 > 固定路径 > PATH。"""
    path = _running_onion_path()
    if path:
        return path
    for candidate in ONION_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return _where_onion()


def start_onion():
    """启动账号管家并请求开启 9223（已运行则只等待端口）。"""
    if _running_onion_path():
        print("[CDP] 账号管家已在运行，等待 9223 调试端口就绪...")
        return True
    exe = find_onion()
    if not exe:
        print("[CDP] 未找到 onion.exe，请手动启动账号管家并开启调试端口 9223")
        return False
    print(f"[CDP] 正在启动账号管家: {exe}")
    try:
        return _launch(exe, "--remote-debugging-port=9223")
    except Exception as e:
        print(f"[CDP] 启动失败: {e}")
        return False


def _launch(exe, *extra_args):
    """拉起账号管家，返回是否启动成功。"""
    try:
        subprocess.Popen([exe, *extra_args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[CDP] 启动失败: {e}")
        return False


def _wait_ready(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cdp_ready():
            return True
        time.sleep(2)
    return False


def ensure_cdp(allow_restart=True):
    """确保 9223 可用：通就直接返回；不通自动拉起账号管家并等待。

    兼容三种情形：
    1) 账号管家在跑且端口已开 → 直接用
    2) 账号管家在跑但端口没开 → 先等（用户可能正手动开），再重启它并带调试参数
    3) 账号管家没在跑 → 直接带调试参数启动
    """
    initial_probe = probe_cdp()
    if initial_probe.get("ready"):
        print(f"[CDP] 9223 调试端口已就绪：{initial_probe.get('endpoint')}")
        return True

    print("[CDP] 9223 未就绪，尝试自动恢复...")
    exe = find_onion()
    if not exe:
        print("[CDP] 未找到 onion.exe，请手动启动账号管家并开启调试端口 9223")
        return False

    running_path = _running_onion_path()
    # The safe (no-restart) path must not perform another process enumeration;
    # this also keeps it usable under restricted WMI permissions.
    running = _running_onion_info() if running_path and allow_restart else None
    if running_path:
        process_id = running.get("ProcessId") if running else None
        command_line = str(running.get("CommandLine") or "") if running else ""
        configured_port = _debug_port_from_command_line(command_line)
        if configured_port and configured_port != 9223:
            print(f"[CDP] 账号管家进程 PID={process_id} 使用调试端口 {configured_port}，目标端口为 9223")
        else:
            print(f"[CDP] 账号管家进程 PID={process_id or '未知'}，命令行未发现 9223 参数")
        # 情形 2：已在运行但端口未开。先等用户/自身开启，不行则带参重启。
        print(f"[CDP] 账号管家在运行但调试端口未开，先等待 {EXISTING_PROCESS_WAIT_SECONDS} 秒...")
        if _wait_ready(EXISTING_PROCESS_WAIT_SECONDS):
            print("[CDP] 9223 调试端口已就绪")
            return True
        if not allow_restart:
            print("[CDP] 账号管家仍在运行，但 9223 未就绪；本次不重启以保护登录态")
            print(f"[CDP] 探测结果：{initial_probe.get('error') or '无响应'}")
            print("      请在账号管家设置中开启远程调试端口 9223，或允许采集任务自动重启账号管家")
            return False
        print("[CDP] 仍未就绪，重启账号管家并开启调试端口...")
        try:
            kill_command = ["taskkill", "/F"]
            if process_id:
                kill_command.extend(["/PID", str(process_id), "/T"])
            else:
                kill_command.extend(["/IM", "onion.exe"])
            subprocess.run(kill_command,
                           capture_output=True, timeout=10)
            time.sleep(2)
        except Exception:
            pass
    else:
        print(f"[CDP] 正在启动账号管家: {exe}")

    # 带调试端口参数启动（旧版忽略该参数也不影响）
    if not _launch(exe, "--remote-debugging-port=9223"):
        return False
    print(f"[CDP] 等待 9223 调试端口就绪（最多 {WAIT_SECONDS} 秒）...")
    if _wait_ready(WAIT_SECONDS):
        print("[CDP] 9223 调试端口已就绪")
        return True

    print(f"[CDP] 等待 {WAIT_SECONDS} 秒后 9223 仍不可用")
    print("      请打开账号管家，在设置中手动开启「调试端口 9223」后重试")
    return False


if __name__ == "__main__":
    sys.exit(0 if ensure_cdp() else 1)
