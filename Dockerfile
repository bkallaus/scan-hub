# FF-680W ScanHub
# Ubuntu because Epson builds and tests its Linux driver there. If apt reports
# unmet dependencies for the Epson .debs, try: --build-arg BASE_IMAGE=ubuntu:24.04
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

# Epson Scan 2 bundle (core driver + closed-source network plugin), x86_64 .deb.
# Either drop epsonscan2-bundle-*.x86_64.deb.tar.gz into ./vendor/ before
# building, or let the build download it from this URL.
ARG EPSON_BUNDLE_URL=https://download-center.epson.com/f/module/ee3a3a45-95aa-4c80-b14d-ad4cff5f7428/epsonscan2-bundle-6.7.90.0.x86_64.deb.tar.gz
# Set to false for a smaller image without searchable-PDF (OCR) support.
ARG INSTALL_OCR=true

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    DATA_DIR=/data \
    WEB_PORT=8680

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sane-utils python3 python3-venv curl ca-certificates tzdata \
 && if [ "$INSTALL_OCR" = "true" ]; then \
      apt-get install -y --no-install-recommends ocrmypdf tesseract-ocr tesseract-ocr-eng ; fi \
 && rm -rf /var/lib/apt/lists/*

COPY vendor/ /tmp/vendor/
# After installing the driver, dll.conf is fully commented out so SANE only loads
# backends from /etc/sane.d/dll.d (epsonscan2). Discovery is faster and the
# generic epson2 backend can't grab the scanner first.
RUN set -eux; \
    bundle="$(ls /tmp/vendor/epsonscan2-bundle-*x86_64.deb.tar.gz 2>/dev/null | head -n1 || true)"; \
    if [ -z "$bundle" ]; then \
      curl -fL --retry 3 -o /tmp/epsonscan2-bundle.tar.gz "$EPSON_BUNDLE_URL"; \
      bundle=/tmp/epsonscan2-bundle.tar.gz; \
    fi; \
    mkdir /tmp/es2; tar -xzf "$bundle" -C /tmp/es2 --strip-components=1; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/es2/core/*.deb /tmp/es2/plugins/*.deb; \
    rm -rf /tmp/es2 /tmp/vendor /tmp/epsonscan2-bundle.tar.gz /var/lib/apt/lists/*; \
    sed -i 's/^\([^#]\)/#\1/' /etc/sane.d/dll.conf; \
    if ! grep -qsx 'epsonscan2' /etc/sane.d/dll.d/* ; then echo epsonscan2 >> /etc/sane.d/dll.conf; fi; \
    find /usr/lib -name 'libsane-epsonscan2.so*' | grep -q . || \
      { echo "epsonscan2 SANE backend not found after install" >&2; exit 1; }

# App packages live in a virtualenv so they can't clash with the system
# Python packages ocrmypdf depends on.
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

WORKDIR /app
VOLUME ["/data"]
EXPOSE 8680
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEB_PORT\",\"8680\")}/healthz', timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["/opt/venv/bin/python", "-m", "app.main"]
