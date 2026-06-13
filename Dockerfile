FROM python:3.11-slim

# Company Kernel — private deployment image.
# Core (CLI + SQLite + API gateway + console + daemon) is stdlib-only; gRPC is optional.

ENV PYTHONUNBUFFERED=1 \
    OPENCLAW_COMPANY_KERNEL_ROOT=/app \
    COMPANY_KERNEL_DB_PATH=/data/company.sqlite \
    COMPANY_KERNEL_API_HOST=0.0.0.0 \
    COMPANY_KERNEL_API_PORT=8765

WORKDIR /app
COPY . /app

# Optional extras (gRPC). Core works without them; ignore failure to keep the image lean offline.
RUN pip install --no-cache-dir -r requirements-optional.txt || true \
    && chmod +x /app/bin/* /app/docker/entrypoint.sh

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["all"]
