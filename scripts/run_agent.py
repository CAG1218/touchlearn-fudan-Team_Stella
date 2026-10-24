#!/usr/bin/env python
"""Launch the TouchLearn interactive agent from a source checkout.

The embedded Python used on the competition laptop does not honour
``PYTHONPATH``, so this launcher inserts ``src`` explicitly before importing
the package.  It also works with a normal CPython installation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from touchlearn.agent.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
