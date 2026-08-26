#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Never negotiate HTTP/3, because a reverse proxy that lies about it takes the bridge down.

`niquests` -- which this app and `nc_py_api` both use -- reads the `Alt-Svc` response header and
upgrades the connection to HTTP/3 when a server advertises it. When the advertisement is wrong, and
QUIC is not actually reachable, niquests does not fall back. It raises `MustDowngradeError` and the
request fails. Retries do not help, because the advertisement is still there on the next attempt.

That is exactly what happened in production, on both sides at once: the watchlist poll to korsi-api
and the admin page's registration call to Nextcloud both failed, because the same reverse proxy sits
in front of both and advertises `h3` while UDP/443 is closed. The bridge looked healthy, minted its
token, said it was watching for calls, and could not make a single request.

**Advertising `h3` without serving it is a server-side fault, and it is still ours to survive.** This
app is installed in a customer's own infrastructure and talks to whatever proxy they run. A browser
tolerates the mismatch; requiring every customer's proxy to be correct about QUIC before a meeting can
be read is not a support position worth defending. HTTP/2 over TLS is what the connection ends up
using anyway, so there is nothing to gain from the upgrade here: the two conversations are a poll
every minute and a small POST every few minutes.

`nc_py_api` builds its sessions in one place, `nc_py_api._session`, from names it imported into that
module. Replacing those two names with subclasses that default the flag is enough, and it is stable in
a way the alternatives are not: `disable_http3` is niquests' documented parameter, and the module
attribute is an ordinary import binding rather than a private method whose shape could change.
"""

import logging

import niquests
from nc_py_api import _session as nc_session

LOGGER = logging.getLogger("lt")


class Session(niquests.Session):
	"""A synchronous session that stays on HTTP/1.1 or HTTP/2."""

	def __init__(self, *args, **kwargs) -> None:
		kwargs.setdefault("disable_http3", True)
		super().__init__(*args, **kwargs)


class AsyncSession(niquests.AsyncSession):
	"""An asynchronous session that stays on HTTP/1.1 or HTTP/2."""

	def __init__(self, *args, **kwargs) -> None:
		kwargs.setdefault("disable_http3", True)
		super().__init__(*args, **kwargs)


def install() -> None:
	"""Point `nc_py_api` at the two subclasses. Call before anything builds a Nextcloud session.

	Idempotent, so importing this module twice is harmless. Not done at import time, because a module
	whose import silently changes another library's behaviour is the kind of thing that gets moved by
	an import sorter and stops working.
	"""
	if nc_session.Session is Session:
		return
	nc_session.Session = Session  # type: ignore[misc]
	nc_session.AsyncSession = AsyncSession  # type: ignore[misc]
	LOGGER.debug("HTTP/3 negotiation is disabled for Nextcloud sessions", extra={"tag": "connection"})
