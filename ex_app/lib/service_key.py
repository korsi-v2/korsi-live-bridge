#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
r"""Reading the ZITADEL service key out of an environment variable it may not have survived.

`KORSI_SERVICE_KEY` is a JSON document wrapping a multi-line PEM, and it reaches this container through
however many layers of quoting the customer's deployment tooling puts in the way. Coolify -- which is
what Korsi's own Nextcloud stacks run on -- escapes backslashes in environment values: the JSON still
parses, `keyId` and `userId` are still correct, the PEM still carries its BEGIN and END markers, and
every line break inside it is now the two characters `\` and `n`. `cryptography` refuses such a key, so
the first token request fails with a signing error during the first meeting anyone cared about, and
nothing in that failure points at an environment variable.

Three things happen here that a plain `json.loads` does not do.

**Base64 is accepted.** A single base64 token has no quotes, no braces, no backslashes and no newlines,
so a deployment UI has nothing left to escape. `provision-bridge` prints that form as the recommended
one. Raw JSON keeps working, because the two are told apart by the first character rather than by a
second variable an operator could set to the wrong thing.

**The PEM is repaired, not reported.** A PEM's base64 body is whitespace-insensitive, so any damage that
leaves the base64 characters in order is losslessly recoverable: undo the escape sequences, keep only
the characters that belong to the alphabet, re-wrap at 64 columns. That covers the escaped-backslash
case, the whole-thing-on-one-line case and the CRLF case without an operator having to know which one
they have. It is done unconditionally, because the alternative is a second code path exercised only on
deployments nobody can reach.

**Something is actually signed.** Looking for BEGIN and END proves nothing -- the mangled key has both,
which is exactly why it is so quiet a failure. The only check worth making is the operation that will
really be performed later, so a throwaway assertion gets signed and any failure is reported against the
name of the variable that caused it.
"""

import base64
import json
import re
import time
from dataclasses import dataclass

import jwt as pyjwt

#: A PEM label plus its body, requiring the END label to match the BEGIN label. `re.DOTALL` so this
#: also matches a block collapsed onto one line, which is what a paste into a single-line settings
#: field produces.
_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----", re.DOTALL)

#: What a deployment layer leaves behind when it treats a JSON string as shell-ish text. Longest
#: first, so the `\r\n` pair is not half-consumed by the `\n` rule.
_ESCAPES = (("\\r\\n", "\n"), ("\\n", "\n"), ("\\r", "\n"), ("\\t", "\n"))

#: Everything that is not base64. Applied to the PEM body only, where no other character is ever
#: legitimate -- which is what makes discarding them safe rather than merely convenient. Note the
#: ordering dependency on `_ESCAPES`: `n` is a base64 character, so a literal `\n` has to become a
#: newline *before* this runs, or the backslash is dropped and a stray `n` is kept.
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/=]")

_PEM_LINE_WIDTH = 64

_PASTE_HINT = "paste either form of the value provision-bridge printed"


class ServiceKeyError(ValueError):
	"""`KORSI_SERVICE_KEY` is present but cannot be used, with the reason in the message.

	A `ValueError` because callers that predate this module catch that, and because a bad value here
	is bad input rather than a broken invariant.
	"""


@dataclass(frozen=True)
class ServiceKey:
	"""A service key that has been proven able to sign."""

	key_id: str
	user_id: str
	private_key: str

	#: How the value arrived, and what had to be undone to make it usable. Surfaced by the status
	#: endpoint: a value that needed repair will need repairing again after every deploy, and the
	#: repair is otherwise invisible precisely because it worked.
	encoding: str = "json"
	repairs: tuple[str, ...] = ()


def load_service_key(raw: str) -> ServiceKey:
	"""Parse, repair and test-sign the service key.

	Raises
	------
	ServiceKeyError
		The value is not usable. The message names `KORSI_SERVICE_KEY` and says what is wrong with
		it, and never contains key material -- it is logged, returned by the status endpoint and shown
		in Nextcloud's admin UI when the app refuses to enable.

	"""
	text, encoding = _decode(raw)

	try:
		document = json.loads(text)
	except json.JSONDecodeError as exc:
		raise ServiceKeyError(
			f"KORSI_SERVICE_KEY is not valid JSON: {exc.msg} at position {exc.pos}. {_PASTE_HINT}"
		) from exc

	if not isinstance(document, dict):
		raise ServiceKeyError(f"KORSI_SERVICE_KEY is JSON but not an object. {_PASTE_HINT}")

	absent = [name for name in ("keyId", "userId", "key") if not document.get(name)]
	if absent:
		raise ServiceKeyError(f"KORSI_SERVICE_KEY is missing {', '.join(absent)}. {_PASTE_HINT}")

	private_key, repairs = _repair_pem(str(document["key"]))
	key_id = str(document["keyId"])
	_verify_can_sign(private_key, key_id=key_id)

	return ServiceKey(
		key_id=key_id,
		user_id=str(document["userId"]),
		private_key=private_key,
		encoding=encoding,
		repairs=repairs,
	)


