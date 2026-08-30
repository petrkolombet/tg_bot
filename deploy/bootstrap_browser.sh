#!/usr/bin/env bash
# Установка браузерного демона TG-бота (playwright chromium-headless-shell).
#
# Делает всё на свежем сервере:
#   1. ставит системные библиотеки chromium (точечный список, --no-install-recommends);
#   2. ставит pikage playwright через pip (если ещё нет);
#   3. ставит chromium-headless-shell (`playwright install --only-shell chromium`);
#   4. создаёт /etc/tg-browser.env — прокси берёт из текущего окружения;
#   5. ставит unit из deploy/tg-browser.service (пути → реальный путь репо);
#   6. включает и запускает сервис.
#
# Требуется root. Запуск:  bash deploy/bootstrap_browser.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE="tg-browser"
UNIT_SRC="$SCRIPT_DIR/$SERVICE.service"
UNIT_DST="/etc/systemd/system/$SERVICE.service"
ENV_FILE="/etc/$SERVICE.env"

if [[ $EUID -ne 0 ]]; then
    echo "Нужны права root (запусти как root или через sudo)." >&2
    exit 1
fi
command -v python3 >/dev/null || { echo "Не найден python3" >&2; exit 1; }

echo "== Республика позиции: $REPO_DIR"

echo "== apt-библиотеки chromium"
apt-get update -y
apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libasound2t64 libatk1.0-0t64 libatspi2.0-0t64 \
    libdbus-1-3 libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libxcursor1 libcups2t64 libpango-1.0-0 \
    libxshmfence1 libglib2.0-0t64 libatk-bridge2.0-0t64 libcairo2 \
    fonts-liberation

echo "== pip: playwright"
if ! python3 -c "import playwright" 2>/dev/null; then
    python3 -m pip install --no-cache-dir --break-system-packages playwright \
        || python3 -m pip install --no-cache-dir playwright
else
    echo "playwright уже установлен — пропускаю"
fi

echo "== chromium-headless-shell"
python3 -m playwright install --only-shell chromium

echo "== $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
    echo "уже существует — не перезаписываю (может содержать кастомный прокси)"
else
    local_proxy="${https_proxy:-${http_proxy:-${HTTPS_PROXY:-${HTTP_PROXY:-}}}}"
    {
        echo "# Прокси для браузерного демона. Сгенерирован bootstrap_browser.sh."
        echo "# ВАЖНО: не используйте символ % в логине/пароле — systemd его не переживёт."
        if [[ -n "$local_proxy" ]]; then
            echo "http_proxy=$local_proxy"
            echo "https_proxy=$local_proxy"
        else
            echo "# Прокси в окружении не было — демон пойдёт напрямую."
        fi
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "создан: $ENV_FILE"
fi

echo "== unit из шаблона (подстановка пути репо)"
sed -e "s|/root/tg_bot|$REPO_DIR|g" "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"

echo "== systemd"
systemctl daemon-reload
systemctl enable --now "$SERVICE.service"
sleep 1
systemctl is-active "$SERVICE.service"
echo "Готово: $SERVICE активен."