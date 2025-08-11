#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/lordot/proxysmart_monitoring"
WORKDIR="$(mktemp -d -t proxysmart-XXXXXXXX)"
INSTALL_DIR="/usr/local/bin"
STATE_DIR="/var/lib/proxysmart"
LOG_DIR="/var/log"
VARS=(MG_USER MG_PASSWORD MG_TG_TOKEN MG_TG_CHAT)

trap 'rm -rf "$WORKDIR"' EXIT

require_root() {
  [[ $EUID -eq 0 ]] || { echo "Запустите скрипт с sudo/root."; exit 1; }
}

pkg_install() {
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip git cron ca-certificates
}

# --- Чтение значений (env -> /dev/tty) ---
read_var_value() {
  local var="$1"
  # 1) из окружения процесса?
  if [[ -n "${!var-}" ]]; then
    printf '%s' "${!var}"
    return 0
  fi
  # 2) спросим через /dev/tty
  if [[ -e /dev/tty && -r /dev/tty ]]; then
    local val=""
    while :; do
      case "$var" in
        MG_PASSWORD|MG_TG_TOKEN)
          printf "%s " "${var} не задан. Введите значение:" > /dev/tty
          stty -F /dev/tty -echo 2>/dev/null || true
          IFS= read -r val < /dev/tty || val=""
          stty -F /dev/tty echo 2>/dev/null || true
          echo > /dev/tty
          ;;
        *)
          printf "%s " "${var} не задан. Введите значение:" > /dev/tty
          IFS= read -r val < /dev/tty || val=""
          ;;
      esac
      [[ -n "$val" ]] && break
      echo "Значение не может быть пустым." > /dev/tty
    done
    printf '%s' "$val"
    return 0
  fi
  echo "[ERR] ${var} не задан и нет TTY. Запустите так:" >&2
  echo "  sudo env MG_USER=... MG_PASSWORD=... MG_TG_TOKEN=... MG_TG_CHAT=... ./install.sh" >&2
  exit 1
}

escape_for_env() {
  python3 - <<'PY'
import sys
v=sys.stdin.read().rstrip("\n").replace("\\","\\\\").replace('"','\\"')
print(v,end="")
PY
}

ensure_env_permanent() {
  touch /etc/environment
  chmod 0644 /etc/environment

  # собираем гарантированно НЕпустые значения
  declare -A VAL=()
  for v in "${VARS[@]}"; do
    VAL["$v"]="$(read_var_value "$v")"
  done

  # создаём новый файл из старого, вырезая прежние MG_* строки
  local tmp="$(mktemp)"
  # сохраняем все строки, КРОМЕ MG_*
  grep -vE '^(MG_USER|MG_PASSWORD|MG_TG_TOKEN|MG_TG_CHAT)=' /etc/environment 2>/dev/null > "$tmp" || true

  # добавляем наши MG_* строки
  for v in "${VARS[@]}"; do
    esc="$(printf '%s' "${VAL[$v]}" | escape_for_env)"
    printf '%s="%s"\n' "$v" "$esc" >> "$tmp"
    export "$v=${VAL[$v]}"
    echo "[env] $v=${VAL[$v]}"
  done

  # бэкап и атомарная замена
  cp /etc/environment "/etc/environment.bak.$(date +%F_%H-%M-%S)" 2>/dev/null || true
  install -m 0644 -o root -g root "$tmp" /etc/environment
  rm -f "$tmp"

  # вернём значения наружу
  for v in "${VARS[@]}"; do
    printf -v "$v" '%s' "${VAL[$v]}"
  done
}

clone_repo() {
  echo "[git] Клонирую репозиторий в ${WORKDIR}"
  git clone --depth=1 "$REPO_URL" "$WORKDIR/repo"
}

install_requirements() {
  local req=""
  for cand in requirements.txt requeriments.txt requiremetns.txt; do
    [[ -f "$WORKDIR/repo/$cand" ]] && { req="$WORKDIR/repo/$cand"; break; }
  done
  if [[ -n "$req" ]]; then
    echo "[pip] Устанавливаю зависимости из $(basename "$req")"
    local BSP=""
    python3 -m pip help install 2>/dev/null | grep -q -- '--break-system-packages' && BSP="--break-system-packages"
    python3 -m pip install -r "$req" $BSP
  else
    echo "[pip] requirements(.txt) не найден — пропускаю."
  fi
}

prep_fs() {
  mkdir -p "$STATE_DIR"
  chmod 0755 "$STATE_DIR"
  touch "$LOG_DIR/proxysmart_modems_list.log" "$LOG_DIR/proxysmart_modems_check.log"
  chmod 0644 "$LOG_DIR/proxysmart_modems_list.log" "$LOG_DIR/proxysmart_modems_check.log"
}

copy_scripts() {
  shopt -s nullglob
  for f in "$WORKDIR"/repo/*.py; do
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

  # Текущий crontab root без нашего блока
  crontab -u root -l 2>/dev/null \
    | awk -v b="$BEGIN" -v e="$END" '
        $0==b {inb=1; next}
        $0==e {inb=0; next}
        !inb {print}
      ' > "$TMP" || true

  {
    echo "$BEGIN"
    echo "# Env для задач ниже:"
    # важно: пишем ИЗ УЖЕ СОБРАННЫХ значений (а не перечитываем файл)
    for v in "${VARS[@]}"; do
      printf '%s="%s"\n' "$v" "${!v}"
    done
    echo
    echo 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'
    echo 'SHELL=/bin/bash'
    echo
    if [[ -f "$repo_cron" ]]; then
      sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$repo_cron"
    else
      # дефолт, если файла crontab нет в репо
      echo '*/20 * * * * python3 /usr/local/bin/proxysmart_modems_check.py >> /var/log/proxysmart_modems_check.log 2>&1'
      echo '*/15 * * * * python3 /usr/local/bin/proxysmart_modems_list.py  >> /var/log/proxysmart_modems_list.log  2>&1'
    fi
    echo "$END"
  } >> "$TMP"

  crontab -u root "$TMP"
  rm -f "$TMP"
  echo "[cron] Блок PROXYSMART обновлён"
}

main() {
  require_root
  pkg_install
  ensure_env_permanent
  clone_repo
  install_requirements
  prep_fs
  copy_scripts
  install_cron
  echo "Готово ✅"
  echo "Проверь значения:  grep -E '^(MG_USER|MG_PASSWORD|MG_TG_TOKEN|MG_TG_CHAT)=' /etc/environment"
  echo "Проверь cron:      sudo crontab -l | sed -n '/PROXYSMART-BEGIN/,/PROXYSMART-END/p'"
}

main "$@"
