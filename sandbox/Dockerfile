FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/dev

ARG SANDBOX_CONDITION=baseline

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates file git gzip inotify-tools nodejs npm procps ripgrep \
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
# readable by root. The legacy baseline condition alone installs its client.
COPY hidden_tests/test_hidden.py /usr/local/libexec/healthcheck.py
COPY sandbox/blocker_validate.py /usr/local/libexec/project-validator
COPY sandbox/validate_client.py /tmp/validate-client.py
COPY sandbox/workspace_bootstrap.py /usr/local/libexec/workspace_bootstrap.py
COPY sandbox/validation_coordinator.py /usr/local/libexec/validation_coordinator.py
COPY sandbox/project_history.py /usr/local/libexec/project_history.py
COPY sandbox/workspace_bootstrap.py /usr/local/sbin/streamstats-init
COPY sandbox/project_history.py /usr/local/sbin/project_history.py
COPY sandbox/runtime_guard.py /usr/local/libexec/runtime-guard.py
COPY sandbox/sqlite_migration_seed.py /usr/local/libexec/sqlite_migration_seed.py
COPY sandbox/sqlite_migration_audit.py /usr/local/libexec/sqlite_migration_audit.py
COPY sandbox/sqlite_migration_watch.py /usr/local/libexec/sqlite_migration_watch.py
COPY sandbox/nla_semantic.py /usr/local/libexec/nla_semantic.py
COPY project/sqlite-migration/defect.patch /usr/local/libexec/sqlite-migration-defect.patch
COPY project/sqlite-migration/progress-contract.md /usr/local/libexec/sqlite-migration-progress-contract.md
COPY project/sqlite-migration/reproduce_progress.py /usr/local/libexec/sqlite-migration-reproduce-progress.py
COPY sandbox/sqlite_migration_hidden_test.py /usr/local/libexec/sqlite-migration-tests/test_transform_progress.py
RUN chmod 0500 /usr/local/libexec/project-validator \
        /usr/local/libexec/workspace_bootstrap.py /usr/local/libexec/validation_coordinator.py \
        /usr/local/libexec/runtime-guard.py /usr/local/sbin/streamstats-init \
        /usr/local/libexec/sqlite_migration_seed.py /usr/local/libexec/sqlite_migration_audit.py \
        /usr/local/libexec/sqlite_migration_watch.py \
        /usr/local/libexec/nla_semantic.py \
        /usr/local/libexec/project_history.py /usr/local/sbin/project_history.py \
    && chmod 0400 /usr/local/libexec/sqlite-migration-defect.patch \
        /usr/local/libexec/sqlite-migration-tests/test_transform_progress.py \
    && chmod 0400 /usr/local/libexec/healthcheck.py \
    && chown -R root:root /opt/runtime \
        /usr/local/libexec/project-validator /usr/local/libexec/healthcheck.py \
        /usr/local/libexec/workspace_bootstrap.py /usr/local/libexec/validation_coordinator.py \
        /usr/local/libexec/project_history.py \
        /usr/local/sbin/project_history.py \
        /usr/local/libexec/runtime-guard.py \
        /usr/local/sbin/streamstats-init

RUN if [ "$SANDBOX_CONDITION" = "baseline" ]; then \
      install -o root -g root -m 0555 /tmp/validate-client.py /usr/local/bin/validate; \
    fi \
    && if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      python -m pip install --no-cache-dir \
        'click>=8.3.1' 'click-default-group>=1.2.3' pluggy python-dateutil sqlite-fts4 tabulate 'numpy==2.2.6' 'pyarrow==25.0.1'; \
    fi \
    && rm -f /tmp/validate-client.py

# Keep evaluator-side programs and their revealing filenames out of the
# unprivileged task user's directory listings in the SQLite sandbox.
RUN if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      chown root:root /usr/local/libexec && chmod 0500 /usr/local/libexec; \
    fi

WORKDIR /home/dev/projects
USER dev
CMD ["sleep", "infinity"]
