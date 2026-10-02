#!/bin/zsh
# Удаляет службу автозапуска Search Agent.
LABEL="xyz.playstate.search-agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Автозапуск удалён. Сервер можно запускать вручную: .venv/bin/python -m app"
