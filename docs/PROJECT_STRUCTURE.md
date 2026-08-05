# CyberGym 项目结构整理报告

整理日期：2026-08-04

## 整理目标

本次整理把散落在仓库根目录和 `scripts/` 顶层的实验文件按职责归类，降低入口脚本、配置、任务清单和历史记录混在一起造成的误用风险。整理过程中同步更新了脚本内部引用、默认路径和面向用户的运行文档。

历史实验结果和下载数据没有删除或重排，暂停的实验也没有重新启动。

## 整理后的主要结构

```text
cybergym/
├── README.md                 # 上游项目使用说明
├── docs/
│   ├── PROJECT_STRUCTURE.md  # 本报告
│   └── experiments/          # 本地实验设计、数据准备和历史记录
├── scripts/
│   ├── README.md             # 脚本入口索引
│   ├── evaluation/           # 评测、批量运行、验证、统计
│   ├── serving/              # 模型服务与验证服务启动
│   ├── monitoring/           # 下载和资源监控
│   ├── data/                 # 数据下载工具
│   │   └── server/           # Docker runner/server 数据下载
│   ├── manifests/            # 全量、子集和 smoke 任务清单
│   └── profiles/             # API 模型非敏感配置
├── src/cybergym/             # CyberGym Python 包
│   └── agents/                # Agent 公共沙箱运行时与 API 协议适配器
├── examples/                 # 官方 Agent 示例
├── docker/                   # 自定义 Agent 容器文件
├── cybergym_data/            # benchmark 数据（保留原位）
├── outputs/                  # 新 API 批次及整理后的普通日志
└── .runs/                    # 既有历史/本地模型运行产物（保留原位）
```

仓库中的 `.venv*`、`.cache`、`.ruff_cache`、`.binary-eval-download` 和
`cybergym_data_gz` 都是环境、缓存或下载暂存目录，不属于源码结构，因此没有移动。

## `scripts/` 分组说明

### `evaluation/`

- `run_api_subset.sh`：外部 OpenAI-compatible API 的批次入口。
- `run_langgraph_eval.py`：单任务 LangGraph Agent 主程序。
- `run_claude_code_eval.py`：单任务 Claude Code Agent SDK 主程序；仅暴露受控 MCP 工具。
- `run_full_qwen36_official.sh`：官方 Qwen 全量入口。
- `run_source_subset_model.sh`、`run_subset_model.sh`：本地模型批次和单任务 supervisor。
- `verify_agent_result.py`、`verify_and_record.py`：PoC 验证与结果落盘。
- `record_failed_task.py`、`summarize_api_batch.py`：失败任务清单和总体/ARVO/OSS-Fuzz 指标。
- `compare_*.py`：历史运行及 source subset 对比。

### `serving/`

- `start_cybergym_server.sh`：验证服务启动器。
- `serve_vllm_*.sh`：vLLM 服务入口。
- `serve_transformers_8bit.py` 与对应 shell：Transformers 8-bit 备用服务。

### `data/`、`monitoring/`

- `data/download_source_subset_data.py`：下载 source subset。
- `data/server/`：完整、subset、binary-only runner 下载工具。
- `monitoring/`：完整数据集续传和主机内存保护脚本。

### `manifests/`、`profiles/`

- `manifests/all_tasks.txt`：1507 个全量任务。
- 其他 `.txt`：API smoke、source subset、官方新子集和 GPT-OSS 子集。
- `profiles/*.env`：模型 URL、名称、超时和预算等非敏感参数；API key 不写入文件。
- `profiles/deepseek-v4-claude-code.env`：Claude Code runtime + DeepSeek 的实验 profile。

## Agent runtime 选择

`scripts/evaluation/run_api_subset.sh` 是统一批次 supervisor。profile 或命令环境中的
`USE_CLAUDE_CODE_AGENT` 决定单任务入口：

- `false`（默认）：调用 `run_langgraph_eval.py`；
- `true`：调用 `run_claude_code_eval.py`，并为该批次启动一个 loopback 协议桥。

