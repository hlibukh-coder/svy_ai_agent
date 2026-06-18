# СВЮ.КЛУБ — AI Telegram Manager

AI-менеджер для компании СВЮ.КЛУБ (продажа крепежа и метизов). Работает через реальный Telegram аккаунт (Telethon), общается с клиентами как живой менеджер, берёт данные из BAS (1С-подобная система) и создаёт заказы.

## Стек

- Python 3.11
- [Telethon](https://github.com/LonamiWebs/Telethon) — Telegram MTProto клиент
- OpenAI GPT-4o с function calling
- aiosqlite — история диалогов
- aiohttp — BAS OData запросы
- APScheduler — проактивные напоминания
- python-dotenv — конфигурация

## Структура

```
src/
  index.py      — главный файл, запуск, обработка сообщений
  prompt.py     — системный промпт + мок данные
  tools.py      — tools для OpenAI + execute_tool
  bas.py        — слой BAS OData (моки + реальные запросы)
  context.py    — SQLite история диалогов
  scheduler.py  — проактивные сообщения (APScheduler cron 10:00/10:15/10:30)
tests/
  test_prompt.py
  test_bas.py
  test_context.py
  test_tools.py
  test_scheduler.py
```

## Быстрый старт

### Локально

```bash
pip install -r requirements.txt
cp .env.example .env   # заполни переменные
python -m src.index
```

### Docker

```bash
docker compose up --build
```

При первом запуске Telethon запросит код подтверждения — введи его в терминал. Сессия сохраняется в `session/`.

## Переменные окружения

| Переменная         | Описание                                      |
|--------------------|-----------------------------------------------|
| `TG_API_ID`        | Telegram API ID (my.telegram.org)             |
| `TG_API_HASH`      | Telegram API Hash                             |
| `TG_PHONE`         | Номер телефона аккаунта (+380...)             |
| `MANAGER_TG_ID`    | Telegram ID менеджера (не отвечать себе)      |
| `ESCALATION_CHAT_ID` | Chat ID для эскалаций                       |
| `OPENAI_API_KEY`   | OpenAI API ключ                               |
| `BAS_URL`          | URL BAS OData endpoint                        |
| `BAS_USER`         | Логин BAS                                     |
| `BAS_PASS`         | Пароль BAS                                    |
| `USE_MOCK`         | `true` — использовать мок данные              |
| `DB_PATH`          | Путь к SQLite базе (default: `data/history.db`) |

## Тесты

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

50 тестов покрывают: BAS слой, SQLite историю, системный промпт, tools/execute_tool, scheduler логику.

## Бизнес-логика

- **Постоянный клиент** (есть в BAS + есть заказы) — не спрашивает данные которые уже знает, предлагает повторить последний заказ
- **Известный контакт** (есть в BAS, заказов нет) — помогает с товаром
- **Новый лид** (нет в BAS) — сначала помогает с товаром, реквизиты спрашивает только перед заказом
- **Эскалация** — при жалобе, запросе скидки/отсрочки, просьбе живого менеджера — пересылает в `ESCALATION_CHAT_ID`
- **Проактивные напоминания** (10:00) — проверяет клиентов у которых подходит время повторного заказа
- **Возврат потерянных клиентов** (10:15) — пишет клиентам которые не покупали более 60 дней
- **Уведомления о новых товарах** (10:30) — персонально сообщает клиентам о новинках на основе истории покупок
- **Статус заказа** — `get_order_status` по ID заказа из истории BAS
- **Уточнение у поставщика** — `check_supplier` если товара нет на складе или недостаточно
