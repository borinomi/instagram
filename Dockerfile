FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgtk-3-0 libasound2 libx11-xcb1 libdbus-glib-1-2 \
        libxt6 libxtst6 libxcomposite1 libxdamage1 libxrandr2 \
        libxcursor1 libxi6 libpci3 libegl1 libgl1 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/camoufox-profile

EXPOSE 8026

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8026"]