# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

面向浙大城市学院的模型原生校园认知 Agent。公开仓库包含两个子应用：`apps/api`（Python 3.12 + FastAPI + SQLAlchemy async + Alembic，uv 管理）和 `apps/web`（Next.js + React + TypeScript，Node 24）。产品与架构规格从 `docs/00-index.md` 进入，改动某个子系统前先读对应 Spec；术语见 `docs/glossary.md`。

## 常用命令

后端（共享根目录 `.venv`，由 uv 创建）：

```bash
make api-install                 # uv venv + editable 安装 apps/api[dev]
make api-migrate                 # alembic upgrade head
make api-dev                     # uvicorn，端口 8000
make api-test                    # pytest apps/api/tests
.venv/bin/pytest apps/api/tests/test_runtime.py -k 名称    # 单测
.venv/bin/ruff check apps/api/src apps/api/tests
.venv/bin/ruff format apps/api/src apps/api/tests          # CI 用 --check 校验
```

前端：`make web-install` / `make web-dev`（端口 3000）/ `make web-build`；类型检查 `cd apps/web && npm run typecheck`。

提交前的完整门槛是 CI（`.github/workflows/ci.yml`）：ruff check + ruff format --check + alembic 迁移 + pytest + npm typecheck + build。`make check` 只覆盖其中的 api-test 和 web-build，不要只跑它。

数据同步与检索调试用 CLI：`.venv/bin/hzcu-agent`（`list-sources`、`sync-sources --limit 3`、`search-memory`、`reindex-memory`、`sync-worker`），运行手册见 `docs/14-source-registry-operations.md`。

## 文档先行（硬规则）

- 行为或边界变化：先改对应 PRD/Spec（`docs/01`–`10`），再改实现。
- 关键架构变化：新增或废弃 ADR（`docs/adr/`），不得静默改写历史 ADR。
- 新增 P0/P1 需求必须录入 `docs/12-requirements-traceability.md`。

## 安全红线

- 校园实时能力只允许 `campus_notice.read`。禁止实现任何申请、提交、报名等业务写操作。
- 学生凭据不得进入公开仓库、文件、数据库、日志或模型上下文。
- 绝不读取、显示或提交 `API.txt`、`.env` 等含密钥的文件。
- 文档、测试和示例中不得出现真实密码、Cookie、Token、学号。
- 新增或变更校内实时采集器必须同步更新 Source Registry（`apps/api/src/hzcu_agent/resources/sources.yaml`）、权限边界和测试。

## 约定

- 文档一律中文、UTF-8；代码注释与标识符用英文。
- Python：ruff，line-length 100，规则 E/F/I/UP/B/ASYNC，格式化用 ruff format。
- Web 没有 ESLint，`tsc --noEmit` 是唯一静态检查。
- pytest 已配 `asyncio_mode = auto`（async 测试不需要标记）和 `-s`。

## 环境注意

- 本目录尚未 `git init`（.gitignore 和 CI 配置已就位，之后会初始化）。执行任何 git 操作前先与用户确认。
- 路径含空格（`HZCU Agent`），shell 命令中必须加引号。
- `mnt/`、`apps/api/mnt/`、`apps/api/tmp/` 是测试把 Windows 临时路径误解析成相对路径产生的杂物目录，不要在其中工作，也不要提交；`data/`、`apps/api/data/`、`output/`、`var/` 是本地运行产物。
- 默认演示模式（`HZCU_MODEL_PROVIDER=demo`）真实执行检索与证据链但不做语义推理；启用真实模型需复制 `.env.example` 为 `.env` 并配置 openai provider。
