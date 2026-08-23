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


def get_llm_config() -> Dict[str, str]:
    """读取 LLM 配置；优先环境变量，其次 .env，最后默认值。"""
    dotenv = load_env()

    def pick(key: str, default: str) -> str:
        return os.environ.get(key) or dotenv.get(key) or default

    return {
        "provider": "deepseek",
        "api_key": pick("DEEPSEEK_API_KEY", ""),
        "base_url": pick("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": pick("DEEPSEEK_MODEL", "deepseek-chat"),
    }
