FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/New_York

WORKDIR /app

# System deps for pandas-ta / numpy builds
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ and logs/ are mounted from host in docker-compose.yml
RUN mkdir -p data logs

CMD ["python", "-m", "src.main"]
