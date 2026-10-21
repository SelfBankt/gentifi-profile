#!/usr/bin/env bash
# Sets up the Gentifi owner bot on the VPS already running the toolkit (setup-vps.sh),
# alongside it: same gentifi user, its own service on 127.0.0.1:8765, nothing public.
# Safe to run again; gentifi-bot-update runs it after pulling.
# First clone this repo to /srv/gentifi/profile with the deploy key (see docs/vps.md), then
# run as root:  bash /srv/gentifi/profile/deploy/setup-bot.sh
set -euo pipefail
REPO=/srv/gentifi/profile
ETC=/etc/gentifi-bot
PROFILES=/srv/gentifi/profiles

echo "== Python and git"
if ! python3 -c 'import ensurepip, venv' 2>/dev/null || ! command -v git >/dev/null; then
  apt-get update
  apt-get install -y python3-venv git
fi
id gentifi >/dev/null 2>&1 || useradd --system --home /srv/gentifi --shell /usr/sbin/nologin gentifi

echo "== The bot (tested package versions only)"
[ -x "$REPO/.venv/bin/python" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip
"$REPO/.venv/bin/pip" install -q -c "$REPO/deploy/constraints.txt" "$REPO[server,nostr]"
"$REPO/.venv/bin/pip" install -q --no-deps --force-reinstall "$REPO"  # always this checkout's code

echo "== Settings ($ETC/.env, readable by root and the bot only)"
install -d -m 750 -o root -g gentifi "$ETC"
[ -f "$ETC/.env" ] || install -m 640 -o root -g gentifi /dev/null "$ETC/.env"
if ! grep -q '^GENTIFI_NOSTR_MASTER_KEY=' "$ETC/.env"; then
  # Made here and never shown: keep your own copy (see docs/vps.md), or Gentifi can't sign for businesses again
  printf 'GENTIFI_NOSTR_MASTER_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> "$ETC/.env"
  echo "Made a new Nostr master key in $ETC/.env"
fi
chown root:gentifi "$ETC/.env"
chmod 640 "$ETC/.env"

install -d -m 700 -o gentifi -g gentifi /var/lib/gentifi-bot  # the database, also the service's StateDirectory

echo "== Profiles ($PROFILES)"
install -d -m 755 -o root -g gentifi "$PROFILES"
if [ -z "$(ls -A "$PROFILES")" ]; then
  cp "$REPO"/examples/*.json "$PROFILES"/
  echo "Copied the fictional example businesses in, for testing. Remove them once real ones are added."
fi

echo "== Commands, service and nightly backup"
for name in bot bot-backup bot-update; do
  install -m 755 "$REPO/deploy/gentifi-$name.sh" "/usr/local/bin/gentifi-$name.new"
  mv -f "/usr/local/bin/gentifi-$name.new" "/usr/local/bin/gentifi-$name"
done
install -m 644 "$REPO/deploy/gentifi-bot.service" /etc/systemd/system/gentifi-bot.service
install -m 644 "$REPO/deploy/gentifi-bot-backup.service" /etc/systemd/system/gentifi-bot-backup.service
install -m 644 "$REPO/deploy/gentifi-bot-backup.timer" /etc/systemd/system/gentifi-bot-backup.timer
systemctl daemon-reload
systemctl enable -q gentifi-bot gentifi-bot-backup.timer
systemctl start gentifi-bot-backup.timer

echo
if ! grep -q '^TELEGRAM_BOT_TOKEN=' "$ETC/.env"; then
  echo "Done, except the bot token: copy it from your Mac (docs/vps.md), then stop the bot on"
  echo "your Mac and run: systemctl start gentifi-bot"
elif systemctl is-active -q gentifi-bot; then
  echo "Done. The bot is running."
else
  echo "Done. Stop the bot on your Mac first (only one copy can run), then: systemctl start gentifi-bot"
fi
