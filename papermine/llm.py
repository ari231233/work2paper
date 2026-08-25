"""LLM 接入层：统一 LLMProvider 抽象 + DeepSeek 实现 + 无 key 兜底 + 结构化输出。

对应 docs/build-plan.md §3.1 与 docs/architecture.md §7：

- ``DeepSeekProvider``：走 OpenAI 兼容接口（``base_url`` / ``model`` / ``api_key`` 从 config 读）。
- 结构化输出：请求 JSON mode，返回后按传入 ``schema`` 校验，失败重试 2 次，仍失败抛 ``SchemaError``。
- ``NullProvider``：无 key 时的离线兜底，``complete()`` 返回空 dict，上层据此降级到确定性规则。
- HTTP 客户端使用 httpx（本项目唯一第三方依赖）。

注意：本模块不依赖 jsonschema，内置一个覆盖本项目 Agent schema 需求的极简 JSON Schema 校验子集。

M16 方向⑤：``DeepSeekProvider`` 支持 LLM 调用缓存（相同输入 → 复用输出）。缓存键含
model + system + user + schema + temperature，故 prompt / schema 内容变化自动失效；仅缓存
schema 校验成功的输出，失败（SchemaError / LLMError）不落缓存。``cache_dir=None``（默认）
时关闭缓存，``get_provider`` 按需开启（目录 ``~/.papermine/llm_cache``）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import httpx

from . import storage
from .config import get_llm_config

__all__ = [
    "LLMError",
    "SchemaError",
    "LLMProvider",
    "DeepSeekProvider",
    "NullProvider",
    "get_provider",
    "complete_fast",
    "DEFAULT_LLM_CACHE_TTL",
]


class LLMError(Exception):
    """LLM 调用层面失败：无 key、网络错误、HTTP 非 200、响应格式异常。"""


class SchemaError(Exception):
    """结构化输出未满足传入 schema（重试耗尽后仍失败）。"""


class LLMProvider(Protocol):
    """统一的 LLM 抽象接口（冻结契约，见 docs/build-plan.md §3.1）。"""

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        """返回符合 schema 的 dict；失败抛 LLMError / SchemaError。"""
        ...


def complete_fast(llm: Any, system: str, user: str,
                  schema: dict, temperature: float = 0.2) -> dict:
    """用便宜快模型调用（M15 方向③）；provider 不支持分级时回退 ``complete``（同模型）。

    冻结契约 §3.1 只定义了 ``complete``，此处**不改变**其签名，而是新增一个分级入口：
    上层（retrieval / evidence 等「翻译 / gap_note / 简单校验」环节）调用本函数，
    底层 ``DeepSeekProvider.complete_fast`` 走 ``fast_model``；``NullProvider`` 返回空 dict；
    测试桩只实现 ``complete`` 时自动回退，保证不回归。
    """
    if llm is None:
        return {}
    fast = getattr(llm, "complete_fast", None)
    if callable(fast):
        return fast(system, user, schema, temperature)
    return llm.complete(system, user, schema, temperature)


# ---------------------------------------------------------------------------
# 极简 JSON Schema 校验（仅覆盖本项目用到的子集）
# ---------------------------------------------------------------------------

def _type_matches(expected: str, value: Any) -> bool:
    """判断 value 是否符合单个 JSON Schema 类型。未知类型视为无约束。"""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(schema: Dict[str, Any], data: Any, path: str = "$") -> List[str]:
    """按 JSON Schema 校验 data，返回错误信息列表（空列表 = 通过）。

    支持的子集：type（字符串或数组）、enum、properties、required、items、additionalProperties: false，
    并沿 properties / items 递归校验嵌套结构。
    """
    if not isinstance(schema, dict):
        return []
    errors: List[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        types = [expected_type] if isinstance(expected_type, str) else expected_type
        if not any(_type_matches(t, data) for t in types):
            errors.append(
                "{} 应为 {}，实际为 {}".format(path, "/".join(types), type(data).__name__)
            )
            # 类型不符时不再深入子校验，避免误报
            return errors

    if "enum" in schema and data not in schema["enum"]:
        errors.append("{} 取值 {} 不在枚举 {}".format(path, repr(data), schema["enum"]))

    if "properties" in schema:
        if not isinstance(data, dict):
            errors.append("{} 应为对象".format(path))
            return errors
        properties = schema["properties"] or {}
        for key, sub_schema in properties.items():
            if key in data:
                errors.extend(_validate_schema(sub_schema, data[key], "{}.{}".format(path, key)))
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    errors.append("{} 存在未声明字段 {}".format(path, key))

    if "required" in schema:
        if not isinstance(data, dict):
            errors.append("{} 应为对象".format(path))
        else:
            for req in schema["required"]:
                if req not in data:
                    errors.append("{} 缺少必需字段 {}".format(path, req))

    if "items" in schema:
        if not isinstance(data, list):
            errors.append("{} 应为数组".format(path))
        else:
            for i, item in enumerate(data):
                errors.extend(_validate_schema(schema["items"], item, "{}[{}]".format(path, i)))

    return errors


# ---------------------------------------------------------------------------
# DeepSeek 实现
# ---------------------------------------------------------------------------

def _build_system(system: str, schema: Dict[str, Any]) -> str:
    """把输出 schema 注入 system 指令，并要求只输出 JSON 对象。"""
    schema_text = json.dumps(schema, ensure_ascii=False)
    return (
        system
        + "\n\n请只输出一个 JSON 对象（不要输出任何多余文字、解释或 markdown 代码块），"
        + "并且必须严格满足以下 JSON Schema：\n"
        + schema_text
    )


def _parse_content(content: str) -> Dict[str, Any]:
    """把模型返回的 content 解析为 dict；解析失败抛 SchemaError（计入重试）。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SchemaError("模型未返回合法 JSON：{}".format(exc)) from exc
    if not isinstance(data, dict):
        raise SchemaError("模型输出不是 JSON 对象（期望 object）")
    return data


