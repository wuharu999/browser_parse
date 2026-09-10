# Only supplies QEMU tools for the local Cube development VM.
# The container is not the agent sandbox and receives no model credentials.
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils curl ca-certificates openssh-client python3 ripgrep util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /cube-dev
USER 1000:1000
