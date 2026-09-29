FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN sed -i \
        -e 's|http://deb.debian.org/debian|http://mirrors.ustc.edu.cn/debian|g' \
        -e 's|http://deb.debian.org/debian-security|http://mirrors.ustc.edu.cn/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-xetex \
        texlive-lang-chinese \
        texlive-fonts-recommended \
        fonts-noto-core \
        fonts-noto-cjk \
        lmodern \
    && rm -rf /var/lib/apt/lists/*

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
