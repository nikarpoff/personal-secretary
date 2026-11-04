#!/usr/bin/env sh
# Команда OpenClaw в контейнере шлюза: в работающем - через exec,
# до первого запуска (onboard, config patch) - разовым контейнером без зависимостей.
#   scripts/openclaw.sh mcp probe personal-assistant
#   scripts/openclaw.sh config patch --stdin < openclaw/openclaw.patch.json5
set -e
cd "$(dirname "$0")/.."

# После деплоя из CI проект compose, образы и имена контейнеров лежат в deploy.env
if [ -f deploy.env ]; then
  set -a
  . ./deploy.env
  set +a
fi

cli_entry=dist/index.js
if [ "${1:-}" = "config" ] && [ "${2:-}" = "patch" ]; then
  for argument in "$@"; do
    if [ "$argument" = "--stdin" ]; then
      cli_entry=/opt/personal-assistant/render-config.mjs
    fi
  done
fi

if [ -n "$(docker compose ps --status running -q openclaw-gateway)" ]; then
  exec docker compose exec -T openclaw-gateway node "$cli_entry" "$@"
fi

# Каталог состояния должен принадлежать node (uid 1000), иначе EACCES.
# stdin закрыт: иначе контейнер заберёт ввод, предназначенный команде (config patch --stdin)
if ! docker compose run --rm --no-deps -T openclaw-init </dev/null >/dev/null 2>&1; then
  echo "Не удалось отдать data/openclaw пользователю node: docker compose run --rm openclaw-init" >&2
  exit 1
fi
exec docker compose run --rm --no-deps -T --entrypoint node openclaw-gateway "$cli_entry" "$@"
