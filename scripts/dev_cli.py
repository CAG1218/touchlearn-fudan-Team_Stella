"""Run the CLI from a checkout without installing the package.

Usage:
    python scripts/dev_cli.py validate examples/robot_arm/diagram.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from touchlearn.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
