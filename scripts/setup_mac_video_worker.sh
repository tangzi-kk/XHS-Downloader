#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env.video-worker"
VENV_DIR="${PROJECT_DIR}/.venv-video-worker"
PLIST="${HOME}/Library/LaunchAgents/com.xhs-downloader.video-worker.plist"
LOG_DIR="${HOME}/Library/Logs/XHS-Downloader"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "缺少 ${ENV_FILE}。请先创建它并填写 FEISHU_* 与 VIDEO_WORKER_GITHUB_TOKEN。"
  exit 2
fi

mkdir -p "${LOG_DIR}" "$(dirname "${PLIST}")"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${PROJECT_DIR}/requirements.txt" curl-cffi

cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.xhs-downloader.video-worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "${PROJECT_DIR}" &amp;&amp; set -a &amp;&amp; source "${ENV_FILE}" &amp;&amp; set +a &amp;&amp; exec "${VENV_DIR}/bin/python" scripts/run_local_video_worker.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/video-worker.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/video-worker.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl kickstart -k "gui/$(id -u)/com.xhs-downloader.video-worker"
launchctl print "gui/$(id -u)/com.xhs-downloader.video-worker" >/dev/null

echo "Mac mini 视频 Worker 已安装并启动。"
echo "plist=${PLIST}"
echo "stdout=${LOG_DIR}/video-worker.out.log"
echo "stderr=${LOG_DIR}/video-worker.err.log"
