"""Test package bootstrap.

The repository uses a src layout.  Adding ``src`` here lets the standard
library test runner import the package without an editable install, which is
useful on the competition laptop where only the bundled Python is available.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"
