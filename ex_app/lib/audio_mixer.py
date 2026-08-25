#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every publisher in one call, summed into a single mono stream.

**Why one stream and not one per speaker.** Upstream opens a transcriber per publisher, which is right
for live captions: each caption is labelled with who said it. Korsi is serving organizations whose
meetings are mostly people in a room, joining through one laptop -- so a per-publisher split would pay
the speech provider N times to separate voices that arrive on one microphone anyway, and would still
not tell you who was speaking. One stream, one bill. ADR-0021 D2.

What is given up is speaker attribution, and that loss is deliberate and bounded: a live reading is
explicitly not the meeting's record. Diarization and confirmed speaker names come from the recording
afterwards, through the batch pipeline, and only that transcript is citable.

**Why this resamples when upstream does not.** Upstream's transcriber downmixes stereo to mono with a
numpy mean and sends the result to Vosk declaring 48 kHz -- while never reading `frame.sample_rate` or
`frame.format`. It works because aiortc's Opus decoder happens to emit s16 48 kHz. That is an
assumption about a dependency's internals, holding up audio somebody is billed to transcribe, and
silently producing chipmunk-speech if it ever stops being true. `AudioResampler` states the contract
instead, and libav does the conversion.

**Why a clock and not frame-by-frame.** Mixing means deciding what "at the same time" means. Frames
from different peer connections arrive interleaved and at slightly different rates, and a mixer that
summed whatever was to hand would drift. So each track is drained into its own buffer by its own task,
and one clock task takes an equal slice from every buffer on a fixed cadence -- padding with silence
for a publisher who has not delivered, which is exactly what a muted participant should sound like.
"""

import asyncio
import logging
import time
from contextlib import suppress

import numpy as np
from av.audio.resampler import AudioResampler
from constants import (
	MIXER_CHUNK_MS,
	MIXER_MAX_BUFFER_CHUNKS,
	MIXER_NUM_CHANNELS,
	MIXER_SAMPLE_RATE,
)
from livetypes import StreamEndedException

LOGGER = logging.getLogger("lt")

#: Samples in one mixed chunk. 20 ms at 48 kHz = 960, which is also one Opus frame, so in the common
#: case a chunk is one frame per publisher and no partial-frame arithmetic happens at all.
CHUNK_SAMPLES = MIXER_SAMPLE_RATE * MIXER_CHUNK_MS // 1000

#: Bytes in one mixed chunk, s16 mono.
CHUNK_BYTES = CHUNK_SAMPLES * 2


class _TrackBuffer:
	"""One publisher's audio, normalised and waiting to be mixed.

	Holds `int16` samples in a list of arrays rather than one growing array: a call runs for hours and
	repeatedly reallocating a buffer to append 20 ms is the kind of cost that only shows up in
	production.
	"""

	def __init__(self, session_id: str) -> None:
		self.session_id = session_id
		self._pending: list[np.ndarray] = []
		self._pending_samples = 0
		self.ended = False

	def push(self, samples: np.ndarray) -> None:
		self._pending.append(samples)
		self._pending_samples += samples.size

		# A publisher whose audio nobody is draining is a memory leak with a long fuse. This can only
		# happen if the mixer clock has stalled, in which case the oldest audio is the least useful
		# thing to keep.
		limit = CHUNK_SAMPLES * MIXER_MAX_BUFFER_CHUNKS
		while self._pending_samples > limit and self._pending:
			dropped = self._pending.pop(0)
			self._pending_samples -= dropped.size
			LOGGER.warning("Mixer buffer overflow, dropped %d samples", dropped.size, extra={
				"session_id": self.session_id,
				"tag": "mixer",
			})

	def take(self, count: int) -> np.ndarray:
		"""Exactly `count` samples, zero-padded when the publisher has not delivered that many.

		Padding rather than waiting, because waiting for one participant would stop the clock for
		everybody. A publisher who is muted, whose connection is stalling, or who has just joined
		contributes silence, which is the correct contribution.
		"""
		if self._pending_samples == 0:
			return np.zeros(count, dtype=np.int16)

		chunks: list[np.ndarray] = []
		gathered = 0
		while gathered < count and self._pending:
			head = self._pending[0]
			needed = count - gathered
			if head.size <= needed:
				chunks.append(head)
				gathered += head.size
				self._pending.pop(0)
			else:
				chunks.append(head[:needed])
				self._pending[0] = head[needed:]
				gathered += needed
		self._pending_samples -= gathered

		if gathered < count:
			chunks.append(np.zeros(count - gathered, dtype=np.int16))
		return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


class AudioMixer:
	"""N tracks in, one mono s16 48 kHz stream out.

	Tracks are attached and detached while the call runs; the output stream does not notice. That is
	the property the whole design needs -- the speech connection is per *call*, so a participant
	leaving must not end it, which is exactly what upstream's per-publisher transcriber does.
	"""

	def __init__(self) -> None:
		self._buffers: dict[str, _TrackBuffer] = {}
		self._pullers: dict[str, asyncio.Task] = {}
		self._lock = asyncio.Lock()
		self._out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIXER_MAX_BUFFER_CHUNKS)
		self._clock: asyncio.Task | None = None
		self._closed = False

	@property
	def publisher_count(self) -> int:
		return len(self._buffers)

	async def start(self) -> None:
		if self._clock is None or self._clock.done():
			self._clock = asyncio.create_task(self._run_clock(), name="mixer-clock")

	async def attach(self, session_id: str, stream) -> None:  # noqa: ANN001 - AudioStream, avoiding a cycle
		"""Start draining one publisher's track into the mix."""
		async with self._lock:
			if session_id in self._buffers:
				LOGGER.debug("Track already attached to the mixer", extra={
					"session_id": session_id,
					"tag": "mixer",
				})
				return
			buffer = _TrackBuffer(session_id)
			self._buffers[session_id] = buffer
			self._pullers[session_id] = asyncio.create_task(
				self._run_puller(buffer, stream), name=f"mixer-pull-{session_id}"
			)
		LOGGER.info("Attached a track to the mixer", extra={
			"session_id": session_id,
			"publishers": len(self._buffers),
			"tag": "mixer",
		})

	async def detach(self, session_id: str) -> None:
		"""Stop draining one publisher. The mixed stream continues."""
		async with self._lock:
			task = self._pullers.pop(session_id, None)
			self._buffers.pop(session_id, None)
		if task and not task.done():
			task.cancel()
			with suppress(asyncio.CancelledError, Exception):
				await task
		LOGGER.info("Detached a track from the mixer", extra={
			"session_id": session_id,
			"publishers": len(self._buffers),
			"tag": "mixer",
		})

	async def read(self) -> bytes:
		"""The next mixed chunk. Blocks until the clock produces one."""
		return await self._out.get()

	async def close(self) -> None:
		self._closed = True
		if self._clock and not self._clock.done():
			self._clock.cancel()
			with suppress(asyncio.CancelledError, Exception):
				await self._clock
		self._clock = None
		async with self._lock:
			pullers = list(self._pullers.values())
			self._pullers.clear()
			self._buffers.clear()
		for task in pullers:
			if not task.done():
				task.cancel()
		for task in pullers:
			with suppress(asyncio.CancelledError, Exception):
				await task
		while not self._out.empty():
			with suppress(Exception):
				self._out.get_nowait()

	# ------------------------------------------------------------------ internals

	async def _run_puller(self, buffer: _TrackBuffer, stream) -> None:  # noqa: ANN001
		"""Drain one track, normalising every frame to mono s16 48 kHz.

		One resampler per track, kept across frames on purpose: `AudioResampler` carries filter state,
		and building a fresh one per frame both costs more and puts a discontinuity at every frame
		boundary.
		"""
		resampler = AudioResampler(
			format="s16",
			layout="mono" if MIXER_NUM_CHANNELS == 1 else "stereo",
			rate=MIXER_SAMPLE_RATE,
		)
		try:
			while True:
				frame = await stream.receive()
				for converted in _resample(resampler, frame):
					samples = np.frombuffer(converted.planes[0], dtype=np.int16)
					# `planes[0]` can be longer than the frame: libav pads to an alignment boundary,
					# and the padding is not silence, it is whatever was in that memory.
					valid = converted.samples * MIXER_NUM_CHANNELS
					buffer.push(np.array(samples[:valid], dtype=np.int16))
		except asyncio.CancelledError:
			raise
		except StreamEndedException:
			LOGGER.debug("Track ended, leaving the mix", extra={
				"session_id": buffer.session_id,
				"tag": "mixer",
			})
		except Exception as e:  # noqa: BLE001 - one publisher's failure must not end the call
			LOGGER.exception("Error draining a track into the mixer", exc_info=e, extra={
				"session_id": buffer.session_id,
				"tag": "mixer",
			})
		finally:
			buffer.ended = True

	async def _run_clock(self) -> None:
		"""Emit one mixed chunk every `MIXER_CHUNK_MS` of wall time.

		Paced against a monotonic deadline rather than `sleep(chunk)`, so the small overshoot of every
		iteration does not accumulate: at 20 ms a one-millisecond drift per tick is three seconds an
		hour, and the speech provider is being told this audio is real time.
		"""
		next_deadline = time.monotonic()
		try:
			while not self._closed:
				next_deadline += MIXER_CHUNK_MS / 1000
				delay = next_deadline - time.monotonic()
				if delay > 0:
					await asyncio.sleep(delay)
				else:
					# Fell behind. Give up the slices we missed rather than bursting to catch up,
					# which would send the provider audio faster than real time.
					next_deadline = time.monotonic()

				chunk = await self._mix_once()
				if chunk is None:
					continue
				try:
					self._out.put_nowait(chunk)
				except asyncio.QueueFull:
					# Nothing is reading the mix. Drop the oldest chunk: a stalled sender recovering
					# into a large backlog is worse than a gap, because it arrives as a burst the
					# provider reads as speech at the wrong time.
					with suppress(Exception):
						self._out.get_nowait()
					with suppress(Exception):
						self._out.put_nowait(chunk)
					LOGGER.warning("Mixer output queue full, dropped a chunk", extra={"tag": "mixer"})
		except asyncio.CancelledError:
			raise

	async def _mix_once(self) -> bytes | None:
		"""Sum one slice from every attached publisher.

		Returns `None` when nobody is publishing. Silence would be defensible -- it is what the room
		sounded like -- but sending it means paying to transcribe the gap between a bridge joining and
		the first participant unmuting, which on a scheduled call is minutes.
		"""
		async with self._lock:
			buffers = list(self._buffers.values())

		if not buffers:
			return None

		if len(buffers) == 1:
			return buffers[0].take(CHUNK_SAMPLES).tobytes()

		# int32 to sum in, because two people talking at once overflows int16 and wrapping turns a
		# loud moment into noise the transcriber reads as a different word.
		accumulator = np.zeros(CHUNK_SAMPLES, dtype=np.int32)
		for buffer in buffers:
			accumulator += buffer.take(CHUNK_SAMPLES)
		np.clip(accumulator, -32768, 32767, out=accumulator)
		return accumulator.astype(np.int16).tobytes()


def _resample(resampler: AudioResampler, frame) -> list:  # noqa: ANN001, ANN201
	"""`AudioResampler.resample` across PyAV versions.

	Older releases return a single frame, newer ones a list. Normalising here rather than pinning a
	version: this bridge is deployed by AppAPI into images built at times Korsi does not choose.
	"""
	converted = resampler.resample(frame)
	if converted is None:
		return []
	if isinstance(converted, list):
		return converted
	return [converted]
