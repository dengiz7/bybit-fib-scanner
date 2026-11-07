FROM python:3.12-slim
WORKDIR /app
COPY . /app
EXPOSE 8080
CMD ["python", "scanner.py"]
