# Use official Python image
FROM python:3.9-slim

# Install FFmpeg (Crucial for your app)
RUN apt-get update && apt-get install -y ffmpeg

# Hugging Face requires a specific user setup to run safely
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Copy requirements and install them
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your code into the container
COPY --chown=user . .

# Hugging Face ONLY listens on port 7860
EXPOSE 7860

# Start your server
CMD ["python", "server.py"]