FROM python:3.11-slim

# libreoffice: PDF conversion
# tesseract-ocr: Tesseract engine used by img2table for table cell text extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice tesseract-ocr && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=300 -r requirements.txt
# Download openwakeword resource models (melspectrogram.onnx, embedding_model.onnx, etc.)
# These are not bundled in the pip package and must be fetched once at build time.
RUN python -c "from openwakeword.utils import download_models; download_models()"

COPY app ./app
COPY scripts ./scripts
COPY smart_clerk_test_ui.html ./smart_clerk_test_ui.html

ENV PYTHONPATH=/app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
