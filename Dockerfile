FROM python:3.13-alpine3.21 AS builder

WORKDIR /opt/epaper
ADD requirements.txt /opt/epaper

RUN set -eux \
    && apk update \
    && apk add \
        build-base \
        alpine-sdk \
        zlib-dev \
        musl-dev \
        jpeg-dev \
        libgpiod-dev \
        linux-headers \
    && pip install virtualenv \
    && virtualenv /opt/virtualenv \
    && /opt/virtualenv/bin/pip install --upgrade pip \
    && /opt/virtualenv/bin/pip install -r requirements.txt

FROM python:3.13-alpine3.21

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/virtualenv /opt/virtualenv

WORKDIR /opt/epaper

ADD piaware-epaper.py /opt/epaper
ADD epaper.ttf /opt/epaper

CMD ["/opt/virtualenv/bin/python", "/opt/epaper/piaware-epaper.py"]
