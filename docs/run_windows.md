# Windows 运行说明

本仓库在 Windows 上用 Git Bash + 嵌入式 Python 3.14.6 开发。本文记录实际可用的命令和几个容易踩的坑。

## 1. 最快路径：不安装任何东西

仓库根目录执行：

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
pip install -e ".[ui]"     # Streamlit 预览
pip install -e ".[qr]"     # 二维码生成
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

# 跑全部测试
python -m unittest discover -s tests -t .
```

`build` 和 `all` 的区别只是默认输出目录；两者都会在检查失败时返回非零退出码。

## 4. Streamlit 预览

在标准 Python 环境安装可选依赖后：

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
```

CI 使用 GitHub Actions 的 setup-python，在 3.10 和 3.12 上安装 `.[dev]` 后跑测试和双案例构建。
