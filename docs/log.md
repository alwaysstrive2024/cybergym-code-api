# 变更日志

本文件记录相对上游基线的重要功能、行为和工程流程变化，不记录逐行代码细节。

维护约定：从本日志建立起，每次修改代码、脚本、配置或文档后都必须立即追加对应日志，使用精确到秒的 UTC 时间标题（`YYYY-MM-DDTHH:MM:SSZ`）；同一秒内完成且属于同一主题的连续调整可以合并描述，并注明验证情况。历史上没有可靠秒级记录的内容必须明确标为“补记”，不得伪造原修改时间。

## 2026-08-06

#### 2026-08-06T02:55:58Z

- 提交前复核自 `d8f8b88` 以来的全部工作树改动和本日志，确认缓存前缀稳定、session resume、确定性 ID、session 隔离、cached-token usage、实时监控及上下文工具链均有对应记录，profile 与文档未包含真实 API Key。
- 根据后续采用 binary server data 的部署决定，移除按随机整批任务预拉取完整 `vul/fix` 镜像的临时方案；保留运行期 HTTP 5xx/传输错误快速标记为验证基础设施失败的保护，避免模型重复提交消耗 token。
- 验证：项目 `tests/` 73 项及 1 个 subtest 通过；Agent/评测/数据脚本 Ruff、批量入口与全部 profile 的 shell 语法、`git diff --check` 均通过。仅有既有 FastAPI/httpx 弃用警告。

## 2026-08-05

基线：`origin/feature/claude-code-agent-entry`

### Agent 上下文与会话稳定性

#### 2026-08-05T16:17:24Z

- 防止运行期验证基础设施故障被误记为普通无效 PoC：`submit_poc` 遇到 HTTP 5xx 或 httpx 传输错误时记录结构化 `infrastructure_failure`，Agent 不再进入下一 session 或 finalization 重试，summary 使用 `verification_infrastructure_failure` 终止原因并返回失败状态。
- 确认 v4 的 500 均来自完整验证镜像缺失后访问 Docker Hub 时的 TLS handshake timeout、EOF 或 auth token header timeout；不再提交按整批任务预拉取完整镜像的临时方案，后续由 binary server data 路线解决验证数据准备。
- 验证：Ruff、Python compileall、shell 语法、git diff whitespace 及 500 分类针对性断言通过。当前环境未安装 pytest。

#### 2026-08-05T13:46:13Z

- 调整复杂源码任务的单次读取粒度：`read_file` 默认范围由 160 行提高到 240 行，字符硬上限由 16000 提高到 20000，仍保留 400 行硬上限；工具描述不再要求每次都做 narrow read。
- 将策略门控和提示中的带假设窄读阈值由 80 行统一提高到 200 行，并同步 context/metrics 对缺省读取范围的统计口径，避免只改提示文字而实际策略仍按旧阈值执行。
- 验证：Ruff 检查、Python compileall、git diff whitespace 检查及默认参数/策略阈值针对性断言通过；当前环境未安装 pytest，因此未执行 pytest 风格测试集。

#### 2026-08-05T09:29:36Z（历史补记，非原修改时间）

