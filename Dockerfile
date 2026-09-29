FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
ARG PIP_PRIMARY_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_FALLBACK_INDEX_URL=https://pypi.org/simple
RUN (pip install --no-cache-dir --index-url "$PIP_PRIMARY_INDEX_URL" -r requirements.txt \
        || pip install --no-cache-dir --index-url "$PIP_FALLBACK_INDEX_URL" -r requirements.txt) \
    && addgroup --system --gid 10001 nota \
    && adduser --system --uid 10001 --ingroup nota --home /app nota

COPY object ./object
COPY deploy/maintenance-loop.sh ./deploy/maintenance-loop.sh
RUN mkdir -p /data && chown -R nota:nota /app /data

USER nota
EXPOSE 7860
CMD ["python", "object/server.py"]
