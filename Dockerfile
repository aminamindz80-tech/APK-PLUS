FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV ATELIER_DB=/data/atelier.db
VOLUME /data
EXPOSE 8000
CMD ["python", "server.py", "--port", "8000"]
