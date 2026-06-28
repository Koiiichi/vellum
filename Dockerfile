FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    playwright==1.60.0 \
    fastapi==0.138.1 \
    uvicorn==0.49.0 \
    openai==2.44.0 \
    pydantic==2.13.4 \
    python-dotenv==1.2.2 \
    "google-api-python-client==2.198.0" \
    "google-auth-oauthlib==1.4.0" \
    "google-cloud-storage==3.12.0"

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium --with-deps

RUN groupadd --gid 1001 vellum && \
    useradd --uid 1001 --gid vellum --no-create-home vellum

COPY . .

RUN chown -R vellum:vellum /app

USER vellum

ENV SCRAPER_HEADLESS=true
ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["python", "main.py"]
