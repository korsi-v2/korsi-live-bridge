#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from enum import IntEnum

from pydantic import BaseModel


class StunServer(BaseModel):
	urls: list[str]

class TurnServer(BaseModel):
	urls: list[str]
	username: str
	credential: str

class HPBSettings(BaseModel):
	server: str
	stunservers: list[StunServer]
	turnservers: list[TurnServer]


class SigConnectResult(IntEnum):
	SUCCESS = 0
	FAILURE = 1  # do not retry
	RETRY   = 2


class ReconnectMethod(IntEnum):
	NO_RECONNECT = 0
	SHORT_RESUME = 1
	FULL_RECONNECT = 2


class CallFlag(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class SpreedClientException(Exception):
	"""Base exception for SpreedClient errors."""


class SpreedRateLimitedException(SpreedClientException):
	"""Exception raised when the Spreed Client is rate limited by the HPB server."""


class StreamEndedException(Exception):
	...
