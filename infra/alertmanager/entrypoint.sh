#!/bin/sh
# Render `alertmanager.yml` with the SMTP values from the environment.
#
# Why not `--setenv`: Alertmanager expands `${VAR}` only in `*_file` paths and a
# few fields, NOT in `to`/`smarthost` — a config that relies on it starts and
# silently sends nowhere. `envsubst` is not in the image either, so the
# substitution is done with `sed` over the exact placeholders this file uses.
#
# A missing value is a hard error: Alertmanager would otherwise boot with an
# empty recipient and we would believe mail is configured.
set -eu

: "${SUPPORT_MAIL:?SUPPORT_MAIL is not set — alert mail has no recipient}"
: "${ALERT_SMTP_FROM:?ALERT_SMTP_FROM is not set}"
: "${ALERT_SMTP_SMARTHOST:?ALERT_SMTP_SMARTHOST is not set}"
: "${ALERT_SMTP_USER:?ALERT_SMTP_USER is not set}"
: "${ALERT_SMTP_PASSWORD:?ALERT_SMTP_PASSWORD is not set}"

# `|` as the delimiter: passwords routinely contain `/` and `@`.
# The values are sed-replacement-safe because they are validated above and come
# from `.env`, not from a request.
sed \
  -e "s|\${SUPPORT_MAIL}|${SUPPORT_MAIL}|g" \
  -e "s|\${ALERT_SMTP_FROM}|${ALERT_SMTP_FROM}|g" \
  -e "s|\${ALERT_SMTP_SMARTHOST}|${ALERT_SMTP_SMARTHOST}|g" \
  -e "s|\${ALERT_SMTP_USER}|${ALERT_SMTP_USER}|g" \
  -e "s|\${ALERT_SMTP_PASSWORD}|${ALERT_SMTP_PASSWORD}|g" \
  /etc/alertmanager/alertmanager.tpl.yml > /tmp/alertmanager.yml

exec /bin/alertmanager \
  --config.file=/tmp/alertmanager.yml \
  --storage.path=/alertmanager \
  --web.external-url=http://localhost:9093
