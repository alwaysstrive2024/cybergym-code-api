# 环境变量与终端启动参数

本文说明 `scripts/profiles/*.env` 中的主要配置、批量运行脚本的终端参数，以及 API Key 和总结模型的配置方式。真实 API Key 只应通过终端环境变量传入，不要写入 profile、脚本或 Git。

## 启动命令格式

```bash
bash scripts/evaluation/run_api_subset_test_50.sh PROFILE_ENV TASKS_FILE BATCH_NAME
```

三个位置参数均为必填：

1. `PROFILE_ENV`：模型 profile，例如 `scripts/profiles/glm-52.env`。
2. `TASKS_FILE`：任务清单，例如 `scripts/manifests/api_smoke_tasks.txt`。
3. `BATCH_NAME`：本次运行名称，只能是单个目录名，不能包含 `/`。

默认从任务清单抽取 50 个任务。设置 `EVAL_SAMPLE_SIZE=0` 时使用整个清单；设置为其他非负整数时抽取对应数量。

## 可直接使用的 GLM 示例

```bash
cd /root/cybergym

# Key 只放在当前终端环境中；变量名来自 glm-52.env 的 API_KEY_ENV。
export CYBERGYM_GLM_API_KEY='替换为真实Key'

# 推荐策略：达到预算后提示收敛，但不阻止工具。
export AGENT_POLICY_MODE=guided

# 本例仅运行 smoke 清单中的 1 个任务。
export EVAL_SAMPLE_SIZE=1

bash scripts/evaluation/run_api_subset_test_50.sh \
  scripts/profiles/glm-52.env \
  scripts/manifests/api_smoke_tasks.txt \
  glm52_guided_smoke
```

如果希望与旧行为对照，只需在启动前改为：

```bash
export AGENT_POLICY_MODE=baseline
```

如果要强制限制宽泛浏览：

```bash
export AGENT_POLICY_MODE=enforced
```

## Profile 的模型与认证参数

### `API_BASE_URL`

主模型服务地址。LangGraph 路线以及 Claude Code bridge 路线均通过该地址访问上游模型。

### `API_MODEL`

主 Agent 使用的模型 ID。模型 ID 必须是 `API_BASE_URL` 对应服务实际支持的名称。

### `API_KEY_ENV`

保存“API Key 所在环境变量的名字”，不是 Key 本身。例如：

```bash
API_KEY_ENV="CYBERGYM_GLM_API_KEY"
```

表示运行前应在终端执行：

```bash
export CYBERGYM_GLM_API_KEY='替换为真实Key'
```

常见 profile 使用的变量名如下：

| Profile 类型 | 默认 Key 环境变量 |
|---|---|
| GLM | `CYBERGYM_GLM_API_KEY` |
| DeepSeek | `CYBERGYM_DEEPSEEK_API_KEY` |
| Kimi | `CYBERGYM_KIMI_API_KEY` |
| Sonnet/LiteLLM | `CYBERGYM_SONNET_API_KEY` |
| 通用复现 profile | `CYBERGYM_MODEL_API_KEY` |
| Claude 官方接口 | `ANTHROPIC_API_KEY` |

### `API_MODE`

指定上游 API 协议模式。通常保留 profile 中的值，不应仅根据模型名称猜测协议。

### `API_PROMPT_CACHE_KEY_MODE`

控制 Claude Code bridge 是否向 OpenAI-compatible router 发送稳定的 `prompt_cache_key`。`off` 不发送；`stable` 根据每个任务独立生成的 session UUID 的不可逆哈希生成稳定 Key。GLM profile 默认使用 `stable`，其他 profile 默认 `off`。该字段帮助 router 进行缓存分区路由，但实际缓存能力仍取决于上游实现。

缓存验收时可以固定本地 bridge 端口并实时查看不含 prompt 正文的诊断：

```bash
export CYBERGYM_CLAUDE_BRIDGE_PORT=39829
watch -n 1 curl -s http://127.0.0.1:39829/cache-status
```

`previous_request_is_prefix=true` 表示上一轮完整历史是本轮消息的稳定前缀；`cached_tokens>0` 才表示上游实际报告缓存命中。诊断只包含会话哈希、累计消息哈希、数量和 token 统计。

### `API_MAX_TOKENS`、`API_TEMPERATURE` 与 `API_MAX_STEPS`

- `API_MAX_TOKENS`：单次模型响应的最大生成 token 数。
- `API_TEMPERATURE`：采样温度；漏洞复现实验通常使用较低值以提高稳定性。
- `API_MAX_STEPS`：单任务允许的最大 Agent 步数，不等于读取预算或首次提交期限。

### `API_CONTEXT_TOKEN_BUDGET`

主 Agent 对话上下文的目标 token 预算。它控制整体上下文管理；`AGENT_SOURCE_CHAR_BUDGET` 则只累计通过 `read_file` 展示的源码字符，两者单位不同。

### `API_MAX_TOOL_RESULT_CHARS`

单个工具结果允许展示给模型的最大字符数。超过限制时会进行确定性清洗、裁剪，并在启用总结模型时生成结构化摘要。

GLM profile 默认使用 `20000`。`read_file` 本身有 `16000` 字符硬上限，因此普通源码读取通常可以保持连续；更长的命令结果仍会进入总结或裁剪流程。运行前可用 `export API_MAX_TOOL_RESULT_CHARS=数值` 覆盖。

