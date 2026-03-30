FROM python:3.12-slim

ARG CACHEBUST=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libglib2.0-0 \
    libgl1 \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY generate_v5_newtempl.py .
COPY tpl_v2/ ./tpl_v2/
COPY fonts/ ./fonts/

RUN mkdir -p /app/data/photos /app/data/backups

# Symlink so /app/data/fonts and /app/data/tpl_v2 work too
RUN ln -sf /app/fonts /app/data/fonts && \
    ln -sf /app/tpl_v2 /app/data/tpl_v2

ENV REPORT_DIR=/app/data

CMD ["python3", "bot.py"]
