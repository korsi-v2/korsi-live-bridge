#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The speech connection: mixed audio out, confirmed text in.

Replaces upstream's `VoskTranscriber`, and the difference is not just the provider.

**Full duplex, not lock-step.** Upstream sends a chunk and awaits exactly one reply, both under one
mutex. That works against a server that answers each chunk once and nothing else. Soniox answers when
it has something to say -- several responses for one chunk, none for another, an unsolicited `finished`
at the end -- so a send/recv pair would desynchronise on the first busy moment and then read every
response as the answer to the wrong chunk. Here a sender pump and a receiver loop run independently
and share no lock.

**Only final tokens are kept.** Soniox emits provisional tokens immediately and revises them; a token
marked `is_final` never changes again. Live captions want the provisional stream, because a caption
that appears late is useless. Korsi wants the opposite: the text goes into an LLM analysis every few
minutes, and provisional text means analysing sentences the model already retracted. So non-final
tokens are counted (they prove audio is being processed) and discarded.

**The credential is refreshed, not held.** Korsi mints a temporary key scoped to one session with a
lifetime in minutes and a `max_session_duration_seconds` cap. Soniox answers `403
temp_api_key_session_expired` when the cap elapses and `413 max_duration_reached` at its own 300-minute
ceiling. Both mean the same thing here: get a fresh credential from Korsi and reconnect. Neither is an
error, and a two-hour meeting hits the first several times.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

from constants import (
	SONIOX_CONNECT_TIMEOUT,
	SONIOX_FINALIZE_GRACE_SECONDS,
	SONIOX_MAX_RECONNECTS,
	SONIOX_RECONNECT_BACKOFF_SECONDS,
)
from korsi_types import LiveSttCredential
from utils import get_ssl_context
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

LOGGER = logging.getLogger("lt")

#: Soniox error types that mean "this connection is spent, open another one with a fresh key".
#: Everything else on this list would be a genuine failure to report.
_RENEWABLE_ERRORS = frozenset({"temp_api_key_session_expired", "max_duration_reached"})

#: Error types worth another connection with the *same* key, after a pause.
_TRANSIENT_ERRORS = frozenset({"service_unavailable", "internal_error", "request_timeout"})


