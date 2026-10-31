# Personal Assistant

ИИ-секретарь в Telegram: ставит события в Google Calendar, напоминает о них, ведёт заметки и отвечает на вопросы по общей базе заметок и событий. Разговор ведёт агент [OpenClaw](https://docs.openclaw.ai) на моделях OpenAI. Этот сервис - его «руки»: Google Calendar, база знаний (SQLite) и напоминания. Агент вызывает сервис по MCP, а напоминания сервис шлёт в Telegram сам, через того же бота.

```
 Вы ⇄ Telegram-бот (@BotFather)
          │ long polling
          ▼
   OpenClaw gateway ── модель OpenAI, навык secretary, утренняя сводка (automation)
          │ MCP: http://personal-assistant:8000/mcp/ + Bearer AUTH_TOKEN
          ▼
   personal-assistant ──────► Google Calendar API
     │  SQLite: заметки, зеркало календаря, FTS5 + эмбеддинги OpenAI
     └──────────────────────► Telegram Bot API: напоминания от имени того же бота
```

Почему напоминания живут в сервисе, а не в автоматизациях OpenClaw: они должны приходить вовремя и всегда, даже когда модель недоступна, и не тратить токены на каждую проверку. Утреннюю сводку, где нужен «ум», делает OpenClaw.

## Возможности

- **События.** Создание, перенос, удаление, повторяющиеся события (RRULE), события на весь день. Время агент передаёт в вашем часовом поясе.
- **Напоминания в Telegram.** За `REMINDER_MINUTES` до начала (по умолчанию 15 минут). Если у события в Google настроены свои уведомления, берутся они. Важные события получают ещё напоминания за сутки и за два часа (`IMPORTANT_REMINDER_MINUTES`). В напоминании - место, ссылка и заметки к событию.
- **Важные события** помечаются красным цветом в Google Calendar (`IMPORTANT_COLOR_ID`). Так их видно в календаре, и событие можно сделать важным прямо из интерфейса Google, сменив цвет.
- **Заметки** с тегами, в том числе привязанные к событию: «запиши к встрече с Петром: спросить про бюджет».
- **База знаний.** Один поиск по заметкам и событиям, в том числе прошедшим. Поиск по словам (FTS5 с отсечением русских окончаний: «встречи» находит «встреча») и по смыслу (эмбеддинги OpenAI). Результаты сливаются через reciprocal rank fusion.
- **Утренняя сводка** - автоматизация OpenClaw: события дня с заметками и важное на неделю.

## Быстрый старт

Нужны сервер с Docker, аккаунт Google и ключ OpenAI API.

### 1. Telegram-бот

1. В [@BotFather](https://t.me/BotFather): `/newbot` → имя → токен вида `123456:ABC...`.
2. Свой числовой user id: напишите [@userinfobot](https://t.me/userinfobot) или откройте `https://api.telegram.org/bot<токен>/getUpdates` после любого сообщения боту.

### 2. Доступ к Google Calendar

1. [Google Cloud Console](https://console.cloud.google.com/) → новый проект → **APIs & Services → Library** → включите **Google Calendar API**.
2. **OAuth consent screen**: тип External, добавьте себя в Test users. Затем **Publish app** (статус «In production»). Иначе Google отзывает refresh-токен через 7 дней и календарь отвалится. Предупреждение «приложение не проверено» для личного использования нормально.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → скачайте JSON в `secrets/client_secret.json`.
4. Получите токен:

```bash
uv sync
uv run scripts/authorize_google.py --client secrets/client_secret.json --out secrets/google.json
```

На сервере без браузера пробросьте порт и откройте напечатанную ссылку у себя: `ssh -L 8765:localhost:8765 <сервер>`, затем тот же скрипт с `--no-browser`. Скрипт запрашивает только доступ к событиям (`calendar.events`).

Вместо OAuth можно взять сервисный аккаунт: ключ JSON в `secrets/google.json`, календарь расшарен на e-mail аккаунта с правом «Вносить изменения», `GOOGLE_CALENDAR_ID` = ваш e-mail. Ограничение: без Google Workspace сервисный аккаунт не может приглашать участников.

### 3. Конфигурация

```bash
cp .env.example .env
openssl rand -hex 32   # для AUTH_TOKEN
openssl rand -hex 32   # для OPENCLAW_GATEWAY_TOKEN
```

Заполните в `.env`: `AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENAI_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`, при необходимости `TIMEZONE`.

```bash
mkdir -p data/assistant data/openclaw secrets
sudo chown -R 1000:1000 data/openclaw     # OpenClaw работает от пользователя node (uid 1000)
```

### 4. Сервис

```bash
docker compose up -d --build personal-assistant
curl -s localhost:8090/health                                   # {"status":"ok"}
docker compose logs personal-assistant | grep -i calendar       # Google Calendar client ready
```

### 5. OpenClaw

Команды OpenClaw выполняет `scripts/openclaw.sh`: в работающем шлюзе через `docker compose exec`, до первого запуска - разовым контейнером.

```bash
# Онбординг: ключ OpenAI и токен шлюза остаются в .env, в конфиг пишутся только ссылки на них
scripts/openclaw.sh onboard --non-interactive --accept-risk \
  --mode local --no-install-daemon --skip-channels --skip-skills --skip-search --skip-health --skip-ui \
  --auth-choice openai-api-key --secret-input-mode ref \
  --gateway-auth token --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN

# Telegram только для вас, MCP-сервер, часовой пояс, ограничение инструментов агента
TG_ID=123456789     # ваш user id, тот же, что TELEGRAM_CHAT_ID
sed "s/<YOUR_TELEGRAM_USER_ID>/$TG_ID/g" openclaw/openclaw.patch.json5 | scripts/openclaw.sh config patch --stdin
scripts/openclaw.sh config validate

# Навык секретаря
mkdir -p data/openclaw/workspace/skills && cp -r openclaw/skills/secretary data/openclaw/workspace/skills/
sudo chown -R 1000:1000 data/openclaw

docker compose up -d
scripts/openclaw.sh mcp probe personal-assistant      # personal-assistant: 9 tools
scripts/openclaw.sh skills info secretary             # secretary ✓ Ready
```

Если ваш пояс не Москва, поправьте `userTimezone` в `openclaw/openclaw.patch.json5` вместе с `TIMEZONE` в `.env`. Шлюз стартует около 20 секунд после того, как сервис станет healthy.

Модель онбординг выбирает сам. Сменить её: `scripts/openclaw.sh models list --provider openai`, затем `scripts/openclaw.sh models set openai/<модель>`.

### 6. Утренняя сводка

```bash
scripts/openclaw.sh automations add --name "Утренняя сводка" \
  --cron "0 8 * * *" --tz Europe/Moscow --session isolated \
  --message "Утренняя сводка по навыку secretary" \
  --announce --channel telegram --to $TG_ID
```

Проверка: напишите боту «что у меня завтра?», «поставь на пятницу в 15:00 созвон с Петей, это важно», «запиши к созвону: обсудить бюджет».

## Инструменты агента (MCP)

Эндпоинт `POST /mcp/` (Streamable HTTP, stateless, JSON-ответы), авторизация `Authorization: Bearer <AUTH_TOKEN>`.

| Инструмент | Что делает |
|---|---|
| `list_events` | События периода (по умолчанию сегодня и неделя вперёд) с заметками и текущим временем `now` |
| `create_event` | Новое событие: время, длительность, место, `important`, `remind_minutes`, `recurrence` |
| `update_event` | Меняет только переданные поля; при переносе длительность сохраняется |
| `delete_event` | Удаляет событие; `recurring_event_id` удаляет всю серию |
| `add_note` | Заметка с тегами, опционально привязанная к событию |
| `update_note` / `delete_note` | Правка и удаление заметки |
| `list_notes` | Последние заметки, фильтр по тегу и событию |
| `search` | Поиск по заметкам и событиям, по словам и по смыслу |

Ошибки предметной области («Event not found», «Event must end after it starts») доходят до модели текстом: она может исправить вызов.

## REST API

Те же операции для отладки и других клиентов, Swagger - на `/docs`. Авторизация та же: без токена 401, с неверным 403.

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/v1/events?start=&end=&q=&limit=` | События периода с заметками |
| `POST` | `/v1/events` | Создать событие → 201 |
| `PATCH` | `/v1/events/{id}` | Изменить событие |
| `DELETE` | `/v1/events/{id}` | Удалить событие → 204 |
| `GET` | `/v1/notes?tag=&event_id=&limit=` | Заметки, новые первыми |
| `POST` | `/v1/notes` | Создать заметку → 201 |
| `GET` / `PATCH` / `DELETE` | `/v1/notes/{id}` | Заметка: получить, изменить, удалить |
| `GET` | `/v1/search?q=&kind=&limit=` | Поиск по базе знаний |

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
  "recurring_event_id": null, "link": "https://www.google.com/calendar/event?eid=...", "notes": []
}
```

Формат времени: `2026-11-06T15:00` - локальное время в `TIMEZONE`, явное смещение (`+03:00`, `Z`) учитывается. `2026-11-06` - событие на весь день. У событий на весь день `end` - последний день включительно.

| Код | Когда |
|---|---|
| 400 | Неверное время, конец раньше начала, пустая заметка или запрос |
| 401 / 403 | Нет токена / неверный токен |
| 404 | Нет события или заметки с таким id |
| 422 | Неверное тело запроса; Google отклонил событие (например, неверный RRULE) |
| 502 | Ошибка Google Calendar, нет доступа к календарю, ошибка обновления токена |
| 503 | Календарь не настроен (`GOOGLE_CREDENTIALS_FILE`) или Google недоступен |
| 504 | Google не ответил вовремя |

## Как работают напоминания

1. Раз в `CALENDAR_SYNC_INTERVAL` секунд сервис зеркалирует в SQLite события от «сутки назад» до `CALENDAR_SYNC_DAYS` вперёд. Повторяющиеся события разворачиваются в отдельные вхождения. Удалённые в Google события из зеркала уходят. События, созданные и изменённые через агента, попадают в зеркало сразу.
2. Каждые 30 секунд сервис ищет события, у которых подошло время напоминания:
   - уведомления по умолчанию в Google → `REMINDER_MINUTES`;
   - свои уведомления события в Google (или `remind_minutes` от агента) → они; пустой список - без них;
   - важное событие → плюс `IMPORTANT_REMINDER_MINUTES`.
   Пустой список отключает обычные напоминания, но не дополнительные у важного события.
3. Напоминание, время которого наступило раньше создания события, пропускается: «за сутки» для встречи, поставленной вечером на утро, не придёт.
4. Если сервис лежал и пропустил несколько напоминаний, придёт одно - ближайшее к началу, и только если событие ещё не началось.
5. Отправленное напоминание помечается по ключу (событие, время начала, минуты). После переноса события напоминания придут заново. Если Telegram не ответил, напоминание повторится на следующем тике.

У событий на весь день нет часа, от которого считать минуты. Они попадают в утреннюю сводку.

## Конфигурация

Переменные окружения (`.env`); все необязательны, но без Google, Telegram и OpenAI соответствующие функции выключены.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AUTH_TOKEN` | - | Bearer-токен REST и MCP; без него авторизация выключена |
| `DATA_DIR` | `/data` | Каталог базы знаний `assistant.db` |
| `TIMEZONE` | `Europe/Moscow` | Часовой пояс владельца |
| `GOOGLE_CREDENTIALS_FILE` | - | Токен `authorized_user` или ключ сервисного аккаунта |
| `GOOGLE_CALENDAR_ID` | `primary` | Календарь; для сервисного аккаунта - e-mail владельца |
| `CALENDAR_SYNC_INTERVAL` | `60` | Период зеркалирования календаря, секунды |
| `CALENDAR_SYNC_DAYS` | `30` | Глубина зеркала вперёд, дни; должна покрывать самое раннее напоминание |
| `IMPORTANT_COLOR_ID` | `11` | Цвет важного события (11 - красный «Томатный») |
| `TELEGRAM_BOT_TOKEN` | - | Токен бота; без него напоминания только пишутся в лог |
| `TELEGRAM_CHAT_ID` | - | Ваш user id; задаётся вместе с токеном |
| `REMINDER_MINUTES` | `15` | Напоминания по умолчанию, минуты через запятую |
| `IMPORTANT_REMINDER_MINUTES` | `1440,120` | Дополнительные напоминания важных событий |
| `OPENAI_API_KEY` | - | Ключ для эмбеддингов; без него поиск только полнотекстовый |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-совместимый API |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Модель эмбеддингов |
| `EMBEDDING_DIMENSIONS` | `512` | Размер вектора; смена модели или размера переиндексирует базу в фоне |
| `LOG_LEVEL` / `LOG_DIAGNOSE` | `INFO` / `False` | Логирование |

Переменные OpenClaw (`OPENCLAW_GATEWAY_TOKEN`, `OPENCLAW_IMAGE`, `OPENCLAW_TZ`) описаны в `.env.example`. Если OpenAI или Telegram недоступны с сервера напрямую, задайте `HTTPS_PROXY` (и `NO_PROXY=personal-assistant,localhost`): его подхватывают оба контейнера.

Неверная конфигурация не роняет сервис трейсбеком: каждая причина пишется в лог строкой `Bad configuration: ...`, после чего процесс завершается.

## Безопасность

- Бот отвечает только вам: `dmPolicy: "allowlist"` с вашим user id, вы же владелец слэш-команд OpenClaw.
- У агента нет shell, записи файлов и веба: профиль инструментов `messaging` плюс `read` для навыков и память. Приглашение в календарь от постороннего может содержать prompt injection, и через него агент максимум изменит ваш календарь или заметки. Удаление агент подтверждает у вас (инструкция навыка, у инструментов пометка `destructiveHint`).
- Google-доступ ограничен событиями (`calendar.events`): ни списка календарей, ни прав доступа.
- Порты сервиса (8090) и Control UI OpenClaw (18789) опубликованы только на `127.0.0.1`. Control UI открывайте через `ssh -L 18789:localhost:18789 <сервер>`.
- Токены не пишутся в лог; логгер httpx заглушён, потому что URL Bot API содержит токен бота.

## Устройство

```
app/
├── main.py                 настройки → логирование → FastAPI → /health → REST → /mcp
├── api_v1/
│   ├── views.py            REST /v1/events, /v1/notes, /v1/search
│   ├── tools.py            MCP-инструменты агента и McpEndpoint
│   ├── schemas.py          pydantic-контракт API и инструментов
│   └── errors.py           AppError → {"detail": ...}
├── core/
│   ├── engine.py           AssistantEngine: фасад календаря, заметок и поиска
│   ├── scheduler.py        фоновый цикл: зеркало календаря, напоминания, индексация
│   ├── reminders.py        какие напоминания пора отправить и их текст
│   ├── clock.py            разбор локального времени агента
│   ├── knowledge/
│   │   ├── repository.py   SQLite: заметки, зеркало, отметки напоминаний, FTS5, векторы
│   │   └── search.py       гибридный поиск: BM25 + косинус, слияние RRF
│   ├── upstream/           клиенты Google Calendar, Telegram Bot API, OpenAI Embeddings
│   ├── auth/               Bearer для REST (Depends) и для смонтированного MCP (ASGI)
│   ├── config/             Settings (env) и constants.py (лимиты, эвристики поиска)
│   ├── entities.py         Event, Note, SearchHit, Reminder
│   └── exceptions.py       AppError и наследники
└── utils/                  lifespan (движок + MCP-сервер), логирование
openclaw/
├── openclaw.patch.json5    патч конфига OpenClaw: Telegram, MCP, пояс, политика инструментов
└── skills/secretary/       навык агента: как работать с календарём и заметками
scripts/
├── authorize_google.py     разовый вход в Google
└── openclaw.sh             команды OpenClaw в контейнере шлюза
tests/                      pytest: напоминания, поиск, клиент Google, движок с фейками, API и MCP
```

SQLite работает в одном выделенном потоке: соединение не переходит между потоками, записи упорядочены без своих блокировок. Google Calendar вызывается через общий `httpx.AsyncClient`. google-auth только загружает учётные данные и обновляет access-токен в потоке под блокировкой: `google-api-python-client` синхронный и не потокобезопасный.

## Разработка

```bash
uv sync
uv run pytest -q                           # тесты без сети и ключей
cp .env.example .env                       # DATA_DIR=./data/assistant для локального запуска
uv run fastapi dev app/main.py             # http://localhost:8000/docs
```

## Ограничения

- Один календарь (`GOOGLE_CALENDAR_ID`) и один владелец.
- Перенос времени через id серии меняет начало серии целиком; одно вхождение переносится по его собственному id.
- Полнотекстовый поиск угадывает основу слова отсечением окончаний: это эвристика, не морфология. Семантический поиск закрывает остальное, если задан `OPENAI_API_KEY`.
- Синтаксис CLI и конфига OpenClaw проверен на версии 2026.10.9. Если команды изменятся, зафиксируйте `OPENCLAW_IMAGE` на этой версии.
