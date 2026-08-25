#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""What decides that a call should be read at all.

**This is the biggest departure from upstream, and it is a change of trigger.** Upstream joins a call
because a *viewer* turned captions on: Talk's PHP side posts `roomToken` plus that participant's
session id, and the app leaves again sixty seconds after the last viewer turns them off. Text is even
dropped when no viewer is subscribed. That is right for captions, which exist for the person reading
them.

Korsi's reason is the opposite one. The organizations this serves record meetings the way they would
run a dictaphone: somebody starts the call and expects that Korsi is paying attention, and nobody is
going to press a button in a Talk sidebar to make the record exist. So the trigger is Korsi's own
watchlist -- the rooms mapped to an operational case -- and a call in one of those rooms is read whether
or not a single human is watching the panel.

**Discovery happens over signaling, not the Talk API.** The bridge holds a room-level signaling session
for each watched room and sees calls start in the participant updates it already receives. The
alternative was polling Talk's OCS room endpoint, which needs a Nextcloud user who is a participant of
every case room -- a membership somebody has to maintain by hand, and a silent gap in coverage the day
they forget. The internal-secret signaling connection needs no membership at all, and the code to hold
one already exists in `SpreedClient`.

**Nothing here decides whether to read a call.** The watcher notices that one started and asks; korsi-api
answers. A decline is cached briefly, so a two-hour call in an unregistered room is asked about once
rather than on every poll.
"""

import asyncio
import logging
import os
import time
from contextlib import suppress
from datetime import datetime

from constants import (
	DECLINE_CACHE_SECONDS,
	WATCHLIST_ERROR_BACKOFF_SECONDS,
	WATCHLIST_FALLBACK_POLL_SECONDS,
	WATCHLIST_MAX_BACKOFF_SECONDS,
)
from korsi_client import KorsiClient
from korsi_types import KorsiApiError, LiveCloseReason, LiveDeclineReason
from livetypes import HPBSettings, SigConnectResult
from spreed_client import SpreedClient

LOGGER = logging.getLogger("lt")


class CallWatcher:
	"""Holds a signaling session for every watched room and reads the calls that happen in them."""

	def __init__(self, *, korsi: KorsiClient) -> None:
		self._korsi = korsi
		self._hpb_settings: HPBSettings | None = None
		self._clients: dict[str, SpreedClient] = {}
		self._lock = asyncio.Lock()
		self._task: asyncio.Task | None = None
		self._stopping = False
		#: room token -> monotonic time the decline stops being remembered
		self._declined_until: dict[str, float] = {}
		self._bridge_version = os.environ.get("APP_VERSION") or None

	# ------------------------------------------------------------------ lifecycle

	def start(self, hpb_settings: HPBSettings) -> None:
		self._hpb_settings = hpb_settings
		self._stopping = False
		if self._task is None or self._task.done():
			self._task = asyncio.create_task(self._run(), name="korsi-call-watcher")
			LOGGER.info("Call watcher started", extra={"tag": "watcher"})

	async def stop(self) -> None:
		"""Stop watching and close every live session that is still open.

		Closing them here rather than letting korsi-api's sweep notice matters for money: a session the
		sweep closes has held a metering reservation for twenty minutes past the end of the call, and a
		container restart during the working day would leave one behind for every meeting in progress.
		"""
		self._stopping = True
		if self._task and not self._task.done():
			self._task.cancel()
			with suppress(asyncio.CancelledError, Exception):
				await self._task
		self._task = None

		async with self._lock:
			clients = list(self._clients.items())
			self._clients.clear()

		for room_token, client in clients:
			LOGGER.info("Shutting down a watched room", extra={"room_token": room_token, "tag": "watcher"})
			with suppress(Exception):
				await client.stop_reading(LiveCloseReason.BRIDGE_STOPPED)
			with suppress(Exception):
				await client.close()

	@property
	def watched_rooms(self) -> list[str]:
		return list(self._clients.keys())

	# ------------------------------------------------------------------ the poll loop

	async def _run(self) -> None:
		poll = WATCHLIST_FALLBACK_POLL_SECONDS
		backoff = WATCHLIST_ERROR_BACKOFF_SECONDS
		try:
			while not self._stopping:
				try:
					watchlist = await self._korsi.watchlist()
					backoff = WATCHLIST_ERROR_BACKOFF_SECONDS
					poll = watchlist.poll_interval_seconds or WATCHLIST_FALLBACK_POLL_SECONDS

					if not watchlist.enabled:
						# Korsi says live assistance is off here. Stop watching entirely rather than
						# holding signaling connections for calls that will always be declined.
						if self._clients:
							LOGGER.info("Live assistance is disabled for this instance, releasing rooms", extra={
								"rooms": len(self._clients),
								"tag": "watcher",
							})
							await self._reconcile(set())
					else:
						await self._reconcile({room.room_remote_id for room in watchlist.rooms})

					await asyncio.sleep(poll)
				except KorsiApiError as e:
					if e.is_auth_failure:
						# Nothing this bridge can do about its own credentials. Back off to the
						# ceiling immediately instead of retrying every thirty seconds forever: the
						# fix is a deployment change, and the logs should say so once an hour.
						LOGGER.error(
							"Korsi rejected this bridge's credentials. Check KORSI_CLIENT_ID,"
							" KORSI_CLIENT_SECRET and KORSI_TOKEN_SCOPE.",
							exc_info=e, extra={"tag": "watcher"},
						)
						backoff = WATCHLIST_MAX_BACKOFF_SECONDS
					else:
						LOGGER.warning("Watchlist poll failed", exc_info=e, extra={
							"backoff": backoff,
							"tag": "watcher",
						})
					await asyncio.sleep(backoff)
					backoff = min(backoff * 2, WATCHLIST_MAX_BACKOFF_SECONDS)
		except asyncio.CancelledError:
			raise
		except Exception as e:  # noqa: BLE001 - the watcher dying silently would look like "no meetings"
			LOGGER.exception("Call watcher stopped unexpectedly", exc_info=e, extra={"tag": "watcher"})

	async def _reconcile(self, wanted: set[str]) -> None:
		"""Hold a signaling session for exactly the rooms Korsi named."""
		async with self._lock:
			current = set(self._clients.keys())

		for room_token in wanted - current:
			await self._watch_room(room_token)

		for room_token in current - wanted:
			await self._release_room(room_token)

		# A client whose signaling died is replaced rather than repaired: `SpreedClient` already
		# retries internally, so reaching `defunct` means it gave up, and the next poll is the natural
		# moment to start a fresh one.
		async with self._lock:
			dead = [token for token, client in self._clients.items() if client.defunct.is_set()]
		for room_token in dead:
			LOGGER.info("Replacing a defunct room watcher", extra={"room_token": room_token, "tag": "watcher"})
			await self._release_room(room_token)
			if room_token in wanted:
				await self._watch_room(room_token)

	async def _watch_room(self, room_token: str) -> None:
		"""Join one room's signaling without joining any call in it."""
		if self._hpb_settings is None:
			return

		client = SpreedClient(
			room_token,
			self._hpb_settings,
			on_call_started=self._on_call_started,
			on_call_ended=self._on_call_ended,
		)
		async with self._lock:
			self._clients[room_token] = client

		result = await client.connect()
		if result is not SigConnectResult.SUCCESS:
			LOGGER.warning("Could not watch a room, will retry on the next poll", extra={
				"room_token": room_token,
				"result": int(result),
				"tag": "watcher",
			})
			await self._release_room(room_token)
			return

		LOGGER.info("Watching a room for calls", extra={"room_token": room_token, "tag": "watcher"})

	async def _release_room(self, room_token: str) -> None:
		async with self._lock:
			client = self._clients.pop(room_token, None)
		if client is None:
			return
		with suppress(Exception):
			await client.stop_reading(LiveCloseReason.BRIDGE_STOPPED)
		with suppress(Exception):
			await client.close()

	# ------------------------------------------------------------------ callbacks from a room

	async def _on_call_started(self, room_token: str, started_at: datetime) -> None:
		"""A call began in a watched room. Ask Korsi whether to read it."""
		if self._stopping:
			return

		deadline = self._declined_until.get(room_token)
		if deadline and time.monotonic() < deadline:
			LOGGER.debug("Call started in a room Korsi recently declined, not asking again", extra={
				"room_token": room_token,
				"tag": "watcher",
			})
			return

		async with self._lock:
			client = self._clients.get(room_token)
		if client is None:
			return

		try:
			decision = await self._korsi.open_session(
				room_remote_id=room_token,
				started_at=started_at,
				bridge_version=self._bridge_version,
			)
		except KorsiApiError as e:
			LOGGER.warning("Could not ask Korsi about a call", exc_info=e, extra={
				"room_token": room_token,
				"tag": "watcher",
			})
			return

		if not decision.enabled or decision.session is None:
			reason = decision.reason
			self._declined_until[room_token] = time.monotonic() + max(
				decision.retry_after_seconds, DECLINE_CACHE_SECONDS
			)
			# An unregistered room is the ordinary case, not a problem: most conversations in a
			# customer's Nextcloud have nothing to do with Korsi. Anything else is worth an operator's
			# attention, because it means a room somebody *did* link is not being read.
			if reason is LiveDeclineReason.ROOM_NOT_REGISTERED:
				LOGGER.debug("Korsi does not track this room", extra={
					"room_token": room_token,
					"tag": "watcher",
				})
			else:
				LOGGER.warning("Korsi declined to read this call", extra={
					"room_token": room_token,
					"reason": reason.value if reason else None,
					"tag": "watcher",
				})
			return

		LOGGER.info("Korsi accepted a call, starting to read it", extra={
			"room_token": room_token,
			"live_session_id": decision.session.live_session_id,
			"meeting_id": decision.session.meeting_id,
			"meeting_created": decision.session.meeting_created,
			"tag": "watcher",
		})
		await client.start_reading(decision.session, self._korsi)

	async def _on_call_ended(self, room_token: str, reason: LiveCloseReason) -> None:
		"""The call is over. Stop reading but keep watching the room for the next one."""
		async with self._lock:
			client = self._clients.get(room_token)
		if client is None:
			return
		self._declined_until.pop(room_token, None)
		with suppress(Exception):
			await client.stop_reading(reason)