class SonioxStream:
	"""One speech connection for one call.

	Survives participants joining and leaving, because it is fed by the mixer rather than by a track.
	Survives credential expiry, because it can ask for another. Does not survive the call ending, which
	is the only thing that should end it.
	"""

	def __init__(
		self,
		*,
		room_token: str,
		credential: LiveSttCredential,
		renew: Callable[[], Awaitable[LiveSttCredential]],
		read_audio: Callable[[], Awaitable[bytes]],
	) -> None:
		self._room_token = room_token
		self._credential = credential
		self._renew = renew
		self._read_audio = read_audio

		self._ws: ClientConnection | None = None
		self._sender: asyncio.Task | None = None
		self._receiver: asyncio.Task | None = None
		self._closing = False
		self._reconnects = 0

		#: Confirmed text since the last `drain_text()`. A string per token, joined on read: tokens are
		#: subwords and arrive thousands to a meeting, and `str +=` in a loop is quadratic.
		self._final_parts: list[str] = []
		self._final_lock = asyncio.Lock()

		#: How much audio Soniox says it has turned into final tokens. The honest measure of progress:
		#: a call where this stops advancing is a call where the audio path has broken, which looks
		#: exactly like a quiet meeting if you only count bytes sent.
		self.final_audio_proc_ms = 0
		self.total_audio_proc_ms = 0
		self.bytes_sent = 0

		#: Set when the provider ends the stream in a way reconnecting cannot fix.
		self.failed = asyncio.Event()
		self.failure_reason: str | None = None

	# ------------------------------------------------------------------ lifecycle

	async def start(self) -> None:
		await self._open()
		self._sender = asyncio.create_task(self._run_sender(), name=f"soniox-send-{self._room_token}")
		self._receiver = asyncio.create_task(self._run_receiver(), name=f"soniox-recv-{self._room_token}")

	async def _open(self) -> None:
		url = self._credential.websocket_url
		ssl_ctx = get_ssl_context(url)
		self._ws = await connect(
			url,
			**({"ssl": ssl_ctx} if ssl_ctx else {}),  # type: ignore[arg-type]
			open_timeout=SONIOX_CONNECT_TIMEOUT,
			# Soniox sends no pings of its own during silence and this connection can be idle for
			# minutes in a quiet meeting. Keeping our own keepalive on means a dead TCP path is
			# noticed in seconds rather than when somebody finally speaks.
			ping_interval=20,
			ping_timeout=20,
			max_size=None,
		)
		await self._ws.send(json.dumps(self._start_request()))
		LOGGER.info("Opened the speech connection", extra={
			"room_token": self._room_token,
			"model": self._credential.model,
			"language_hints": list(self._credential.language_hints),
			"tag": "soniox",
		})

	def _start_request(self) -> dict:
		"""The config frame.

		Every value comes from the credential korsi-api minted, including the audio format and sample
		rate. Nothing here is a local constant, on purpose: this container runs in a customer's
		Nextcloud, and a hard-coded model name is a coordinated redeploy the day Korsi changes model.

		Diarization is off. It costs more, and the transcript this produces must never be used for
		attribution -- the room is one microphone, so provider speaker labels would be guesses
		presented as facts. Confirmed speakers come from the batch pipeline afterwards (ADR-0021 D6).
		"""
		request: dict = {
			"api_key": self._credential.api_key,
			"model": self._credential.model,
			"audio_format": self._credential.audio_format,
			"sample_rate": self._credential.sample_rate,
			"num_channels": self._credential.num_channels,
			"enable_speaker_diarization": False,
			# Let the model finalise on natural pauses rather than only when we ask. A meeting is
			# mostly pauses, and each one is a chance to confirm text for free.
			"enable_endpoint_detection": True,
		}
		if self._credential.language_hints:
			request["language_hints"] = list(self._credential.language_hints)
		return request

	async def aclose(self) -> None:
		"""Stop sending, ask Soniox to finish, and let go.

		The empty frame is the documented graceful end: Soniox flushes whatever it was holding, sends
		a `finished` response and closes. Worth waiting a moment for, because the last few seconds of
		a meeting are often the part with the decision in it.
		"""
		self._closing = True
		if self._sender and not self._sender.done():
			self._sender.cancel()
		with contextlib.suppress(asyncio.CancelledError, Exception):
			if self._sender:
				await self._sender
		self._sender = None

		if self._ws is not None:
			with contextlib.suppress(Exception):
				await self._ws.send(b"")
			if self._receiver is not None:
				# Give the receiver a moment to read the `finished` response the empty frame triggers,
				# so the last tokens land in `_final_parts` before the segmenter's flush drains it.
				with contextlib.suppress(TimeoutError, Exception):
					await asyncio.wait_for(asyncio.shield(self._receiver), SONIOX_FINALIZE_GRACE_SECONDS)

		if self._receiver and not self._receiver.done():
			self._receiver.cancel()
		with contextlib.suppress(asyncio.CancelledError, Exception):
			if self._receiver:
				await self._receiver
		self._receiver = None

		if self._ws is not None:
			with contextlib.suppress(Exception):
				await self._ws.close()
			self._ws = None

	# ------------------------------------------------------------------ text

	async def finalize(self) -> None:
		"""Ask Soniox to confirm everything it is holding, and wait briefly for it.

		Called just before cutting a segment. Without it, the sentence somebody was in the middle of
		when the interval elapsed sits unfinalised and lands in the *next* segment, which is how a
		decision gets attributed to the wrong five minutes of a meeting.

		Best effort by design: if the provider does not answer in time the segment goes without the
		tail rather than the interval slipping.
		"""
		if self._ws is None or self._closing:
			return
		with contextlib.suppress(Exception):
			await self._ws.send(json.dumps({"type": "finalize"}))
			await asyncio.sleep(SONIOX_FINALIZE_GRACE_SECONDS)

	async def drain_text(self) -> str:
		"""Every confirmed token since the last drain, and reset.

		Draining rather than reading means a segment is posted exactly once: korsi-api stores segments
		as append-only rows keyed by sequence, so re-sending text already accepted would double it in
		the transcript the analysis reads.
		"""
		async with self._final_lock:
			text = "".join(self._final_parts)
			self._final_parts.clear()
		return text.strip()

	# ------------------------------------------------------------------ pumps

	async def _run_sender(self) -> None:
		"""Mixed audio to the provider, as fast as the mixer produces it.

		No pacing here. The mixer is already the clock -- it emits a chunk per 20 ms of wall time --
		so this loop naturally runs at real time and adding a second rate limit would only add jitter.
		"""
		try:
			while not self._closing:
				chunk = await self._read_audio()
				ws = self._ws
				if ws is None:
					continue
				try:
					await ws.send(chunk)
					self.bytes_sent += len(chunk)
				except ConnectionClosed:
					# The receiver owns reconnection; it saw the close and knows why. Losing this
					# chunk is correct: it is 20 ms of a meeting, and holding it would put stale
					# audio at the front of the next connection.
					await asyncio.sleep(0.1)
		except asyncio.CancelledError:
			raise

	async def _run_receiver(self) -> None:
		"""Read responses, keep the confirmed text, and reconnect when the provider says to."""
		try:
			while not self._closing:
				message = await self._next_message()
				if message is None:
					return
				if not message:
					continue

				if message.get("error_code") or message.get("error_type"):
					if not await self._handle_error(message):
						return
					continue

				await self._absorb(message)

				if message.get("finished"):
					LOGGER.info("Speech provider finished the stream", extra={
						"room_token": self._room_token,
						"final_audio_proc_ms": self.final_audio_proc_ms,
						"tag": "soniox",
					})
					return
		except asyncio.CancelledError:
			raise
		except Exception as e:  # noqa: BLE001 - a failed receiver must report, not vanish
			LOGGER.exception("Error reading from the speech provider", exc_info=e, extra={
				"room_token": self._room_token,
				"tag": "soniox",
			})
			self.failure_reason = str(e)
			self.failed.set()

	async def _next_message(self) -> dict | None:
		"""One decoded response.

		Three outcomes, and the receiver loop needs to tell them apart: a dict to act on, an empty dict
		meaning "nothing here, keep reading", and `None` meaning the loop is over.
		"""
		ws = self._ws
		if ws is None:
			return None
		try:
			raw = await ws.recv()
		except ConnectionClosed as e:
			if self._closing:
				return None
			LOGGER.warning("Speech connection closed unexpectedly", extra={
				"room_token": self._room_token,
				"code": getattr(e, "code", None),
				"tag": "soniox",
			})
			return {} if await self._reconnect(renew_credential=False) else None

		if isinstance(raw, bytes):
			return {}
		try:
			decoded = json.loads(raw)
		except json.JSONDecodeError:
			LOGGER.error("Non-JSON response from the speech provider", extra={
				"room_token": self._room_token,
				"tag": "soniox",
			})
			return {}
		return decoded if isinstance(decoded, dict) else {}

	async def _absorb(self, message: dict) -> None:
		"""Keep the confirmed tokens; count the rest.

		Token `text` carries its own leading spaces, so joining without a separator reproduces the
		sentence. Inserting one would put a space inside every word split across two subword tokens.
		"""
		tokens = message.get("tokens") or []
		confirmed = [str(t.get("text", "")) for t in tokens if t.get("is_final")]
		if confirmed:
			async with self._final_lock:
				self._final_parts.extend(confirmed)

		self.final_audio_proc_ms = int(message.get("final_audio_proc_ms") or self.final_audio_proc_ms)
		self.total_audio_proc_ms = int(message.get("total_audio_proc_ms") or self.total_audio_proc_ms)

	async def _handle_error(self, message: dict) -> bool:
		"""Decide what a provider error means. Returns False when the stream is over for good."""
		error_type = str(message.get("error_type") or "")
		LOGGER.warning("Speech provider returned an error", extra={
			"room_token": self._room_token,
			"error_type": error_type,
			"error_code": message.get("error_code"),
			"error_message": message.get("error_message"),
			"provider_request_id": message.get("request_id"),
			"tag": "soniox",
		})

		if error_type in _RENEWABLE_ERRORS:
			# Expected on any long meeting, not a fault. A fresh key from Korsi and a new connection.
			return await self._reconnect(renew_credential=True)
		if error_type in _TRANSIENT_ERRORS:
			return await self._reconnect(renew_credential=False)

		# Anything else -- a bad request, an exhausted balance, a rate limit -- is not fixed by trying
		# again with the same inputs, and the honest outcome is a call whose live reading stops and
		# says why.
		self.failure_reason = f"{error_type}: {message.get('error_message')}"
		self.failed.set()
		return False

	async def _reconnect(self, *, renew_credential: bool) -> bool:
		"""Open a new connection, optionally with a new credential. Returns False when out of attempts.

		Text already confirmed is kept: it is in `_final_parts`, which belongs to the call rather than
		to the connection. What is lost is the audio in flight during the gap, which is a second or two
		and unrecoverable in any case -- there is no buffer to replay from, because Korsi never stores
		the audio.
		"""
		if self._closing:
			return False

		self._reconnects += 1
		if self._reconnects > SONIOX_MAX_RECONNECTS:
			LOGGER.error("Giving up on the speech connection after %d reconnects", self._reconnects - 1, extra={
				"room_token": self._room_token,
				"tag": "soniox",
			})
			self.failure_reason = f"speech connection failed {self._reconnects - 1} times"
			self.failed.set()
			return False

		with contextlib.suppress(Exception):
			if self._ws is not None:
				await self._ws.close()
		self._ws = None

		if renew_credential:
			try:
				self._credential = await self._renew()
			except Exception as e:  # noqa: BLE001 - reported as a stream failure below
				LOGGER.exception("Could not renew the speech credential", exc_info=e, extra={
					"room_token": self._room_token,
					"tag": "soniox",
				})
				self.failure_reason = f"credential renewal failed: {e}"
				self.failed.set()
				return False
		else:
			await asyncio.sleep(SONIOX_RECONNECT_BACKOFF_SECONDS * self._reconnects)

		try:
			await self._open()
		except Exception as e:  # noqa: BLE001 - one more loop of _reconnect decides whether to stop
			LOGGER.exception("Could not reopen the speech connection", exc_info=e, extra={
				"room_token": self._room_token,
				"tag": "soniox",
			})
			return await self._reconnect(renew_credential=False)

		LOGGER.info("Reopened the speech connection", extra={
			"room_token": self._room_token,
			"attempt": self._reconnects,
			"renewed_credential": renew_credential,
			"tag": "soniox",
		})
		return True
