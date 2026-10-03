FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY turntable_source.py .

RUN mkdir -p /config/ssh

EXPOSE 8927 8383

ENTRYPOINT ["python3", "turntable_source.py"]