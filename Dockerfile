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

# Utilisateur non-root (UID/GID 1000, fixes pour pouvoir chown un volume hôte
# existant : `chown -R 1000:1000 <dossier_data_hote>` si vous migrez depuis
# une version qui tournait en root).
RUN groupadd -r -g 1000 ranger \
    && useradd -r -u 1000 -g ranger -d /app -s /usr/sbin/nologin ranger \
    && mkdir -p /data \
    && chown -R ranger:ranger /app /data

VOLUME ["/data"]
USER ranger

EXPOSE 7000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','7000')+'/health').read()" || exit 1

CMD ["python", "main.py"]
