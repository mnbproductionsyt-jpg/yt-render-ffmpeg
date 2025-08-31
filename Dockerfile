FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Logs sin buffer para que Cloud Run los muestre
ENV PYTHONUNBUFFERED=1

# Gunicorn escuchando en $PORT y con logs capturados
CMD ["bash","-lc","exec gunicorn -b 0.0.0.0:${PORT:-8080} app:app --workers 1 --threads 2 --timeout 3600 --capture-output --access-logfile - --error-logfile - --log-level debug"]
