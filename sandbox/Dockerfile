FROM python:3.12-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg poppler-utils procps ripgrep time \
    jq sqlite3 unzip zip zstd lz4 tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim fonts-dejavu-core \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@0.153.4 \
 && pip install --no-cache-dir uv==0.11.29

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
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin analysis \
 && mkdir -p /workspace \
 && chown analysis:analysis /workspace

USER analysis
WORKDIR /workspace
CMD ["sleep", "infinity"]
