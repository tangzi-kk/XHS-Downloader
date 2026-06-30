# Mac mini 视频 Worker

Mac mini 是主力视频 Worker，GitHub Actions 只在 Mac mini 超过 10 分钟没有心跳时兜底。

## 本地环境文件

在项目根目录创建 `.env.video-worker`，只保存在 Mac mini 本机，不提交 Git：

```bash
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
FEISHU_VIDEO_TASK_TABLE_ID=
VIDEO_WORKER_GITHUB_REPOSITORY=tangzi-kk/XHS-Downloader
VIDEO_WORKER_GITHUB_TOKEN=
VIDEO_TASK_POLL_SECONDS=5
VIDEO_DISPATCH_INTERVAL_SECONDS=0
VIDEO_WORKER_HEARTBEAT_INTERVAL_SECONDS=60
```

`VIDEO_WORKER_GITHUB_TOKEN` 只用于更新 GitHub Actions Variables 中的心跳与兜底锁。

## 安装

```bash
cd /Users/tangtang/Projects/XHS-Downloader
bash scripts/setup_mac_video_worker.sh
```

## 检查

```bash
cd /Users/tangtang/Projects/XHS-Downloader
bash scripts/doctor_mac_video_worker.sh
```

## 卸载

```bash
cd /Users/tangtang/Projects/XHS-Downloader
bash scripts/uninstall_mac_video_worker.sh
```
