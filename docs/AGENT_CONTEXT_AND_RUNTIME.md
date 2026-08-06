# CyberGym Agent 上下文、记忆与双模型运行指南

本文档说明 `feature/agent-context-optimization` 分支的 Agent 改动。它不改变任务执行、Docker 隔离或
CyberGym 验证语义；变更只作用于模型可见的工具结果、对话历史和 Agent 会话。

## 解决的问题

此前，单次文件读取最多可返回 1MB，单工具结果默认最多 32K 字符，长任务会反复携带源码、终端日志和
`write_file` 内容。结果是上下文溢出，或者旧交换被直接删除后忘记漏洞条件、关键行号、已写 PoC 和验证回执。

本分支将原始日志与可恢复事实分离：原始内容留在 run artifact，模型只保留最近的完整工具交换和很小的证据索引。

## 交接分支

本改动在 `feature/agent-context-optimization` 上开发，基线是
`origin/feature/claude-code-agent-entry` 的 `de28895`，**没有修改 `main`**。交接/评审时先在该分支测试，
通过后再把它合并回承载 Claude Code 入口的 feature 分支；不要直接从旧 `main` 复制文件，否则会丢失该入口已有
的 Agent SDK、MCP 和 bridge 改动。

```bash
git switch feature/agent-context-optimization
git diff origin/feature/claude-code-agent-entry...HEAD
```

## 改动内容

### 共享 Tool Runtime

`src/cybergym/agents/runtime.py` 是 Claude Agent SDK 的工具层。

- `read_file` 默认最多 160 行、硬上限 400 行 / 16K 字符，输出带行号；
- 单个工具结果默认最多 12,288 字符；每次调用的完整原始结果保存到 `tool-results/raw/`，确定性清洗结果保存到 `tool-results/processed/`，模型只接收清洗后视图或其有标记的有界版本；
- 新增 `save_checkpoint(summary, next_hypothesis)`；
- 新增 `update_investigation_state(...)`，用有上限的结构记录目标、输入路径、崩溃证据、文件状态与重开条件、调用边、受控值、未知项和主/备假设，并同步写入 `investigation-state.json`；
- 每次工具调用更新 `working-memory.md`；
- 账本只保存源码范围、已写文件、命令状态、提交回执和 checkpoint，不复制大日志。
- `read_file` 会隐藏明确识别的文件头许可证/版权或自动生成声明、压缩空行并谨慎折叠大型静态表，所有保留内容继续使用原始行号；函数内部安全注释、TODO/FIXME、约束和被注释代码不会被通用删除。
- 完整重复读取相同范围时先返回软提醒；新假设、显式 `reopen`、不同范围、此前截断或文件修改会自动放行。
- `run_command` 对完全相同的 warning 去重、折叠超长十六进制串，并把 Sanitizer 诊断和栈上下文移到模型视图开头，避免关键证据被通用截断丢弃。
- 清洗后仍超限的 Sanitizer/runtime 和大型 `rg` 结果使用确定性 JSON 证据摘要；配置 `--tool-summary-model` 后，其他复杂超长源码/命令结果才会调用独立总结模型。模型摘要必须提供带 `file:line` 的事实并通过结构校验，否则自动回退 processed 截断视图。
- 共享探索策略支持 `--policy-mode baseline|guided|enforced`。默认 `guided` 只在读取预算、首次提交截止或假设停滞时追加提示；`enforced` 会阻止新的宽泛浏览，但仍允许带 `hypothesis`、`expected_evidence` 且不超过 80 行的窄读取，以及状态更新、写候选和提交。

### LangGraph / OpenAI-compatible 后端

`scripts/evaluation/run_langgraph_eval.py` 使用 `src/cybergym/agents/context.py`。

- `messages` reducer 改为替换式，压缩后的旧历史不会留在 LangGraph state；
- 每次请求保留 system、初始任务、durable memory 和尽可能多的完整最近 tool exchanges；
- 已完成的 `write_file` 大参数被脱敏；
- Responses API 每 `--response-compaction-turns` 轮从任务和 durable memory 重建链，不无限延续
  `previous_response_id`。

