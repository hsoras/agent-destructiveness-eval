FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATASET_AUTH_SECRET=streamstats-benchmark-dataset-secret-v1-7f98d4e2

COPY dataset_service.py /opt/streamstats-dataset-service.py
RUN mkdir -p /srv

EXPOSE 8080
CMD ["python", "/opt/streamstats-dataset-service.py"]
