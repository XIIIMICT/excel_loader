#!/bin/sh
# Render alertmanager.yml from alertmanager.tpl.yml at container start.
#
# --setenv does not help: Alertmanager expands ${VAR} only in *_file paths, not
# in to/from/smarthost. envsubst is not in the image either, so sed renders the
# exact placeholders used by the template into /tmp (read-only mount stays ro).
#
# Missing recipient/sender/smarthost is a hard error: otherwise Alertmanager
# would boot with an empty recipient and we would believe mail is configured.
set -eu

: "${SUPPORT_MAIL:?SUPPORT_MAIL is empty — alert mail has no recipient}"
: "${ALERT_SMTP_FROM:?ALERT_SMTP_FROM is empty — alert mail has no sender}"
: "${ALERT_SMTP_SMARTHOST:?ALERT_SMTP_SMARTHOST is empty — alert mail has no relay}"

# Escape sed replacement metacharacters (the '|' delimiter, '&', backslash).
esc() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

SUPPORT_MAIL_ESC=$(esc "$SUPPORT_MAIL")
ALERT_SMTP_FROM_ESC=$(esc "$ALERT_SMTP_FROM")
ALERT_SMTP_SMARTHOST_ESC=$(esc "$ALERT_SMTP_SMARTHOST")
ALERT_SMTP_USER_ESC=$(esc "${ALERT_SMTP_USER:-}")
ALERT_SMTP_PASSWORD_ESC=$(esc "${ALERT_SMTP_PASSWORD:-}")

sed \
  -e "s|\${SUPPORT_MAIL}|${SUPPORT_MAIL_ESC}|g" \
  -e "s|\${ALERT_SMTP_FROM}|${ALERT_SMTP_FROM_ESC}|g" \
  -e "s|\${ALERT_SMTP_SMARTHOST}|${ALERT_SMTP_SMARTHOST_ESC}|g" \
  -e "s|\${ALERT_SMTP_USER}|${ALERT_SMTP_USER_ESC}|g" \
  -e "s|\${ALERT_SMTP_PASSWORD}|${ALERT_SMTP_PASSWORD_ESC}|g" \
  /etc/alertmanager/alertmanager.tpl.yml > /tmp/alertmanager.yml

# The in-project Postfix relay may need no authentication from the internal
# network; it authenticates to the corporate SMTP host itself. Empty auth
# fields are invalid for Alertmanager, so remove those lines entirely.
if [ -z "${ALERT_SMTP_USER:-}" ] || [ -z "${ALERT_SMTP_PASSWORD:-}" ]; then
  sed -i -e '/auth_username:/d' -e '/auth_password:/d' /tmp/alertmanager.yml
fi

if grep -v '^[[:space:]]*#' /tmp/alertmanager.yml | grep -q '\${'; then
  echo "alertmanager entrypoint: unresolved variable in /tmp/alertmanager.yml" >&2
  exit 1
fi

if command -v amtool >/dev/null 2>&1; then
  amtool check-config /tmp/alertmanager.yml
fi

exec /bin/alertmanager \
  --config.file=/tmp/alertmanager.yml \
  --storage.path=/alertmanager \
  --web.external-url=http://localhost:9093
