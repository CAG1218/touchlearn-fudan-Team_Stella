"""Optional Streamlit preview for the two TouchLearn cases.

Run with a standard Python environment:

    pip install -e ".[ui]"
    streamlit run app/streamlit_app.py

The UI calls the same compiler as the CLI.  It does not re-implement layout.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from touchlearn.audio import build_script  # noqa: E402
from touchlearn.geometry import build_mesh  # noqa: E402
from touchlearn.layout import build_layout  # noqa: E402
from touchlearn.legend import legend_to_svg  # noqa: E402
from touchlearn.model import DiagramError, diagram_from_dict  # noqa: E402
from touchlearn.printability import check_layout  # noqa: E402
from touchlearn.schema import has_errors, validate_payload  # noqa: E402
from touchlearn.stl import write_binary_stl  # noqa: E402
from touchlearn.svg import layout_to_svg  # noqa: E402

CASES = {
    "六轴机械臂": ROOT / "examples" / "robot_arm" / "diagram.json",
    "二次函数 y = x² − 2x − 3": ROOT / "examples" / "quadratic" / "diagram.json",
}


@st.cache_data(show_spinner=False)
def compile_case(path_text: str, modified_ns: int) -> dict:
    payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    issues = validate_payload(payload)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise DiagramError("; ".join(f"{issue.path}: {issue.message}" for issue in errors))

    diagram = diagram_from_dict(payload)
    layout = build_layout(diagram)
    mesh = build_mesh(layout)
    report = check_layout(diagram, layout, mesh)

    with tempfile.TemporaryDirectory() as directory:
        stl_path = write_binary_stl(mesh, Path(directory) / "model.stl")
        stl_bytes = stl_path.read_bytes()

    return {
        "title": diagram.title,
        "title_zh": diagram.title_zh,
        "id": diagram.id,
        "svg": layout_to_svg(layout),
        "legend": legend_to_svg(layout),
        "stl": stl_bytes,
        "report": report.to_dict(),
        "audio": build_script(diagram),
    }


def _preview(svg: str, height: int) -> None:
    responsive = svg.replace('width="148mm" height="210mm"', 'width="100%" height="auto"')
    responsive = responsive.replace('width="105mm" height="148mm"', 'width="100%" height="auto"')
    st.components.v1.html(f'<div style="max-width:560px">{responsive}</div>', height=height)


def main() -> None:
    st.set_page_config(page_title="触见 TouchLearn", page_icon="◼", layout="wide")
    st.title("触见 TouchLearn")
    st.caption("把 STEM 图重构成可触摸的 3D 结构图，配编号盲文和扫码音频。")

    name = st.sidebar.radio("案例", list(CASES))
    source = CASES[name]
    modified_ns = source.stat().st_mtime_ns
    result = compile_case(str(source), modified_ns)
    report = result["report"]

    st.subheader(f"{result['title_zh']} / {result['title']}")
    if report["passed"]:
        st.success("可打印性检查 PASS")
    else:
        st.error("可打印性检查 FAIL，请先修复布局")

    left, right = st.columns([3, 2])

    with left:
        tabs = st.tabs(["A5 触觉板预览", "A6 图例卡"])
        with tabs[0]:
            _preview(result["svg"], 760)
        with tabs[1]:
            _preview(result["legend"], 560)

    with right:
        st.markdown("### 指标")
        metrics = report["metrics"]
        mesh = metrics.get("mesh", {})
        st.metric("三角形", f"{mesh.get('triangles', '—'):,}")
        st.metric("体积", f"{mesh.get('volume_mm3', '—')} mm³")
        st.metric("水密", "是" if mesh.get("watertight") else "否")
        st.metric("最小标签间距", f"{metrics.get('min_label_spacing_mm', '—')} mm")
        st.metric("最小净空", f"{metrics.get('min_label_clearance_mm', '—')} mm")

        st.markdown("### 下载")
        st.download_button("STL", result["stl"], file_name=f"{result['id']}.stl", mime="model/stl")
        st.download_button(
            "SVG 预览",
            result["svg"],
            file_name=f"{result['id']}_preview.svg",
            mime="image/svg+xml",
        )
        st.download_button(
            "A6 图例卡",
            result["legend"],
            file_name=f"{result['id']}_legend.svg",
            mime="image/svg+xml",
        )
        st.download_button(
            "音频脚本",
            result["audio"],
            file_name=f"{result['id']}_audio.txt",
            mime="text/plain",
        )
        st.download_button(
            "检查报告",
            json.dumps(report, ensure_ascii=False, indent=2),
            file_name=f"{result['id']}_report.json",
            mime="application/json",
        )

    with st.expander("检查问题"):
        if report["issues"]:
            st.dataframe(report["issues"], use_container_width=True)
        else:
            st.write("没有 error 或 warning。")

    with st.expander("音频脚本"):
        st.text(result["audio"])

    st.caption(
        "v0.1.0：两个案例已通过自动检查，但尚未实体打印，也尚未经过盲人用户测试。"
        " 详见 README 的“当前状态与边界”。"
    )


if __name__ == "__main__":
    main()
