# -*- coding: utf-8 -*-
"""v3ai —— Victoria 3 决策层（感知 / 世界模型 / 审查 / 执行 / LLM 循环）。

三端分工：
  * 数据端：``v3calc.live`` / ``v3calc.savefile`` 读事实，``v3calc.gamedata`` 读规则；
  * 计算端：``v3calc.gdp`` / ``pm`` / ``scenario`` 算 GDP、PM 可用性、换 PM 增量；
    ``v3calc.advisor``（经 ``v3ai.advisor_bridge``）算价格反馈、候选包、简报、台账；
  * 执行端：``v3bridge`` 文件协议（``requests.txt`` / ``pmm.txt`` / ``queue.txt``）。

本包只做决策与编排，不直接写游戏内存：
  LLM 看概况 → 自主调工具 → 提议动作 → 脚本审查 → 执行端 → 结果反馈。
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
