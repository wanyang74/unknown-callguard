FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY callguard/ ./callguard/
COPY rules*.md ./
ENV PORT=8080 DB_PATH=/data/calls.db
CMD ["python", "-m", "callguard"]
