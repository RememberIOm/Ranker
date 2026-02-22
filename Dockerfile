FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 세션 데이터 디렉토리 (Fly Volume이 /data에 마운트됨)
RUN mkdir -p /data/sessions
# Docker 환경에서는 저장 경로를 /data/sessions 로 설정하도록 환경변수 추가
ENV SESSION_DIR=/data/sessions

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
