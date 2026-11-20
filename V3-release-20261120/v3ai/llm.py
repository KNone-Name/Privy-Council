# -*- coding: utf-8 -*-
"""LLM 接入层：OpenAI 兼容的 chat/completions + 工具调用。

支持云端 / 本地任意 OpenAI 兼容端点（DeepSeek、Ollama、vLLM、llama.cpp server…），
由 ``config.json`` 的 provider 档案切换；本模块不绑定任何厂商 SDK。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .util import safe_float, truncate_text


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str = ""
    raw: dict = field(default_factory=dict)
    reasoning: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(str(part.get("text", "")))
                elif "text" in part:
                    out.append(str(part["text"]))
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return "" if content is None else str(content)


def parse_tool_call(tc: dict, i: int = 0) -> ToolCall:
    fn = tc.get("function") or {}
    name = fn.get("name") or tc.get("name") or ""
    raw = fn.get("arguments")
    if raw is None:
        raw = tc.get("arguments") or "{}"
    if isinstance(raw, dict):
        args = raw
        raw_s = json.dumps(raw, ensure_ascii=False)
    else:
        raw_s = str(raw)
        try:
            args = json.loads(raw_s) if raw_s.strip() else {}
        except ValueError:
            args = {}
            raw_s = raw_s
    return ToolCall(id=str(tc.get("id") or "call_%d" % i), name=name,
                    arguments=args if isinstance(args, dict) else {},
                    raw_arguments=raw_s)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_text_tool_call(text: str) -> ToolCall | None:
    """兼容不支持原生 tool_calls 的模型：从正文里捡一个 ``{"tool":...}``。

    只在很保守的形状下启用，避免把最终 JSON 误当动作。
    """
    if not text:
        return None
    candidates = []
    m = _JSON_BLOCK.search(text)
    if m:
        candidates.append(m.group(1))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("arguments", obj.get("args", {}))
        if isinstance(name, str) and name and isinstance(args, dict):
            return ToolCall(id="txt_%d" % int(time.time() * 1000), name=name,
                            arguments=args, raw_arguments=json.dumps(args, ensure_ascii=False))
    return None


class LLMClient:
    def __init__(self, profile: dict):
        self.profile = dict(profile or {})
        self.name = self.profile.get("name", "?")
        self.model = self.profile.get("model", "")
        self.base_url = str(self.profile.get("base_url", "")).rstrip("/")
        self.api_key = str(self.profile.get("api_key", ""))
        self.timeout = float(self.profile.get("timeout_s", 180) or 180)
        self.max_retries = int(self.profile.get("max_retries", 2) or 2)

    # -- HTTP ---------------------------------------------------------
    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self._endpoint(), data=data,
                                         headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    raise LLMError("LLM 返回不是 JSON：%s" % truncate_text(raw, 500)) from exc
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                last = LLMError("HTTP %s %s：%s" % (exc.code, exc.reason,
                                                  truncate_text(body, 800)))
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise last from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = LLMError("连接 LLM 失败：%s" % exc)
                if attempt < self.max_retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise last from exc
        raise last or LLMError("LLM 调用失败")

    # -- chat ---------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto", temperature: float | None = None,
             max_tokens: int | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": (self.profile.get("temperature", 0.7)
                            if temperature is None else temperature),
            "max_tokens": (self.profile.get("max_tokens", 8192)
                           if max_tokens is None else max_tokens),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        obj = self._post(payload)
        return self._parse(obj)

    def _parse(self, obj: dict) -> LLMResponse:
        choices = obj.get("choices") or []
        if not choices:
            raise LLMError("LLM 返回没有 choices：%s" % truncate_text(json.dumps(obj, ensure_ascii=False), 500))
        msg = choices[0].get("message") or {}
        content = _text_of(msg.get("content"))
        reasoning = _text_of(msg.get("reasoning_content") or msg.get("reasoning"))
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            calls.append(parse_tool_call(tc, i))
        fc = msg.get("function_call")
        if fc and not calls:
            calls.append(parse_tool_call({"function": fc}, 0))
        if not calls and content:
            one = parse_text_tool_call(content)
            if one is not None:
                calls = [one]
        return LLMResponse(
            content=content,
            tool_calls=calls,
            usage=obj.get("usage") or {},
            model=obj.get("model") or self.model,
            raw=obj,
            reasoning=reasoning,
        )

    def list_models(self) -> list[str]:
        url = self.base_url
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        if not url.endswith("/models"):
            url = url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=min(self.timeout, 30)) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
        return [m.get("id", "") for m in (obj.get("data") or []) if m.get("id")]

    def ping(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
            return True, "ok，模型数 %d%s" % (len(models), ("：" + ", ".join(models[:5])) if models else "")
        except Exception as exc:
            return False, str(exc)


class MockLLM:
    """脚本化 LLM：测试/离线回放用，不联网。"""

    def __init__(self, responses: list[LLMResponse | dict] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.model = "mock"
        self.name = "mock"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto", temperature: float | None = None,
             max_tokens: int | None = None) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            return LLMResponse(content="（mock 无更多响应）", model="mock")
        r = self.responses.pop(0)
        if isinstance(r, LLMResponse):
            return r
        return LLMResponse(
            content=r.get("content", ""),
            tool_calls=[ToolCall(**tc) if isinstance(tc, dict) and "name" in tc
                        else parse_tool_call(tc) for tc in (r.get("tool_calls") or [])],
            model="mock",
        )

    def list_models(self) -> list[str]:
        return ["mock"]

    def ping(self) -> tuple[bool, str]:
        return True, "mock"


def tool_call(name: str, arguments: dict | None = None, call_id: str | None = None,
              content: str = "") -> LLMResponse:
    """便捷构造：一条带单个工具调用的 mock 响应。"""
    return LLMResponse(
        content=content,
        tool_calls=[ToolCall(id=call_id or ("mock_" + name), name=name,
                             arguments=arguments or {},
                             raw_arguments=json.dumps(arguments or {}, ensure_ascii=False))],
        model="mock",
    )


def make_llm(profile: dict | None = None):
    if not profile or profile.get("name") == "mock":
        return MockLLM([])
    return LLMClient(profile)


def parse_temperature(v: Any) -> float | None:
    return safe_float(v, None)
