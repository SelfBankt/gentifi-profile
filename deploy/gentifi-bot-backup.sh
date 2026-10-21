#!/usr/bin/env bash
# Backs up the bot's database (owner links, invites, encrypted Nostr keys) to
# /srv/gentifi/backups, keeping 14 days. The Nostr master key is not in it: keep
# your own copy of /etc/gentifi-bot/.env's GENTIFI_NOSTR_MASTER_KEY somewhere safe.
# Run nightly by gentifi-bot-backup.timer; run by hand any time: gentifi-bot-backup
set -euo pipefail
DEST=/srv/gentifi/backups
DB=/var/lib/gentifi-bot/relay.db
install -d -m 700 "$DEST"
if [ ! -f "$DB" ]; then echo "No bot database yet ($DB)"; exit 0; fi
file="$DEST/bot-$(date +%F).db"
# SQLite's own backup, so a write in progress can't leave a broken copy
python3 -c 'import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()' "$DB" "$file.tmp"
chmod 600 "$file.tmp"
mv -f "$file.tmp" "$file"
find "$DEST" -name 'bot-*.db' -mtime +14 -delete
echo "Backed up to $file ($(du -h "$file" | cut -f1))"
