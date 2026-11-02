FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
COPY alembic.ini ./
COPY alembic ./alembic
RUN useradd --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "documind.main:app", "--host", "0.0.0.0", "--port", "8000"]