布尔值必须严格写为小写 `true` 或 `false`。Claude Code runner 使用官方 Python Agent
SDK 自带的 Claude Code binary，通过 in-process MCP 注册 `list_files`、`read_file`、
`write_file`、`run_command` 和 `submit_poc`。原生文件与 Bash 工具被移出上下文，MCP
操作统一落到 `src/cybergym/agents/runtime.py` 的无网络 Docker sandbox。

`src/cybergym/agents/anthropic_bridge.py` 把 Claude Code 的 Anthropic Messages/SSE 请求
转换为现有 profile 使用的 OpenAI-compatible Chat Completions 请求。桥只监听本机，
每批生成临时凭证；上游 API key 不传入 Claude Code 子进程。Claude Code 模式默认调用
`/submit-diff`，因此模型同步看到 vulnerable/fixed 结果，验证脚本也查询同一批次服务和
PoC 数据库。

## 主要路径迁移

| 原路径 | 新路径 |
|---|---|
| `scripts/run_api_subset.sh` | `scripts/evaluation/run_api_subset.sh` |
| `scripts/run_langgraph_eval.py` | `scripts/evaluation/run_langgraph_eval.py` |
| `scripts/run_*model.sh` | `scripts/evaluation/run_*model.sh` |
| `scripts/verify*.py` | `scripts/evaluation/verify*.py` |
| `scripts/serve_*` | `scripts/serving/serve_*` |
| `scripts/start_cybergym_server.sh` | `scripts/serving/start_cybergym_server.sh` |
| `scripts/server_data/` | `scripts/data/server/` |
| `scripts/model_profiles/` | `scripts/profiles/` |
| `scripts/all_task.txt` | `scripts/manifests/all_tasks.txt` |
| `scripts/*subset*.txt` | `scripts/manifests/*subset*.txt` |
| 根目录实验 `.md` | `docs/experiments/` |
| 根目录 `compress.log` | `outputs/logs/compress.log` |

`runtime.md` 和 `errortime.md` 是按时间记录的历史审计日志，其中旧命令保持原样，不能视作当前入口；当前命令以根目录 `README.md`、`scripts/README.md` 和本报告为准。

## 代码适配

移动后已同步修改以下行为：

- shell 入口从两级子目录正确定位仓库根目录；
- 评测 supervisor 指向新的 `evaluation/`、`serving/` 路径；
- source subset 的 Python 默认清单切换到 `manifests/source_subset_tasks.txt`；
- README 和可执行实验文档中的下载、验证、profile、manifest 路径已更新；
- API 批次仍使用 `outputs/<batch-name>/` 作为大目录，任务结果统一放在其 `tasks/` 子目录。

## 数据保留与清理范围

- 保留 `.runs/`、`outputs/` 中的所有实验结果、数据库、轨迹和失败任务清单。
- 保留 `cybergym_data/`、Docker 数据和各类下载缓存。
- 仅移除了 `scripts/` 下可自动再生的 Python `__pycache__`。
- 没有启动、恢复或继续任何评测实验。

## 当前推荐命令

```bash
cd /root/cybergym
export CYBERGYM_DEEPSEEK_API_KEY='...'
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/deepseek-v4-official-repro.env \
  .runs/deepseekv4-flash-full/failed_tasks.txt \
  deepseek-v4-official-repro-r2
```

这条命令只作为后续运行示例，本次整理没有执行它。批次名必须是单个目录名，结果会写入 `outputs/deepseek-v4-official-repro-r2/`。

## 验证结果

整理完成后执行了不启动实验的静态和入口检查：

- 12 个 shell 文件全部通过 `bash -n`；
- 12 个 Python 文件全部通过 AST 语法解析；
- 10 个关键 Python 入口的 `--help` 均能正常加载；
- `run_api_subset.sh` 的参数校验入口正常；
- `all_tasks.txt` 为 1507 行、1507 个唯一任务；
- 当前脚本和操作型文档中没有遗留旧路径；
- `git diff --check` 通过。

当前环境没有安装 `shellcheck`，因此该项未执行；这不影响上述 shell 语法检查。
