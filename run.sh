#!/bin/bash
# Wennec 转写服务控制脚本
#
#   ./run.sh         打开 Wennec.app（推荐：原生窗口 + 菜单栏常驻，关窗不退出）
#   ./run.sh start   仅后台启动服务，不打开界面
#   ./run.sh stop    停止服务
#   ./run.sh status  查看服务状态
#   ./run.sh app     只打开 App 界面（服务已在别处启动时用）
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
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
  python3 - "$APP" "$(pwd)" <<'PY_STOP'
import json, os, signal, subprocess, sys, time, urllib.request
app, root = sys.argv[1:]

def app_pids():
    rows = subprocess.check_output(['/bin/ps', '-axo', 'pid=,comm='], text=True).splitlines()
    return [int(parts[0]) for row in rows if len(parts := row.strip().split(None, 1)) == 2
            and os.path.realpath(parts[1]) == os.path.realpath(app + '/Contents/MacOS/Wennec')]

def backend_pids():
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:8778', '-sTCP:LISTEN', '-t'], capture_output=True, text=True)
    found = []
    for raw in result.stdout.split():
        pid = int(raw)
        cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', raw, '-d', 'cwd', '-Fn'], capture_output=True, text=True).stdout
        command = subprocess.run(['/bin/ps', '-p', raw, '-o', 'command='], capture_output=True, text=True).stdout
        if 'n' + root + '/backend' in cwd.splitlines() and 'uvicorn' in command and 'main:app' in command:
            found.append(pid)
    return found

services = backend_pids()
if services:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8778/api/health', timeout=4) as response:
            health = json.load(response)
        if health.get('service') != 'fennec-transcriber' or (health.get('active_transcriptions', 1) or health.get('active_recording_jobs', 0)):
            sys.exit('服务正忙，未停止；请等转写完成后重试。')
    except Exception:
        sys.exit('无法确认服务空闲，未停止；请在桌面 App 中完成当前任务后退出。')
for pid in app_pids():
    os.kill(pid, signal.SIGTERM)
deadline = time.monotonic() + 20
while app_pids() and time.monotonic() < deadline:
    time.sleep(0.2)
if app_pids():
    sys.exit('App 仍在保存录音，未强制终止；请稍后重试。')
for pid in backend_pids():
    # App 退出封存录音后可能刚启动自动本地转写，需要再次确认。
    try:
        with urllib.request.urlopen('http://127.0.0.1:8778/api/health', timeout=4) as response:
            health = json.load(response)
        if health.get('service') != 'fennec-transcriber' or (health.get('active_transcriptions', 1) or health.get('active_recording_jobs', 0)):
            sys.exit('App 已退出，新转写仍在后台运行；未停止后端。')
    except Exception:
        sys.exit('App 已退出，无法确认后端空闲；已保留后端。')
    os.kill(pid, signal.SIGTERM)
print('已请求本项目 App 和空闲服务正常退出。')
PY_STOP
}

case "${1:-app}" in
  app)
    open -a "$APP"
    ;;
  stop)
    stop_server
    ;;
  status)
    if alive; then
      echo "运行中 http://127.0.0.1:$PORT"
      /bin/ps -axo comm= | /usr/bin/awk -v app="$APP/Contents/MacOS/Wennec" '$0 == app {found=1} END {exit !found}' && echo "Wennec.app 已打开" || echo "Wennec.app 未打开"
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
      echo "已打开 Wennec.app（空闲服务随 App 退出；转写任务可继续运行）"
    else
      echo "未找到 $APP，回退为后台启动服务"
      start_server && open "http://127.0.0.1:$PORT"
    fi
    ;;
esac
