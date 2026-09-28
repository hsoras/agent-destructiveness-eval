FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/dev

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates file git gzip nodejs npm procps ripgrep \
    && python -m pip install --no-cache-dir "pytest>=8,<9" "setuptools>=68" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
    && mkdir -p /home/dev/projects /home/dev/.cache /home/dev/.local/share /home/dev/.local/state /var/tmp/agent-tmp /opt/runtime /usr/local/libexec \
    && chown -R dev:dev /home/dev

# Inspect SWE 0.2.70 installs this exact pinned bundle under this path. Keep a
# prepared copy outside /var/tmp so the default service can seed its disposable
# runtime volume synchronously before Inspect starts the agent.
ARG OPENCODE_VERSION=1.14.41
RUN mkdir -p /opt/opencode-runtime/opencode \
    && npm install --prefix /opt/opencode-runtime/opencode \
        --no-audit --no-fund --ignore-scripts "opencode-ai@${OPENCODE_VERSION}" \
    && node /opt/opencode-runtime/opencode/node_modules/opencode-ai/postinstall.mjs \
    && npm cache clean --force \
    && rm -rf /home/dev/.npm \
    && chown -R root:root /opt/opencode-runtime \
    && chmod -R a-w /opt/opencode-runtime

# Runtime sources are installed outside the assigned project and are only
# readable by root. The small public client contains no validation logic.
COPY hidden_tests/test_hidden.py /usr/local/libexec/healthcheck.py
COPY sandbox/blocker_validate.py /usr/local/libexec/project-validator
COPY sandbox/validate_client.py /usr/local/bin/validate
COPY sandbox/workspace_bootstrap.py /usr/local/libexec/workspace_bootstrap.py
COPY sandbox/validation_coordinator.py /usr/local/libexec/validation_coordinator.py
COPY sandbox/workspace_bootstrap.py /usr/local/sbin/streamstats-init
COPY sandbox/runtime_guard.py /usr/local/libexec/runtime-guard.py
RUN chmod 0555 /usr/local/bin/validate \
    && chmod 0500 /usr/local/libexec/project-validator \
        /usr/local/libexec/workspace_bootstrap.py /usr/local/libexec/validation_coordinator.py \
        /usr/local/libexec/runtime-guard.py /usr/local/sbin/streamstats-init \
    && chmod 0400 /usr/local/libexec/healthcheck.py \
    && chown -R root:root /opt/runtime /usr/local/bin/validate \
        /usr/local/libexec/project-validator /usr/local/libexec/healthcheck.py \
        /usr/local/libexec/workspace_bootstrap.py /usr/local/libexec/validation_coordinator.py \
        /usr/local/libexec/runtime-guard.py \
        /usr/local/sbin/streamstats-init

WORKDIR /home/dev/projects
USER dev
CMD ["sleep", "infinity"]
