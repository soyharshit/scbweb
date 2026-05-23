FROM python:3.11-slim

RUN apt update && apt install -y ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -U pip && pip install -r requirements.txt

COPY . .

CMD ["uv", "run", "-m", "main"]
