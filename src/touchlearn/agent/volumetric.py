"""Volumetric STL output for the TouchLearn agent.

The relief plate is a 2.5D product.  This module adds the second output mode
the user can choose for a 3D subject: a scaled, printable display model.

Two paths are supported:

* ``build_robot_volume`` turns the same :class:`RobotSpec` that drives the
  relief plate into a solid pedestal, base, links, hinge joints and gripper.
* ``load_mesh`` / ``normalize_mesh`` import an STL or OBJ supplied by the user,
  scale it to a target height, drop it onto the build plate and keep the
  transform in the manifest so the result is reproducible.

Everything is pure Python and emits closed shells.  Overlapping shells are
intentional: slicers union them, while each shell stays manifold and the STL
can still be checked edge by edge.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import braille, font
from ..geometry import (
    Mesh,
    Point3,
    Triangle,
    circle_polygon,
    ensure_ccw,
    primitive_polygons,
    segment_polygon,
    triangulate,
)
from ..layout import Point, Primitive, rect_polygon
from .parametric import DEFAULT_ROBOT, RobotSpec

Vec3 = tuple[float, float, float]


# ---------------------------------------------------------------------------
# Small vector and mesh helpers
# ---------------------------------------------------------------------------


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: Vec3, value: float) -> Vec3:
    return (a[0] * value, a[1] * value, a[2] * value)


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _unit(a: Vec3) -> Vec3:
    length = math.sqrt(_dot(a, a))
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return _mul(a, 1.0 / length)


def _tri_centroid(triangle: Triangle) -> Vec3:
    a, b, c = triangle
    return ((a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0, (a[2] + b[2] + c[2]) / 3.0)


def _add_triangle(mesh: Mesh, a: Vec3, b: Vec3, c: Vec3, hint: Vec3) -> None:
    """Append a triangle, flipping the winding so its normal points to ``hint``.

    ``hint`` is a vector from the primitive centre to the triangle centroid.
    It is exact for the convex primitives used here (boxes, cylinders, spheres)
    and keeps the volume positive without a global post-pass.
    """
    normal = _cross(_sub(b, a), _sub(c, a))
    if _dot(normal, normal) <= 1e-12:
        return
    centroid = _tri_centroid((a, b, c))
    if _dot(normal, hint) < 0:
        b, c = c, b
    mesh.triangles.append((a, b, c))


def _add_quad(mesh: Mesh, a: Vec3, b: Vec3, c: Vec3, d: Vec3, hint: Vec3) -> None:
    _add_triangle(mesh, a, b, c, hint)
    _add_triangle(mesh, a, c, d, hint)


def _shell_offset(mesh: Mesh) -> float:
    """A sub-micron z offset unique to the current shell.

    Text strokes are decomposed into one shell per segment and joint.  Two
    collinear segments share an end edge exactly, which would make that edge
    belong to four triangles.  The offset keeps every shell manifold on its own;
    slicers union the 0.1 µm overlap without a visible seam.
    """
    return (len(mesh.triangles) % 997) * 1e-7


def _shift_z(point: Vec3, dz: float) -> Vec3:
    return (point[0], point[1], point[2] + dz)


def _prism_xy(mesh: Mesh, polygon: Sequence[Point], z0: float, z1: float) -> None:
    """Extrude an XY polygon upward; the polygon is normalised to CCW."""
    points = ensure_ccw([(float(x), float(y)) for x, y in polygon])
    if len(points) < 3:
        return
    dz = _shell_offset(mesh)
    z0 += dz
    z1 += dz
    centre_x = sum(point[0] for point in points) / len(points)
    centre_y = sum(point[1] for point in points) / len(points)
    for a, b, c in triangulate(points):
        mesh.triangles.append(((a[0], a[1], z1), (b[0], b[1], z1), (c[0], c[1], z1)))
        mesh.triangles.append(((a[0], a[1], z0), (c[0], c[1], z0), (b[0], b[1], z0)))
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        _add_quad(
            mesh,
            (point[0], point[1], z0),
            (nxt[0], nxt[1], z0),
            (nxt[0], nxt[1], z1),
            (point[0], point[1], z1),
            ((point[0] + nxt[0]) / 2.0 - centre_x, (point[1] + nxt[1]) / 2.0 - centre_y, 0.0),
        )


def _box_xy(mesh: Mesh, x: float, y: float, width: float, depth: float, z0: float, z1: float, *, chamfer: float = 0.0) -> None:
    _prism_xy(mesh, rect_polygon(x, y, width, depth, chamfer), z0, z1)


def _oriented_box(mesh: Mesh, p0: Vec3, p1: Vec3, half_width: float, half_depth: float) -> None:
    """A rectangular link between two 3D points.

    The wide dimension lies in the XZ plane (the arm's bending plane); the
    narrow dimension is the depth along Y, which is how a real link is
    machined or printed.
    """
    axis = _sub(p1, p0)
    horizontal = (axis[0], 0.0, axis[2])
    direction = _unit(horizontal)
    if direction == (0.0, 0.0, 0.0):
        direction = (0.0, 0.0, 1.0)
    side = (direction[2], 0.0, -direction[0])
    up = (0.0, 1.0, 0.0)
    dz = _shell_offset(mesh)
    p0 = _shift_z(p0, dz)
    p1 = _shift_z(p1, dz)
    corners_a = [
        _add(p0, _add(_mul(side, half_width), _mul(up, half_depth))),
        _add(p0, _add(_mul(side, -half_width), _mul(up, half_depth))),
        _add(p0, _add(_mul(side, -half_width), _mul(up, -half_depth))),
        _add(p0, _add(_mul(side, half_width), _mul(up, -half_depth))),
    ]
    corners_b = [
        _add(p1, _add(_mul(side, half_width), _mul(up, half_depth))),
        _add(p1, _add(_mul(side, -half_width), _mul(up, half_depth))),
        _add(p1, _add(_mul(side, -half_width), _mul(up, -half_depth))),
        _add(p1, _add(_mul(side, half_width), _mul(up, -half_depth))),
    ]
    centre = _mul(_add(p0, p1), 0.5)
    for index in range(4):
        nxt = (index + 1) % 4
        _add_quad(mesh, corners_a[index], corners_a[nxt], corners_b[nxt], corners_b[index], centre)
    _add_quad(mesh, corners_a[0], corners_a[1], corners_a[2], corners_a[3], _sub(p0, centre))
    _add_quad(mesh, corners_b[0], corners_b[1], corners_b[2], corners_b[3], _sub(p1, centre))


def _cylinder(mesh: Mesh, p0: Vec3, p1: Vec3, radius: float, segments: int = 28) -> None:
    dz = _shell_offset(mesh)
    p0 = _shift_z(p0, dz)
    p1 = _shift_z(p1, dz)
    axis = _sub(p1, p0)
    length = math.sqrt(_dot(axis, axis))
    if length <= 1e-9 or radius <= 0:
        return
    w = _mul(axis, 1.0 / length)
    helper = (0.0, 0.0, 1.0) if abs(w[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _unit(_cross(w, helper))
    v = _unit(_cross(w, u))
    ring0: list[Vec3] = []
    ring1: list[Vec3] = []
    for index in range(segments):
        angle = 2.0 * math.pi * index / segments
        offset = _add(_mul(u, radius * math.cos(angle)), _mul(v, radius * math.sin(angle)))
        ring0.append(_add(p0, offset))
        ring1.append(_add(p1, offset))
    centre = _mul(_add(p0, p1), 0.5)
    for index in range(segments):
        nxt = (index + 1) % segments
        _add_quad(mesh, ring0[index], ring0[nxt], ring1[nxt], ring1[index], centre)
        _add_triangle(mesh, p0, ring0[nxt], ring0[index], _sub(p0, centre))
        _add_triangle(mesh, p1, ring1[index], ring1[nxt], _sub(p1, centre))


def _sphere(mesh: Mesh, centre: Vec3, radius: float, slices: int = 24, stacks: int = 14) -> None:
    if radius <= 0:
        return
    centre = _shift_z(centre, _shell_offset(mesh))
    rings: list[list[Vec3]] = []
    for stack in range(1, stacks):
        phi = math.pi * stack / stacks
        z = radius * math.cos(phi)
        ring_radius = radius * math.sin(phi)
        ring = []
        for slice_index in range(slices):
            theta = 2.0 * math.pi * slice_index / slices
            ring.append(
                (
                    centre[0] + ring_radius * math.cos(theta),
                    centre[1] + ring_radius * math.sin(theta),
                    centre[2] + z,
                )
            )
        rings.append(ring)
    top = (centre[0], centre[1], centre[2] + radius)
    bottom = (centre[0], centre[1], centre[2] - radius)
    for slice_index in range(slices):
        nxt = (slice_index + 1) % slices
        _add_triangle(mesh, top, rings[0][slice_index], rings[0][nxt], _sub(top, centre))
        _add_triangle(mesh, bottom, rings[-1][nxt], rings[-1][slice_index], _sub(bottom, centre))
    for stack in range(len(rings) - 1):
        for slice_index in range(slices):
            nxt = (slice_index + 1) % slices
            a = rings[stack][slice_index]
            b = rings[stack][nxt]
            c = rings[stack + 1][nxt]
            d = rings[stack + 1][slice_index]
            _add_quad(mesh, a, b, c, d, _sub(a, centre))


# ---------------------------------------------------------------------------
# Volumetric robot
# ---------------------------------------------------------------------------


@dataclass
class VolumeResult:
    mesh: Mesh
    meta: dict[str, Any]


def _forward_kinematics(spec: RobotSpec, scale: float) -> list[Vec3]:
    """Joint centres in the XZ plane, with Z up."""
    joints: list[Vec3] = [(0.0, 0.0, spec.base_height * scale)]
    theta = 0.0
    x = 0.0
    z = spec.base_height * scale
    for angle, length in zip(spec.joint_angles_deg, spec.link_lengths):
        theta += math.radians(angle)
        x += length * scale * math.sin(theta)
        z += length * scale * math.cos(theta)
        joints.append((x, 0.0, z))
    return joints


def _gripper(mesh: Mesh, tip: Vec3, direction: Vec3, scale: float) -> None:
    direction = _unit(direction)
    if direction == (0.0, 0.0, 0.0):
        direction = (0.0, 0.0, 1.0)
    side = (direction[2], 0.0, -direction[0])
    palm_end = _add(tip, _mul(direction, 5.0 * scale))
    _oriented_box(mesh, tip, palm_end, 6.0 * scale, 5.0 * scale)
    for sign in (1.0, -1.0):
        root = _add(palm_end, _mul(side, sign * 3.8 * scale))
        end = _add(root, _add(_mul(direction, 8.0 * scale), _mul(side, sign * 1.2 * scale)))
        _oriented_box(mesh, root, end, 1.5 * scale, 3.2 * scale)


def _suction(mesh: Mesh, tip: Vec3, direction: Vec3, scale: float) -> None:
    direction = _unit(direction)
    if direction == (0.0, 0.0, 0.0):
        direction = (0.0, 0.0, 1.0)
    stem_end = _add(tip, _mul(direction, 4.0 * scale))
    _cylinder(mesh, tip, stem_end, 2.2 * scale, segments=20)
    cup_end = _add(stem_end, _mul(direction, 2.0 * scale))
    _cylinder(mesh, stem_end, cup_end, 5.0 * scale, segments=28)


def _robot_once(spec: RobotSpec, scale: float) -> tuple[Mesh, dict[str, Any]]:
    mesh = Mesh([])
    pedestal_top = 4.0
    pedestal_w = max(88.0, spec.base_width * scale + 58.0)
    pedestal_d = 90.0
    arm_x = -pedestal_w / 2.0 + 29.0
    pedestal_depth = pedestal_d

    _prism_xy(mesh, rect_polygon(-pedestal_w / 2.0, -pedestal_depth / 2.0, pedestal_w, pedestal_depth, 5.0), 0.0, pedestal_top)

    # Flat 34 x 34 mm pad for a printed QR label, clear of the arm and the
    # nameplate.  Its top is 2 mm above the pedestal.
    qr_centre = (pedestal_w / 2.0 - 27.0, -(pedestal_depth / 2.0 - 24.0))
    _box_xy(
        mesh,
        qr_centre[0] - 17.0,
        qr_centre[1] - 17.0,
        34.0,
        34.0,
        pedestal_top - 0.1,
        pedestal_top + 2.0,
        chamfer=2.0,
    )

    # Tactile and print nameplate on the front-left of the pedestal.
    text_primitive = Primitive("text", ((-pedestal_w / 2.0 + 7.0, 22.0),), text="ROBOT ARM", height=3.4)
    for polygon in primitive_polygons(text_primitive):
        _prism_xy(mesh, [(x, -y) for x, y in polygon], pedestal_top - 0.1, pedestal_top + 0.9)
    braille_primitive = Primitive("braille", ((-pedestal_w / 2.0 + 7.0, 31.0),), text="robot")
    for polygon in primitive_polygons(braille_primitive):
        _prism_xy(mesh, [(x, -y) for x, y in polygon], pedestal_top - 0.1, pedestal_top + 0.7)

    base_radius = max(6.0, spec.base_width * scale / 2.0)
    base_height = spec.base_height * scale
    _cylinder(mesh, (arm_x, 0.0, pedestal_top), (arm_x, 0.0, pedestal_top + base_height), base_radius)
    collar_top = pedestal_top + base_height + 2.0
    _cylinder(mesh, (arm_x, 0.0, pedestal_top + base_height - 0.1), (arm_x, 0.0, collar_top), base_radius * 0.66, segments=24)

    joints = _forward_kinematics(spec, scale)
    shift = (arm_x, 0.0, pedestal_top)
    joints = [(_add(point, shift)) for point in joints]

    for index in range(len(spec.joint_angles_deg)):
        start = joints[index]
        end = joints[index + 1]
        _oriented_box(
            mesh,
            start,
            end,
            max(2.2, spec.link_widths[index] * scale / 2.0),
            max(3.0, spec.link_widths[index] * scale * 0.55),
        )
    for index in range(spec.dof):
        point = joints[index]
        radius = max(2.4, spec.joint_radii[min(index, len(spec.joint_radii) - 1)] * scale)
        depth = max(9.0, spec.link_widths[min(index, len(spec.link_widths) - 1)] * scale * 1.15)
        _cylinder(mesh, (point[0], -depth / 2.0, point[2]), (point[0], depth / 2.0, point[2]), radius, segments=26)

    tip = joints[-1]
    previous = joints[-2]
    direction = _unit(_sub(tip, previous))
    _cylinder(
        mesh,
        (tip[0], -4.0 * scale, tip[2]),
        (tip[0], 4.0 * scale, tip[2]),
        max(2.2, spec.joint_radii[-1] * scale * 0.8),
        segments=22,
    )
    if spec.end_effector == "suction":
        _suction(mesh, tip, direction, scale)
    elif spec.end_effector != "none":
        _gripper(mesh, tip, direction, scale)

    bounds = mesh.bounds()
    meta = {
        "scale": scale,
        "pedestal_mm": [round(pedestal_w, 2), round(pedestal_d, 2), pedestal_top],
        "qr_pad_mm": 34.0,
        "qr_pad_centre_mm": [round(qr_centre[0], 2), round(qr_centre[1], 2), pedestal_top + 2.0],
        "joint_positions_mm": [[round(value, 3) for value in point] for point in joints],
        "dof": spec.dof,
        "end_effector": spec.end_effector,
        "size_mm": [round(bounds[3] - bounds[0], 2), round(bounds[4] - bounds[1], 2), round(bounds[5] - bounds[2], 2)],
        "nameplate": {"print": "ROBOT ARM", "braille": "robot"},
    }
    return mesh, meta


def build_robot_volume(
    spec: RobotSpec | None = None,
    *,
    target_height_mm: float = 135.0,
    build_volume_mm: tuple[float, float, float] = (220.0, 220.0, 250.0),
) -> VolumeResult:
    spec = spec or DEFAULT_ROBOT
    if spec.dof == 0:
        raise ValueError("robot spec needs at least one joint")
    probe, _ = _robot_once(spec, 1.0)
    probe_height = probe.bounds()[5] - probe.bounds()[2]
    scale = min(1.6, target_height_mm / max(probe_height, 1.0))
    mesh, meta = _robot_once(spec, scale)
    bounds = mesh.bounds()
    size = (bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
    room = min(
        build_volume_mm[0] * 0.95 / max(size[0], 1e-6),
        build_volume_mm[1] * 0.95 / max(size[1], 1e-6),
        build_volume_mm[2] * 0.95 / max(size[2], 1e-6),
    )
    if room < 1.0:
        scale *= room
        mesh, meta = _robot_once(spec, scale)
        meta["rescaled_for_build_volume"] = True
    meta.update(
        {
            "mode": "volumetric_model",
            "subject": "robot_arm",
            "spec": spec.to_dict(),
            "size_mm": [round(value, 2) for value in (mesh.bounds()[3] - mesh.bounds()[0], mesh.bounds()[4] - mesh.bounds()[1], mesh.bounds()[5] - mesh.bounds()[2])],
            "build_volume_mm": list(build_volume_mm),
            "shells": "closed overlapping shells; slicer unions them",
        }
    )
    return VolumeResult(mesh, meta)


# ---------------------------------------------------------------------------
# Imported STL / OBJ
# ---------------------------------------------------------------------------


def _finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def read_stl_mesh(path: str | Path) -> Mesh:
    target = Path(path)
    data = target.read_bytes()
    if len(data) >= 84:
        count = struct.unpack("<I", data[80:84])[0]
        if 84 + count * 50 == len(data):
            triangles: list[Triangle] = []
            offset = 84
            for _ in range(count):
                values = struct.unpack("<12fH", data[offset : offset + 50])
                coords = values[3:12]
                if _finite(coords):
                    triangles.append(
                        (
                            (coords[0], coords[1], coords[2]),
                            (coords[3], coords[4], coords[5]),
                            (coords[6], coords[7], coords[8]),
                        )
                    )
                offset += 50
            if not triangles:
                raise ValueError("STL contains no finite triangles")
            return Mesh(triangles)
    return _read_ascii_stl(target)


def _read_ascii_stl(path: Path) -> Mesh:
    triangles: list[Triangle] = []
    vertices: list[Point3] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                point = (float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                continue
            if _finite(point):
                vertices.append(point)
                if len(vertices) == 3:
                    triangles.append((vertices[0], vertices[1], vertices[2]))
                    vertices = []
    if not triangles:
        raise ValueError("not a binary or ASCII STL, or the file has no triangles")
    return Mesh(triangles)


def read_obj_mesh(path: str | Path) -> Mesh:
    target = Path(path)
    vertices: list[Point3] = []
    triangles: list[Triangle] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            try:
                point = (float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                continue
            if _finite(point):
                vertices.append(point)
        elif parts[0] == "f" and len(parts) >= 4:
            indices: list[int] = []
            for token in parts[1:]:
                raw = token.split("/")[0]
                try:
                    index = int(raw)
                except ValueError:
                    indices = []
                    break
                if index < 0:
                    index = len(vertices) + index
                else:
                    index -= 1
                indices.append(index)
            if len(indices) < 3:
                continue
            for offset in range(1, len(indices) - 1):
                a, b, c = indices[0], indices[offset], indices[offset + 1]
                if 0 <= a < len(vertices) and 0 <= b < len(vertices) and 0 <= c < len(vertices):
                    triangles.append((vertices[a], vertices[b], vertices[c]))
    if not triangles:
        raise ValueError("OBJ has no triangulatable faces")
    return Mesh(triangles)


def load_mesh(path: str | Path) -> Mesh:
    suffix = Path(path).suffix.lower()
    if suffix == ".stl":
        return read_stl_mesh(path)
    if suffix == ".obj":
        return read_obj_mesh(path)
    raise ValueError(f"unsupported mesh format: {suffix or 'unknown'} (use STL or OBJ)")


def _transform(mesh: Mesh, scale: float, dx: float, dy: float, dz: float) -> Mesh:
    return Mesh(
        [
            (
                (a[0] * scale + dx, a[1] * scale + dy, a[2] * scale + dz),
                (b[0] * scale + dx, b[1] * scale + dy, b[2] * scale + dz),
                (c[0] * scale + dx, c[1] * scale + dy, c[2] * scale + dz),
            )
            for a, b, c in mesh.triangles
        ]
    )


def normalize_mesh(
    mesh: Mesh,
    *,
    target_height_mm: float = 120.0,
    build_volume_mm: tuple[float, float, float] = (220.0, 220.0, 250.0),
) -> VolumeResult:
    if not mesh.triangles:
        raise ValueError("mesh is empty")
    bounds = mesh.bounds()
    size = (bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
    longest = max(size)
    if longest <= 1e-9:
        raise ValueError("mesh has zero extent")
    scale = target_height_mm / max(size[2], 1e-6) if size[2] > 1e-6 else target_height_mm / longest
    room = min(
        build_volume_mm[0] * 0.95 / max(size[0] * scale, 1e-6),
        build_volume_mm[1] * 0.95 / max(size[1] * scale, 1e-6),
        build_volume_mm[2] * 0.95 / max(size[2] * scale, 1e-6),
    )
    if room < 1.0:
        scale *= room
    dx = -((bounds[0] + bounds[3]) / 2.0) * scale
    dy = -((bounds[1] + bounds[4]) / 2.0) * scale
    dz = -bounds[2] * scale
    result = _transform(mesh, scale, dx, dy, dz)
    final = result.bounds()
    meta = {
        "mode": "volumetric_model",
        "subject": "imported_mesh",
        "source_bounds_mm": [round(value, 3) for value in bounds],
        "source_size_mm": [round(value, 3) for value in size],
        "scale": scale,
        "translation_mm": [round(dx, 3), round(dy, 3), round(dz, 3)],
        "size_mm": [round(final[3] - final[0], 3), round(final[4] - final[1], 3), round(final[5] - final[2], 3)],
        "build_volume_mm": list(build_volume_mm),
    }
    return VolumeResult(result, meta)


def mesh_health(mesh: Mesh) -> dict[str, Any]:
    area = 0.0
    downward = 0.0
    for a, b, c in mesh.triangles:
        normal = _cross(_sub(b, a), _sub(c, a))
        magnitude = math.sqrt(_dot(normal, normal))
        if magnitude <= 1e-12:
            continue
        area += magnitude / 2.0
        if normal[2] / magnitude < -0.2:
            downward += magnitude / 2.0
    return {
        "triangles": mesh.triangle_count(),
        "bounds_mm": [round(value, 3) for value in mesh.bounds()],
        "watertight": mesh.is_watertight(),
        "volume_mm3": round(mesh.volume(), 3),
        "surface_area_mm2": round(area, 3),
        "downward_area_fraction": round(downward / area, 4) if area > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Silhouette relief from an imported mesh
# ---------------------------------------------------------------------------


def _point_in_triangle(px: float, py: float, a: Point, b: Point, c: Point) -> bool:
    def cross(u: Point, v: Point, w: Point) -> float:
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    c1 = cross(a, b, (px, py))
    c2 = cross(b, c, (px, py))
    c3 = cross(c, a, (px, py))
    has_negative = c1 < -1e-9 or c2 < -1e-9 or c3 < -1e-9
    has_positive = c1 > 1e-9 or c2 > 1e-9 or c3 > 1e-9
    return not (has_negative and has_positive)


def mesh_relief(
    mesh: Mesh,
    *,
    plate_width_mm: float = 148.0,
    plate_height_mm: float = 210.0,
    margin_mm: float = 6.0,
    base_thickness_mm: float = 3.0,
    relief_height_mm: float = 2.4,
    resolution_mm: float = 1.0,
    max_triangles: int = 200_000,
) -> VolumeResult:
    """Turn an imported mesh into a projectable silhouette relief plate.

    The two largest source axes are projected onto the plate; the third axis
    is collapsed.  The result is a watertight heightfield, not a semantic
    tactile diagram: it preserves outline and major protrusions but cannot
    recover labels or internal structure.  The agent therefore offers this as
    a fallback and records the projected axes in the manifest.
    """
    if not mesh.triangles:
        raise ValueError("mesh is empty")
    if len(mesh.triangles) > max_triangles:
        mesh = Mesh(mesh.triangles[:max_triangles])
    bounds = mesh.bounds()
    extents = [bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]]
    axes = sorted(range(3), key=lambda index: extents[index], reverse=True)[:2]
    u_index, v_index = axes[0], axes[1]
    u_min, u_max = bounds[u_index], bounds[u_index + 3]
    v_min, v_max = bounds[v_index], bounds[v_index + 3]
    if u_max - u_min <= 1e-9 or v_max - v_min <= 1e-9:
        raise ValueError("mesh projection has zero extent")

    content_left = margin_mm + 4.0
    content_right = plate_width_mm - margin_mm - 38.0
    content_top = margin_mm + 26.0
    content_bottom = plate_height_mm - margin_mm - 8.0
    content_width = max(20.0, content_right - content_left)
    content_height = max(20.0, content_bottom - content_top)
    scale = min(content_width / (u_max - u_min), content_height / (v_max - v_min))
    resolution = max(0.6, min(resolution_mm, 2.0))
    columns = max(8, int(round(plate_width_mm / resolution)))
    rows = max(8, int(round(plate_height_mm / resolution)))
    cell_w = plate_width_mm / columns
    cell_h = plate_height_mm / rows
    used_width = (u_max - u_min) * scale
    used_height = (v_max - v_min) * scale
    origin_x = content_left + (content_width - used_width) / 2.0
    origin_y = content_top + (content_height - used_height) / 2.0

    occupied = [[False] * columns for _ in range(rows)]
    for a, b, c in mesh.triangles:
        pa = (origin_x + (a[u_index] - u_min) * scale, origin_y + (a[v_index] - v_min) * scale)
        pb = (origin_x + (b[u_index] - u_min) * scale, origin_y + (b[v_index] - v_min) * scale)
        pc = (origin_x + (c[u_index] - u_min) * scale, origin_y + (c[v_index] - v_min) * scale)
        tri_min_x, tri_max_x = min(pa[0], pb[0], pc[0]), max(pa[0], pb[0], pc[0])
        tri_min_y, tri_max_y = min(pa[1], pb[1], pc[1]), max(pa[1], pb[1], pc[1])
        i0 = max(0, int(tri_min_x / cell_w))
        i1 = min(columns - 1, int(tri_max_x / cell_w) + 1)
        j0 = max(0, int(tri_min_y / cell_h))
        j1 = min(rows - 1, int(tri_max_y / cell_h) + 1)
        for j in range(j0, j1 + 1):
            cy = (j + 0.5) * cell_h
            for i in range(i0, i1 + 1):
                if occupied[j][i]:
                    continue
                cx = (i + 0.5) * cell_w
                if _point_in_triangle(cx, cy, pa, pb, pc):
                    occupied[j][i] = True

    if not any(any(row) for row in occupied):
        raise ValueError("projected silhouette is empty")

    # A raised cell lifts its four corner nodes.  Shared nodes make the
    # heightfield continuous and keep the top surface watertight.
    node_height = [[base_thickness_mm] * (columns + 1) for _ in range(rows + 1)]
    for j in range(rows):
        for i in range(columns):
            if occupied[j][i]:
                top = base_thickness_mm + relief_height_mm
                node_height[j][i] = top
                node_height[j][i + 1] = top
                node_height[j + 1][i] = top
                node_height[j + 1][i + 1] = top

    mesh_out = Mesh([])
    x_of = lambda i: i * cell_w
    y_of = lambda j: j * cell_h
    z_of = lambda i, j: node_height[j][i]
    for j in range(rows):
        for i in range(columns):
            p00 = (x_of(i), y_of(j), z_of(i, j))
            p10 = (x_of(i + 1), y_of(j), z_of(i + 1, j))
            p11 = (x_of(i + 1), y_of(j + 1), z_of(i + 1, j + 1))
            p01 = (x_of(i), y_of(j + 1), z_of(i, j + 1))
            mesh_out.triangles.append((p00, p10, p11))
            mesh_out.triangles.append((p00, p11, p01))
    # Outer walls follow the actual node heights, so a silhouette that reaches
    # the plate edge cannot leave an open edge against a constant-height wall.
    for i in range(columns):
        x0, x1 = x_of(i), x_of(i + 1)
        _add_quad(mesh_out, (x0, 0.0, 0.0), (x1, 0.0, 0.0), (x1, 0.0, z_of(i + 1, 0)), (x0, 0.0, z_of(i, 0)), (0.0, -1.0, 0.0))
        _add_quad(mesh_out, (x1, plate_height_mm, 0.0), (x0, plate_height_mm, 0.0), (x0, plate_height_mm, z_of(i, rows)), (x1, plate_height_mm, z_of(i + 1, rows)), (0.0, 1.0, 0.0))
    for j in range(rows):
        y0, y1 = y_of(j), y_of(j + 1)
        _add_quad(mesh_out, (0.0, y0, 0.0), (0.0, y1, 0.0), (0.0, y1, z_of(0, j + 1)), (0.0, y0, z_of(0, j)), (-1.0, 0.0, 0.0))
        _add_quad(mesh_out, (plate_width_mm, y1, 0.0), (plate_width_mm, y0, 0.0), (plate_width_mm, y0, z_of(columns, j)), (plate_width_mm, y1, z_of(columns, j + 1)), (1.0, 0.0, 0.0))

    # Bottom face as a perimeter fan: its boundary uses the same vertices as
    # the side walls, so there are no T-junctions, and it stays O(perimeter)
    # instead of adding two triangles for every grid cell.
    perimeter: list[Vec3] = []
    for i in range(columns + 1):
        perimeter.append((x_of(i), 0.0, 0.0))
    for j in range(1, rows + 1):
        perimeter.append((plate_width_mm, y_of(j), 0.0))
    for i in range(columns - 1, -1, -1):
        perimeter.append((x_of(i), plate_height_mm, 0.0))
    for j in range(rows - 1, 0, -1):
        perimeter.append((0.0, y_of(j), 0.0))
    centre = (plate_width_mm / 2.0, plate_height_mm / 2.0, 0.0)
    for index, point in enumerate(perimeter):
        _add_triangle(mesh_out, centre, perimeter[(index + 1) % len(perimeter)], point, (0.0, 0.0, -1.0))

    # Title and a flat 30 mm QR reserve.  They are separate raised shells on
    # the flat margin, so they do not disturb the heightfield topology.
    title = Primitive("text", ((margin_mm + 2.0, margin_mm + 4.0),), text="3D RELIEF", height=5.0)
    for polygon in primitive_polygons(title):
        _prism_xy(mesh_out, polygon, base_thickness_mm - 0.1, base_thickness_mm + 0.8)
    qr_x = plate_width_mm - margin_mm - 31.0
    qr_y = margin_mm + 2.0
    frame = 1.0
    for x, y, width, depth in (
        (qr_x - frame, qr_y - frame, 32.0, frame),
        (qr_x - frame, qr_y + 31.0, 32.0, frame),
        (qr_x - frame, qr_y, frame, 31.0),
        (qr_x + 31.0, qr_y, frame, 31.0),
    ):
        _box_xy(mesh_out, x, y, width, depth, base_thickness_mm - 0.1, base_thickness_mm + 0.6)

    meta = {
        "mode": "silhouette_relief",
        "subject": "imported_mesh",
        "projected_axes": ["xyz"[u_index], "xyz"[v_index]],
        "grid": [columns, rows],
        "resolution_mm": round(resolution, 3),
        "margin_mm": margin_mm,
        "relief_height_mm": relief_height_mm,
        "base_thickness_mm": base_thickness_mm,
        "occupied_cells": sum(1 for row in occupied for value in row if value),
        "plate_mm": [plate_width_mm, plate_height_mm],
        "qr_area_mm": 30.0,
        "title": "3D RELIEF",
        "size_mm": [round(value, 3) for value in (mesh_out.bounds()[3] - mesh_out.bounds()[0], mesh_out.bounds()[4] - mesh_out.bounds()[1], mesh_out.bounds()[5] - mesh_out.bounds()[2])],
        "note": "Outline-only projection. Labels, hidden features and internal structure are not recovered.",
    }
    return VolumeResult(mesh_out, meta)
