FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7000 \
    RANGER_DB=/data/ranger.db

WORKDIR /app

# Dépendances (couche cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY . .

# Volume pour la base de cache SQLite
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 7000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','7000')+'/health').read()" || exit 1

CMD ["python", "main.py"]
