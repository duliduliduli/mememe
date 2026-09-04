#!/usr/bin/env python3
"""Assemble the executor from local part files so imports never hit the network."""
from pathlib import Path

_dir = Path(__file__).resolve().parent
_src = (_dir / "executor_part1.py").read_text() + (_dir / "executor_part2.py").read_text()
exec(compile(_src, str(_dir / "executor_assembled.py"), "exec"), globals())
