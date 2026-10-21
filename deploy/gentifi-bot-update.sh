#!/usr/bin/env bash
# Pulls the latest gentifi-profile, reinstalls the bot and restarts it, then checks it answers.
# Installed as /usr/local/bin/gentifi-bot-update by setup-bot.sh. Run as root: gentifi-bot-update
set -euo pipefail
REPO=/srv/gentifi/profile
KEY=/root/.ssh/gentifi-profile-deploy
git_() { GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes" git -c safe.directory="$REPO" -C "$REPO" "$@"; }
before=$(git_ rev-parse --short HEAD)
git_ pull --ff-only -q
after=$(git_ rev-parse --short HEAD)
bash "$REPO/deploy/setup-bot.sh" >/dev/null
systemctl restart gentifi-bot
for _ in $(seq 1 30); do
  # Any HTTP answer means the server is up (a bare GET on /mcp is refused, which is fine)
  if [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/mcp)" != "000" ]; then
    echo "Updated $before -> $after. The bot is running."
    exit 0
  fi
  sleep 1
done
echo "Updated $before -> $after, but the bot isn't answering. Recent log:" >&2
journalctl -u gentifi-bot -n 30 --no-pager >&2
exit 1
