# -*- coding: utf-8 -*-
"""配置：LLM provider 档案、执行模式、审查护栏、路径。

设计成 ``config.json`` + 环境变量覆盖，类似 DSH 的 provider 切换：
  * ``python -m v3ai config use deepseek`` 切换云端；
  * ``python -m v3ai config use local`` 切换本地；
  * ``V3AI_API_KEY`` / ``V3AI_BASE_URL`` / ``V3AI_MODEL`` 可临时覆盖。
"""
from __future__ import annotations

import copy
import os
from typing import Any

from .util import atomic_write_json, read_json

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PKG_DIR, "config.json")

# 本地端点沿用旧 llm_config.json 的习惯；模型名可由用户改。
DEFAULT_PROFILES: dict[str, dict] = {
    "local": {
        "label": "本地 OpenAI 兼容端点",
        "base_url": "http://127.0.0.1:18200/v1",
        "api_key": "",
        "model": "kvmem-polystrata-qwen35",
        "temperature": 0.7,
        "max_tokens": 8192,
        "supports_tools": True,
        "timeout_s": 180,
    },
    "deepseek": {
        "label": "DeepSeek 云端",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.4,
        "max_tokens": 8192,
        "supports_tools": True,
        "timeout_s": 180,
    },
    "ollama": {
        "label": "Ollama 本地",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "qwen3:32b",
        "temperature": 0.7,
        "max_tokens": 8192,
        "supports_tools": True,
        "timeout_s": 300,
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "active_provider": "local",
    "providers": copy.deepcopy(DEFAULT_PROFILES),
    "source": "auto",                      # auto | live | file
    "loop": {
        "enabled": False,                  # 连续模式默认关闭
        "interval_s": 20,                  # 轮间隔秒；0 = 立刻
        "execute": True,                   # 连续模式下自动执行本轮动作
        "pause_if_stale": True,            # 快照过期时暂停连续模式
    },
    "paths": {
        "snapshot": os.path.join(ROOT_DIR, "snap.json"),
        "state_dir": os.path.join(PKG_DIR, "state"),
        "state_meta": os.path.join(PKG_DIR, "data", "state_meta.json"),
        "game_dir": None,                  # None = 自动发现
        "journal": None,                   # None = state_dir/journal.jsonl
    },
    "service": {
        "injector": "bridge/injector.exe",
        "dll": "bridge/v3inject.dll",
        "watch_script": "v3.py",
        "watch_interval": 5,
        "llm_start_cmd": "",
        "llm_stop_cmd": "",
    },
    "executor": {
        # dry_run：只审查/模拟，不写控制文件；bridge：真实写文件；mock：测试
        "mode": "dry_run",
        "bridge": {
            "dir": os.path.join(ROOT_DIR, "bridge"),
            "timeout_s": 30,
            "poll_s": 0.2,
            "log": "v3inject.log",
            # v3bridge 0.3 适配 1.15.1；doctor 用它判断指纹是否匹配
            "expected_exe_size": 100221048,
        },
    },
    "review": {
        "allow_conditional_pm": False,
        "max_pending": 16,
        "min_treasury": 0.0,
        "max_snapshot_age_s": 3600,
        "require_world_loaded": True,
        "warn_pm_patterns": ["worker_exploitation_", "pm_simple_organization"],
        "block_pm_patterns": [],
        "warn_pm_modifier_keys": [
            "bureaucracy", "construction", "mortality", "standard_of_living",
            "radicals", "loyalists", "qualifications", "literacy", "innovation",
            "authority", "influence", "infrastructure",
        ],
    },
    "agent": {
        "mode": "tools",                   # tools | oneshot（本地小模型快速路径）
        "max_tool_rounds": 24,             # 0 = 不限制（内部上限 200）
        "max_actions_per_cycle": 8,
        "max_levels_per_action": 5,
        "cycle_interval_s": 10,
        "auto_execute": True,
        "require_confirm": False,
        "allow_ai_strategy_switch": True,
        "max_tool_result_chars": 16000,    # 0 = 不限制
        "max_context_chars": 0,            # 0 = 不限制（近似：优先裁剪最长工具结果）
        "max_history_cycles": 12,          # 0 = 不限制
        "brief_level": "compact",          # compact | full
        "toolset": "full",                 # full | core（小模型/小上下文用 core）
        "freeze_cycle": True,              # 回合内冻结快照/队列；反馈处理完再刷新
        "temperature": None,
        "max_tokens": None,
    },
    # 决策计算层（v3calc.advisor）：算好价格反馈/风险/建造点，再交给 LLM 取舍
    "advisor": {
        "enabled": True,
        "char_budget": 2400,               # 简报字符上限
        "top": 8,                          # 候选包条数
        "plan_items": 3,                   # 贪心方案前 N 项进简报
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path() -> str:
    return os.environ.get("V3AI_CONFIG") or DEFAULT_CONFIG_PATH


class Config:
    def __init__(self, data: dict, path: str | None = None):
        self.file = path or config_path()
        self.data = _deep_merge(DEFAULT_CONFIG, data or {})

    # -- 生命周期 -----------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = path or config_path()
        raw = read_json(p, default=None)
        cfg = cls(raw or {}, p)
        if raw is None:
            cfg.save()
        return cfg

    def save(self) -> None:
        atomic_write_json(self.file, self.data)

    # -- 通用访问 -----------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    # -- provider -----------------------------------------------------
    @property
    def provider_name(self) -> str:
        return os.environ.get("V3AI_PROVIDER") or self.data.get("active_provider") or "local"

    def provider(self, name: str | None = None) -> dict:
        name = name or self.provider_name
        prof = copy.deepcopy((self.data.get("providers") or {}).get(name) or {})
        if not prof:
            raise KeyError("没有 provider 档案：%s" % name)
        # 环境变量覆盖（不落盘，适合临时切模型/换 key）
        env_map = {
            "base_url": "V3AI_BASE_URL",
            "api_key": "V3AI_API_KEY",
            "model": "V3AI_MODEL",
            "temperature": "V3AI_TEMPERATURE",
            "max_tokens": "V3AI_MAX_TOKENS",
            "timeout_s": "V3AI_TIMEOUT_S",
        }
        for key, env in env_map.items():
            v = os.environ.get(env)
            if v not in (None, ""):
                prof[key] = float(v) if key in ("temperature", "timeout_s") else (
                    int(v) if key == "max_tokens" else v)
        prof["name"] = name
        prof.setdefault("model", "unknown")
        prof.setdefault("base_url", "http://127.0.0.1:18200/v1")
        prof.setdefault("api_key", "")
        prof.setdefault("temperature", 0.7)
        prof.setdefault("max_tokens", 8192)
        prof.setdefault("supports_tools", True)
        prof.setdefault("timeout_s", 180)
        return prof

    def use_provider(self, name: str) -> None:
        if name not in (self.data.get("providers") or {}):
            raise KeyError("没有 provider 档案：%s" % name)
        self.data["active_provider"] = name
        self.save()

    def upsert_provider(self, name: str, values: dict) -> None:
        profs = self.data.setdefault("providers", {})
        cur = profs.setdefault(name, {})
        cur.update(values or {})
        self.save()

    # -- 便捷属性 -----------------------------------------------------
    @property
    def agent(self) -> dict:
        return self.data.get("agent") or {}

    @property
    def review(self) -> dict:
        return self.data.get("review") or {}

    @property
    def executor(self) -> dict:
        return self.data.get("executor") or {}

    @property
    def advisor(self) -> dict:
        return self.data.get("advisor") or {}

    @property
    def paths(self) -> dict:
        return self.data.get("paths") or {}

    def path(self, key: str) -> str:
        v = self.paths.get(key)
        if v:
            return os.path.expanduser(str(v))
        if key == "state_dir":
            return os.path.join(PKG_DIR, "state")
        if key == "snapshot":
            return os.path.join(ROOT_DIR, "snap.json")
        if key == "state_meta":
            return os.path.join(PKG_DIR, "data", "state_meta.json")
        return ""

    @property
    def journal_path(self) -> str:
        p = self.paths.get("journal")
        if p:
            return os.path.expanduser(str(p))
        return os.path.join(self.path("state_dir"), "journal.jsonl")

    @property
    def strategy_path(self) -> str:
        return os.path.join(self.path("state_dir"), "strategy.json")
