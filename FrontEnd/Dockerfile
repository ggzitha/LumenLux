# High-performance Python container for LumenLux-Zero Frontend & OCR Studio
FROM python:3.11-slim

# Avoid writing .pyc files and enable unbuffered output for real-time logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies:
# - curl: container healthcheck
# - tesseract-ocr, tesseract-ocr-eng, tesseract-ocr-ind: OCR engine & trained models
# - libgl1, libglib2.0-0: image and vision processing libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ind \
    libtesseract-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure persistent volume directories exist inside the container
RUN mkdir -p /app/data/images /app/data/models /app/data/tessdata

# Copy application source code
COPY . .

# Expose web application port and WebRTC streaming port
EXPOSE 8080 8557/tcp 8557/udp

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/login || exit 1

# Launch production server with Uvicorn (timeout-graceful-shutdown=0 for instant exit)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--timeout-graceful-shutdown", "0"]
