#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Everything this bridge says to korsi-api.

Five calls out and no callbacks in, which is the whole shape of the integration: korsi-api never
reaches into a customer's Nextcloud. Anything the bridge needs to know it asks for.

**Why an OAuth2 machine user and not a shared secret.** korsi-api has three non-JWT planes already
(`/internal/v1`, `/shared/v1`, `/management/v1`) and each is a separate thing to reason about when
auditing who can reach what. A fourth one for this bridge would be a new security surface for a
client that ZITADEL can already issue a perfectly ordinary token to, whose service-account role grants
exactly `meeting.read` / `meeting.ingest` / `meeting.analyze` and nothing else. See ADR-0021 D4.

**JWT-profile, not client credentials, and this is not a preference.** ZITADEL does not assert project
roles into a token obtained through `client_credentials`: the token comes back valid, correctly
addressed to korsi-api, and carrying no `urn:zitadel:iam:org:project:<id>:roles` claim at all. korsi-api
reads roles from the token alone -- deliberately, so that revoking a role cannot be outvoted by a stale
database row -- so such a token resolves to a principal with no permissions and every call is refused as
forbidden, with nothing in the failure pointing at the cause. The JWT-profile grant does assert them.
Verified against ZITADEL v4.16 by trying both.

The practical benefit is that the private key never leaves this container: there is no shared secret in
flight on each token request, only a short-lived assertion signed with a key Korsi cannot read back.

