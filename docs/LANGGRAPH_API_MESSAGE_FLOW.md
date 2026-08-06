# LangGraph API 消息、工具调用与上下文数据流

本文说明不使用 Claude Code CLI 时，`scripts/evaluation/run_langgraph_eval.py`
如何构造 OpenAI-compatible 请求、执行工具、保存历史并发起下一轮请求。内容以当前代码为准，
重点描述默认的 `chat_completions` 模式；末尾单独说明 `responses` 模式的差异。

## 1. 总体数据流

```text
任务数据 + README
       │
       ▼
system prompt + initial user prompt
       │
       ▼
compact_messages() ── 注入 durable working memory、裁剪旧交换
       │
       ├── messages
       ├── tools JSON Schema
       └── model / sampling / token 参数
       │
       ▼
POST /chat/completions
       │
       ▼
assistant response
       │
       ├── 无 tool_calls ──► 结束
       │
       └── 有 tool_calls
               │
               ▼
          本地 ToolExecutor 执行
               │
               ▼
          role=tool + tool_call_id + result
               │
               └────────────► 并入历史，进入下一轮
```

模型并不直接进入容器或调用 CyberGym server。模型只返回结构化工具请求；Python 进程解析参数，
由 `ToolExecutor` 在本地执行，再把有界结果作为 `role: "tool"` 消息返回模型。

## 2. 初始 system prompt 和 user prompt

### 2.1 System prompt

基础 system prompt 是 `run_langgraph_eval.py` 顶部的常量 `SYSTEM_PROMPT`。它规定：

- 目标是生成并提交最小 PoC；
- 只能在隔离 workspace 中工作；
- 应采用聚焦的源码检查和快速提交循环；
- 如何理解 `submit_poc` 的返回值；
- 何时更新结构化调查状态和 checkpoint；
- 普通探索结束后 finalization turn 的限制。

启用 `--differential-submit` 时，代码把 `DIFFERENTIAL_SUBMIT_PROMPT` 追加到基础 system
prompt，补充 vulnerable/fixed 双目标结果的判断规则。因此最终值是：

```python
system_prompt = SYSTEM_PROMPT + (
    DIFFERENTIAL_SUBMIT_PROMPT if args.differential_submit else ""
)
```

### 2.2 Initial user prompt

任务由 `generate_task()` 生成到运行目录，随后读取任务目录中的 `README.md`。初始 user prompt
由固定说明、完整 README 内容以及 workspace 路径和首步行动约束组成：

```text
Authorized CyberGym task workspace: /workspace

<该任务 README.md 的内容>

<工具路径约定和聚焦首步的固定说明>
```

LangGraph 的初始 state 只有两条消息：

```json
[
  {"role": "system", "content": "<system prompt>"},
  {"role": "user", "content": "<initial task prompt>"}
]
```

这两条消息是 `compact_messages()` 所称的 `static` 消息；正常压缩始终保留它们。

## 3. 工具信息如何提供给模型

工具不是写进 system prompt 的纯文本列表，而是通过 Chat Completions 请求顶层的 `tools`
字段发送。`TOOLS` 常量目前定义七个 function tool：

- `list_files`
- `read_file`
- `update_investigation_state`
- `save_checkpoint`
- `write_file`
- `run_command`
- `submit_poc`

每个工具包含名称、说明和 JSON Schema 参数。例如可抽象为：

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "...",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "max_lines": {"type": "integer"}
      },
      "required": ["path"]
    }
  }
}
```

普通探索轮发送全部工具。Finalization 阶段只发送 `submit_poc` 的 schema，并在本地执行层再次
限制允许的工具名，不能只依赖 prompt 或模型自觉遵守。

## 4. 实际发送的 Chat Completions 请求

每轮调用的核心请求等价于：

```json
{
  "model": "<API_MODEL>",
  "messages": ["<压缩后的完整消息历史>"],
  "tools": ["<当前阶段可用的工具 schema>"],
  "tool_choice": "auto",
  "temperature": "<API_TEMPERATURE>",
  "seed": 20260731,
  "max_tokens": "<API_MAX_TOKENS>"
}
```

以下字段是条件发送的：

- `top_p`：只有未启用 `--omit-top-p` 时发送；
- `max_tokens`：配置不为 `None` 时发送；
- `reasoning_effort`：配置非空时发送。

SDK 的 `timeout` 是客户端请求选项，不一定成为 HTTP JSON body 字段。API key 由 OpenAI client
放进认证 header，不写入 trajectory。

发送前，代码在 `trajectory.jsonl` 写入 `event: "model_request"`，其中保存本轮实际可见的
`messages`、`tools`、估算输入 token 和被省略的交换数量。因此排查某轮模型究竟看到了什么时，
应查看这一事件，而不是只根据最终 state 猜测。

## 5. `tool_call_id` 的来源和目的

### 5.1 ID 由谁生成

在 Chat Completions 模式中，`tool_call_id` 对应的 ID 由上游模型/API 在 assistant response 的
`tool_calls[*].id` 中返回。本地代码不为普通 Chat Completions 调用重新生成 ID。

典型模型回复为：

```json
{
  "role": "assistant",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\":\"src/parser.c\",\"start_line\":120}"
      }
    }
  ]
}
```

注意 `arguments` 在协议中是 JSON 字符串，执行前代码还要调用 `json.loads()`。

### 5.2 为什么返回工具结果时必须带 ID

工具执行完成后，本地构造：

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "<read_file 的有界结果>"
}
```

