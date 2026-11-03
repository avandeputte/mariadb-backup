# MariaDB Backup Manager -- the web UI plus the MariaDB client tools.
#
# The image does not contain a database server: it connects to yours over
# the network (or a mounted socket). The client comes from Debian, which
# carries a current MariaDB LTS; a client at least as new as the server is
# what mariadb-dump wants.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-flask \
        python3-requests \
        python3-waitress \
        mariadb-client \
        openssl \
        ca-certificates \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mariadb-backup-manager
COPY app.py VERSION ./
COPY mbm/ ./mbm/
COPY static/ ./static/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh && rm -rf mbm/__pycache__

ENV MBM_DATA_DIR=/data \
    MBM_DEFAULT_BACKUP_DIR=/backups \
    MBM_LISTEN=0.0.0.0 \
    MBM_PORT=8090 \
    PYTHONUNBUFFERED=1

EXPOSE 8090
VOLUME ["/data", "/backups"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MBM_PORT}/api/whoami" >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "/opt/mariadb-backup-manager/app.py"]
