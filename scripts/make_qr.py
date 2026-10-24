"""Generate the QR sticker for a case after its audio page exists.

This script is optional; it requires ``qrcode[pil]``:

    pip install -e ".[qr]"
    python scripts/make_qr.py --url https://example.org/touchlearn/robot --out dist/robot-arm-6axis/qr.png

The QR is intentionally not part of the STL: the plate reserves a flat 30 x 30
mm area, and the sticker is applied after printing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a QR sticker for a TouchLearn case.")
    parser.add_argument("--url", required=True, help="public URL of the audio page")
    parser.add_argument("--out", required=True, help="output PNG path; an SVG is written next to it")
    parser.add_argument("--box-size", type=int, default=10, help="pixels per QR module (PNG)")
    parser.add_argument("--border", type=int, default=2, help="quiet-zone modules")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print("error: --url must be an absolute http(s) URL", file=sys.stderr)
        return 2

    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        print("error: qrcode is not installed; run: pip install -e \".[qr]\"", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=args.box_size,
        border=args.border,
    )
    qr.add_data(args.url)
    qr.make(fit=True)

    png = qr.make_image(fill_color="black", back_color="white")
    png.save(out)

    svg_path = out.with_suffix(".svg")
    svg_qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=args.box_size,
        border=args.border,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    svg_qr.add_data(args.url)
    svg_qr.make(fit=True)
    svg_qr.make_image().save(svg_path)

    print(f"url: {args.url}")
    print(f"png: {out}")
    print(f"svg: {svg_path}")
    print("reminder: print the sticker on waterproof material and keep the 30 x 30 mm reserve flat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
