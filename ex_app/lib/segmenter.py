#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cutting the confirmed transcript into intervals and posting them to Korsi.

The one place in the bridge that decides *when* text leaves, and it decides nothing: both intervals
arrive in the session korsi-api opened, and every accept echoes the current one so a cadence change
reaches a call already in progress. ADR-0021 D9 -- policy lives in Korsi, not in the customer's
Nextcloud.

**Empty segments are posted, not skipped.** A quiet twenty minutes is indistinguishable from a dead
bridge unless the bridge keeps speaking: korsi-api closes sessions whose newest segment is older than
`ABANDONED_AFTER_SECONDS`, so silence has to be reported rather than withheld. The server is built for
it -- an empty segment advances `last_segment_at` and queues no analysis, so the heartbeat is free.

**Sequence numbers are ours and contiguous.** korsi-api stores segments as append-only rows unique on
`(session, sequence)` and ignores a duplicate, which makes a retried post harmless. It also means a
segment must not be posted twice with two different sequences, so the counter advances only on a
successful accept.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from korsi_client import KorsiClient
from korsi_types import KorsiApiError, LiveSessionClosed

LOGGER = logging.getLogger("lt")


class Segmenter:
	"""Posts one interval of transcript at a time, for one live session."""

	def __init__(
		self,
		*,
		room_token: str,
		live_session_id: str,
		client: KorsiClient,
		first_interval_seconds: int,
		interval_seconds: int,
		finalize: Callable[[], Awaitable[None]],
		drain_text: Callable[[], Awaitable[str]],
		on_session_gone: Callable[[], Awaitable[None]],
	) -> None:
		self._room_token = room_token
		self._live_session_id = live_session_id
		self._client = client
		self._first_interval = first_interval_seconds
		self._interval = interval_seconds
		self._finalize = finalize
		self._drain_text = drain_text
		self._on_session_gone = on_session_gone

		self._task: asyncio.Task | None = None
		self._stopping = False
		self._sequence = 0
		self._started_at = time.monotonic()
		#: Where the next segment begins, in ms since the session opened. Carried rather than
		#: recomputed so segments tile the call exactly, with no gap and no overlap even when a post
		#: took a moment.
		self._cursor_ms = 0
		#: Text drained from the speech stream that failed to reach korsi-api. Held here rather than
		#: pushed back into the stream, because the stream's buffer belongs to the receiver task and
		#: reaching into it from the segmenter would be two writers on one list.
		self._carried = ""

	@property
	def segments_posted(self) -> int:
		return self._sequence

	def start(self) -> None:
		if self._task is None or self._task.done():
			self._started_at = time.monotonic()
			self._task = asyncio.create_task(self._run(), name=f"segmenter-{self._room_token}")

	async def stop(self, *, flush: bool = True) -> None:
		"""Stop cutting intervals, optionally posting whatever is left first.

		Flushing matters more than it looks: a meeting that ends four minutes into a five-minute
		interval would otherwise throw away its last four minutes, and the end of a meeting is where
		the decisions are.
		"""
		self._stopping = True
		if self._task and not self._task.done():
			self._task.cancel()
			with suppress(asyncio.CancelledError, Exception):
				await self._task
		self._task = None

		if flush:
			try:
				await self._finalize()
				await self._post_once()
			except LiveSessionClosed:
				LOGGER.info("Session already closed, dropping the final segment", extra={
					"room_token": self._room_token,
					"tag": "segment",
				})
			except Exception as e:  # noqa: BLE001 - the call is ending either way
				LOGGER.warning("Could not post the final segment", exc_info=e, extra={
					"room_token": self._room_token,
					"tag": "segment",
				})

	async def _run(self) -> None:
		"""Sleep an interval, cut, post, repeat.

		The first interval is deliberately much shorter than the rest. A panel that says "nothing yet"
		for the first five minutes of a meeting is a panel nobody opens again, and the point of a live
		reading is to be there while the meeting can still change.
		"""
		wait = self._first_interval
		try:
			while not self._stopping:
				await asyncio.sleep(wait)
				try:
					await self._finalize()
					await self._post_once()
				except LiveSessionClosed:
					LOGGER.info("Korsi closed this session, stopping the segmenter", extra={
						"room_token": self._room_token,
						"live_session_id": self._live_session_id,
						"tag": "segment",
					})
					await self._on_session_gone()
					return
				except KorsiApiError as e:
					# Keep the text and the sequence. The next interval carries both this interval's
					# transcript and the next one, which is a longer segment rather than a lost one.
					LOGGER.warning("Segment post failed, retrying on the next interval", exc_info=e, extra={
						"room_token": self._room_token,
						"tag": "segment",
					})
				wait = self._interval
		except asyncio.CancelledError:
			raise

	async def _post_once(self) -> None:
		"""Cut the accumulated text at the current instant and send it.

		Raises whatever `append_segment` raises. Caller decides -- a closed session ends the call, a
		transient failure does not.
		"""
		text = await self._take_text()
		ended_ms = int((time.monotonic() - self._started_at) * 1000)
		if ended_ms <= self._cursor_ms:
			# Guards the flush-immediately-after-a-post case, where the interval is zero-width and
			# korsi-api would reject `ended_ms <= started_ms`.
			ended_ms = self._cursor_ms + 1

		sequence = self._sequence + 1
		try:
			accepted = await self._client.append_segment(
				live_session_id=self._live_session_id,
				sequence=sequence,
				started_ms=self._cursor_ms,
				ended_ms=ended_ms,
				text=text,
			)
		except Exception:
			# Hold the text so the next attempt carries it. Losing a segment to a failed post is how
			# a live reading develops a hole nobody can explain afterwards. `_cursor_ms` deliberately
			# does not advance either, so the retried segment still covers the interval it was read
			# from rather than claiming to be the later one it was sent in.
			self._carried = text
			raise

		self._sequence = accepted.accepted_sequence
		self._cursor_ms = ended_ms
		if accepted.segment_interval_seconds > 0 and accepted.segment_interval_seconds != self._interval:
			LOGGER.info("Korsi changed the segment interval", extra={
				"room_token": self._room_token,
				"was": self._interval,
				"now": accepted.segment_interval_seconds,
				"tag": "segment",
			})
			self._interval = accepted.segment_interval_seconds

		LOGGER.info("Posted a transcript segment", extra={
			"room_token": self._room_token,
			"live_session_id": self._live_session_id,
			"sequence": self._sequence,
			"chars": len(text),
			"snapshot_queued": accepted.snapshot_queued,
			"tag": "segment",
		})

	async def _take_text(self) -> str:
		"""Newly confirmed text, behind anything a previous post failed to deliver.

		The stream is always drained, even when there is carried text: leaving it would mean the
		buffer growing for the rest of the call, and the transcript arriving in the wrong order once
		the post finally succeeded.
		"""
		carried = self._carried
		self._carried = ""
		fresh = await self._drain_text()
		if carried and fresh:
			return f"{carried} {fresh}"
		return carried or fresh
