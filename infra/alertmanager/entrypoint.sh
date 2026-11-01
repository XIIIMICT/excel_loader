#!/bin/sh
# Рендерит `alertmanager.yml` с SMTP-значениями из окружения.
#
# Почему не `--setenv`: Alertmanager подставляет ${VAR} только в *_file-полях,
# НЕ в `to`/`smarthost`. envsubst в образе нет, поэтому подстановка — sed
# ровно по плейсхолдерам этого шаблона.
#
# Отсутствие значения — жёсткая гарантия остановки: иначе Alertmanager
# стартует с пустым получателем, и казалось бы настроенная почта молчит.

set -eu

: "${SUPPORT_MAIL:?SUPPORT_MAIL не задана — у алертов нет получателя (см. .env.example)}"
: "${ALERT_SMTP_FROM:?ALERT_SMTP_FROM не задано}"
: "${ALERT_SMTP_SMARTHOST:?ALERT_SMTP_SMARTHOST не задано}"
: "${ALERT_SMTP_USER:?ALERT_SMTP_USER не задано}"
: "${ALERT_SMTP_PASSWORD:?ALERT_SMTP_PASSWORD не задано}"

# Разделитель `|`: в паролях бывают `/` и `@`.
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
