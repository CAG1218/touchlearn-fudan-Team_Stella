"""QR assets for the audio track.

The QR code is never embossed into the STL: a tactile QR without contrast is
not reliably scannable.  The bundle therefore contains a flat QR PNG plus an
A6 printable card; the model's pedestal (or the plate's top-right reserve)
gives it a flat place to sit.
"""

from __future__ import annotations

import base64
import html
import json
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
    audio_available: bool = True,
    script_url: str = "audio_script.txt",
) -> str:
    safe_title = html.escape(title)
    safe_subtitle = html.escape(subtitle)
    safe_script = html.escape(script).replace("\n", "<br/>")
    script_json = json.dumps(script, ensure_ascii=False).replace("</", "<\\/")
    if audio_available:
        player = f'<audio controls autoplay preload="metadata" src="{html.escape(audio_url)}"></audio>'
    else:
        player = (
            '<div class="fallback">'
            "<p>当前部署没有可用的语音合成服务，讲解稿已经生成。</p>"
            '<button class="button" id="speakBtn" type="button">用浏览器朗读</button>'
            "</div>"
        )
    audio_download = (
        f'<a class="button" href="{html.escape(download_url)}">下载音频</a>'
        if audio_available and download_url
        else ""
    )
    fallback_script = ""
    if not audio_available:
        fallback_script = f"""
<script>
(() => {{
  const text = {script_json};
  const button = document.getElementById("speakBtn");
  if (!button || !("speechSynthesis" in window)) return;
  let speaking = false;
  const reset = () => {{ speaking = false; button.textContent = "用浏览器朗读"; }};
  button.addEventListener("click", () => {{
    if (speaking) {{ speechSynthesis.cancel(); reset(); return; }}
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 0.95;
    utterance.onend = reset;
    utterance.onerror = reset;
    speechSynthesis.speak(utterance);
    speaking = true;
    button.textContent = "停止朗读";
  }});
}})();
</script>
"""
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
  .fallback p {{ margin:0 0 12px; color:#52606d; line-height:1.7; }}
  .script {{ margin-top:22px; background:#fffdf7; border-left:4px solid #b8863b; padding:18px 18px 18px 20px; border-radius:0 12px 12px 0; line-height:1.9; font-size:16px; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:22px; }}
  a.button, button.button {{ display:inline-block; padding:10px 16px; border-radius:999px; background:#172033; color:#fff; text-decoration:none; font-size:14px; border:0; cursor:pointer; font-family:inherit; }}
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
    {player}
  </div>
  <div class="script">{safe_script}</div>
  <div class="actions">
    {audio_download}
    <a class="button ghost" href="{html.escape(script_url)}">下载讲解稿</a>
    <a class="button ghost" href="{html.escape(model_url)}">下载 STL</a>
  </div>
  <footer>音频为机器合成，关键尺寸与读图顺序以触觉图和合规报告为准。</footer>
</main>
{fallback_script}
</body>
</html>
"""
