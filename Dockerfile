# Conector Tiendanube. Se construye desde la raíz del repo:  docker build -t conector .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONECTOR_DB=/datos/conector.db

WORKDIR /srv
COPY conector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY conector/ ./

# Corre sin permisos de administrador; la base vive en /datos (un volumen que no se borra).
RUN useradd --system --uid 10001 conector && mkdir -p /datos && chown conector /datos
USER conector
VOLUME ["/datos"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/salud', timeout=4)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
