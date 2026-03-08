# 1. Start with a Python environment
FROM python:3.10-slim

# 2. Set the working directory inside the cloud server
WORKDIR /app

# 3. INSTALL FFMPEG (This is the line you asked about!)
# This ensures the Linux server can process your music files
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# 4. Copy your requirements list and install the libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy everything else from your PC to the cloud server
COPY . .

# 6. Tell the server to use port 7860 (Hugging Face standard)
ENV PORT=7860
EXPOSE 7860

# 7. Start the app
CMD ["python", "server.py"]