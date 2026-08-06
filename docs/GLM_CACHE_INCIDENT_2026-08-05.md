# GLM 长上下文实验缓存失效事故说明

## 事故等级与状态

- 事故日期：2026-08-05
- 影响范围：Claude Code CLI 经 CyberGym Anthropic bridge 调用 OpenAI-compatible GLM router 的长上下文实验
- 严重程度：严重
- 当前状态：所有实验已暂停，尚未批准恢复
- 数据处置：未删除或覆盖已有实验文件

## 事故摘要

GLM 长上下文 Agent 实验被发现 router/Key 侧记录的 prompt cache 命中率为 0。代码专项审计确认，Anthropic bridge 在重建 OpenAI Chat Completions 历史消息时会一次性消费 `reasoning_content`：同一个历史 tool call 在一次请求中包含 reasoning，后续请求中该字段消失，导致已经发送过的历史前缀发生变化。

此外，bridge 未映射 Anthropic 缓存控制字段，未读取或回传上游 cached-token usage；reasoning cache 在不同 session 之间全局共享；缺失 tool ID 时存在随机 ID 和流式 ID 不一致问题。这些问题共同造成缓存前缀不稳定、缓存不可观测和潜在模型费用放大。

缓存命中率为 0 不等同于模型在同一 session 内失忆。客户端仍可能在每轮重新发送历史消息；问题在于上游无法复用已经计算过的稳定前缀，每轮需要重新处理不断增长的输入。它会显著影响费用、延迟、稳定性和批次可比性，并可能间接迫使系统进行更频繁的上下文压缩。

## 已确认的技术根因

### 1. 历史 reasoning 被一次性消费

`src/cybergym/agents/anthropic_bridge.py` 原实现通过 `reasoning_cache.pop(tool_call_id)` 恢复上游 thinking/reasoning。该值在第一次恢复后被删除，而 Claude Code 后续请求会继续重放相同历史 tool call。

因此请求前缀会发生以下变化：

```text
请求 N+1：assistant + reasoning_content + tool_call
请求 N+2：assistant + tool_call
```

使用不含真实任务内容的纯合成三轮消息已经稳定复现：相同历史 payload 连续转换两次后，生成的 OpenAI `messages` 不相等。

### 2. Anthropic 缓存语义未映射到 GLM router

Claude Code 的 Anthropic Messages content blocks 可能携带 `cache_control`。当前 bridge 展平文本并转换工具结构，但不会把该字段映射成 router 支持的缓存字段。

是否可以显式开启 GLM prompt cache，仍需 router 管理方确认：

- Chat Completions 是否支持自动 prefix cache；
- 是否支持 `prompt_cache_key` 或自定义 `extra_body`；
- 缓存的最小前缀长度和 TTL；
- cached token 的响应字段和计费口径。

在协议未确认前，不能通过猜测字段修复缓存控制。

### 3. 缓存 usage 没有正确映射

当前 bridge 只映射普通的输入和输出 token，没有读取 OpenAI-compatible usage 中可能存在的 `prompt_tokens_details.cached_tokens`，也没有向 Claude SDK返回对应的 cache-read/cache-creation token。

因此，即使上游发生部分缓存命中，客户端侧也无法正确观察。由于 router/Key 日志同样显示命中率为 0，本次事故暂按缓存未生效进行风险评估。

### 4. reasoning cache 缺少 session 隔离

bridge 进程内只有一个全局 reasoning cache，所有任务和 session 共用，索引只使用 tool call ID。ID 复用或全局淘汰可能改变历史重建结果，并存在跨会话串扰风险。

### 5. tool ID 存在不稳定边界

- Anthropic 历史 tool block 缺少 ID 时，bridge 使用随机 UUID；相同 payload 被再次转换时可能生成不同前缀。
- 流式上游 tool call 缺少 ID 时，对外发送的 fallback ID 与 reasoning cache 使用的 ID 可能不一致。

正常请求通常应包含 tool ID，但 bridge 不应使用非确定性行为掩盖畸形输入。

### 6. 主动 session 轮换曾缩短可复用前缀

Claude Code 路线设置了 session turn budget。原实现达到边界后启动新 session，并用结构化 working memory 替代完整旧对话，形成明确的缓存边界；finalization 阶段也会启动新的 query。事故修复已改为使用 SDK 返回的 `session_id` 恢复同一会话，working memory 作为新消息附加，阶段 turn budget 仍然保留。

## 额外模型费用来源

### 工具结果总结模型

