FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends \
    krb5-user \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

CMD ["gunicorn", "--workers=2", "--bind=0.0.0.0:8000", "--timeout", "120", "main:app"]