- 验证：项目自身测试持续随实现扩充；最终数量和检查结果见本节末尾的收尾记录。全程未启动实验、未读取具体实验数据。仓库级无路径限制的 pytest 会误收集 `examples/agents/` 内多个独立上游项目并因其未安装依赖失败，因此正式验证限定为本项目 `tests/`。
- 开始按 `possible_improve.md` 分阶段升级工具结果管线：每次工具调用分别保存不可变的 `tool-results/raw/` 与确定性清洗后的 `tool-results/processed/`，轨迹记录模型实际使用的视图、字符数、产物路径和清洗统计；许可证等噪声不再需要等到超长截断后才另存。
- 优先升级 Claude Code CLI/SDK 路线的长期记忆：增加统一的 `update_investigation_state` 工具，按上限记录目标、输入路径、崩溃证据、文件相关性与重开条件、调用边、受控值、未知项和主/备假设，同时落盘机器可读 `investigation-state.json` 并继续生成人类可读工作记忆。
- 为 Claude Code 的精确重复源码读取增加软门控；已完整读取的相同范围会返回此前文件结论和重开条件，只有新假设、显式重开、不同范围、上次截断或文件已被修改时才重新返回源码。
- 扩展确定性降噪规则：目录列表默认跳过 VCS、缓存、`node_modules` 等无关树；源码仅隐藏可明确识别的文件头许可证/版权或自动生成声明并保留原始行号；命令结果将 Sanitizer 类型、栈帧及相邻上下文提升到结果开头，防止关键崩溃证据落在长日志中部而被截断。
- 改进 Claude 工具层的目录选择：默认裁掉常见构建产物、编辑器目录和依赖缓存，并在固定扫描上限内优先展示 README、fuzz/harness、crash/Sanitizer 线索和构建入口；截断提示会明确结果经过优先级筛选，减少大源码树的无效展开。
- 增加清洗、审计产物、结构化记忆和重复读取门控的回归测试，覆盖许可证与自动生成头、函数内注释保留、原始行号、长日志中部 Sanitizer 栈、raw/processed 一致性、文件修改后的缓存失效及状态字段校验。
- 更新 Agent 运行指南，明确 raw/processed 的审计语义、结构化状态文件、许可证与静态数据清洗边界、重复读取放行条件，以及模型实际接收的是哪一层结果，避免后续排查把清洗结果误认为原始源码。
- 在 Claude Code 路线验证后，将 LangGraph 的实际执行绑定到同一套共享 `TaskSandbox`/`ToolExecutor`，同步开放结构化状态与带假设的源码读取参数，使许可证清洗、raw/processed 审计、目录降噪、重读门控和工作记忆不再因后端不同而漂移。
- 让命令清洗器感知实际命令类型：成功且无 Sanitizer/错误的超长构建日志压缩为退出状态、唯一 warning 与尾部目标摘要；大型 `rg` 结果增加按文件计数的证据索引。原始输出仍完整保留，失败构建和崩溃诊断不会套用成功摘要。
- 收紧重读索引的内存边界和路径一致性：`./file`、`file` 与 `/workspace/file` 归一为同一目标，旧读取明细会随固定长度队列一起淘汰，避免长任务中门控缓存自身持续增长。
- 谨慎清理源码顶部的长 Doxygen 文件概述：仅对含 `@file`、长度达到阈值且不含参数、返回值、边界、所有权、安全条件或 TODO/FIXME 的纯说明块折叠；许可证块若混有任何高价值安全注释则整块保留，避免为了删除版权文字误删漏洞语义。
- 支持清理恰好被读取边界截断的标准许可证头：只有片段位于文件开头、包含明确许可证关键词且每一行仍保持块注释形态时才省略，覆盖 LGPL 正文尚未读到结尾 `*/` 的情况。
- 调整工作记忆超限策略：按目标、崩溃证据、调用链、受控值、当前假设、checkpoint/验证回执、文件状态、未知项和历史索引的优先级逐行装入固定预算，不再用头尾字符截断切断中间证据或半行 JSON/Markdown。
- 根据多 agent 安全复核收紧静态表折叠：只处理无注释、无宏、无指针、无字符串、无 designated initializer、无预处理指令的纯数字字面量表，并保留开头/结尾各三行样本及原始行号；表达式表、状态机、函数指针和任何带注释数据全部原样返回。
- 为结构化记忆的路径和调用边增加硬边界：拒绝 `..` 与 `/workspace` 外绝对路径，统一路径别名，并分别限制 caller、callee 和 evidence 长度，保证条目数量有界之外单条内容也不能撑爆 `investigation-state.json`。
- 扩大 Sanitizer 优先证据窗口：除错误类型、项目栈帧、读写大小和分配位置外，还保留每个关键命中后的有限上下文，使输入文件、对象容量等紧邻说明不会留在长日志中部被截掉。
- 扩展许可证识别到脚本、汇编和 SQL 常见的 `#`、`;`、`--` 行注释，并允许保留 shebang/编码声明后再折叠许可证；仍只处理文件顶部连续且含强许可证关键词的片段。
- 强化工具结果审计链：raw/processed 产物使用独占创建避免静默覆盖，trajectory 同时记录两者的 SHA-256；后续可直接验证模型视图对应哪份原始输出及清洗后内容。
- 对齐 LangGraph 与 Claude Code 的工具参数边界，并在共享 runtime 再做独立校验：读取行号/行数/字符数、目录数量和命令超时均拒绝非正值，结构化状态数组与主要文本字段增加长度上限，避免依赖不同 provider 是否严格执行 JSON Schema。
- 实现选择性的结构化证据摘要层：仅当确定性清洗后仍超限，且结果可被可靠识别为 Sanitizer/runtime 错误或大型 `rg` 搜索时，生成 `tool-results/summaries/*.json` 并让模型接收该摘要；短结果和无法安全提证的源码不触发摘要，后者继续使用有标记截断。trajectory 记录 summary 路径、类型、字符数和 SHA-256，raw/processed 仍完整保留。
- 为复杂超长源码/命令结果增加可选独立总结 Agent 接口；只有确定性处理后仍超限才调用，并强制校验 `artifact_type`、带 `location` 的 `proven_facts`、`uncertainties` 和 8K 总上限。总结器异常、自由文本或无定位事实会自动回退到 processed 有界视图，并在 trajectory 记录回退类型，不影响工具调用。
- 物理删除 LangGraph 入口中约 350 行已失效的 Sandbox/ToolExecutor 复制实现及专用 helper/import，直接导入共享 runtime；历史由 Git 保留，不再把旧审计语义作为“参考代码”留在生产入口，消除未来误改或重新绕回旧路径的风险。
- 增加共享探索策略与指标状态：统一统计工具/读取次数、唯一范围、重复读取拦截、模型可见源码字符、首次候选写入/提交、无效提交、假设修订和停滞步数；支持 `baseline`（仅采集）、`guided`（默认软提示）和 `enforced`（阻止无假设的宽泛浏览）三种模式，并为首次提交、读取量和假设停滞设置有界阈值。
- 将最终阶段的工具限制下沉到共享 executor：Claude Code 不再只靠 prompt 要求“仅提交”，进入 finalization 后实际只允许 `submit_poc`；LangGraph 同步标记相同 phase。Claude 的 `summary.json` 追加兼容性的 `metrics` 字段，不改变既有 status、termination 或 submissions 语义。
- 为两条评测入口增加统一策略参数：`--policy-mode baseline|guided|enforced`、读取次数/源码字符预算、停滞工具阈值和首次提交工具截止点；配置随 run config 持久化，默认 guided 只提示不改变工具结果语义，需显式 enforced 才执行浏览门控。
- 新增不读取工具正文的 trajectory 指标聚合器，并标准化 `submission_outcome` 事件；可统计有效/任意提交、首次提交步、无效提交数、重复读取率、raw/processed/model-visible 字符量、确定性压缩量、上下文超限、策略提示/拦截和运行时长，为固定配置的 A/B 对照提供统一口径。
- 增加自动证据入账：源码处理元数据记录最多 30 个带原始行号的函数定义签名；Sanitizer/runtime 结构化证据中的错误类型、栈和读写/对象信息自动进入 crash evidence，无需 Agent 再复制整段日志到 checkpoint。
- 提供可直接启用的独立总结 Agent 适配器：支持 Anthropic Messages HTTP 与 OpenAI-compatible Chat Completions，固定温度、JSON-only 证据协议和 48K 输入硬上限；模型输出仍须经过 runtime 的 location/字段/大小校验，不能绕过确定性清洗或审计产物。
- 两条入口增加可选 `--tool-summary-model`：Claude Code 使用同一官方/bridge Anthropic-compatible 端点，LangGraph 使用现有 OpenAI-compatible client；默认不启用以避免短结果和普通运行增加模型成本，只有显式配置且确定性处理后仍超限时才调用。
- 增加 `scripts/evaluation/summarize_trajectory_metrics.py`，可对多个 trajectory 输出同口径 JSON 指标而不读取 raw 工具正文或实验数据，便于后续固定模型、seed、轮次和验证服务进行 baseline/guided/enforced A/B 对比。
- 批量 API/Claude Code 入口透传 `AGENT_POLICY_MODE`、读取/字符/停滞/首次提交阈值及可选 `AGENT_TOOL_SUMMARY_MODEL`，并在启动前校验模式和正整数预算；批量运行不再只能依赖单任务入口的硬编码默认值。
- 加固策略与总结边界：畸形 `max_lines` 不再能让读取门控抛出未捕获异常或伪装成窄读取；独立总结结果必须至少包含一条带位置的事实或明确 uncertainty，空 JSON 即使格式合法也会回退。

