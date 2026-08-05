# Claude Code Agent 评测入口

CyberGym API 批次入口支持两种 Agent runtime：

- `USE_CLAUDE_CODE_AGENT=false`：使用原有 LangGraph Agent；
- `USE_CLAUDE_CODE_AGENT=true`：使用官方 Claude Code Agent SDK。

Claude Code runtime 还支持 `CLAUDE_CODE_PROVIDER=bridge`（默认，使用 OpenAI-compatible 上游）与
`CLAUDE_CODE_PROVIDER=anthropic`（直连官方 Anthropic API）。关于上下文、记忆、API 和完整运行命令，见
[AGENT_CONTEXT_AND_RUNTIME.md](AGENT_CONTEXT_AND_RUNTIME.md)。

当 `CLAUDE_CODE_PROVIDER=bridge` 时，Claude Code runtime 通过仅监听 `127.0.0.1` 的批次级协议桥，
将 Anthropic Messages/SSE 转换成现有 profile 使用的 OpenAI-compatible Chat Completions。runtime 只获得
临时网关凭证，不获得上游 API key。`CLAUDE_CODE_PROVIDER=anthropic` 时则跳过桥，使用
`ANTHROPIC_API_KEY` 直连官方 Anthropic API。两种模式都只能调用六个 in-process MCP 工具；文件与命令操作
继续在无网络、降权的 Docker sandbox 中执行。

## 直接启动 DeepSeek smoke test

```bash
cd /root/cybergym
uv sync --extra agent --extra server
export CYBERGYM_DEEPSEEK_API_KEY='你的模型服务key'

USE_CLAUDE_CODE_AGENT=true bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/api_smoke_tasks.txt \
  claudecode-agent-deepseek-v4
```

这条命令不需要设置 `CYBERGYM_API_KEY`，也不需要手动启动或填写 CyberGym Server
地址。入口脚本会为该批次：

1. 生成只存在于进程环境中的临时 CyberGym 管理 Key；
2. 选择空闲的 loopback 端口并启动本地 CyberGym Server；
3. 把自动得到的 `http://127.0.0.1:<port>` 传给 Agent 和最终验证程序；
4. 结束时关闭本批次启动的 Server 和 Claude 协议桥。

模型服务 Key 仍然只放在本地环境变量 `CYBERGYM_DEEPSEEK_API_KEY` 中，不会写入
profile、轨迹或 Git。

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

## 连接已有 CyberGym Server

默认无需连接已有 Server。如果赛事方提供了远程验证服务，则显式设置服务地址和赛事方
提供的管理 Key，入口脚本不会再启动本地 Server：

```bash
export CYBERGYM_SERVER_URL='https://cybergym.example.com'
export CYBERGYM_API_KEY='赛事方提供的key'

USE_CLAUDE_CODE_AGENT=true bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4.env \
  scripts/manifests/api_smoke_tasks.txt \
  claudecode-agent-deepseek-v4-remote
```

## 同时运行多个模型

每个不同的 batch name 都有独立的随机端口、临时网关令牌、Server 数据库、日志和输出
目录，因此可以并行启动。不要复用同一个 batch name，也不要为多个进程手动指定同一个
`CYBERGYM_SERVER_PORT`。dataset 和 Docker image 可以共享。
