#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The part of korsi-api's contract this bridge speaks.

Hand-written rather than generated, and deliberately **lenient where korsi-api is strict**. Every
model here sets no `extra="forbid"`: korsi-api's own contracts do forbid extras, which is right for a
server validating what it is handed, and wrong for a client reading what it is given. A field added
to `LiveSessionOpened` in a Korsi release must not stop a bridge that is running inside a customer's
Nextcloud and cannot be redeployed on Korsi's schedule.

The mirror image of the same rule: this file models only the fields the bridge acts on. `LiveSessionView`
has ten and the bridge reads none of them, so it is not here at all -- the close response is discarded.
Modelling a field the bridge does not use would create an obligation to keep it accurate for no reader.

Source of truth: `korsi-api/korsi_api/contracts/meeting_live.py`.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LiveCloseReason(StrEnum):
	"""Why a session ended. Sent on close; korsi-api renders it to a reader.

	The bridge legitimately produces four of these. `MAX_DURATION` belongs to korsi-api's own sweep
	and is here only so the enum matches the server's.
	"""

	CALL_ENDED = "call_ended"
	"""Talk said the call is over: everyone left, or the HPB sent `bye`."""

	BRIDGE_STOPPED = "bridge_stopped"
	"""This container is shutting down while the call is still going."""

	ABANDONED = "abandoned"
	"""Never sent by the bridge -- it is what korsi-api's sweep concludes when the bridge went away
	without closing. Present so a decoded server response never fails on it."""

	MAX_DURATION = "max_duration"
	"""Also the server's conclusion, not the bridge's."""

	BRIDGE_ERROR = "bridge_error"
	"""Something in here broke badly enough to stop reading a call that is still running. Reported
	rather than swallowed, so a meeting with a truncated live reading says why."""


class LiveDeclineReason(StrEnum):
	"""Why korsi-api said no. Logged, never acted on differently -- except for one.

	`ROOM_NOT_REGISTERED` is the ordinary case and must not be logged as a problem: most calls in a
	customer's Nextcloud happen in rooms Korsi has nothing to do with.
	"""

	ROOM_NOT_REGISTERED = "room.not_registered"
	DISABLED_FOR_TENANT = "live.disabled_for_tenant"
	NO_ALLOWANCE = "billing.no_allowance"
	ALREADY_ACTIVE = "live.already_active"
	STT_UNAVAILABLE = "live.stt_unavailable"


class LiveSttCredential(BaseModel):
	"""A temporary key for the speech provider, and the audio format to send it.

	**The format travels with the credential on purpose.** It would be one line of constant in this
	file, and then it would be a constant inside every customer's Nextcloud, needing a coordinated
	redeploy the day Korsi changes speech model. The bridge is a pipe; the pipe does not hold
	opinions about encoding.
	"""

	api_key: str
	websocket_url: str
	model: str
	max_session_duration_seconds: int
	expires_at: datetime
	audio_format: str = "pcm_s16le"
	sample_rate: int = 48000
	num_channels: int = 1
	language_hints: tuple[str, ...] = ()


class LiveSessionOpened(BaseModel):
	"""Korsi said yes: which meeting this call is, how often to post, and the key to transcribe with."""

	live_session_id: str
	meeting_id: str
	operation_case_id: str | None = None
	meeting_created: bool = False
	first_segment_interval_seconds: int
	segment_interval_seconds: int
	stt: LiveSttCredential


class LiveSessionDecision(BaseModel):
	"""Read this call, or do not.

	Always a `200`. A decline is an answer, not an error -- which is what lets the bridge treat "this
	room is not registered" and "your credentials are wrong" as the different things they are.
	"""

	enabled: bool
	reason: LiveDeclineReason | None = None
	session: LiveSessionOpened | None = None
	retry_after_seconds: int = 60


class LiveSegmentAccepted(BaseModel):
	"""What came back from posting a segment.

	`segment_interval_seconds` is echoed on every accept so a cadence change reaches a running bridge
	without reopening the session. The bridge re-reads it every time rather than caching what it was
	told at open (D9: every policy comes from korsi-api).
	"""

	accepted_sequence: int
	segment_count: int = 0
	snapshot_queued: bool = False
	segment_interval_seconds: int


class LiveWatchlistRoom(BaseModel):
	"""One Talk conversation Korsi would read a call in."""

	room_remote_id: str
	operation_case_id: str | None = None
	room_title: str | None = None


class LiveWatchlist(BaseModel):
	"""Which rooms to watch, how often to ask again, and whether to watch at all.

	`enabled: false` means live assistance is off for this instance. The room list is still returned,
	and the bridge still stops -- but an operator reading the logs can tell "switched off" from
	"nobody has linked a room yet", which are two different tickets.
	"""

	rooms: tuple[LiveWatchlistRoom, ...] = ()
	poll_interval_seconds: int = Field(default=60)
	enabled: bool = True


class KorsiApiError(Exception):
	"""A call to korsi-api failed in a way the caller has to decide about.

	Carries the status code because exactly one distinction matters upstream: `401`/`403` means this
	bridge's credentials are wrong and retrying is pointless until somebody fixes the deployment,
	while anything else is worth another attempt.
	"""

	def __init__(self, message: str, *, status: int | None = None) -> None:
		super().__init__(message)
		self.status = status

	@property
	def is_auth_failure(self) -> bool:
		return self.status in (401, 403)


class LiveSessionClosed(KorsiApiError):
	"""korsi-api considers this session finished; posting to it again will not start working.

	Its own type because it is the one API failure with a correct local response: stop transcribing
	this call and tear the session down, rather than retrying a segment forever.
	"""
