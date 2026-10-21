"""Nostr publishing: signatures, key custody, profile mapping and a round trip through a local relay."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("websockets")

from gentifi_profile.nostr import (KeyStore, LocalRelay, LocalSigner, NostrKeyError, Publisher, RelayError,
                                   add_master_key, check_relays, fetch, finish, npub, profile_events, verify)
from gentifi_profile.nostr.cli import main as nostr_cli
from gentifi_profile.nostr.mapping import geohash
from gentifi_profile.store import Directory, RequestBook

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
MASTER = bytes(range(32))
PK = "ab" * 32


@pytest.fixture
def farm():
    return json.loads((EXAMPLES / "farm-shop.json").read_text())


def by_d(events, kind):
    return {next(t[1] for t in e["tags"] if t[0] == "d"): e for e in events if e["kind"] == kind}


def tag(event, name):
    return next((t[1:] for t in event["tags"] if t[0] == name), None)


# ------------------------------------------------------------ events and keys


def test_signing_matches_bip340_and_nip19_vectors():
    signer = LocalSigner((3).to_bytes(32, "big"))
    assert signer.pubkey == "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"
    assert npub("3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d") == \
        "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    event = finish({"kind": 1, "created_at": 1760990000, "tags": [["t", "gentifi"]], "content": "Café £3.50"}, signer)
    assert verify(event)
    assert not verify({**event, "content": "Café £3.60"}), "tampered content fails"
    assert not verify({**event, "sig": "00" * 64})


def test_keys_are_made_once_and_kept_encrypted(tmp_path):
    db = tmp_path / "relay.db"
    first = KeyStore(db, MASTER).signer(FARM)
    again = KeyStore(db, MASTER).signer(FARM)
    assert first.pubkey == again.pubkey
    assert KeyStore(db, MASTER).pubkey(FARM) == first.pubkey
    stored = sqlite3.connect(db).execute("SELECT secret FROM nostr_keys").fetchone()[0]
    assert first._key.secret not in stored, "the secret is never stored in the clear"
    with pytest.raises(NostrKeyError, match="wrong"):
        KeyStore(db, bytes(32)).signer(FARM)


def test_master_key_is_added_once_and_never_overwritten(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc")
    assert add_master_key(env)
    text = env.read_text()
    assert text.startswith("TELEGRAM_BOT_TOKEN=abc\nGENTIFI_NOSTR_MASTER_KEY=")
    assert not add_master_key(env)
    assert env.read_text() == text
    assert env.stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------ mapping


def test_profile_becomes_a_stall_products_and_listings(farm):
    events = profile_events(farm, 1760990000, pubkey=PK)
    meta = json.loads(next(e for e in events if e["kind"] == 0)["content"])
    assert meta["name"] == "Hollowbrook Farm Shop"
    assert meta["about"].startswith("Fictional example."), "demo businesses stay marked as fictional"

    stall = json.loads(by_d(events, 30017)[FARM]["content"])
    assert stall["currency"] == "GBP"
    assert {z["id"]: z["cost"] for z in stall["shipping"]} == {"collection": 0, "delivery": 3}

    products = by_d(events, 30018)
    assert set(products) == {"eggs:eggs_6", "eggs:eggs_12", "veg_box"}, "only fixed-price products"
    dozen = json.loads(products["eggs:eggs_12"]["content"])
    assert (dozen["price"], dozen["quantity"], dozen["stall_id"]) == (3.5, 14, FARM)
    assert json.loads(products["eggs:eggs_6"]["content"])["quantity"] is None, "untracked stock is null, not 0"
    assert tag(products["eggs:eggs_12"], "freshness") == ["2026-10-15T08:10:00+01:00", "confirmed", "message_parse"]

    listings = by_d(events, 30402)
    assert set(listings) == {"eggs", "eggs:eggs_6", "eggs:eggs_12", "veg_box", "lamb_half", "farm_tour"}
    assert tag(listings["eggs:eggs_12"], "price") == ["3.50", "GBP"]
    assert tag(listings["lamb_half"], "price") is None
    assert "Price on request" in listings["lamb_half"]["content"]
    assert tag(listings["farm_tour"], "status") == ["active"]
    assert tag(listings["veg_box"], "g") == [geohash(51.487, -2.437, 7)]
    for event in events:
        assert ["L", "com.gentifi"] in event["tags"] and ["l", FARM, "com.gentifi"] in event["tags"]


def test_sold_out_shows_as_zero_and_sold(farm):
    for rec in farm["availability"]:
        if rec.get("offering_id") == "eggs":
            rec["status"] = "out_of_stock"
            rec.pop("quantity", None)
    events = profile_events(farm, 1760990000, pubkey=PK)
    assert json.loads(by_d(events, 30018)["eggs:eggs_12"]["content"])["quantity"] == 0
    assert tag(by_d(events, 30402)["eggs:eggs_12"], "status") == ["sold"]
    assert tag(by_d(events, 30402)["eggs:eggs_12"], "stock") == ["0"]
    assert tag(by_d(events, 30402)["eggs"], "status") == ["active"], "the half dozen isn't known to be sold out"
    farm["availability"].append({**farm["availability"][0], "variant_id": "eggs_6"})
    assert tag(by_d(profile_events(farm, 1760990000, pubkey=PK), 30402)["eggs"], "status") == ["sold"], "every size"


def test_listings_follow_the_market_spec(farm):
    farm["offerings"][1]["images"] = ["https://example.org/veg.jpg"]
    events = profile_events(farm, 1760990000, pubkey=PK)
    listings = by_d(events, 30402)
    eggs, dozen, veg, tour = listings["eggs"], listings["eggs:eggs_12"], listings["veg_box"], listings["farm_tour"]
    assert tag(eggs, "type") == ["variable", "physical"]
    assert tag(dozen, "type") == ["variation", "physical"]
    assert ["a", f"30402:{PK}:eggs"] in dozen["tags"], "a size points at its product"
    assert "Half dozen (£2.00 per 6), Dozen (£3.50 per dozen)" in eggs["content"]
    assert tag(dozen, "stock") == ["14"] and tag(listings["eggs:eggs_6"], "stock") is None, "untracked: no stock tag"
    assert tag(dozen, "visibility") == ["on-sale"]
    assert ["spec", "Price per", "dozen"] in dozen["tags"]
    assert tag(veg, "image") == ["https://example.org/veg.jpg", "", "0"]
    assert ["a", f"30405:{PK}:{FARM}"] in veg["tags"]
    assert [t[1] for t in veg["tags"] if t[0] == "shipping_option"] == [f"30406:{PK}:collection", f"30406:{PK}:delivery"]
    assert not [t for t in tour["tags"] if t[0] == "shipping_option"], "a farm tour isn't shipped"
    assert tag(tour, "type") == ["simple", "physical"]

    collection = by_d(events, 30405)[FARM]
    refs = [t[1] for t in collection["tags"] if t[0] == "a"]
    assert refs == [f"30402:{PK}:{d}" for d in ["eggs", "veg_box", "lamb_half", "farm_tour"]], "sizes aren't listed twice"
    assert tag(collection, "title") == ["Hollowbrook Farm Shop"]

    shipping = by_d(events, 30406)
    pickup, delivery = shipping["collection"], shipping["delivery"]
    assert (tag(pickup, "service"), tag(pickup, "price"), tag(pickup, "country")) == (["pickup"], ["0", "GBP"], ["GB"])
    assert tag(pickup, "location") == ["Hollowbrook Lane, Pucklechurch, BS16 9XX"]
    assert tag(pickup, "g") == [geohash(51.487, -2.437, 7)]
    assert (tag(delivery, "service"), tag(delivery, "price")) == (["standard"], ["3.00", "GBP"])
    assert delivery["content"] == "Local delivery to BS16, BS30 and BS37 on Fri. £3.00, free over £30.00."

    farm["business"]["location"]["premises_open_to_public"] = False
    pickup = by_d(profile_events(farm, 1760990000, pubkey=PK), 30406)["collection"]
    assert tag(pickup, "location") == ["Pucklechurch, South Gloucestershire"], "no street address for private premises"

    for rec in farm["availability"]:
        if rec.get("variant_id") == "eggs_12":
            rec["status"] = "preorder"
    assert tag(by_d(profile_events(farm, 1760990000, pubkey=PK), 30402)["eggs:eggs_12"], "visibility") == ["pre-order"]


def test_paused_business_deletes_its_listings(farm):
    events = profile_events(farm, 1760990000, paused=True, pubkey=PK)
    assert [e["kind"] for e in events] == [0, 5]
    refs = [t[1] for t in events[1]["tags"] if t[0] == "a"]
    assert f"30017:{PK}:{FARM}" in refs
    assert f"30402:{PK}:farm_tour" in refs
    assert f"30405:{PK}:{FARM}" in refs and f"30406:{PK}:delivery" in refs
    with pytest.raises(ValueError):
        profile_events(farm, 1760990000, paused=True)


def test_public_relays_need_permission():
    assert check_relays(["ws://127.0.0.1:7777", "ws://localhost:1"]) == ["ws://127.0.0.1:7777", "ws://localhost:1"]
    with pytest.raises(RelayError, match="public relay"):
        check_relays(["wss://relay.damus.io"])
    assert check_relays(["wss://relay.damus.io"], allow_public=True)
    with pytest.raises(RelayError):
        check_relays([])
    with pytest.raises(RelayError):
        check_relays(["https://relay.example"])


# ------------------------------------------------------------ round trip


@pytest.fixture
def relay_url():
    relay = LocalRelay()
    return relay.start()


def wait_for(check, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_publish_then_sold_out_pause_and_resume_reach_the_relay(tmp_path, relay_url):
    directory = Directory.from_folder(EXAMPLES)
    book = RequestBook(directory)
    keys = KeyStore(tmp_path / "relay.db", MASTER)
    publisher = Publisher(directory, keys, [relay_url], lock=book.lock)
    results = publisher.publish(FARM)
    assert all(ok for ok, _ in results[relay_url].values()), results
    pubkey = keys.pubkey(FARM)

    def held():
        return fetch(relay_url, [{"authors": [pubkey]}])

    first = held()
    assert len(by_d(first, 30018)) == 3 and len(by_d(first, 30402)) == 6
    assert all(verify(e) for e in first)

    publisher.start()  # republishes on every change from here on
    book.mark_sold_out(FARM, {"eggs"})
    assert wait_for(lambda: json.loads(by_d(held(), 30018)["eggs:eggs_12"]["content"])["quantity"] == 0)
    assert len(by_d(held(), 30018)) == 3, "the new version replaced the old one"
    assert json.loads(by_d(held(), 30018)["eggs:eggs_6"]["content"])["quantity"] == 0, "every size sells out"

    directory.pause(FARM)
    assert wait_for(lambda: not by_d(held(), 30402))
    assert {e["kind"] for e in held()} == {0, 5}

    directory.resume(FARM)
    assert wait_for(lambda: len(by_d(held(), 30402)) == 6), "resuming outdates the deletion"
    publisher.stop()


def test_a_shop_being_onboarded_is_published_only_once_live(tmp_path, relay_url):
    directory = Directory.from_folder(EXAMPLES)
    directory.drafts.add(FARM)
    keys = KeyStore(tmp_path / "relay.db", MASTER)
    publisher = Publisher(directory, keys, [relay_url])
    publisher.start()
    plumber = "gtf_brooksideplumbing"
    assert wait_for(lambda: keys.pubkey(plumber) and fetch(relay_url, [{"authors": [keys.pubkey(plumber)]}]))
    assert keys.pubkey(FARM) is None, "nothing signed or sent for a draft"
    directory.go_live(FARM)
    assert wait_for(lambda: keys.pubkey(FARM) and len(fetch(relay_url, [{"authors": [keys.pubkey(FARM)]}])) == 14)

    directory.drafts.add(FARM)  # back in onboarding (gentifi-relay invite --onboard): take the listings down
    directory.changed(FARM)

    def held():
        return fetch(relay_url, [{"authors": [keys.pubkey(FARM)]}])

    assert wait_for(lambda: {e["kind"] for e in held()} == {0, 5})
    assert "updating its listing" in next(e for e in held() if e["kind"] == 5)["content"]
    directory.go_live(FARM)
    assert wait_for(lambda: len(by_d(held(), 30402)) == 6)
    publisher.stop()


def test_cli_publishes_to_a_local_relay_and_refuses_public_ones(tmp_path, relay_url, monkeypatch, capsys):
    monkeypatch.setenv("GENTIFI_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("GENTIFI_NOSTR_MASTER_KEY", MASTER.hex())
    monkeypatch.chdir(tmp_path)
    assert nostr_cli(["publish", FARM, "--profiles", str(EXAMPLES), "--relay", "wss://relay.damus.io"]) == 1
    assert "public relay" in capsys.readouterr().err
    assert nostr_cli(["publish", FARM, "--profiles", str(EXAMPLES), "--relay", relay_url]) == 0
    assert "14 of 14 events accepted" in capsys.readouterr().out
    assert nostr_cli(["show", FARM, "--relay", relay_url]) == 0
    out = capsys.readouterr().out
    assert "14 events" in out and "eggs:eggs_12" in out
    assert MASTER.hex() not in out
