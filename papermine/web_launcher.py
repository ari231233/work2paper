"""M26 本地 Web 启动器：统一管理 FastAPI 与 Next.js 进程。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

import httpx


def frontend_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "frontend"


def npm_executable() -> Optional[str]:
    return shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")


def _frontend_command(npm: str, dev: bool, host: str, port: int) -> List[str]:
    command = [npm, "run", "start", "--", "--hostname", host, "--port", str(port)]
    if dev:
        command.append("--dev")
    return command


def _backend_command(host: str, port: int) -> List[str]:
    return [
        sys.executable, "-m", "uvicorn", "web.app:create_app", "--factory",
        "--host", host, "--port", str(port),
    ]


def _ensure_frontend(npm: str, env: Dict[str, str], dev: bool) -> None:
    root = frontend_dir()
    if not (root / "package.json").exists():
        raise RuntimeError("未找到 Web 前端目录：{}".format(root))
    if not (root / "node_modules").exists():
        raise RuntimeError("前端依赖尚未安装，请先执行：cd web/frontend && npm ci")
    if not dev and not (root / ".next" / "BUILD_ID").exists():
        print("[papermine] 首次启动，正在构建 Web 前端……")
        completed = subprocess.run([npm, "run", "build"], cwd=str(root), env=env)
        if completed.returncode != 0:
            raise RuntimeError("Web 前端构建失败，请查看上方输出")


def _wait_ready(url: str, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务启动失败（退出码 {}）".format(process.returncode))
        try:
            if httpx.get(url, timeout=0.8).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("等待服务启动超时：{}".format(url))


def _stop(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_web(
    host: str = "127.0.0.1",
    api_port: int = 8000,
    web_port: int = 3000,
    open_browser: bool = True,
    dev: bool = False,
) -> int:
    """启动本地后端与前端，直到 Ctrl+C；返回进程退出码。"""
    npm = npm_executable()
    if not npm:
        raise RuntimeError("未找到 Node.js/npm，请先安装 Node.js 18 或更高版本")
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    api_origin = "http://{}:{}".format(browser_host, api_port)
    web_url = "http://{}:{}".format(browser_host, web_port)
    env = dict(os.environ)
    env["PAPERMINE_API_ORIGIN"] = api_origin
    _ensure_frontend(npm, env, dev)

    backend: Optional[subprocess.Popen] = None
    frontend: Optional[subprocess.Popen] = None
    try:
        backend = subprocess.Popen(_backend_command(host, api_port), env=env)
        _wait_ready(api_origin + "/health", backend)
        frontend = subprocess.Popen(
            _frontend_command(npm, dev, host, web_port),
            cwd=str(frontend_dir()), env=env,
        )
        _wait_ready(web_url, frontend, timeout=60.0)
        print("[papermine] Web 已启动：{}".format(web_url))
        print("[papermine] 按 Ctrl+C 停止服务")
        if open_browser:
            webbrowser.open(web_url)
        while True:
            if backend.poll() is not None:
                return int(backend.returncode or 1)
            if frontend.poll() is not None:
                return int(frontend.returncode or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[papermine] 正在停止 Web 服务……")
        return 0
    finally:
        _stop(frontend)
        _stop(backend)
