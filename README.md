# skupgotsms

Telegram-бот для [gotsms.org](https://app.gotsms.org) с фичей **автобай**: жадно выкупает номера выбранного сервиса пока хватает баланса. Single-user — пускает только тебя по `TELEGRAM_USER_ID`.

## Что умеет
- 💰 Баланс
- 🛒 Разовая покупка (выбор сервиса → плана → кнопка)
- 🤖 **Автобай**: создаёшь job на конкретный план, скрипт каждые N минут (по умолчанию 5, настраивается через бот) дёргает API и покупает номера пока `balance >= price`. На каждую покупку шлёт уведомление с номером.
- 📱 Список активных номеров
- 📨 Непрочитанные SMS (помечаются прочитанными при просмотре)

## Стек
Python 3.12, aiogram 3, APScheduler, httpx, aiosqlite.

## Установка

### 1. Получи токены
- Telegram-бот: создать у [@BotFather](https://t.me/BotFather), забрать токен.
- Свой Telegram user id: написать [@userinfobot](https://t.me/userinfobot).
- gotsms API-токен: профиль на app.gotsms.org → включить API → скопировать токен.

### 2. Локально
```bash
git clone https://github.com/<you>/skupgotsms.git
cd skupgotsms
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env
python main.py
```

### 3. Docker
```bash
cp .env.example .env
# отредактируй .env
docker compose up -d --build
docker compose logs -f
```

### 4. systemd на VPS
```bash
sudo useradd -r -s /usr/sbin/nologin skupgotsms
sudo mkdir -p /opt/skupgotsms
sudo chown skupgotsms: /opt/skupgotsms
sudo -u skupgotsms git clone https://github.com/<you>/skupgotsms.git /opt/skupgotsms
cd /opt/skupgotsms
sudo -u skupgotsms python3.12 -m venv .venv
sudo -u skupgotsms .venv/bin/pip install -r requirements.txt
sudo -u skupgotsms cp .env.example .env
sudo -u skupgotsms nano .env
sudo cp systemd/skupgotsms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skupgotsms
sudo journalctl -u skupgotsms -f
```

## Использование
1. `/start` в боте.
2. **🤖 Автобай → ➕ Новый автобай** → выбираешь сервис → выбираешь план (страна, длительность, цена). Job создаётся и сразу запускается (первый тик — мгновенно, дальше — каждые 5 мин).
3. Тапни по job в списке чтобы выключить, изменить интервал или удалить.

Все автобаи переживают рестарт — состояние в SQLite (`data/skupgotsms.sqlite`).

## Замечания
- Автобай выкупает **последовательно в одном тике** до исчерпания баланса. При желании можно вынести лимит на тик в настройки.
- Цена плана подтягивается с сервера на каждом тике, поэтому подорожание не повредит.
- На 401/403 от API автобай автоматически отключается.

## Конфиг
Все переменные в `.env` (см. `.env.example`):
- `TELEGRAM_BOT_TOKEN` — токен бота
- `TELEGRAM_USER_ID` — твой Telegram id (других пользователей бот игнорирует)
- `GOTSMS_API_TOKEN` — токен gotsms.org
- `GOTSMS_BASE_URL` — по умолчанию `https://app.gotsms.org`
- `DB_PATH` — путь к SQLite (по умолчанию `data/skupgotsms.sqlite`)
- `DEFAULT_AUTOBUY_INTERVAL_MIN` — дефолтный интервал автобая в минутах (по умолчанию 5)
