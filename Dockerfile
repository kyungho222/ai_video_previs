FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && pip check
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN useradd --uid 10001 --gid video --create-home video \
    && mkdir -p /app/ai_video/jutsu_auth/data \
    && chown video:video /app/ai_video/jutsu_auth/data \
    && mkdir -p /app/ai_video/runtime/archive /app/ai_video/runtime/recovered /app/ai_video/runtime/wan /app/ai_video/runtime/wan-inputs /app/ai_video/runtime/data \
    && chown -R video:video /app/ai_video/runtime
USER video
EXPOSE 9000
CMD ["uvicorn", "service_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9000", "--workers", "1"]
