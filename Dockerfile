FROM python:3.11-slim

WORKDIR /app

# 系統依賴：Playwright + Chrome 共用
RUN apt-get update && apt-get install -y \
    wget gnupg2 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Google Chrome（ZOZOTOWN 用 undetected-chromedriver 需要）
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && google-chrome --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium（通用網站 fallback 用）
RUN playwright install chromium

COPY . .

ENV PORT=8000
ENV CHROME_VERSION=0
ENV UC_HEADLESS=true
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
