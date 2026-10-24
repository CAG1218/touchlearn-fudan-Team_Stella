"""Binary STL output and lightweight inspection helpers."""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path

from .geometry import Mesh, Point3, Triangle


def _normal(a: Point3, b: Point3, c: Point3) -> Point3:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def write_binary_stl(mesh: Mesh, path: str | Path, header: str = "TouchLearn v0.2.0 - Team Stella") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    header_bytes = header.encode("ascii", errors="replace")[:80].ljust(80, b" ")
    with target.open("wb") as handle:
        handle.write(header_bytes)
        handle.write(struct.pack("<I", len(mesh.triangles)))
        for triangle in mesh.triangles:
            a, b, c = triangle
            nx, ny, nz = _normal(a, b, c)
            handle.write(
                struct.pack(
                    "<12fH",
                    nx,
                    ny,
                    nz,
                    a[0],
                    a[1],
                    a[2],
                    b[0],
                    b[1],
                    b[2],
                    c[0],
                    c[1],
                    c[2],
                    0,
                )
            )
    return target


def read_stl_info(path: str | Path) -> dict[str, object]:
    target = Path(path)
    with target.open("rb") as handle:
        header = handle.read(80)
        raw_count = handle.read(4)
        if len(raw_count) != 4:
            raise ValueError("not a binary STL: missing triangle count")
        triangle_count = struct.unpack("<I", raw_count)[0]
        expected = 84 + triangle_count * 50
        actual = target.stat().st_size
        if expected != actual:
            raise ValueError(f"not a binary STL: expected {expected} bytes, found {actual}")
    return {
        "path": str(target),
        "header": header.decode("ascii", errors="replace").rstrip(),
        "triangles": triangle_count,
        "bytes": actual,
    }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
