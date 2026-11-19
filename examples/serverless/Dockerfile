# GPU application dependencies are in the image, outside any mounted storage.
FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404
RUN python -m venv --system-site-packages /opt/gipper-worker
ENV PATH="/opt/gipper-worker/bin:${PATH}"
RUN python -m pip install --no-cache-dir runpod==2.3.0
WORKDIR /app
COPY worker.py /app/worker.py
CMD ["python", "-u", "/app/worker.py"]