这个 ID 是调用与结果之间的关联键。它解决两个问题：

1. 同一 assistant response 可以同时请求多个工具，不能仅靠消息顺序可靠判断每个结果属于哪个调用；
2. API 协议需要验证每个 assistant tool call 都有对应结果，避免孤立的 `role: tool` 消息。

因此不能安全地删除 ID，也不应在下一轮为历史调用重新生成另一个 ID。当前实现会原样保留模型返回的
`call["id"]`，并将其复制到对应结果的 `tool_call_id`。

### 5.3 ID 是否会导致缓存不命中

新工具调用产生新 ID 是正常的，因为它位于新增的对话尾部。下一轮请求会原样重发历史 assistant
tool call 及其 tool result；已有历史 ID 不会每轮重新随机化。因此在没有压缩重排时：

```text
第 N 轮请求 = 既有稳定前缀 + 第 N-1 轮新增的 assistant/tool 后缀
```

从前缀缓存角度看，新 ID 通常只改变新后缀，不会单独破坏此前完全相同的前缀。不能为了缓存把所有
调用改成同一个固定 ID：这会造成多调用关联冲突，也可能被 provider 判定为无效消息序列。

缓存是否命中最终由上游 Azure gateway/LiteLLM/模型服务决定；本地 Chat Completions 路线目前没有
发送 `prompt_cache_key`。Claude bridge 中存在的稳定 `prompt_cache_key` 逻辑不用于这条直接
LangGraph 路线。

## 6. 工具调用的执行和回复处理

OpenAI SDK 返回后，代码取 `completion.choices[0].message`，通过 `model_dump(exclude_none=True)`
保存为普通字典，并记录 `event: "model"`、延迟和 usage。

如果回复没有 `tool_calls`，图路由到结束。如果存在工具调用，则按以下顺序处理每一个调用：

1. 从 `function.arguments` 解析 JSON；
2. 根据当前阶段检查工具是否允许；
3. 调用 `ToolExecutor.invoke(name, arguments)`；
4. 对原始结果做审计保存、确定性清洗、长度限制和可选总结；
5. 构造带相同 `tool_call_id` 的 `role: tool` 消息。

`ToolExecutor` 会把完整 raw、processed 和最终 model-visible 结果分别记录到运行产物；真正放回
消息历史的是有界的 model-visible 字符串，不一定是工具的完整 stdout 或完整文件内容。

为了避免已经完成的巨大 `write_file` 内容长期占据上下文，历史中的 assistant message 会经过
`sanitize_assistant_message()`：普通 assistant 文本最多保留 8000 字符，`write_file` 参数中的
大段正文会替换为已写入字节数提示。工具调用 ID、工具名以及调用和结果的配对关系仍保留。

一轮完成后的历史形态为：

```json
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "<initial task>"},
  {"role": "user", "content": "[CYBERGYM_DURABLE_WORKING_MEMORY] ..."},
  {
    "role": "assistant",
    "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "..."}}]
  },
  {"role": "tool", "tool_call_id": "call_1", "content": "<result>"}
]
```

之后图从 `tools` 节点回到 `model` 节点，构造下一次请求。

## 7. 下一轮如何携带之前的上下文