**The scope string is configuration, not code.** Three reserved URNs have to be present -- the API
audience, the roles assertion and the acting organization -- and each omission fails somewhere far from
its cause. Composing them here would put Korsi's identity-provider topology inside every customer's
infrastructure, to be redeployed the day it changes. Korsi's `provision-bridge` emits the finished
string; the bridge pastes it into a token request and holds no opinion about it. Section 2 of the design
doc: the bridge holds no policy.
"""

import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import jwt as pyjwt
from http_transport import AsyncSession
from korsi_types import (
	KorsiApiError,
	LiveCloseReason,
	LiveSegmentAccepted,
	LiveSessionClosed,
	LiveSessionDecision,
	LiveSttCredential,
	LiveWatchlist,
)
from service_key import load_service_key

LOGGER = logging.getLogger("lt")

#: Refresh a token this long before it expires. Long enough that no in-flight request dies of an
#: expiry it was issued under, short enough that the bridge is not re-minting constantly.
TOKEN_REFRESH_MARGIN_SECONDS = 60

#: How long any single call to korsi-api may take. Generous because a segment POST enqueues an
#: analysis job, and short enough that a hung API does not wedge the segment pump for a whole call.
REQUEST_TIMEOUT_SECONDS = 30

#: korsi-api error codes the bridge treats specially rather than as "some failure".
_SESSION_GONE_CODES = frozenset({"live.session_closed", "live.session_not_found"})

#: How ZITADEL names an asserted-roles claim, both in its all-projects and its per-project form. The
#: project id in the middle is not something the bridge knows or needs to know: any claim shaped like
#: this carries role keys, and their presence is the whole question.
_ROLES_CLAIM = re.compile(r"^urn:zitadel:iam:org:project:(?:[^:]+:)?roles$")


def _status_of(response: Any) -> int:
	"""The response's status, treating "no status" as a failure rather than as a pass.

	`niquests` types `status_code` as optional because a response object can exist for an exchange that
	never completed. Comparing that against 400 with `>=` would let it through as success, so an
	incomplete response would be handed to a JSON decoder as if the server had answered.
	"""
	status = getattr(response, "status_code", None)
	return int(status) if status is not None else 599


class KorsiClient:
	"""An authenticated conversation with one Korsi tenant.

	One instance per process. The token is shared across every call and every room, because it
	identifies the *bridge*, not the call.
	"""

	def __init__(self) -> None:
		self._base_url = os.environ["KORSI_API_URL"].rstrip("/")
		self._token_url = os.environ["KORSI_TOKEN_URL"]
		self._scope = os.environ["KORSI_TOKEN_SCOPE"]

		# The service key ZITADEL generated, as either the JSON document `provision-bridge` printed or
		# base64 of it. Parsed *and test-signed* at construction, so a value the deployment layer
		# mangled fails when the app is enabled rather than in the middle of the first call somebody
		# cared about. See `service_key` for why that is not paranoia.
		key = load_service_key(os.environ["KORSI_SERVICE_KEY"])
		self._key_id: str = key.key_id
		self._user_id: str = key.user_id
		self._private_key: str = key.private_key
		if key.repairs:
			# Warned about rather than passed over silently: a value that needed repair will arrive
			# damaged again on the next deploy, and the operator can stop that by switching to base64.
			LOGGER.warning(
				"KORSI_SERVICE_KEY arrived damaged and was repaired to make it usable."
				" Set it as base64 to stop the deployment layer from mangling it.",
				extra={"repairs": list(key.repairs), "encoding": key.encoding, "tag": "korsi"},
			)

		#: The assertion's audience is the identity provider, not korsi-api: it is addressed to
		#: whoever will exchange it, and the token that comes back is what carries korsi-api's
		#: audience. Derived from the token endpoint so there is one fewer setting to get wrong.
		self._issuer = self._token_url.split("/oauth/", 1)[0]

		self._token: str | None = None
		self._token_expires_at: float = 0.0
		self._token_lock = asyncio.Lock()
		self._session: AsyncSession | None = None

	async def aclose(self) -> None:
		if self._session is not None:
			with_suppressed = self._session
			self._session = None
			try:
				await with_suppressed.close()
			except Exception as e:  # noqa: BLE001 - shutdown path, nothing to escalate to
				LOGGER.debug("Error closing the Korsi HTTP session", exc_info=e, extra={"tag": "korsi"})

	async def _http(self) -> AsyncSession:
		"""The session every call to korsi-api goes through.

		`http_transport.AsyncSession` rather than niquests' own, so HTTP/3 is never negotiated. See
		that module: a proxy advertising `h3` it cannot serve makes every request fail outright, and
		this bridge does not get to choose the proxy in front of Korsi.
		"""
		if self._session is None:
			self._session = AsyncSession()
		return self._session

	# ------------------------------------------------------------------ auth

	async def _bearer(self) -> str:
		"""A valid access token, minting one if the cached token is gone or nearly expired.

		Under a lock because the segment pump, the watchlist poll and a credential renewal can all
		notice the expiry in the same tick, and three simultaneous `client_credentials` grants for
		one machine user is how a bridge gets itself rate-limited by the identity provider.
		"""
		async with self._token_lock:
			now = asyncio.get_running_loop().time()
			if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
				return self._token

			http = await self._http()
			try:
				response = await http.post(
					self._token_url,
					data={
						"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
						"assertion": self._assertion(),
						"scope": self._scope,
					},
					headers={"Content-Type": "application/x-www-form-urlencoded"},
					timeout=REQUEST_TIMEOUT_SECONDS,
				)
			except Exception as e:
				raise KorsiApiError(f"could not reach the token endpoint: {e}") from e

			status = _status_of(response)
			if status >= 400:
				# Deliberately not logging the body: a failed client_credentials response can echo
				# the request, and the request carries the client secret.
				raise KorsiApiError(f"token request rejected with {status}", status=status)

			payload = response.json()
			token = payload.get("access_token")
			if not token:
				raise KorsiApiError("token response carried no access_token")

			self._token = str(token)
			# Default to a short life when the provider does not say: a token treated as longer-lived
			# than it is produces 401s in the middle of a call, which is the worst time to find out.
			self._token_expires_at = now + float(payload.get("expires_in") or 300)
			LOGGER.info("Minted a Korsi access token", extra={
				"expires_in": payload.get("expires_in"),
				"tag": "korsi",
			})
			return self._token

	def _assertion(self) -> str:
		"""A short-lived JWT proving this bridge holds the service key.

		One minute of validity, because it is exchanged immediately and never stored. A long
		expiry would only widen the window in which a captured assertion is replayable.
		"""
		now = int(time.time())
		return pyjwt.encode(
			{
				"iss": self._user_id,
				"sub": self._user_id,
				"aud": self._issuer,
				"iat": now,
				"exp": now + 60,
			},
			self._private_key,
			algorithm="RS256",
			headers={"kid": self._key_id},
		)

	def forget_token(self) -> None:
		"""Drop the cached token so the next call mints a fresh one.

		Called when korsi-api answers 401 with a token this bridge believed was valid, which happens
		legitimately: a key rotation, or a clock difference that made the local expiry optimistic.
		"""
		self._token = None
		self._token_expires_at = 0.0

	async def fresh_token_claims(self) -> dict[str, Any]:
		"""Mint a token and report what it asserts, without returning the token itself.

		This exists because of the failure that cost the most to diagnose and is invisible from every
		other vantage point: a token can be issued, correctly signed, correctly addressed to korsi-api,
		and carry no roles at all -- at which point korsi-api refuses every call as forbidden and
		nothing in the refusal mentions roles or scopes. Decoding the token this bridge would actually
		use settles that in one line, so an administrator does not have to distinguish "the credential
		is wrong" from "the scope is wrong" by trying things.

		Signature verification is skipped on purpose. The issuer produced this token seconds ago, the
		bridge is not a resource server for it, and fetching JWKS to check it would add a second thing
		that can fail while diagnosing the first.
		"""
		self.forget_token()
		token = await self._bearer()
		claims = pyjwt.decode(token, options={"verify_signature": False})

		roles: list[str] = []
		for name, value in claims.items():
			if _ROLES_CLAIM.match(name) and isinstance(value, dict):
				roles.extend(str(role) for role in value)

		audience = claims.get("aud")
		return {
			"subject": claims.get("sub"),
			"audience": audience if isinstance(audience, list) else [audience],
			"expires_at": claims.get("exp"),
			"roles": sorted(set(roles)),
		}

	# ------------------------------------------------------------------ transport

	async def _request(
		self,
		method: str,
		path: str,
		*,
		json_body: dict[str, Any] | None = None,
		retry_auth: bool = True,
	) -> dict[str, Any]:
		"""One call to korsi-api, with the 401 retry that token caching makes necessary.

		Exactly one retry, and only for 401. Anything else is returned to the caller to decide
		about -- retry policy for a segment is not the same as for a watchlist poll, and this layer
		does not know which one it is serving.
		"""
		token = await self._bearer()
		http = await self._http()
		url = f"{self._base_url}{path}"

		try:
			response = await http.request(
				method,
				url,
				json=json_body,
				headers={
					"Authorization": f"Bearer {token}",
					"Accept": "application/json",
				},
				timeout=REQUEST_TIMEOUT_SECONDS,
			)
		except Exception as e:
			raise KorsiApiError(f"{method} {path} failed to reach korsi-api: {e}") from e

		status = _status_of(response)
		if status == 401 and retry_auth:
			LOGGER.info("Korsi rejected the token, re-minting once", extra={"path": path, "tag": "korsi"})
			self.forget_token()
			return await self._request(method, path, json_body=json_body, retry_auth=False)

		if status >= 400:
			raise self._problem_to_error(response, status, method=method, path=path)

		if not response.content:
			return {}
		parsed = response.json()
		return parsed if isinstance(parsed, dict) else {}

	@staticmethod
	def _problem_to_error(response: Any, status: int, *, method: str, path: str) -> KorsiApiError:
		"""Turn korsi-api's `application/problem+json` into the right local exception.

		The `code` matters more than the status: `live.session_closed` arrives as a 409, and 409 on
		its own would read as "conflict, try again" when the correct response is to stop transcribing
		this call.

		**A body that is not a problem document is reported as exactly that.** korsi-api always names
		its errors -- every one of them carries a `code` -- so a status with no code did not come from
		korsi-api. It came from whatever sits in front of it, which means korsi-api is not serving
		rather than refusing. Those are different problems with different owners, and the earlier
		version of this method rendered the second as `503 unknown:`, which reads like Korsi answered
		unhelpfully instead of like Korsi never answered.
		"""
		code = ""
		title = ""
		parsed = False
		try:
			problem = response.json()
			if isinstance(problem, dict):
				parsed = True
				code = str(problem.get("code", ""))
				title = str(problem.get("title", ""))
		except Exception:  # noqa: BLE001, S110 - a gateway's HTML error page is still an error
			# The body itself is not logged. A gateway error page is a screenful of markup per
			# failure, and the sentence built below carries everything that can be acted on.
			pass

		if code:
			message = f"{method} {path} -> {status} {code}: {title}".strip()
		else:
			# Named rather than blamed on Korsi: the content type is what tells an operator whether
			# they are looking at a reverse proxy, a load balancer or a captive portal.
			content_type = ""
			with suppress(Exception):
				content_type = str(response.headers.get("content-type", ""))
			if parsed:
				shape = "JSON without a code"
			else:
				shape = f"a {content_type} body" if content_type else "a body of no stated type"
			message = (
				f"{method} {path} -> {status}, and the response is {shape} rather than one of"
				" korsi-api's error documents. Something in front of korsi-api answered, so"
				" korsi-api is most likely not running or not healthy."
			)

		if code in _SESSION_GONE_CODES:
			return LiveSessionClosed(message, status=status)
		return KorsiApiError(message, status=status)

	# ------------------------------------------------------------------ the five calls

	async def watchlist(self) -> LiveWatchlist:
		"""Which Talk conversations Korsi would read a call in."""
		payload = await self._request("GET", "/api/v1/meetings/live/watchlist")
		return LiveWatchlist.model_validate(payload)

	async def open_session(
		self, *, room_remote_id: str, started_at: datetime, bridge_version: str | None = None
	) -> LiveSessionDecision:
		"""Ask whether to read this call, and open a session if the answer is yes.

		`started_at` is when the *call* started, not when the bridge noticed. korsi-api uses it to
		decide which meeting this is -- a Talk room is reused for every meeting a case holds, so the
		timestamp is what separates this morning's standup from this afternoon's review.
		"""
		payload = await self._request(
			"POST",
			"/api/v1/meetings/live/sessions",
			json_body={
				"room_remote_id": room_remote_id,
				"started_at": started_at.astimezone(UTC).isoformat(),
				"bridge_version": bridge_version,
			},
		)
		return LiveSessionDecision.model_validate(payload)

	async def append_segment(
		self, *, live_session_id: str, sequence: int, started_ms: int, ended_ms: int, text: str
	) -> LiveSegmentAccepted:
		"""Post one interval's worth of transcript.

		Raises
		------
		LiveSessionClosed
			korsi-api will not accept more transcript for this session. Stop and tear down; do not
			retry, and do not keep the text for later -- a live reading has no value once the call
			this session belonged to is over.
		KorsiApiError
			Anything else. Worth one more attempt on the next interval.

		"""
		payload = await self._request(
			"POST",
			f"/api/v1/meetings/live/sessions/{live_session_id}/segments",
			json_body={
				"sequence": sequence,
				"started_ms": started_ms,
				"ended_ms": ended_ms,
				"text": text,
			},
		)
		return LiveSegmentAccepted.model_validate(payload)

	async def renew_credential(self, *, live_session_id: str) -> LiveSttCredential:
		"""A fresh speech credential for a call that outlived the first one.

		Korsi mints these with a lifetime measured in minutes; a two-hour meeting therefore needs
		several. The bridge asks when its current key is close to expiring rather than on a schedule,
		because a key it never had to use is a charge nobody incurred.
		"""
		payload = await self._request("POST", f"/api/v1/meetings/live/sessions/{live_session_id}/stt")
		return LiveSttCredential.model_validate(payload)

	async def close_session(
		self, *, live_session_id: str, ended_at: datetime, reason: LiveCloseReason
	) -> None:
		"""Tell Korsi the call is over.

		The response is discarded on purpose: it is the session's final state, and the bridge has
		nothing left to do with it. What matters is that the call was made, so korsi-api settles the
		metering hold now rather than leaving it to the abandoned-session sweep twenty minutes later.

		Idempotent server-side, so a close that races the sweep is not an error.
		"""
		await self._request(
			"POST",
			f"/api/v1/meetings/live/sessions/{live_session_id}/close",
			json_body={
				"ended_at": ended_at.astimezone(UTC).isoformat(),
				"reason": reason.value,
			},
		)
