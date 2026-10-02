#!/bin/zsh
# Устанавливает службу macOS: сервер Search Agent запускается, когда открывают страницу поиска
# (обращение к 127.0.0.1:8000), и сам завершается, когда страницу закрыли.
# Удаление: scripts/uninstall-autostart.sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.vadimdesign.search-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$ROOT/.venv/bin/python"

[[ -x "$PY" ]] || { echo "Нет $PY — сначала: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "Порт 8000 занят другим процессом. Остановите его (например, сервер, запущенный вручную) и повторите."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>app</string><string>--on-demand</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>Sockets</key>
  <dict>
    <key>Listeners</key>
    <dict>
      <key>SockNodeName</key><string>127.0.0.1</string>
      <key>SockServiceName</key><string>8000</string>
    </dict>
  </dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/search-agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/search-agent.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Готово. Сервер запустится, когда вы откроете index.html или http://127.0.0.1:8000, и остановится, когда закроете."
echo "Журнал: ~/Library/Logs/search-agent.log"
