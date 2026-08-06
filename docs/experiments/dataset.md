# CyberGym 数据与运行镜像准备

## 2026-07-30：准备开始

本轮仅准备评测所需数据和 Docker runner，不启动任何模型或评测。下载统一使用 `https://hf-mirror.com`，并通过 `NO_PROXY/no_proxy` 强制使该镜像域名不经过代理。数据会落在仓库内，便于断点续传、校验和复用。

计划先盘点官方脚本和已有缓存，再下载完整 benchmark 数据、验证服务数据及 runner 镜像；每完成一个阶段会在本文档登记实际位置、命令和校验结果。

## 镜像站与代理策略

已验证下面的镜像 API 请求返回 HTTP 200，且请求使用了直连：

```bash
export HF_ENDPOINT=https://hf-mirror.com
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}hf-mirror.com,.hf-mirror.com"
export no_proxy="$NO_PROXY"
```

所有本轮 Hugging Face 下载都会显式继承这三个变量。若镜像站不可用，下载器保留断点；后续会先尝试镜像的标准 resolve 路径，再按文件级别切换到 Hugging Face 直连，而不会重新下载已完成文件。

## 已有内容

- `cybergym_data/`：约 1.8 GiB，包含此前 subset 的数据缓存，作为完整数据集的续传目标。
- `.binary-eval-download/server-data/cybergym-server-data.7z`：约 7.9 GiB 的历史未完成二进制验证包；保留，待完整 benchmark 数据完成后再续传和校验。
- 可用磁盘：约 6.1 TiB，足以容纳完整 benchmark、验证包与 Docker runner。

## 正在启动：完整 benchmark 数据

目标数据集为 `sunblaze-ucb/cybergym`，固定到已检查的公开 revision `bde190ded494e52bc684b66073b436c9d992c7c6`。完整快照下载目标为仓库内的 `cybergym_data/`，会复用此前 subset，不会覆盖已完成文件。后台日志为 `.runs/prepare/full-dataset.log`。

实际下载已切换为受控终端会话（避免本环境回收脱离终端的后台子进程）。下载器仍是 Hugging Face 的可恢复 `snapshot_download`，终端意外中断时可用下列命令安全续传：

```bash
cd /root/cybergym
HF_ENDPOINT=https://hf-mirror.com \
NO_PROXY="${NO_PROXY:+$NO_PROXY,}hf-mirror.com,.hf-mirror.com" \
no_proxy="$NO_PROXY" HF_HUB_DISABLE_XET=1 \
.venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download(repo_id="sunblaze-ucb/cybergym", repo_type="dataset", revision="bde190ded494e52bc684b66073b436c9d992c7c6", local_dir="cybergym_data", max_workers=4)'
```

首次 4 并发请求在约 682 MiB 后遭遇镜像站 TLS EOF；这不是数据集缺失，Hugging Face 的本地缓存已经保留完成的块。为提高长传输稳定性，当前续传改为单连接、120 秒下载超时；若再次失败，会继续从同一目录重试而非删除缓存。

## Docker runner 准备

Docker 的完整 runner 清单来自下载完成后的 `cybergym_data/tasks.json`。官方脚本已经支持按任务 ID 拉取：

```bash
cd /root/cybergym
.venv/bin/python scripts/data/server/download.py \
  --tasks-file ./cybergym_data/tasks.json --max-workers 1
```

这会顺序准备 `n132/arvo:<id>-vul/fix`、`cybergym/oss-fuzz:<id>-vul/fix`，并拉取 `cybergym/oss-fuzz-base-runner:latest`。顺序拉取能复用层，也不会因大量并发请求导致 registry 限流。完整任务数据下载期间，先预拉取 binary-only 模式还需要的三个基础版本：`20200102`、`20190802`、`20220102`；其余任务 runner 在主数据下载完成后启动，以优先保障 Hugging Face 数据吞吐。

Docker 本地层缓存由 Docker 管理；可用镜像可通过：

```bash
docker image ls 'n132/arvo' 'cybergym/oss-fuzz*'
```

当前进度（本次启动后）：完整数据目录已从约 1.8 GiB 增至约 2.5 GiB，已经有 97 个任务数据文件落盘并可被后续续传复用。基础镜像中 `latest` 和 `20200102` 已就绪；其余两个版本仍在串行拉取。尚未启动任何模型或评测。

后续进度更新：数据目录已达到约 11 GiB、730 个任务数据文件；镜像站在单连接持续传输约 8.8 GiB 后再次返回 TLS EOF。已下载文件和不完整块仍在本地缓存，基础 runner 四个版本（`latest`、`20200102`、`20190802`、`20220102`）已全部可用。下载器将切换到自动重试循环；每次失败均从 `cybergym_data/` 续传。
## 30 分钟监督

`scripts/monitoring/monitor_full_dataset.sh` 是当前下载监督器：它以镜像直连、单连接和 120 秒超时运行下载；传输异常后等待 20 秒自动断点续传；每 30 分钟将数据目录大小、已落盘文件数和 worker PID 写入 `.runs/prepare/dataset-monitor.log`。它不启动模型或评测。

实时采样（30 秒）：下载目录与下载进程写入均增加约 40.5 MB，即约 1.29 MB/s；worker 仍存活，目录为约 12 GiB、791 个数据文件。维持当前镜像、直连和单连接设置。

后续监督检查：30 分钟心跳已在 16:26 UTC 正常写入；当前约 14 GiB、955 个数据文件。10 秒空闲采样后复测的 30 秒写入为 20 MiB（约 0.67 MiB/s），并观察到新的 Hugging Face 不完整块正在增长，确认下载未断开，不需要人工恢复。

代理诊断：镜像站的 LFS 文件会重定向至 `cas-bridge.xethub.hf.co` 和 `us.aws.cdn.hf.co`。原本仅豁免 `hf-mirror.com`，使重定向后的大文件仍通过本地 Clash（实测远端为 `127.0.0.1`）。监督器现将下载进程设为 `NO_PROXY=*` / `no_proxy=*`，确保整条镜像到 CDN 链路直连；镜像源和断点目录不变。

监督器也已升级为每分钟记录 `bytes_per_minute`。若连续五分钟低于 256 KiB/min，则判定为卡死，终止当前 worker 并自动从缓存续传；每 30 分钟继续记录完整心跳。

下载实现说明：当前使用的 `snapshot_download(repo_id="sunblaze-ucb/cybergym", HF_ENDPOINT=https://hf-mirror.com)` 下载的就是完整 `cybergym` 数据集；它不是额外数据。镜像 Git 端点可用，但 Git LFS 与当前下载器最终都会把大文件重定向到 Hugging Face/Xet CDN。因此改用 `git clone https://hf-mirror.com/datasets/sunblaze-ucb/cybergym` 不会绕过 CDN、不会提高已下载文件的速度，并会放弃当前 `cybergym_data/` 的精确续传状态。镜像下载和 CDN 下载均已强制直连，不需要访问 GitHub 或通过 Clash。
