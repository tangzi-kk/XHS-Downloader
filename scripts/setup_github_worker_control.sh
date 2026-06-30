#!/usr/bin/env bash
set -euo pipefail

REPO="${VIDEO_WORKER_GITHUB_REPOSITORY:-tangzi-kk/XHS-Downloader}"

if ! command -v gh >/dev/null 2>&1; then
  echo "缺少 gh CLI，请先安装并登录 GitHub。"
  exit 2
fi

gh variable set MAC_VIDEO_WORKER_HEARTBEAT_MS --repo "${REPO}" --body "0"
gh variable set VIDEO_WORKER_FALLBACK_LOCK_UNTIL_MS --repo "${REPO}" --body "0"
gh variable set VIDEO_WORKER_FALLBACK_LOCK_OWNER --repo "${REPO}" --body ""
echo "GitHub Worker 控制变量已初始化：${REPO}"
