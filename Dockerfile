FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py mirror.py pool.py panel.py shop.py ./
COPY admin.html manifest.webmanifest sw.js icon.svg icon-192.png icon-512.png ./
CMD ["python","bot.py"]
