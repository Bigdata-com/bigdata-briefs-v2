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
    git \
    coreutils

RUN curl -sSL https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 \
    -o /usr/local/bin/supercronic && chmod +x /usr/local/bin/supercronic

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
COPY --chown=bigdata:bigdata crontab ./crontab
COPY --chown=bigdata:bigdata start.sh ./start.sh
COPY --chown=bigdata:bigdata run_daily.sh ./run_daily.sh

# Strip any CR (\r) so scripts checked out on Windows (CRLF) still exec in the Linux container
RUN sed -i 's/\r$//' /code/start.sh /code/run_daily.sh /code/crontab \
    && chmod +x /code/start.sh /code/run_daily.sh

RUN uv sync --no-dev

ENV DB_STRING="sqlite:////data/bigdata_briefs.db"

CMD ["/code/start.sh"]
