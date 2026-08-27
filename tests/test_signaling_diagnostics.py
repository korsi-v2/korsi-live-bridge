#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Can a signaling failure be read out of the log without turning DEBUG on?

Regression tests for a production diagnosis that took a day. The HPB rejected seven of the bridge's
messages with `processing_failed`, reporting each by the id it had been sent with. The bridge's record of
what it sent with which id was a DEBUG line, and the bridge runs at INFO -- so the log held seven errors
about messages nobody could identify, next to a peer connection that failed for no stated reason.

Two things close that gap and both are checked here: an outgoing message is summarised at send time so
an error can name it, and the candidates offered are logged by *type*, because "host only" and "no relay"
are the readings that matter and neither is apparent from a list of SDP lines.
"""

from spreed_client import _candidate_type, _summarize_sent


def message(data_type: str, *, sdp: str = "v=0\r\n" + "a=fake\r\n" * 400) -> dict:
	return {
		"type": "message",
		"id": "6",
		"message": {
			"recipient": {"type": "session", "sessionid": "peer-session"},
			"data": {"to": "peer-session", "type": data_type, "payload": {"sdp": sdp}},
		},
	}


def test_a_candidate_line_is_read_by_its_type():
	assert _candidate_type("candidate:1 1 udp 2130706431 10.0.14.13 43449 typ host") == "host"
	assert _candidate_type(
		"candidate:2 1 udp 1694498815 10.0.14.1 45042 typ srflx raddr 10.0.14.13 rport 43449"
	) == "srflx"
	assert _candidate_type(
		"candidate:3 1 udp 16777215 1.2.3.4 50000 typ relay raddr 10.0.14.13 rport 43449"
	) == "relay"


def test_an_unreadable_candidate_line_does_not_raise():
	"""Runs while building a log record on the path that answers an offer, so it may not throw."""
	assert _candidate_type("") == "unknown"
	assert _candidate_type("candidate:4 1 udp 100 1.2.3.4 5 typ") == "unknown"
	assert _candidate_type("candidate:5 1 udp 100 1.2.3.4 5 no-type-field-here") == "unknown"


def test_only_the_types_sdp_defines_are_reported():
	"""The line comes off the network and the answer becomes a log label. A stray "typ" followed by
	anything at all must not turn into a candidate type nobody can look up.
	"""
	assert _candidate_type("nonsense with no typ field") == "unknown"
	assert _candidate_type("candidate:6 1 udp 100 1.2.3.4 5 typ ../../etc/passwd") == "unknown"
	assert _candidate_type("candidate:7 1 udp 100 1.2.3.4 5 typ prflx") == "prflx"


def test_a_summary_identifies_the_message_an_error_refers_to():
	assert _summarize_sent(message("answer")) == {
		"type": "message",
		"data_type": "answer",
		"recipient": "peer-session",
	}
	assert _summarize_sent(message("candidate"))["data_type"] == "candidate"


def test_a_summary_carries_no_payload():
	"""It goes into a log file an administrator reads over HTTP. Session descriptions are large, and
	repeating them per error turns one failed call into megabytes.
	"""
	assert "a=fake" not in repr(_summarize_sent(message("answer")))


def test_messages_with_no_recipient_summarize_to_their_type():
	"""`hello` and `bye` are sent to the server, not to a peer. They still get ids, so they can still
	be what an error is about.
	"""
	assert _summarize_sent({"type": "hello", "hello": {"version": "2.0"}}) == {"type": "hello"}
	assert _summarize_sent({"type": "bye", "bye": {}}) == {"type": "bye"}


def test_a_malformed_message_still_summarizes():
	"""Defensive because the alternative is an exception inside the send path, which would turn a
	logging convenience into a dropped signaling message.
	"""
	assert _summarize_sent({"type": "message", "message": "not a dict"}) == {"type": "message"}
	assert _summarize_sent({}) == {"type": None}
