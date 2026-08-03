#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'Activation refusée: %s\n' "$*" >&2
  return 1
}

[ "${EUID}" -eq 0 ] || fail "exécuter avec sudo"
[ "$#" -eq 3 ] || fail "usage: $0 ARCHIVE SHA256 REVISION"

archive="$1"
expected_archive_sha="$2"
expected_revision="$3"

[[ "$expected_archive_sha" =~ ^[0-9a-f]{64}$ ]] \
  || fail "empreinte SHA-256 invalide"
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] \
  || fail "révision Git invalide"
[ -f "$archive" ] || fail "archive introuvable: $archive"

actual_archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
[ "$actual_archive_sha" = "$expected_archive_sha" ] \
  || fail "empreinte de l'archive incorrecte"

prefix="energie-stress-monitor-${expected_revision}/"
while IFS= read -r member; do
  case "$member" in
    "$prefix"*) ;;
    *) fail "membre hors préfixe attendu: $member" ;;
  esac
  case "$member" in
    /*|*'/../'*|../*|*'/..') fail "chemin d'archive dangereux: $member" ;;
  esac
done < <(tar -tzf "$archive")

workdir="$(mktemp -d /var/tmp/energie-release.XXXXXX)"
cleanup() {
  rm -rf -- "$workdir"
}
trap cleanup EXIT

tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$workdir"
release="${workdir}/${prefix%/}"

required=(
  builder.py
  test_builder.py
  deploy/energie-snapshot.service
  deploy/energie-snapshot.timer
)
for relative in "${required[@]}"; do
  [ -f "${release}/${relative}" ] || fail "fichier de release absent: $relative"
done

python_bin=/opt/energie/venv/bin/python3
[ -x "$python_bin" ] || fail "interpréteur de production absent: $python_bin"
getent group energie >/dev/null || fail "groupe système energie absent"
[ -r /etc/energie/env ] || fail "configuration /etc/energie/env absente ou illisible"

PYTHONPYCACHEPREFIX="${workdir}/pycache" \
  "$python_bin" -m py_compile "${release}/builder.py" "${release}/test_builder.py"
PYTHONDONTWRITEBYTECODE=1 \
  "$python_bin" "${release}/test_builder.py"
systemd-analyze verify \
  "${release}/deploy/energie-snapshot.service" \
  "${release}/deploy/energie-snapshot.timer"

backup="$(mktemp -d "/var/backups/energie-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
chown root:root "$backup"
chmod 0700 "$backup"

targets=(
  /opt/energie/builder.py
  /opt/energie/REVISION
  /etc/systemd/system/energie-snapshot.service
  /etc/systemd/system/energie-snapshot.timer
)
for target in "${targets[@]}"; do
  if [ -e "$target" ]; then
    cp -a -- "$target" "${backup}/$(basename "$target")"
  fi
done

timer_was_enabled=0
if systemctl is-enabled --quiet energie-snapshot.timer; then
  timer_was_enabled=1
fi
activation_started=0

rollback() {
  local rc="$?"
  trap - ERR
  set +e
  if [ "$activation_started" -eq 1 ]; then
    printf 'Échec d’activation; restauration depuis %s\n' "$backup" >&2
    for target in "${targets[@]}"; do
      saved="${backup}/$(basename "$target")"
      if [ -e "$saved" ]; then
        cp -a -- "$saved" "$target"
      else
        rm -f -- "$target"
      fi
    done
    systemctl daemon-reload
    if [ "$timer_was_enabled" -eq 1 ]; then
      systemctl enable --now energie-snapshot.timer
    else
      systemctl disable --now energie-snapshot.timer
    fi
    systemctl start energie-snapshot.service
  fi
  exit "$rc"
}
trap rollback ERR

activation_started=1
install -o root -g energie -m 0640 "${release}/builder.py" /opt/energie/builder.py
printf '%s\n' "$expected_revision" > "${workdir}/REVISION"
install -o root -g energie -m 0640 "${workdir}/REVISION" /opt/energie/REVISION
install -o root -g root -m 0644 \
  "${release}/deploy/energie-snapshot.service" \
  /etc/systemd/system/energie-snapshot.service
install -o root -g root -m 0644 \
  "${release}/deploy/energie-snapshot.timer" \
  /etc/systemd/system/energie-snapshot.timer

systemctl daemon-reload
systemctl start energie-snapshot.service

result="$(systemctl show energie-snapshot.service --property=Result --value)"
status="$(systemctl show energie-snapshot.service --property=ExecMainStatus --value)"
[ "$result" = "success" ] || fail "service en échec: Result=$result"
[ "$status" = "0" ] || fail "service en échec: ExecMainStatus=$status"

systemctl enable --now energie-snapshot.timer
systemctl is-active --quiet energie-snapshot.timer

"$python_bin" -c '
import json
import sys

with open("/var/www/html/energie/snapshot.json", encoding="utf-8") as handle:
    snapshot = json.load(handle)
for name, expected in (("brent", "RBRTE"), ("wti", "RWTC")):
    item = snapshot.get("series", {}).get(name) or {}
    if item.get("tip_source") != "eia":
        raise SystemExit("source pétrole inattendue pour " + name)
    if item.get("source_series") != expected:
        raise SystemExit("série EIA inattendue pour " + name)
    if item.get("source_refresh_mode") not in ("network", "cache"):
        raise SystemExit("preuve de rafraîchissement absente pour " + name)
print(json.dumps({
    "generated": snapshot.get("generated"),
    "composite": snapshot.get("composite"),
    "brent": snapshot["series"]["brent"],
    "wti": snapshot["series"]["wti"],
}, ensure_ascii=False))
'

[ "$(cat /opt/energie/REVISION)" = "$expected_revision" ] \
  || fail "révision active non vérifiable"

activation_started=0
trap - ERR
printf 'Energie Stress Monitor %s activé. Sauvegarde: %s\n' \
  "$expected_revision" "$backup"
