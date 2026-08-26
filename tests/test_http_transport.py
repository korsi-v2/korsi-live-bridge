#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Is HTTP/3 actually off, on both sides?

Regression tests for a production outage. A reverse proxy advertised `h3` through `Alt-Svc` while
QUIC was unreachable, niquests refused to downgrade, and every request the bridge made failed --
the watchlist poll to korsi-api and the admin page's registration call to Nextcloud alike, because
the same proxy fronted both. Nothing about the bridge looked wrong: it minted its token and reported
that it was watching for calls.

So these check the two things that must stay true, and check them against the objects the app really
builds rather than against the flag being passed somewhere.
"""

import niquests
import pytest
from http_transport import AsyncSession, Session, install


def test_the_sync_session_disables_http3():
	assert Session()._disable_http3 is True


def test_the_async_session_disables_http3():
	assert AsyncSession()._disable_http3 is True


def test_an_explicit_choice_still_wins():
	"""`setdefault`, not an assignment: a caller that needs HTTP/3 is not overruled by this module."""
	assert Session(disable_http3=False)._disable_http3 is False


def test_the_subclasses_are_still_niquests_sessions():
	"""Everything downstream is typed against niquests, so this must remain a subtype."""
	assert isinstance(Session(), niquests.Session)
	assert isinstance(AsyncSession(), niquests.AsyncSession)


def test_installing_points_nc_py_api_at_them():
	"""`nc_py_api._session` builds every Nextcloud session from these two names."""
	from nc_py_api import _session as nc_session

	install()

	assert nc_session.Session is Session
	assert nc_session.AsyncSession is AsyncSession


def test_installing_twice_is_harmless():
	from nc_py_api import _session as nc_session

	install()
	install()

	assert nc_session.Session is Session


def test_the_korsi_client_session_disables_http3(monkeypatch):
	"""The other half: the client the bridge talks to korsi-api with."""
	import asyncio
	import json

	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric import rsa
	from korsi_client import KorsiClient

	pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.TraditionalOpenSSL,
		encryption_algorithm=serialization.NoEncryption(),
	).decode()

	for name, value in {
		"KORSI_API_URL": "https://api.example.test",
		"KORSI_TOKEN_URL": "https://auth.example.test/oauth/v2/token",
		"KORSI_TOKEN_SCOPE": "openid urn:zitadel:iam:org:projects:roles",
		"KORSI_SERVICE_KEY": json.dumps({"keyId": "k", "userId": "u", "key": pem}),
	}.items():
		monkeypatch.setenv(name, value)

	async def check():
		client = KorsiClient()
		try:
			session = await client._http()
			assert session._disable_http3 is True
		finally:
			await client.aclose()

	asyncio.run(check())


@pytest.mark.parametrize("name", ["Session", "AsyncSession"])
def test_niquests_still_takes_the_flag(name):
	"""The guard on the whole approach: `disable_http3` is niquests' documented parameter.

	If a future niquests drops or renames it, this fails here rather than by quietly negotiating
	HTTP/3 again in a customer's infrastructure.
	"""
	import inspect

	assert "disable_http3" in inspect.signature(getattr(niquests, name).__init__).parameters
