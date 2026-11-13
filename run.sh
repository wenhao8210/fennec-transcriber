#!/bin/bash
# Wennec 转写服务控制脚本
#
#   ./run.sh         后台启动本地服务，浏览器访问 http://127.0.0.1:8778
#   ./run.sh start   仅后台启动服务，不打开界面
#   ./run.sh stop    停止服务
#   ./run.sh status  查看服务状态
#   ./run.sh app     只打开 App 界面（服务已在别处启动时用）
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

APP="$(cd "$(dirname "$0")" && pwd)/Wennec.app"
PORT=8778

alive() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/sessions"; }

start_server() {
  if alive; then echo "服务已在运行 http://127.0.0.1:$PORT"; return 0; fi
  echo "启动中..."
  cd backend
  nohup ../venv/bin/uvicorn main:app --port $PORT --host 127.0.0.1 > /tmp/fennec-server.log 2>&1 &
  disown
  cd ..
  for i in $(seq 1 30); do
    sleep 1
    if alive; then echo "✅ 启动成功 http://127.0.0.1:$PORT"; return 0; fi
  done
  echo "❌ 启动失败，查看日志: /tmp/fennec-server.log"; return 1
}

stop_server() {
  pkill -f "uvicorn main:app" 2>/dev/null
  pkill -f "Contents/MacOS/Wennec" 2>/dev/null
  echo "已停止服务"
}

case "${1:-start}" in
  app)
    open -a "$APP"
    ;;
  stop)
    stop_server
    ;;
  status)
    if alive; then
      echo "运行中 http://127.0.0.1:$PORT"
      pgrep -f "Contents/MacOS/Wennec" >/dev/null && echo "Wennec.app 已打开" || echo "Wennec.app 未打开"
    else
      echo "未运行"
    fi
    ;;
  start)
    start_server
    ;;
  *)  # 默认：打开 App，由 App 自己托管服务
    if [ -d "$APP" ]; then
      open -a "$APP"
      echo "已打开 Wennec.app（服务由 App 托管，退出 App 即停止服务）"
    else
      echo "未找到 $APP，回退为后台启动服务"
      start_server && open "http://127.0.0.1:$PORT"
    fi
    ;;
esac
