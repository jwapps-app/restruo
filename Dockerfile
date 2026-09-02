FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The app never needs root: it talks to Portainer over HTTP and writes one
# directory. Dropping privileges limits what a bug in it, or in a dependency,
# could reach.
RUN groupadd --gid 1000 restruo \
 && useradd --uid 1000 --gid restruo --no-create-home --shell /usr/sbin/nologin restruo

COPY app/ ./app/
COPY web/ ./web/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 0755 /app/docker-entrypoint.sh && chown -R restruo:restruo /app

ARG GIT_SHA=dev
ENV RESTRUO_VERSION=$GIT_SHA

EXPOSE 8080
ENV CONFIG_PATH=/config/config.yaml

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
