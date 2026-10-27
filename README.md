# AML 时序证据记忆服务

面向 [Agent Memory Leaderboard（AML）](https://agentmemoryleaderboard.ai/) 文本记忆赛道，按比赛适配版研究方案实现原文写入和证据检索。服务只返回历史证据，由下游生成最终答案。

实现版本 `1.0.0`：中文分词 FTS5/BM25、中文二元/三元字符索引、名称/数字/日期线索匹配、RRF、规则问题要点与覆盖选择、冗余惩罚、会话邻域补全、角色标记和保守时序排序。无需模型密钥或外部向量数据库。

## 骨架已实现的契约行为

| 契约要求 | 位置 |
|---|---|
| `POST /aml/add` 字段原样回显（平台逐字节比对） | `app/main.py` |
| `POST /aml/search` 返回 `{"data": [...]}`，空数组 = 弃权 | `app/main.py` |
| `GET /health` 免认证返回 2xx | `app/main.py` |
| `request_id` 幂等账本（重试不重复，冲突负载 409） | `app/store.py` |
| `user_id` 隔离边界 | `app/main.py` + `app/store.py` |
| Bearer / Token / X-Api-Key 三种认证头 | `app/main.py` |

## 快速开始

### Windows 空白服务器免安装包

Windows Server x64 可以直接运行打包后的 `aml-server.exe`，无需安装 Python、Docker 或数据库。
解压 `dist/AML-Memory-Server-1.0.0-windows-x64.zip` 后运行 `start.cmd`；首次启动自动生成带随机密钥的 `.env`，数据保存在 EXE 同目录的 `data` 中。
完整部署说明见 `packaging/windows/README-Windows.md`。开发机运行 `powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1` 可重新构建。

### 源码运行

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS
pip install -r requirements.txt

uvicorn app.main:app --port 8000  # 另开终端：
python scripts/smoke_check.py http://127.0.0.1:8000
pytest -v
```

Docker 一键：`copy .env.example .env`（填入密钥）后 `docker compose up -d --build`。

`requirements-lock.txt` 记录本次 Windows/Python 3.12 验证环境的依赖版本，可用 `pip install -r requirements-lock.txt` 复现。`requirements.txt` 保持跨平台依赖范围；Docker/Linux 仍需在目标环境验证。

Windows 已创建项目 `.venv` 时可直接用 `.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`。本地 uvicorn 不会自动读取 `.env`，需要设置环境变量或加 `--env-file .env`。Docker Compose 会加载 `.env`。

## 用官方 BEAM 链路本地评测

`scripts/eval_beam.py` 复用 AML 官方 `data/beam/pipeline.py` 的回答/评审提示词，对模板做端到端质量自测：

```bash
# 数据：从 Hugging Face（Mohammadta/BEAM，CC BY-SA 4.0）获取公开数据，整理为
#   conversations.json  [{id, messages:[{role,content,timestamp}]}]
#   questions.jsonl     {id, conversation_id, question, question_type, rubric_nuggets:[...]}
python scripts/eval_beam.py ingest --data conversations.json --server http://127.0.0.1:8000
python scripts/eval_beam.py search --data questions.jsonl --server http://127.0.0.1:8000 --out eval_input.jsonl
python scripts/eval_beam.py score  --input eval_input.jsonl \
    --pipeline ..\agent-memory-leaderboard\data\beam\pipeline.py   # 需先设 ANSWER_API_* / JUDGE_API_* 环境变量
```

`tests/fixtures/` 下有合成样例可跑通 ingest/search（score 需要真实模型密钥）。注意：本地复测结果与官方成绩不可直接比较（评测契约版本、私有题目、锁定模型不同）；评测数据用毕 30 天内删除。

## 写入与检索行为

`POST /aml/add` 保存逐字原文、role、user_id、session_id、原始 Unix 毫秒 timestamp 和写入时间 created_at。同一会话按到达顺序追加 message_index，因此调用方应按会话顺序写入；缺失原始时间时保持 NULL。重复文本作为独立事件保留，仅相同 request_id/负载重试去重。账本、原文和三路索引在同一事务提交，索引失败时全部回滚。

旧版数据库在首次启动时自动迁移并建立索引，已有记录 ID 和账本不变。旧记录无法恢复缺失的原始时间，序号根据旧库插入顺序补齐。迁移前建议备份 `data/memory.db`。

`POST /aml/search` 保持 `query/user_id/top_k` 输入和 `{"data":[...]}` 输出，每项仍只有 `id/content/score/created_at`；可接受的 `options` 保留但不作为历史事实使用。结果按最终 score 降序、数量不超过 top_k（1 到 100），无匹配返回空数组。score 是内部排序值，不是概率。词法和字符路径使用 FTS5 的 BM25；各路排名以 RRF 融合，再按要点覆盖与冗余惩罚选择证据。

`content` 格式为一行 `[source {JSON 元数据}]` 后换行接原始消息正文。元数据包含 source_id、role、session_id、message_index、timestamp_ms、evidence（direct 或 context）。正文保持逐字原文，created_at 保持实际写入时间。这样即使下游只读取 content，也能区分 AI 建议、用户陈述及补充上下文。

所有召回、日期过滤和邻域查询都应用 user_id 边界。上下文还限制在同一 session_id。识别明确的 `2025年前`、`after 2024-12-31`、`since 2024` 等方向性日期条件，原始时间未知的记录不会通过该条件；只提及日期时不自动施加时间截断。询问最初/现在时依据原始时间调整排序，跨会话不凭时间先后推断因果或更正。矛盾原文可同时返回，不自动生成裁决。

内部 `delete_memory(user_id,id)` 会通过数据库触发器同步删除索引，尚未暴露额外公开删除接口。身份验证为比赛服务级 API Key；user_id 是平台提供的命名空间，不是终端用户登录认证。生产使用需由可信平台/网关负责分配 user_id。

## 配置与实验边界

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| AML_API_KEY | 空 | 空时本地免认证；部署时设置 |
| AML_AUTH_MODE | bearer | none 显式关闭认证；其余模式接受三类兼容认证头 |
| RETRIEVAL_MODE | hybrid | hybrid 三路融合；bm25 仅词法召回；abstain 空结果 |
| RETRIEVAL_CANDIDATE_LIMIT | 200 | 各路径候选上限 |
| RETRIEVAL_CONTEXT_WINDOW | 1 | 同会话前后邻域宽度，0 关闭 |
| RETRIEVAL_CHAR_BUDGET | 24000 | 返回 content 总字符预算，含来源头；不是 token 数 |

基础版使用规则要点识别和小型显式词典（如数据库关联 MySQL/PostgreSQL）扩展检索。角色和时间标记基于原始字段，变化/原因线索只用于排序，不声称完成通用语义理解。大模型问题分解、向量召回、完整问答轮次补全、持久化语义版本图和全套组件消融开关尚未实现，需进一步实验验证收益。

运行 `python -m pytest -q` 检查接口、三类认证头、用户隔离、真实 HTTP 冒烟与样例 BEAM ingest/search、时序查询、迁移、删除、并发重试和事务回滚。BEAM 脚本通过环境变量 AML_API_KEY 发送认证，同会话分块依次写入，不同会话可并行。本地脚本当前按每会话一个用户构造样例，不代表已验证同一用户的跨会话评测能力；跨会话行为另有检索测试。

## 目录结构

```text
aml-starter-template/
├── app/
│   ├── main.py       # FastAPI 入口：/aml/add /aml/search /health + 认证
│   ├── config.py     # 环境变量配置
│   ├── schemas.py    # 契约请求/响应模型
│   ├── store.py      # SQLite 原文、账本、迁移和事务索引
│   └── retriever.py  # 问题规划、多路召回、覆盖选择和邻域补全
├── scripts/smoke_check.py
├── tests/test_contract.py
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

## 参赛注意

- 本地 Smoke 通过 ≠ 官方成绩；提交前复核官方接口指南（契约可能更新）
- 已保持本项目提供的接口契约，尚未向 AML 平台提交官方兼容性测试或完整评测；文档中的提交流程和类别应以官方当期规则为准
- 评测数据仅限当次运行使用，30 天内删除
- 生产部署必须设置 `AML_API_KEY` 并走 HTTPS
