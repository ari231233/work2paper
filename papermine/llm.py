"""LLM 接入层：统一 LLMProvider 抽象 + DeepSeek 实现 + 无 key 兜底 + 结构化输出。

对应 docs/build-plan.md §3.1 与 docs/architecture.md §7：

- ``DeepSeekProvider``：走 OpenAI 兼容接口（``base_url`` / ``model`` / ``api_key`` 从 config 读）。
- 结构化输出：请求 JSON mode，返回后按传入 ``schema`` 校验，失败重试 2 次，仍失败抛 ``SchemaError``。
- ``NullProvider``：无 key 时的离线兜底，``complete()`` 返回空 dict，上层据此降级到确定性规则。
- HTTP 客户端使用 httpx（本项目唯一第三方依赖）。

注意：本模块不依赖 jsonschema，内置一个覆盖本项目 Agent schema 需求的极简 JSON Schema 校验子集。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Protocol

import httpx

from .config import get_llm_config

__all__ = [
    "LLMError",
    "SchemaError",
    "LLMProvider",
    "DeepSeekProvider",
    "NullProvider",
    "get_provider",
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


class DeepSeekProvider:
    """DeepSeek 实现：OpenAI 兼容 chat/completions + JSON mode + schema 校验重试。"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout: float = 60.0, max_retries: int = 2,
                 client: Optional[httpx.Client] = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        # 复用长连接 Client（连接池 + keep-alive + TLS 复用）；测试注入 MockTransport 时用之。
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        """请求结构化输出并校验 schema；校验失败重试 max_retries 次，仍失败抛 SchemaError。"""
        last_err: Optional[SchemaError] = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._call_once(system, user, schema, temperature)
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

    def _call_once(self, system: str, user: str,
                   schema: dict, temperature: float) -> Dict[str, Any]:
        payload = {
            "model": self.model,
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

    ``complete()`` 恒返回空 dict：上层拿到空结果后应降级到确定性规则（架构 §7 / §8）。
    """

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        return {}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def get_provider() -> LLMProvider:
    """读 config（papermine/config.py）：无 api_key 返回 NullProvider，否则返回 DeepSeekProvider。"""
    cfg = get_llm_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return NullProvider()
    return DeepSeekProvider(
        api_key=api_key,
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
    )
