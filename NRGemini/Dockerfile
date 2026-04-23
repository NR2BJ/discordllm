FROM python:slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 fonts-noto-cjk fonts-nanum && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY gemini.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "gemini.py"]
