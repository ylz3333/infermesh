FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY infermesh ./infermesh
COPY config ./config
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --no-create-home app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
CMD ["uvicorn", "infermesh.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

