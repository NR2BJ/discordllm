FROM python:slim
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY llm.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "llm.py"]
