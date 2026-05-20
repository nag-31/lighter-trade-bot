FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yaml .

RUN mkdir -p data

EXPOSE 8080

CMD ["python", "-m", "src.dashboard"]