# ---------------------------------------------------------------------------
# LLM 调用缓存（M16 方向⑤）：相同输入 → 复用输出
# ---------------------------------------------------------------------------

LLM_CACHE_SCHEMA = "llm_cache"
LLM_CACHE_SCHEMA_VERSION = 1
# 缓存 TTL：键已含 model+prompt+schema+temperature，30 天足够；过期自动失效回源
DEFAULT_LLM_CACHE_TTL = 30 * 24 * 3600


def _llm_cache_dir() -> Path:
    """LLM 调用缓存目录：``~/.papermine/llm_cache``（``PAPERMINE_HOME`` 可覆盖）。"""
    return storage.data_root() / "llm_cache"


class DeepSeekProvider:
    """DeepSeek 实现：OpenAI 兼容 chat/completions + JSON mode + schema 校验重试。

    M15 模型分级（方向③）：同一 provider 支持两个模型——
    - ``model``：核心推理模型（ideate / evaluate 等），走 ``complete()``；
    - ``fast_model``：便宜快模型（翻译 / gap_note / 简单校验），走 ``complete_fast()``。
    二者默认相同（``fast_model`` 缺省时 = ``model``），分级可插拔且不破坏冻结契约 §3.1。
    """

    def __init__(self, api_key: str, base_url: str, model: str,
                 fast_model: Optional[str] = None,
                 timeout: float = 60.0, max_retries: int = 2,
                 client: Optional[httpx.Client] = None,
                 cache_dir: Optional[Path] = None,
                 cache_ttl: Optional[float] = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fast_model = fast_model or model
        self.timeout = timeout
        self.max_retries = max_retries
        # 复用长连接 Client（连接池 + keep-alive + TLS 复用）；测试注入 MockTransport 时用之。
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        # M16 方向⑤：LLM 调用缓存。cache_dir=None 关闭（测试 / 显式直连）；否则启用。
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._cache_ttl = float(cache_ttl) if cache_ttl is not None else DEFAULT_LLM_CACHE_TTL

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        """核心推理模型：请求结构化输出并校验 schema；失败重试 max_retries 次。"""
        return self._complete(self.model, system, user, schema, temperature)

    def complete_fast(self, system: str, user: str,
                      schema: dict, temperature: float = 0.2) -> dict:
        """便宜快模型（M15 方向③）：与 ``complete`` 相同的结构化输出 + 校验重试语义。"""
        return self._complete(self.fast_model, system, user, schema, temperature)

    def _complete(self, model: str, system: str, user: str,
                  schema: dict, temperature: float) -> dict:
        """按指定模型请求结构化输出并校验 schema；校验失败重试，仍失败抛 SchemaError。

        M16 方向⑤：启用缓存时，相同输入（model+system+user+schema+temperature）命中缓存
        直接复用，不再重复调 LLM；仅缓存 schema 校验成功的输出，失败不落缓存。
        """
        cache_key: Optional[str] = None
        if self._cache_dir is not None:
            cache_key = self._cache_key(model, system, user, schema, temperature)
            cached = self._cache_read(cache_key)
            if cached is not None:
                return cached

        result = self._complete_uncached(model, system, user, schema, temperature)

        if cache_key is not None:
            self._cache_write(cache_key, result)
        return result

    def _complete_uncached(self, model: str, system: str, user: str,
                           schema: dict, temperature: float) -> dict:
        """不经过缓存的实际调用：JSON mode + schema 校验 + 重试（失败抛 LLMError/SchemaError）。"""
        last_err: Optional[SchemaError] = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._call_once(model, system, user, schema, temperature)
            except LLMError:
                # 网络 / HTTP / 响应结构问题不属于 schema 校验失败，直接上抛
                raise
            except SchemaError as exc:
                last_err = exc
                continue

            errors = _validate_schema(schema, data)
            if not errors:
                return data
            last_err = SchemaError(
                "schema 校验失败（第 {}/{} 次）：{}".format(
                    attempt + 1, self.max_retries + 1, "; ".join(errors[:3])
                )
            )
        raise last_err if last_err is not None else SchemaError("schema 校验失败")

    # -- M16 缓存辅助 --
    def _cache_key(self, model: str, system: str, user: str,
                   schema: Dict[str, Any], temperature: float) -> str:
        """缓存键：model + system + user + schema + temperature 的规范化 JSON 摘要。

        键含全部影响输出的输入（prompt / schema 版本变更 → system / schema 文本变化 → 键变化），
        故内容变化自动失效，无需显式清缓存；temperature 归一化为 float 避免 0 / 0.0 分键。
        """
        payload = {
            "model": model,
            "system": system,
            "user": user,
            "schema": schema,
            "temperature": float(temperature),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / ("c_" + key + ".json")

    def _cache_read(self, key: str) -> Optional[Dict[str, Any]]:
        """命中且未过期返回缓存结果，否则 None；缓存损坏/过期视为 miss（静默回源）。"""
        path = self._cache_path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if time.time() - float(data.get("cached_at", 0)) > self._cache_ttl:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def _cache_write(self, key: str, result: Dict[str, Any]) -> None:
        """写入缓存（原子替换）；失败静默，绝不影响主流程。"""
        path = self._cache_path(key)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "_schema": LLM_CACHE_SCHEMA,
                "_schema_version": LLM_CACHE_SCHEMA_VERSION,
                "model": self.model,
                "cached_at": time.time(),
                "result": result,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _call_once(self, model: str, system: str, user: str,
                   schema: dict, temperature: float) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _build_system(system, schema)},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        return _parse_content(self._post(payload))

    def _post(self, payload: Dict[str, Any]) -> str:
        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError("HTTP 请求失败：{}".format(exc)) from exc

        if resp.status_code != 200:
            raise LLMError("DeepSeek API 返回 {}：{}".format(resp.status_code, resp.text[:500]))

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("DeepSeek 响应格式异常：{}".format(exc)) from exc

        if isinstance(content, str):
            return content
        return json.dumps(content)


# ---------------------------------------------------------------------------
# 无 key 兜底
# ---------------------------------------------------------------------------

class NullProvider:
    """无 API key 时的离线兜底实现。

    ``complete()`` / ``complete_fast()`` 恒返回空 dict：上层拿到空结果后应降级到确定性规则
    （架构 §7 / §8）。
    """

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        return {}

    def complete_fast(self, system: str, user: str,
                      schema: dict, temperature: float = 0.2) -> dict:
        return {}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def get_provider() -> LLMProvider:
    """读 config（papermine/config.py）：无 api_key 返回 NullProvider，否则返回 DeepSeekProvider。

    M16 方向⑤：有 key 时启用 LLM 调用缓存（``~/.papermine/llm_cache``），相同输入复用输出。
    """
    cfg = get_llm_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return NullProvider()
    return DeepSeekProvider(
        api_key=api_key,
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
        fast_model=cfg.get("fast_model") or cfg.get("model", "deepseek-chat"),
        cache_dir=_llm_cache_dir(),
    )
