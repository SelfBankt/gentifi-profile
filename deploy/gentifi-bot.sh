#!/usr/bin/env bash
# gentifi-relay as the bot's user, with its database and settings:
#   gentifi-bot team | invite <business> --onboard | review <business> | merchants | check
# Installed as /usr/local/bin/gentifi-bot by setup-bot.sh. Run as root.
set -euo pipefail
cd /etc/gentifi-bot  # gentifi-relay reads .env from here; the token never goes on the command line
exec runuser -u gentifi -- env GENTIFI_RELAY_DB=/var/lib/gentifi-bot/relay.db \
  /srv/gentifi/profile/.venv/bin/gentifi-relay "$@"
