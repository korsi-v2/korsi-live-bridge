# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Upstream builds Kaldi and the Vosk API from source on top of a CUDA devel image: an hour or more of
# compilation, several gigabytes of image, and a GPU runtime the customer has to have. All of it existed
# to run speech recognition inside this container.
#
# This bridge runs no model. It mixes a call's audio and forwards it to Soniox under a temporary key
# korsi-api mints per session, so what is left is a Python process holding two websockets. A slim base
# and wheels are enough, and the image builds in about a minute.
FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC

# `aiortc` and `av` ship manylinux wheels with their codec libraries bundled, so no compiler and no
# libopus/libvpx/ffmpeg packages are needed here. `git` is only for the supervisor pin in
# requirements.txt; `curl` and `procps` are used by the healthcheck and by AppAPI's FRP client.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        procps \
    && rm -rf /var/lib/apt/lists/*

# FRP client, so AppAPI can reach this container through HaRP without a Docker socket proxy.
RUN set -ex; \
    ARCH=$(uname -m); \
    if [ "$ARCH" = "aarch64" ]; then \
      FRP_URL="https://raw.githubusercontent.com/nextcloud/HaRP/main/exapps_dev/frp_0.61.1_linux_arm64.tar.gz"; \
    else \
      FRP_URL="https://raw.githubusercontent.com/nextcloud/HaRP/main/exapps_dev/frp_0.61.1_linux_amd64.tar.gz"; \
    fi; \
    echo "Downloading FRP client from $FRP_URL"; \
    curl -L "$FRP_URL" -o /tmp/frp.tar.gz; \
    tar -C /tmp -xzf /tmp/frp.tar.gz; \
    mv /tmp/frp_0.61.1_linux_* /tmp/frp; \
    cp /tmp/frp/frpc /usr/local/bin/frpc; \
    chmod +x /usr/local/bin/frpc; \
    rm -rf /tmp/frp /tmp/frp.tar.gz

RUN python3 -m venv /venv

COPY requirements.txt /
RUN --mount=type=cache,target=/root/.cache/pip \
    /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt && rm requirements.txt

# Add application files.
ADD /ex_app/cs[s] /ex_app/css
ADD /ex_app/im[g] /ex_app/img
ADD /ex_app/j[s] /ex_app/js
ADD /ex_app/l10[n] /ex_app/l10n
ADD /ex_app/li[b] /ex_app/lib

# Copy scripts with the proper permissions.
COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /
COPY --chmod=775 logger_config.yaml /
COPY --chmod=644 supervisord.conf /etc/supervisor/supervisord.conf

WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "/venv/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
HEALTHCHECK --interval=20s --timeout=2s --retries=300 CMD /healthcheck.sh
