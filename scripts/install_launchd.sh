#!/usr/bin/env bash
# 一键把 job-agent daily 注册为 macOS launchd 每日定时任务（默认每天 08:30）。
# 用法:
#   scripts/install_launchd.sh               # 安装/刷新并加载
#   scripts/install_launchd.sh --kickstart   # 安装后立即触发一次（部署验证）
# 卸载: launchctl bootout gui/$(id -u)/com.jobagent.daily
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
LABEL="com.jobagent.daily"
PLIST_SRC="$REPO_ROOT/deploy/com.jobagent.daily.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_N="$(id -u)"

mkdir -p "$REPO_ROOT/logs" "$HOME/Library/LaunchAgents"

sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" -e "s|__UV_BIN__|$UV_BIN|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# 先卸旧再装载，保证幂等
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$PLIST_DST"

echo "已安装并加载: $PLIST_DST"
launchctl print "gui/$UID_N/$LABEL" | grep -E "program |run interval|state" || true

if [[ "${1:-}" == "--kickstart" ]]; then
    echo "立即触发一次（部署验证）..."
    launchctl kickstart "gui/$UID_N/$LABEL"
    echo "触发完成，稍后查看日志: tail -f $REPO_ROOT/logs/launchd.log"
fi
