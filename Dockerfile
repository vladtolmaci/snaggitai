FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY generate_v5_newtempl.py .
COPY tpl_v2/ ./tpl_v2/
COPY fonts/ ./fonts/

RUN mkdir -p /app/data/photos /app/data/backups
RUN ln -sf /app/fonts /app/data/fonts && \
    ln -sf /app/tpl_v2 /app/data/tpl_v2

ENV REPORT_DIR=/app/data
ENV ASSETS_DIR=/app

CMD ["python3", "bot.py"]
