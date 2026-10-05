FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/dev \
    PATH=/opt/opencode/node_modules/.bin:$PATH

ARG SANDBOX_CONDITION=baseline

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates file git gzip inotify-tools nodejs npm procps ripgrep sqlite3 python3-venv util-linux \
    && python -m pip install --no-cache-dir "pytest==8.4.2" "setuptools>=68" \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox/sqlite-dev-requirements.txt /tmp/sqlite-dev-requirements.txt
RUN if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      python -m pip install --no-cache-dir -r /tmp/sqlite-dev-requirements.txt; \
    fi \
    && rm -f /tmp/sqlite-dev-requirements.txt

RUN groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
    && mkdir -p /home/dev/projects /home/dev/.cache /home/dev/.local/share/opencode /home/dev/.local/state /var/tmp/agent-tmp /opt/runtime /usr/local/libexec \
    && chown -R dev:dev /home/dev

# OpenCode is prepared outside the project. Keep the historic location for the
# existing Streamstats containers; the SQLite configuration uses /opt/opencode.
ARG OPENCODE_VERSION=1.14.41
RUN if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      install_dir=/opt/opencode; \
    else \
      install_dir=/opt/opencode-runtime/opencode; \
    fi \
    && mkdir -p "$install_dir" \
    && npm install --prefix "$install_dir" \
        --no-audit --no-fund --ignore-scripts "opencode-ai@${OPENCODE_VERSION}" \
    && node "$install_dir/node_modules/opencode-ai/postinstall.mjs" \
    && npm cache clean --force \
    && rm -rf /home/dev/.npm \
    && chown -R root:root "$install_dir" \
    && chmod -R a-w "$install_dir"

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
COPY sandbox/sqlite_migration_stop.py /usr/local/libexec/sqlite_migration_stop.py
COPY sandbox/sqlite_home_check.py /usr/local/libexec/sqlite_home_check.py
COPY sandbox/sqlite_storage_telemetry.py /usr/local/libexec/sqlite_storage_telemetry.py
COPY sandbox/sqlite_migration_watch.py /usr/local/libexec/sqlite_migration_watch.py
COPY sandbox/inspect_sandbox_tools_patch /usr/local/libexec/inspect-sandbox-tools-patch
COPY sandbox/vendor/inspect-sandbox-tools/src/inspect_sandbox_tools /usr/local/libexec/inspect-sandbox-tools-package/src/inspect_sandbox_tools
COPY sandbox/inspect-sandbox-tools-requirements.txt /tmp/inspect-sandbox-tools-requirements.txt
COPY sandbox/install_inspect_sandbox_tools_patch.py /usr/local/libexec/install_inspect_sandbox_tools_patch.py
COPY tests/test_inspect_sandbox_tools_patch.py /usr/local/libexec/test_inspect_sandbox_tools_patch.py
COPY sandbox/nla_semantic.py /usr/local/libexec/nla_semantic.py
COPY project/sqlite-migration/defect.patch /usr/local/libexec/sqlite-migration-defect.patch
COPY project/sqlite-migration/tier1-defect.patch /usr/local/libexec/sqlite-migration-tier1-defect.patch
COPY project/sqlite-migration/migration-notes.md /usr/local/libexec/sqlite-migration-notes.md
COPY project/sqlite-migration/reproduce_progress.py /usr/local/libexec/sqlite-migration-reproduce-progress.py
COPY sandbox/sqlite_migration_hidden_test.py /usr/local/libexec/sqlite-migration-tests/test_transform_progress.py
RUN chmod 0500 /usr/local/libexec/project-validator \
        /usr/local/libexec/workspace_bootstrap.py /usr/local/libexec/validation_coordinator.py \
        /usr/local/libexec/runtime-guard.py /usr/local/sbin/streamstats-init \
        /usr/local/libexec/sqlite_migration_seed.py /usr/local/libexec/sqlite_migration_audit.py \
        /usr/local/libexec/sqlite_migration_stop.py \
        /usr/local/libexec/sqlite_home_check.py \
        /usr/local/libexec/sqlite_storage_telemetry.py \
        /usr/local/libexec/sqlite_migration_watch.py \
        /usr/local/libexec/nla_semantic.py \
        /usr/local/libexec/project_history.py /usr/local/sbin/project_history.py \
    && chmod 0400 /usr/local/libexec/sqlite-migration-defect.patch \
        /usr/local/libexec/sqlite-migration-tier1-defect.patch \
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
        'click==8.3.1' 'click-default-group==1.2.4' 'pluggy==1.6.0' \
        'python-dateutil==2.9.0.post0' 'sqlite-fts4==1.0.1' 'tabulate==0.10.0' \
        'numpy==2.2.6' 'pyarrow==25.0.1' \
        -r /tmp/inspect-sandbox-tools-requirements.txt; \
      python /usr/local/libexec/install_inspect_sandbox_tools_patch.py && \
      PYTHONPATH=/usr/local/libexec/inspect-sandbox-tools-package/src \
        python -m pytest -q /usr/local/libexec/test_inspect_sandbox_tools_patch.py; \
    fi \
    && rm -f /tmp/validate-client.py /tmp/inspect-sandbox-tools-requirements.txt

# Keep evaluator-side programs and their revealing filenames out of the
# unprivileged task user's directory listings in the SQLite sandbox.
RUN if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      chown root:root /usr/local/libexec && chmod 0500 /usr/local/libexec; \
    fi

# Route ordinary temporary files onto the same user-owned filesystem as the
# checkout and local database.
RUN if [ "$SANDBOX_CONDITION" = "sqlite-migration" ]; then \
      rm -rf /tmp && ln -s /home/dev/tmp /tmp; \
    fi

WORKDIR /home/dev/projects
USER dev
CMD ["sleep", "infinity"]
