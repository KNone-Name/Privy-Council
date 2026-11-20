"""在 v3live 目录内直接运行：python run.py <命令>

等价于在父目录运行 python -m v3live <命令>。
"""
import os
import sys

# GBK 控制台下遇到编码不了的字符不要崩，替换成 ? 即可
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v3live.cli import main

if __name__ == "__main__":
    sys.exit(main())
