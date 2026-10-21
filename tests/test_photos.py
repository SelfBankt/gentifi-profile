"""Owners' photos of their prices: passed to the Gentifi team with a draft read from them."""

import subprocess
from pathlib import Path

import pytest

from gentifi_profile.relay import Links, MockChannel, OwnerRelay
from gentifi_profile.relay.drafts import Drafter
from gentifi_profile.store import Directory, RequestBook

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
OWNER, TEAM, STRANGER = "1001", "3003", "2002"
CSV = ("name,price,price_from,unit,category,description,allergens,dietary,notice,check\n"
       "Free-range eggs,3.5,,dozen,,,,,,\n"
       "Seasonal veg box,,,,,,,,,no price read: add it, or this row is left out\n"
       "Farm tour,,8,,,,,,,\n")


class FakeNode:
    """Stands in for `node gentifi.js draft`: writes the files it would."""

    def __init__(self, returncode=0, stderr=""):
        self.calls, self.returncode, self.stderr = [], returncode, stderr

    def __call__(self, args, env, capture_output, text, timeout):
        self.calls.append((args, env))
        if self.returncode == 0:
            (Path(args[args.index("--out") + 1]) / "catalogue.csv").write_text(CSV)
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


@pytest.fixture
def bot(tmp_path):
    def make(drafter=True, node=None):
        book = RequestBook(Directory.from_folder(EXAMPLES))
        links, chat = Links(), MockChannel()
        node = node or FakeNode()
        d = Drafter(tmp_path / "toolkit", tmp_path / "drafts", {"PATH": "/usr/bin", "GENTIFI_AI_MODEL": "kimi-k3"},
                    run=node) if drafter else None
        relay = OwnerRelay(book, chat, links, drafter=d, background=lambda fn: fn())
        chat.type(OWNER, f"/start {links.invite(FARM)}", name="Jo")
        chat.type(TEAM, f"/start {links.team_invite()}", name="Finn")
        relay.step()
        return relay, chat, node, tmp_path / "drafts"
    return make


def test_a_photo_reaches_the_team_with_a_draft_to_check(bot):
    relay, chat, node, drafts = bot()
    file_id = chat.photo(OWNER, b"JPEG-BYTES")
    relay.step()
    assert chat.last(OWNER).text.startswith("Thanks, I've got your photo.")
    team = [m for m in chat.messages.values() if m.chat_id == TEAM]
    passed_on = next(m for m in team if m.photo)
    assert (passed_on.photo, passed_on.text) == (file_id, "Hollowbrook Farm Shop sent a photo of their prices.")
    draft = team[-1].text
    assert draft.startswith("Draft for Hollowbrook Farm Shop: 3 items, 1 to check.")
    assert "• Free-range eggs: £3.50 per dozen" in draft
    assert "• Seasonal veg box: no price  ⚠ no price read" in draft
    assert "• Farm tour: from £8.00" in draft
    assert f"gentifi-bot review {FARM}" in draft

    args, env = node.calls[0]
    assert args[2:4] == ["draft", "--photo"] and args[4].endswith("/photo.jpg")
    assert Path(args[4]).read_bytes() == b"JPEG-BYTES" and Path(args[4]).parent.parent == drafts / FARM
    assert env == {"PATH": "/usr/bin", "GENTIFI_AI_MODEL": "kimi-k3"}, "only the drafter's own settings"


def test_drafting_problems_go_to_the_team_not_the_owner(bot):
    relay, chat, _, _ = bot(node=FakeNode(returncode=1, stderr="The AI service refused the key: check ~/.gentifi/ai-key."))
    chat.photo(OWNER, b"JPEG")
    relay.step()
    assert chat.last(TEAM).text == ("Couldn't draft Hollowbrook Farm Shop's photo: "
                                    "The AI service refused the key: check ~/.gentifi/ai-key.")
    assert chat.last(OWNER).text.startswith("Thanks, I've got your photo.")


def test_without_a_drafter_the_photo_still_reaches_the_team(bot):
    relay, chat, _, _ = bot(drafter=False)
    chat.photo(OWNER, b"JPEG")
    relay.step()
    assert any(m.photo for m in chat.messages.values() if m.chat_id == TEAM)
    assert chat.last(TEAM).text.startswith("Read it with gentifi draft on your Mac")


def test_strangers_and_unreadable_files_get_a_plain_answer(bot):
    relay, chat, node, _ = bot()
    chat.photo(STRANGER, b"JPEG")
    from gentifi_profile.relay import Incoming
    chat.inbox.append(Incoming("file", OWNER, "image/heic"))
    relay.step()
    assert chat.last(STRANGER).text.startswith("This bot is for businesses on Gentifi.")
    assert "Send a photo of your price board" in chat.last(OWNER).text
    assert node.calls == []


def test_drafting_needs_the_toolkit_and_a_model(tmp_path):
    toolkit = tmp_path / "toolkit"
    (toolkit / "bin").mkdir(parents=True)
    environ = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin"),
               "GENTIFI_TOOLKIT": str(toolkit), "GENTIFI_AI_MODEL": "kimi-k3",
               "GENTIFI_AI_KEY_FILE": "/etc/gentifi-bot/ai-key", "TELEGRAM_BOT_TOKEN": "123:secret",
               "GENTIFI_NOSTR_MASTER_KEY": "ab" * 32}
    assert Drafter.from_env(tmp_path / "drafts", environ) is None, "no toolkit yet"
    (toolkit / "bin" / "gentifi.js").write_text("")
    drafter = Drafter.from_env(tmp_path / "drafts", environ)
    if drafter is None:
        pytest.skip("node isn't installed here")
    assert "TELEGRAM_BOT_TOKEN" not in drafter.env and "GENTIFI_NOSTR_MASTER_KEY" not in drafter.env
    assert drafter.env["GENTIFI_AI_KEY_FILE"] == "/etc/gentifi-bot/ai-key"
    assert Drafter.from_env(tmp_path / "drafts", {**environ, "GENTIFI_AI_MODEL": ""}) is None, "no model"
