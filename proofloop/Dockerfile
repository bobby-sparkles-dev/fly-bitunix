FROM python:3.12-slim
WORKDIR /app
COPY proofloop/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY proofloop ./proofloop
ENV PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["sh", "-c", "uvicorn proofloop.app:app --host 0.0.0.0 --port ${PORT:-10000}"]
