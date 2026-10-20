# LifeContext image: the API listens on 127.0.0.1 inside the container and no
# port is published; use `docker exec` or bin/lifecontext-docker. Build from a
# checkout with `docker build -t lifecontext .` or `docker compose build`.
# See docs/install.md. Source-available under the Business Source License 1.1.

# Debian 12: pg0, the embedded Postgres, is built on it and documents it.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

FROM ${PYTHON_IMAGE} AS wheel
WORKDIR /src
COPY pyproject.toml LICENSE NOTICE ./
COPY src ./src
RUN python -m pip install --no-cache-dir --disable-pip-version-check build==1.6.1 \
    && python -m build --wheel --outdir /dist .

FROM ${PYTHON_IMAGE}
# The runtime user. A bind-mounted vault must belong to this uid; rebuild with
# --build-arg LIFECONTEXT_UID=$(id -u) to match your host account.
ARG LIFECONTEXT_UID=1000
ARG LIFECONTEXT_GID=1000

LABEL org.opencontainers.image.title="LifeContext" \
      org.opencontainers.image.description="Your private, durable context layer for AI" \
      org.opencontainers.image.source="https://github.com/Hantok/lifecontext" \
      org.opencontainers.image.licenses="BUSL-1.1"

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data/home \
    HF_HOME=/opt/lifecontext/huggingface \
    PATH=/opt/lifecontext/venv/bin:$PATH

# Tesseract with English, Russian and Ukrainian; tini as PID 1; iproute2 (ss)
# lets Hindsight find its daemon when the container stops; the embedded
# Postgres (pg0) needs Kerberos GSSAPI, readline, the time zone data, and ps
# (procps), with which it tells a live postmaster from a stale pid file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr tini iproute2 \
        libgssapi-krb5-2 libreadline8 tzdata procps \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${LIFECONTEXT_GID}" lifecontext \
    && useradd --uid "${LIFECONTEXT_UID}" --gid lifecontext --home-dir /data/home \
        --no-create-home --shell /usr/sbin/nologin lifecontext \
    && install -d -o lifecontext -g lifecontext -m 0700 /data/home /data/vault

# The dependencies from the hashed lock for this architecture (CPU-only PyTorch),
# then the package; owned by root so the service cannot change them.
ARG TARGETARCH
COPY requirements/linux-x86_64.txt requirements/linux-aarch64.txt /opt/lifecontext/requirements/
COPY --from=wheel /dist/ /tmp/dist/
RUN case "${TARGETARCH:-amd64}" in \
        amd64) lock=linux-x86_64 ;; \
        arm64) lock=linux-aarch64 ;; \
        *) echo "no dependency lock for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && python -m venv /opt/lifecontext/venv \
    && /opt/lifecontext/venv/bin/pip install --require-hashes --no-deps \
        -r "/opt/lifecontext/requirements/$lock.txt" \
    && /opt/lifecontext/venv/bin/pip install --no-deps /tmp/dist/lifecontext-*.whl \
    && /opt/lifecontext/venv/bin/pip check \
    && rm -rf /tmp/dist \
    && /opt/lifecontext/venv/bin/python -I -c 'import torch; assert torch.__version__.endswith("+cpu"), torch.__version__'

# The embedding and rerank models Hindsight loads, at fixed revisions (keep in step
# with install.sh), and the tokenizer file, fetched now so the container starts
# with HF_HUB_OFFLINE=1 and no downloads. Hindsight loads the "main" reference,
# which is pointed at the fixed revision. The service user owns the cache, as it
# would in a home directory.
ARG EMBEDDING_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
ARG RERANKER_REVISION=4ceabf2d1e212e16da0d1fb94d5dea66a9a1cca0
RUN install -d -o lifecontext -g lifecontext -m 0755 /opt/lifecontext/huggingface /opt/lifecontext/tiktoken
USER lifecontext
RUN HOME=/tmp/seed TIKTOKEN_CACHE_DIR=/opt/lifecontext/tiktoken /opt/lifecontext/venv/bin/python -I - \
        "$EMBEDDING_REVISION" "$RERANKER_REVISION" <<'PY' && rm -rf /tmp/seed
import sys
from pathlib import Path

import tiktoken
from huggingface_hub import constants, snapshot_download

from lifecontext.hindsight import EMBEDDING_MODEL, RERANKER_MODEL

models = ((EMBEDDING_MODEL, sys.argv[1], ["*.json", "*.txt", "model.safetensors"]),
          (RERANKER_MODEL, sys.argv[2], ["*.json", "*.model", "pytorch_model.bin"]))
for repo, revision, patterns in models:
    snapshot_download(repo, revision=revision, allow_patterns=patterns)
    main = Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "refs" / "main"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(revision)
    print(repo, "at", revision)
tiktoken.get_encoding("cl100k_base")
PY
# Prove the offline start: both models load from the cache alone, as the service user.
RUN HOME=/tmp/seed HF_HUB_OFFLINE=1 /opt/lifecontext/venv/bin/python -I - <<'PY' && rm -rf /tmp/seed
from pathlib import Path

from sentence_transformers import CrossEncoder, SentenceTransformer

from lifecontext.hindsight import EMBEDDING_MODEL, RERANKER_MODEL, model_cache_status
from lifecontext.paths import LifeContextPaths

SentenceTransformer(EMBEDDING_MODEL, device="cpu")
CrossEncoder(RERANKER_MODEL, device="cpu")
status = model_cache_status(LifeContextPaths(Path("/data/home")))
assert status["complete"], status
print("offline load ok:", sorted(item["model"] for item in status["models"].values()))
PY
# Prove the embedded Postgres runs here: pg0 reports an instance as running only
# when psql can query it. On failure, list the libraries its binaries miss.
RUN HOME=/tmp/seed /opt/lifecontext/venv/bin/python -I - <<'PY' && rm -rf /tmp/seed
import glob
import os
import subprocess

from pg0 import Pg0

instance = Pg0(name="image-check")
info = instance.start()
if not (info.running and info.uri):
    libraries = ":".join(glob.glob(os.path.expanduser("~/.pg0/installation/*/lib")))
    for binary in sorted(glob.glob(os.path.expanduser("~/.pg0/installation/*/bin/*"))):
        listing = subprocess.run(["ldd", binary], capture_output=True, text=True, check=False,
                                 env={**os.environ, "LD_LIBRARY_PATH": libraries}).stdout
        missing = [line.split()[0] for line in listing.splitlines() if "not found" in line]
        if missing:
            print(os.path.basename(binary), "misses", missing)
    raise SystemExit("pg0 started Postgres but cannot query it")
instance.drop()
print("embedded Postgres ok:", info.version)
PY
USER root
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/lifecontext-entrypoint

ENV HF_HUB_OFFLINE=1
USER lifecontext
WORKDIR /data/home
VOLUME ["/data/home", "/data/vault"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --start-interval=5s --retries=3 \
    CMD ["/opt/lifecontext/venv/bin/python", "-I", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lifecontext-entrypoint"]
CMD ["serve"]