#### 2026-08-05T09:29:59Z

- 修正本日志的维护格式：此前本轮修改虽逐项追加，但只有日期标题、没有秒级时间。由于真实历史秒数无法可靠反推，上述内容统一明确标为当前时间的“历史补记”；从本条开始，每批修改完成后立即建立独立 UTC 秒级标题。

#### 2026-08-05T09:30:33Z

- 完成本轮非实验回归：项目 `tests/` 59 项通过（另有 1 个 subtest），Agent 核心模块与 trajectory 指标脚本通过 Ruff，批量入口通过 `bash -n`，`git diff --check` 通过。仅保留现有 FastAPI/httpx 弃用警告；未读取训练数据、`outputs/`、真实 trajectory 内容或 prompt 设置，也未启动任何实验。

#### 2026-08-05T09:31:01Z

- 撤销 Ruff 对 `anthropic_bridge.py` 造成的无关空行格式变化，使本轮差异严格限定在上下文、清洗、策略、指标及其入口接线范围内。

#### 2026-08-05T09:38:47Z

- 为 `scripts/profiles/` 下全部 9 个 `.env` profile 写入统一推荐值和逐参数注释：默认 `guided`、18 次读取、120000 源码字符、6 次假设停滞和第 12 次工具调用首次提交提醒。总结 Agent 默认使用当前主模型；可在 source profile 前导出同一 endpoint 上的其他模型 ID 覆盖，显式导出空字符串则关闭模型总结。

