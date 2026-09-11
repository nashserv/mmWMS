FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91
ENV APP_ENV=production PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-mock.txt ./
# Заглушка потока 0 ставится вместе с сервисом: она нужна потокам B и C, пока
# они пишут клиентов, и снимается вместе с флагом WMS_MOCK, а не пересборкой.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements-mock.txt
COPY --chown=65532:65532 app ./app
# Из какого коммита собран образ. Без этого «стенд поднят из main» —
# утверждение, а не факт: контейнер, собранный вчера из другого клона,
# выглядит точно так же (`docker inspect` покажет revision).
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA

EXPOSE 8080
USER 65532:65532
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
