FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Disable threading in PIL/Pillow and limit connection pools
ENV PILLOW_MAX_IMAGE_PIXELS=None \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

# config.json is mounted at runtime via docker-compose volume
ENTRYPOINT ["python3", "src/main.py"]
