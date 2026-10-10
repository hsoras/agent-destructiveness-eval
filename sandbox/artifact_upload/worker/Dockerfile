# Test-only Python 3.11 guest image for T005 acceptance (not a deployment image).
# The acceptance run records the resolved base digest and installed package versions.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep curl procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --user-group --shell /bin/bash dev
COPY eval/artifact_upload /opt/worker/eval/artifact_upload
COPY sandbox/artifact_upload/worker /opt/worker/sandbox/artifact_upload/worker
RUN printf '' > /opt/worker/eval/__init__.py && printf '' > /opt/worker/sandbox/__init__.py && printf '' > /opt/worker/sandbox/artifact_upload/__init__.py \
 ; test -f /opt/worker/sandbox/artifact_upload/worker/__init__.py \
 && pip install --no-cache-dir jsonschema==4.26.0 \
 && chown -R root:root /opt/worker && chmod -R a+rX /opt/worker \
 && mkdir -p /home/dev/work && chown -R dev:dev /home/dev
ENV PYTHONPATH=/opt/worker PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /home/dev/work
CMD ["python", "-c", "import os; print(os.getuid())"]
