FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY app/ app/

# Create logs directory
RUN mkdir -p logs

# Never run as root
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "app.main"]