#### 2026-08-05T09:40:35Z

- 验证全部 profile 均通过 `bash -n`；在隔离 shell 中逐个 source 后，推荐策略值和“总结模型默认跟随当前模型”均正确生效，预先导出 `AGENT_TOOL_SUMMARY_MODEL=''` 也能在全部 profile 中可靠关闭模型总结；`git diff --check` 通过。

#### 2026-08-05T09:42:21Z

- 将 `scripts/profiles/` 全部 `.env` 中的说明注释统一改为中文，并明确总结 Agent 默认复用主模型的 endpoint 与认证 key，不需要单独密钥；额外模型只能填写同一 endpoint/provider 可访问的模型 ID，空字符串继续表示关闭模型总结。

#### 2026-08-05（既有基线记录，建立日志时未保留秒级时间）

- 修复了 Claude Code 的总轮次限制与单个 session 轮次限制混淆的问题。单个 session 到达上限后会保留工作记忆并继续下一 session，直到总预算耗尽或完成提交。
- 修复了 Claude Code SDK 将正常的 session 轮次边界当作任务失败的问题，并完善了轮次耗尽、取消和异常中断时的结果状态记录。
- 改进 LangGraph 的上下文压缩策略：为工具描述和模型输出预留空间，只保留连续的近期交互，去除重复记忆和系统消息，并对已记录证据去重。
- 增加上下文超限恢复能力。仅在确认属于上下文长度错误时缩减历史或重置响应链后重试，其他请求错误仍按原异常处理。
- 增加可选的 `top_p` 省略能力，以兼容不允许同时传入部分采样参数的模型服务。

### 批量评测与资源管理

- 增加多任务并行评测能力，可配置并发数、总内存预算和单任务预留内存；内存压力过高时会等待，而不是继续启动任务。
- 统一 API 批量评测入口，复现与子集入口复用同一个 supervisor，减少不同运行方式之间的行为偏差。
- 增加固定数量随机抽样并持久化任务清单的能力，重试时复用同一批任务；同时保留运行完整清单的模式。
- 改进批次中断清理流程，优先让评测进程释放 Docker 沙箱，再停止辅助服务，降低孤儿进程和残留容器风险。
- 增加网关连续限流/失败的熔断保护，并让协议桥正确透传限流语义，避免无效重试持续消耗资源。
- 完善失败任务记录、批次汇总、结果对比、运行监控和资源保护工具。

### 任务数据与沙箱

- 优化大型任务归档的装载方式：准备任务时不再重复复制大型压缩包，而是在创建 Docker 工作区时按需加入归档，降低磁盘占用和准备开销。
- 为归档暂存增加路径与文件类型校验，并在装载后清理临时清单，保证原始数据保持只读且不会泄露宿主机路径到任务工作区。

### 模型、数据与实验支持

