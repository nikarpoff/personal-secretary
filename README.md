# Personal Assistant

ИИ-секретарь в Telegram: текст и голосовые, Google Calendar, заметки Obsidian и напоминания.
Разговор ведёт [OpenClaw](https://docs.openclaw.ai) через OpenRouter; backend предоставляет
инструменты MCP и сам отправляет напоминания в Telegram. Голос распознаёт отдельный Whisper-сервис.

```text
Telegram ⇄ OpenClaw ── OpenRouter
              ├────── Whisper: голос → текст
              └────── MCP → personal-assistant
                              ├── Google Calendar
                              ├── Obsidian/Secretary + assistant-state
                              └── Telegram: напоминания
Obsidian Windows/Android ⇄ HTTPS WebDAV ⇄ data/obsidian
```

Сервис создаёт, переносит и удаляет события, поддерживает повторения, важность и уведомления.
Заметки, идеи, задачи и карточки людей хранятся в Markdown. Для вопроса о сохранённых сведениях
агент получает до трёх выдержек одним вызовом `get_note_context`: весь ответ до 4000 символов.
Для задач уведомление включает явное `remind_at`; один `due_at` задаёт только срок.

## Быстрый старт

Нужны Linux VPS с Docker Compose 2.24+, аккаунт Google, бот Telegram и ключ OpenRouter.

### 1. Telegram и Google

Создайте бота через [@BotFather](https://t.me/BotFather) и узнайте свой числовой Telegram user id.
В Google Cloud включите Calendar API, настройте OAuth consent screen и создайте OAuth client
типа Desktop app. Скачайте его JSON в `secrets/client_secret.json`, затем получите токен:

```bash
uv sync
uv run scripts/authorize_google.py --client secrets/client_secret.json --out secrets/google.json
```

Для сервера без браузера используйте `--no-browser` и SSH-проброс
`ssh -L 8765:localhost:8765 <сервер>`. В `GOOGLE_CREDENTIALS_FILE` указывайте полученный токен,
а не JSON OAuth-клиента. Чтобы refresh-токен не истекал через семь дней в режиме Testing,
переведите OAuth-приложение в Production.

Вместо OAuth допустим сервисный аккаунт: расшарьте на его e-mail календарь с правом изменения
событий, а в `GOOGLE_CALENDAR_ID` укажите календарь владельца. Без Google Workspace сервисный
аккаунт не может приглашать участников.

### 2. Конфигурация и backend

```bash
cp .env.example .env
openssl rand -hex 32  # AUTH_TOKEN
openssl rand -hex 32  # OPENCLAW_GATEWAY_TOKEN
```

Заполните токены, `TELEGRAM_CHAT_ID`, `OPENROUTER_API_KEY`, `OPENCLAW_MODEL` и `TIMEZONE`.
Модели нужна поддержка вызова инструментов. Backend и gateway читают общий `.env` через
`env_file`; пути внутри контейнеров `NOTES_ROOT=/notes` и `ASSISTANT_STATE_DIR=/assistant-state`
уже заданы в примере. Данные остаются в обычных каталогах хоста:

```bash
mkdir -p secrets data/assistant-state data/obsidian/Secretary/{Inbox,Tasks,Ideas,Knowledge,People,Archive}
mkdir -p data/obsidian/Personal
chmod 700 data/assistant-state

docker compose up -d --build personal-assistant
curl -f localhost:8090/health
```

Backend видит только `Secretary`: это явная область, которую владелец разрешает ассистенту
читать и менять. `Personal` остаётся для самого владельца. Полный vault синхронизирует WebDAV.

### 3. OpenClaw

```bash
scripts/openclaw.sh onboard --non-interactive --accept-risk \
  --mode local --no-install-daemon --skip-bootstrap --skip-channels --skip-skills --skip-search --skip-health --skip-ui \
  --auth-choice openrouter-api-key --secret-input-mode ref \
  --gateway-auth token --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN

scripts/openclaw.sh plugins install @openclaw/duckduckgo-plugin --pin
scripts/openclaw.sh plugins enable duckduckgo
scripts/openclaw.sh config patch --stdin < openclaw/openclaw.patch.json5
scripts/openclaw.sh config validate
scripts/openclaw.sh agents set-identity --agent main --workspace /home/node/.openclaw/workspace --from-identity

docker compose up -d
scripts/openclaw.sh mcp probe personal-assistant
scripts/openclaw.sh skills info secretary
```

`openclaw-init` готовит владельца каталога состояния, копирует личность из
`openclaw/workspace/*.md` и удаляет `BOOTSTRAP.md`. Правьте личность в репозитории:
при следующем запуске init он заменит соответствующие workspace-файлы.
Навык `openclaw/skills/secretary` монтируется только на чтение прямо в workspace;
копировать его вручную не нужно. После обновления Compose пересоздайте gateway через
`docker compose up -d --force-recreate openclaw-gateway`, затем отправьте боту `/new`.

Строковые `${VAR}` в патче разрешает OpenClaw. Числовые `WEB_*` преобразует wrapper при
`config patch --stdin`: OpenClaw не приводит строки окружения к числам. После изменения
этих параметров в `.env` пересоздайте gateway и повторите команду применения патча.

Control UI доступен через SSH-туннель `ssh -N -L 18789:127.0.0.1:18789 <сервер>`.
Одноразовую ссылку входа выдаёт `scripts/openclaw.sh dashboard --no-open`.

### 4. Утренняя сводка

```bash
# Замените 123456789 на свой TELEGRAM_CHAT_ID
scripts/openclaw.sh automations add --name "Утренняя сводка" \
  --cron "0 8 * * *" --tz Europe/Moscow --session isolated \
  --message "Утренняя сводка по навыку secretary" \
  --announce --channel telegram --to 123456789
```

Замените часовой пояс, если он отличается от Москвы. Проверьте бота сообщениями
«что у меня завтра?» и «поставь на пятницу в 15:00 созвон с Петей, это важно».

## Голосовые

Задайте `WHISPER_URL` адресом своего сервера, например
`https://whisper.example.com/v1/audio/transcriptions`, и при необходимости `WHISPER_API_KEY`.

Обработчик `openclaw/whisper/transcribe.mjs` отправляет файл как multipart с `model=whisper-1`,
`language=ru` и извлекает `text` из JSON. Ограничения: 20 МиБ, HTTP-запрос 120 секунд,
CLI-процесс 150 секунд. Учётные данные LLM для распознавания не используются.
Если сервер требует другой маршрут или модель, измените `WHISPER_URL`/`WHISPER_MODEL`.

## Инструменты

MCP: `POST /mcp/`, Streamable HTTP, `Authorization: Bearer <AUTH_TOKEN>`.

| Инструмент | Назначение |
|---|---|
| `list_events` | События периода, поиск `query`, текущее время `now` и связи `person_ids` |
| `create_event` / `update_event` | Время, длительность, место, важность, уведомления и связанные люди |
| `delete_event` | Удаление события или серии после подтверждения владельца |
| `get_note_context` | До 3 выдержек по теме, весь JSON до 4000 символов; без Archive и закрытых задач |
| `search_notes` / `read_note` | Поиск до 5 результатов и выбранный фрагмент до 4000 символов |
| `create_note` | Заметка, идея, задача, справочная запись или карточка человека |
| `append_note` / `update_note` / `archive_note` | Изменение выбранной записи с проверкой `expected_version` |
| `notes_status` | Время и диагностика заметок/напоминаний |

REST календаря: `GET/POST /v1/events`, `PATCH/DELETE /v1/events/{id}`. Swagger — `/docs`.
Время без смещения читается в `TIMEZONE`; дата означает событие на весь день, `end` — последний
день включительно. При переносе без нового конца сохраняется длительность.

## Напоминания и хранение

Календарь читается раз в `CALENDAR_SYNC_INTERVAL`, уведомления проверяются каждые 30 секунд.
Обычные смещения задаёт `REMINDER_MINUTES`, важные получают дополнительно
`IMPORTANT_REMINDER_MINUTES`. Индивидуальные уведомления события заменяют обычные;
пустой список не отключает дополнительные уведомления важных событий.

Кэш календаря и отметки его уведомлений находятся в RAM, поэтому после рестарта возможен
повтор до начала события. Отметки задач Obsidian постоянны в `data/assistant-state`.
Для одного vault нужен один backend worker. Авария между приёмом Telegram и сохранением
отметки может дать повтор; синхронизация Obsidian не заменяет резервную копию.

## Безопасность и документация

Telegram принимает сообщения только от `TELEGRAM_CHAT_ID`; порты backend и gateway открыты
на `127.0.0.1`. У агента запрещены runtime-команды и запись файлов, включён `workspaceOnly`.
Vault не монтируется в gateway, заметки доступны через ограниченные MCP-инструменты.
Текст событий, заметок и сайтов считается данными. Перед удалением события и массовыми
изменениями агент запрашивает подтверждение.

- [Obsidian и WebDAV](docs/obsidian.md): пути, `secrets/webdav.env`, синхронизация и резервирование.
- [Карточки людей](docs/people.md): имена, контекст и связи встреч.
- [Поиск и веб](docs/web-search.md): параметры `WEB_*`, ограничения и диагностика.
- [CI/CD](docs/ci-cd.md): тесты и сборка `dev` в GHCR, деплой по SSH и релиз по тегу.

Для локальной разработки на Linux установите зависимости через `uv sync`, замените контейнерные
пути заметок в `.env` абсолютными локальными путями и запустите `uv run fastapi dev app/main.py`.
CLI и конфигурация OpenClaw рассчитаны на `2026.10.9`; образ можно закрепить через `OPENCLAW_IMAGE`.
