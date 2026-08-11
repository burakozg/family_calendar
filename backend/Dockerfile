# Image = Python + pinned deps only. Code (backend/, frontend/) and data stay
# bind-mounted at runtime so edits remain live — the image just removes the
# pip-install-at-boot step and enables a read-only root filesystem (S7).
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
