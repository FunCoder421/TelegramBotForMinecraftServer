# Minecraft Telegram Admin Bot

Telegram-бот для безопасного управления одним Minecraft-сервером через `systemd` и RCON.

## Возможности

- Авторизация только по числовому Telegram ID.
- Работает только в личном чате с ботом.
- Запуск, остановка и перезапуск `server2-vanilla.service`.
- Подтверждение перед остановкой и перезапуском.
- Статус systemd, PID, память, CPU-время, число рестартов и проверка RCON.
- Последние 5–100 строк консоли через `journalctl`.
- Обновление консоли кнопкой.
- Любые Minecraft-команды через локальный RCON.
- Журнал действий администраторов с ротацией.
- Никаких произвольных Bash-команд из Telegram.

## 1. Создать Telegram-бота

1. В телеграмме найдите `@BotFather`.
2. Выполните команду `/newbot`.
3. Сохраните токен.
4. Узнать свой ID можно командой `/id` у уже запущенного бота.

## 2. Включение RCON на Minecraft-сервере

```bash
nano /srv/servers/server2/vanilla/server.properties
```

Установите:

```properties
enable-rcon=true
rcon.port=25578
rcon.password=СЛОЖНЫЙ_ДЛИННЫЙ_ПАРОЛЬ
broadcast-rcon-to-ops=false
```

Перезапустите Minecraft-сервер:

```bash
sudo systemctl restart server2-vanilla.service
```

RCON должен использоваться только локально. Не открывате порт `25578` в роутере. При включённом UFW можно явно закрыть входящие подключения:

```bash
sudo ufw deny 25578/tcp
```

## 3. Установка бота

Распакуте архив, перейдите в папку и выполните:

```bash
sudo apt update
sudo apt install -y python3 python3-venv sudo
sudo bash install.sh
```

Установщик спросит токен от BotFather, Telegram ID админов, RCON-пароль и порт.

## 4. Проверка

```bash
sudo systemctl status mc-admin-bot.service
sudo journalctl -fu mc-admin-bot.service
```

Перезапуск бота:

```bash
sudo systemctl restart mc-admin-bot.service
```

## Команды Telegram

```text
/start                меню
/id                   показать Telegram ID
/status               статус сервера
/console 40           последние строки консоли, от 5 до 100
/cmd list             выполнить Minecraft-команду
/players              список игроков
/say текст            сообщение игрокам
/start_server         запустить сервер
/stop_server          остановить с подтверждением
/restart_server       перезапустить с подтверждением
```

## Добавить или удалить администратора

```bash
sudo nano /etc/mc-admin-bot.env
```

Пример:

```ini
ADMIN_IDS="111111111,222222222"
```

После изменения:

```bash
sudo systemctl restart mc-admin-bot.service
```

## Безопасность

- Доступ проверяется по `message.from_user.id`, а не по изменяемому username.
- Бот не принимает Linux-команды.
- В `sudoers` разрешены только три точные операции с одним systemd-сервисом.
- Пароль RCON и токен хранятся в `/etc/mc-admin-bot.env` с правами `0640`.
- Все административные действия записываются в `/var/lib/mc-admin-bot/audit.log`.

Просмотр журнала действий:

```bash
sudo tail -f /var/lib/mc-admin-bot/audit.log
```
