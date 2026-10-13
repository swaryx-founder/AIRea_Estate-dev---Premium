FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

# Raw image: no app secrets. Runtime config comes from Container App env (Azure DevOps Library).
# BUILD_ID is set at deploy time via sync_aca_env.py — not baked at docker build.
ENV CONFIG_FROM_ENV_ONLY=true
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f "http://localhost:${PORT}/api/health" || exit 1

CMD ["sh", "start.sh"]
