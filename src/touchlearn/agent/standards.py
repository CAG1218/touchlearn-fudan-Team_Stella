"""Standards registry and compliance gates.

Two rules govern this module:

1. A number is only labelled with a standard when the number can be traced to
   a source.  Public summaries are marked as such; paywalled clauses are marked
   ``needs_review`` instead of being invented.
2. A design can be *printable* without being *fully compliant*.  The report
   keeps those two conclusions separate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import braille
from ..geometry import Mesh, feature_polygons
from ..layout import LayoutResult, Primitive
from ..printability import feature_distance

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_DEVIATION = "DEVIATION"
STATUS_REVIEW = "NEEDS_REVIEW"
STATUS_NA = "NOT_APPLICABLE"


@dataclass(frozen=True)
class StandardRef:
    number: str
    title: str
    status: str
    published: str
    effective: str
    authority: str
    url: str
    scope: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "status": self.status,
            "published": self.published,
            "effective": self.effective,
            "authority": self.authority,
            "url": self.url,
            "scope": self.scope,
            "note": self.note,
        }


@dataclass(frozen=True)
class Rule:
    id: str
    standard: str
    clause: str
    title: str
    requirement: str
    value: str
    unit: str
    source_url: str
    confidence: str
    applicability: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "standard": self.standard,
            "clause": self.clause,
            "title": self.title,
            "requirement": self.requirement,
            "value": self.value,
            "unit": self.unit,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "applicability": self.applicability,
            "note": self.note,
        }


@dataclass(frozen=True)
class Check:
    rule_id: str
    status: str
    measured: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "measured": self.measured,
            "message": self.message,
        }


@dataclass
class ComplianceReport:
    profile: str
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def printable(self) -> bool:
        return not any(check.status == STATUS_FAIL for check in self.checks)

    @property
    def standards_clear(self) -> bool:
        return not any(check.status in {STATUS_FAIL, STATUS_DEVIATION, STATUS_REVIEW} for check in self.checks)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for check in self.checks:
            result[check.status] = result.get(check.status, 0) + 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "printable": self.printable,
            "standards_clear": self.standards_clear,
            "counts": self.counts(),
            "checks": [check.to_dict() for check in self.checks],
            "metrics": self.metrics,
            "standards": [standard.to_dict() for standard in STANDARDS],
        }


STANDARDS: tuple[StandardRef, ...] = (
    StandardRef(
        number="GB/T 40142-2021",
        title="触摸图形设计及图例",
        status="现行",
        published="2021-05-21",
        effective="2021-05-21",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=E72CE9A9B4200759B27F08F13118F5B8",
        scope="触摸图形的组成要素、设计、校对及图例；适用于触摸图形的设计、制作、应用。",
        note="国家标准全文公开系统只提供题录，公开流传的 PDF 仅含封面与目次。本项目的数值来自民政部国家康复辅具研究中心的公开解读，购标后必须逐条复核。",
    ),
    StandardRef(
        number="GB/T 39758-2021",
        title="无障碍设计 盲文在标志、设备和器具上的应用",
        status="现行",
        published="2021-03-09",
        effective="2021-03-09",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=63A112B519D601DBA74264AAEB314DB0",
        scope="标志、设备和器具上的盲文尺寸参数、材料特性及应用指南；等同采用 ISO 17049:2013。",
        note="适用于标志/设备/器具，不是触摸教材专用标准；作为当前可核实的盲文三维尺寸交叉依据。",
    ),
    StandardRef(
        number="GB/T 44725-2024",
        title="盲文出版物版式",
        status="现行",
        published="2024-10-26",
        effective="2025-02-01",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=724E7E79DD866A0D29A2EF7B1F2755B0",
        scope="盲文图书、盲文期刊的版式，包括封面、版权页、辅文、正文、插图及表格；也适用于内部资料性出版物。",
        note="单张 A5/A4 触觉板不是盲文出版物，本版不强制适用；当输出包含盲文说明册或图例册时启用。",
    ),
    StandardRef(
        number="GF 0019-2018",
        title="国家通用盲文方案",
        status="现行语言文字规范",
        published="2018-03-09",
        effective="2018-07-01",
        authority="教育部、国家语委、中国残联",
        url="http://www.moe.gov.cn/jyb_sjzl/ziliao/A19/201807/W020180725666187054299.pdf",
        scope="国家通用盲文的声母、韵母、声调、标点和书写规则。",
        note="编码规则必须以此为准。当前 v1 触觉板仍使用 UEB 英文与阿拉伯数字，中文盲文标注尚未开启，报告中单列为待复核项。",
    ),
    StandardRef(
        number="国歌盲文方案（2025）",
        title="《中华人民共和国国歌》国家通用盲文方案",
        status="现行语言文字规范",
        published="2024-12-04",
        effective="2025-03-01",
        authority="教育部、国家语委、中国残联",
        url="https://www.gov.cn/yaowen/liebiao/202412/content_6990996.htm",
        scope="国歌的国家通用盲文书写方案，包括五线谱方案和简谱方案。",
        note="当前产品不生成乐谱，本版不适用；若后续增加音乐/国歌内容，必须单独实现并复核。",
    ),
    StandardRef(
        number="GB/T 45281-2025",
        title="盲用多媒体信息处理技术要求",
        status="现行",
        published="2025-02-28",
        effective="2025-02-28",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=DFBB30B952CDA59D1B822398B2C00B8F",
        scope="为视障者解说多媒体信息所需要的通用制作要求、脚本制作要求、解说录制及视频要求。",
        note="用于扫码音频的制作清单；本版未取得条文全文，脚本与录音只做公开范围的可核查约束。",
    ),
    StandardRef(
        number="GB/T 45567-2025",
        title="信息与文献 盲用资源描述",
        status="现行",
        published="2025-04-25",
        effective="2025-11-01",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://std.samr.gov.cn/gb/search/gbDetailed?id=33D40F11614A5D92E06397BE0A0A5B93",
        scope="盲文文献、明盲对照、触摸地图、触摸乐谱、电子盲文、无障碍电影等盲用资源的描述。",
        note="用于作业产物的元数据字段设计；本版先保留题名、类型、适用对象、介质、来源和版本字段。",
    ),
    StandardRef(
        number="GB/T 18028-2010",
        title="中国盲文数学、物理、化学符号",
        status="现行（复审结论：修订）",
        published="2011-01-14",
        effective="2011-05-01",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D7D8D5D3A7E05397BE0A0AB82A",
        scope="盲文数学、物理、化学符号的写法和使用规则；适用于盲文教育、出版及有关领域。",
        note="2026-03-02 复审结论为修订，修订计划 20261379-T-314 正在起草。v1 尚未接入中文盲文数学符号，函数板使用 UEB/ASCII 标注并单列为待复核项。",
    ),
    StandardRef(
        number="GB/T 16431-2008",
        title="中国盲文音乐符号",
        status="现行",
        published="2008-09-19",
        effective="2009-03-01",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=D9F2C762080DA763F452CC173F6EFC28",
        scope="盲文常用音乐符号、声乐符号、器乐符号和常用记谱法；适用于盲文音乐教育与出版。",
        note="当前产品不生成乐谱，本版不适用；若扩展音乐内容，必须与国歌盲文方案一并复核。",
    ),
    StandardRef(
        number="GB/T 15720-2008",
        title="中国盲文",
        status="已废止（2025-11-03）",
        published="2008-09-19",
        effective="2009-03-01",
        authority="国家市场监督管理总局、国家标准化管理委员会",
        url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=484E4150A1F62E81B0553422E6A8A292",
        scope="盲符结构与参数、现行盲文方案、汉语双拼盲文方案、分词连写和书写格式。",
        note="不得再作为现行符合性依据。其点径/点距/方距数值仅作为历史工程参考；当前编码依据改为 GF 0019-2018。",
    ),
)


RULES: tuple[Rule, ...] = (
    Rule(
        id="tactile.point_diameter",
        standard="GB/T 40142-2021",
        clause="公开解读（条文待购标复核）",
        title="点状触觉符号最小直径",
        requirement="点状符号直径不小于 2 mm",
        value=">=2.0",
        unit="mm",
        source_url="https://kffj.mca.gov.cn/n852/c26013/content.html",
        confidence="public-summary",
        applicability="tactile_plate",
        note="来源为国家康复辅具研究中心对标准研制过程的公开解读，不是标准原文。",
    ),
    Rule(
        id="tactile.line_height",
        standard="GB/T 40142-2021",
        clause="公开解读（条文待购标复核）",
        title="触觉线条高度",
        requirement="公开解读给出的线条高度为 0.1–0.15 mm",
        value="0.10-0.15",
        unit="mm",
        source_url="https://kffj.mca.gov.cn/n852/c26013/content.html",
        confidence="public-summary",
        applicability="tactile_plate",
        note="该值对应纸张/微胶囊类触觉图工艺。FDM 采用 0.8 mm 耐用剖面，报告中标为工艺偏离，不冒充标准符合。",
    ),
    Rule(
        id="tactile.annotation_distance",
        standard="GB/T 40142-2021",
        clause="公开解读（条文待购标复核）",
        title="标注与图形的距离",
        requirement="标注距图形边缘 2–12 mm",
        value="2.0-12.0",
        unit="mm",
        source_url="https://kffj.mca.gov.cn/n852/c26013/content.html",
        confidence="public-summary",
        applicability="tactile_plate",
    ),
    Rule(
        id="braille.dot_diameter",
        standard="GB/T 39758-2021",
        clause="尺寸参数（二手解读，待购标复核）",
        title="盲文点径",
        requirement="盲文点底部直径 1.0–1.7 mm",
        value="1.0-1.7",
        unit="mm",
        source_url="https://www.antpedia.com/standard/1685272379-10.html",
        confidence="secondary",
        applicability="tactile_plate",
        note="该标准等同采用 ISO 17049:2013；数值来自公开解读，购标后需核对表 1。",
    ),
    Rule(
        id="braille.dot_height",
        standard="GB/T 39758-2021",
        clause="尺寸参数（二手解读，待购标复核）",
        title="盲文点高",
        requirement="盲文点中心高度 0.3–0.7 mm",
        value="0.3-0.7",
        unit="mm",
        source_url="https://www.antpedia.com/standard/1685272379-10.html",
        confidence="secondary",
        applicability="tactile_plate",
    ),
    Rule(
        id="braille.clearance",
        standard="GB/T 39758-2021",
        clause="空白区域（二手解读，待购标复核）",
        title="盲文周边安全空白",
        requirement="盲文周边留出不小于 6 mm 的空白区域",
        value=">=6.0",
        unit="mm",
        source_url="https://www.antpedia.com/standard/1685272379-10.html",
        confidence="secondary",
        applicability="tactile_plate",
        note="用于盲文行与图形/板边的净空检查；单张触觉板按保守值执行。",
    ),
    Rule(
        id="braille.dot_spacing",
        standard="历史工程参考（GB/T 15720-2008 已废止）",
        clause="盲符技术参数（历史参考）",
        title="同格点距",
        requirement="同格相邻点位圆心距 2.2–2.8 mm；本项目取 2.5 mm",
        value="2.2-2.8",
        unit="mm",
        source_url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=484E4150A1F62E81B0553422E6A8A292",
        confidence="legacy",
        applicability="tactile_plate",
        note="GB/T 15720-2008 已于 2025-11-03 废止，不能作为现行符合性依据；仅作为尺寸连续性参考。",
    ),
    Rule(
        id="plate.margin",
        standard="工程约束（非标准条文）",
        clause="—",
        title="板边安全边距",
        requirement="触觉元素距板边不小于 6 mm",
        value=">=6.0",
        unit="mm",
        source_url="",
        confidence="engineering",
        applicability="tactile_plate",
        note="为夹持、倒角和手指进入留出的工程余量，不冒充国家标准。",
    ),
    Rule(
        id="plate.max_height",
        standard="工程约束（非标准条文）",
        clause="—",
        title="单板最大高度",
        requirement="底板 3 mm + 浮雕/盲文不超过 4 mm",
        value="<=4.0",
        unit="mm",
        source_url="",
        confidence="engineering",
        applicability="tactile_plate",
    ),
    Rule(
        id="mesh.watertight",
        standard="制造门禁（非标准条文）",
        clause="—",
        title="网格水密",
        requirement="每条边恰好被两个三角形共享，体积为正",
        value="required",
        unit="",
        source_url="",
        confidence="engineering",
        applicability="both",
        note="这是切片前置条件，不是国家标准条款。",
    ),
    Rule(
        id="mesh.min_feature",
        standard="制造门禁（非标准条文）",
        clause="—",
        title="最小网格边（数值退化）",
        requirement="三角形边长不小于 0.2 mm； tactile 线宽/点径另由线宽与点径规则约束",
        value=">=0.2",
        unit="mm",
        source_url="",
        confidence="engineering",
        applicability="both",
    ),
    Rule(
        id="encoding.chinese_braille",
        standard="GF 0019-2018",
        clause="编码规则",
        title="中文盲文标注",
        requirement="中文标注应使用国家通用盲文方案编码",
        value="required",
        unit="",
        source_url="http://www.moe.gov.cn/jyb_sjzl/ziliao/A19/201807/W020180725666187054299.pdf",
        confidence="official-scope",
        applicability="tactile_plate",
        note="v1 触觉板仍使用 UEB 英文和阿拉伯数字；中文盲文编码模块尚未启用，报告标为待复核。",
    ),
    Rule(
        id="encoding.chinese_math_braille",
        standard="GB/T 18028-2010",
        clause="数学符号写法与使用规则（条文待购标复核）",
        title="中文盲文数学符号",
        requirement="数学公式、运算符号和函数记号应使用中国盲文数学符号",
        value="required",
        unit="",
        source_url="https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D7D8D5D3A7E05397BE0A0AB82A",
        confidence="official-scope",
        applicability="tactile_plate",
        note="该标准 2026 年复审结论为修订。v1 的函数板使用 UEB/ASCII 标注，中文盲文数学模块尚未接入，报告中与中文盲文一并列为待复核。",
    ),
    Rule(
        id="audio.script",
        standard="GB/T 45281-2025",
        clause="脚本与解说（条文待购标复核）",
        title="扫码音频脚本",
        requirement="脚本应说明画面内容、触摸顺序和关键信息，录制清晰、可暂停跟做",
        value="checklist",
        unit="",
        source_url="https://www.spc.org.cn/online/dfbb30b952cda59d1b822398b2c00b8f.html",
        confidence="official-scope",
        applicability="audio",
        note="本版只按标准适用范围建立脚本清单，未取得条文全文。",
    ),
)


def standards_registry() -> list[dict[str, Any]]:
    return [standard.to_dict() for standard in STANDARDS]


def rules_registry() -> list[dict[str, Any]]:
    return [rule.to_dict() for rule in RULES]


def _primitive_width(primitive: Primitive) -> float:
    if primitive.kind == "polyline":
        return primitive.width
    if primitive.kind == "circle":
        return primitive.radius * 2.0
    if primitive.kind == "braille":
        return braille.DOT_DIAMETER_MM
    if primitive.kind == "text":
        return max(1.0, primitive.height / 5.0)
    return float("inf")


def _point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        if (current[1] > y) != (previous[1] > y):
            intersection = (previous[0] - current[0]) * (y - current[1]) / (previous[1] - current[1]) + current[0]
            if x < intersection:
                inside = not inside
    return inside


def _overhang_fraction(mesh: Mesh) -> float:
    total = 0.0
    overhang = 0.0
    bounds = mesh.bounds()
    build_plate_z = bounds[2]
    for a, b, c in mesh.triangles:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue
        area = length / 2.0
        total += area
        centroid_z = (a[2] + b[2] + c[2]) / 3.0
        on_build_plate = centroid_z <= build_plate_z + 0.2
        if not on_build_plate and nz / length < -math.cos(math.radians(45.0)):
            overhang += area
    return overhang / total if total else 0.0


def _min_edge(mesh: Mesh) -> float:
    best = float("inf")
    for a, b, c in mesh.triangles:
        for first, second in ((a, b), (b, c), (c, a)):
            distance = math.dist(first, second)
            if 1e-9 < distance < best:
                best = distance
    return best


def check_plate(
    layout: LayoutResult,
    mesh: Mesh,
    *,
    profile: str = "durable-fdm",
    label_clearance_min: float = 2.0,
    qr_size: float = 30.0,
    audio_ready: bool = False,
) -> ComplianceReport:
    report = ComplianceReport(profile=profile)
    constraints = layout.diagram.constraints
    base_thickness = float(constraints.get("base_thickness_mm", 3.0))
    relief = float(constraints.get("relief_height_mm", 0.8))
    braille_height = float(constraints.get("braille_height_mm", 0.6))
    min_width = float(constraints.get("min_line_width_mm", 1.0))
    margin = float(layout.diagram.canvas.margin_mm)

    widths = []
    point_diameters = []
    for feature in layout.features:
        for primitive in feature.primitives:
            width = _primitive_width(primitive)
            if math.isfinite(width):
                widths.append((feature.id, width))
            if primitive.kind == "circle":
                point_diameters.append((feature.id, primitive.radius * 2.0))
    min_width_seen = min((value for _, value in widths), default=float("inf"))
    min_point = min((value for _, value in point_diameters), default=float("inf"))

    if min_width_seen >= min_width - 1e-6:
        report.checks.append(Check("tactile.line_width", STATUS_PASS, f"{min_width_seen:.2f} mm", "所有线宽/笔画宽度达到工程下限"))
    else:
        report.checks.append(Check("tactile.line_width", STATUS_FAIL, f"{min_width_seen:.2f} mm", f"最小线宽低于 {min_width:.2f} mm"))

    if min_point == float("inf"):
        report.checks.append(Check("tactile.point_diameter", STATUS_NA, "n/a", "本图没有点状符号，此项不适用"))
    elif min_point >= 2.0 - 1e-6:
        report.checks.append(Check("tactile.point_diameter", STATUS_PASS, f"{min_point:.2f} mm", "点状符号直径达到公开解读下限 2 mm"))
    else:
        report.checks.append(Check("tactile.point_diameter", STATUS_FAIL, f"{min_point:.2f} mm", "点状符号小于 2 mm"))

    if 0.10 <= relief <= 0.15:
        report.checks.append(Check("tactile.line_height", STATUS_PASS, f"{relief:.2f} mm", "浮雕高度落在公开解读范围"))
    else:
        report.checks.append(
            Check(
                "tactile.line_height",
                STATUS_DEVIATION,
                f"{relief:.2f} mm",
                "FDM 耐用剖面高于公开解读的 0.1–0.15 mm；这是工艺适配，需在交付说明中明示，不作为标准符合性结论。",
            )
        )

    if 1.0 - 1e-6 <= braille.DOT_DIAMETER_MM <= 1.7 + 1e-6:
        report.checks.append(Check("braille.dot_diameter", STATUS_PASS, f"{braille.DOT_DIAMETER_MM:.2f} mm", "点径落在 1.0–1.7 mm"))
    else:
        report.checks.append(Check("braille.dot_diameter", STATUS_FAIL, f"{braille.DOT_DIAMETER_MM:.2f} mm", "点径超出范围"))

    if 0.3 - 1e-6 <= braille_height <= 0.7 + 1e-6:
        report.checks.append(Check("braille.dot_height", STATUS_PASS, f"{braille_height:.2f} mm", "点高落在 0.3–0.7 mm"))
    else:
        report.checks.append(Check("braille.dot_height", STATUS_FAIL, f"{braille_height:.2f} mm", "点高超出范围"))

    if 2.2 - 1e-6 <= braille.DOT_SPACING_MM <= 2.8 + 1e-6:
        report.checks.append(
            Check(
                "braille.dot_spacing",
                STATUS_REVIEW,
                f"{braille.DOT_SPACING_MM:.2f} mm",
                "点距落在历史参考范围；GB/T 15720-2008 已废止，GB/T 39758 的对应表需购标核对。",
            )
        )
    else:
        report.checks.append(Check("braille.dot_spacing", STATUS_FAIL, f"{braille.DOT_SPACING_MM:.2f} mm", "点距超出历史参考范围"))

    labels = [feature for feature in layout.features if feature.kind == "label"]
    non_labels = [feature for feature in layout.features if feature.kind != "label" and feature.kind != "grid"]
    clearance_seen = float("inf")
    clearance_pair = ""
    for label in labels:
        for other in non_labels:
            if other.kind == "qr_zone":
                continue
            if label.semantic_id and label.semantic_id == other.semantic_id:
                continue
            distance = feature_distance(label, other, 20.0)
            if distance < clearance_seen:
                clearance_seen = distance
                clearance_pair = f"{label.id} / {other.id}"
    if clearance_seen == float("inf"):
        report.checks.append(
            Check(
                "tactile.annotation_distance",
                STATUS_NA,
                "n/a",
                "没有需要仲裁的标注-图形对，此项不适用",
            )
        )
    elif clearance_seen >= label_clearance_min - 1e-6:
        report.checks.append(Check("tactile.annotation_distance", STATUS_PASS, f"{clearance_seen:.2f} mm", "标注净空达到 2 mm 下限"))
    else:
        report.checks.append(
            Check(
                "tactile.annotation_distance",
                STATUS_FAIL,
                f"{clearance_seen:.2f} mm",
                f"标注 {clearance_pair} 净空低于 {label_clearance_min:.2f} mm",
            )
        )

    if margin >= 6.0 - 1e-6:
        report.checks.append(Check("plate.margin", STATUS_PASS, f"{margin:.2f} mm", "板边距达到 6 mm"))
    else:
        report.checks.append(Check("plate.margin", STATUS_FAIL, f"{margin:.2f} mm", "板边距小于 6 mm"))

    qr_feature = next((feature for feature in layout.features if feature.kind == "qr_zone"), None)
    if qr_feature is None:
        report.checks.append(Check("plate.qr_zone", STATUS_FAIL, "missing", "缺少二维码预留区"))
    else:
        left, top, width, height = qr_feature.bbox()
        if width >= qr_size - 1e-6 and height >= qr_size - 1e-6:
            report.checks.append(Check("plate.qr_zone", STATUS_PASS, f"{width:.1f}×{height:.1f} mm", "二维码预留区达到 30×30 mm"))
        else:
            report.checks.append(Check("plate.qr_zone", STATUS_FAIL, f"{width:.1f}×{height:.1f} mm", "二维码预留区不足 30×30 mm"))

    notch_present = not _point_in_polygon((2.5, 2.5), layout.base_polygon)
    report.checks.append(
        Check(
            "plate.orientation_notch",
            STATUS_PASS if notch_present else STATUS_FAIL,
            "present" if notch_present else "missing",
            "左上定向缺口" if notch_present else "缺少左上定向缺口",
        )
    )

    bounds = mesh.bounds()
    max_z = bounds[5]
    if max_z <= base_thickness + max(relief, braille_height) + 0.01:
        report.checks.append(Check("plate.max_height", STATUS_PASS, f"{max_z:.2f} mm", "最高点符合板厚 + 浮雕/盲文高度"))
    else:
        report.checks.append(Check("plate.max_height", STATUS_FAIL, f"{max_z:.2f} mm", "最高点超过预期"))

    if mesh.volume() > 0:
        report.checks.append(Check("mesh.volume", STATUS_PASS, f"{mesh.volume():.1f} mm³", "体积为正"))
    else:
        report.checks.append(Check("mesh.volume", STATUS_FAIL, f"{mesh.volume():.1f} mm³", "体积非正，法向可能反向"))

    if mesh.is_watertight():
        report.checks.append(Check("mesh.watertight", STATUS_PASS, "yes", "每条边恰好两个三角形"))
    else:
        report.checks.append(Check("mesh.watertight", STATUS_FAIL, "no", "存在开放边或非流形边"))

    min_edge = _min_edge(mesh)
    if min_edge >= 0.2 - 1e-6:
        report.checks.append(Check("mesh.min_feature", STATUS_PASS, f"{min_edge:.3f} mm", "最小网格边高于数值退化下限"))
    else:
        report.checks.append(Check("mesh.min_feature", STATUS_FAIL, f"{min_edge:.3f} mm", "存在近似退化的三角面，切片可能失败"))

    report.checks.append(
        Check(
            "encoding.chinese_braille",
            STATUS_REVIEW,
            "UEB + 阿拉伯数字",
            "当前板面使用 UEB 英文和阿拉伯数字；中文盲文标注需接入 GF 0019-2018 编码表、数学符号需接入 GB/T 18028-2010 后再宣称完全符合。",
        )
    )
    report.checks.append(
        Check(
            "audio.script",
            STATUS_PASS if audio_ready else STATUS_REVIEW,
            "ready" if audio_ready else "script-only",
            "讲解稿已生成" if audio_ready else "讲解稿已生成，音频尚未生成",
        )
    )

    report.metrics = {
        "canvas_mm": [layout.width, layout.height],
        "margin_mm": margin,
        "base_thickness_mm": base_thickness,
        "relief_mm": relief,
        "braille_height_mm": braille_height,
        "min_line_width_mm": round(min_width_seen, 3),
        "min_point_diameter_mm": round(min_point, 3),
        "min_label_clearance_mm": round(clearance_seen, 3),
        "mesh": mesh.stats(),
        "mesh_min_edge_mm": round(min_edge, 4),
    }
    return report


def check_volumetric(
    mesh: Mesh,
    *,
    profile: str = "durable-fdm",
    build_volume_mm: tuple[float, float, float] = (220.0, 220.0, 250.0),
    needs_support_threshold: float = 0.05,
    qr_area_mm: float | None = None,
) -> ComplianceReport:
    report = ComplianceReport(profile=profile)
    bounds = mesh.bounds()
    size = (bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
    overhang = _overhang_fraction(mesh)
    min_edge = _min_edge(mesh)

    if mesh.volume() > 0:
        report.checks.append(Check("mesh.volume", STATUS_PASS, f"{mesh.volume():.1f} mm³", "体积为正"))
    else:
        report.checks.append(Check("mesh.volume", STATUS_FAIL, f"{mesh.volume():.1f} mm³", "体积非正"))

    if mesh.is_watertight():
        report.checks.append(Check("mesh.watertight", STATUS_PASS, "yes", "每条边恰好两个三角形"))
    else:
        report.checks.append(Check("mesh.watertight", STATUS_FAIL, "no", "存在开放边或非流形边"))

    if min_edge >= 0.2 - 1e-6:
        report.checks.append(Check("mesh.min_feature", STATUS_PASS, f"{min_edge:.3f} mm", "最小网格边高于数值退化下限"))
    else:
        report.checks.append(Check("mesh.min_feature", STATUS_FAIL, f"{min_edge:.3f} mm", "存在近似退化的三角面，切片可能失败"))

    fits = all(size[index] <= build_volume_mm[index] + 1e-6 for index in range(3))
    report.checks.append(
        Check(
            "mesh.build_volume",
            STATUS_PASS if fits else STATUS_FAIL,
            f"{size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f} mm",
            "尺寸在 220×220×250 mm 打印空间内" if fits else "超出默认打印空间",
        )
    )

    if overhang <= needs_support_threshold:
        report.checks.append(Check("mesh.overhang", STATUS_PASS, f"{overhang * 100:.1f}%", "45° 以下悬垂面积低于 5%"))
    else:
        report.checks.append(
            Check(
                "mesh.overhang",
                STATUS_REVIEW,
                f"{overhang * 100:.1f}%",
                "悬垂面积超过 5%，切片时建议开启树状支撑；这是工艺提示，不是模型错误。",
            )
        )

    if qr_area_mm is None:
        report.checks.append(Check("model.qr_area", STATUS_REVIEW, "unknown", "未声明二维码平面；若需要扫码音频，应在底座预留平面。"))
    elif qr_area_mm >= 28.0 - 1e-6:
        report.checks.append(Check("model.qr_area", STATUS_PASS, f"{qr_area_mm:.1f} mm", "底座二维码平面达到 28 mm"))
    else:
        report.checks.append(Check("model.qr_area", STATUS_FAIL, f"{qr_area_mm:.1f} mm", "底座二维码平面不足 28 mm"))

    report.metrics = {
        "size_mm": [round(value, 3) for value in size],
        "volume_mm3": round(mesh.volume(), 3),
        "triangles": mesh.triangle_count(),
        "overhang_fraction": round(overhang, 4),
        "mesh_min_edge_mm": round(min_edge, 4),
        "build_volume_mm": list(build_volume_mm),
    }
    return report


def check_relief(
    mesh: Mesh,
    *,
    profile: str = "durable-fdm",
    plate_width_mm: float = 148.0,
    plate_height_mm: float = 210.0,
    margin_mm: float = 6.0,
    base_thickness_mm: float = 3.0,
    relief_height_mm: float = 2.4,
    qr_area_mm: float = 30.0,
    build_volume_mm: tuple[float, float, float] = (220.0, 220.0, 250.0),
    audio_ready: bool = False,
) -> ComplianceReport:
    """Checks for an imported mesh projected onto a full tactile plate.

    A silhouette relief is a heightfield plate, not a scaled display model and
    not a semantic tactile diagram.  The report therefore combines plate-level
    checks with the mesh integrity checks, and states the projection limits in
    the surrounding manifest instead of pretending the result is a redrawn
    textbook figure.
    """
    report = ComplianceReport(profile=profile)
    bounds = mesh.bounds()
    size = (bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
    overhang = _overhang_fraction(mesh)
    min_edge = _min_edge(mesh)

    if mesh.volume() > 0:
        report.checks.append(Check("mesh.volume", STATUS_PASS, f"{mesh.volume():.1f} mm³", "体积为正"))
    else:
        report.checks.append(Check("mesh.volume", STATUS_FAIL, f"{mesh.volume():.1f} mm³", "体积非正"))
    if mesh.is_watertight():
        report.checks.append(Check("mesh.watertight", STATUS_PASS, "yes", "每条边恰好两个三角形"))
    else:
        report.checks.append(Check("mesh.watertight", STATUS_FAIL, "no", "存在开放边或非流形边"))
    if min_edge >= 0.2 - 1e-6:
        report.checks.append(Check("mesh.min_feature", STATUS_PASS, f"{min_edge:.3f} mm", "最小网格边高于数值退化下限"))
    else:
        report.checks.append(Check("mesh.min_feature", STATUS_FAIL, f"{min_edge:.3f} mm", "存在近似退化的三角面，切片可能失败"))

    fits = all(size[index] <= build_volume_mm[index] + 1e-6 for index in range(3))
    report.checks.append(
        Check(
            "mesh.build_volume",
            STATUS_PASS if fits else STATUS_FAIL,
            f"{size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f} mm",
            "尺寸在 220×220×250 mm 打印空间内" if fits else "超出默认打印空间",
        )
    )
    if overhang <= 0.05:
        report.checks.append(Check("mesh.overhang", STATUS_PASS, f"{overhang * 100:.1f}%", "45° 以下悬垂面积低于 5%"))
    else:
        report.checks.append(
            Check("mesh.overhang", STATUS_REVIEW, f"{overhang * 100:.1f}%", "悬垂面积超过 5%，建议开启树状支撑")
        )

    expected = (plate_width_mm, plate_height_mm)
    size_ok = abs(size[0] - expected[0]) <= 0.5 and abs(size[1] - expected[1]) <= 0.5
    report.checks.append(
        Check(
            "plate.size",
            STATUS_PASS if size_ok else STATUS_FAIL,
            f"{size[0]:.1f}×{size[1]:.1f} mm",
            f"板面为 A5 {expected[0]:.0f}×{expected[1]:.0f} mm" if size_ok else "板面尺寸偏离 A5",
        )
    )
    report.checks.append(Check("plate.margin", STATUS_PASS if margin_mm >= 6.0 else STATUS_FAIL, f"{margin_mm:.1f} mm", "四周留出 6 mm 安全边"))
    report.checks.append(
        Check("plate.base_thickness", STATUS_PASS if base_thickness_mm >= 2.4 else STATUS_FAIL, f"{base_thickness_mm:.1f} mm", "底板厚度不低于 2.4 mm")
    )
    report.checks.append(
        Check(
            "tactile.line_height",
            STATUS_DEVIATION,
            f"{relief_height_mm:.1f} mm",
            "FDM 浮雕高于印刷触觉图的 0.1–0.15 mm 公开解读值；这是耐久性工艺适配，需在交付说明中明示。",
        )
    )
    if qr_area_mm >= 28.0 - 1e-6:
        report.checks.append(Check("plate.qr_zone", STATUS_PASS, f"{qr_area_mm:.1f} mm", "预留 30 mm 二维码平面"))
    else:
        report.checks.append(Check("plate.qr_zone", STATUS_FAIL, f"{qr_area_mm:.1f} mm", "二维码平面不足 28 mm"))
    report.checks.append(
        Check(
            "content.language",
            STATUS_REVIEW,
            "Latin capitals",
            "板面标题为拉丁字母；中文标题与中文盲文需接入 GF 0019-2018 编码表后再宣称完全符合。",
        )
    )
    report.checks.append(
        Check(
            "audio.script",
            STATUS_PASS if audio_ready else STATUS_REVIEW,
            "ready" if audio_ready else "script-only",
            "讲解稿已生成" if audio_ready else "讲解稿已生成，音频尚未生成",
        )
    )
    report.metrics = {
        "size_mm": [round(value, 3) for value in size],
        "volume_mm3": round(mesh.volume(), 3),
        "triangles": mesh.triangle_count(),
        "overhang_fraction": round(overhang, 4),
        "mesh_min_edge_mm": round(min_edge, 4),
    }
    return report