def describe(raw: str | None) -> dict[str, object]:
	"""A redacted account of the service key, for the status endpoint.

	Names the key and the machine user, because neither is a secret and seeing them is how an operator
	confirms that the value which landed is the one Korsi issued. Never the private key, and never a
	fragment of it: this is read over HTTP by a Nextcloud administrator.
	"""
	if not raw:
		return {"present": False}
	try:
		key = load_service_key(raw)
	except ServiceKeyError as exc:
		return {"present": True, "usable": False, "problem": str(exc)}
	return {
		"present": True,
		"usable": True,
		"encoding": key.encoding,
		"repairs": list(key.repairs),
		"key_id": key.key_id,
		"user_id": key.user_id,
	}


def _decode(raw: str) -> tuple[str, str]:
	"""The JSON document, whether the variable held it directly or base64 of it.

	Discriminated by the first character after unwrapping, because base64 cannot begin with `{` and
	the JSON document cannot begin with anything else.
	"""
	text = raw.strip()

	# A value that travelled through a shell or a YAML scalar can arrive still wearing its quotes.
	for quote in ('"', "'"):
		if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
			text = text[1:-1].strip()
			break

	if text.startswith("{"):
		return text, "json"

	# Whitespace first: a long base64 value pasted into a textarea comes back with line breaks in it,
	# and `validate=True` -- which is what makes this a reliable test rather than a lenient one --
	# rejects them.
	candidate = "".join(text.split())
	try:
		decoded = base64.b64decode(candidate, validate=True).decode("utf-8")
	except ValueError as exc:
		raise ServiceKeyError(
			f"KORSI_SERVICE_KEY is neither a JSON object nor base64 of one. {_PASTE_HINT}"
		) from exc

	decoded = decoded.strip()
	if not decoded.startswith("{"):
		raise ServiceKeyError(
			f"KORSI_SERVICE_KEY decoded from base64, but the result is not a JSON object. {_PASTE_HINT}"
		)
	return decoded, "base64"


def _repair_pem(value: str) -> tuple[str, tuple[str, ...]]:
	"""A loadable PEM, and a note of whatever had to be undone to get one."""
	repairs: list[str] = []

	text = value
	for sequence, replacement in _ESCAPES:
		if sequence in text:
			text = text.replace(sequence, replacement)
			repairs.append(f"turned literal {sequence} escapes back into line breaks")

	block = _PEM_BLOCK.search(text)
	if block is None:
		raise ServiceKeyError(
			"KORSI_SERVICE_KEY holds no complete PEM private key block, so the key looks truncated."
			" Check that the whole value reached the setting, including its -----END ...----- line."
		)

	label, body = block.group(1), block.group(2)
	packed = _NOT_BASE64.sub("", body)
	if not packed:
		raise ServiceKeyError("KORSI_SERVICE_KEY has PEM markers with no key between them")

	wrapped = "\n".join(packed[at:at + _PEM_LINE_WIDTH] for at in range(0, len(packed), _PEM_LINE_WIDTH))
	rebuilt = f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"

	if rebuilt.strip() != text.strip():
		repairs.append("re-wrapped the PEM body")

	return rebuilt, tuple(repairs)


def _verify_can_sign(private_key: str, *, key_id: str) -> None:
	"""Sign a throwaway assertion, because that is the only check that proves anything.

	The realistic failure -- an escaped or re-wrapped PEM -- has both markers and all three JSON
	fields, so every cheaper check passes it. This costs about a millisecond, and runs when the app is
	enabled and when the status endpoint is read, not per request.
	"""
	now = int(time.time())
	try:
		pyjwt.encode(
			{"iss": "korsi-live-bridge-probe", "iat": now, "exp": now + 1},
			private_key,
			algorithm="RS256",
			headers={"kid": key_id},
		)
	except Exception as exc:
		# The exception text comes from `cryptography` and describes the encoding, not the contents.
		raise ServiceKeyError(
			"the private key in KORSI_SERVICE_KEY cannot be used to sign:"
			f" {type(exc).__name__}: {exc}"
		) from exc