- 增加多种本地及远端模型的启动脚本和非敏感 profile，覆盖 vLLM、Transformers 8-bit 以及不同量化/服务配置。
- 增加全量、官方新增子集、source subset 和模型专项任务清单，以及相应的下载、运行和结果对比入口。
- 补充实验策略、模型配置、数据准备、运行记录和错误时间线文档，便于复现实验与审计历史结果。

### 项目结构与文档

- 按评测、服务、监控、数据、任务清单和模型配置重新整理 `scripts/`，并保留兼容入口，降低脚本散落和路径误用风险。
- 更新 README 中的数据下载和结果验证路径，新增脚本索引与项目结构说明。
- 增加针对上下文压缩、Claude Code 多 session 行为和大型任务归档装载的回归测试。
#### 2026-08-05T09:44:03Z

- 将 `scripts/profiles/claude-sonnet-litellm.env` 和 `scripts/profiles/glm-52.env` 中最后两处英文行尾说明改为中文；未改动参数值。
#### 2026-08-05T09:44:51Z

- 完成全部 `scripts/profiles/*.env` 的 `bash -n` 语法检查，并确认遗留英文说明已清除；`git diff --check` 通过。检查过程未读取训练数据、输出、轨迹内容或 prompt 设置。
#### 2026-08-05T09:53:53Z

- 新增 `docs/ENV_AND_TERMINAL_PARAMETERS.md`，集中说明 profile、API Key、模型、上下文策略、总结模型和批量启动参数，并提供可直接复制的 GLM terminal 示例及常见运行变体。未读取训练数据、输出、轨迹内容或 prompt 设置。
#### 2026-08-05T09:55:06Z

- 校验 `docs/ENV_AND_TERMINAL_PARAMETERS.md` 的关键章节与启动命令引用；确认示例使用的 GLM profile 和 smoke 任务清单均存在，`git diff --check` 通过。未启动实验。
#### 2026-08-05T10:11:20Z

- 应用户要求停止正在运行的 `glm52_guided_50` 实验进程组；先发送正常终止信号，再仅对同一已确认进程组清理残留进程，最终存活进程数为 0。未删除已有实验文件。
- 将 `scripts/profiles/glm-52.env` 的 `API_MAX_TOOL_RESULT_CHARS` 从硬编码 `8192` 改为可覆盖的默认值 `20000`。该值高于 `read_file` 的 `16000` 字符硬上限，可避免普通源码读取触发模型总结或首尾截断，同时保留超长命令结果的总结机制。
- 同步更新 `docs/ENV_AND_TERMINAL_PARAMETERS.md`，说明 GLM 默认值、连续源码读取原因及终端覆盖方式。
#### 2026-08-05T10:11:47Z

- 验证 GLM profile 与批量启动脚本的 shell 语法；确认 profile 展开后的工具结果上限为 `20000`、策略为 `guided`，API Key 环境变量已配置，`git diff --check` 通过。未读取或修改实验数据、输出、轨迹内容或 prompt 设置。
#### 2026-08-05T10:12:13Z

- 使用新批次名 `glm52_guided_50_continuous` 启动 GLM 正式 50 任务实验，避免与旧批次的 `8192` 配置结果混合；启动进程 PID/PGID 为 `1076046`。保持 `guided`，总结模型继续默认复用当前 GLM 模型。

#### 2026-08-05T10:12:23Z

- 启动后进行只读进程核验：新批次进程组有 37 个存活进程，17 个 evaluator 均使用 `--max-tool-result-chars 20000` 与 `--policy-mode guided`。未查看实验输出、轨迹内容、训练数据或 prompt 设置。
#### 2026-08-05T10:38:32Z

- 将 `scripts/profiles/glm-52.env` 的默认实验并发数 `EVAL_CONCURRENCY` 从 `17` 调整为 `1`，用于降低 GLM router 的持续请求压力。
#### 2026-08-05T11:19:37Z

- 修复 Anthropic bridge 对历史 `reasoning_content` 的一次性消费：将 `reasoning_cache.pop()` 改为非破坏性读取，确保同一历史 tool call 在连续请求中的 OpenAI `messages` 前缀保持稳定；补充重复转换回归测试。
#### 2026-08-05T11:22:02Z

- 为 Anthropic bridge 增加确定性 tool-call ID：缺失 ID 时根据稳定的消息位置、工具名和参数生成哈希 ID，避免相同请求重试时 UUID 改变前缀；流式响应统一使用同一个 effective ID 对外发送并索引 reasoning cache。补充重试稳定性与流式 ID 一致性测试。
#### 2026-08-05T11:23:18Z

