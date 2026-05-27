FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App fayllari
COPY . .

# Chromium allaqachon image ichida — qo'shimcha install kerak emas
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["python", "main.py"]
