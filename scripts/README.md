# Scripts 目录

脚本按职责分组。所有命令都建议从仓库根目录执行；脚本内部会自行解析仓库根路径。

## 目录索引

| 目录 | 用途 |
|---|---|
| `evaluation/` | Agent 评测、批量运行、验证、失败任务记录与结果汇总 |
| `serving/` | 本地模型服务和 CyberGym 验证服务启动器 |
| `monitoring/` | 长任务和主机资源监控 |
| `data/` | benchmark 数据与 Docker runner 下载 |
| `manifests/` | 任务 ID 清单；每行一个 `arvo:<id>` 或 `oss-fuzz:<id>` |
| `profiles/` | API 模型的非敏感配置；密钥仍只从环境变量读取 |

## 常用入口

API 模型批次：

```bash
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/all_tasks.txt \
  deepseek-v4-full-r1
```

同一个批次入口支持两种 Agent runtime。默认 `USE_CLAUDE_CODE_AGENT=false`，继续使用
LangGraph；设为 `true` 时使用官方 Claude Code Agent SDK，并在批次内启动一个仅监听
loopback 的 Anthropic→OpenAI 协议桥，因此现有 OpenAI-compatible DeepSeek profile
无需更换 API endpoint：

```bash
export CYBERGYM_DEEPSEEK_API_KEY='...'
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4-claude-code.env \
  scripts/manifests/api_smoke_tasks.txt \
  deepseek-v4-claude-code-smoke-r1
```

也可以不新增 profile，在命令环境中覆盖开关：

```bash
USE_CLAUDE_CODE_AGENT=true bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/api_smoke_tasks.txt \
  deepseek-v4-claude-code-smoke-r1
```

开关只接受小写的 `true` 或 `false`。Claude Code 模式只向模型暴露受控的 CyberGym
MCP 工具，文件和命令仍在无网络 Docker sandbox 中执行，并默认同步验证 vulnerable
与 fixed 两个目标。该模式是“Claude Code runtime + 非 Claude 模型”的实验性组合，
不属于 Anthropic 官方支持的模型路由方式。

运行结果写入 `outputs/<batch-name>/`，其中包含 `tasks/`、`logs/`、`server/`、
`failed_tasks.txt` 和批次汇总文件。Claude Code 模式还会写入
`logs/claude-code-gateway.log`；每个任务的 `config.json` 记录
`agent_backend: "claude_code"`。

本地 Qwen 全量评测：

```bash
bash scripts/evaluation/run_full_qwen36_official.sh <batch-name>
```

下载 server runner：

```bash
.venv/bin/python scripts/data/server/download.py \
  --tasks-file cybergym_data/tasks.json
```

汇总已有 API 批次：

```bash
.venv/bin/python scripts/evaluation/summarize_api_batch.py \
  --tasks-file scripts/manifests/all_tasks.txt \
  --run-root outputs \
  --batch-name <batch-name> \
  --output-dir outputs/<batch-name>
```

更完整的目录说明和迁移映射见 [`docs/PROJECT_STRUCTURE.md`](../docs/PROJECT_STRUCTURE.md)。
