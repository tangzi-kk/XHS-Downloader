# XHS-Downloader · 飞书同步增强版

一个面向个人内容归档与飞书多维表格工作流的小红书素材下载工具，由 `tangzi-kk` 持续维护。

本项目基于 [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) 二次开发，保留原项目的小红书作品解析与下载能力，并增加飞书图片上传、视频任务队列、失败重试和附件回写能力。

## 主要能力

- 解析小红书作品信息和媒体下载地址。
- 下载图片、视频及相关素材。
- 将图片上传到飞书多维表格。
- 将一篇笔记中的多个真实视频拆成独立持久化任务。
- 单 Worker 串行下载视频、截取封面并立即回写飞书。
- 支持服务重启恢复、超时重试、过期链接刷新和大视频分片上传。

## 快速开始

要求 Python 3.12 或更高版本，并确保系统已安装 ffmpeg。

```bash
git clone https://github.com/tangzi-kk/XHS-Downloader.git
cd XHS-Downloader
python -m pip install -r requirements.txt
```

启动原有命令行程序：

```bash
python main.py
```

启动 API：

```bash
python main.py api
```

启动飞书视频 Worker：

```bash
python main.py video-worker
```

## 飞书视频队列

飞书自动化继续调用：

```text
POST /feishu_upload_video_bundle
```

API 只负责持久化入队，不等待视频下载。独立 Worker 永远一次处理一个视频，单条失败不会阻塞后续任务。完整表结构、环境变量和 Render 部署步骤见 [飞书视频任务队列部署说明](docs/video-queue-setup.md)。

## 环境变量

```text
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_BITABLE_APP_TOKEN
FEISHU_BITABLE_TABLE_ID
FEISHU_VIDEO_TASK_TABLE_ID
```

部署生产环境前，请不要把 Cookie、Token、App Secret 或其他凭据提交到仓库。

## 开发与验证

```bash
python -m unittest discover -s tests -v
python -m py_compile source/application/app.py
python -m py_compile source/application/video_worker.py
python -m py_compile main.py
```

## 上游与许可

感谢原项目作者及贡献者提供的基础能力。本分支的修改继续遵循仓库中的 [GNU General Public License v3.0](LICENSE)。分发修改版本时请保留许可证、版权信息和上游归属说明。

本项目仅用于合法的个人数据备份、学习和技术研究。使用者应遵守目标平台服务条款、著作权规则以及所在地法律法规，并自行承担使用责任。
