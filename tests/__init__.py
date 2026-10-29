"""Test package bootstrap.

The repository uses a src layout.  Adding ``src`` here lets the standard
library test runner import the package without an editable install, which is
useful on the competition laptop where only the bundled Python is available.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Tests must never reach a paid endpoint.  Clearing these before the package is
# imported also stops ``llm.load_env()`` from filling them out of a developer's
# real ``.env`` (the loader skips names already present in ``os.environ``).
for _name in (
    "TOUCHLEARN_LLM_PROVIDER",
    "TOUCHLEARN_LLM_URL",
    "TOUCHLEARN_LLM_KEY",
    "TOUCHLEARN_LLM_MODEL",
    "TOUCHLEARN_VLM_PROVIDER",
    "TOUCHLEARN_VLM_URL",
    "TOUCHLEARN_VLM_KEY",
    "TOUCHLEARN_VLM_MODEL",
    "TOUCHLEARN_LLM_TIMEOUT",
    "TOUCHLEARN_LLM_EFFORT",
    "TOUCHLEARN_SCENE_MODEL",
    "TOUCHLEARN_SCENE_EFFORT",
    "TOUCHLEARN_LLM_PROXY",
):
    os.environ[_name] = ""

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"
