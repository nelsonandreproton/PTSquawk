FROM python:3.12-slim

RUN useradd -u 1000 -m appuser \
 && mkdir -p /data \
 && chown 1000:1000 /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

USER appuser
EXPOSE 5000

CMD ["python", "-u", "main.py"]
