FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["bash","-lc","gunicorn -b 0.0.0.0:${PORT:-8080} app:app --workers 2 --threads 4 --timeout 3600"]
