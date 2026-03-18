FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY main.py .

ENV PATH=/root/.local/bin:$PATH

# Expose the Flask webhook / health port
EXPOSE 8080

CMD ["python", "main.py"]
