# Container image for the voice shopping evaluation monitor.
# Runs as a non-root user. The monitor refuses to listen on a public address without API keys, so create
# keys (see README) before starting the container. Put a TLS proxy in front (see deploy/Caddyfile).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1     VOICESHOP_DB=/data/voiceshop.db     VOICESHOP_KEYS_FILE=/data/api_keys.json

RUN useradd --system --uid 10001 --home-dir /app voiceshop
WORKDIR /app

# The package reads its catalogue and scenarios from the checkout, so the source stays in /app.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY data ./data
COPY scenarios ./scenarios
RUN pip install --no-cache-dir -e . && mkdir -p /data && chown voiceshop /data

USER voiceshop
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3     CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=4).status == 200 else 1)"

CMD ["voiceshop", "serve", "--host", "0.0.0.0", "--port", "8765"]
