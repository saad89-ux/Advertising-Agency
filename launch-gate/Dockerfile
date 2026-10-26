# Launch Gate backend (API + worker) for Render.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1) Dependencies first so Docker caches this layer when only code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Chromium + its system libraries: the worker opens landing pages to verify them.
#    Set INSTALL_BROWSER=false to skip (smaller image, but page checks will not work).
ARG INSTALL_BROWSER=true
RUN mkdir -p /ms-playwright && \
    if [ "$INSTALL_BROWSER" = "true" ]; then \
      playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*; \
    fi

# 3) Application code (see .dockerignore for what is excluded).
COPY . .

# 4) Never run as root inside the container.
RUN useradd --create-home --uid 1000 app && \
    chown -R app:app /app && \
    chmod -R a+rX /ms-playwright
USER app

EXPOSE 8000
CMD ["sh", "deploy/start.sh"]
