#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import hashlib
import hmac
import logging
import os
import re
import ssl
from collections.abc import Callable
from functools import wraps
from time import time
from typing import Any
from urllib.parse import urlparse

from constants import CACHE_TTL
from livetypes import HPBSettings
from nc_py_api import NextcloudApp
from service_key import ServiceKeyError, load_service_key

LOGGER = logging.getLogger("lt.utils")


def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


def get_ssl_context(server_addr: str) -> ssl.SSLContext | None:
	nc = NextcloudApp()

	if server_addr.startswith(("ws://", "http://")):
		LOGGER.info("Using default SSL context for insecure WebSocket connection (ws://)", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return None

	cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
	if cert_verify in ("true", "1"):
		LOGGER.info("Skipping certificate verification for WebSocket connection", extra={
			"server_addr": server_addr,
			"SKIP_CERT_VERIFY": cert_verify,
			"tag": "connection",
		})
		ssl_ctx = ssl.SSLContext()
		ssl_ctx.check_hostname = False
		ssl_ctx.verify_mode = ssl.CERT_NONE
		return ssl_ctx

	if nc.app_cfg.options.nc_cert and isinstance(nc.app_cfg.options.nc_cert, ssl.SSLContext):
		# Use the SSL context provided by nc_py_api
		LOGGER.info("Using SSL context provided by nc_py_api", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return nc.app_cfg.options.nc_cert

	# verify certificate normally and don't use SSLContext from nc_py_api
	LOGGER.info("Using default SSL context for WebSocket connection", extra={
		"server_addr": server_addr,
		"tag": "connection",
	})
	return None


def check_hpb_env_vars():
	# Check if the required environment variables are set
	required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
	missing_vars = [var for var in required_vars if not os.getenv(var)]
	if missing_vars:
		raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

	hpb_url = os.environ["LT_HPB_URL"]
	hpb_url_host = urlparse(hpb_url).hostname
	if not hpb_url_host:
		raise ValueError(
			f"Could not detect hostname in LT_HPB_URL env var: {hpb_url}. "
			"Verify that it is a valid URL with a protocol and hostname."
		)


#: Everything the bridge needs to talk to Korsi. All four, because there is no useful partial state:
#: without any one of them the bridge cannot ask which rooms to watch, and therefore does nothing.
KORSI_REQUIRED_VARS = (
	"KORSI_API_URL",
	"KORSI_TOKEN_URL",
	"KORSI_TOKEN_SCOPE",
	"KORSI_SERVICE_KEY",
)


def check_korsi_env_vars() -> list[str]:
	"""What is wrong with the Korsi settings, as a list of problems an operator can act on.

	Returns descriptions rather than raising, and never a value: two of these are secrets, and this
	result is rendered into the Nextcloud admin UI and into a status endpoint.
	"""
	missing = [var for var in KORSI_REQUIRED_VARS if not os.getenv(var)]

	api_url = os.getenv("KORSI_API_URL")
	if api_url and not urlparse(api_url).hostname:
		missing.append("KORSI_API_URL (not a valid URL)")
	token_url = os.getenv("KORSI_TOKEN_URL")
	if token_url and not urlparse(token_url).hostname:
		missing.append("KORSI_TOKEN_URL (not a valid URL)")

	# Fully loaded and test-signed here as well as at construction, so a value the deployment layer
	# damaged is named by the status endpoint and by the enable handler -- which is where somebody is
	# looking -- rather than only by a signing error during the first meeting.
	service_key = os.getenv("KORSI_SERVICE_KEY")
	if service_key:
		try:
			load_service_key(service_key)
		except ServiceKeyError as e:
			missing.append(str(e))

	# The roles assertion is the one reserved scope whose absence produces a working token that
	# is refused by korsi-api for an unrelated-looking reason. Worth naming before that happens.
	scope = os.getenv("KORSI_TOKEN_SCOPE")
	if scope and "urn:zitadel:iam:org:projects:roles" not in scope:
		missing.append("KORSI_TOKEN_SCOPE (missing the projects:roles scope)")

	return missing


def get_hpb_settings() -> HPBSettings:
	check_hpb_env_vars()
	try:
		nc = NextcloudApp()
		settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
		hpb_settings = HPBSettings(**settings)
		LOGGER.debug("HPB settings retrieved successfully", extra={
			"stun_servers": [s.urls for s in hpb_settings.stunservers],
			"turn_servers": [t.urls for t in hpb_settings.turnservers],
			"server": hpb_settings.server,
			"tag": "hpb_settings",
		})
		return hpb_settings
	except Exception as e:
		raise Exception("Error getting HPB settings") from e


def sanitize_websocket_url(ws_url: str) -> str:
	ws_url = re.sub(r"^http://", "ws://", ws_url)
	ws_url = re.sub(r"^https://", "wss://", ws_url)
	if not ws_url.removesuffix("/").endswith("/spreed"):
		ws_url = ws_url.removesuffix("/") + "/spreed"
	return ws_url


# does not support caching of kwargs for recall
def timed_cache(ttl: int = CACHE_TTL):
	def decorator(fn: Callable):
		cached_store: dict[tuple, tuple[float, Any]] = {}
		@wraps(fn)
		def wrapper(*args, **kwargs):
			if args in cached_store:
				cached_time, cached_value = cached_store[args]
				if (time() - cached_time) < ttl:
					return cached_value
			new_val = fn(*args, **kwargs)
			cached_store[args] = (time(), new_val)
			return new_val
		return wrapper
	return decorator


# does not support caching of kwargs for recall
def timed_cache_async(ttl: int = CACHE_TTL):
	def decorator(fn: Callable):
		cached_store: dict[tuple, tuple[float, Any]] = {}
		@wraps(fn)
		async def wrapper(*args, **kwargs):
			if args in cached_store:
				cached_time, cached_value = cached_store[args]
				if (time() - cached_time) < ttl:
					return cached_value
			new_val = await fn(*args, **kwargs)
			cached_store[args] = (time(), new_val)
			return new_val
		return wrapper
	return decorator
