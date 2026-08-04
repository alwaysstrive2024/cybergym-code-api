# Claude Code Agent 评测入口

CyberGym API 批次入口支持两种 Agent runtime：

- `USE_CLAUDE_CODE_AGENT=false`：使用原有 LangGraph Agent；
- `USE_CLAUDE_CODE_AGENT=true`：使用官方 Claude Code Agent SDK。

Claude Code 模式通过仅监听 `127.0.0.1` 的批次级协议桥，将 Anthropic Messages/SSE
转换成现有 profile 使用的 OpenAI-compatible Chat Completions。Claude Code 子进程只
获得临时网关凭证，不获得上游 API key。它只能调用五个 in-process MCP 工具；文件与
命令操作继续在无网络、降权的 Docker sandbox 中执行。

## DeepSeek 单任务 smoke test

```bash
cd /root/cybergym
uv sync --extra agent --extra server
export CYBERGYM_DEEPSEEK_API_KEY='你的key'

bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4-claude-code.env \
  scripts/manifests/claude_code_smoke_task.txt \
  deepseek-v4-claude-code-smoke-r1
```

也可以在已有 OpenAI-compatible profile 前覆盖开关；命令仍然只有三个位置参数：

```bash
USE_CLAUDE_CODE_AGENT=true bash scripts/evaluation/run_api_subset.sh \
  <profile.env> <tasks.txt> <batch-name>
```

开关只接受小写 `true` 或 `false`。结果写入 `outputs/<batch-name>/`：

- `logs/claude-code-gateway.log`：协议桥日志；
- `logs/<task>.log`：单任务运行日志；
- `tasks/<task>/trajectory.jsonl`：SDK 消息及完整工具轨迹；
- `tasks/<task>/summary.json`：Agent 结束原因与提交记录；
- `tasks/<task>/verification.json`：同一批次验证服务/PoC 数据库给出的最终结果。

Claude Code 模式默认同步调用 `/submit-diff`，因此每次 PoC 提交立即返回 vulnerable 与
fixed 两侧输出。使用非 Claude 模型驱动 Claude Code runtime 属于实验性、非 Anthropic
官方支持的组合；协议桥为 DeepSeek thinking tool-call 回合保留一次性
`reasoning_content`，但不会把它写入轨迹或作为可见文本返回给 Agent。
