# AGENTS.md

面向在本仓库工作的 AI 编程助手（TRAE、WorkBuddy 及其他代理）的约定。
架构与模块职责见 `docs/architecture.md`，提交口径见 `docs/submission.md`。

## 运行与验证

```bash
python scripts/run_agent.py --port 8765            # 启动，浏览器打开 http://127.0.0.1:8765
python scripts/run_agent.py --port 8765 --no-browser
python -m unittest discover -t . -s tests          # 全量测试（不依赖 pytest）
python -m unittest tests.test_agent_llm -v         # 单模块
python scripts/degrade_drill.py --out <path>       # 降级演练，需先启动本地模型
```

## 硬性约束

- 版本号在 `src/touchlearn/__init__.py` 固定为 `0.4.0`，`deploy/install-update.sh` 会硬校验；升级必须同时改脚本。
- 密钥、令牌、服务器口令只从环境变量或本机 `.env` 读取，不得写入代码、注释、测试夹具或提交历史。
- 几何、盲文、可打印性与合规结论保持确定性；大模型只做理解（分类、抽取、场景 JSON），其输出不得直接当作几何结果。
- 本地兜底只在文本任务生效（`TOUCHLEARN_LOCAL_FALLBACK=1`），图片识别不回落。
- 测试不依赖真实云端或真实本地模型，统一使用本地 stub 服务。

## 提交约定

- 一个提交只做一件事，说明写清「现象 / 根因 / 变化 / 验证」。
- 修复类改动在 `docs/fix-reports/` 按既有格式补一篇报告。
- 新增环境变量时同步更新 `.env.example`；AI 参与的编码与调试记录到 `docs/ai-collaboration.md`。
