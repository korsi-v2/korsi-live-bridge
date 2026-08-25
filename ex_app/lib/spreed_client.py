#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import dataclasses
import json
import logging
import os
import weakref
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlparse

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from audio_mixer import AudioMixer
from audio_stream import AudioStream
from constants import (
	HPB_PING_TIMEOUT,
	HPB_SHUTDOWN_TIMEOUT,
	ICE_GATHERING_TIMEOUT,
	MSG_RECEIVE_TIMEOUT,
)
from korsi_client import KorsiClient
from korsi_types import LiveCloseReason, LiveSessionOpened, LiveSttCredential
from livetypes import (
	CallFlag,
	HPBSettings,
	ReconnectMethod,
	SigConnectResult,
	SpreedRateLimitedException,
)
from nc_py_api import NextcloudApp
from segmenter import Segmenter
from soniox_stream import SonioxStream
from utils import get_ssl_context, hmac_sha256, sanitize_websocket_url
from websockets import ClientConnection
from websockets import State as WsState
from websockets import connect
from websockets.exceptions import WebSocketException

LOGGER = logging.getLogger("lt.spreed_client")


@dataclasses.dataclass
class PeerConnection:
	session_id: str
	pc: RTCPeerConnection


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		hpb_settings: HPBSettings,
		*,
		# `Coroutine` rather than `Awaitable`, because these are handed straight to
		# `asyncio.create_task`, which does not accept a plain awaitable.
		on_call_started: Callable[[str, datetime], Coroutine[Any, Any, None]],
		on_call_ended: Callable[[str, LiveCloseReason], Coroutine[Any, Any, None]],
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.peer_connection_lock = asyncio.Lock()
		self.defunct = asyncio.Event()
		self._close_task: asyncio.Task | None = None
		self._reconnect_task: asyncio.Task | None = None

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websocket_url = sanitize_websocket_url(os.environ["LT_HPB_URL"])
		self._backendURL = nc.app_cfg.endpoint + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self._on_call_started = on_call_started
		self._on_call_ended = on_call_ended

		# ---- reading state: everything below exists only while Korsi has an open session ----

		#: True between `start_reading` and `stop_reading`. Everything that costs money is gated on it,
		#: and it is what separates this fork's two jobs: sitting in a room watching for calls, which is
		#: one idle websocket, and reading a call, which is a speech connection and an LLM every few
		#: minutes. Upstream has only the second state.
		self.reading = False
		self._reading_lock = asyncio.Lock()
		self._session: LiveSessionOpened | None = None
		self._korsi: KorsiClient | None = None
		self._mixer: AudioMixer | None = None
		self._speech: SonioxStream | None = None
		self._segmenter: Segmenter | None = None
		self._credential_expires_at: datetime | None = None
		#: Set once per call, so a participants update that mentions three people already in a call does
		#: not ask Korsi three times.
		self._call_announced = False

		#: Non-internal sessions currently in the call with audio. Maintained whether or not the bridge
		#: is reading, because the set at the moment Korsi says yes is exactly who to request audio from.
		self._publishers_in_call: set[str] = set()


	async def _resume_connection(self) -> bool:
		"""
		Raises
		------
		SpreedRateLimitedException: when the HPB server rate limits the client during resume
		"""  # noqa
		try:
			await self.send_message({
				"type": "hello",
				"hello": {
					"version": "2.0",
					"resumeid": self.resumeid,
				}
			})
		except Exception as e:
			LOGGER.exception("Error resuming connection to HPB with short hello", exc_info=e, extra={
				"room_token": self.room_token,
				"tag": "short_resume",
			})
			return False

		msg_counter = 0
		# wait for the hello response with the new session ID
		while msg_counter < 10:
			message = await self.receive(MSG_RECEIVE_TIMEOUT)
			if message is None:
				LOGGER.error("No message received for %s secs while resuming, aborting...", MSG_RECEIVE_TIMEOUT, extra={
					"room_token": self.room_token,
					"tag": "short_resume",
				})
				return False

			if message.get("type") == "hello":
				self.sessionid = message["hello"]["sessionid"]
				LOGGER.info("Resumed connection with new session ID", extra={
					"sessionid": self.sessionid,
					"room_token": self.room_token,
					"tag": "short_resume",
				})
				return True

			if message.get("type") == "error":
				LOGGER.error(
					"Signaling error message received during a short resume", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"error_received": message,
						"tag": "short_resume",
					},
				)

				err_code = message.get("error", {}).get("code")
				if err_code == "no_such_session":
					LOGGER.info("Performing a full reconnect since the previous session expired", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "short_resume",
					})
					return False

				if err_code == "too_many_requests":
					LOGGER.error("Rate limited by the HPB during short resume, giving up", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "short_resume",
					})
					raise SpreedRateLimitedException()

				# some other error, do not retry
				return False

			msg_counter += 1

		# we did not receive the hello message for 10 messages
		return False

	async def connect(self, reconnect: ReconnectMethod = ReconnectMethod.NO_RECONNECT) -> SigConnectResult:  # noqa: C901
		if self._server and self._server.state == WsState.OPEN and reconnect != ReconnectMethod.FULL_RECONNECT:
			LOGGER.debug("Already connected to signaling server, skipping connect", extra={
				"room_token": self.room_token,
				"reconnect": reconnect,
				"tag": "connection",
			})
			return SigConnectResult.SUCCESS

		websocket_host = urlparse(self._websocket_url).hostname
		ssl_ctx = get_ssl_context(self._websocket_url)
		try:
			self._server = await connect(
				self._websocket_url,
				**({
					"server_hostname": websocket_host,
					"ssl": ssl_ctx,  # type: ignore[arg-type]
				} if ssl_ctx else {}),
				ping_timeout=HPB_PING_TIMEOUT,
			)
		except Exception as e:
			LOGGER.exception("Error connecting to signaling server, retrying...", exc_info=e, extra={
				"room_token": self.room_token,
				"reconnect": reconnect,
				"tag": "connection",
			})
			if reconnect != ReconnectMethod.NO_RECONNECT:
				await asyncio.sleep(2)
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
			return SigConnectResult.RETRY

		if reconnect == ReconnectMethod.SHORT_RESUME:
			self._reconnect_task = None
			try:
				res = await self._resume_connection()
			except SpreedRateLimitedException:
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				return SigConnectResult.FAILURE
			except Exception as e:
				LOGGER.exception("Unexpected error during short resume, retrying connection", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				if reconnect != ReconnectMethod.NO_RECONNECT:
					self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.SHORT_RESUME))
				return SigConnectResult.RETRY

			if res:
				LOGGER.info("Resumed connection to signaling server for room token: %s", self.room_token, extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				# Re-announce being in the call only if we were reading one. A resume during an idle
				# watch must not put the bridge into a call nobody started -- it would show up as a
				# participant in the room's UI and, worse, keep the call alive after the last human left.
				if self.reading:
					await self.send_incall()
				await self.send_join()
				return SigConnectResult.SUCCESS

			LOGGER.info("Short resume failed, performing full reconnect for room token: %s", self.room_token, extra={
				"room_token": self.room_token,
				"tag": "connection",
			})
			if reconnect != ReconnectMethod.NO_RECONNECT:
				await asyncio.sleep(2)
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
			return SigConnectResult.RETRY

		if reconnect == ReconnectMethod.FULL_RECONNECT:
			self._reconnect_task = None
			LOGGER.info("Performing full reconnect for room token: %s", self.room_token, extra={
				"room_token": self.room_token,
				"tag": "connection",
			})
			try:
				await asyncio.wait_for(self.close(), HPB_SHUTDOWN_TIMEOUT)
			except TimeoutError:
				LOGGER.warning("Timeout while closing SpreedClient during full reconnect, proceeding anyway", extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
			finally:
				self.defunct.set()
				self._monitor = None
				self.resumeid = None
				self.sessionid = None
				self._server = None

		await self.send_hello()

		msg_counter = 0
		while True:
			message = await self.receive(MSG_RECEIVE_TIMEOUT)
			if message is None:
				LOGGER.error("No message received for %s secs, aborting...", MSG_RECEIVE_TIMEOUT, extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				return SigConnectResult.FAILURE

			if message.get("type") == "error":
				LOGGER.error(
					"Signaling error message received: %s\nDetails: %s", message.get("error", {}).get("message"),
					message.get("error", {}).get("details"), extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					},
				)

				message_code = message.get("error", {}).get("code")
				if message_code == "duplicate_session":
					LOGGER.error("Duplicate session found, aborting connection", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					})
					return SigConnectResult.FAILURE
				if message_code == "room_join_failed":
					LOGGER.error("Room join failed, retrying...", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					})
					if reconnect != ReconnectMethod.NO_RECONNECT:
						await asyncio.sleep(2)
						self._reconnect_task = asyncio.create_task(
							self.connect(reconnect=ReconnectMethod.FULL_RECONNECT),
						)
					return SigConnectResult.RETRY

				return SigConnectResult.FAILURE

			if message.get("type") == "bye":
				LOGGER.info("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				return SigConnectResult.FAILURE

			if message.get("type") == "welcome":
				LOGGER.debug("Welcome message received", extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				continue

			if message.get("type") == "hello":
				self.sessionid = message["hello"]["sessionid"]
				self.resumeid = message["hello"]["resumeid"]
				LOGGER.debug("Hello message received", extra={
					"sessionid": self.sessionid,
					"resumeid": self.resumeid,
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				break

			if msg_counter > 10:
				LOGGER.error(
					"Too many messages received without 'welcome', reconnecting...",
					extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					},
				)
				if reconnect != ReconnectMethod.NO_RECONNECT:
					await asyncio.sleep(2)
					self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
				return SigConnectResult.RETRY

		self.defunct.clear()
		self._monitor = asyncio.create_task(self.signalling_monitor(), name=f"signalling_monitor-{self.room_token}")

		# Join the room, but do not join the call. Upstream sends `incall` here because it is only ever
		# constructed for a call somebody asked to have transcribed; this fork holds a session in every
		# watched room continuously, and announcing itself as a call participant in all of them would
		# both mislead the UI and, in an empty room, start a call.
		#
		# `incall` is sent by `start_reading` instead, once Korsi has said yes.
		await self.send_join()
		if self.reading:
			# A full reconnect during a call we were already reading. The call did not end while the
			# socket was down, so rejoin it rather than waiting for a participants update.
			await self.send_incall()

		LOGGER.info("Connected to signaling server", extra={
			"room_token": self.room_token,
			"reading": self.reading,
			"tag": "connection",
		})
		return SigConnectResult.SUCCESS

	# ------------------------------------------------------------------ reading a call

	async def start_reading(self, session: LiveSessionOpened, korsi: KorsiClient) -> None:
		"""Join the call and start turning it into text Korsi analyses.

		Everything expensive starts here: the mixer, the speech connection and the interval timer. Held
		under a lock because a participants update and a watcher retry can both arrive at the moment a
		call starts, and two speech connections for one call is two bills.
		"""
		async with self._reading_lock:
			if self.reading:
				LOGGER.debug("Already reading this call", extra={
					"room_token": self.room_token,
					"tag": "reading",
				})
				return

			self._session = session
			self._korsi = korsi
			self._credential_expires_at = session.stt.expires_at

			self._mixer = AudioMixer()
			await self._mixer.start()

			self._speech = SonioxStream(
				room_token=self.room_token,
				credential=session.stt,
				renew=self._renew_credential,
				read_audio=self._mixer.read,
			)
			try:
				await self._speech.start()
			except Exception as e:
				LOGGER.exception("Could not open the speech connection, not reading this call", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "reading",
				})
				await self._mixer.close()
				self._mixer = None
				self._speech = None
				# Close the session Korsi opened. Leaving it dangling would hold a metering reservation
				# for a call that produced nothing, until the sweep noticed twenty minutes later.
				with suppress(Exception):
					await korsi.close_session(
						live_session_id=session.live_session_id,
						ended_at=datetime.now(UTC),
						reason=LiveCloseReason.BRIDGE_ERROR,
					)
				self._session = None
				self._korsi = None
				return

			self._segmenter = Segmenter(
				room_token=self.room_token,
				live_session_id=session.live_session_id,
				client=korsi,
				first_interval_seconds=session.first_segment_interval_seconds,
				interval_seconds=session.segment_interval_seconds,
				finalize=self._speech.finalize,
				drain_text=self._speech.drain_text,
				on_session_gone=self._on_session_gone,
			)
			self._segmenter.start()
			self.reading = True

		await self.send_incall()
		# Ask every participant already talking for their audio. Without this the bridge would only
		# pick up people who joined *after* it did, which on a call that was already running is
		# everybody who matters.
		await self._request_offers_from_active_publishers()

		LOGGER.info("Reading a call", extra={
			"room_token": self.room_token,
			"live_session_id": session.live_session_id,
			"meeting_id": session.meeting_id,
			"first_interval": session.first_segment_interval_seconds,
			"interval": session.segment_interval_seconds,
			"tag": "reading",
		})

	async def stop_reading(self, reason: LiveCloseReason) -> None:
		"""Stop reading, flush the last segment, and close the Korsi session.

		Order matters and is the reverse of `start_reading` for one reason: the segmenter is stopped
		with a flush *before* the speech connection closes, so the tail of the meeting -- which is where
		the decisions are -- is finalised and posted rather than discarded with the connection.
		"""
		async with self._reading_lock:
			if not self.reading:
				return
			self.reading = False
			segmenter, speech, mixer = self._segmenter, self._speech, self._mixer
			session, korsi = self._session, self._korsi
			self._segmenter = self._speech = self._mixer = None
			self._session = self._korsi = None
			self._call_announced = False

		if segmenter is not None:
			with suppress(Exception):
				await segmenter.stop(flush=True)

		if speech is not None:
			with suppress(Exception):
				await speech.aclose()

		if mixer is not None:
			with suppress(Exception):
				await mixer.close()

		# Drop the peer connections: they belong to the call, not to the room, and holding them across
		# a call boundary would leave the next call answering offers with stale transceivers.
		async with self.peer_connection_lock:
			connections = list(self.peer_connections.values())
			self.peer_connections.clear()
		for entry in connections:
			with suppress(Exception):
				await entry.pc.close()

		with suppress(Exception):
			await self.send_bye_to_call()

		if session is not None and korsi is not None:
			effective = reason
			if speech is not None and speech.failed.is_set():
				effective = LiveCloseReason.BRIDGE_ERROR
				LOGGER.warning("Closing a session whose speech connection failed", extra={
					"room_token": self.room_token,
					"failure": speech.failure_reason,
					"tag": "reading",
				})
			try:
				await korsi.close_session(
					live_session_id=session.live_session_id,
					ended_at=datetime.now(UTC),
					reason=effective,
				)
				LOGGER.info("Closed the live session", extra={
					"room_token": self.room_token,
					"live_session_id": session.live_session_id,
					"reason": effective.value,
					"segments": segmenter.segments_posted if segmenter else 0,
					"tag": "reading",
				})
			except Exception as e:  # noqa: BLE001 - the sweep is the backstop
				LOGGER.warning("Could not close the live session; Korsi will reap it", exc_info=e, extra={
					"room_token": self.room_token,
					"live_session_id": session.live_session_id,
					"tag": "reading",
				})

	async def _on_session_gone(self) -> None:
		"""korsi-api says this session is finished while the call is still up.

		Stop reading but keep watching the room. The call may well continue, and if a later call in the
		same room is accepted the bridge should read that one.
		"""
		await self.stop_reading(LiveCloseReason.CALL_ENDED)

	async def _renew_credential(self) -> LiveSttCredential:
		"""A fresh speech credential.

		Called only when Soniox says the current one is spent, never on a timer -- see the note in
		`constants.py`. A long meeting does this several times.
		"""
		if self._korsi is None or self._session is None:
			raise RuntimeError("cannot renew a speech credential outside a reading session")
		credential = await self._korsi.renew_credential(live_session_id=self._session.live_session_id)
		self._credential_expires_at = credential.expires_at
		LOGGER.info("Renewed the speech credential", extra={
			"room_token": self.room_token,
			"expires_at": credential.expires_at.isoformat(),
			"tag": "reading",
		})
		return credential

	async def _announce_call_ended(self, reason: LiveCloseReason) -> None:
		"""Tell the watcher the call is over, if there was one to end.

		Also resets `_call_announced`, which is what allows the next call in this room to be offered to
		Korsi. Without the reset a room would be read once and then never again for as long as the
		container lived.
		"""
		was_announced = self._call_announced
		self._call_announced = False
		if not was_announced and not self.reading:
			return
		asyncio.create_task(  # noqa: RUF006 - deliberately not awaited inside the monitor loop
			self._on_call_ended(self.room_token, reason),
			name=f"call-ended-{self.room_token}",
		)

	async def _drop_peer_connection(self, session_id: str) -> None:
		async with self.peer_connection_lock:
			entry = self.peer_connections.pop(session_id, None)
		if entry is None:
			return
		with suppress(Exception):
			if entry.pc.connectionState not in ("closed", "failed"):
				await entry.pc.close()

	async def _request_offers_from_active_publishers(self) -> None:
		"""Ask for audio from everyone already in the call.

		Best effort: the HPB answers with offers, which the monitor handles. A participant who is muted
		still has an audio track, so this is not restricted to people currently speaking.
		"""
		for session_id in list(self._publishers_in_call):
			with suppress(Exception):
				await self.send_offer_request(session_id)

	async def send_message(self, message: dict):
		if not self._server:
			LOGGER.error("No server connection, cannot send message", extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			return

		self.id += 1
		message["id"] = str(self.id)
		try:
			await self._server.send(json.dumps(message))
		except WebSocketException as e:
			LOGGER.exception("HPB websocket error, reconnecting...", exc_info=e, extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			if not self._reconnect_task or self._reconnect_task.done():
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.SHORT_RESUME))
			return
		except Exception as e:
			LOGGER.exception("Unexpected error send a message to HPB, ignoring", exc_info=e, extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			# ignore the exception, most probably TypeError which is not expected to happen anyway
			return

		LOGGER.debug("Message sent", extra={
			"id": self.id,
			"room_token": self.room_token,
			"sent_message": message,
			"tag": "send_message",
		})

	async def send_hello(self):
		nonce = token_urlsafe(64)
		await self.send_message({
			"type": "hello",
			"hello": {
				"version": "2.0",
				"auth": {
					"type": "internal",
					"params": {
						"random": nonce,
						"token": hmac_sha256(self.secret, nonce),
						"backend": self._backendURL,
					}
				},
			},
		})

	async def send_incall(self):
		await self.send_message({
			"type": "internal",
			"internal": {
				"type": "incall",
				"incall": {
					"incall": CallFlag.IN_CALL,
				},
			},
		})

	async def send_join(self):
		await self.send_message({
			"type": "room",
			"room": {
				"roomid": self.room_token,
				"sessionid": self.sessionid
			}
		})

	async def send_offer_request(self, publisher_session_id):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"type": "requestoffer",
					"roomType": "video"
				}
			}
		})

	async def send_offer_answer(self, publisher_session_id, offer_sid, sdp):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"to": publisher_session_id,
					"type": "answer",
					"roomType": "video",
					"sid": offer_sid,
					"payload": {
						"nick": "Korsi",
						"type": "answer",
						"sdp": sdp
					}
				}
			}
		})

	async def send_candidate(self, sender, offer_sid, candidate_str):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": sender,
				},
				"data": {
					"to": sender,
					"type": "candidate",
					"sid": offer_sid,
					"roomType": "video",
					"payload": {
						"candidate": {
							"candidate": candidate_str,
							"sdpMLineIndex": 0,
							"sdpMid": "0",
						}
					}
				}
			}
		})

	async def send_bye(self):
		await self.send_message({
			"type": "bye",
			"bye": {}
		})

	async def send_bye_to_call(self):
		"""Leave the call but stay in the room.

		`incall: 0` rather than `bye`, which is the distinction this fork needs and upstream does not:
		`bye` ends the whole signaling session, and the bridge has to keep its room session to notice
		the *next* call. So the bridge stops being a call participant and goes back to watching.
		"""
		await self.send_message({
			"type": "internal",
			"internal": {
				"type": "incall",
				"incall": {
					"incall": CallFlag.DISCONNECTED,
				},
			},
		})

	async def close(self):  # noqa: C901
		if self.defunct.is_set():
			LOGGER.debug("SpreedClient is already defunct, skipping close", extra={
				"room_token": self.room_token,
				"tag": "client",
			})
			return

		if self._reconnect_task and not self._reconnect_task.done():
			LOGGER.debug("Cancelling reconnect task", extra={
				"room_token": self.room_token,
				"tag": "reconnect",
			})
			self._reconnect_task.cancel()
		self._reconnect_task = None

		with suppress(Exception):
			if self._monitor and not self._monitor.done():
				LOGGER.debug("Cancelling monitor task", extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				# Cancel the monitor task if it's still running
				self._monitor.cancel()
		self._monitor = None

		with suppress(Exception):
			await self.send_bye()

		# Reading is torn down by `stop_reading`, which the watcher calls before this. Not repeated here
		# on purpose: closing the Korsi session is the one step in teardown that must happen exactly
		# once, and a second attempt from a close path would race the first.
		with suppress(Exception):
			for pc in self.peer_connections.values():
				if pc.pc.connectionState != "closed" and pc.pc.connectionState != "failed":
					LOGGER.debug("Closing peer connection", extra={
						"session_id": pc.session_id,
						"room_token": self.room_token,
						"tag": "peer_connection",
					})
					with suppress(Exception):
						await pc.pc.close()
			async with self.peer_connection_lock:
				self.peer_connections.clear()
			self.resumeid = None
			self.sessionid = None

		with suppress(Exception):
			if self._server and self._server.state == WsState.OPEN:
				LOGGER.info("Closing WebSocket connection for room", extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				# Close the WebSocket connection if it's still open
				await self._server.close()
			self._server = None

		# No leave callback. Upstream calls back into `Application` to delete itself from the registry,
		# because there the client's existence *is* the transcription. Here the watcher owns the
		# registry and reconciles it against Korsi's watchlist on every poll, so a client that has
		# become defunct is noticed and replaced rather than having to announce it.
		self.defunct.set()

	async def receive(self, timeout: int = 0) -> dict | None:
		if not self._server:
			LOGGER.debug("No server connection, cannot receive message", extra={
				"room_token": self.room_token,
				"tag": "receive",
			})
			return None

		# caller handles the exceptions
		if timeout > 0:
			received_msg = await asyncio.wait_for(self._server.recv(), timeout)
		else:
			received_msg = await self._server.recv()

		message = json.loads(received_msg)
		LOGGER.debug("Message received", extra={
			"recv_message": message,
			"room_token": self.room_token,
			"tag": "receive",
		})
		return message

	async def signalling_monitor(self):  # noqa: C901
		"""Monitor the signaling server for incoming messages."""
		while True:
			try:
				message = await self.receive()
			except WebSocketException as e:
				LOGGER.exception("HPB websocket error, reconnecting...", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._reconnect_task or self._reconnect_task.done():
					self._reconnect_task = asyncio.create_task(
						self.connect(reconnect=ReconnectMethod.SHORT_RESUME),
						name=f"close-{self.room_token}"
					)
				await asyncio.sleep(2)
				continue
			except asyncio.CancelledError:
				LOGGER.debug("Signalling monitor task cancelled", extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
				raise
			except Exception as e:
				LOGGER.exception("Unexpected error in signalling monitor", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
				break

			if message.get("type") == "error":
				LOGGER.error(
					"Error message received: %s\nDetails: %s",
					message.get("error", {}).get("message"),
					message.get("error", {}).get("details"),
					extra={
						"room_token": self.room_token,
						"recv_message": message,
						"tag": "monitor",
					},
				)
				if message.get("error", {}).get("code") == "processing_failed":
					# this is most probably related to a transcript reception failure on HPB side
					# we can try to continue
					continue
				if message.get("error", {}).get("code") == "client_not_found":
					# "No MCU client found to send message to."
					# transcript sent to a participant who is not in the call anymore
					continue

				# only close if the error is not recoverable
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
				return

			if (
				message["type"] == "event"
				and message["event"]["target"] == "participants"
				and message["event"]["type"] == "update"
			):
				LOGGER.info("Participants update received", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "participants",
				})

				if message["event"]["update"].get("all") and message["event"]["update"].get("incall") == 0:
					# The call ended for everybody. Upstream closes the whole client here; this fork
					# stops reading and keeps the room session, because the next call in this room
					# should be read too and rebuilding the signaling session for it would mean
					# missing its first minute.
					LOGGER.info("Call ended for everyone", extra={
						"room_token": self.room_token,
						"tag": "participants",
					})
					self._publishers_in_call.clear()
					await self._announce_call_ended(LiveCloseReason.CALL_ENDED)
					continue

				users_update = message["event"]["update"].get("users", [])
				if not users_update:
					continue

				for user_desc in users_update:
					if user_desc.get("internal", False):
						continue

					session_id = user_desc.get("sessionId")
					if not session_id:
						continue
					in_call = user_desc.get("inCall") or CallFlag.DISCONNECTED

					if in_call == CallFlag.DISCONNECTED:
						LOGGER.info("Participant left the call", extra={
							"session_id": session_id,
							"room_token": self.room_token,
							"tag": "participants",
						})
						self._publishers_in_call.discard(session_id)
						# Detach from the mix rather than tearing anything down. The speech connection
						# belongs to the call, so one person leaving a four-person meeting must not end
						# it -- which is precisely what upstream's per-publisher transcriber does.
						if self._mixer is not None:
							await self._mixer.detach(session_id)
						await self._drop_peer_connection(session_id)
						continue

					if in_call & CallFlag.IN_CALL and in_call & CallFlag.WITH_AUDIO:
						self._publishers_in_call.add(session_id)

						# First evidence that a call is happening in this room. Ask Korsi, once.
						if not self._call_announced:
							self._call_announced = True
							LOGGER.info("A call started in a watched room", extra={
								"room_token": self.room_token,
								"session_id": session_id,
								"tag": "participants",
							})
							# Detached: opening a session is an HTTP round trip and the monitor must
							# keep draining the signaling socket, or the HPB backs up behind us.
							asyncio.create_task(  # noqa: RUF006 - lifetime is the monitor's
								self._on_call_started(self.room_token, datetime.now(UTC)),
								name=f"call-started-{self.room_token}",
							)

						if not self.reading:
							# Not reading this call, either because Korsi has not answered yet or
							# because it declined. Requesting audio would cost bandwidth for nothing.
							continue

						async with self.peer_connection_lock:
							existing = self.peer_connections.get(session_id)
							if existing and existing.pc.connectionState not in ("closed", "failed"):
								continue
						await self.send_offer_request(session_id)
						continue

				# Everyone with audio has gone and only the bridge is left in the call.
				if self.reading and not self._publishers_in_call:
					LOGGER.info("No participants left in the call", extra={
						"room_token": self.room_token,
						"tag": "participants",
					})
					await self._announce_call_ended(LiveCloseReason.CALL_ENDED)

			if message["type"] == "message" and message["message"]["data"]["type"] == "offer":
				LOGGER.debug("Received offer message", extra={
					"recv_message": message,
					"room_token": self.room_token,
					"tag": "offer",
				})
				await self.handle_offer(message)
				continue

			if message["type"] == "message" and message["message"]["data"]["type"] == "candidate":
				LOGGER.debug("Received candidate message", extra={
					"recv_message": message,
					"peer_session_id": message["message"]["sender"]["sessionid"],
					"room_token": self.room_token,
					"tag": "candidate",
				})
				candidate = candidate_from_sdp(message["message"]["data"]["payload"]["candidate"]["candidate"])
				candidate.sdpMid = message["message"]["data"]["payload"]["candidate"]["sdpMid"]
				candidate.sdpMLineIndex = message["message"]["data"]["payload"]["candidate"]["sdpMLineIndex"]
				async with self.peer_connection_lock:
					if message["message"]["sender"]["sessionid"] not in self.peer_connections:
						continue
					await self.peer_connections[message["message"]["sender"]["sessionid"]].pc.addIceCandidate(candidate)
				continue

			if message["type"] == "bye":
				LOGGER.info("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "bye",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")

	async def handle_offer(self, message):  # noqa: C901
		"""Handle incoming offer messages."""
		spkr_sid = message["message"]["sender"]["sessionid"]
		async with self.peer_connection_lock:
			if (
				spkr_sid in self.peer_connections
				and self.peer_connections[spkr_sid].pc.connectionState != "closed"
				and self.peer_connections[spkr_sid].pc.connectionState != "failed"
			):
				LOGGER.debug("Peer connection for user already exists, skipping offer request", extra={
					"session_id": spkr_sid,
					"room_token": self.room_token,
					"tag": "participants",
				})
				return

		ice_servers = []
		for stunserver in self.hpb_settings.stunservers:
			ice_servers.append(
				RTCIceServer(urls=stunserver.urls)
			)
		for turnserver in self.hpb_settings.turnservers:
			ice_servers.append(
				RTCIceServer(
					urls=turnserver.urls,
					username=turnserver.username,
					credential=turnserver.credential,
				)
			)
		if len(ice_servers) == 0:
			ice_servers = None
		rtc_config = RTCConfiguration(iceServers=ice_servers)
		pc = RTCPeerConnection(configuration=rtc_config)
		weakself = weakref.ref(self)

		@pc.on("connectionstatechange")
		async def on_connectionstatechange():
			LOGGER.info("Peer connection state changed", extra={
				"session_id": spkr_sid,
				"connection_state": pc.connectionState,
				"room_token": weakself().room_token if weakself() else None,
				"tag": "peer_connection",
			})

			if not weakself():
				LOGGER.error("SpreedClient instance is gone, cannot handle connection state change", extra={
					"session_id": spkr_sid,
					"connection_state": pc.connectionState,
					"room_token": None,
					"tag": "peer_connection",
				})
				return

			if pc.connectionState == "failed":
				LOGGER.warning("Peer connection state for '%s' is '%s'", spkr_sid, pc.connectionState, extra={
					"session_id": spkr_sid,
					"connection_state": pc.connectionState,
					"room_token": weakself().room_token,
					"tag": "peer_connection",
				})
				async with weakself().peer_connection_lock:
					if spkr_sid in weakself().peer_connections:
						del weakself().peer_connections[spkr_sid]

		pc.addTransceiver("audio", direction="recvonly")
		@pc.on("track")
		async def on_track(track):
			if track.kind == "audio":
				LOGGER.debug("Receiving %s track from %s", track.kind, spkr_sid, extra={
					"session_id": spkr_sid,
					"room_token": weakself().room_token if weakself() else None,
					"tag": "track",
				})

				if not weakself():
					LOGGER.error("SpreedClient instance is gone, cannot handle track", extra={
						"session_id": spkr_sid,
						"room_token": None,
						"tag": "track",
					})
					return

				mixer = weakself()._mixer
				if mixer is None:
					# An offer answered after `stop_reading` tore the mixer down. Dropping the track is
					# correct: there is nothing to feed and no session to feed it to.
					LOGGER.debug("Track arrived with no mixer, dropping it", extra={
						"session_id": spkr_sid,
						"room_token": weakself().room_token,
						"tag": "track",
					})
					with suppress(Exception):
						track.stop()
					return

				# Into the mix, not into a transcriber of its own. One speech connection per call is
				# the whole cost argument (ADR-0021 D2), and it is also what lets the connection
				# outlive any individual participant.
				await mixer.attach(spkr_sid, AudioStream(track))

		async with self.peer_connection_lock:
			self.peer_connections[spkr_sid] = PeerConnection(session_id=spkr_sid, pc=pc)

		await pc.setRemoteDescription(
			RTCSessionDescription(type="offer", sdp=message["message"]["data"]["payload"]["sdp"])
		)

		answer = await pc.createAnswer()
		await pc.setLocalDescription(answer)
		await self.send_offer_answer(message["message"]["data"]["from"], message["message"]["data"]["sid"], answer.sdp)
		LOGGER.debug("Sent answer for offer from %s", spkr_sid, extra={
			"session_id": spkr_sid,
			"answer": answer,
			"room_token": self.room_token,
			"tag": "offer",
		})

		# @pc.on("icecandidate") may not work as well so we wait for the gathering to complete
		# https://github.com/aiortc/aiortc/issues/1344
		waited_for = 0
		while pc.iceGatheringState != "complete" and waited_for <= ICE_GATHERING_TIMEOUT:
			await asyncio.sleep(0.2)
			waited_for += 0.2

		if waited_for > ICE_GATHERING_TIMEOUT and pc.iceGatheringState != "complete":
			LOGGER.warning("Timed out waiting for ice gathering to complete, continuing with the peer connection still")

		local_sdp = pc.localDescription.sdp
		LOGGER.debug("Local SDP for %s:", spkr_sid, extra={
			"session_id": spkr_sid,
			"room_token": self.room_token,
			"local_sdp": local_sdp,
			"tag": "offer",
		})

		candidates = []
		for line in local_sdp.splitlines():
			if line.startswith("a=candidate:"):
				candidates.append(line[2:])
				await self.send_candidate(
					message["message"]["sender"]["sessionid"],
					message["message"]["data"]["sid"],
					line[2:],
				)

		LOGGER.info("Sent candidates to the peer", extra={ "candidates": candidates })