工具结果总结 Agent 默认复用当前 GLM 模型和同一个 router/Key，会产生独立的模型输入与输出费用。该费用需要与主 Agent 的缓存失效费用分别核算，不能混为同一根因。

### 工具结果阈值曾经过低

GLM profile 曾使用 8192 字符工具结果阈值，导致普通源码读取更容易进入模型总结或首尾裁剪。该参数已调整为 20000，使不超过 `read_file` 16000 字符硬上限的正常读取保持连续。该问题主要影响上下文质量和额外总结调用，不是前缀变化的根因。

## 影响评估

### 稳定性

- 长上下文每轮重复 prefill，首 token 延迟持续增长；
- 无法利用已计算的历史前缀降低重复计算量。

### 实验有效性

- 缓存为 0 本身不代表同一 session 丢失历史；
- 不稳定的历史重建可能改变模型实际接收的上下文；
- 早期工具结果压缩可能破坏连续源码阅读；
- 不同缓存条件下的批次成本和延迟不可直接比较；
- 事故期间结果在重新验证前不应直接作为正式结论。

### 费用

主要费用预计来自主 GLM Agent和独立的 GLM 工具结果总结 API 调用。本地容器与验证服务属于次要基础设施成本。

实际金额必须以 router 账单为准，需要汇总：

- 未缓存输入 token；
- 缓存输入 token；
- 输出 token；
- 主 Agent 与总结 Agent 请求数；
- summary 模型请求数；
- 失败请求是否计费；
- 各类 token 单价。

若长任务后期有 80%～95% 的输入属于可复用历史，并假设缓存 token 价格比普通输入低 90%，仅缓存未生效就可能使输入费用相对理想缓存状态增加约 3.6～6.9 倍。工具结果总结费用应在此之外单独统计。该区间是条件估算，不是本次事故的确定账单金额。

## 已采取的处置

- 暂停所有 CyberGym 实验；
- 将 GLM 工具结果上限从 8192 调整为可覆盖的默认 20000；
- 对 bridge、Claude Code session、缓存 usage 和测试覆盖进行专项代码审计；
- 使用纯合成消息复现历史前缀不稳定；
- 开始按根因逐项修复，实验在验收完成前保持暂停。

## 修复计划

1. 将历史 reasoning 恢复改成非破坏性操作，保证重复转换相同 payload 得到相同 messages。
2. 增加连续三轮以上的字节级前缀稳定回归测试。
3. 统一流式 tool call 的 effective ID，并消除随机 fallback 对重复转换稳定性的影响。
4. 将 reasoning cache 按 session 隔离，增加明确的 TTL/LRU 生命周期。
5. 同时支持流式与非流式 cached-token usage 映射。
6. 向 router 管理方确认 GLM Chat Completions 的缓存协议，再实现显式缓存字段转换。
7. 评估 session turn budget 与 resume 机制，明确跨 session 的缓存和记忆边界。

## 恢复实验的必要条件

以下条件全部满足前，不恢复批量正式实验：

- 合成三轮前缀稳定测试通过；
- 流式和非流式 tool ID 测试通过；
- 多 session reasoning 隔离测试通过；
- cached-token usage 映射测试通过；
- router 缓存协议得到明确确认；
- 单任务能够观察到稳定运行和可解释的缓存指标；
- Key 已设置费用上限、速率限制和告警；
- 单任务 token 与费用经过人工核对；
- 明确批准后才恢复正式实验。

## 责任与流程改进

本次事故暴露的是实验启动前缺少长上下文成本验收和缓存稳定性测试，而不是单一供应商错误。后续应把以下检查作为发布门禁：

- 长上下文累计 token 模拟；
- 三轮以上请求前缀一致性；
- 缓存命中与 usage 对账；
- 失败请求计费确认；
- 单任务成本上限；
- 批次总预算和自动熔断；
- summary Agent 的独立成本核算；
- 实验开始前的人工批准记录。

## 当前结论

事故的首要代码根因是 bridge 使用一次性 `pop()` 恢复历史 reasoning，导致已经存在的历史消息在后续请求中发生变化。缓存控制和 usage 映射缺失、全局 reasoning cache、随机 tool ID 与 session 轮换共同构成本次缓存事故；工具结果总结属于独立模型费用来源。

当前不能把问题简单归因于“OpenAI Chat Completions 不能缓存”或“Key 被限制”。Chat Completions 是否能够缓存取决于 GLM router 的实现，而当前 bridge 必须先保证历史前缀稳定并正确实现可观测性。
