FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
COPY mock_provider ./mock_provider
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN pip install --no-cache-dir . && chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
