FROM python:slim

# 호스트 유저(기본 UID/GID 1000)와 같은 ID로 실행해 볼륨에 만드는 파일이 root 소유가 되지 않게 한다
ARG PUID=1000
ARG PGID=1000
RUN groupadd -g "${PGID}" app \
    && useradd -u "${PUID}" -g "${PGID}" -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown -R "${PUID}:${PGID}" /app /data

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY --chown=${PUID}:${PGID} llm.py .
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app
USER ${PUID}:${PGID}
CMD ["python", "llm.py"]
