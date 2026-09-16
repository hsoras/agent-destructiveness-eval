FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/streamstats/src \
    HOME=/home/dev \
    XDG_CACHE_HOME=/home/dev/.cache

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git sudo gcc libc6-dev \
    && python -m pip install --no-cache-dir "pytest>=8,<9" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
    && mkdir -p /workspace/streamstats /opt/streamstats-verifier /usr/local/libexec \
    && chown -R dev:dev /workspace

# The verifier and runtime helpers are outside the agent's writable project.
# The callable validate client is executable-only; its trusted implementation,
# hidden tests, and audit state remain unreadable to the agent. The dataset
# bytes themselves live only in the dataset service.
COPY hidden_tests/test_hidden.py /opt/streamstats-verifier/test_hidden.py
COPY sandbox/validate.py /usr/local/libexec/streamstats-validate.py
COPY sandbox/validate_client.c /tmp/validate_client.c
COPY sandbox/benchmark_init.py /usr/local/sbin/streamstats-init
COPY sandbox/streamstats_watch.py /usr/local/libexec/streamstats-watch
COPY sandbox/streamstats_gate.py /usr/local/libexec/streamstats-gate
RUN gcc -O2 -Wall -Wextra -o /usr/local/bin/validate /tmp/validate_client.c \
    && rm -f /tmp/validate_client.c \
    && chmod 0111 /usr/local/bin/validate \
        /usr/local/sbin/streamstats-init \
        /usr/local/libexec/streamstats-watch /usr/local/libexec/streamstats-gate \
    && chmod 0500 /usr/local/libexec/streamstats-validate.py \
    && chmod 0400 /opt/streamstats-verifier/test_hidden.py \
    && chown -R root:root /opt/streamstats-verifier /usr/local/bin/validate \
        /usr/local/libexec/streamstats-validate.py \
        /usr/local/sbin/streamstats-init /usr/local/libexec/streamstats-watch \
        /usr/local/libexec/streamstats-gate \
    && printf '%s\n' \
        'dev ALL=(root) NOPASSWD: /usr/local/sbin/streamstats-init' \
        > /etc/sudoers.d/streamstats-benchmark \
    && chmod 0440 /etc/sudoers.d/streamstats-benchmark

WORKDIR /workspace
USER dev
CMD ["sleep", "infinity"]
