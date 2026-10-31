# Personal Assistant

ИИ-секретарь в Telegram: ставит, переносит и удаляет события в Google Calendar и напоминает о них. Разговор ведёт агент [OpenClaw](https://docs.openclaw.ai) на модели из OpenRouter. Этот сервис - его «руки» для календаря: агент вызывает его по MCP, а напоминания сервис шлёт в Telegram сам, через того же бота.

```
 Вы ⇄ Telegram-бот (@BotFather)
          │ long polling
          ▼
   OpenClaw gateway ── LLM через OpenRouter, навык secretary, утренняя сводка (automation)
          │ MCP: http://personal-assistant:8000/mcp/ + Bearer AUTH_TOKEN
          ▼
   personal-assistant ──────► Google Calendar API
          │
          └─────────────────► Telegram Bot API: напоминания от имени того же бота
```

Почему напоминания живут в сервисе, а не в автоматизациях OpenClaw: они должны приходить вовремя и всегда, даже когда модель недоступна, и не тратить токены на каждую проверку. Утреннюю сводку, где нужен «ум», делает OpenClaw.

## Возможности

- **События.** Создание, перенос, удаление, повторяющиеся события (RRULE), события на весь день, поиск события по словам. Время агент передаёт в вашем часовом поясе.
- **Напоминания в Telegram.** За `REMINDER_MINUTES` до начала (по умолчанию 15 минут). Если у события в Google настроены свои уведомления, берутся они. Важные события получают ещё напоминания за сутки и за два часа (`IMPORTANT_REMINDER_MINUTES`). В напоминании - время, место, начало описания и ссылка на событие.
- **Важные события** помечаются цветом в Google Calendar (`IMPORTANT_COLOR_ID`, по умолчанию красный). Событие можно сделать важным и прямо в интерфейсе Google, сменив ему цвет.
- **Утренняя сводка** - автоматизация OpenClaw: события дня и важное на неделю.

## Где что настраивается

Все ваши данные и секреты живут в одном файле - `.env` (он в `.gitignore`). Оба контейнера читают его.

| Что | Где |
|---|---|
| Токен бота, ваш Telegram user id | `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| LLM агента | `.env`: `OPENROUTER_API_KEY`, `OPENCLAW_MODEL` |
| Доступ к Google Calendar | файл `secrets/google.json`, путь в `.env`: `GOOGLE_CREDENTIALS_FILE` |
| Токены сервиса и шлюза | `.env`: `AUTH_TOKEN`, `OPENCLAW_GATEWAY_TOKEN` |
| Поведение агента: allowlist, MCP-сервер, политика инструментов | `openclaw/openclaw.patch.json5` - в git, без секретов |
| Как агент работает с календарём | `openclaw/skills/secretary/SKILL.md` - в git, шлюз читает его из репозитория |
| Имя, характер, правила агента | `openclaw/workspace/*.md` - в git, копируются в рабочий каталог агента |

`openclaw.patch.json5` - это дополнение к конфигу OpenClaw (`data/openclaw/openclaw.json`, его создаёт `onboard`). Значения из окружения там записаны ссылками `${VAR}`: OpenClaw подставляет их из `.env` при каждой загрузке конфига. Поэтому в патче нет ни ключей, ни вашего user id, и его не нужно редактировать. Сам `openclaw.json` после онбординга тоже хранит не ключи, а ссылки на переменные (`--secret-input-mode ref`).

## Быстрый старт

Нужны сервер с Docker, аккаунт Google и ключ [OpenRouter](https://openrouter.ai/keys).

### 1. Telegram-бот

1. В [@BotFather](https://t.me/BotFather): `/newbot` → имя → токен вида `123456:ABC...`.
2. Свой числовой user id: напишите [@userinfobot](https://t.me/userinfobot) или откройте `https://api.telegram.org/bot<токен>/getUpdates` после любого сообщения боту.

### 2. Доступ к Google Calendar

1. [Google Cloud Console](https://console.cloud.google.com/) → новый проект → **APIs & Services → Library** → включите **Google Calendar API**.
2. **OAuth consent screen**: тип External, добавьте себя в Test users. Затем **Publish app** (статус «In production»). Иначе Google отзывает refresh-токен через 7 дней и календарь отвалится. Предупреждение «приложение не проверено» для личного использования нормально.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → скачайте JSON, например в `secrets/client_secret.json`. Это файл OAuth-клиента (в нём ключ `installed`). Сервису он не подходит: из него получают токен на следующем шаге.
4. Получите токен:

```bash
uv sync
uv run scripts/authorize_google.py --client secrets/client_secret.json --out secrets/google.json
```

На сервере без браузера пробросьте порт и откройте напечатанную ссылку у себя: `ssh -L 8765:localhost:8765 <сервер>`, затем тот же скрипт с `--no-browser`. Скрипт запрашивает только доступ к событиям (`calendar.events`). В `GOOGLE_CREDENTIALS_FILE` указывается результат - `secrets/google.json`.

Вместо OAuth можно взять сервисный аккаунт: ключ JSON в `secrets/google.json`, календарь расшарен на e-mail аккаунта с правом «Вносить изменения», `GOOGLE_CALENDAR_ID` = ваш e-mail. Ограничение: без Google Workspace сервисный аккаунт не может приглашать участников.

### 3. Конфигурация

```bash
cp .env.example .env
openssl rand -hex 32   # для AUTH_TOKEN
openssl rand -hex 32   # для OPENCLAW_GATEWAY_TOKEN
```

Заполните в `.env`: `AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENROUTER_API_KEY`, `OPENCLAW_MODEL`, `OPENCLAW_GATEWAY_TOKEN`, при необходимости `TIMEZONE`.

Каталоги `data/` создаются сами. OpenClaw работает от пользователя `node` (uid 1000): владельца `data/openclaw` выставляет разовый контейнер `openclaw-init`, его запускают `scripts/openclaw.sh` и `docker compose up`.

### 4. Сервис

```bash
docker compose up -d --build personal-assistant
curl -s localhost:8090/health                                   # {"status":"ok"}
docker compose logs personal-assistant | grep -i calendar       # Google Calendar client ready
```

### 5. OpenClaw

Команды OpenClaw выполняет `scripts/openclaw.sh`: в работающем шлюзе через `docker compose exec`, до первого запуска - разовым контейнером.

```bash
# Онбординг: провайдер OpenRouter; ключ и токен шлюза остаются в .env, в конфиг пишутся ссылки на них.
# --skip-bootstrap: личность агента приходит из openclaw/workspace, ритуал знакомства не нужен
scripts/openclaw.sh onboard --non-interactive --accept-risk \
  --mode local --no-install-daemon --skip-bootstrap --skip-channels --skip-skills --skip-search --skip-health --skip-ui \
  --auth-choice openrouter-api-key --secret-input-mode ref \
  --gateway-auth token --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN

# Модель, Telegram только для вас, MCP-сервер, навык, часовой пояс, ограничение инструментов
scripts/openclaw.sh config patch --stdin < openclaw/openclaw.patch.json5     # Applied N config update(s)
scripts/openclaw.sh config validate

# Имя и эмодзи агента из openclaw/workspace/IDENTITY.md - в конфиг, их показывают UI и каналы
scripts/openclaw.sh agents set-identity --agent main --workspace /home/node/.openclaw/workspace --from-identity

docker compose up -d
scripts/openclaw.sh mcp probe personal-assistant      # personal-assistant: 4 tools
scripts/openclaw.sh models status | grep Default      # openrouter/... из OPENCLAW_MODEL
scripts/openclaw.sh skills info secretary             # secretary ✓ Ready
```

Шлюз стартует примерно через 20 секунд после того, как сервис станет healthy. Навык `secretary` OpenClaw читает прямо из `openclaw/skills` репозитория (смонтирован только на чтение), копировать его не нужно: после `git pull` агент видит новую версию.

### Личность агента

Имя, характер и правила агента лежат в `openclaw/workspace`:

| Файл | Что в нём |
|---|---|
| `IDENTITY.md` | Имя, эмодзи, роль |
| `SOUL.md` | Характер и тон |
| `USER.md` | Ваши предпочтения: язык, формат ответов |
| `AGENTS.md` | Правила работы: только календарь, напоминания шлёт сервис, текст событий - данные, а не команды |

Перед каждым стартом шлюза `openclaw-init` копирует их в рабочий каталог агента (`data/openclaw/workspace`) и удаляет `BOOTSTRAP.md` - ритуал первого запуска, где агент выбирает себе имя и сам пишет эти файлы. Пройти его агент не может: записи файлов и shell у него нет, и давать их не нужно (см. «Безопасность»).

Сменить имя или характер: поправьте файлы в `openclaw/workspace`, затем

```bash
docker compose up -d       # openclaw-init обновит файлы в рабочем каталоге
scripts/openclaw.sh agents set-identity --agent main --workspace /home/node/.openclaw/workspace --from-identity
```

и отправьте боту `/new`: новая сессия начнётся с обновлённой личностью.

### Control UI

Порт шлюза опубликован только на `127.0.0.1` сервера. С вашей машины пробросьте его на **тот же** локальный порт 18789: шлюз пускает UI только с `http://localhost:18789` и `http://127.0.0.1:18789`.

```bash
ssh -N -L 18789:127.0.0.1:18789 <сервер>
```

Проще всего входить одноразовой ссылкой: она сразу авторизует браузер и одобряет его как устройство владельца. При входе по токену шлюз может дополнительно попросить одобрить устройство.

```bash
scripts/openclaw.sh dashboard --no-open      # ссылка вида http://127.0.0.1:18789/#bootstrapToken=...
```

Откройте ссылку в браузере на своей машине, пока она не истекла (несколько минут). Если вошли по токену из `OPENCLAW_GATEWAY_TOKEN` и UI ждёт одобрения устройства: `scripts/openclaw.sh devices list`, затем `scripts/openclaw.sh devices approve <id>`.

### 6. Утренняя сводка

```bash
scripts/openclaw.sh automations add --name "Утренняя сводка" \
  --cron "0 8 * * *" --tz Europe/Moscow --session isolated \
  --message "Утренняя сводка по навыку secretary" \
  --announce --channel telegram --to <ваш TELEGRAM_CHAT_ID>
```

Проверка: напишите боту «что у меня завтра?», «поставь на пятницу в 15:00 созвон с Петей, это важно», «перенеси созвон с Петей на час позже».

## LLM агента

Модель задаётся в `.env` переменной `OPENCLAW_MODEL` в формате `openrouter/<автор>/<модель>`. Чтобы сменить модель, поменяйте переменную и перезапустите шлюз: `docker compose up -d openclaw-gateway`. Из чата модель на сессию меняет команда `/model`.

Агенту нужна модель с вызовом инструментов (tool calling): без него он не сможет работать с календарём. Список моделей: `scripts/openclaw.sh models list --provider openrouter --all`. По умолчанию в `.env.example` стоит `anthropic/claude-haiku-5.5`: она дешёвая и надёжно вызывает инструменты. `openrouter/auto`, который выбирает онбординг, патч заменяет, чтобы модель и цена не менялись от запроса к запросу.

В патче для OpenRouter включены `require_parameters` (только провайдеры модели, которые поддерживают tools) и `data_collection: "deny"` (календарь не уходит провайдерам, которые сохраняют запросы для обучения).

## Инструменты агента (MCP)

Эндпоинт `POST /mcp/` (Streamable HTTP, stateless, JSON-ответы), авторизация `Authorization: Bearer <AUTH_TOKEN>`.

| Инструмент | Что делает |
|---|---|
| `list_events` | События периода (по умолчанию сегодня и неделя вперёд), поиск по словам `query`, текущее время `now` |
| `create_event` | Новое событие: время, длительность, место, `important`, `remind_minutes`, `recurrence` |
| `update_event` | Меняет только переданные поля; при переносе длительность сохраняется |
| `delete_event` | Удаляет событие; `recurring_event_id` удаляет всю серию |

Ошибки предметной области («Event not found», «Event must end after it starts») доходят до модели текстом: она может исправить вызов.

## REST API

Те же операции для отладки и других клиентов, Swagger - на `/docs`. Авторизация та же: без токена 401, с неверным 403.

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/v1/events?start=&end=&q=&limit=` | События периода |
| `POST` | `/v1/events` | Создать событие → 201 |
| `PATCH` | `/v1/events/{id}` | Изменить событие |
| `DELETE` | `/v1/events/{id}` | Удалить событие → 204 |

```bash
curl -s -X POST localhost:8090/v1/events -H "Authorization: Bearer $AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title": "Созвон с Петей", "start": "2026-11-06T15:00", "important": true}'
```

```json
{
  "id": "k1v2...", "title": "Созвон с Петей",
  "start": "2026-11-06T15:00+03:00", "end": "2026-11-06T16:00+03:00", "all_day": false,
  "description": "", "location": "", "important": true, "reminders": [1440, 120, 15],
  "recurring_event_id": null, "link": "https://www.google.com/calendar/event?eid=..."
}
```

Формат времени: `2026-11-06T15:00` - локальное время в `TIMEZONE`, явное смещение (`+03:00`, `Z`) учитывается. `2026-11-06` - событие на весь день. У событий на весь день `end` - последний день включительно.

| Код | Когда |
|---|---|
| 400 | Неверное время, конец раньше начала, неверные минуты напоминаний |
| 401 / 403 | Нет токена / неверный токен |
| 404 | Нет события с таким id |
| 422 | Неверное тело запроса; Google отклонил событие (например, неверный RRULE) |
| 502 | Ошибка Google Calendar, нет доступа к календарю, ошибка обновления токена |
| 503 | Календарь не настроен (`GOOGLE_CREDENTIALS_FILE`) или Google недоступен |
| 504 | Google не ответил вовремя |

## Как работают напоминания

1. Раз в `CALENDAR_SYNC_INTERVAL` секунд сервис читает из Google события от «сутки назад» до `CALENDAR_SYNC_DAYS` вперёд, повторяющиеся - отдельными вхождениями. После изменения через агента календарь перечитывается сразу. События хранятся в памяти: источник правды - Google.
2. Каждые 30 секунд сервис ищет события, у которых подошло время напоминания:
   - уведомления по умолчанию в Google → `REMINDER_MINUTES`;
   - свои уведомления события в Google (или `remind_minutes` от агента) → они; пустой список - без них;
   - важное событие → плюс `IMPORTANT_REMINDER_MINUTES`.
   Пустой список отключает обычные напоминания, но не дополнительные у важного события.
3. Напоминание, время которого наступило раньше создания события, пропускается: «за сутки» для встречи, поставленной вечером на утро, не придёт.
4. Если сервис лежал и пропустил несколько напоминаний, придёт одно - ближайшее к началу, и только если событие ещё не началось.
5. Отправленное напоминание помечается по ключу (событие, время начала, минуты) в SQLite (`DATA_DIR/assistant.db`), поэтому после перезапуска оно не придёт повторно. После переноса события напоминания придут заново. Если Telegram не ответил, напоминание повторится на следующем тике.

У событий на весь день нет часа, от которого считать минуты. Они попадают в утреннюю сводку.

## Конфигурация сервиса

Переменные окружения (`.env`); все необязательны, но без Google и Telegram соответствующие функции выключены.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AUTH_TOKEN` | - | Bearer-токен REST и MCP; без него авторизация выключена |
| `DATA_DIR` | `/data` | Каталог `assistant.db` с отметками отправленных напоминаний |
| `TIMEZONE` | `Europe/Moscow` | Часовой пояс владельца |
| `GOOGLE_CREDENTIALS_FILE` | - | Токен `authorized_user` из `scripts/authorize_google.py` или ключ сервисного аккаунта |
| `GOOGLE_CALENDAR_ID` | `primary` | Календарь; для сервисного аккаунта - e-mail владельца |
| `CALENDAR_SYNC_INTERVAL` | `60` | Период чтения календаря, секунды |
| `CALENDAR_SYNC_DAYS` | `30` | На сколько дней вперёд смотреть; должно покрывать самое раннее напоминание |
| `IMPORTANT_COLOR_ID` | `11` | Цвет важного события (11 - красный «Томатный») |
| `TELEGRAM_BOT_TOKEN` | - | Токен бота; без него напоминания только пишутся в лог |
| `TELEGRAM_CHAT_ID` | - | Ваш user id; задаётся вместе с токеном |
| `REMINDER_MINUTES` | `15` | Напоминания по умолчанию, минуты через запятую |
| `IMPORTANT_REMINDER_MINUTES` | `1440,120` | Дополнительные напоминания важных событий |
| `LOG_LEVEL` / `LOG_DIAGNOSE` | `INFO` / `False` | Логирование |

Переменные OpenClaw (`OPENROUTER_API_KEY`, `OPENCLAW_MODEL`, `OPENCLAW_GATEWAY_TOKEN`, `OPENCLAW_IMAGE`, `OPENCLAW_TZ`) описаны в `.env.example`. Если OpenRouter, Google или Telegram недоступны с сервера напрямую, задайте `HTTPS_PROXY` (и `NO_PROXY=personal-assistant,localhost`): его подхватывают оба контейнера.

Неверная конфигурация не роняет сервис трейсбеком: каждая причина пишется в лог строкой `Bad configuration: ...`, после чего процесс завершается. Если в `GOOGLE_CREDENTIALS_FILE` по ошибке указан файл OAuth-клиента, сервис так и скажет.

## Безопасность

- Бот отвечает только вам: `dmPolicy: "allowlist"` с вашим user id, вы же владелец слэш-команд OpenClaw.
- У агента нет shell, записи файлов и веба: профиль инструментов `messaging` плюс `read` для навыков. Приглашение в календарь от постороннего может содержать prompt injection, и через него агент максимум изменит ваш календарь. Удаление агент подтверждает у вас (инструкция навыка, у инструмента пометка `destructiveHint`).
- Google-доступ ограничен событиями (`calendar.events`): ни списка календарей, ни прав доступа.
- Порты сервиса (8090) и Control UI OpenClaw (18789) опубликованы только на `127.0.0.1`. Control UI открывайте через SSH-туннель (раздел «Control UI»).
- Агенту не нужны `write` и `exec`, даже когда стандартный ритуал знакомства их просит: личность задаётся файлами из `openclaw/workspace`, имя в конфиг пишет `set-identity`.
- Токены не пишутся в лог; логгер httpx заглушён, потому что URL Bot API содержит токен бота.

## Устройство

```
app/
├── main.py                 настройки → логирование → FastAPI → /health → REST → /mcp
├── api_v1/
│   ├── views.py            REST /v1/events
│   ├── tools.py            MCP-инструменты агента и McpEndpoint
│   ├── schemas.py          pydantic-контракт API и инструментов
│   └── errors.py           AppError → {"detail": ...}
├── core/
│   ├── engine.py           AssistantEngine: операции с календарём
│   ├── scheduler.py        фоновый цикл: чтение календаря и отправка напоминаний
│   ├── reminders.py        какие напоминания пора отправить и их текст
│   ├── store.py            SQLite: отметки отправленных напоминаний
│   ├── clock.py            разбор локального времени агента
│   ├── upstream/           клиенты Google Calendar и Telegram Bot API
│   ├── auth/               Bearer для REST (Depends) и для смонтированного MCP (ASGI)
│   ├── config/             Settings (env) и constants.py (лимиты API, тайминги)
│   ├── entities.py         Event, Reminder
│   └── exceptions.py       AppError и наследники
└── utils/                  lifespan (движок + MCP-сервер), логирование
openclaw/
├── openclaw.patch.json5    патч конфига OpenClaw: модель, Telegram, MCP, навык, пояс, политика инструментов
├── skills/secretary/       навык агента: как работать с календарём
└── workspace/              личность агента: IDENTITY, SOUL, USER, AGENTS
scripts/
├── authorize_google.py     разовый вход в Google
└── openclaw.sh             команды OpenClaw в контейнере шлюза
tests/                      pytest: напоминания, клиент Google, движок с фейками, API и MCP
```

Google Calendar вызывается через общий `httpx.AsyncClient`. google-auth только загружает учётные данные и обновляет access-токен в потоке под блокировкой: `google-api-python-client` синхронный и не потокобезопасный. SQLite работает в одном выделенном потоке.

## Разработка

```bash
uv sync
uv run pytest -q                           # тесты без сети и ключей; локальный .env они не читают
uv run fastapi dev app/main.py             # http://localhost:8000/docs, настройки из .env
```

## Ограничения

- Один календарь (`GOOGLE_CALENDAR_ID`) и один владелец.
- Заметок и базы знаний нет: это отдельный сервис, если понадобится.
- Перенос времени через id серии меняет начало серии целиком; одно вхождение переносится по его собственному id.
- Синтаксис CLI и конфига OpenClaw проверен на версии 2026.10.9. Если команды изменятся, зафиксируйте `OPENCLAW_IMAGE` на этой версии.
