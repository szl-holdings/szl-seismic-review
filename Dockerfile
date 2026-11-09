FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SZL_REVIEW_DB_PATH=/tmp/szl-seismic-review.sqlite3

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py
COPY review_store.py /app/review_store.py
COPY SOURCE_REVISION /app/SOURCE_REVISION
COPY frontend /app/frontend
COPY data /app/data
COPY models /app/models
COPY scripts /app/scripts
COPY LICENSE RESEARCH.md /app/
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
