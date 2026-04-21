FROM python:3.14.3-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY llm.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "llm.py"]
