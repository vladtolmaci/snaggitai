FROM python:3.12-slim

# PDF generation deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libglib2.0-0 \
    libgl1 \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot code
COPY bot.py ./bot.py

# Report generation
COPY generate_v5_newtempl.py .

# Templates and fonts — must be in these folders
COPY tpl_v2/ ./tpl_v2/
COPY fonts/  ./fonts/

# Data dir for photos and temp files
RUN mkdir -p /app/data/photos /app/data/backups

ENV REPORT_DIR=/app/data

CMD ["python3", "bot.py"]
