FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Set timezone to Central Time
ENV TZ=America/Chicago
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Firefox for Playwright
RUN playwright install firefox

# Copy bot code (not .env - secrets come from Fly)
COPY bot.py config.py ./

# Run the bot (waits for 8pm CT release time, then books)
CMD ["python", "-u", "bot.py"]
