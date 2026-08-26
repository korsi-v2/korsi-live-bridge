#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Does this bridge speak korsi-api's actual contract?

The failure this guards against is the expensive one: the bridge is deployed inside a customer's
Nextcloud and cannot be corrected on Korsi's schedule, so a field name that drifted is a customer whose
meetings stop being recorded until somebody visits their infrastructure.

So the request bodies are validated against korsi-api's own `openapi.json` -- the generated artifact, not
a copy of the shapes written down here -- and the responses are decoded from the schemas' own examples.
Skipped when korsi-api is not checked out alongside, because this repo is deployed on its own.
"""

import json
from pathlib import Path

import pytest
from korsi_client import KorsiClient
from korsi_types import LiveCloseReason

jsonschema = pytest.importorskip("jsonschema")


def _generate_key() -> str:
	"""An RSA key for the assertion tests.

	Generated per run rather than committed. A fixture private key in a repository is a
	credential-shaped thing that eventually gets copied somewhere it matters, and a secret
	scanner flagging it is the good outcome.
	"""
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric import rsa

	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	return key.private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.TraditionalOpenSSL,
		encryption_algorithm=serialization.NoEncryption(),
	).decode()


_TEST_KEY = _generate_key()

OPENAPI = Path(__file__).resolve().parents[2] / "korsi-api" / "openapi.json"
pytestmark = pytest.mark.skipif(not OPENAPI.is_file(), reason="korsi-api/openapi.json not available")


@pytest.fixture(scope="module")
def spec() -> dict:
	return json.loads(OPENAPI.read_text(encoding="utf-8"))


def validate(spec: dict, schema_name: str, payload: dict) -> None:
	"""Check one body against a named component schema, with `$ref`s resolved against the document."""
	schema = dict(spec["components"]["schemas"][schema_name])
	schema["components"] = spec["components"]
	jsonschema.validate(payload, schema)


class Recorder:
	"""Captures what `KorsiClient` would send, instead of sending it."""

	def __init__(self, response: dict):
		self.calls: list[tuple[str, str, dict | None]] = []
		self._response = response

	async def __call__(self, method, path, *, json_body=None, retry_auth=True):
		self.calls.append((method, path, json_body))
		return self._response


@pytest.fixture
def client(monkeypatch) -> KorsiClient:
	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_TOKEN_SCOPE": (
			"openid urn:zitadel:iam:org:project:id:x:aud "
			"urn:zitadel:iam:org:projects:roles urn:zitadel:iam:org:id:y"
		),
		"KORSI_SERVICE_KEY": json.dumps(
			{"type": "serviceaccount", "keyId": "k1", "userId": "u1", "key": _TEST_KEY}
		),
	}.items():
		monkeypatch.setenv(name, value)
	return KorsiClient()


# ------------------------------------------------------------------ requests


async def test_open_session_body_matches_the_contract(spec, client, monkeypatch):
	from datetime import UTC, datetime

	recorder = Recorder({
		"enabled": False,
		"reason": "room.not_registered",
		"retry_after_seconds": 60,
	})
	monkeypatch.setattr(client, "_request", recorder)

	await client.open_session(
		room_remote_id="abc123",
		started_at=datetime(2026, 8, 19, 10, 30, tzinfo=UTC),
		bridge_version="1.0.0",
	)

	method, path, body = recorder.calls[0]
	assert (method, path) == ("POST", "/api/v1/meetings/live/sessions")
	validate(spec, "OpenLiveSession", body)
	# Sent as UTC regardless of the container's timezone: korsi-api matches this against meetings
	# scheduled in the tenant's own zone, and a naive local timestamp would pick the wrong meeting.
	assert body["started_at"].endswith("+00:00")


async def test_append_segment_body_matches_the_contract(spec, client, monkeypatch):
	recorder = Recorder({
		"accepted_sequence": 3,
		"segment_count": 3,
		"snapshot_queued": True,
		"segment_interval_seconds": 300,
	})
	monkeypatch.setattr(client, "_request", recorder)

	accepted = await client.append_segment(
		live_session_id="1cf1c0de-0000-4000-8000-000000000001",
		sequence=3,
		started_ms=600_000,
		ended_ms=900_000,
		text="what was said",
	)

	method, path, body = recorder.calls[0]
	assert method == "POST"
	assert path == "/api/v1/meetings/live/sessions/1cf1c0de-0000-4000-8000-000000000001/segments"
	validate(spec, "AppendLiveSegment", body)
	assert accepted.accepted_sequence == 3
	assert accepted.segment_interval_seconds == 300


async def test_close_session_body_matches_the_contract(spec, client, monkeypatch):
	from datetime import UTC, datetime

	recorder = Recorder({})
	monkeypatch.setattr(client, "_request", recorder)

	await client.close_session(
		live_session_id="1cf1c0de-0000-4000-8000-000000000001",
		ended_at=datetime(2026, 8, 19, 11, 0, tzinfo=UTC),
		reason=LiveCloseReason.CALL_ENDED,
	)

	method, path, body = recorder.calls[0]
	assert method == "POST"
	assert path.endswith("/close")
	validate(spec, "CloseLiveSession", body)


async def test_renew_credential_uses_the_stt_path(client, monkeypatch):
	recorder = Recorder({
		"api_key": "temp",
		"websocket_url": "wss://stt-rt.soniox.com/transcribe-websocket",
		"model": "stt-rt-v5",
		"max_session_duration_seconds": 14400,
		"expires_at": "2026-08-19T10:45:00+00:00",
	})
	monkeypatch.setattr(client, "_request", recorder)

	await client.renew_credential(live_session_id="sess")

	# `/stt`, which is what the router actually mounts. The design doc called it `renew-stt`.
	assert recorder.calls[0][1] == "/api/v1/meetings/live/sessions/sess/stt"


# ------------------------------------------------------------------ responses


async def test_every_close_reason_the_server_defines_is_understood(spec):
	"""A reason the bridge cannot decode is a close it cannot report."""
	server_reasons = set(spec["components"]["schemas"]["LiveCloseReason"]["enum"])
	assert server_reasons == {reason.value for reason in LiveCloseReason}


async def test_decline_reasons_the_server_defines_are_understood(spec):
	from korsi_types import LiveDeclineReason

	server_reasons = set(spec["components"]["schemas"]["LiveDeclineReason"]["enum"])
	assert server_reasons == {reason.value for reason in LiveDeclineReason}


async def test_an_accepted_session_decodes_with_its_speech_credential(client, monkeypatch):
	from datetime import UTC, datetime

	recorder = Recorder({
		"enabled": True,
		"retry_after_seconds": 60,
		"session": {
			"live_session_id": "1cf1c0de-0000-4000-8000-000000000001",
			"meeting_id": "1cf1c0de-0000-4000-8000-000000000002",
			"operation_case_id": "1cf1c0de-0000-4000-8000-000000000003",
			"meeting_created": True,
			"first_segment_interval_seconds": 120,
			"segment_interval_seconds": 300,
			"stt": {
				"api_key": "temp-key",
				"websocket_url": "wss://stt-rt.soniox.com/transcribe-websocket",
				"model": "stt-rt-v5",
				"max_session_duration_seconds": 14400,
				"expires_at": "2026-08-19T10:45:00+00:00",
				"audio_format": "pcm_s16le",
				"sample_rate": 48000,
				"num_channels": 1,
				"language_hints": ["fa"],
			},
		},
	})
	monkeypatch.setattr(client, "_request", recorder)

	decision = await client.open_session(room_remote_id="abc", started_at=datetime.now(UTC))

	assert decision.enabled
	assert decision.session is not None
	# The audio format travels with the credential rather than being a constant in this container --
	# and `pcm_s16le` is the name Soniox documents for raw 16-bit PCM.
	assert decision.session.stt.audio_format == "pcm_s16le"
	assert decision.session.stt.sample_rate == 48000
	assert decision.session.stt.num_channels == 1
	assert decision.session.stt.language_hints == ("fa",)


async def test_unknown_response_fields_do_not_break_the_bridge(client, monkeypatch):
	"""A Korsi release that adds a field must not stop a bridge nobody can redeploy today."""
	recorder = Recorder({
		"rooms": [{
			"room_remote_id": "abc",
			"operation_case_id": "1cf1c0de-0000-4000-8000-000000000003",
			"room_title": "Weekly review",
			"something_added_next_year": {"nested": True},
		}],
		"poll_interval_seconds": 45,
		"enabled": True,
		"a_new_top_level_field": 1,
	})
	monkeypatch.setattr(client, "_request", recorder)

	watchlist = await client.watchlist()

	assert watchlist.poll_interval_seconds == 45
	assert watchlist.rooms[0].room_remote_id == "abc"


# ------------------------------------------------------------------ authentication


async def test_the_assertion_is_signed_with_the_service_key(client):
	"""What the bridge sends to the token endpoint, and why each claim is there.

	Not client credentials: ZITADEL does not assert project roles into a `client_credentials`
	token, so korsi-api would see a valid token with no permissions.
	"""
	import jwt as pyjwt
	from cryptography.hazmat.primitives import serialization

	assertion = client._assertion()

	public = (
		serialization.load_pem_private_key(_TEST_KEY.encode(), password=None)
		.public_key()
		.public_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PublicFormat.SubjectPublicKeyInfo,
		)
	)
	claims = pyjwt.decode(
		assertion, public, algorithms=["RS256"], audience="https://auth.example.test"
	)

	assert claims["iss"] == "u1"
	assert claims["sub"] == "u1"
	# Addressed to the identity provider, which is what exchanges it. korsi-api's audience is
	# carried by the token that comes back, not by this.
	assert claims["aud"] == "https://auth.example.test"
	# Short-lived: it is exchanged immediately, so a longer life would only widen the replay
	# window.
	assert 0 < claims["exp"] - claims["iat"] <= 60
	assert pyjwt.get_unverified_header(assertion)["kid"] == "k1"


async def test_a_malformed_service_key_fails_at_construction(monkeypatch):
	"""Reported when the app is enabled, not in the middle of the first meeting."""
	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_TOKEN_SCOPE": "openid",
		"KORSI_SERVICE_KEY": json.dumps({"type": "serviceaccount"}),
	}.items():
		monkeypatch.setenv(name, value)

	with pytest.raises(ValueError, match="KORSI_SERVICE_KEY"):
		KorsiClient()


async def test_missing_roles_scope_is_named_before_it_causes_a_refusal(monkeypatch):
	"""The one misconfiguration whose symptom points nowhere near its cause."""
	from utils import check_korsi_env_vars

	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_SERVICE_KEY": json.dumps({"keyId": "k", "userId": "u", "key": "x"}),
		"KORSI_TOKEN_SCOPE": "openid urn:zitadel:iam:org:project:id:x:aud",
	}.items():
		monkeypatch.setenv(name, value)

	problems = check_korsi_env_vars()

	assert any("projects:roles" in problem for problem in problems)


async def test_a_truncated_key_is_reported_before_the_first_call(monkeypatch):
	"""The realistic pasting accident: a multi-line PEM into a one-line form field."""
	from utils import check_korsi_env_vars

	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_TOKEN_SCOPE": "openid urn:zitadel:iam:org:projects:roles",
		"KORSI_SERVICE_KEY": json.dumps(
			{"keyId": "k", "userId": "u", "key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow"}
		),
	}.items():
		monkeypatch.setenv(name, value)

	assert any("truncated" in problem for problem in check_korsi_env_vars())


async def test_a_whole_key_passes_the_check(monkeypatch):
	from utils import check_korsi_env_vars

	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_TOKEN_SCOPE": "openid urn:zitadel:iam:org:projects:roles",
		"KORSI_SERVICE_KEY": json.dumps({"keyId": "k", "userId": "u", "key": _TEST_KEY}),
	}.items():
		monkeypatch.setenv(name, value)

	assert check_korsi_env_vars() == []
