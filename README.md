# PiAware Status Display

Based on the [Waveshare 264x176 Resolution (2.7 Inch) e-Paper Display](https://www.amazon.de/gp/product/B075FWLMRV/) this is a Status Display for my [PiAware Feeder](https://www.flightaware.com/adsb/piaware/build). It's basically to play a bit with a Raspberry Pi and e-Paper Displays, learn Python (it's the "first thing" I ever wrote in Python - Code is certainly far from perfect, but it works).

PiAware is just serving as a Data Source as it's running at home and provided more value to me than Weather (I can look outside after all) or Bitcoin Price.

## Setup

Clone Repository

```bash
git clone https://github.com/hexa2k9/piaware-epaper.git /opt/piaware-epaper
```

Setup Python Virtualenv & Requirements

```bash
cd /opt/piaware-epaper
virtualenv .venv
.venv/bin/pip install -r requirements.txt
```

Install Systemd Unit

```bash
cd /opt/piaware-epaper
cp piaware-epaper.service /etc/systemd/system
systemctl daemon-reload
systemctl enable piaware-epaper.service
```

Install & Adjust Configuration

```bash
cd /opt/piaware-epaper
cp piaware-epaper.default.dist /etc/default/piaware-epaper

## Adjust as needed
vim /etc/default/piaware-epaper
```

Start Service

```bash
systemctl start piaware-epaper.service
```

## Display Buttons

| Button   | Function       |
|----------|----------------|
| `KEY1`   | - none -       |
| `KEY2`   | Clear Display  |
| `KEY3`   | Refresh Status |
| `KEY4`   | Shutdown       |
