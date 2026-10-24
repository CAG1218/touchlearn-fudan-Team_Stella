"""Build both v0 cases into ``dist/`` and refresh ``docs/images/``.

Run from any directory:

    python scripts/build_examples.py

The script fails when either case does not pass printability.  It never
silently ships a broken plate.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from touchlearn import __version__  # noqa: E402
from touchlearn.cli import _checked_diagram, _compile  # noqa: E402
from touchlearn.stl import read_stl_info  # noqa: E402

CASES = (
    ROOT / "examples" / "robot_arm" / "diagram.json",
    ROOT / "examples" / "quadratic" / "diagram.json",
)
DIST = ROOT / "dist"
IMAGES = ROOT / "docs" / "images"


def build_case(source: Path) -> dict:
    diagram = _checked_diagram(source)
    out_dir = DIST / diagram.id
    result = _compile(diagram, out_dir)

    stl_info = read_stl_info(Path(result["paths"]["stl"]))
    report = result["report"]
    summary = {
        "id": diagram.id,
        "title": diagram.title,
        "title_zh": diagram.title_zh,
        "printability": "PASS" if report.passed else "FAIL",
        "issues": [issue.to_dict() for issue in report.issues],
        "stl": {
            "sha256": result["paths"]["stl_sha256"],
            "triangles": stl_info["triangles"],
            "bytes": stl_info["bytes"],
        },
        "mesh": report.metrics.get("mesh", {}),
        "files": {
            name: str(Path(result["paths"][name]).relative_to(ROOT)).replace("\\", "/")
            for name in ("stl", "svg", "legend", "report", "trace", "audio", "diagram")
        },
    }

    shutil.copyfile(result["paths"]["svg"], IMAGES / f"{diagram.id}.svg")
    shutil.copyfile(result["paths"]["legend"], IMAGES / f"{diagram.id}_legend.svg")

    print(
        f"{diagram.id}: {'PASS' if report.passed else 'FAIL'} | "
        f"{stl_info['triangles']} triangles | "
        f"{report.metrics.get('mesh', {}).get('volume_mm3', '?')} mm3 | "
        f"sha256 {result['paths']['stl_sha256'][:12]}..."
    )
    for issue in report.issues:
        print(f"  [{issue.severity}] {issue.code}: {issue.message}")
    return summary


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)

    cases = [build_case(source) for source in CASES]
    manifest = {
        "tool": f"touchlearn {__version__}",
        "canvas_mm": [148.0, 210.0],
        "cases": cases,
    }
    manifest_path = DIST / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"manifest: {manifest_path}")

    failed = [case for case in cases if case["printability"] != "PASS"]
    if failed:
        print(f"FAILED: {', '.join(case['id'] for case in failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
