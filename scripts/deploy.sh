#!/usr/bin/env sh
# Шаги CI/CD на целевом сервере. Запускает GitHub Actions по SSH из каталога деплоя:
#   scripts/deploy.sh deliver   - deploy.env от CI, вход в GHCR (токен в stdin), загрузка образов
#   scripts/deploy.sh up        - пересоздание контейнеров, патч конфига OpenClaw, ожидание healthy
# deploy.env хранит образы, имена контейнеров, порты и проект compose; .env - секреты приложения
set -eu
cd "$(dirname "$0")/.."

# Образы, которые собирает CI; OpenClaw и Caddy остаются на версиях из .env и compose.yml
IMAGE_KEYS="PERSONAL_ASSISTANT_IMAGE OBSIDIAN_WEBDAV_IMAGE"
HEALTH_ATTEMPTS=60
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

  services=personal-assistant
  case ",${COMPOSE_PROFILES:-}," in
    *,obsidian-sync,*) services="$services obsidian-webdav" ;;
  esac

  if [ -n "${REGISTRY_USER:-}" ]; then
    docker login ghcr.io -u "$REGISTRY_USER" --password-stdin >/dev/null
    trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
  fi
  # Только свои образы: pull всего проекта молча обновил бы OpenClaw с тегом latest
  # shellcheck disable=SC2086
  docker compose pull $services
}

up() {
  load_env
  # Каталоги томов заранее: отсутствующий Docker создаёт от root
  mkdir -p secrets data/assistant-state data/obsidian/Secretary data/obsidian/Personal
  chmod 700 data/assistant-state

  # Пересоздаём всё: файлы, смонтированные по одному, после обновления указывают на старые копии
  docker compose up -d --no-build --force-recreate
  wait_healthy "${PERSONAL_ASSISTANT_CONTAINER:-personal-assistant}"

  if [ ! -f data/openclaw/openclaw.json ]; then
    echo "::warning::OpenClaw ещё не настроен: выполните onboard из README, затем перезапустите деплой"
    return 0
  fi
  sh scripts/openclaw.sh config patch --stdin <openclaw/openclaw.patch.json5
  wait_healthy "${OPENCLAW_GATEWAY_CONTAINER:-openclaw-gateway}"
}

case "${1:-}" in
  deliver) deliver ;;
  up) up ;;
  *)
    echo "Использование: scripts/deploy.sh deliver|up" >&2
    exit 2
    ;;
esac
