#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The segmenter, which owns the two things that are hard to notice going wrong.

A lost segment leaves a hole in a transcript nobody can reconstruct afterwards -- the audio is gone,
because Korsi never stored it. And overlapping or gapped `started_ms`/`ended_ms` mis-attributes what was
said to the wrong minutes of the meeting, which is exactly the thing the live reading is for.
"""

import asyncio

import pytest
from korsi_types import KorsiApiError, LiveSegmentAccepted, LiveSessionClosed
from segmenter import Segmenter


class FakeKorsi:
	"""Records what was posted and can be told to fail."""

	def __init__(self, *, interval=300, fail_times=0, raise_closed=False):
		self.posted: list[dict] = []
		self.interval = interval
		self.fail_times = fail_times
		self.raise_closed = raise_closed

	async def append_segment(self, *, live_session_id, sequence, started_ms, ended_ms, text):
		if self.raise_closed:
			raise LiveSessionClosed("closed", status=409)
		if self.fail_times > 0:
			self.fail_times -= 1
			raise KorsiApiError("boom", status=500)
		self.posted.append({
			"live_session_id": live_session_id,
			"sequence": sequence,
			"started_ms": started_ms,
			"ended_ms": ended_ms,
			"text": text,
		})
		return LiveSegmentAccepted(
			accepted_sequence=sequence,
			segment_count=len(self.posted),
			snapshot_queued=bool(text.strip()),
			segment_interval_seconds=self.interval,
		)


def build(korsi, *, texts, first=0, interval=0, gone=None):
	"""A segmenter whose speech stream hands out `texts`, one per drain."""
	pending = list(texts)
	finalized = {"count": 0}

	async def finalize():
		finalized["count"] += 1

	async def drain():
		return pending.pop(0) if pending else ""

	async def on_gone():
		if gone is not None:
			gone.set()

	segmenter = Segmenter(
		room_token="room1",
		live_session_id="sess1",
		client=korsi,
		first_interval_seconds=first,
		interval_seconds=interval,
		finalize=finalize,
		drain_text=drain,
		on_session_gone=on_gone,
	)
	return segmenter, finalized


@pytest.mark.asyncio
async def test_posts_text_and_advances_the_sequence():
	korsi = FakeKorsi()
	segmenter, finalized = build(korsi, texts=["hello there", "and then this"])

	await segmenter._post_once()
	await segmenter._post_once()

	assert [p["sequence"] for p in korsi.posted] == [1, 2]
	assert [p["text"] for p in korsi.posted] == ["hello there", "and then this"]
	assert segmenter.segments_posted == 2
	# `finalize` is the caller's job, not `_post_once`'s.
	assert finalized["count"] == 0


@pytest.mark.asyncio
async def test_segments_tile_the_call_with_no_gap_and_no_overlap():
	korsi = FakeKorsi()
	segmenter, _ = build(korsi, texts=["a", "b", "c"])

	for _ in range(3):
		await asyncio.sleep(0.01)
		await segmenter._post_once()

	# Each segment starts exactly where the previous one ended. A recomputed start would leave the
	# duration of the post itself unaccounted for on every interval.
	assert korsi.posted[0]["started_ms"] == 0
	for earlier, later in zip(korsi.posted, korsi.posted[1:], strict=False):
		assert later["started_ms"] == earlier["ended_ms"]
		assert later["ended_ms"] > later["started_ms"]


@pytest.mark.asyncio
async def test_empty_segments_are_posted_as_a_heartbeat():
	korsi = FakeKorsi()
	segmenter, _ = build(korsi, texts=[""])

	await segmenter._post_once()

	# Silence has to be reported: korsi-api closes a session whose newest segment is too old, so a
	# quiet twenty minutes would otherwise be indistinguishable from a dead bridge.
	assert len(korsi.posted) == 1
	assert korsi.posted[0]["text"] == ""


@pytest.mark.asyncio
async def test_a_failed_post_keeps_the_text_and_the_sequence():
	korsi = FakeKorsi(fail_times=1)
	segmenter, _ = build(korsi, texts=["first words", "second words"])

	with pytest.raises(KorsiApiError):
		await segmenter._post_once()
	assert korsi.posted == []
	assert segmenter.segments_posted == 0

	await segmenter._post_once()

	# The retry carries both intervals' text, in order, as one longer segment rather than losing the
	# first -- and still claims the interval it was read from.
	assert len(korsi.posted) == 1
	assert korsi.posted[0]["sequence"] == 1
	assert korsi.posted[0]["started_ms"] == 0
	assert korsi.posted[0]["text"] == "first words second words"


@pytest.mark.asyncio
async def test_a_cadence_change_from_korsi_is_adopted():
	korsi = FakeKorsi(interval=120)
	segmenter, _ = build(korsi, texts=["x"], interval=300)

	await segmenter._post_once()

	# Policy comes from korsi-api on every accept, so a cadence change reaches a call in progress.
	assert segmenter._interval == 120


@pytest.mark.asyncio
async def test_a_closed_session_stops_the_loop_and_reports_it():
	korsi = FakeKorsi(raise_closed=True)
	gone = asyncio.Event()
	segmenter, _ = build(korsi, texts=["x"], first=0, interval=0, gone=gone)

	segmenter.start()
	await asyncio.wait_for(gone.wait(), 2)

	# Told the owner rather than retrying: a live reading has no value once its call is over.
	assert korsi.posted == []


@pytest.mark.asyncio
async def test_stop_flushes_the_tail_of_the_meeting():
	korsi = FakeKorsi()
	segmenter, finalized = build(korsi, texts=["the decision we just made"], first=3600, interval=3600)

	segmenter.start()
	await asyncio.sleep(0.01)
	await segmenter.stop(flush=True)

	# The meeting ended a long way inside its interval. Without the flush the part with the decision in
	# it would be discarded.
	assert len(korsi.posted) == 1
	assert korsi.posted[0]["text"] == "the decision we just made"
	assert finalized["count"] == 1


@pytest.mark.asyncio
async def test_zero_width_intervals_still_produce_a_valid_range():
	korsi = FakeKorsi()
	segmenter, _ = build(korsi, texts=["a", "b"])

	await segmenter._post_once()
	await segmenter._post_once()  # immediately after, so no wall time has passed

	# korsi-api rejects ended_ms <= started_ms, and a flush right after a post is exactly that case.
	assert korsi.posted[1]["ended_ms"] > korsi.posted[1]["started_ms"]
