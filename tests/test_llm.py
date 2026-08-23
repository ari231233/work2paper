"""M1 LLM 接入层单测：接口契约、schema 校验重试、无 key 兜底。"""
from __future__ import annotations

import json

import httpx
import pytest

from papermine import llm
from papermine.llm import (
    LLMError,
    SchemaError,
    DeepSeekProvider,
    NullProvider,
    get_provider,
    _validate_schema,
)


SCHEMA = {
    "type": "object",
    "required": ["title"],
    "properties": {"title": {"type": "string"}},
}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_json(payload):
    return httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
    )


# ---------------------------------------------------------------------------
# 接口契约：get_provider
# ---------------------------------------------------------------------------

def test_get_provider_no_key_returns_null(monkeypatch):
    monkeypatch.setattr(
        llm, "get_llm_config",
        lambda: {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    )
    assert isinstance(get_provider(), NullProvider)


def test_get_provider_with_key_returns_deepseek(monkeypatch):
    monkeypatch.setattr(
        llm, "get_llm_config",
        lambda: {"api_key": "sk-1", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    )
    provider = get_provider()
    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == "deepseek-chat"
    assert provider.base_url == "https://api.deepseek.com"


def test_null_provider_returns_empty_dict():
    provider = NullProvider()
    assert provider.complete("sys", "user", SCHEMA) == {}


# ---------------------------------------------------------------------------
# DeepSeekProvider：成功 + JSON mode 请求体
# ---------------------------------------------------------------------------

def test_deepseek_complete_success():
    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "deepseek-chat",
                                client=_client(lambda req: _ok_json({"title": "你好"})))
    assert provider.complete("sys", "user", SCHEMA) == {"title": "你好"}


def test_deepseek_requests_json_mode_and_schema(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return _ok_json({"title": "x"})

    provider = DeepSeekProvider("sk", "https://api.deepseek.com/", "deepseek-chat",
                                client=_client(handler))
    provider.complete("你是助手", "用户输入", SCHEMA, temperature=0.0)

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["auth"] == "Bearer sk"
    body = captured["body"]
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "你是助手" in body["messages"][0]["content"]
    assert "title" in body["messages"][0]["content"]  # schema 注入 system
    assert body["messages"][1] == {"role": "user", "content": "用户输入"}


# ---------------------------------------------------------------------------
# schema 校验失败 → 重试 → 成功 / 抛错
# ---------------------------------------------------------------------------

def test_deepseek_retries_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return _ok_json({"no_title": 1})  # 缺 required 字段
        return _ok_json({"title": "ok"})

    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m", client=_client(handler))
    assert provider.complete("sys", "user", SCHEMA) == {"title": "ok"}
    assert len(calls) == 2


def test_deepseek_raises_schema_error_after_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return _ok_json({"bad": 1})  # 永远不满足 schema

    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m", client=_client(handler))
    with pytest.raises(SchemaError):
        provider.complete("sys", "user", SCHEMA)
    assert len(calls) == 3  # 1 次 + 2 次重试


def test_deepseek_raises_schema_error_on_non_json():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "不是 json"}}]})

    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m", client=_client(handler))
    with pytest.raises(SchemaError):
        provider.complete("sys", "user", SCHEMA)
    assert len(calls) == 3


def test_deepseek_raises_schema_error_on_non_object():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "[1, 2]"}}]})

    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m", client=_client(handler))
    with pytest.raises(SchemaError):
        provider.complete("sys", "user", SCHEMA)


# ---------------------------------------------------------------------------
# 网络 / HTTP 错误：不重试，直接抛 LLMError
# ---------------------------------------------------------------------------

def test_deepseek_raises_llm_error_on_http_error():
    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m",
                                client=_client(lambda req: httpx.Response(500, text="boom")))
    with pytest.raises(LLMError):
        provider.complete("sys", "user", SCHEMA)


def test_deepseek_raises_llm_error_on_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    provider = DeepSeekProvider("sk", "https://api.deepseek.com", "m", client=_client(handler))
    with pytest.raises(LLMError):
        provider.complete("sys", "user", SCHEMA)


# ---------------------------------------------------------------------------
# 极简 schema 校验器
# ---------------------------------------------------------------------------

def test_validate_schema_required_and_types():
    assert _validate_schema(SCHEMA, {"title": "x"}) == []
    assert _validate_schema(SCHEMA, {}) != []
    assert _validate_schema(SCHEMA, {"title": 1}) != []
    assert _validate_schema(SCHEMA, "not-an-object") != []


def test_validate_schema_array_items_and_nested():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["a"],
            "properties": {"a": {"type": "integer"}},
        },
    }
    assert _validate_schema(schema, [{"a": 1}]) == []
    assert _validate_schema(schema, [{"a": "x"}]) != []
    assert _validate_schema(schema, [{}]) != []
    assert _validate_schema(schema, "not-an-array") != []


def test_validate_schema_enum_and_additional_properties():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"verdict": {"enum": ["proceed", "rework", "drop"]}},
    }
    assert _validate_schema(schema, {"verdict": "proceed"}) == []
    assert _validate_schema(schema, {"verdict": "maybe"}) != []
    assert _validate_schema(schema, {"verdict": "proceed", "extra": 1}) != []