- 为 Anthropic bridge 的 reasoning cache 增加会话命名空间：使用稳定 system/首条消息的不可逆哈希与 tool-call ID 组合索引，不保存 prompt 正文，避免并发任务使用相同 tool ID 时互相覆盖 reasoning。补充跨会话隔离测试。
#### 2026-08-05T11:24:11Z

- 为 Anthropic bridge 增加统一 cached-token usage 映射：读取上游 `prompt_tokens_details.cached_tokens`，在流式和非流式响应中返回 `cache_read_input_tokens`，并从普通 `input_tokens` 中扣除缓存部分以避免重复计数；补充两条 usage 回归测试。
#### 2026-08-05T11:26:09Z

- 修改 Claude Code 分阶段执行：后续 exploration 和 finalization query 使用 SDK 返回的 `session_id` 恢复同一会话，不再每 24 turn 丢弃完整历史并创建无关联 session；保留阶段 turn budget，并补充 resume 传递测试。
#### 2026-08-05T11:27:21Z

- 为 Claude Code bridge 增加可配置的 `API_PROMPT_CACHE_KEY_MODE=off|stable`；`stable` 使用 session 起始内容的不可逆哈希生成并转发 OpenAI `prompt_cache_key`。GLM profile 默认启用 `stable`，批量脚本增加参数校验与 bridge 传递，补充稳定 Key 回归测试及参数文档。
#### 2026-08-05T11:28:15Z

- 修复 Anthropic bridge 的上游异常分类：明确保留 429 和上游 5xx，超时映射为 504，连接/未知网关错误才映射为 502；补充状态分类回归测试，避免限流、超时和服务端故障被统一误报为 502。
#### 2026-08-05T11:31:02Z

- 修正 `anthropic_bridge.py` 的 import 区块格式，使缓存修复代码通过 Ruff import 检查。
#### 2026-08-05T11:31:46Z

- 将 bridge reasoning cache 的 4096 条淘汰上限从全局改为按 session 独立计算，避免一个长会话或其他任务淘汰当前会话的历史 reasoning 并再次改变前缀；补充跨 session 淘汰隔离测试。
#### 2026-08-05T11:55:21Z

- 为每个 Claude Code 任务显式生成一次 UUID session ID，同时绑定到 `ClaudeAgentOptions.session_id` 并通过 `X-Cybergym-Session-ID` 传给 bridge；bridge 校验 UUID 后使用不可逆哈希作为 reasoning namespace 与稳定 `prompt_cache_key`，不再依赖 tool ID 或 prompt 内容区分会话。补充会话隔离测试。
#### 2026-08-05T11:56:17Z

- 为 Anthropic bridge 增加无正文的实时 prompt-cache 监控：逐会话比较累计消息哈希与 tools 哈希，报告上一请求是否为当前前缀、公共前缀消息数、prompt/cached token 和命中率；新增 `/cache-status` 状态端点与流式/非流式 usage 更新。批量脚本支持固定 `CYBERGYM_CLAUDE_BRIDGE_PORT`，参数文档增加 `watch + curl` 监控示例，并补充前缀稳定/变化测试。
#### 2026-08-05T11:58:29Z

- 加固 bridge reasoning 生命周期：不再淘汰活跃会话的历史 reasoning；检测同一 session/tool ID 被不同 reasoning 复用时 fail-closed，防止静默覆盖旧前缀。缺失上游 tool ID时加入稳定请求哈希作为确定性 ID seed，避免跨响应碰撞；替换淘汰测试并增加冲突与跨请求 ID测试。
#### 2026-08-05T12:10:53Z

- 为 Claude Code bridge 的 OpenAI streaming 请求显式增加 `stream_options.include_usage=true`，确保正式流式路线返回 prompt/cached token usage 并驱动实时缓存监控；补充 gateway 流式 usage 请求测试。
#### 2026-08-05T12:22:09Z

- 修复 Claude Code 分阶段 resume 参数冲突：首次 query 使用显式 `session_id`，后续 resume/finalization 清空 `session_id` 并只传 `resume=<原会话ID>`，避免 CLI 因同时出现 `--session-id` 与 `--resume` 而 exit 1；自定义 session header 仍保持同一 UUID。补充参数互斥回归测试。
