# API Key 方式复现与执行

这份最小交付使用 OpenAI-compatible Chat Completions API 运行 CyberGym。仓库只保存
API 地址、模型名和“读取哪个环境变量”，不会保存 API Key 本身。更换 Key 时无需改代码。

## 1. 环境准备

需要 Python 3.12、`uv`、Docker、`curl` 和 `flock`。在仓库根目录执行：

```bash
uv sync --extra agent --extra server
docker build -t cybergym-langgraph-agent:0.1 docker/langgraph-agent
```

CyberGym dataset 不包含在 Git 仓库中。按项目根目录 `README.md` 下载后，默认应位于：

```text
cybergym_data/data/
```

也可以通过 `CYBERGYM_DATA_DIR` 指向其他本地路径。dataset、运行结果、缓存、数据库和
本地密钥文件均已加入 `.gitignore`。

## 2. 更换 API Key

在当前终端导出同伴自己的新 Key：

```bash
export CYBERGYM_MODEL_API_KEY='替换为新的模型 API Key'
```

不要把真实 Key 写入 `scripts/profiles/api-reproduction.env`、命令脚本、文档或 Git。
profile 中的关键配置是：

```bash
API_KEY_ENV="CYBERGYM_MODEL_API_KEY"
```

运行器通过该名称读取 `os.environ`，并仅在内存中传给
`OpenAI(base_url=..., api_key=...)`。`outputs/*/tasks/*/config.json` 只记录环境变量名，
不记录 Key 值。

如果需要切换 API 服务或模型，只修改 profile 中的 `API_BASE_URL` 和 `API_MODEL`。

## 3. 执行 smoke 测试

```bash
bash scripts/evaluation/run_api_reproduction.sh \
  scripts/profiles/api-reproduction.env \
  scripts/manifests/api_smoke_tasks.txt \
  teammate-smoke-r1
```

脚本会启动本地 CyberGym 验证服务、依次运行任务、验证 PoC，并将结果写入
`outputs/teammate-smoke-r1/`。重复执行同一批次名时，已完成任务会跳过。

若验证服务已在其他位置运行，可设置：

```bash
export CYBERGYM_SERVER_URL='http://127.0.0.1:18666'
```

`CYBERGYM_API_KEY` 是 CyberGym 验证服务自身的管理 Key，与模型服务的
`CYBERGYM_MODEL_API_KEY` 不同。本地模式会为每批自动生成临时管理 Key；连接远程
验证服务时，需要在服务端和运行端设置相同的新值：

```bash
export CYBERGYM_API_KEY='替换为新的验证服务管理 Key'
```

## 4. 结果检查

主要文件如下：

- `accuracy_summary.txt`：批次进度和准确率；
- `failed_tasks.txt`：可直接重跑的失败任务清单；
- `tasks/<task>/config.json`：非敏感运行参数；
- `tasks/<task>/trajectory.jsonl`：模型请求、回复和工具轨迹；
- `tasks/<task>/verification.json`：最终验证结果。

提交或分享前可确认没有 Key 被跟踪：

```bash
git status --short
git diff --cached --check
git diff --cached --name-only
```
