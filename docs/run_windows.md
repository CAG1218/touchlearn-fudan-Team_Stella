# Windows 运行说明

本仓库在 Windows 上用 Git Bash + 嵌入式 Python 3.14.6 开发。本文记录实际可用的命令和几个容易踩的坑。

## 1. 最快路径：不安装任何东西

仓库根目录执行交互式前端：

```bash
python scripts/run_agent.py --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。不配置模型也能离线演示：任意需求走关键词/模板，函数走正则，图片只留档。界面是聊天式工作台，上传、草稿确认、生成、合规报告、音频和二维码都在同一条对话里。

手机或其他设备访问：

- 同一 Wi-Fi：打开终端打印的 `http://<局域网 IP>:8765`；
- 临时公网：`python scripts/run_public.py`，会生成 `trycloudflare.com` 地址和访问码；
- 固定部署：见 [deploy.md](deploy.md)。

命令行构建两个基准案例：

```bash
python scripts/dev_cli.py validate examples/robot_arm/diagram.json
python scripts/dev_cli.py check examples/robot_arm/diagram.json
python scripts/dev_cli.py all examples/robot_arm/diagram.json -o dist/robot-arm-6axis
```

`scripts/dev_cli.py` 只做一件事：

```python
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from touchlearn.cli import main
```

它绕过了两个环境问题：

- 嵌入式 Python 不读 `PYTHONPATH`，所以不能靠设置环境变量找到 `src/`；
- 嵌入式 Python 缺少 setuptools，`pip install -e .` 会失败。

`python -m touchlearn` 在没有安装包的环境里不可用，必须走 `scripts/dev_cli.py`，或者用下面的普通 Python 虚拟环境。

### 接入大模型

复制 `.env.example` 为 `.env`，至少填：

```dotenv
TOUCHLEARN_LLM_PROVIDER=dashscope
TOUCHLEARN_LLM_KEY=sk-...
```

视觉模型默认复用文本预置；自定义网关再填 `TOUCHLEARN_VLM_*`。不配置时页面会显示“离线”，功能自动降级。详细预置、端点和隐私说明见 [providers.md](providers.md)。

## 2. 正式安装

如果机器上有标准 CPython（推荐 3.10–3.12）：

```bash
python -m venv .venv
source .venv/Scripts/activate      # Git Bash
# 或 PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

之后可以直接：

```bash
touchlearn --version
touchlearn all examples/quadratic/diagram.json -o dist/quadratic-y-x2-2x-3
python -m unittest discover -s tests -t .
```

可选：

```bash
pip install -e ".[ui]"           # Streamlit 预览
pip install -e ".[qr]"           # 二维码生成
pip install -e ".[docs-reader]"  # PDF 文字层读取（pypdf）
```

## 3. 常用命令

```bash
# 只校验 schema 和手写约束
python scripts/dev_cli.py validate examples/robot_arm/diagram.json

# 只跑布局，输出 SVG 和 layout.json 到 build/
python scripts/dev_cli.py layout examples/robot_arm/diagram.json

# 只跑可打印性检查，不写文件
python scripts/dev_cli.py check examples/quadratic/diagram.json --json

# 完整构建，输出到 dist/<id>/
python scripts/dev_cli.py all examples/robot_arm/diagram.json -o dist/robot-arm-6axis

# 只生成音频脚本
python scripts/dev_cli.py audio examples/quadratic/diagram.json

# 两个案例一起构建，刷新 dist/、docs/images/、dist/manifest.json
python scripts/build_examples.py

# 启动交互式智能体（默认 0.0.0.0:8765）
python scripts/run_agent.py --port 8765

# 不自动打开浏览器，二维码使用指定公网/局域网地址
python scripts/run_agent.py --port 8765 --no-browser --public-url http://192.168.1.20:8765

