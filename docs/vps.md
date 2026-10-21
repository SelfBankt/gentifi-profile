# Running the owner bot on the VPS

The bot (Telegram relay plus the reference MCP server) runs on the Gentifi VPS next to
the toolkit's `gentifi-host`, as its own service on `127.0.0.1:8765`. Nothing about it is
public: it reaches Telegram by polling, so no port is opened and Caddy is unchanged.

| What | Where |
| --- | --- |
| Code (root-owned, pulled with a read-only deploy key) | `/srv/gentifi/profile` |
| Business profiles the bot serves | `/srv/gentifi/profiles`, and `gentifi-profile.json` in each toolkit business folder |
| Secret the toolkit's host uses to hand orders to the bot | `/var/lib/gentifi-bot/handoff-secret` (made on first start) |
| Bot token and Nostr master key (root and `gentifi` only) | `/etc/gentifi-bot/.env` |
| AI key for reading owners' photos (OpenCode Go; root and `gentifi` only) | `/etc/gentifi-bot/ai-key` |
| Owners' photos and the drafts read from them | `/var/lib/gentifi-bot/drafts/<business>/<time>/` |
| Database: owner links, invites, encrypted Nostr keys | `/var/lib/gentifi-bot/relay.db` |
| Nightly database backups (14 days) | `/srv/gentifi/backups/bot-*.db` |
| Service | `gentifi-bot` (`journalctl -u gentifi-bot` for its log) |

Commands, run as root on the VPS:

```bash
gentifi-bot team                       # link your own Telegram to hear about owners
gentifi-bot invite <business> --onboard
gentifi-bot review <business>          # after editing its profile
gentifi-bot merchants
gentifi-bot-update                     # pull, reinstall, restart, check
gentifi-bot-backup                     # back up now (also nightly)
```

Only one copy of the bot can poll Telegram. Stop the one on your Mac before starting
this one, and don't run both again.

## First-time setup

Each step is one command. VPS steps run as root on the VPS; Mac steps in Terminal on
the Mac.

1. **VPS: make a deploy key** (read-only access to this private repository). It prints
   the public half, which is safe to share:

   ```bash
   ssh-keygen -q -t ed25519 -N "" -C gentifi-vps -f /root/.ssh/gentifi-profile-deploy && cat /root/.ssh/gentifi-profile-deploy.pub
   ```

2. **GitHub: add it as a read-only deploy key.** Open
   <https://github.com/SelfBankt/gentifi-profile/settings/keys/new>, title it
   "Gentifi VPS (read-only)", paste the line step 1 printed, leave "Allow write access"
   unticked and click Add key. (Use the website rather than `gh repo deploy-key add`:
   keys added by `gh` are removed if its login is ever signed out.)

3. **VPS: get the code**:

   ```bash
   GIT_SSH_COMMAND="ssh -i /root/.ssh/gentifi-profile-deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" git clone -q git@github.com:SelfBankt/gentifi-profile.git /srv/gentifi/profile
   ```

4. **VPS: set it up** (installs the bot, makes a new Nostr master key, copies in the
   fictional example businesses, installs the service and backups; doesn't start it):

   ```bash
   bash /srv/gentifi/profile/deploy/setup-bot.sh
   ```

5. **Mac: copy the bot token across** without showing it (use however you normally log
   in to the VPS in place of `root@mcp.gentifi.com`):

   ```bash
   grep '^TELEGRAM_BOT_TOKEN=' ~/gentifi/gentifi-profile/.env | ssh root@mcp.gentifi.com 'f=/etc/gentifi-bot/.env; if grep -q "^TELEGRAM_BOT_TOKEN=" $f; then echo "Already there"; else cat >> $f && echo "Token added"; fi'
   ```

6. **Mac: stop the bot here**: close the "start-gentifi-bot" Terminal window (or press
   Ctrl+C in it).

7. **VPS: start it there and check it**:

   ```bash
   systemctl start gentifi-bot && sleep 5 && gentifi-bot check && journalctl -u gentifi-bot -n 5 --no-pager
   ```

8. **VPS: link your Telegram again** (the VPS starts with an empty database): run
   `gentifi-bot team` and open the link, then `gentifi-bot invite gtf_hollowbrookfarm`
   to connect the test shop to your chat.

9. **Keep a copy of the Nostr master key** somewhere safe, such as a password manager.
   Without it Gentifi can't sign for its businesses again. It's the
   `GENTIFI_NOSTR_MASTER_KEY` line in `/etc/gentifi-bot/.env`; the database backups
   don't include it.

## Photos from owners

Owners can send the bot a photo of their price board or menu. It goes to your team chat,
and the bot drafts a catalogue from it with the toolkit's `gentifi draft`, using the
model in the service file (`GENTIFI_AI_MODEL`) and the key in `/etc/gentifi-bot/ai-key`.
Copy the key from the Mac once, without showing it:

```bash
ssh root@mcp.gentifi.com 'install -m 640 -o root -g gentifi /dev/stdin /etc/gentifi-bot/ai-key' < ~/.gentifi/ai-key
```

The draft (catalogue.csv, notes.md and the photo) is saved under
`/var/lib/gentifi-bot/drafts/<business>/<time>/`. Check every name and price against the
photo before using it; nothing changes for the shop until you do.

## Testing from the Mac

The server only listens on the VPS itself. To use it from Claude Desktop on the Mac,
open a tunnel and leave the window open; the "gentifi-local-test" connector
(`http://127.0.0.1:8765/mcp`) then reaches the VPS:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@mcp.gentifi.com
```

## Adding a real business

Export it from the toolkit as a profile (`gentifi profile`), put the file in
`/srv/gentifi/profiles`, then `gentifi-bot invite <business> --onboard` and send the
owner the link. Until the toolkit's confirmation page hands orders to the bot (not built
yet), customers' orders through `mcp.gentifi.com` still go to the business by email or
its checkout, not to Telegram.
