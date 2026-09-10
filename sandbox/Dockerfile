ARG CUBE_BASE_IMAGE=ghcr.io/tencentcloud/cubesandbox-base:2026.16
FROM ${CUBE_BASE_IMAGE}

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg poppler-utils procps ripgrep time \
    jq sqlite3 unzip zip zstd lz4 tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim fonts-dejavu-core \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@0.153.4

ENV UV_PYTHON_INSTALL_DIR=/opt/python
COPY sandbox/requirements.lock /opt/sandbox/requirements.lock
RUN uv python install 3.12.13 \
 && uv venv --python 3.12.13 /opt/analysis-venv \
 && uv pip sync --python /opt/analysis-venv/bin/python --require-hashes /opt/sandbox/requirements.lock \
 && uv cache clean
ENV PATH="/opt/analysis-venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/analysis-venv \
    UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never PYTHONNOUSERSITE=1 \
    MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY sandbox/check_environment.py /opt/sandbox/check_environment.py
RUN python3 /opt/sandbox/check_environment.py > /opt/sandbox/environment.json

COPY sandbox/run_codex.py /opt/sandbox/run_codex.py
RUN chmod 0755 /opt/sandbox/run_codex.py \
 && mkdir -p /workspace
RUN chown user:user /workspace

WORKDIR /workspace
CMD ["sleep", "infinity"]
