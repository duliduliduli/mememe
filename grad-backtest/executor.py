#!/usr/bin/env python3
"""Load the real executor from a local gzip+base64 payload so tests and Railway
never hit the network on import."""
from __future__ import annotations

import base64
import gzip
from pathlib import Path

_payload = Path(__file__).with_name("executor_payload.b64")
_src = gzip.decompress(base64.b64decode(_payload.read_text().encode("ascii")))
exec(compile(_src, str(Path(__file__).with_name("executor_restored.py")), "exec"), globals())
