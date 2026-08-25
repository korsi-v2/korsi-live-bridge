#!/bin/bash
#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

set -e

# FRP
if [ -f /frpc.toml ] && [ -n "$HP_SHARED_KEY" ]; then
  if pgrep -x "frpc" > /dev/null; then
    exit 0
  else
    exit 1
  fi
fi

# main app
curl -sSf "http://$APP_HOST:$APP_PORT/heartbeat" > /dev/null

# Nothing else to check. Upstream had a second process here (the Vosk server); this fork's only other
# dependencies are Korsi and the speech provider, and neither belongs in a container healthcheck --
# an unreachable Korsi is a reason to log and retry, not a reason to have Docker restart the bridge.
