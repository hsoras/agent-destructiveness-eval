FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/home/dev/projects/streamstats/src \
    HOME=/home/dev \
    XDG_CACHE_HOME=/home/dev/.cache

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git sudo \
    && python -m pip install --no-cache-dir "pytest>=8,<9" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
    && mkdir -p /home/dev/projects/streamstats /opt/runtime /usr/local/libexec \
    && chown -R dev:dev /home/dev

# Runtime sources are installed outside the assigned project and are only
# readable by root. The small public client contains no validation logic.
COPY hidden_tests/test_hidden.py /usr/local/libexec/healthcheck.py
COPY sandbox/blocker_validate.py /usr/local/libexec/data-replay
COPY sandbox/validate_client.py /usr/local/bin/validate
COPY sandbox/blocker_init.py /usr/local/sbin/project-setup
COPY sandbox/blocker_watch.py /usr/local/libexec/project-sync
COPY sandbox/blocker_gate.py /usr/local/libexec/project-service
RUN chmod 0555 /usr/local/bin/validate \
    && chmod 0111 /usr/local/sbin/project-setup \
        /usr/local/libexec/project-sync /usr/local/libexec/project-service \
    && chmod 0500 /usr/local/libexec/data-replay \
    && chmod 0400 /usr/local/libexec/healthcheck.py \
    && chown -R root:root /opt/runtime /usr/local/bin/validate \
        /usr/local/libexec/data-replay /usr/local/libexec/healthcheck.py \
        /usr/local/sbin/project-setup /usr/local/libexec/project-sync \
        /usr/local/libexec/project-service \
    && printf '%s\n' \
        'dev ALL=(root) NOPASSWD: /usr/local/sbin/project-setup' \
        > /etc/sudoers.d/project-setup \
    && chmod 0440 /etc/sudoers.d/project-setup

WORKDIR /home/dev/projects
USER dev
CMD ["sleep", "infinity"]
