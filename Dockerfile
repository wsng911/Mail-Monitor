FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ARG VERSION=dev
ENV APP_VERSION=${VERSION}

CMD ["python", "-u", "monitor.py"]
