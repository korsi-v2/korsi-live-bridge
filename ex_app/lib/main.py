# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The ExApp entry point.

**Almost no HTTP surface, and that is the design.** Upstream exposes routes for starting transcription,
setting a room's language and listing translation languages, because Talk's PHP side drives it: a
participant presses a button and Nextcloud tells the app what to do. This bridge is driven from the
other end -- it asks Korsi which rooms to watch and notices calls itself -- so the only routes left are
the two AppAPI requires and one an administrator needs to tell "working" from "silently doing nothing".

The consequence worth stating: enabling this app in Nextcloud starts a background loop that talks to
Korsi. Nothing in the Talk UI turns it on or off, because for Korsi's customers the recording is not
an opt-in per meeting.
"""

import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Event

import urllib3

# isort: off
from livetypes import SpreedClientException
from dotenv import load_dotenv
load_dotenv()

# skip certificate verification for all nc_py_api connections if env var is set
__skip_cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
if __skip_cert_verify in ("true", "1"):
	os.environ["NPA_NC_CERT"] = "false"
	urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# isort: on

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from logger import get_logging_config, setup_logging
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, run_app, set_handlers, setup_nextcloud_logging
from service import Application
from utils import check_korsi_env_vars, get_hpb_settings

LOGGER_CONFIG_NAME = "../../logger_config.yaml"
LOGGER = logging.getLogger("lt")
SERVICE: Application
ENABLED = Event()

# No models to fetch. Upstream downloads Vosk models into persistent storage on install -- gigabytes
# per language, on the customer's disk, with a pinned revision to keep in step. Speech happens at
# Soniox under a key Korsi mints per session, so this container carries no model and needs no
# persistent storage at all.


@asynccontextmanager
async def lifespan(app: FastAPI):
	global SERVICE
	set_handlers(app, enabled_handler)
	SERVICE = Application()
	nc = NextcloudApp()
	if nc.enabled_state:
		ENABLED.set()
		try:
			SERVICE.hpb_settings = get_hpb_settings()
		except Exception as e:
			LOGGER.warning("Failed to get the HPB settings when app is enabled", exc_info=e)
		try:
			await SERVICE.start()
		except Exception as e:
			LOGGER.error("Could not start watching for calls on startup", exc_info=e)
	LOGGER.info("App is %s on startup", "enabled" if ENABLED.is_set() else "disabled")
	yield
	# Closes the live sessions of any call in progress rather than leaving them for korsi-api's sweep.
	with suppress(Exception):
		await SERVICE.stop()


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware
ROUTER_V1 = APIRouter(prefix="/api/v1", tags=["v1"])


@APP.exception_handler(SpreedClientException)
async def spreed_client_exception_handler(request, exc: SpreedClientException):
	return JSONResponse(
		status_code=500,
		content={"error": str(exc)},
	)


@APP.get("/enabled")
async def get_enabled():
	return {"enabled": ENABLED.is_set()}


@ROUTER_V1.get("/status",
	description=(
		"Whether the bridge is watching for calls, and which rooms Korsi told it to watch."
		" Read this to distinguish a working bridge waiting for a meeting from a misconfigured one."
	),
	responses={200: {"description": "Current state of the bridge."}},
)
async def get_status():
	status = SERVICE.status()
	status["enabled"] = ENABLED.is_set()
	# Reports which variables are missing, never their values: two of them are secrets.
	status["missing_configuration"] = check_korsi_env_vars()
	return status


APP.include_router(ROUTER_V1)


# until capabilities is supported in nc_py_api
@APP.get("/capabilities")
async def get_capabilities() -> dict[str, dict]:
	"""What this app tells Nextcloud it can do.

	Not `live_transcription`. Upstream advertises that so Talk offers a captions button, and this fork
	has no captions to offer: the transcript goes to Korsi, and it is not shown in Talk at all. Claiming
	the upstream capability would put a button in the Talk UI that does nothing.
	"""
	return {
		f"{os.environ['APP_ID']}": {
			"version": f"{os.environ['APP_VERSION']}",
			"features": ["korsi_live_meeting_bridge"],
		}
	}


def enabled_handler(enabled: bool, nc: NextcloudApp | AsyncNextcloudApp) -> str:
	"""Nextcloud turned the app on or off.

	Returning a non-empty string makes Nextcloud refuse to enable the app and show the reason, which is
	the right behaviour for missing configuration: an app that enables successfully and then cannot
	reach Korsi looks installed and is not working.
	"""
	print(f"enabled={enabled}", flush=True)
	if not enabled:
		ENABLED.clear()
		return ""

	missing = check_korsi_env_vars()
	if missing:
		return (
			"Korsi is not configured. Set these deploy options and try again: " + ", ".join(missing)
		)

	ENABLED.set()
	try:
		SERVICE.hpb_settings = get_hpb_settings()
	except Exception as e:
		LOGGER.warning("Failed to get the HPB settings when app is enabled", exc_info=e)
		return (
			"Could not read Nextcloud Talk's signaling settings. Talk needs a High Performance"
			" Backend configured before this bridge can listen to calls."
		)
	return ""


if __name__ == "__main__":
	os.chdir(Path(__file__).parent)
	logging_config = get_logging_config(LOGGER_CONFIG_NAME)
	setup_logging(logging_config)
	setup_nextcloud_logging("lt", logging.WARNING)
	uv_log_config = uvicorn.config.LOGGING_CONFIG  # pyright: ignore[reportAttributeAccessIssue]
	uv_log_config["formatters"]["json"] = logging_config["formatters"]["json"]
	uv_log_config["handlers"]["file_json"] = logging_config["handlers"]["file_json"]
	uv_log_config["loggers"]["uvicorn"]["handlers"].append("file_json")
	uv_log_config["loggers"]["uvicorn.access"]["handlers"].append("file_json")
	run_app("main:APP", log_level="info")
