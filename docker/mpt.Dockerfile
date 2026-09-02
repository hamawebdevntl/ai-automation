# MoneyPrinterTurbo, built from our fork at services/mpt.
#
# Build context is the submodule: docker build -f docker/mpt.Dockerfile services/mpt
#
# Upstream's own Dockerfile launches the Streamlit WebUI, which is not what the
# pipeline drives -- we need the REST API in main.py. Our fork's entrypoint
# renders config.toml from the environment and Secrets Manager first, because
# MoneyPrinterTurbo reads all configuration from that file at import time and
# honours only two environment overrides in its entire codebase.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg imagemagick fonts-dejavu-core curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ImageMagick's default policy forbids reading/writing the formats MoviePy uses
# for text overlays, which surfaces as an opaque render failure rather than a
# permissions error.
RUN if [ -f /etc/ImageMagick-6/policy.xml ]; then \
      sed -i 's/rights="none"/rights="read|write"/g' /etc/ImageMagick-6/policy.xml; \
    fi

WORKDIR /MoneyPrinterTurbo

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt \
 && python -m pip install --no-cache-dir boto3

COPY . .

# Renders, the stock-footage cache and task state all live here. Mounted from
# EFS in production: MoneyPrinterTurbo has no object-storage support at all, so
# on ephemeral storage a task replacement loses every in-flight render.
VOLUME ["/MoneyPrinterTurbo/storage"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/ping || exit 1

ENTRYPOINT ["sh", "scripts/docker-entrypoint.sh"]
