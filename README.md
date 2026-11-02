# Personal Assistant

ИИ-секретарь в Telegram: принимает текст и голосовые, ведёт события Google Calendar, сохраняет заметки, идеи и небольшие задачи в Obsidian и напоминает о них. Голосовые распознаёт внешний Whisper-сервер, затем текст получает агент [OpenClaw](https://docs.openclaw.ai) на моделях OpenAI. Агент вызывает этот сервис по MCP, а напоминания сервис шлёт в Telegram сам, через того же бота.

```
 Вы ⇄ Telegram-бот (@BotFather)
          │ long polling
          ▼
   OpenClaw gateway ── модель OpenAI, навык secretary, утренняя сводка (automation)
          ├─────────────► Whisper API: голосовое → текст для ассистента
          │ MCP: http://personal-assistant:8000/mcp/ + Bearer AUTH_TOKEN
          ▼
   personal-assistant ──────► Google Calendar API
     ├─────────────────────► Secretary/*.md + assistant-state/*.json
     │  RAM: кэш календаря; задачи Obsidian имеют постоянные отметки доставки
     └──────────────────────► Telegram Bot API: напоминания от имени того же бота

 Windows / Android Obsidian ⇄ Remotely Save ⇄ HTTPS WebDAV ⇄ полный vault на VPS
                                                             ├── Secretary (ассистент)
                                                             └── Personal (только владелец)
```

Напоминания живут в сервисе, чтобы работать, когда модель недоступна, и не тратить токены на каждую проверку. Утреннюю сводку, где нужен «ум», делает OpenClaw. Источник событий — Google Calendar; его кэш и отметки отправки находятся в RAM. Задачи Obsidian хранятся в Markdown, их отметки доставки — в постоянных JSON-файлах.

## Возможности

- **События.** Создание, перенос, удаление, повторяющиеся события (RRULE), события на весь день. Время агент передаёт в вашем часовом поясе.
- **Голосовые.** OpenClaw отправляет аудио из Telegram на внешний Whisper, а распознанный текст обрабатывает как обычное сообщение владельца.
- **Напоминания в Telegram.** За `REMINDER_MINUTES` до начала (по умолчанию 15 минут). Если у события в Google настроены свои уведомления, берутся они. Важные события получают ещё напоминания за сутки и за два часа (`IMPORTANT_REMINDER_MINUTES`). В напоминании - название, время, место и ссылка на событие.
- **Важные события** помечаются красным цветом в Google Calendar (`IMPORTANT_COLOR_ID`). Так их видно в календаре, и событие можно сделать важным прямо из интерфейса Google, сменив цвет.
- **Утренняя сводка** - автоматизация OpenClaw: события дня и важное на неделю.
- **Заметки Obsidian.** Inbox, идеи, задачи и справочные записи в обычном Markdown. Поиск возвращает максимум пять коротких результатов, чтение — выбранный фрагмент. Изменения проверяют версию файла, постоянное удаление заменено архивированием.
- **Напоминания о задачах.** Явное `remind_at` проверяет Python без LLM и создания события Calendar; отметки сохраняются после перезапуска. Срок `due_at` сам по себе не включает уведомление.
- **Windows и Android.** Опциональный HTTPS WebDAV и Remotely Save синхронизируют полный vault; Python видит только Secretary, OpenClaw не получает mount vault.
- **Интернет.** Поиск DuckDuckGo без отдельного поискового API-ключа и чтение публичных страниц. Ассистент может искать по просьбе и для актуальных вопросов, указывая источники; объём страниц ограничен.

## Obsidian на VPS

Подключение Remotely Save, HTTPS и авторизация, соответствие путей, формат Markdown,
конфликты, резервирование и миграция описаны в [docs/obsidian.md](docs/obsidian.md).
Перед обновлением существующего VPS подготовьте новые каталоги по инструкции:
Compose не создаёт пустой vault вместо отсутствующего bind mount. WebDAV включается
отдельным профилем `obsidian-sync`, а работа с заметками в backend не требует Google Calendar.

Поиск DuckDuckGo требует установки плагина в OpenClaw перед применением патча.
Подключение к существующему VPS, ограничения и диагностика описаны в [docs/web-search.md](docs/web-search.md).

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
mkdir -p data/openclaw secrets data/assistant-state
mkdir -p data/obsidian/Secretary/{Inbox,Tasks,Ideas,Knowledge,Archive} data/obsidian/Personal
chmod 700 data/assistant-state
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

# Поисковый плагин: без API-ключа; --pin сохраняет точную установленную версию.
# Установите и включите ДО применения патча, в котором выбран provider=duckduckgo.
scripts/openclaw.sh plugins install @openclaw/duckduckgo-plugin --pin
scripts/openclaw.sh plugins enable duckduckgo

# Telegram только для вас, MCP-сервер, часовой пояс, ограничение инструментов агента
TG_ID=123456789     # ваш user id, тот же, что TELEGRAM_CHAT_ID
sed "s/<YOUR_TELEGRAM_USER_ID>/$TG_ID/g" openclaw/openclaw.patch.json5 | scripts/openclaw.sh config patch --stdin
scripts/openclaw.sh config validate

# Навык секретаря
mkdir -p data/openclaw/workspace/skills && cp -r openclaw/skills/secretary data/openclaw/workspace/skills/
sudo chown -R 1000:1000 data/openclaw

docker compose up -d
scripts/openclaw.sh mcp probe personal-assistant      # Calendar + bounded note tools
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

Проверка: напишите боту «что у меня завтра?», «поставь на пятницу в 15:00 созвон с Петей, это важно», «перенеси созвон на 16:00».

## Голосовые через внешний Whisper

OpenClaw скачивает голосовое Telegram и перед обработкой сообщения вызывает `openclaw/whisper/transcribe.mjs`. Скрипт загружает файл на `https://whisper.a16.ainergy.pro:8443/v1/audio/transcriptions`, извлекает `text` из JSON-ответа и передаёт в OpenClaw обычный текст. После этого ассистент отвечает или вызывает инструменты календаря. Отдельный Telegram-бот и новые MCP-инструменты для распознавания не нужны.

Запрос использует `multipart/form-data`: `file`, `model=whisper-1`, `priority=0`, `response_format=json`, `language=ru`, `batch_size=4`, `vad_enabled=true`, `word_timestamps=false`. Размер аудио ограничен 20 МиБ, HTTP-запрос — 120 секундами, весь запуск обработчика — 150 секундами. Сервер должен принимать формат голосовых Telegram OGG/Opus.

Параметры в `.env` относятся только к шлюзу OpenClaw:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `WHISPER_URL` | `https://whisper.a16.ainergy.pro:8443/v1/audio/transcriptions` | Полный адрес распознавания |
| `WHISPER_MODEL` | `whisper-1` | Имя модели в запросе |
| `WHISPER_LANGUAGE` | `ru` | Язык аудио |
| `WHISPER_API_KEY` | пусто | Необязательный Bearer-токен, если сервер потребует авторизацию |

Без `WHISPER_API_KEY` запрос идёт без заголовка `Authorization`. Скрипт не читает `OPENAI_API_KEY`. При заданном `HTTPS_PROXY` Node использует настройки прокси; для прямого соединения с Whisper добавьте `whisper.a16.ainergy.pro` к существующему `NO_PROXY`, например `NO_PROXY=personal-assistant,localhost,127.0.0.1,whisper.a16.ainergy.pro`.

Для уже развёрнутого сервера сначала перенесите на него обновлённые файлы проекта, включая `openclaw/whisper/transcribe.mjs` и Compose. Затем выполните в каталоге проекта:

```bash
# Пересоздание добавит read-only mount обработчика и переменные Whisper
docker compose up -d --force-recreate openclaw-gateway

# Полный актуальный патч также включает DuckDuckGo: сначала установите поисковый плагин.
# Повторная установка не нужна, если плагин уже установлен и включён.
scripts/openclaw.sh plugins install @openclaw/duckduckgo-plugin --pin
scripts/openclaw.sh plugins enable duckduckgo

# Применение полного патча: подставьте тот же id, что TELEGRAM_CHAT_ID
TG_ID=123456789
sed "s/<YOUR_TELEGRAM_USER_ID>/$TG_ID/g" openclaw/openclaw.patch.json5 | scripts/openclaw.sh config patch --stdin
scripts/openclaw.sh config validate

# Обновление инструкций ассистента
mkdir -p data/openclaw/workspace/skills && cp -r openclaw/skills/secretary data/openclaw/workspace/skills/
sudo chown -R 1000:1000 data/openclaw
```

Изменения в исходниках сами по себе не меняют конфигурацию на внешнем сервере. Обработчик монтируется в шлюз как `/opt/secretary/transcribe-whisper.mjs` и запускается штатным CLI-процессором аудио OpenClaw; профиль инструментов агента остаётся `messaging`.

В списке распознавания задан только этот обработчик, без резервного облачного STT. Обработка медиа в OpenClaw выполняется по возможности: сбой Whisper не останавливает весь диалог. Если расшифровка отсутствует, навык предлагает повторить голосовое или написать текстом.

## Инструменты агента (MCP)

Эндпоинт `POST /mcp/` (Streamable HTTP, stateless, JSON-ответы), авторизация `Authorization: Bearer <AUTH_TOKEN>`.

| Инструмент | Что делает |
|---|---|
| `list_events` | События периода (по умолчанию сегодня и неделя вперёд) и текущее время `now` |
| `create_event` | Новое событие: время, длительность, место, `important`, `remind_minutes`, `recurrence` |
| `update_event` | Меняет только переданные поля; при переносе длительность сохраняется |
| `delete_event` | Удаляет событие; `recurring_event_id` удаляет всю серию |
| `search_notes` | Поиск и фильтры заметок, максимум 5 коротких результатов; без Archive по умолчанию |
| `read_note` | Выбранный UUID, раздел или фрагмент до 4000 символов с версией и источником |
| `create_note` | Создать note/task/idea/knowledge; вернуть короткую квитанцию |
| `append_note` / `update_note` | Дополнить/изменить запись, проверив `expected_version` |
| `archive_note` | Архивировать одну запись с проверкой версии |
| `notes_status` | Текущее время и диагностика индекса/доставки без содержимого заметок |

Ошибки предметной области («Event not found», «Event must end after it starts») доходят до модели текстом: она может исправить вызов.

## REST API

Те же операции для отладки и других клиентов, Swagger - на `/docs`. Авторизация та же: без токена 401, с неверным 403.

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/v1/events?start=&end=&q=&limit=` | События периода; `q` фильтрует их средствами Google Calendar |
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
| 400 | Неверное время, конец раньше начала |
| 401 / 403 | Нет токена / неверный токен |
| 404 | Нет события с таким id |
| 422 | Неверное тело запроса; Google отклонил событие (например, неверный RRULE) |
| 502 | Ошибка Google Calendar, нет доступа к календарю, ошибка обновления токена |
| 503 | Календарь не настроен (`GOOGLE_CREDENTIALS_FILE`) или Google недоступен |
| 504 | Google не ответил вовремя |

## Как работают напоминания

1. Раз в `CALENDAR_SYNC_INTERVAL` секунд сервис получает из Google Calendar события от «сутки назад» до `CALENDAR_SYNC_DAYS` вперёд и обновляет временный кэш в RAM. Повторяющиеся события разворачиваются в отдельные вхождения. При успешном чтении удалённые в Google события из кэша уходят.
2. Каждые 30 секунд сервис ищет события, у которых подошло время напоминания:
   - уведомления по умолчанию в Google → `REMINDER_MINUTES`;
   - свои уведомления события в Google (или `remind_minutes` от агента) → они; пустой список - без них;
   - важное событие → плюс `IMPORTANT_REMINDER_MINUTES`.
   Пустой список отключает обычные напоминания, но не дополнительные у важного события.
3. Напоминание, время которого наступило раньше создания события, пропускается: «за сутки» для встречи, поставленной вечером на утро, не придёт.
4. Если сервис лежал и пропустил несколько напоминаний, придёт одно - ближайшее к началу, и только если событие ещё не началось.
5. Отправленное напоминание помечается в RAM по ключу (событие, время начала, минуты). После переноса события напоминания придут заново. Если Telegram не ответил, сервис попробует отправить напоминание на следующем тике.
6. При перезапуске кэш и отметки отправки теряются. После загрузки событий из Google уже отправленное напоминание может прийти повторно, если событие ещё не началось.

У событий на весь день нет часа, от которого считать минуты. Они попадают в утреннюю сводку.

## Конфигурация

Настройки сервиса задаются переменными окружения (`.env`). Compose передаёт каждому сервису только перечисленные ему ключи; ключ OpenAI получает шлюз OpenClaw. Без Google инструменты календаря выключены. Без Telegram календарные напоминания пишутся в лог, задачи остаются недоставленными. Пути заметок, состояние и WebDAV описаны в [инструкции Obsidian](docs/obsidian.md).

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AUTH_TOKEN` | - | Bearer-токен REST и MCP; при включённых заметках обязателен |
| `TIMEZONE` | `Europe/Moscow` | Часовой пояс владельца |
| `GOOGLE_CREDENTIALS_FILE` | - | Токен `authorized_user` или ключ сервисного аккаунта |
| `GOOGLE_CALENDAR_ID` | `primary` | Календарь; для сервисного аккаунта - e-mail владельца |
| `CALENDAR_SYNC_INTERVAL` | `60` | Период чтения событий Google Calendar в кэш RAM, секунды |
| `CALENDAR_SYNC_DAYS` | `30` | Горизонт чтения календаря вперёд, дни; должен покрывать самое раннее напоминание |
| `IMPORTANT_COLOR_ID` | `11` | Цвет важного события (11 - красный «Томатный») |
| `TELEGRAM_BOT_TOKEN` | - | Токен бота; без него напоминания только пишутся в лог |
| `TELEGRAM_CHAT_ID` | - | Ваш user id; задаётся вместе с токеном |
| `REMINDER_MINUTES` | `15` | Напоминания по умолчанию, минуты через запятую |
| `IMPORTANT_REMINDER_MINUTES` | `1440,120` | Дополнительные напоминания важных событий |
| `LOG_LEVEL` / `LOG_DIAGNOSE` | `INFO` / `False` | Логирование |
| `NOTES_ROOT` / `ASSISTANT_STATE_DIR` | не заданы вне Compose | Secretary и отдельное служебное состояние; задаются вместе, требуют `AUTH_TOKEN` |
| `OBSIDIAN_VAULT_NAME` | `Obsidian` | Имя vault/remote base directory и ссылок Obsidian |
| `NOTE_REMINDER_POLL_SECONDS` | `30` | Период проверки `remind_at` задач |

Переменные OpenClaw (`OPENAI_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`, `OPENCLAW_IMAGE`, `OPENCLAW_TZ`) описаны в `.env.example`. Если OpenAI или Telegram недоступны с сервера напрямую, задайте `HTTPS_PROXY` (и `NO_PROXY=personal-assistant,localhost`): его подхватывают оба контейнера.

Неверная конфигурация не роняет сервис трейсбеком: каждая причина пишется в лог строкой `Bad configuration: ...`, после чего процесс завершается.

## Безопасность

- Бот отвечает только вам: `dmPolicy: "allowlist"` с вашим user id, вы же владелец слэш-команд OpenClaw.
- У агента профиль `messaging` плюс `read` для навыков и `tools.fs.workspaceOnly=true`. Runtime-команды и файловые мутации запрещены, elevated выключен; навык secretary читается из workspace. Vault в gateway не монтируется. Приглашения и заметки могут содержать prompt injection: их текст считается данными. Перед удалением события и массовыми изменениями агент спрашивает подтверждение.
- Для интернета отдельно разрешены `web_search` и `web_fetch`. Частные сети для чтения страниц не разрешаются; текст сайтов не даёт права менять календарь или заметки. Поисковые запросы должны обходиться без личных данных.
- Backend получает только Secretary и отдельный assistant-state. MCP не принимает произвольные пути; Personal, .obsidian и служебная история не выдаются. Заметки не добавляются в автоматически загружаемую память OpenClaw.
- Google-доступ ограничен событиями (`calendar.events`): ни списка календарей, ни прав доступа.
- Порты сервиса (8090) и Control UI OpenClaw (18789) опубликованы только на `127.0.0.1`. Control UI открывайте через `ssh -L 18789:localhost:18789 <сервер>`.
- Токены не пишутся в лог; логгер httpx заглушён, потому что URL Bot API содержит токен бота.

## Устройство

```
app/
├── main.py                 настройки → логирование → FastAPI → /health → REST → /mcp
├── api_v1/
│   ├── views.py            REST /v1/events
│   ├── tools.py            MCP-инструменты агента и McpEndpoint
│   ├── note_tools.py       ограниченные MCP-инструменты заметок
│   ├── schemas.py          pydantic-контракт API и инструментов
│   └── errors.py           AppError → {"detail": ...}
├── core/
│   ├── engine.py           AssistantEngine: операции с календарём
│   ├── scheduler.py        чтение календаря, кэш в RAM и отправка напоминаний
│   ├── notes/              Markdown/frontmatter, ограниченный поиск, версии и конфликты
│   ├── note_reminders.py   задачи Obsidian и постоянные JSON-отметки доставки
│   ├── reminders.py        какие напоминания пора отправить и их текст
│   ├── clock.py            разбор локального времени агента
│   ├── upstream/           клиенты Google Calendar и Telegram Bot API
│   ├── auth/               Bearer для REST (Depends) и для смонтированного MCP (ASGI)
│   ├── config/             Settings (env) и constants.py (лимиты)
│   ├── entities.py         Event, Reminder
│   └── exceptions.py       AppError и наследники
└── utils/                  lifespan (движок + MCP-сервер), логирование
openclaw/
├── openclaw.patch.json5    патч конфига OpenClaw: Telegram, MCP, пояс, политика инструментов
├── whisper/transcribe.mjs  отправка голосовых внешнему Whisper и извлечение текста
└── skills/secretary/       навык агента: как работать с календарём
scripts/
├── authorize_google.py     разовый вход в Google
└── openclaw.sh             команды OpenClaw в контейнере шлюза
tests/                      pytest: напоминания, клиент Google, движок с фейками, API и MCP
deploy/webdav/              WsgiDAV, шаблон авторизации и HTTPS Caddy
docs/obsidian.md             настройка Windows/Android, безопасность и резервирование
```

Google Calendar вызывается через общий `httpx.AsyncClient`. google-auth загружает учётные данные и обновляет access-токен в потоке под блокировкой. Календарные события и их отметки на диск не сохраняются; заметки и отметки задач Obsidian постоянны.

## Разработка

```bash
uv sync
uv run pytest -q                           # тесты без сети и ключей
cp .env.example .env
uv run fastapi dev app/main.py             # http://localhost:8000/docs
```

## Ограничения

- Один календарь (`GOOGLE_CALENDAR_ID`) и один владелец.
- Перенос времени через id серии меняет начало серии целиком; одно вхождение переносится по его собственному id.
- После перезапуска до начала события уже отправленные напоминания могут повториться: отметки хранятся только в RAM.
- Для задач Obsidian отметки постоянны, но авария между приёмом сообщения Telegram и сохранением отметки может вызвать дубль. Запускайте один backend worker с одним каталогом состояния.
- Remotely Save синхронизирует только при работающем Obsidian; телефон может приостанавливать приложение. Синхронизация не заменяет резервную копию и не гарантирует слияние одновременных офлайн-правок.
- DuckDuckGo используется через экспериментальный HTML-поиск: возможны CAPTCHA и изменения разметки. `web_fetch` не исполняет JavaScript; страницы с авторизацией или сложным интерфейсом могут не читаться. Поискового API-ключа не требуется, токены модели расходуются как обычно.
- Синтаксис CLI и конфига OpenClaw проверен на версии 2026.10.9. Если команды изменятся, зафиксируйте `OPENCLAW_IMAGE` на этой версии.
