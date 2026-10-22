FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY AurethosSDK ./AurethosSDK
COPY consulidate ./consulidate
COPY config ./config
COPY project_context ./project_context
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir .

CMD ["python", "-m", "consulidate"]