# 跑全部测试
python -m unittest discover -s tests -t .
```

`build` 和 `all` 的区别只是默认输出目录；两者都会在检查失败时返回非零退出码。

## 4. 推荐前端与旧 Streamlit 预览

推荐使用 `scripts/run_agent.py` 启动的交互式前端（支持任意需求、上传、草稿确认、构建、合规报告和二维码）。旧的 Streamlit 页面仍保留，只覆盖两个基准案例。在标准 Python 环境安装可选依赖后：

```bash
pip install -e ".[ui]"
streamlit run app/streamlit_app.py
```

页面可以切换两个案例，查看 SVG 预览、检查指标、下载 STL 和音频脚本。它调用的是同一个编译器，不是另一套逻辑。

嵌入式 Python 没有 Streamlit，不要在那个环境里尝试；用标准 Python 虚拟环境。

## 5. 常见问题

### `ModuleNotFoundError: No module named 'touchlearn'`

- 在仓库根目录运行命令；
- 用 `python scripts/dev_cli.py ...`；
- 或者完成 `pip install -e .` 后再用 `touchlearn`。

不要设置 `PYTHONPATH` 指望嵌入式 Python 生效，它不会读。

### 前端打不开 / 手机扫码打不开

- 端口被占用时换一个：`python scripts/run_agent.py --port 8877`；
- 只在本机用可以加 `--host 127.0.0.1`；
- 手机扫码要求电脑和手机同一 Wi-Fi，且 Windows 防火墙放行该端口；
- 二维码地址不对时显式传 `--public-url http://<电脑局域网IP>:8765`。

### 上传 PDF 后提示“没有可提取的文字层”

这是扫描件，不是程序把文字读丢了。两种处理：

1. 把关键页导出为 PNG，走图片理解（需配置视觉模型）；
2. 安装 `pypdf` 后重试文字型 PDF：`pip install pypdf`。

当前不内置 PDF 光栅化，扫描件不会假装被读懂。

### 页面显示“离线” / 模型不可用

- 检查 `.env` 是否在仓库根目录或启动目录；
- 检查 `TOUCHLEARN_LLM_PROVIDER`、`KEY`、`MODEL` 是否齐全；
- 打开 `http://127.0.0.1:8765/api/providers` 看 `configured` 和 `message`；
- 本地 Ollama 不需要 key，但要先启动服务并拉取模型。

### `pip install -e .` 报 `BackendUnavailable` / 找不到 setuptools

嵌入式发行版不带 setuptools。两个选择：

1. 不安装，直接用 `scripts/dev_cli.py`；
2. 换标准 CPython 建虚拟环境。

### `python` 指向了别的 Python

Git Bash 里用：

```bash
which python
python --version
```

确认是预期解释器。需要固定路径时直接写完整路径。

### 中文输出乱码

Git Bash 通常没问题。PowerShell 脚本开头加：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

### CSV / JSON 打开是乱码

所有文本文件都是 UTF-8。用 VS Code、Notepad++ 或 Excel 的"数据 → 从文本"并选择 UTF-8。

### 构建成功但切片软件提示网格错误

先跑：

```bash
python scripts/dev_cli.py check examples/robot_arm/diagram.json
```

确认 `mesh_watertight` 为 true。v0 的检查是拓扑级的，不检查自交；如果切片器仍报错，把 `report.json` 和切片器截图一起记录，回到布局或几何层排查。

### 生成的文件在哪里

- CLI 默认：`dist/<diagram.id>/`；
- `layout` 默认：`build/<diagram.id>/`；
- 交互式智能体：`outputs/drafts/<draft_id>/` 与 `outputs/jobs/<job_id>/`；
- 测试临时文件：系统临时目录，测试结束自动清理。

`build/` 被 `.gitignore` 忽略；`dist/` 是提交进仓库的正式产物。

## 6. 重新生成提交产物

改完源码或案例后：

```bash
python -m unittest discover -s tests -t .
python scripts/build_examples.py
git status --short
```

确认 `dist/` 与 `docs/images/` 的变更符合预期，再提交。不要手工编辑 `dist/` 里的文件。

## 7. 环境记录

开发时实际使用的版本：

```text
Python 3.14.6 (embedded, Windows)
Git for Windows / Git Bash
pytest 未安装；测试使用标准库 unittest
jsonschema 可选；未安装时走手写校验
pypdf 可用；缺失时 PDF 读取会明确提示，不影响其他链路
TTS：Windows Microsoft Huihui Desktop（zh-CN）
```

CI 使用 GitHub Actions 的 setup-python，在 3.10 和 3.12 上安装 `.[dev]` 后跑测试和双案例构建。
