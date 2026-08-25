#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The application object: one Korsi client, one call watcher, nothing else.

Upstream's `Application` is a registry of `SpreedClient`s keyed by room token, with the retry and
deferral logic for joining a call on request. All of that moved to `CallWatcher`, because the thing
being reconciled changed: upstream reconciles against *requests from viewers*, and this fork reconciles
against Korsi's watchlist. What is left is lifecycle -- start when Nextcloud enables the app, stop when
it does not -- and a status read for the admin who wants to know whether any of this is working.
"""

import logging

from call_watcher import CallWatcher
from korsi_client import KorsiClient
from livetypes import HPBSettings, SpreedClientException
from utils import get_hpb_settings

LOGGER = logging.getLogger("lt")


class Application:
	"""Everything the container is doing, which is either nothing or watching rooms."""

	def __init__(self) -> None:
		self.hpb_settings: HPBSettings | None = None
		self._korsi: KorsiClient | None = None
		self._watcher: CallWatcher | None = None

	@property
	def running(self) -> bool:
		return self._watcher is not None

	async def start(self) -> None:
		"""Begin watching, or explain why not.

		Deliberately not silent about missing configuration. A bridge that starts, finds no Korsi
		credentials and quietly does nothing is indistinguishable from a bridge that is working and
		waiting for a meeting -- and the difference only becomes visible after somebody's meeting was
		not recorded.
		"""
		if self._watcher is not None:
			LOGGER.debug("Already watching", extra={"tag": "application"})
			return

		if self.hpb_settings is None:
			try:
				self.hpb_settings = get_hpb_settings()
			except Exception as e:
				raise SpreedClientException(
					"No HPB settings found. Either the app is not enabled in Nextcloud or the"
					" signaling settings could not be fetched."
				) from e

		try:
			self._korsi = KorsiClient()
		except KeyError as e:
			raise SpreedClientException(
				f"Korsi is not configured: environment variable {e} is missing. The bridge cannot"
				" ask Korsi which rooms to watch."
			) from e

		self._watcher = CallWatcher(korsi=self._korsi)
		self._watcher.start(self.hpb_settings)
		LOGGER.info("Korsi live bridge is watching for calls", extra={"tag": "application"})

	async def stop(self) -> None:
		"""Stop watching and close every live session that is still open.

		Closing sessions here is what makes a container restart cheap: korsi-api would otherwise hold
		each one's metering reservation until the abandoned-session sweep noticed, twenty minutes after
		a deploy that took thirty seconds.
		"""
		if self._watcher is not None:
			await self._watcher.stop()
			self._watcher = None
		if self._korsi is not None:
			await self._korsi.aclose()
			self._korsi = None
		LOGGER.info("Korsi live bridge stopped", extra={"tag": "application"})

	def status(self) -> dict:
		"""What an admin needs to see to tell "working" from "silently broken"."""
		return {
			"watching": self.running,
			"rooms": self._watcher.watched_rooms if self._watcher else [],
			"hpb_configured": self.hpb_settings is not None,
		}
