FROM python:3.12-slim

WORKDIR /app

# Install system deps for numpy/pandas
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway volume mount point for persistent SQLite DB
ENV DATABASE_URL=sqlite:////data/swing_trader.db

CMD ["python", "main.py"]
