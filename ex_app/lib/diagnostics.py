#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Answering "why is there no live reading" without shell access to the container.

This bridge runs in somebody else's infrastructure, and the people who will be looking at it when it
does not work are Nextcloud administrators, not Korsi engineers. Between a Nextcloud administrator and
a working live reading there are seven independent things that can be wrong -- a mangled service key,
a shortened scope, an unreachable API, a tenant with the feature switched off, a room nobody linked to
a case, Talk without a high performance backend, a container that never enabled -- and from the outside
all seven look identical: the meeting happens and nothing appears.

So the self-test performs each step in the order the bridge itself performs them, reports each one
separately, and stops at the first failure that makes the rest meaningless. The point is not to say
whether the bridge is healthy. It is to name the one thing to go and fix.

The log tail exists for the same reason. The container's own log is behind a Docker socket the
administrator may not have, and the interesting lines are already structured JSON.
"""

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

from korsi_client import KorsiClient
from korsi_types import KorsiApiError
from service_key import describe
from utils import check_korsi_env_vars, get_hpb_settings

LOGGER = logging.getLogger("lt")

#: The one role `provision-bridge` grants this machine user. Named here so the self-test can say "the
#: token carries no roles" or "the token carries the wrong ones" instead of printing a list and leaving
#: the reader to know what should have been in it.
EXPECTED_ROLE = "service_account"

#: How many log lines may be asked for at once. A ceiling because the response is rendered in a browser
#: tab and the file is capped at 20 MB by the rotating handler.
MAX_LOG_LINES = 500

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


async def self_test() -> dict[str, Any]:
	"""Run every step between "app enabled" and "Korsi is listening", in order.

	Returns a list of named checks rather than a single verdict, and includes the checks that were
	skipped: "we never got as far as asking Korsi" is itself the answer sometimes.
	"""
	checks: list[dict[str, Any]] = []

	problems = check_korsi_env_vars()
	checks.append(_check(
		"configuration",
		not problems,
		"every Korsi setting is present and the service key can sign"
		if not problems
		else "; ".join(problems),
	))
	if problems:
		return _finish(checks, skipped=("talk_signaling", "korsi_token", "korsi_watchlist"))

	# In a thread because it is a synchronous OCS round trip to Nextcloud, and this is running on the
	# event loop that is also pumping a call's audio.
	try:
		settings = await asyncio.to_thread(get_hpb_settings)
		checks.append(_check("talk_signaling", True, f"Talk's high performance backend is at {settings.server}"))
	except Exception as e:  # noqa: BLE001 - every failure here is reported, none is escalated
		checks.append(_check(
			"talk_signaling",
			False,
			f"could not read Talk's signaling settings: {e}."
			" Talk needs a high performance backend before any call can be read.",
		))

	client: KorsiClient | None = None
	try:
		client = KorsiClient()
	except Exception as e:  # noqa: BLE001
		checks.append(_check("korsi_token", False, f"could not build a Korsi client: {e}"))
		return _finish(checks, skipped=("korsi_watchlist",))

	try:
		checks.extend(await _korsi_checks(client))
	finally:
		await client.aclose()

	return _finish(checks, skipped=())


async def _korsi_checks(client: KorsiClient) -> list[dict[str, Any]]:
	"""Mint a token, look at what it asserts, then use it."""
	checks: list[dict[str, Any]] = []

	try:
		claims = await client.fresh_token_claims()
	except KorsiApiError as e:
		# Unreachable and refused are separate problems with separate owners: one is this network, the
		# other is the credential. `KorsiApiError` carries a status only when there was a response.
		advice = (
			"KORSI_TOKEN_URL is wrong, or this server cannot reach Korsi's identity provider"
			if e.status is None
			else "KORSI_SERVICE_KEY is not a key this identity provider recognises, or it has expired"
		)
		checks.append(_check("korsi_token", False, f"no token was issued: {e}. {advice}."))
		checks.append(_check("korsi_watchlist", None, "not attempted: there is no token to use"))
		return checks
	except Exception as e:  # noqa: BLE001
		checks.append(_check("korsi_token", False, f"no token was issued: {e}"))
		checks.append(_check("korsi_watchlist", None, "not attempted: there is no token to use"))
		return checks

	roles = claims.get("roles") or []
	has_role = EXPECTED_ROLE in roles
	checks.append(_check(
		"korsi_token",
		has_role,
		f"a token was issued for {claims.get('subject')} carrying the {EXPECTED_ROLE} role"
		if has_role
		else (
			f"a token was issued for {claims.get('subject')} but it carries no {EXPECTED_ROLE} role"
			f" (roles: {', '.join(roles) or 'none'}). Korsi will refuse every call. This is almost"
			" always KORSI_TOKEN_SCOPE: it must still contain urn:zitadel:iam:org:projects:roles"
			" and the organization URN exactly as Korsi printed them."
		),
		details={"audience": claims.get("audience"), "roles": roles},
	))

	try:
		watchlist = await client.watchlist()
	except KorsiApiError as e:
		checks.append(_check(
			"korsi_watchlist",
			False,
			f"Korsi refused the watchlist request: {e}",
		))
		return checks
	except Exception as e:  # noqa: BLE001
		checks.append(_check("korsi_watchlist", False, f"could not reach Korsi: {e}"))
		return checks

	if not watchlist.enabled:
		detail = (
			"Korsi answered, and says live assistance is switched off for this tenant. Nothing will be"
			" read until it is enabled on the Korsi side; the bridge is otherwise fine."
		)
	elif not watchlist.rooms:
		detail = (
			"Korsi answered and live assistance is on, but no Talk conversation is linked to a case yet."
			" Link a room to an operational case in Korsi and it will appear here."
		)
	else:
		detail = f"Korsi is watching {len(watchlist.rooms)} conversation(s)"

	checks.append(_check(
		"korsi_watchlist",
		watchlist.enabled,
		detail,
		details={"rooms": [room.room_remote_id for room in watchlist.rooms]},
	))
	return checks


def _check(name: str, ok: bool | None, detail: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
	"""One step's result. `ok=None` means it was not attempted, which is not the same as failing."""
	result: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
	if details:
		result.update(details)
	return result


