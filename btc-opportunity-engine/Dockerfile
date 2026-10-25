FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml ./
COPY btc_edge ./btc_edge
COPY preview ./preview
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["python", "-m", "btc_edge.service"]
