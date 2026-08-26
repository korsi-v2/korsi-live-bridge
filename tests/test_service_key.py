#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
r"""Does the service key survive the trip into someone else's deployment platform?

These are regression tests for a real failure, and worth reading as a description of it. A tenant's
bridge was deployed with a correct service key and could not mint a token. The value in the container
was valid JSON, had all three fields, had the PEM's BEGIN and END markers, and was unusable: Coolify
had escaped the backslashes in the environment value, so every line break inside the key was now the
two characters `\\` and `n`. Every check the bridge performed passed it, and the only symptom was that
meetings were not analysed.

So the cases below are the mangling that happens in the field, not the mangling that is easy to
imagine. Each one is a value an operator could plausibly end up with.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from service_key import ServiceKeyError, describe, load_service_key


@pytest.fixture(scope="module")
def pem() -> str:
	"""A real RSA private key, generated per run.

	Not a fixture file. A private key committed to a repository is a credential-shaped thing that
	eventually gets copied somewhere it matters, and a secret scanner flagging it is the good outcome.
	"""
	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	return key.private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.TraditionalOpenSSL,
		encryption_algorithm=serialization.NoEncryption(),
	).decode()


def document(pem: str) -> str:
	return json.dumps({"type": "serviceaccount", "keyId": "k1", "userId": "u1", "key": pem})


# ---------------------------------------------------------------- the forms that should work


def test_raw_json_is_accepted(pem):
	key = load_service_key(document(pem))

	assert key.key_id == "k1"
	assert key.user_id == "u1"
	assert key.encoding == "json"
	assert key.repairs == ()


def test_base64_is_accepted(pem):
	"""The recommended form, because there is nothing in it for a settings field to escape."""
	key = load_service_key(base64.b64encode(document(pem).encode()).decode())

	assert key.key_id == "k1"
	assert key.encoding == "base64"
	assert key.repairs == ()
	assert key.private_key.startswith("-----BEGIN")


def test_base64_survives_being_wrapped_across_lines(pem):
	"""A long value pasted into a textarea comes back with line breaks in it."""
	encoded = base64.b64encode(document(pem).encode()).decode()
	wrapped = "\n".join(encoded[at:at + 60] for at in range(0, len(encoded), 60))

	assert load_service_key(wrapped).encoding == "base64"


def test_surrounding_quotes_are_ignored(pem):
	"""A value that travelled through a shell or a YAML scalar can arrive still wearing them."""
	assert load_service_key(f'"{document(pem)}"').key_id == "k1"


# ---------------------------------------------------------------- the damage that happens in the field


def test_escaped_newlines_are_repaired_and_reported(pem):
	"""The Coolify failure, exactly as it arrived.

	This value has valid JSON, all three fields, and both PEM markers. Every check short of using the
	key passes it, which is why the bridge now uses the key.
	"""
	mangled = document(pem).replace("\\n", "\\\\n")

	key = load_service_key(mangled)

	assert key.private_key.strip() == pem.strip()
	# Reported rather than silently fixed: a value damaged once is damaged again on the next deploy,
	# and the operator can end that by switching to base64.
	assert key.repairs, "a repaired key must say that it was repaired"


def test_a_key_collapsed_onto_one_line_is_repaired(pem):
	"""A multi-line PEM pasted into a single-line form field, with the line breaks simply gone."""
	head, _, rest = pem.partition("-----\n")
	body, _, _ = rest.partition("\n-----END")
	collapsed = f"{head}-----{body.replace(chr(10), '')}-----END RSA PRIVATE KEY-----"

	key = load_service_key(json.dumps({"keyId": "k1", "userId": "u1", "key": collapsed}))

	assert key.private_key.strip() == pem.strip()


def test_crlf_line_endings_are_repaired(pem):
	key = load_service_key(json.dumps({"keyId": "k1", "userId": "u1", "key": pem.replace("\n", "\r\n")}))

	assert key.private_key.strip() == pem.strip()


# ---------------------------------------------------------------- the damage that cannot be repaired


def test_a_truncated_key_says_so(pem):
	"""Half a key. Not recoverable, and the message has to name what is missing."""
	half = pem[: len(pem) // 2]

	with pytest.raises(ServiceKeyError, match="truncated"):
		load_service_key(json.dumps({"keyId": "k1", "userId": "u1", "key": half}))


def test_a_corrupt_key_body_is_caught_by_signing(pem):
	"""Both markers, the right fields, and a body that is not a key.

	The case that motivated test-signing instead of checking for markers.
	"""
	corrupt = "-----BEGIN RSA PRIVATE KEY-----\nQUJDREVG\n-----END RSA PRIVATE KEY-----\n"

	with pytest.raises(ServiceKeyError, match="cannot be used to sign"):
		load_service_key(json.dumps({"keyId": "k1", "userId": "u1", "key": corrupt}))


def test_missing_fields_are_named(pem):
	with pytest.raises(ServiceKeyError, match="keyId, userId"):
		load_service_key(json.dumps({"key": pem}))


def test_a_value_that_is_neither_json_nor_base64_says_so():
	with pytest.raises(ServiceKeyError, match="neither a JSON object nor base64"):
		load_service_key("this is not it at all")


def test_base64_of_something_that_is_not_the_document_says_so():
	with pytest.raises(ServiceKeyError, match="not a JSON object"):
		load_service_key(base64.b64encode(b"hello there").decode())


# ---------------------------------------------------------------- what the admin page may show


def test_describe_never_returns_the_private_key(pem):
	report = describe(document(pem))

	assert report["usable"] is True
	assert report["key_id"] == "k1"
	assert report["user_id"] == "u1"
	# The whole point of a separate reporting function. This is rendered in a browser, screenshotted
	# and pasted into support tickets.
	assert "-----BEGIN" not in json.dumps(report)


def test_describe_reports_an_unusable_key_instead_of_raising():
	report = describe(json.dumps({"keyId": "k1", "userId": "u1", "key": "nonsense"}))

	assert report["usable"] is False
	assert "truncated" in str(report["problem"])


def test_describe_reports_an_absent_key():
	assert describe(None) == {"present": False}
