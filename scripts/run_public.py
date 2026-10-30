"""Start TouchLearn and a temporary Cloudflare quick tunnel.

The quick tunnel needs no Cloudflare account and gives a random
``https://*.trycloudflare.com`` address.  It is meant for demos and for
checking the phone layout from another network; the address changes every
time the process restarts.  For an always-on deployment use Docker/Render,
see ``docs/deploy.md``.

Login uses phone/e-mail verification codes.  A public demo without SMTP or
SMS credentials falls back to development mode, where the code is shown on
the page; configure a channel before using this for a real event.

Usage:
    python scripts/run_public.py
    python scripts/run_public.py --no-tunnel
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:  # pragma: no cover - very old Python
    pass

ROOT = Path(__file__).resolve().parents[1]
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def port_available(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        for candidate in packages.glob("Cloudflare.cloudflared_*/cloudflared.exe"):
            return str(candidate)
    for candidate in (
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
        Path("/usr/local/bin/cloudflared"),
        Path("/usr/bin/cloudflared"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def auth_snapshot(base_url: str) -> dict:
    try:
        with urllib.request.urlopen(base_url + "/api/health", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("auth") or {}
    except Exception:  # noqa: BLE001 - optional status display
        return {}


def wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - startup probe
            pass
        time.sleep(0.4)
    return False


def drain(stream, sink: "queue.Queue[str]") -> None:
    try:
        for line in stream:
            sink.put(line.rstrip())
    except Exception:  # noqa: BLE001 - reader thread
        pass


def lan_address(port: int) -> str:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from touchlearn.agent.server import detect_lan_ip

        return f"http://{detect_lan_ip()}:{port}"
    except Exception:  # noqa: BLE001 - optional convenience
        return f"http://127.0.0.1:{port}"


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动触见并建立临时公网入口")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（手机同 Wi-Fi 可访问）")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--outputs", default="", help="产物目录；默认沿用服务配置")
    parser.add_argument("--no-tunnel", action="store_true", help="只启动本地/局域网服务，不建立隧道")
    parser.add_argument("--tunnel-timeout", type=float, default=60.0, help="等待公网地址的秒数")
    args = parser.parse_args()

    if not port_available(args.host, args.port):
        print(f"端口 {args.port} 已被占用。请先确认是不是触见已经在运行；换端口用 --port。")
        return 2

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if args.outputs.strip():
        env["TOUCHLEARN_OUTPUTS"] = args.outputs.strip()

    server_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_agent.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--no-browser",
    ]
    print("正在启动触见服务……")
    server = subprocess.Popen(server_cmd, cwd=ROOT, env=env)
    local_url = f"http://127.0.0.1:{args.port}"
    try:
        if not wait_http(local_url + "/api/health"):
            print("服务没有在预期时间内就绪。请查看上方日志。")
            return 1

        print()
        auth = auth_snapshot(local_url)
        channels = []
        if auth.get("phone"):
            channels.append("手机号")
        if auth.get("email"):
            channels.append("邮箱")
        print("触见 TouchLearn 已启动")
        print(f"  本机:   {local_url}")
        print(f"  局域网: {lan_address(args.port)}")
        print(f"  登录:   {' / '.join(channels) if channels else '未配置验证码通道'}")
        if auth.get("dev_mode"):
            print("  [警告] 开发模式：验证码直接显示在页面上；公网演示结束前请配置 SMTP 或短信。")
        if not args.no_tunnel:
            cloudflared = find_cloudflared()
            if not cloudflared:
                print()
                print("没有找到 cloudflared。临时公网入口需要先安装：")
                print("  winget install --id Cloudflare.cloudflared")
                print("或从 https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ 下载。")
                print("当前仍可通过局域网地址访问。按 Ctrl+C 停止。")
                server.wait()
                return 0

            print()
            print("正在建立临时公网隧道……")
            tunnel = subprocess.Popen(
                [cloudflared, "tunnel", "--url", local_url, "--no-autoupdate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            lines: "queue.Queue[str]" = queue.Queue()
            threading.Thread(target=drain, args=(tunnel.stdout, lines), daemon=True).start()
            public_url = ""
            seen: list[str] = []
            deadline = time.time() + args.tunnel_timeout
            while time.time() < deadline and tunnel.poll() is None:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                seen.append(line)
                match = TUNNEL_RE.search(line)
                if match:
                    public_url = match.group(0)
                    break

            if not public_url:
                print("隧道没有返回公网地址。cloudflared 最近的输出：")
                for line in seen[-12:]:
                    print("  " + line)
                print("当前仍可通过局域网地址访问。")
                stop_process(tunnel)
                server.wait()
                return 0

            payload = {
                "url": public_url,
                "local_url": local_url,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "auth_dev_mode": bool(auth.get("dev_mode")),
                "temporary": True,
            }
            out_dir = ROOT / "outputs"
            out_dir.mkdir(exist_ok=True)
            (out_dir / "public_access.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print()
            print("=" * 58)
            print("  公网地址（任何设备可访问）")
            print(f"  {public_url}")
            print("=" * 58)
            print("这是临时隧道：本进程停止、电脑休眠或断网后地址失效。")
            print("正式部署见 docs/deploy.md（Docker / Render）。")
            print("按 Ctrl+C 停止。")
            print()
            server.wait()
            return server.returncode or 0
        else:
            print()
            print("未建立公网隧道。按 Ctrl+C 停止。")
            server.wait()
            return server.returncode or 0
    except KeyboardInterrupt:
        print("\n正在停止触见与隧道……")
        return 0
    finally:
        stop_process(locals().get("tunnel"))
        stop_process(server)


if __name__ == "__main__":
    raise SystemExit(main())
