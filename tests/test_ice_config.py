#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Does the bridge gather candidates against servers it can actually reach?

Regression tests for a production failure in which a call was joined, a peer connection was negotiated,
and no audio ever arrived. Talk had TURN configured on its public host name; inside the container that
name resolved to a reverse proxy in front of the host, so the bridge gathered a host candidate on a
Docker address, a reflexive candidate on the Docker gateway, no relay candidate at all, and failed after
a minute of connectivity checks. Every log line up to that point said the bridge was working.

The override that fixes it has one property worth protecting above the rest: it replaces *where* the
allocation request goes without inventing a credential, because the credential is Talk's to mint. A
version of this that grew its own shared secret would be a second thing to keep in step with Talk, and
would fail exactly when Talk rotated.
"""

import pytest
from ice_config import STUN_URLS_VAR, TURN_URLS_VAR, describe, resolve_ice_servers
from livetypes import HPBSettings, StunServer, TurnServer


def settings(*, stun=("stun:public.example:3478",), turn=("turn:public.example:3478",)) -> HPBSettings:
	"""HPB settings shaped like Talk's, which offers public host names to everybody equally."""
	return HPBSettings(
		server="wss://public.example/standalone-signaling/spreed",
		stunservers=[StunServer(urls=list(stun))] if stun else [],
		turnservers=(
			[TurnServer(urls=list(turn), username="1780000000:korsi", credential="minted-by-talk")]
			if turn
			else []
		),
	)


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
	"""Each test states its own override, so a stray value in the environment cannot decide the result."""
	monkeypatch.delenv(STUN_URLS_VAR, raising=False)
	monkeypatch.delenv(TURN_URLS_VAR, raising=False)


def test_talks_servers_are_used_when_nothing_is_overridden():
	servers, described = resolve_ice_servers(settings())

	assert described["stun_source"] == "talk"
	assert described["turn_source"] == "talk"
	assert described["stun"] == ["stun:public.example:3478"]
	assert described["turn"] == ["turn:public.example:3478"]
	assert servers is not None
	assert [server.urls for server in servers] == [
		["stun:public.example:3478"],
		["turn:public.example:3478"],
	]


def test_an_override_replaces_talks_urls_rather_than_adding_to_them(monkeypatch):
	"""The reason to set this is that Talk's answer does not work from here. Keeping it only spends
	gathering time proving that again on every offer.
	"""
	monkeypatch.setenv(STUN_URLS_VAR, "stun:talk:3478")
	monkeypatch.setenv(TURN_URLS_VAR, "turn:talk:3478?transport=udp")

	_, described = resolve_ice_servers(settings())

	assert described["stun"] == ["stun:talk:3478"]
	assert described["turn"] == ["turn:talk:3478?transport=udp"]
	assert described["stun_source"] == "override"
	assert described["turn_source"] == "override"


def test_an_overridden_turn_url_still_uses_the_credential_talk_minted(monkeypatch):
	"""The whole reason this needs no secret of its own: Talk's TURN REST credential is an HMAC over an
	expiry, with no host and no realm in it, so it is valid at the same server under any name.
	"""
	monkeypatch.setenv(TURN_URLS_VAR, "turn:talk:3478")

	servers, _ = resolve_ice_servers(settings())

	assert servers is not None
	turn = [server for server in servers if server.username]
	assert len(turn) == 1
	assert turn[0].urls == ["turn:talk:3478"]
	assert turn[0].username == "1780000000:korsi"
	assert turn[0].credential == "minted-by-talk"


def test_a_turn_override_cannot_stand_in_for_turn_talk_never_had(monkeypatch, caplog):
	"""No credential means no allocation. Said out loud, because the symptom is once again just a
	missing relay candidate.
	"""
	monkeypatch.setenv(TURN_URLS_VAR, "turn:talk:3478")

	servers, described = resolve_ice_servers(settings(turn=()))

	assert described["turn"] == []
	assert described["turn_source"].startswith("none")
	assert servers is not None
	assert all(server.username is None for server in servers)
	assert "occ talk:turn:add" in caplog.text


def test_multiple_urls_are_split_and_trimmed(monkeypatch):
	monkeypatch.setenv(TURN_URLS_VAR, " turn:talk:3478 , turn:talk:3478?transport=tcp ,, ")

	_, described = resolve_ice_servers(settings())

	assert described["turn"] == ["turn:talk:3478", "turn:talk:3478?transport=tcp"]


def test_no_servers_at_all_is_named_rather_than_shown_as_empty():
	"""`None` is aiortc's "use your defaults", which means Google's public STUN. A deployment that
	cannot reach its own TURN host generally cannot reach that either, so it is labelled.
	"""
	servers, described = resolve_ice_servers(settings(stun=(), turn=()))

	assert servers is None
	assert "aiortc" in described["stun_source"]
	assert "aiortc" in described["turn_source"]


def test_describe_hands_out_no_credential():
	"""Served over HTTP to the admin page, so it must not carry the TURN password."""
	described = describe(settings())

	assert "minted-by-talk" not in repr(described)
	assert set(described) == {"stun", "turn", "stun_source", "turn_source"}


def test_describe_without_settings_says_unknown_not_none():
	"""Before the app is enabled there are no settings, which is a different thing from no servers."""
	assert describe(None)["turn_source"] == "unknown"
