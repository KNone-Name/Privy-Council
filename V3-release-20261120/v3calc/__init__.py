# -*- coding: utf-8 -*-
"""v3calc —— Victoria 3 数据读取（总程序）+ 计算层。

层次：
    v3live   从游戏进程内存读原始数据（已由逆向层验证）
    v3calc   本包：快照模型 / 实时与存档后端 / 游戏数据模型 / GDP / PM 可用性
             + 决策计算层（market / construction / candidates / signals /
               brief / ledger / advisor，见 ADVISOR.md）
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