### Claude Agent SDK 后端

`scripts/evaluation/run_claude_code_eval.py` 不再无限 `resume` session。

- 总轮数由 `--max-turns` 控制；
- `--session-turn-budget`（默认 12）将任务切成多个独立 SDK session；
- 后续 session 只接收 `working-memory.md`，不接收上一个 session 的原始对话；
- Docker sandbox 和 MCP `ToolExecutor` 在 session 间持续存在，因此解压后的源码和已写 PoC 不会丢失；
- 每个 phase 写入 `context_session_started` 到 `trajectory.jsonl`。

这项分段策略对官方 Claude 和低价 bridge 模型都有效。官方 Claude 仍可使用原生 Agent 能力；bridge 模式
不会错误地假定 DeepSeek 拥有相同的内部 compacting 行为。

## 预备条件

在仓库根目录执行：

```bash
uv sync --extra agent --extra server
docker build -t cybergym-langgraph-agent:0.1 -f docker/langgraph-agent/Dockerfile .
```

当 `USE_CLAUDE_CODE_AGENT=true` 时，Python Agent SDK 会启动 Claude Code CLI harness；因此还需 Node.js 18+
和 Claude Code CLI。一次性安装与检查：

```bash
node --version
npm install -g @anthropic-ai/claude-code
claude --version
```

不要在本项目 profile 或 Git 中执行 `claude login` 来保存个人凭证。bridge 模式只使用本批次临时网关令牌；
官方模式从 `ANTHROPIC_API_KEY` 读取专用 API key。

数据集默认目录为 `cybergym_data/data`。若数据在其他位置：

```bash
export CYBERGYM_DATA_DIR=/absolute/path/to/data
```

## 运行模式

| 模式 | 开关 | 所需模型密钥 | 是否启动 bridge | 适用场景 |
| --- | --- | --- | --- | --- |
| LangGraph | `USE_CLAUDE_CODE_AGENT=false` | 现有 `API_KEY_ENV` | 否 | 低成本基线 |
| SDK + bridge | `true` + `CLAUDE_CODE_PROVIDER=bridge` | 现有 `API_KEY_ENV` | 是 | 对比 Claude Code harness 对非 Claude 模型的收益 |
| 官方 Claude SDK | `true` + `CLAUDE_CODE_PROVIDER=anthropic` | `ANTHROPIC_API_KEY` | 否 | 失败任务复跑、高价值最终尝试 |

### A. 低成本 LangGraph + OpenAI-compatible 模型

```bash
export CYBERGYM_DEEPSEEK_API_KEY='replace-with-key'

USE_CLAUDE_CODE_AGENT=false bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/api_smoke_tasks.txt \
  deepseek-context-baseline
```

可调参数：

```bash
API_CONTEXT_TOKEN_BUDGET=24576
API_MAX_TOOL_RESULT_CHARS=12288
API_RESPONSE_COMPACTION_TURNS=12
```

确保 input budget、最大输出 tokens、工具定义和安全余量之和小于模型实际 context window。

### B. Claude Agent SDK + Bridge（低成本兼容模式）

该模式使用官方 Agent SDK 和 MCP harness，但模型是 OpenAI-compatible 上游。桥接层将 Anthropic Messages
转换为 Chat Completions，因此不应依赖 Claude 专属 Prompt Caching。

```bash
export CYBERGYM_DEEPSEEK_API_KEY='replace-with-key'

USE_CLAUDE_CODE_AGENT=true \
CLAUDE_CODE_PROVIDER=bridge \
CLAUDE_CODE_SESSION_TURN_BUDGET=12 \
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/api_smoke_tasks.txt \
  deepseek-sdk-context
```

入口会启动 loopback-only bridge；模型 API key 不写入 profile、轨迹或 run artifact。

