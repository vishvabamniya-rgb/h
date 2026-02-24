FROM python:3.11-slim

WORKDIR /app

# (optional but useful)
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start.sh

CMD ["sh", "start.sh"]
