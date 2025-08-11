#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/lordot/proxysmart_monitoring"
WORKDIR="$(mktemp -d -t proxysmart-XXXXXXXX)"
INSTALL_DIR="/usr/local/bin"
CRON_DIR="/etc/cron.d"
CRON_FILE="${CRON_DIR}/proxysmart_monitoring"
VARS=(MG_USER MG_PASSWORD MG_TG_TOKEN MG_TG_CHAT)

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "Запустите скрипт с правами root (sudo)." >&2
    exit 1
  fi
}

pkg_install() {
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip git cron ca-certificates
}

have_line_in_file() {
  local key="$1" file="$2"
  grep -q -E "^${key}=" "$file" 2>/dev/null || return 1
}

escape_env_value() {
  python3 - <<'PY'
import sys
v=sys.stdin.read()
v=v.replace("\\","\\\\").replace('"','\\"').rstrip("\n")
print(v,end="")
PY
}

ensure_env_permanent() {
  touch /etc/environment
  chmod 0644 /etc/environment

  for var in "${VARS[@]}"; do
    if have_line_in_file "$var" /etc/environment; then
      echo "[env] ${var} уже задан в /etc/environment — пропускаю."
      val="$(grep -E "^${var}=" /etc/environment | head -n1 | sed -E 's/^[^=]+=//; s/^"//; s/"$//')"
      export "${var}=${val}"
      continue
    fi

    if [[ -n "${!var-}" ]]; then
      echo "[env] Использую текущую переменную ${var} из окружения."
      val="${!var}"
    else
      prompt="${var} не задан. Введите значение:"
      case "$var" in
        MG_PASSWORD|MG_TG_TOKEN) read -rsp "$prompt " val; echo ;;
        *)                        read -rp  "$prompt " val ;;
      esac
    fi

    esc_val="$(printf "%s" "$val" | escape_env_value)"
    echo "${var}=\"${esc_val}\"" >> /etc/environment
    export "${var}=${val}"
    echo "[env] Записал ${var} в /etc/environment"
  done
}

clone_repo() {
  echo "[git] Клонирую репозиторий в ${WORKDIR}"
  git clone --depth=1 "$REPO_URL" "$WORKDIR/repo"
}

install_requirements() {
  local req="$WORKDIR/repo/requirements.txt"
  if [[ -f "$req" ]]; then
    echo "[pip] Устанавливаю зависимости из requirements.txt (системно)"
    BSP_FLAG=""
    if python3 - <<'PY'
import sys, subprocess
try:
    out = subprocess.check_output([sys.executable, "-m", "pip", "help", "install"], text=True)
    print("--break-system-packages" if "--break-system-packages" in out else "")
except Exception:
    pass
PY
    then
      BSP_FLAG="--break-system-packages"
    fi
    python3 -m pip install -r "$req" $BSP_FLAG
  else
    echo "[pip] requirements.txt не найден — пропускаю."
  fi
}

copy_scripts() {
  shopt -s nullglob
  for f in "$WORKDIR"/repo/*.py; do
    # добавим shebang, если его нет (чтобы можно было вызывать как исполняемый файл)
    if ! head -n1 "$f" | grep -qE '^#!'; then
      echo "[bin] Добавляю shebang в $(basename "$f")"
      tmpf="$(mktemp)"
      { echo '#!/usr/bin/env python3'; cat "$f"; } > "$tmpf"
      mv "$tmpf" "$f"
    fi
    install -m 0755 "$f" "$INSTALL_DIR/"
    echo "[bin] Установил $INSTALL_DIR/$(basename "$f")"
  done
}

install_cron() {
  local repo_cron="$WORKDIR/repo/crontab"
  local BEGIN="### PROXYSMART-BEGIN"
  local END="### PROXYSMART-END"
  local TMP="$(mktemp)"

  # Текущий crontab без нашего блока
  crontab -u root -l 2>/dev/null \
    | awk -v b="$BEGIN" -v e="$END" '
        $0==b {inb=1; next}
        $0==e {inb=0; next}
        !inb {print}
      ' > "$TMP" || true

  # Добавляем новый блок
  {
    echo "$BEGIN"
    sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$repo_cron"
    echo "$END"
  } >> "$TMP"

  crontab -u root "$TMP"
  rm -f "$TMP"
  echo "[cron] Блок PROXYSMART обновлён"
}


cleanup() { rm -rf "$WORKDIR"; }

main() {
  require_root
  pkg_install
  ensure_env_permanent
  clone_repo
  install_requirements
  copy_scripts
  install_cron
  cleanup
  echo "Готово ✅"
  echo "Cron-файл:   ${CRON_FILE}"
  echo "Скрипты:     ${INSTALL_DIR}/*.py (исполняемые)"
  echo "Переменные:  /etc/environment (для новых сессий)."
}

main "$@"
