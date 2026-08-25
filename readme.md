<!--
  - SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
  - SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Korsi Live Meeting Bridge

Reads Nextcloud Talk calls in rooms Korsi tracks, and sends the **text** to Korsi for live analysis.

A fork of [nextcloud/live_transcription](https://github.com/nextcloud/live_transcription), whose WebRTC
and signaling code does the hard part. Everything downstream of the audio is different, and so is the
reason the app joins a call at all.

## What it does

1. Asks korsi-api which Talk conversations belong to an operational case, and holds a room-level
   signaling session for each of them.
2. Notices a call starting, and asks Korsi whether to read it. Korsi answers with the meeting the call
   belongs to, how often to post, and a temporary speech credential scoped to that one session.
3. Mixes every participant's audio into one mono stream and streams it to Soniox under that credential.
4. Every few minutes, posts the confirmed transcript of that interval to korsi-api, which analyses it and
   renders a live reading in Korsi.
5. Closes the session when the call ends.

**Audio never reaches Korsi.** It goes from this container straight to the speech provider — see
[ADR-0021](https://github.com/korsi-v2/korsi-docs/blob/dev/adr/0021-live-meeting-assistance-is-text-only.md).

## How it differs from upstream

These are design decisions rather than incidental drift, and each one is explained where it lives in the
code.

| | upstream | here |
|---|---|---|
| **Why it joins a call** | a participant turned captions on, per viewer | Korsi's watchlist says the room matters; no human has to opt in |
| **Where text goes** | back to each viewer's browser over signaling | posted to korsi-api; nothing is shown in Talk |
| **Speech provider** | Vosk, in this container, one connection per speaker | Soniox, one connection per call, Korsi-minted temporary key |
| **Audio handling** | assumes s16/48 kHz, hand-downmixes stereo | resamples explicitly to mono s16 48 kHz, mixes N publishers |
| **Provider protocol** | send a chunk, await one reply, under a mutex | independent sender and receiver, no lock |
| **Speaker attribution** | per-speaker captions | none, deliberately — a live reading is not the record |
| **Image** | CUDA base, Kaldi and Vosk built from source | `python:3.12-slim`, wheels only |
| **Persistent storage** | gigabytes of acoustic models | none |
| **Translation** | yes | removed |

**No speaker attribution is a choice, not a gap.** The organizations this serves meet in person and join
through one laptop, so paying per participant would separate voices that share a microphone anyway. The
citable transcript, with confirmed speakers, is produced from the recording afterwards by Korsi's batch
pipeline; the live reading is a working surface for the people in the room and must never be presented as
the meeting's record.

## Requirements

- Nextcloud 33–35 with Talk and a **High Performance Backend**. The bridge authenticates to the HPB as an
  internal client, which is how it watches a room without being a participant of the conversation.
- An AppAPI deploy daemon.
- A Korsi tenant with live meeting assistance enabled, and a registered bridge machine account.

## Configuration

Set these in "Deploy Options", next to "Deploy and Enable", **before** installing. The app refuses to
enable without them rather than starting up and silently recording nothing.

| variable | what it is |
|---|---|
| `LT_HPB_URL` | Talk's HPB signaling URL, ending in `/spreed` |
| `LT_INTERNAL_SECRET` | the HPB's `internalsecret`, not Talk's shared secret |
| `KORSI_API_URL` | e.g. `https://api.korsi.ai` |
| `KORSI_TOKEN_URL` | Korsi's OAuth2 token endpoint |
| `KORSI_CLIENT_ID` / `KORSI_CLIENT_SECRET` | the machine account Korsi issued for this Nextcloud |
| `KORSI_TOKEN_SCOPE` | the scope string Korsi provided, **verbatim** |

`KORSI_TOKEN_SCOPE` carries an audience identifier that addresses the token to korsi-api. Composed by
hand and it will look right and fail: the identity provider issues the token, and Korsi rejects every
call as unauthorized because the roles arrived in a claim it does not read.

There is no speech-provider key here. Korsi mints a session-scoped temporary one per call, so no
long-lived Korsi credential sits in customer infrastructure.

`GET /api/v1/status` reports whether the bridge is watching and which rooms it was given — the way to
tell a working bridge waiting for a meeting from a misconfigured one. It names missing settings, never
their values.

## Development

### Docker

1. `docker build -t ghcr.io/korsi-v2/korsi-live-bridge .`
2. Register a HaRP/Docker socket proxy deploy daemon:
   https://docs.nextcloud.com/server/latest/admin_manual/exapps_management/AppAPIAndExternalApps.html
3. Add a ghcr.io override: `occ app_api:daemon:registry:add --registry-from=ghcr.io --registry-to=local <deploy_daemon_name>`
4. Register the app:
   ```
   occ app_api:app:register korsi_live_bridge <deploy_daemon_name> \
     --info-xml /path/to/korsi-live-bridge/appinfo/info.xml --wait-finish \
     --env LT_HPB_URL=wss://nextcloud.local/standalone-signaling/spreed \
     --env LT_INTERNAL_SECRET=7890 \
     --env KORSI_API_URL=https://api.korsi.ai \
     --env KORSI_TOKEN_URL=https://auth.korsi.ai/oauth/v2/token \
     --env KORSI_CLIENT_ID=... --env KORSI_CLIENT_SECRET=... --env KORSI_TOKEN_SCOPE="..."
   ```

### Bare metal

1. `cp example.env .env` and fill it in.
2. `python -m venv .venv && . .venv/bin/activate && pip install -r requirements_dev.txt`
3. `python ex_app/lib/main.py`
4. Register a manual deploy daemon:
   `occ app_api:daemon:register manual_install "Manual Install" "manual-install" "http" host.docker.internal "http://nextcloud.local"`
5. Register the app:
   ```
   occ app_api:app:register korsi_live_bridge manual_install --json-info \
     "{\"id\":\"korsi_live_bridge\",\"name\":\"Korsi Live Meeting Bridge\",\"daemon_config_name\":\"manual_install\",\"version\":\"1.0.0\",\"secret\":\"12345\",\"port\":23000}" \
     --wait-finish
   ```

### Tests

`pytest`. Covers the mixer's arithmetic and the segmenter's interval bookkeeping — the two places where a
mistake is silent — plus the request bodies, validated against korsi-api's generated `openapi.json` when
that repo is checked out alongside this one.

### Tracking upstream

`upstream` points at `nextcloud/live_transcription`. The fork boundary is deliberately narrow: WebRTC,
HPB signaling and the offer/candidate handling are upstream's and change little, so rebases mostly touch
`spreed_client.py`. `korsi_client.py`, `korsi_types.py`, `audio_mixer.py`, `soniox_stream.py`,
`segmenter.py` and `call_watcher.py` are ours and have no upstream counterpart.

## Licence

AGPL-3.0-or-later, inherited from upstream. The bridge is the one Korsi component whose source must be
offered to the customers running it.
