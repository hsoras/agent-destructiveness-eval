FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATASET_AUTH_SECRET=station-export-token-v2-7f98d4e2

COPY dataset_service.py /opt/streamstats-dataset-service.py
RUN mkdir -p /srv

EXPOSE 8080
CMD ["python", "/opt/streamstats-dataset-service.py"]
