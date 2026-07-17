#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите установщик через sudo: sudo bash install.sh"
  exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/mc-admin-bot"
ENV_FILE="/etc/mc-admin-bot.env"
BOT_USER="mcadminbot"
SERVICE="server2-vanilla.service"

for cmd in python3 systemctl visudo; do
  command -v "$cmd" >/dev/null || { echo "Не найдена команда: $cmd"; exit 1; }
done

if ! id "$BOT_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$BOT_USER"
fi
usermod -aG systemd-journal "$BOT_USER"

install -d -m 0755 "$INSTALL_DIR"
install -m 0644 "$SOURCE_DIR/bot.py" "$INSTALL_DIR/bot.py"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
install -m 0644 "$SOURCE_DIR/README.md" "$INSTALL_DIR/README.md"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
chown -R root:root "$INSTALL_DIR"
chmod 0755 "$INSTALL_DIR/bot.py"

read -r -s -p "Токен от @BotFather: " BOT_TOKEN
echo
read -r -p "Telegram ID админов через запятую: " ADMIN_IDS
read -r -s -p "RCON-пароль из server.properties: " RCON_PASSWORD
echo
read -r -p "RCON-порт [25578]: " RCON_PORT
RCON_PORT="${RCON_PORT:-25578}"

[[ "$BOT_TOKEN" == *:* ]] || { echo "Токен выглядит неверно"; exit 1; }
[[ "$ADMIN_IDS" =~ ^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$ ]] || { echo "ADMIN_IDS должны быть числами через запятую"; exit 1; }
[[ "$RCON_PORT" =~ ^[0-9]+$ ]] || { echo "RCON-порт должен быть числом"; exit 1; }
[[ -n "$RCON_PASSWORD" ]] || { echo "RCON-пароль пустой"; exit 1; }

escape_env() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "$value"
}

cat > "$ENV_FILE" <<EOF
BOT_TOKEN="$(escape_env "$BOT_TOKEN")"
ADMIN_IDS="$(escape_env "$ADMIN_IDS")"
MC_SERVICE="$SERVICE"
RCON_HOST="127.0.0.1"
RCON_PORT="$RCON_PORT"
RCON_PASSWORD="$(escape_env "$RCON_PASSWORD")"
CONSOLE_LINES="40"
AUDIT_LOG="/var/lib/mc-admin-bot/audit.log"
EOF
chown root:mcadminbot "$ENV_FILE"
chmod 0640 "$ENV_FILE"

install -m 0644 "$SOURCE_DIR/mc-admin-bot.service" /etc/systemd/system/mc-admin-bot.service
install -m 0440 "$SOURCE_DIR/sudoers-mc-admin-bot" /etc/sudoers.d/mc-admin-bot
visudo -cf /etc/sudoers.d/mc-admin-bot

systemctl daemon-reload
systemctl enable --now mc-admin-bot.service

echo
echo "Готово. Статус бота:"
systemctl --no-pager --full status mc-admin-bot.service || true
echo
echo "Логи бота: sudo journalctl -fu mc-admin-bot.service"
echo "Не забудьте: enable-rcon=true, rcon.port=$RCON_PORT и тот же rcon.password в server.properties."
