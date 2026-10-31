#!/usr/bin/env sh
# Команда OpenClaw в контейнере шлюза: в работающем - через exec,
# до первого запуска (onboard, config patch) - разовым контейнером без зависимостей.
#   scripts/openclaw.sh mcp probe personal-assistant
#   scripts/openclaw.sh config patch --stdin < openclaw/openclaw.patch.json5
set -e
cd "$(dirname "$0")/.."

if [ -n "$(docker compose ps --status running -q openclaw-gateway)" ]; then
  exec docker compose exec -T openclaw-gateway node dist/index.js "$@"
fi
exec docker compose run --rm --no-deps -T --entrypoint node openclaw-gateway dist/index.js "$@"
