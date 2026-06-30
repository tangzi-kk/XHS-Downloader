#!/usr/bin/env bash
set -euo pipefail

PLIST="${HOME}/Library/LaunchAgents/com.xhs-downloader.video-worker.plist"

launchctl bootout "gui/$(id -u)" "${PLIST}" >/dev/null 2>&1 || true
rm -f "${PLIST}"
echo "Mac mini 视频 Worker launchd 服务已移除。"
