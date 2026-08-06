# API 模型运行

本项目可通过 OpenAI-compatible Chat Completions API 运行 CyberGym，不启动本地模型。Key 只从环境变量读取，绝不写入 profile、运行配置或轨迹。

## Kimi K3 smoke subset

```bash
cd /root/cybergym
export CYBERGYM_KIMI_API_KEY='your-key'
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/kimi-k3.env \
  scripts/manifests/api_smoke_tasks.txt \
  kimi-k3-api-smoke-r1
```

运行产物统一放在 `outputs/kimi-k3-api-smoke-r1/`，每个任务位于其中的
`tasks/<task>/`，批次日志和验证服务数据分别位于 `logs/`、`server/`：

- `config.json`：模型、URL、Key 环境变量名和运行参数，不含 Key。
- `trajectory.jsonl`：`model_request` 是精确发给模型的 messages/tools；`model` 是 API 返回的文本/tool calls；`tool` 是工具实际结果，也是下一轮模型所见的信息。
- `summary.json`、`verification.json`：完成状态及 CyberGym 的 PoC 验证结果。

## 快速切换模型

复制或新建 `scripts/profiles/<name>.env`，只修改非敏感字段：

```bash
API_BASE_URL="https://provider.example/v1"
API_MODEL="provider/model-name"
API_KEY_ENV="CYBERGYM_OTHER_API_KEY"
API_MODE="chat_completions"
```

随后设置对应 Key 并运行不同的 batch：

```bash
export CYBERGYM_OTHER_API_KEY='your-key'
bash scripts/evaluation/run_api_subset.sh \
  scripts/profiles/<name>.env \
  scripts/manifests/api_smoke_tasks.txt \
  other-api-smoke-r1
```

不同 API 是否需要 `/v1` 取决于其 SDK 示例；应使用传给 `OpenAI(base_url=...)` 的原样 URL。不同模型使用不同的 `BATCH_NAME`，结果互不覆盖。

## 工具输出

工具结果不再有 16,000 字符限制。读取单个超过 1 MiB 的文件仍会被拒绝，防止二进制或超大文件塞进模型上下文；普通命令输出、读取内容和验证响应均完整记录。
