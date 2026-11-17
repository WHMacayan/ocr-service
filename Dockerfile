FROM python:3.12-slim

# Tesseract and poppler: OCR fallback for scanned files, as in the Odoo module.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home ocr
WORKDIR /srv/ocr-service
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# The volume mounted here holds the SQLite file, secret.key and the source files.
RUN mkdir /data && chown ocr /data
USER ocr

ENV PYTHONUNBUFFERED=1
# One thread per Tesseract: the service already scans several files at once (ai_parallel), and
# Tesseract's default of one thread per core makes those scans spin against each other for hours.
ENV OMP_THREAD_LIMIT=1
CMD ["flask", "--app", "app", "worker"]
