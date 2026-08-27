#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Which STUN and TURN servers the bridge offers to WebRTC, and why Talk's answer is not always usable.

Talk hands out the ICE servers it hands to browsers: public host names, on the assumption that the peer
is somewhere on the internet. This bridge is not. It runs as a container beside Nextcloud, and where the
public name resolves to a reverse proxy in front of the host, a STUN request from the container leaves
for that proxy and comes back reflecting the Docker gateway, while a TURN allocation does not come back
at all. What that looks like in a log is a peer connection that gathered a host candidate on a private
address, a reflexive candidate on another private address, no relay candidate, and then failed after a
minute of connectivity checks.

The override is for that case: point the bridge straight at the TURN server on the container network
(`turn:talk:3478`) while every browser keeps using the public name.

Only the URLs are overridden. Credentials still come from Talk, because Talk mints them with the TURN
REST scheme -- an HMAC over an expiry timestamp, carrying no host and no realm -- so the credential Talk
minted for the public name is one the same server accepts on its internal name. That is what keeps this
to two settings with no shared secret of its own to configure, rotate, or leak.
"""

import logging
import os

from aiortc.rtcconfiguration import RTCIceServer
from livetypes import HPBSettings

LOGGER = logging.getLogger("lt")

#: Comma-separated, e.g. `stun:talk:3478`. Replaces Talk's STUN list outright rather than adding to it:
#: the reason to set this is that Talk's answer does not work from here, and leaving it in the list only
#: spends gathering time proving that again on every offer.
STUN_URLS_VAR = "KORSI_ICE_STUN_URLS"

#: Comma-separated, e.g. `turn:talk:3478?transport=udp`. Replaces Talk's TURN URLs, keeping the
#: credentials Talk minted.
TURN_URLS_VAR = "KORSI_ICE_TURN_URLS"


def _configured_urls(var: str) -> list[str]:
	return [url.strip() for url in (os.getenv(var) or "").split(",") if url.strip()]


def resolve_ice_servers(hpb_settings: HPBSettings) -> tuple[list[RTCIceServer] | None, dict]:
	"""The ICE servers to gather with, and a description of them safe to log and to serve.

	The description exists because this is the one input to a peer connection that cannot be recovered
	after the fact: by the time a connection has failed, the servers it tried are not in any state
	anybody can read. It names the source of each list, so "Talk offers no TURN server" is
	distinguishable from "the override is in force" -- which look identical in the resulting candidates.

	`None` for the servers is aiortc's "use your defaults", which means Google's public STUN. Kept as
	upstream had it, and named in the description as such, because a deployment that cannot reach the
	host it was configured with generally cannot reach Google either, and a silent fallback is the last
	thing that should be inferred from an empty list.
	"""
	stun_override = _configured_urls(STUN_URLS_VAR)
	turn_override = _configured_urls(TURN_URLS_VAR)

	stun_urls = stun_override or [url for server in hpb_settings.stunservers for url in server.urls]
	servers = [RTCIceServer(urls=list(stun_urls))] if stun_urls else []

	turn_urls: list[str] = []
	if turn_override:
		# Talk's own entry is still what supplies the credential, so an override cannot stand in for
		# TURN that was never configured. Said plainly here, because the symptom of getting this wrong
		# is once again just a missing relay candidate.
		if hpb_settings.turnservers:
			minted = hpb_settings.turnservers[0]
			turn_urls = turn_override
			servers.append(RTCIceServer(
				urls=list(turn_urls),
				username=minted.username,
				credential=minted.credential,
			))
		else:
			LOGGER.error(
				"%s is set but Talk offers no TURN server, so there is no credential to use with it."
				" Configure TURN in Talk first (occ talk:turn:add); the override only changes where the"
				" bridge sends its allocation request, not whether it has one to send.",
				TURN_URLS_VAR,
				extra={"tag": "peer_connection"},
			)
	else:
		for turnserver in hpb_settings.turnservers:
			servers.append(RTCIceServer(
				urls=list(turnserver.urls),
				username=turnserver.username,
				credential=turnserver.credential,
			))
			turn_urls.extend(turnserver.urls)

	description = {
		"stun": list(stun_urls),
		"stun_source": _source(stun_override, stun_urls),
		"turn": list(turn_urls),
		"turn_source": _source(turn_override, turn_urls),
	}
	return (servers or None, description)


def _source(override: list[str], resolved: list[str]) -> str:
	if override and resolved:
		return "override"
	if resolved:
		return "talk"
	return "none (aiortc falls back to its built-in STUN)"


def describe(hpb_settings: HPBSettings | None) -> dict:
	"""The description alone, for the status endpoint and the self-test.

	Discards the servers, so reading this over HTTP cannot hand out a TURN credential.
	"""
	if hpb_settings is None:
		return {"stun": [], "turn": [], "stun_source": "unknown", "turn_source": "unknown"}
	return resolve_ice_servers(hpb_settings)[1]
