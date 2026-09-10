# Рецепт образа взят из шаблона платформы без изменений по существу:
# digest-пиннинг, непривилегированный пользователь, порядок COPY.
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91
ENV APP_ENV=production PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-bus.txt ./
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements-bus.txt
COPY --chown=65532:65532 app ./app
EXPOSE 8080
USER 65532:65532
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
