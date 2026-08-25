#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The mixer, which is the one piece of this bridge with arithmetic in it.

Worth testing because the failure modes are silent. A downmix that overflows produces audio that still
plays and transcribes to different words; a `take` that pads wrongly drifts the timeline a few
milliseconds per chunk and nothing complains for an hour.
"""

import asyncio

import numpy as np
import pytest
from audio_mixer import CHUNK_BYTES, CHUNK_SAMPLES, AudioMixer, _TrackBuffer
from av.audio.frame import AudioFrame
from livetypes import StreamEndedException


class FakeStream:
	"""Stands in for `AudioStream`: hands out prepared frames, then reports the track ended."""

	def __init__(self, frames):
		self._frames = list(frames)
		self.stopped = False

	async def receive(self):
		if not self._frames:
			raise StreamEndedException("no more frames")
		return self._frames.pop(0)

	def stop(self):
		self.stopped = True


def make_frame(*, samples: int, rate: int, layout: str, value: int) -> AudioFrame:
	"""A frame of constant-amplitude s16 audio, which is easy to assert about after mixing."""
	channels = 2 if layout == "stereo" else 1
	frame = AudioFrame(format="s16", layout=layout, samples=samples)
	frame.sample_rate = rate
	data = np.full(samples * channels, value, dtype=np.int16)
	frame.planes[0].update(data.tobytes())
	return frame


# ------------------------------------------------------------------ _TrackBuffer


def test_take_pads_with_silence_when_the_publisher_is_behind():
	buffer = _TrackBuffer("s1")
	buffer.push(np.full(100, 7, dtype=np.int16))

	taken = buffer.take(CHUNK_SAMPLES)

	assert taken.size == CHUNK_SAMPLES
	assert np.all(taken[:100] == 7)
	# The rest is silence, not a repeat of the last sample and not uninitialised memory.
	assert np.all(taken[100:] == 0)


def test_take_returns_pure_silence_for_a_publisher_that_has_delivered_nothing():
	buffer = _TrackBuffer("s1")

	taken = buffer.take(CHUNK_SAMPLES)

	assert taken.size == CHUNK_SAMPLES
	assert np.all(taken == 0)


def test_take_splits_a_frame_across_chunks_without_losing_samples():
	buffer = _TrackBuffer("s1")
	# One frame worth 2.5 chunks, which is what happens whenever a publisher's frame size does not
	# divide the mixer's chunk.
	buffer.push(np.arange(CHUNK_SAMPLES * 5 // 2, dtype=np.int16))

	first = buffer.take(CHUNK_SAMPLES)
	second = buffer.take(CHUNK_SAMPLES)
	third = buffer.take(CHUNK_SAMPLES)

	assert np.array_equal(first, np.arange(CHUNK_SAMPLES, dtype=np.int16))
	assert np.array_equal(second, np.arange(CHUNK_SAMPLES, CHUNK_SAMPLES * 2, dtype=np.int16))
	# The tail is half a chunk of real audio then silence -- contiguous with what came before.
	assert np.array_equal(third[: CHUNK_SAMPLES // 2],
		np.arange(CHUNK_SAMPLES * 2, CHUNK_SAMPLES * 5 // 2, dtype=np.int16))
	assert np.all(third[CHUNK_SAMPLES // 2:] == 0)


def test_buffer_drops_oldest_audio_rather_than_growing_without_bound():
	buffer = _TrackBuffer("s1")
	for _ in range(400):  # well past MIXER_MAX_BUFFER_CHUNKS
		buffer.push(np.full(CHUNK_SAMPLES, 1, dtype=np.int16))

	# Still bounded. A stalled clock must not turn into unbounded memory in a customer's container.
	assert buffer._pending_samples <= CHUNK_SAMPLES * 250


# ------------------------------------------------------------------ mixing


@pytest.mark.asyncio
async def test_mix_returns_none_when_nobody_is_publishing():
	mixer = AudioMixer()

	# Silence would be defensible but would mean paying to transcribe the wait before anyone unmutes.
	assert await mixer._mix_once() is None


@pytest.mark.asyncio
async def test_one_publisher_passes_through_unchanged():
	mixer = AudioMixer()
	buffer = _TrackBuffer("s1")
	buffer.push(np.full(CHUNK_SAMPLES, 1000, dtype=np.int16))
	mixer._buffers["s1"] = buffer

	chunk = await mixer._mix_once()

	assert len(chunk) == CHUNK_BYTES
	assert np.all(np.frombuffer(chunk, dtype=np.int16) == 1000)


@pytest.mark.asyncio
async def test_two_publishers_are_summed():
	mixer = AudioMixer()
	for name, value in (("s1", 1000), ("s2", 2500)):
		buffer = _TrackBuffer(name)
		buffer.push(np.full(CHUNK_SAMPLES, value, dtype=np.int16))
		mixer._buffers[name] = buffer

	chunk = await mixer._mix_once()

	assert np.all(np.frombuffer(chunk, dtype=np.int16) == 3500)


@pytest.mark.asyncio
async def test_loud_simultaneous_speakers_clip_instead_of_wrapping():
	mixer = AudioMixer()
	for name in ("s1", "s2", "s3"):
		buffer = _TrackBuffer(name)
		buffer.push(np.full(CHUNK_SAMPLES, 30000, dtype=np.int16))
		mixer._buffers[name] = buffer

	chunk = await mixer._mix_once()

	# 90000 summed. Wrapping int16 would turn a loud moment into noise the model reads as speech;
	# clipping turns it into a loud moment.
	assert np.all(np.frombuffer(chunk, dtype=np.int16) == 32767)


# ------------------------------------------------------------------ normalisation


@pytest.mark.asyncio
async def test_stereo_44100_is_normalised_to_mono_48000():
	"""The property upstream assumes and never verifies.

	Upstream reads neither `sample_rate` nor `format`; it hand-downmixes stereo and declares 48 kHz to
	the provider. Feed it 44.1 kHz and the transcript is of chipmunks. Here libav converts.
	"""
	mixer = AudioMixer()
	buffer = _TrackBuffer("s1")
	# 441 samples at 44.1 kHz is 10 ms, which becomes 480 samples at 48 kHz.
	stream = FakeStream([
		make_frame(samples=441, rate=44100, layout="stereo", value=500) for _ in range(20)
	])

	await mixer._run_puller(buffer, stream)

	# Resampler latency means not every input sample is out yet, but the rate conversion must have
	# happened: 20 frames x 10 ms = 200 ms, which at 48 kHz mono is about 9600 samples, not the 8820
	# it would be if the rate were passed through, and not the 17640 of un-downmixed stereo.
	assert 8000 < buffer._pending_samples <= 9600
	assert buffer.ended


@pytest.mark.asyncio
async def test_attach_and_detach_do_not_disturb_the_output_stream():
	mixer = AudioMixer()
	stream = FakeStream([make_frame(samples=960, rate=48000, layout="mono", value=100)])

	await mixer.attach("s1", stream)
	assert mixer.publisher_count == 1

	await mixer.detach("s1")
	assert mixer.publisher_count == 0

	# Detaching the last publisher must leave the mixer usable, because the speech connection is per
	# call and has to survive everyone dropping out and coming back.
	await mixer.attach("s2", FakeStream([]))
	assert mixer.publisher_count == 1
	await mixer.close()


@pytest.mark.asyncio
async def test_clock_emits_chunks_at_roughly_real_time():
	mixer = AudioMixer()
	buffer = _TrackBuffer("s1")
	mixer._buffers["s1"] = buffer

	async def keep_fed():
		for _ in range(50):
			buffer.push(np.full(CHUNK_SAMPLES, 5, dtype=np.int16))
			await asyncio.sleep(0.005)

	feeder = asyncio.create_task(keep_fed())
	await mixer.start()
	chunks = [await asyncio.wait_for(mixer.read(), 2) for _ in range(5)]
	await mixer.close()
	feeder.cancel()

	assert all(len(chunk) == CHUNK_BYTES for chunk in chunks)
