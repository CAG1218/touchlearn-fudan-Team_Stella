"""Command line interface for the TouchLearn compiler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .audio import write_script
from .geometry import build_mesh
from .layout import build_layout
from .legend import write_legend_svg
from .model import Diagram, DiagramError, diagram_from_dict
from .printability import check_layout
from .schema import has_errors, validate_payload
from .stl import sha256_file, write_binary_stl
from .svg import write_svg
from .trace import Trace


def _read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiagramError(f"file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise DiagramError(f"invalid JSON in {source}: {exc}") from exc


def _checked_diagram(path: str | Path) -> Diagram:
    payload = _read_json(path)
    issues = validate_payload(payload)
    if has_errors(issues):
        messages = "; ".join(f"{issue.path}: {issue.message}" for issue in issues if issue.severity == "error")
        raise DiagramError(f"{path} failed validation: {messages}")
    return diagram_from_dict(payload)


def _print_issues(issues: list[Any]) -> None:
    if not issues:
        print("OK: no issues")
        return
    for issue in issues:
        print(f"[{issue.severity}] {issue.path}: {issue.message}")


def _feature_manifest(layout: Any) -> dict[str, Any]:
    return {
        "summary": layout.summary(),
        "features": [
            {
                "id": feature.id,
                "kind": feature.kind,
                "label": feature.label,
                "semantic_id": feature.semantic_id,
                "index": feature.index,
                "bbox_mm": [round(value, 3) for value in feature.bbox()],
                "primitives": len(feature.primitives),
            }
            for feature in layout.features
        ],
    }


def cmd_validate(args: argparse.Namespace) -> int:
    payload = _read_json(args.diagram)
    issues = validate_payload(payload)
    _print_issues(issues)
    return 0 if not has_errors(issues) else 1


def cmd_layout(args: argparse.Namespace) -> int:
    diagram = _checked_diagram(args.diagram)
    layout = build_layout(diagram)
    out = Path(args.out) if args.out else Path("build") / diagram.id / "preview.svg"
    write_svg(layout, out)
    manifest = out.with_name("layout.json")
    manifest.write_text(json.dumps(_feature_manifest(layout), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"layout: {layout.summary()}")
    print(f"svg: {out}")
    print(f"manifest: {manifest}")
    return 0


def _compile(diagram: Diagram, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = build_layout(diagram)
    mesh = build_mesh(layout)
    report = check_layout(diagram, layout, mesh)

    trace = Trace()
    trace.add(
        "validate",
        "schema",
        "diagram accepted",
        f"schema_version={diagram.schema_version}; {len(diagram.elements)} semantic elements",
    )
    trace.add_layout(layout.trace)
    trace.add(
        "geometry",
        "extrude",
        f"{mesh.triangle_count()} triangles",
        "base prism plus one prism per tactile primitive; features embedded 0.05 mm into the base",
        mesh.stats(),
    )
    trace.add(
        "printability",
        "check",
        "PASS" if report.passed else "FAIL",
        f"{len(report.issues)} issue(s)",
        report.metrics,
    )

    stl_path = write_binary_stl(mesh, out_dir / "model.stl")
    svg_path = write_svg(layout, out_dir / "preview.svg")
    legend_path = write_legend_svg(layout, out_dir / "legend.svg")
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    trace_path = trace.write_jsonl(out_dir / "trace.jsonl")
    audio_path = write_script(diagram, out_dir / "audio.txt")
    diagram_path = out_dir / "diagram.json"
    diagram_path.write_text(json.dumps(diagram.raw, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "diagram": diagram,
        "layout": layout,
        "mesh": mesh,
        "report": report,
        "paths": {
            "out_dir": str(out_dir),
            "stl": str(stl_path),
            "svg": str(svg_path),
            "legend": str(legend_path),
            "report": str(report_path),
            "trace": str(trace_path),
            "audio": str(audio_path),
            "diagram": str(diagram_path),
            "stl_sha256": sha256_file(stl_path),
        },
    }


def _print_build_result(result: dict[str, Any]) -> None:
    report = result["report"]
    print(f"model: {result['paths']['stl']}")
    print(f"preview: {result['paths']['svg']}")
    print(f"legend: {result['paths']['legend']}")
    print(f"report: {result['paths']['report']}")
    print(f"trace: {result['paths']['trace']}")
    print(f"audio: {result['paths']['audio']}")
    print(f"stl_sha256: {result['paths']['stl_sha256']}")
    print(f"printability: {'PASS' if report.passed else 'FAIL'}")
    for issue in report.issues:
        print(f"  [{issue.severity}] {issue.code}: {issue.message}")


def cmd_build(args: argparse.Namespace) -> int:
    diagram = _checked_diagram(args.diagram)
    out_dir = Path(args.out) if args.out else Path("dist") / diagram.id
    result = _compile(diagram, out_dir)
    _print_build_result(result)
    return 0 if result["report"].passed else 1


def cmd_check(args: argparse.Namespace) -> int:
    diagram = _checked_diagram(args.diagram)
    layout = build_layout(diagram)
    mesh = build_mesh(layout)
    report = check_layout(diagram, layout, mesh)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"diagram: {diagram.id}")
        print(f"printability: {'PASS' if report.passed else 'FAIL'}")
        print(f"metrics: {json.dumps(report.metrics, ensure_ascii=False)}")
        for issue in report.issues:
            print(f"  [{issue.severity}] {issue.code}: {issue.message}")
    return 0 if report.passed else 1


def cmd_audio(args: argparse.Namespace) -> int:
    diagram = _checked_diagram(args.diagram)
    out = Path(args.out) if args.out else Path("build") / diagram.id / "audio.txt"
    write_script(diagram, out)
    print(f"audio script: {out}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    diagram = _checked_diagram(args.diagram)
    out_dir = Path(args.out) if args.out else Path("dist") / diagram.id
    result = _compile(diagram, out_dir)
    _print_build_result(result)
    return 0 if result["report"].passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="touchlearn",
        description="Compile a semantic STEM diagram into a tactile A5 plate.",
    )
    parser.add_argument("--version", action="version", version=f"touchlearn {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a diagram JSON file")
    validate_parser.add_argument("diagram")
    validate_parser.set_defaults(func=cmd_validate)

    layout_parser = subparsers.add_parser("layout", help="generate an SVG preview and layout manifest")
    layout_parser.add_argument("diagram")
    layout_parser.add_argument("-o", "--out", help="output SVG path")
    layout_parser.set_defaults(func=cmd_layout)

    build_parser_ = subparsers.add_parser("build", help="generate STL, SVG, report, trace and audio script")
    build_parser_.add_argument("diagram")
    build_parser_.add_argument("-o", "--out", help="output directory")
    build_parser_.set_defaults(func=cmd_build)

    check_parser = subparsers.add_parser("check", help="run printability and tactile-safety checks")
    check_parser.add_argument("diagram")
    check_parser.add_argument("--json", action="store_true", help="print the full JSON report")
    check_parser.set_defaults(func=cmd_check)

    audio_parser = subparsers.add_parser("audio", help="write the narration script")
    audio_parser.add_argument("diagram")
    audio_parser.add_argument("-o", "--out", help="output text path")
    audio_parser.set_defaults(func=cmd_audio)

    all_parser = subparsers.add_parser("all", help="run validate, layout, build and check in one pass")
    all_parser.add_argument("diagram")
    all_parser.add_argument("-o", "--out", help="output directory")
    all_parser.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DiagramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
