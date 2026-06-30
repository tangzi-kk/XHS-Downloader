#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="${HOME}/Library/LaunchAgents/com.xhs-downloader.video-worker.plist"
LOG_DIR="${HOME}/Library/Logs/XHS-Downloader"

echo "project=${PROJECT_DIR}"
echo "plist_exists=$([[ -f "${PLIST}" ]] && echo yes || echo no)"
echo "env_exists=$([[ -f "${PROJECT_DIR}/.env.video-worker" ]] && echo yes || echo no)"
echo "python=$([[ -x "${PROJECT_DIR}/.venv-video-worker/bin/python" ]] && echo "${PROJECT_DIR}/.venv-video-worker/bin/python" || command -v python3)"
launchctl print "gui/$(id -u)/com.xhs-downloader.video-worker" 2>/dev/null | sed -n '1,80p' || true
echo "--- stdout tail ---"
tail -n 80 "${LOG_DIR}/video-worker.out.log" 2>/dev/null || true
echo "--- stderr tail ---"
tail -n 80 "${LOG_DIR}/video-worker.err.log" 2>/dev/null || true