### `API_RESPONSE_COMPACTION_TURNS`

达到相应对话轮数后触发响应压缩的周期参数，用来降低长会话的上下文增长。

### `EVAL_CONCURRENCY`

并发运行的任务数。提高它会增加吞吐量，同时增加内存和 API 并发压力。

## Agent 上下文策略参数

### `AGENT_POLICY_MODE`

可选值：

- `baseline`：记录指标，但不提示或阻止浏览工具，用于旧行为对照。
- `guided`：达到阈值后追加收敛提示，但本次工具仍会执行；这是推荐默认值。
- `enforced`：达到阈值后阻止宽泛的 `list_files`、`read_file` 和 `run_command`。

确定性清洗和工具结果压缩独立于该模式，因此 `baseline` 下仍然生效。

### `AGENT_READ_CALL_BUDGET`

普通 `read_file` 调用的预算，默认 `18`。达到预算后，`guided` 给出提示，`enforced` 阻止后续宽泛读取，`baseline` 不干预。

### `AGENT_SOURCE_CHAR_BUDGET`

累计展示给主 Agent 的源码字符预算，默认 `120000`。这是字符数而非 token 数，也不是单次结果上限。

### `AGENT_STALE_TOOL_LIMIT`

结构化假设没有更新时，允许连续执行浏览类工具的次数，默认 `6`。用于抑制反复浏览但不形成新判断的行为。

### `AGENT_FIRST_SUBMIT_TOOL_DEADLINE`

首次提交候选结果的工具调用期限，默认 `12`。它是收敛阈值，不是任务终止步数：`guided` 只提示，`enforced` 会限制继续宽泛浏览。

`enforced` 触发门控后，仍允许更新调查状态、写候选、提交，以及不超过 80 行并带有明确假设和预期证据的窄范围读取。

## 工具结果总结模型

### `AGENT_TOOL_SUMMARY_MODEL`

指定用于总结超长工具结果的模型 ID。profile 默认使用当前主模型：

```bash
AGENT_TOOL_SUMMARY_MODEL="${AGENT_TOOL_SUMMARY_MODEL-${API_MODEL}}"
```

该写法有三种行为：

- 未设置：复用当前主模型。
- 设置为模型 ID：使用同一 endpoint 上的指定模型。
- 显式设置为空字符串：关闭模型总结，但保留确定性清洗。

例如，使用同一 router 上的另一个总结模型：

```bash
export AGENT_TOOL_SUMMARY_MODEL='router支持的模型ID'
```

关闭模型总结：

```bash
export AGENT_TOOL_SUMMARY_MODEL=''
```

当前总结模型复用主 Agent 的 endpoint 和 Key，不需要额外 Key。当前实现尚不支持总结模型单独使用另一套 Base URL 和 API Key。

## 环境变量覆盖规则

profile 中常见写法：

```bash
AGENT_POLICY_MODE="${AGENT_POLICY_MODE:-guided}"
```

表示终端未设置或设置为空时使用 `guided`，否则保留终端值。因此应先 `export`，再运行脚本。

总结模型使用的是不带冒号的 `-`：

```bash
AGENT_TOOL_SUMMARY_MODEL="${AGENT_TOOL_SUMMARY_MODEL-${API_MODEL}}"
```

这样显式导出空字符串时不会回退到主模型，从而可以关闭模型总结。

## 常见启动变体

使用默认推荐参数：

```bash
export CYBERGYM_GLM_API_KEY='替换为真实Key'
bash scripts/evaluation/run_api_subset_test_50.sh \
  scripts/profiles/glm-52.env \
  scripts/manifests/api_smoke_tasks.txt \
  glm52_default_smoke
```

使用 baseline，并关闭模型总结：

```bash
export CYBERGYM_GLM_API_KEY='替换为真实Key'
export AGENT_POLICY_MODE=baseline
export AGENT_TOOL_SUMMARY_MODEL=''
export EVAL_SAMPLE_SIZE=1
bash scripts/evaluation/run_api_subset_test_50.sh \
  scripts/profiles/glm-52.env \
  scripts/manifests/api_smoke_tasks.txt \
  glm52_baseline_smoke
```

收紧上下文预算：

```bash
export CYBERGYM_GLM_API_KEY='替换为真实Key'
export AGENT_POLICY_MODE=enforced
export AGENT_READ_CALL_BUDGET=14
export AGENT_SOURCE_CHAR_BUDGET=90000
export AGENT_STALE_TOOL_LIMIT=5
export AGENT_FIRST_SUBMIT_TOOL_DEADLINE=10
export EVAL_SAMPLE_SIZE=1
bash scripts/evaluation/run_api_subset_test_50.sh \
  scripts/profiles/glm-52.env \
  scripts/manifests/api_smoke_tasks.txt \
  glm52_enforced_smoke
```

运行前可用以下命令确认关键变量是否存在；不要打印 Key 的实际内容：

```bash
test -n "${CYBERGYM_GLM_API_KEY:-}" && echo 'GLM Key 已配置'
printf 'policy=%s summary_model=%s\n' \
  "${AGENT_POLICY_MODE:-由profile决定}" \
  "${AGENT_TOOL_SUMMARY_MODEL-由profile决定}"
```
