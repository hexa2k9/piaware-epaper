FROM python:3.12-alpine3.20 AS builder

WORKDIR /opt/epaper
ADD requirements.txt /opt/epaper

RUN set -eux \
    && apk update \
    && apk add \
        build-base \
        alpine-sdk \
        zlib \
        zlib-dev \
        musl-dev \
        jpeg-dev \
        linux-headers \
    && pip install virtualenv \
    && virtualenv /opt/virtualenv \
    && /opt/virtualenv/bin/pip install -r requirements.txt

FROM python:3.12-alpine3.21

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/virtualenv /opt/virtualenv

ADD epd.py /opt/epaper
ADD epd.ttf /opt/epaper

CMD ["/opt/virtualenv/bin/python", "/opt/epaper/epd.py"]