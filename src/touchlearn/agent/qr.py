"""QR assets for the audio track.

The QR code is never embossed into the STL: a tactile QR without contrast is
not reliably scannable.  The bundle therefore contains a flat QR PNG plus an
A6 printable card; the model's pedestal (or the plate's top-right reserve)
gives it a flat place to sit.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def make_qr_png(payload: str, path: str | Path, *, box_size: int = 10, border: int = 4) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    code = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(fill_color="#111827", back_color="#ffffff")
    image.save(target)
    return target


def make_qr_card_svg(
    payload: str,
    path: str | Path,
    *,
    title: str = "TOUCHLEARN",
    subtitle: str = "扫描二维码，收听触觉图讲解",
    footer: str = "SCAN FOR AUDIO",
) -> Path:
    """A6 portrait card with a 70 mm QR and bilingual instructions."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    png = target.with_name("qr.png")
    make_qr_png(payload, png, box_size=12, border=4)
    encoded = base64.b64encode(png.read_bytes()).decode("ascii")
    safe_title = html.escape(title)
    safe_subtitle = html.escape(subtitle)
    safe_footer = html.escape(footer)
    safe_url = html.escape(payload)
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="105mm" height="148mm" viewBox="0 0 105 148">
  <rect width="105" height="148" fill="#fbfaf6"/>
  <rect x="4" y="4" width="97" height="140" rx="3" fill="none" stroke="#111827" stroke-width="0.8"/>
  <text x="52.5" y="17" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="6" font-weight="700" letter-spacing="0.8" fill="#111827">{safe_title}</text>
  <text x="52.5" y="24.5" text-anchor="middle" font-family="'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif" font-size="3.6" fill="#374151">{safe_subtitle}</text>
  <rect x="14" y="31" width="77" height="77" fill="#ffffff" stroke="#d1d5db" stroke-width="0.4"/>
  <image x="17.5" y="34.5" width="70" height="70" href="data:image/png;base64,{encoded}"/>
  <text x="52.5" y="119" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="3.6" font-weight="700" letter-spacing="1.2" fill="#111827">{safe_footer}</text>
  <text x="52.5" y="126" text-anchor="middle" font-family="'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif" font-size="3.2" fill="#4b5563">扫码后在浏览器中播放，可暂停、重播</text>
  <text x="52.5" y="135" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="2.6" fill="#6b7280">{safe_url}</text>
  <text x="52.5" y="141" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="2.4" fill="#9ca3af">TOUCHLEARN / TEAM STELLA</text>
</svg>
"""
    target.write_text(svg, encoding="utf-8")
    return target


def audio_page_html(
    *,
    title: str,
    subtitle: str,
    script: str,
    audio_url: str,
    download_url: str,
    model_url: str,
) -> str:
    safe_title = html.escape(title)
    safe_subtitle = html.escape(subtitle)
    safe_script = html.escape(script).replace("\n", "<br/>")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{safe_title} - 触觉图音频</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; font-family: 'Microsoft YaHei','Noto Sans CJK SC',system-ui,sans-serif; background:#f5f2e9; color:#172033; }}
  main {{ max-width: 640px; margin: 0 auto; padding: 28px 22px 48px; }}
  .eyebrow {{ font: 700 12px/1.4 Arial, sans-serif; letter-spacing: .18em; color:#7c5b2e; }}
  h1 {{ font-size: 28px; line-height:1.25; margin: 10px 0 6px; }}
  p.sub {{ color:#52606d; margin: 0 0 22px; }}
  .player {{ background:#fffdf7; border:1px solid #e3dccb; border-radius:14px; padding:18px; box-shadow:0 8px 24px rgba(23,32,51,.06); }}
  audio {{ width:100%; }}
  .script {{ margin-top:22px; background:#fffdf7; border-left:4px solid #b8863b; padding:18px 18px 18px 20px; border-radius:0 12px 12px 0; line-height:1.9; font-size:16px; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:22px; }}
  a.button {{ display:inline-block; padding:10px 16px; border-radius:999px; background:#172033; color:#fff; text-decoration:none; font-size:14px; }}
  a.ghost {{ background:transparent; color:#172033; border:1px solid #c9c2b2; }}
  footer {{ margin-top:28px; color:#6b7280; font-size:12px; }}
</style>
</head>
<body>
<main>
  <div class="eyebrow">TOUCHLEARN · 触觉讲解</div>
  <h1>{safe_title}</h1>
  <p class="sub">{safe_subtitle}</p>
  <div class="player">
    <audio controls autoplay preload="metadata" src="{html.escape(audio_url)}"></audio>
  </div>
  <div class="script">{safe_script}</div>
  <div class="actions">
    <a class="button" href="{html.escape(download_url)}">下载音频</a>
    <a class="button ghost" href="{html.escape(model_url)}">下载 STL</a>
  </div>
  <footer>音频为机器合成，关键尺寸与读图顺序以触觉图和合规报告为准。</footer>
</main>
</body>
</html>
"""
