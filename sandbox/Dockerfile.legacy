FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/home/dev/streamstats/src

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && python -m pip install --no-cache-dir "pytest>=8,<9" \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/dev/streamstats
WORKDIR /home/dev/streamstats

CMD ["sleep", "infinity"]
