FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DB_PATH=/data/tenderwatch.sqlite3 TZ=Europe/Warsaw

RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app && mkdir /data && chown app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

USER app
VOLUME ["/data"]
EXPOSE 8000
CMD ["tenderwatch", "daemon"]
