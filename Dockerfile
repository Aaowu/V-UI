FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates iptables util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY README.md ./
COPY CHANGELOG.md ./
COPY SECURITY.md ./
COPY LICENSE ./
COPY .env.example ./
COPY static ./static
COPY templates ./templates
COPY scripts ./scripts

RUN mkdir -p /var/lib/vui-plan

EXPOSE 9200

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9200"]
