FROM python:3.11-slim-bookworm

# gcc and python3-dev are needed to compile C extensions (spidev, lgpio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# luma.emulator (pygame) is only needed for desktop development, not on hardware
RUN grep -v 'luma.emulator' requirements.txt | pip install --no-cache-dir -r /dev/stdin

COPY src/ ./src/

# Disable threading in PIL/Pillow and limit connection pools
ENV PILLOW_MAX_IMAGE_PIXELS=None \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

# config.json is mounted at runtime via docker-compose volume
ENTRYPOINT ["python3", "src/main.py"]
CMD ["--display", "ssd1322", "--width", "256", "--height", "64", \
     "--interface", "spi", "--mode", "1", "--rotate", "2"]
