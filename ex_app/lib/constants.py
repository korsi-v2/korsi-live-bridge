#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tuning values.

Split into what came from upstream and what this fork added. Nothing here is a *policy* value: the
segment cadence, the language and whether live assistance runs at all come from korsi-api on the wire.
These are the timeouts and buffer sizes of the machinery, which no Korsi release changes.
"""

# --------------------------------------------------------------------- upstream: HPB and WebRTC

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts
HPB_SHUTDOWN_TIMEOUT = 30  # seconds to wait for the ws connection to shut down gracefully
HPB_PING_TIMEOUT = 120  # seconds to wait for a ping response from HPB server
CACHE_TTL = 15 * 60  # cache values for 15 minutes
ICE_GATHERING_TIMEOUT = 30  # seconds
SEND_TIMEOUT = 10  # timeout for signaling sends
TIMEOUT_INCREASE_FACTOR = 1.5

# --------------------------------------------------------------------- korsi: the audio mixer

#: How much audio one mixed chunk carries. 20 ms is one Opus frame at 48 kHz, so in the common case a
#: chunk is exactly one frame from each publisher and no partial-frame arithmetic happens.
MIXER_CHUNK_MS = 20

#: Mono, because there is one room and one conversation in it. Also what the speech credential asks
#: for -- korsi-api sends `num_channels` and this has to agree with it.
MIXER_NUM_CHANNELS = 1

#: 48 kHz: what WebRTC delivers, so the common path resamples nothing.
MIXER_SAMPLE_RATE = 48000

#: How many chunks may queue before the oldest is dropped, per publisher and on the output. 250 chunks
#: is five seconds. Enough to ride out a scheduling hiccup, small enough that a stalled consumer cannot
#: turn into unbounded memory in a container the customer is running.
MIXER_MAX_BUFFER_CHUNKS = 250

# --------------------------------------------------------------------- korsi: the speech connection

SONIOX_CONNECT_TIMEOUT = 20  # seconds for the websocket handshake

#: How long to wait after asking Soniox to finalise before cutting a segment anyway. Best effort on
#: purpose: a segment missing its last half-sentence is better than an interval that slips.
SONIOX_FINALIZE_GRACE_SECONDS = 2

#: How many times to reopen the speech connection within one call before giving up and reporting the
#: live reading as failed. Credential renewals count, and a four-hour meeting needs several.
SONIOX_MAX_RECONNECTS = 20

#: Multiplied by the attempt number, so repeated failures back off rather than hammering a provider
#: that is already saying it is overloaded.
SONIOX_RECONNECT_BACKOFF_SECONDS = 2

# --------------------------------------------------------------------- korsi: the call watcher

#: Fallback poll interval, used only before the first successful watchlist read. After that korsi-api's
#: own `poll_interval_seconds` applies -- the bridge does not decide how often to ask.
WATCHLIST_FALLBACK_POLL_SECONDS = 60

#: How long to wait after a failed watchlist poll before trying again, and the ceiling that backoff
#: climbs to. The bridge is inside a customer's infrastructure and cannot be told to calm down, so it
#: has to calm down on its own when Korsi is unreachable.
WATCHLIST_ERROR_BACKOFF_SECONDS = 30
WATCHLIST_MAX_BACKOFF_SECONDS = 600

#: How long a decline is remembered before the same room is asked about again. Without it, a call in a
#: room Korsi declined would be re-offered on every poll for the length of the call.
DECLINE_CACHE_SECONDS = 300

#: No proactive credential-renewal interval, on purpose. Soniox states when a key is spent -- `403
#: temp_api_key_session_expired` for the session cap, `413 max_duration_reached` for its own ceiling --
#: and reacting to that is one code path with no clock to keep in step with the provider's. A local
#: timer would have to be conservative enough to renew keys that still had life in them, and would
#: still need the reactive path for the case where the two disagreed.
