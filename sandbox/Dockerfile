ARG CUBE_BASE_IMAGE=ghcr.io/tencentcloud/cubesandbox-base:2026.16
FROM ${CUBE_BASE_IMAGE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates curl gnupg poppler-utils procps \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@0.153.4

COPY sandbox/run_codex.py /opt/sandbox/run_codex.py
RUN chmod 0755 /opt/sandbox/run_codex.py \
 && mkdir -p /workspace

WORKDIR /workspace
CMD ["sleep", "infinity"]
