FROM python:3.14.4-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY gemini.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "gemini.py"]