def _finish(checks: list[dict[str, Any]], *, skipped: tuple[str, ...]) -> dict[str, Any]:
	for name in skipped:
		checks.append(_check(name, None, "not attempted: an earlier check failed"))
	return {
		"ok": all(check["ok"] for check in checks if check["ok"] is not None) and not skipped,
		"checks": checks,
		"service_key": describe(os.getenv("KORSI_SERVICE_KEY")),
	}


def tail_log(*, lines: int = 100, min_level: str = "INFO") -> dict[str, Any]:
	"""The most recent log records, newest last.

	Reads the JSON file the rotating handler writes rather than the container's stdout, because stdout
	needs a Docker socket and this needs a Nextcloud login. Malformed lines are kept as raw text: a
	half-written final line is normal in a file being appended to, and dropping it silently would hide
	the record somebody is looking for.
	"""
	lines = max(1, min(lines, MAX_LOG_LINES))
	wanted = min_level.upper()
	threshold = LOG_LEVELS.index(wanted) if wanted in LOG_LEVELS else LOG_LEVELS.index("INFO")

	path = _log_path()
	if path is None or not path.is_file():
		return {"path": str(path) if path else None, "available": False, "records": []}

	kept: deque[dict[str, Any]] = deque(maxlen=lines)
	with path.open(encoding="utf-8", errors="replace") as handle:
		for raw in handle:
			raw = raw.strip()
			if not raw:
				continue
			try:
				record = json.loads(raw)
			except json.JSONDecodeError:
				kept.append({"level": "RAW", "message": raw})
				continue
			level = str(record.get("level", "INFO")).upper()
			if level in LOG_LEVELS and LOG_LEVELS.index(level) < threshold:
				continue
			kept.append(record)

	return {"path": str(path), "available": True, "records": list(kept)}


def _log_path() -> Path | None:
	"""Where `logger_config.yaml` ends up writing, resolved the same way `get_logging_config` does.

	Duplicated rather than imported because importing it would mean parsing the YAML again on every
	request to an endpoint whose whole job is to be cheap enough to poll.
	"""
	storage = os.getenv("APP_PERSISTENT_STORAGE", "persistent_storage")
	return Path(storage) / "logs" / "lt.log"
