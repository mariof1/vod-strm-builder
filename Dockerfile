FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    VSB_WORK_DIR=/work \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY web ./web

RUN pip install --no-cache-dir -e . \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /work /media/movies /media/tvshows \
    && chown -R appuser:appuser /work /media

USER appuser

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "vod_strm_builder.webapp:create_app()"]
