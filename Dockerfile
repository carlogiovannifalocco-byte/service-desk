FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY service_desk service_desk
EXPOSE 8000
CMD ["sh", "-c", "uvicorn service_desk.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
