# Personal Assistant

ИИ-секретарь в Telegram: ведёт Google Calendar и напоминает о событиях, понимает голосовые, хранит заметки о людях, задачах и идеях в хранилище Obsidian и ищет в интернете. Разговор ведёт агент [OpenClaw](https://docs.openclaw.ai) на модели из OpenRouter. Календарь и напоминания - этот сервис, заметки - [Basic Memory](https://github.com/basicmachines-co/basic-memory), синхронизация заметок с устройствами - [Syncthing](https://syncthing.net). Агент обращается к сервису и к заметкам по MCP.

```
 Вы ⇄ Telegram-бот ──────────────────────────────┐
                                                  ▼
   OpenClaw gateway ── LLM (OpenRouter), Whisper для голосовых, веб-поиск, навык secretary
          │ MCP                         │ MCP
          ▼                             ▼
   personal-assistant              basic-memory ── data/notes ── syncthing ⇄ Obsidian на устройствах
     │ Google Calendar API
     └ Telegram Bot API: напоминания
```

## Возможности

- **Календарь.** Создание, перенос, удаление, повторяющиеся события и события на весь день. Время агент передаёт в вашем часовом поясе.
- **Напоминания** в Telegram за `REMINDER_MINUTES` до начала или по уведомлениям самого события в Google. Важные события (красный цвет в календаре) - ещё за сутки и за два часа. «Напомни в 15:00 позвонить» - событие с напоминанием в момент начала.
- **Голосовые** распознаёт ваш Whisper, агент получает текст.
- **Заметки** - Markdown-файлы хранилища Obsidian: люди, задачи, идеи, справочные материалы. Поиск по словам и по смыслу.
- **Люди на встречах:** событие хранит ссылки на карточки людей, агент читает их для контекста встречи.
- **Веб-поиск** через DuckDuckGo и чтение страниц.
- **Утренняя сводка** - события дня, задачи на сегодня и важное на неделю.

## Где что настраивается

| Что | Где |
|---|---|
| Токены, ключи, ваш Telegram id, модель, Whisper, эмбеддер | `.env` (в `.gitignore`) |
| Доступ к Google Calendar | `secrets/google.json` |
| Правила OpenClaw: allowlist, MCP-серверы, инструменты, Whisper | `openclaw/openclaw.patch.json5` - ссылки `${VAR}` на `.env`, без секретов |
| Как агент работает с календарём и заметками | `openclaw/skills/secretary/SKILL.md` |
| Имя, характер и правила агента | `openclaw/workspace/*.md` |
| Заметки | `data/notes` |

## Запуск

Нужны сервер с Docker, аккаунт Google, ключ [OpenRouter](https://openrouter.ai/keys) и Whisper с OpenAI-совместимым API.

### 1. Telegram и Google

1. Бот: [@BotFather](https://t.me/BotFather) → `/newbot` → токен. Свой user id подскажет [@userinfobot](https://t.me/userinfobot).
2. [Google Cloud Console](https://console.cloud.google.com/): включите **Google Calendar API**; на **OAuth consent screen** добавьте себя в Test users и нажмите **Publish app**, иначе токен истечёт через 7 дней; создайте **OAuth client ID** типа **Desktop app** и скачайте JSON в `secrets/client_secret.json`.
3. Токен календаря:

```bash
uv sync
uv run scripts/authorize_google.py --client secrets/client_secret.json --out secrets/google.json
```

Без браузера на сервере: `ssh -L 8765:localhost:8765 <сервер>` и тот же скрипт с `--no-browser`.

### 2. Конфигурация

```bash
cp .env.example .env      # заполните все <change_me>
```

### 3. OpenClaw

При деплое из CI `scripts/deploy.sh up` делает всё это сам: онбординг, если конфига ещё нет, плагин поиска, патч, имя агента и утреннюю сводку. Образы backend и OpenClaw собираются в CI и скачиваются из GHCR; на сервере сборка не нужна. Вручную - для запуска без CI.

Команды OpenClaw выполняет `scripts/openclaw.sh`: в работающем шлюзе через `docker compose exec`, до первого запуска - разовым контейнером.

```bash
docker compose build openclaw-gateway
scripts/openclaw.sh onboard --non-interactive --accept-risk \
  --mode local --no-install-daemon --skip-bootstrap --skip-channels --skip-skills --skip-search --skip-health --skip-ui \
  --auth-choice openrouter-api-key --secret-input-mode ref \
  --gateway-auth token --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN
scripts/openclaw.sh plugins install @openclaw/duckduckgo-plugin
scripts/openclaw.sh config patch --stdin < openclaw/openclaw.patch.json5     # Applied N config update(s)
scripts/openclaw.sh agents set-identity --agent main --workspace /home/node/.openclaw/workspace --from-identity

docker compose build personal-assistant
docker compose up -d --no-build
scripts/openclaw.sh mcp probe personal-assistant      # 4 tools
scripts/openclaw.sh mcp probe notes                   # 8 tools
```

Шлюз и `data-init` используют один образ. Локально это `personal-openclaw:2026.10.9-node26.12.1`, собранный по официальному Dockerfile из закреплённого коммита OpenClaw. Для готового образа задайте `OPENCLAW_IMAGE` в `.env` и вместо сборки выполните `docker compose pull openclaw-gateway`.

Ключи OpenRouter и Whisper и токен шлюза остаются в `.env`: онбординг и патч пишут в конфиг OpenClaw только ссылки на них.

### 4. Syncthing

1. `ssh -L 8384:127.0.0.1:8384 <сервер>`, откройте `http://localhost:8384` и задайте логин и пароль интерфейса.
2. Поставьте Syncthing на ноутбук и телефон, добавьте сервер по его ID (**Actions → Show ID**).
3. На сервере **Add Folder**: путь `/var/syncthing/notes`, общий доступ с вашими устройствами. На устройстве примите папку и откройте её в Obsidian как хранилище.

Включите в папке **File Versioning** - старые версии заметок останутся у Syncthing.

### 5. Утренняя сводка

```bash
scripts/openclaw.sh automations add --name "Утренняя сводка" \
  --cron "0 8 * * *" --tz Europe/Moscow --session isolated \
  --message "Утренняя сводка по навыку secretary" \
  --announce --channel telegram --to <TELEGRAM_CHAT_ID>
```

## Заметки

Basic Memory хранит каждую заметку Markdown-файлом с frontmatter, Obsidian открывает их как есть. Тип заметки - поле `type`, у каждого типа своя папка:

| Тип | Папка | Что внутри |
|---|---|---|
| `person` | `people` | Кто человек: `- [role]`, `- [contact]`, `- [context]`; связи `- works_at [[Компания]]`; другие имена в `aliases` |
| `task` | `tasks` | Задача без времени: `status: open/done`, `due: YYYY-MM-DD` |
| `knowledge` | `knowledge` | Справочное: инструкции, факты, ссылки |
| `idea` | `ideas` | Идея и теги |
| `note` | `notes` | Всё остальное |

Агент находит заметки поиском по словам и по смыслу, читает их и ходит по связям `[[...]]`. Удалять заметки агент не может - только переносить в `archive`. Задача с конкретным временем - событие в календаре, а не заметка.

Смысловому поиску нужны эмбеддинги: Basic Memory берёт их по OpenAI-совместимому API (`EMBEDDING_*` в `.env`), локальной модели нет. По умолчанию это OpenRouter с многоязычной `baai/bge-m3`; подойдёт и свой сервис с `/v1/embeddings` - адрес, ключ, модель и размерность вектора. После смены модели или размерности пересоберите индекс: `docker compose exec basic-memory basic-memory reindex --embeddings`.

Участников встречи агент связывает с событием через поле `people`: там ссылки (permalink) на карточки людей. Google хранит их в скрытом свойстве события, приглашения никому не уходят.

## Напоминания

- Сервис читает календарь раз в `CALENDAR_SYNC_INTERVAL` секунд на `CALENDAR_SYNC_DAYS` вперёд и сразу после изменения через агента.
- Минуты берутся из уведомлений события в Google, иначе - `REMINDER_MINUTES`; важным добавляются `IMPORTANT_REMINDER_MINUTES`.
- Напоминание, время которого прошло до создания события, пропускается. После простоя приходит одно - ближайшее к началу.
- Отправленные напоминания отмечаются в `data/assistant`, поэтому после перезапуска не повторяются.
- События на весь день попадают в утреннюю сводку.

## Инструменты агента

| Сервер | Инструменты |
|---|---|
| `personal-assistant` | `list_events`, `create_event`, `update_event`, `delete_event`; у событий есть `people` |
| `notes` (Basic Memory) | `bm_write`, `bm_edit`, `bm_read`, `bm_search`, `bm_find`, `bm_list`, `bm_context`, `bm_move` |

Ошибки календаря («Event not found», «Event must end after it starts») доходят до модели текстом, она исправляет вызов.

## REST API

Те же операции с событиями для отладки, Swagger - `http://127.0.0.1:8090/docs`, авторизация `Authorization: Bearer <AUTH_TOKEN>`.

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/v1/events?start=&end=&q=&limit=` | События периода |
| `POST` | `/v1/events` | Создать событие → 201 |
| `PATCH` | `/v1/events/{id}` | Изменить событие |
| `DELETE` | `/v1/events/{id}` | Удалить событие → 204 |

Время: `2026-11-06T15:00` - локальное в `TIMEZONE`, `2026-11-06` - событие на весь день, у него `end` - последний день включительно. Коды: 400 - неверное время, 401/403 - токен, 404 - нет события, 422 - неверный запрос или Google отклонил событие, 502/503/504 - Google недоступен или не настроен.

## Control UI

```bash
ssh -N -L 18789:127.0.0.1:18789 <сервер>        # локальный порт - ровно 18789
scripts/openclaw.sh dashboard --no-open          # одноразовая ссылка входа
```

Ссылка входит и одобряет браузер как устройство владельца. При входе по `OPENCLAW_GATEWAY_TOKEN` устройство одобряют командами `devices list` и `devices approve <id>`.

## Память OpenClaw

OpenClaw 2026.10.9 собирается на [рекомендованном Node 26](https://github.com/openclaw/openclaw/blob/v2026.10.9/docs/install/node-compatibility.md). Штатный CLI сохраняет кэш байткода в `data/openclaw/compile-cache`, healthcheck использует `curl`. Патч запрещает девять перечисленных плагинов и снимает прежние ограничения остальных функций. Лимитов RAM и параллелизма нет.

Изменения применяет `scripts/deploy.sh up`. Расход RAM проверяют через `docker compose stats --no-stream openclaw-gateway` при одинаковой нагрузке; кэш ускоряет повторные запуски, но не гарантирует снижение постоянного расхода RAM.

## Личность агента

Имя, характер и правила - `openclaw/workspace`. Контейнер `data-init` перед стартом копирует их агенту и убирает ритуал первого запуска `BOOTSTRAP.md`. После правок: `docker compose up -d`, затем `set-identity` из раздела «OpenClaw», и `/new` боту.

## Безопасность

- Бот отвечает только владельцу: `dmPolicy: "allowlist"` с `TELEGRAM_CHAT_ID`.
- У агента нет shell и записи файлов; `read` видит только его рабочий каталог. Текст событий, заметок и страниц для агента - данные, а не команды.
- Basic Memory не умеет авторизацию по HTTP: его порт не публикуется, к нему ходит только OpenClaw по сети compose.
- Порты сервиса, Control UI и интерфейса Syncthing опубликованы только на `127.0.0.1`; наружу открыт лишь 22000 для синхронизации.
- Google-доступ ограничен событиями (`calendar.events`).

## Устройство

```
app/
├── main.py                 настройки → логирование → FastAPI → /health → REST → /mcp
├── api_v1/                 REST (views.py), MCP-инструменты (tools.py), схемы, обработчики ошибок
├── core/
│   ├── engine.py           сборка: клиенты, хранилище отметок, цикл напоминаний
│   ├── events.py           операции с событиями
│   ├── event_times.py      начало и конец события из слов агента
│   ├── scheduler.py        цикл: чтение календаря и отправка напоминаний
│   ├── reminders.py        какие напоминания пора отправить и их текст
│   ├── store.py            SQLite: отметки отправленных напоминаний
│   ├── upstream/           клиенты Google Calendar и Telegram Bot API
│   ├── auth/  config/      Bearer-авторизация, настройки и константы
│   └── entities.py  exceptions.py
└── utils/                  lifespan, логирование, время
openclaw/                   патч конфига, навык secretary, личность агента
scripts/                    authorize_google.py, openclaw.sh
data/                       assistant, notes, basic-memory, syncthing, openclaw
```

## Разработка

```bash
uv sync
uv run pytest -q
uv run fastapi dev app/main.py
```

## Ограничения

- Один календарь и один владелец.
- В Whisper уходят только `model`, `language` и файл: нестандартные параметры сервиса задаются его значениями по умолчанию. Whisper во внутренней сети требует `models.providers.openai.request.allowPrivateNetwork: true` в патче.
- Текст заметок уходит эмбеддеру (OpenRouter или ваш сервис), как и разговоры - модели.
- Исходники OpenClaw (2026.10.9), Node (26.12.1), Basic Memory (1.0.0) и Syncthing (2.1.7) закреплены в `compose.yml`; готовые образы переопределяют `OPENCLAW_IMAGE`, `BASIC_MEMORY_IMAGE`, `SYNCTHING_IMAGE` в `.env`.

## CI/CD

Пуш в `dev` прогоняет тесты и собирает образы backend и OpenClaw в GHCR, релизный тег `X.Y.Z` выкатывает последний билд `dev` на сервер по SSH и публикует GitHub Release. Настройка окружения и первый деплой - [docs/ci-cd.md](docs/ci-cd.md).
