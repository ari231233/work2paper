"""配置加载：极简 .env 解析 + LLM（DeepSeek）配置。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


def _find_env_file() -> Optional[Path]:
    """从当前目录向上查找 .env。"""
    here = Path.cwd()
    for p in [here] + list(here.parents):
        candidate = p / ".env"
        if candidate.exists():
            return candidate
    return None


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """极简 .env 解析（KEY=VALUE，支持 # 注释；不覆盖已有环境变量）。"""
    env_file = path or _find_env_file()
    values: Dict[str, str] = {}
    if not env_file or not env_file.exists():
        return values
    with open(env_file, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                values[k] = v
    return values


def get_proxy() -> str:
    """读取代理地址：PAPERMINE_PROXY 优先（环境变量 > .env），其次 HTTPS_PROXY。

    直连外网被干扰（SSL 握手被重置）时，在 .env 配
    ``PAPERMINE_PROXY=http://127.0.0.1:7897`` 即可让 LLM / 检索走代理。空串 = 直连。
    """
    dotenv = load_env()
    return (
        os.environ.get("PAPERMINE_PROXY")
        or dotenv.get("PAPERMINE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or dotenv.get("HTTPS_PROXY")
        or ""
    )


def apply_proxy() -> None:
    """把代理写入 os.environ，供 httpx（trust_env 默认开启）自动使用。"""
    proxy = get_proxy()
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["ALL_PROXY"] = proxy


def get_llm_config() -> Dict[str, str]:
    """读取 LLM 配置；优先环境变量，其次 .env，最后默认值。

    M15 模型分级：``model`` 是核心推理模型（ideate / evaluate 等），``fast_model`` 是
    便宜快模型（翻译 / gap_note / 简单校验）。二者默认都指向 ``deepseek-chat``（行为
    与 M15 之前一致），用户可把 ``DEEPSEEK_FAST_MODEL`` 指向更便宜/更快的模型来降低成本。
    """
    apply_proxy()
    dotenv = load_env()

    def pick(key: str, default: str) -> str:
        return os.environ.get(key) or dotenv.get(key) or default

    return {
        "provider": "deepseek",
        "api_key": pick("DEEPSEEK_API_KEY", ""),
        "base_url": pick("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": pick("DEEPSEEK_MODEL", "deepseek-chat"),
        "fast_model": pick("DEEPSEEK_FAST_MODEL", "deepseek-chat"),
    }
