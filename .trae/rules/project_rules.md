---
description: TouchLearn 触觉生成智能体的项目规则
alwaysApply: true
---

# 项目规则

- 仓库根目录的 `AGENTS.md` 是完整约定，本文件是 TRAE 的入口摘要。
- 改动前先读 `docs/architecture.md` 对应章节，确认模块职责（`geometry` / `layout` / `cnbraille` / `printability` / `agent`）。
- 版本号保持 `0.4.0`，`deploy/install-update.sh` 会硬校验。
- 测试命令：`python -m unittest discover -t . -s tests`，不引入 pytest 作为必需依赖。
- 任何密钥、令牌、服务器口令不得写入仓库内的任何文件。
- 修改完成后同步三处：`docs/fix-reports/`（修复类）、`.env.example`（新环境变量）、`docs/ai-collaboration.md`（AI 协作记录）。
