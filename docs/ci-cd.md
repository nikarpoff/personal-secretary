# CI/CD

GitHub Actions (`.github/workflows/ci-cd.yml`) проверяет и собирает ветку `dev`, а релизный тег
выкатывает последний билд `dev` в прод и публикует релиз.

## Как работает

| Событие | Стадии | Теги образов |
|---|---|---|
| Пуш в `dev` | test → build | `dev`, `sha-<коммит>`, `build-<N>` |
| Пуш в `main` | - | - |
| Тег `X.Y.Z-rcN` | delivery → deploy → release (pre-release) | `X.Y.Z-rcN` |
| Тег `X.Y.Z` | delivery → deploy → release | `X.Y.Z`, `latest` |

1. **test** запускает `uv run pytest` на раннере GitHub. Сборка начинается, только если тесты прошли,
   поэтому тег `dev` всегда указывает на проверенный билд.
2. **build** собирает `personal-assistant` и `obsidian-webdav` и публикует их в GHCR:
   `ghcr.io/nikarpoff/personal-secretary/<образ>`.
3. **delivery** на релизном теге ничего не собирает. Он вешает версию на текущий образ `dev`
   и предупреждает, если тот собран не из коммита тега. Затем копирует в `DEPLOY_PATH`
   `compose.yml`, `scripts/`, `openclaw/`, `deploy/` и `.env.example`, пишет `deploy.env`
   (образы, имена контейнеров, порты, проект compose), входит в GHCR токеном запуска и скачивает
   только свои образы. OpenClaw и Caddy остаются на версиях из `.env` и `compose.yml`.
4. **deploy** выполняет `docker compose up -d --force-recreate` и ждёт `healthy` у backend.
   Если OpenClaw уже настроен (`data/openclaw/openclaw.json`), deploy применяет
   `openclaw/openclaw.patch.json5` и ждёт шлюз.
5. **release** ставит `latest` на образы финальной версии (кандидатам не ставит) и создаёт
   GitHub Release. В заметки идут текст аннотированного тега, список образов и сгенерированный
   список изменений. К релизу прикладывается архив с файлами для развёртывания.

Версия вешается один раз. Если образ с тегом версии уже есть в GHCR, delivery выкатывает его
как есть. Поэтому Re-run запуска тега повторяет деплой той же версии, а Re-run запуска старого
тега откатывает на неё.

## Настройка

**Пакеты.** Репозиторий публичный, поэтому образы в GHCR публичные и бесплатные. Первый
пуш создаёт пакеты и связывает их с репозиторием через метку `org.opencontainers.image.source`.
Если пакет оказался приватным, откройте его страницу и выберите Package settings →
Change visibility. Сервер всё равно входит в GHCR токеном запуска, поэтому приватные
пакеты тоже скачаются.

**Окружение.** Создайте окружение `production` (Settings → Environments). Стоит включить
Required reviewers и ограничить окружение тегами.

Переменные окружения (Variables):

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DEPLOY_HOST` | Адрес сервера | обязательна |
| `DEPLOY_PORT` | Порт SSH | `22` |
| `DEPLOY_USER` | Пользователь в группе `docker` | обязательна |
| `DEPLOY_PATH` | Каталог деплоя: абсолютный или от домашнего каталога | обязательна |
| `COMPOSE_PROJECT_NAME` | Проект compose | имя каталога |
| `COMPOSE_PROFILES` | Профили, например `obsidian-sync` | пусто |
| `PERSONAL_ASSISTANT_CONTAINER` | Контейнер backend | `personal-assistant` |
| `OPENCLAW_INIT_CONTAINER` | Разовый контейнер подготовки OpenClaw | `openclaw-init` |
| `OPENCLAW_GATEWAY_CONTAINER` | Шлюз OpenClaw | `openclaw-gateway` |
| `OBSIDIAN_WEBDAV_CONTAINER` | WebDAV | `obsidian-webdav` |
| `OBSIDIAN_HTTPS_CONTAINER` | Caddy перед WebDAV | `obsidian-https` |
| `PERSONAL_ASSISTANT_PORT` | Порт backend на `127.0.0.1` | `8090` |
| `OPENCLAW_UI_PORT` | Порт Control UI на `127.0.0.1` | `18789` |

Секреты окружения (Secrets):

| Секрет | Назначение |
|---|---|
| `SSH_PRIVATE_KEY` | Ключ деплоя, публичная часть лежит в `authorized_keys` пользователя |
| `SSH_KNOWN_HOSTS` | Вывод `ssh-keyscan -p <порт> <хост>`; без него ключ сервера не проверяется |
| `DOTENV` | Необязательно: весь `.env` приложения. Без него на сервере остаётся свой `.env` |

Если на сервере уже работает стек, запущенный вручную, задайте другие `COMPOSE_PROJECT_NAME`,
имена контейнеров и порты или сначала остановите его. Два шлюза с одним токеном Telegram
перехватывают друг у друга сообщения.

## Первый деплой

1. Подготовьте сервер: Docker Compose 2.24+, пользователь деплоя в группе `docker`,
   `secrets/google.json` в `DEPLOY_PATH` (см. README).
2. Запушьте `dev`, дождитесь сборки и поставьте релизный тег. Delivery остановится, если на сервере нет `.env` и не задан `DOTENV`.
3. Deploy поднимет backend и предупредит, что OpenClaw не настроен. Выполните на сервере
   onboard и установку плагина из README (`scripts/openclaw.sh ...` в `DEPLOY_PATH`), затем
   сделайте Re-run запуска тега. Дальше патч конфига применяется сам.

`scripts/openclaw.sh` читает `deploy.env`, поэтому на сервере команды OpenClaw работают с
проектом и контейнерами из CI. Сам `deploy.env` правит только CI: следующий деплой его
перезапишет.

## Релиз

```bash
# версия в pyproject.toml и FastAPI(version=...) совпадает с тегом
git tag -a 1.1.0 -m "Голосовые заметки и поиск по людям"
git push origin 1.1.0
```

Ставьте тег после того, как сборка нужного коммита в `dev` закончилась: в релиз уходит
последний билд `dev`. Если тег не совпадает с версией в `pyproject.toml`, пайплайн выдаёт
предупреждение.
