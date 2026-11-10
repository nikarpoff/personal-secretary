#!/usr/bin/env sh
# Шаги CI/CD на целевом сервере. Запускает GitHub Actions по SSH из каталога деплоя:
#   scripts/deploy.sh deliver   - deploy.env от CI, вход в GHCR (токен в stdin), загрузка образов
#   scripts/deploy.sh up        - пересоздание контейнеров, настройка OpenClaw (онбординг при первом запуске, плагин,
#                                 патч, имя агента) при остановленном шлюзе, ожидание healthy
# deploy.env хранит образы, имена контейнеров, порты и проект compose; .env - секреты приложения
set -eu
cd "$(dirname "$0")/.."

# Образы, которые собирает CI; Basic Memory и Syncthing - на версиях, закреплённых в compose.yml
IMAGE_KEYS="PERSONAL_ASSISTANT_IMAGE OPENCLAW_RUNTIME_IMAGE"
HEALTH_ATTEMPTS=100
HEALTH_DELAY=3

load_env() {
  set -a
  . ./deploy.env
  set +a
}

wait_healthy() {
  container=$1
  status=missing
  attempt=0
  while [ "$attempt" -lt "$HEALTH_ATTEMPTS" ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)
    case "$status" in
      healthy)
        echo "$container: healthy"
        return 0
        ;;
      unhealthy) break ;;
    esac
    attempt=$((attempt + 1))
    sleep "$HEALTH_DELAY"
  done
  echo "$container: $status" >&2
  docker logs --tail 50 "$container" >&2 || true
  exit 1
}

deliver() {
  if [ ! -f deploy.env.new ]; then
    echo "Нет deploy.env.new: его загружает CI перед этим шагом" >&2
    exit 1
  fi
  # Запуск «только конфиги» не знает тег образа: остаётся тот, что уже развёрнут
  for key in $IMAGE_KEYS; do
    if ! grep -q "^$key=." deploy.env.new && [ -f deploy.env ]; then
      grep "^$key=." deploy.env | tail -n 1 >>deploy.env.new || true
    fi
  done
  mv deploy.env.new deploy.env

  if [ ! -f .env ]; then
    echo "В $(pwd) нет .env: создайте его по .env.example или задайте секрет окружения DOTENV" >&2
    exit 1
  fi
  load_env
  if [ ! -f secrets/google.json ]; then
    echo "::warning::Нет secrets/google.json: календарь не заработает, задайте секрет GOOGLE_CREDENTIALS"
  fi

  if [ -n "${REGISTRY_USER:-}" ]; then
    docker login ghcr.io -u "$REGISTRY_USER" --password-stdin >/dev/null
    trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
  fi
  # Только свои образы: pull всего проекта молча обновил бы сторонние с тегом latest
  docker compose pull personal-assistant
  if [ -n "${OPENCLAW_RUNTIME_IMAGE:-}" ]; then
    docker pull "$OPENCLAW_RUNTIME_IMAGE"
  fi
}

up() {
  load_env
  # Остальные каталоги data/ создаёт и отдаёт uid 1000 контейнер data-init
  mkdir -p secrets data/assistant

  # Шлюз поднимается последним: его конфиг меняется, пока он остановлен. В работающий шлюз
  # команды идут через exec, и перезапуск шлюза обрывал бы их (exit 137)
  docker compose stop openclaw-gateway
  # Пересоздаём: data-init заново кладёт личность агента из openclaw/workspace
  docker compose up -d --no-build --force-recreate data-init personal-assistant basic-memory syncthing
  wait_healthy "${PERSONAL_ASSISTANT_CONTAINER:-personal-assistant}"

  configure_openclaw
  docker compose up -d --no-build --force-recreate openclaw-gateway
  wait_healthy "${OPENCLAW_GATEWAY_CONTAINER:-openclaw-gateway}"
  declare_automations
}

configure_openclaw() {
  # Без онбординга шлюз при старте сам пишет конфиг без gateway.mode и потом не запускается
  if ! sh scripts/openclaw.sh config get gateway.mode >/dev/null 2>&1; then
    echo "OpenClaw не настроен: онбординг"
    sh scripts/openclaw.sh onboard --non-interactive --accept-risk \
      --mode local --no-install-daemon --skip-bootstrap --skip-channels --skip-skills --skip-search --skip-health --skip-ui \
      --auth-choice openrouter-api-key --secret-input-mode ref \
      --gateway-auth token --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN
  fi
  if ! sh scripts/openclaw.sh plugins list 2>/dev/null | grep -q duckduckgo; then
    sh scripts/openclaw.sh plugins install @openclaw/duckduckgo-plugin
  fi
  sh scripts/openclaw.sh config patch --stdin <openclaw/openclaw.patch.json5
  sh scripts/openclaw.sh agents set-identity --agent main --workspace /home/node/.openclaw/workspace --from-identity
}

# Автоматизации живут в состоянии шлюза, а не в конфиге: объявляются в работающем шлюзе.
# Тот же --declaration-key обновляет задачу, а не создаёт копию
declare_automations() {
  sh scripts/openclaw.sh automations add --declaration-key morning-briefing --name "Утренняя сводка" \
    --cron "0 8 * * *" --tz "$(env_value TIMEZONE)" --session isolated \
    --message "Утренняя сводка по навыку secretary" \
    --announce --channel telegram --to "$(env_value TELEGRAM_CHAT_ID)" >/dev/null
}

env_value() {
  sed -n "s/^$1=//p" .env | tail -n 1
}

case "${1:-}" in
  deliver) deliver ;;
  up) up ;;
  *)
    echo "Использование: scripts/deploy.sh deliver|up" >&2
    exit 2
    ;;
esac
