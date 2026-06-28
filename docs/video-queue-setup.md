# 飞书视频任务队列部署说明

## 架构

飞书自动化调用 `POST /feishu_upload_video_bundle` 后，API 只拆分视频链接、写入任务表并返回 `queued`。独立 Worker 使用 `python main.py video-worker` 串行处理任务：下载一个视频、生成封面、上传附件、立即汇总父素材记录，然后再领取下一条。

第一版必须把 Web Service 和 Background Worker 都固定为 **1 个实例**，并关闭自动横向扩容。API 进程会串行执行幂等查询与建任务，Worker 进程也有全局互斥锁，任何时刻最多处理一个视频。飞书多维表格不提供唯一键约束或原子抢锁，因此部署多个 API/Worker 实例会破坏严格幂等或全局单并发，此部署限制属于生产正确性要求。

## 新建「视频任务队列」表

在与主素材表相同的多维表格应用中新增一张表，将其表 ID 配置为 `FEISHU_VIDEO_TASK_TABLE_ID`。URL 字段推荐使用单行文本，兼容性最好。

| 字段名称 | 字段类型 |
| --- | --- |
| 任务键 | 单行文本 |
| 父素材记录ID | 单行文本 |
| 原始笔记链接 | 单行文本 |
| 视频序号 | 数字 |
| 视频直链 | 单行文本 |
| 状态 | 单选 |
| 重试次数 | 数字 |
| 下次重试时间 | 日期时间 |
| 锁定时间 | 日期时间 |
| 最后错误 | 多行文本 |
| 视频文件Token | 单行文本 |
| 封面文件Token | 单行文本 |
| 创建时间 | 创建时间 |

「状态」单选必须包含：

- 待处理
- 处理中
- 待重试
- 成功
- 待人工刷新

## 主素材表字段

新增：

| 字段名称 | 字段类型 |
| --- | --- |
| 视频处理状态 | 单选 |
| 视频处理进度 | 单行文本 |
| 视频失败详情 | 多行文本 |

「视频处理状态」应包含：`处理中`、`完成`、`部分完成`、`视频待处理`。

保留原字段：`原视频`、`视频封面`、`视频链接`。`原视频`和`视频封面`必须为附件字段。

## 飞书自动化请求

接口路径保持不变：`POST /feishu_upload_video_bundle`。

```json
{
  "video_url": "当前记录的视频链接字段",
  "record_id": "当前飞书记录 ID",
  "cover_field": "视频封面",
  "video_field": "原视频",
  "note_url": "原始小红书笔记链接，可选"
}
```

旧自动化可以不传 `note_url`。`video_url` 支持换行、`%0A`、字符串数组；每条 URL 会创建一个独立任务。接口不会下载视频或调用 ffmpeg。

## Render 部署

原 Web Service 保持：

```bash
python main.py api
```

新增一个 Render Background Worker，使用同一仓库、同一套环境变量，实例数固定为 1，启动命令：

```bash
python main.py video-worker
```

两个服务都必须配置：

```text
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_BITABLE_APP_TOKEN
FEISHU_BITABLE_TABLE_ID
FEISHU_VIDEO_TASK_TABLE_ID
```

可选配置：

```text
VIDEO_TASK_POLL_SECONDS=10
VIDEO_TASK_STALE_SECONDS=900
MAX_VIDEO_UPLOAD_BYTES=
VIDEO_DOWNLOAD_MAX_SECONDS=600
```

`MAX_VIDEO_UPLOAD_BYTES` 留空或 `0` 表示不设置额外运维上限。超过飞书单请求 20MB 的视频会自动使用官方分片上传；若设置了此变量，超限任务会保留并进入可见失败/重试状态，不会静默丢弃。Dockerfile 无需修改，继续使用其中的 ffmpeg。

## 重试与恢复

- 第 1 次失败：5 分钟后重试。
- 第 2 次失败：20 分钟后重试。
- 第 3 次失败：60 分钟后重试。
- 第 4 次及以后：6 小时后重试。
- 401/403 且提供 `note_url` 时，Worker 尝试重新解析同序号视频；连续 4 次仍不能刷新则转为「待人工刷新」。
- 「处理中」超过 15 分钟会自动恢复为「待重试」。
- 任一任务失败不会阻塞后续任务。

## 权限检查

飞书应用需要多维表格记录读写权限，以及云空间素材上传权限。发布前请用测试记录验证任务表创建、父表附件写回和大于 20MB 视频的分片上传。
