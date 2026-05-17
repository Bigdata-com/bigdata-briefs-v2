FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apk update && apk add --no-cache \
    curl \
    ca-certificates \
    build-base \
    libffi-dev \
    openssl-dev \
    sqlite-dev \
    gcc \
    musl-dev \
    git

RUN adduser -D bigdata && \
    mkdir /code /data && \
    chown bigdata:bigdata /code /data

RUN find / -perm +6000 -type f -exec chmod a-s {} \; || true

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
RUN chmod +x /bin/uv

USER bigdata
WORKDIR /code

COPY --chown=bigdata:bigdata pyproject.toml uv.lock README.md LICENSE ./
COPY --chown=bigdata:bigdata bigdata_briefs ./bigdata_briefs
COPY --chown=bigdata:bigdata vendor ./vendor

RUN uv sync --no-dev

ENV DB_STRING="sqlite:////data/bigdata_briefs.db"

CMD ["uv", "run", "uvicorn", "bigdata_briefs.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