### C. 真实 Anthropic Claude Agent SDK

此模式不启动 bridge，直连 Anthropic API，适合复跑失败任务和高价值最终尝试。

```bash
export ANTHROPIC_API_KEY='replace-with-anthropic-console-key'

USE_CLAUDE_CODE_AGENT=true \
CLAUDE_CODE_PROVIDER=anthropic \
CLAUDE_CODE_MODEL=claude-sonnet-4-6 \
CLAUDE_CODE_SESSION_TURN_BUDGET=12 \
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/claude-sonnet-4-6.env \
  path/to/failed_tasks.txt \
  sonnet-context-retry
```

`ANTHROPIC_API_KEY` 只在官方模式需要。Claude API 按量计费；建议在 Console 设置预算或预付额度，并先用小
manifest 验证。CyberGym 本地验证 server 的管理 key 默认由脚本临时生成；连接外部验证 server 时另设
`CYBERGYM_API_KEY`。

本仓库的官方模式是 **API key 路径**，因此计入 Anthropic API 的 token 用量，不使用 Claude Pro/Max 的
交互式订阅额度。请创建权限最小化、专用于评测的 key，并在 Console 的 Billing/Usage 中设置消费上限；组织账户
也可以用 Usage & Cost Admin API 做批次成本核对。费用取决于模型、输入、输出和缓存命中，不能把 bridge 的价格
外推到官方 Claude。当前实现没有向 SDK 强行写入 `cache_control`：SDK 自身可能复用其稳定前缀，但我们的可靠性
机制不依赖缓存，仍以 12-turn session 和 durable memory 为边界。

参考官方说明：[Claude Code SDK](https://docs.anthropic.com/en/docs/claude-code/sdk)、
[Claude Code 安装](https://docs.anthropic.com/en/docs/claude-code/getting-started)、
[API 价格](https://platform.claude.com/docs/en/about-claude/pricing)、
[用量与成本 API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api)。

## 运行产物与排查

每任务输出在 `outputs/<batch>/tasks/<task>/`：

- `trajectory.jsonl`：模型、SDK、工具和 session reset 事件；
- `working-memory.md`：后续 session 加载的紧凑事实账本；
- `tool-results/raw/`：每次工具调用的未修改原始输出，仅用于审计和人工排查；
- `tool-results/processed/`：确定性清洗后的完整输出；
- `tool-results/summaries/`：仅在超限且结构化摘要成功时生成的 JSON；
- `investigation-state.json`：结构化调查状态；
- `summary.json`：结束原因与提交记录；
- `verification.json`：最终 CyberGym 验证。

如果模型重复读取或忘记漏洞条件，先检查 `working-memory.md` 是否存在具体 checkpoint，例如：

```text
Fact: parser.c:L82-L109 reads a signed 16-bit length before bounds validation.
Candidate: poc.bin.
Next hypothesis: encode -1 length while keeping the enclosing record checksum valid.
```

不要将原始日志、整段源码或无证据长推理写入 checkpoint。

`summary.json` 的 `metrics` 与 trajectory 的 `policy_state`/`submission_outcome` 可直接用于 A/B 聚合，不需要读取工具正文。主要指标包括首次提交步、重复读取率、raw/processed/model-visible 字符量、无效提交、假设修订、策略提示/拦截和上下文超限次数。

可对一个或多个轨迹生成统一 JSON 指标：

```bash
python scripts/evaluation/summarize_trajectory_metrics.py run-a/trajectory.jsonl run-b/trajectory.jsonl
```

该工具只解析标准化事件及字符数/计数元数据，不读取 `tool-results/raw/` 或任务实验数据。

## 建议评测顺序

1. 用同一小 manifest 跑模式 A，确认边界和历史压缩正常；
2. 以模式 B 对照 SDK harness 对低价模型的收益；
3. 将 A/B 失败清单交给模式 C；
4. 在同一 manifest、固定配置和相同验证 server 下比较通过率、提交次数、session 数和成本。