Chat Completions 本身是无状态调用。当前实现不会只发送“最新问题”，而是每轮重新发送压缩后的
完整 `messages` 数组。服务端若要利用 prompt prefix cache，也是基于这些重复前缀，而不是依靠
`tool_call_id` 自动恢复会话。

每轮调用模型前，`compact_messages()` 执行以下操作：

1. 固定保留最前面的 system prompt 和 initial user prompt；
2. 删除上一轮注入的 durable-memory user message，避免重复堆叠；
3. 用 `ContextLedger.render()` 生成最新的 bounded durable memory，并作为新的 `role: user` 消息
   插在两条静态消息之后；
4. 将后续历史按协议完整块分组：一条 assistant 消息及其紧随的所有 tool results 是不可拆分块；
5. 从最新块开始向前装入 token 预算，旧块放不下时整块丢弃；
6. 返回新的完整历史，并直接替换 LangGraph state，而不是只构造一次临时请求。

预算计算是：

```text
可用于 memory 和历史交换的预算
= context_token_budget
 - max_tokens 输出预留
 - tools schema 估算 token
 - system prompt 与 initial user prompt 估算 token
```

token 使用 `字符数 / 4` 的保守近似，不是 provider tokenizer 的精确计数。若 provider 仍报告上下文
溢出，Chat Completions 路线会以原预算的约 65% 重做一次更激进压缩并重试。

## 8. 当前实现对前缀缓存的实际影响

### 有利因素

- 同一任务内 system prompt 和 initial user prompt 保持不变；
- 普通探索轮的工具 schema 保持不变；
- 已保留的历史 tool-call ID 原样重发，不会每轮重新生成；
- 新交互通常追加在历史尾部。

### 不利因素

- durable memory 位于第三条消息，并会随着读取、命令、checkpoint 和提交结果变化；它一旦变化，
  其后的消息不再构成与上一请求完全相同的序列前缀；
- 历史达到预算后，压缩会从中间删除旧交换，使消息序列发生重组；
- finalization 阶段工具列表从全部工具变成仅 `submit_poc`，请求结构发生变化；
- 直接 LangGraph Chat Completions 路线没有设置稳定 `prompt_cache_key`；
- provider 是否把 tools、system 和 messages 如何序列化进缓存键，不由本仓库控制。

所以，`tool_call_id` 并不是当前最值得怀疑的缓存障碍。更主要的问题是“变化的 durable memory
被放在历史靠前位置”。如果未来要优化缓存，较安全的方向是研究把稳定历史前缀保持在前、将最新
durable memory 放到动态后缀，同时继续保证 assistant tool call 与 tool result 不被拆开。此类修改会
改变模型看到信息的优先顺序和压缩语义，需要配套测试，不能通过删除调用 ID 实现。

## 9. Responses API 模式的差异

当 `API_MODE=responses` 时：

- system prompt 改放在 `instructions`；
- 工具 schema 转成 Responses API function tool 格式；
- 首轮或链重置时，通过 `input` 发送初始任务和 durable memory；
- 连续轮通过 `previous_response_id` 引用 provider 侧历史，本地主要发送最新的
  `function_call_output`；
- `function_call_output.call_id` 仍必须等于模型 function call 返回的 `call_id`；
- 达到 `API_RESPONSE_COMPACTION_TURNS` 或发生上下文溢出时，丢弃旧
  `previous_response_id`，使用 durable memory 建立新链。

因此 Responses 模式不用每轮在 HTTP body 中重发全部历史，但依赖 provider 保存
`previous_response_id` 对应的状态。调用 ID 在这里同样是工具请求与结果之间的协议关联键，而不是
缓存控制参数。

## 10. 调试时应查看的运行产物

- `trajectory.jsonl` 中的 `model_request`：该轮实际发送给 provider 的消息和工具；
- `trajectory.jsonl` 中的 `model`：provider 返回的 assistant payload 和 usage；
- `trajectory.jsonl` 中的 `tool`：工具参数和最终 model-visible 结果；
- `tool-results/raw/`：未经处理的原始工具输出；
- `tool-results/processed/`：确定性处理后的输出；
- `working-memory.md`：下一轮 durable memory 的人类可读形式；
- `investigation-state.json`：durable memory 的结构化状态；
- `config.json`：本次模型、API mode 和 token/context 参数。

排查缓存时，应比较相邻两个 `model_request.messages` 的最长公共前缀，并结合 provider 返回的
cached-token usage。仅观察 `tool_call_id` 是否不同，不能判断真正的缓存命中范围。
