# -*- coding: utf-8 -*-
"""统一入口。

计算层：
    python v3.py read
    python v3.py gdp --save D:\\...\\save.txt --calibrate
    python v3.py pm --building building_food_industry
    python v3.py upgrades --top 20

决策层（v3ai）：
    python v3.py ai doctor
    python v3.py ai overview --snapshot snap.json
    python v3.py ai run --live --mode dry_run
    python v3.py ai mcp
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ai":
        from v3ai.cli import main as ai_main  # noqa: E402
        raise SystemExit(ai_main(sys.argv[2:]))
    from v3calc.cli import main  # noqa: E402
    raise SystemExit(main())
