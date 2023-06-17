#!/usr/bin/env python3 -u
# -*- coding:utf-8 -*-


import os
import sys
import time
import epaper
import signal
import logging
import requests
import RPi.GPIO
import slack_sdk
import sentry_sdk
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError
from requests.packages.urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont
from math import asin, cos, radians, sin, sqrt
from typing import Union, NamedTuple
from datetime import datetime, timedelta


SENTRY_DSN = os.getenv('SENTRY_DSN')
PIAWARE_HOST = os.getenv('PIAWARE_HOST')
EMERGENCY_SQUAWK = {
    '7500': 'Unlawful interference (hijacking)',
    '7600': 'Aircraft has lost verbal communication',
    '7700': 'General emergency',
}


class Position(NamedTuple):
    latitude: float
    longitude: float


def haversine_distance(
    pos1: Position,
    pos2: Position,
    radius: float = 6371.0e3
) -> float:

    """
    Calculate the distance between two points on a sphere (e.g. Earth).
    If no radius is provided then the default Earth radius, in meters, is
    used.

    The haversine formula provides great-circle distances between two points
    on a sphere from their latitudes and longitudes using the law of
    haversines, relating the sides and angles of spherical triangles.

    `Reference <https://en.wikipedia.org/wiki/Haversine_formula>`_

    :param pos1: a Position tuple defining (lat, lon) in decimal degrees
    :param pos2: a Position tuple defining (lat, lon) in decimal degrees
    :param radius: radius of sphere in meters.

    :returns: distance between two points in meters.
    :rtype: float
    """
    lat1, lon1, lat2, lon2 = [radians(x) for x in (*pos1, *pos2)]

    hav = (sin((lat2 - lat1) / 2.0) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2.0) ** 2)
    distance = 2 * radius * asin(sqrt(hav))

    return distance


def get_status() -> list:
    s = requests.Session()
    retries = Retry(total=10, backoff_factor=1)
    s.mount('http://', HTTPAdapter(max_retries=retries))

    try:
        r = s.get(f'http://{PIAWARE_HOST}/status.json?ts={time.time()}')
        res = r.json()
    except ConnectionError:
        logging.error(f'Error requesting status.json (ConnectionError)')
        res = {}
    except:
        logging.error(f'Error requesting status.json (status: {r.status_code}, error: {r.reason})')
        res = {}

    return res


def get_receiver() -> list:
    s = requests.Session()
    retries = Retry(total=10, backoff_factor=1)
    s.mount('http://', HTTPAdapter(max_retries=retries))

    try:
        r = s.get(f'http://{PIAWARE_HOST}/skyaware/data/receiver.json?ts={time.time()}')
        res = r.json()
    except ConnectionError:
        logging.error(f'Error requesting receiver.json (ConnectionError)')
        res = {}
    except:
        logging.error(f'Error requesting receiver.json (status: {r.status_code}, error: {r.reason})')
        res = {}

    return res


def get_aircrafts(
    pos: bool = True,
    mode: str = 'adsb',
    threshold: int = 120,
    raw: bool = False
) -> Union[list, int]:

    supported_modes = ['adsb', 'mlat']
    if mode not in supported_modes:
        raise ValueError(f'Mode "{mode}" unsupported. Supported: {supported_modes}')

    res = list()
    s = requests.Session()
    retries = Retry(total=10, backoff_factor=1)
    s.mount('http://', HTTPAdapter(max_retries=retries))

    try:
        r = s.get(f'http://{PIAWARE_HOST}/skyaware/data/aircraft.json?ts={time.time()}')
        json = r.json()

        if 'aircraft' in json:
            if raw == True:
                return json

            for a in json['aircraft']:
                if mode == 'adsb':
                    if pos == True:
                        if 'seen_pos' in a and a['seen_pos'] <= threshold:
                            res.append(a)
                    else:
                        if 'seen' in a and a['seen'] <= threshold:
                            res.append(a)
                elif mode == 'mlat':
                    if 'mlat' in a and 'lat' in a['mlat']:
                        res.append(a)
                else:
                    logging.warning(f'Unknown Mode: {mode}')

        ac_count = len(res)
        logging.info(f'Found {ac_count} total Flights (Position: {pos}, Mode: {mode}, Threshold: {threshold}s)')

        return ac_count
    except ConnectionError:
        logging.error(f'Error requesting aircraft.json (ConnectionError)')
    except:
        logging.error(f'Error requesting aircraft.json (status: {r.status_code}, error: {r.reason})')


def trim(string: str) -> str:
    return str(string).strip()


def ucfirst(string: str) -> str:
    return string[0].upper() + string[1:]


def format_slack_field(key: str, value: str) -> str:
    return f'*{key}:*\n{value}'


def send_slack_notification(aircraft: str, callsign: str, squawk: str, distance: str) -> bool:
    slack_token = os.getenv('SLACK_BOT_TOKEN')
    slack_channel = os.getenv('SLACK_CHANNEL')

    if slack_token is not None and slack_channel is not None:
        try:
            client = slack_sdk.WebClient(token=slack_token)

            message_blocks = [
                slack_sdk.models.blocks.blocks.HeaderBlock(text=f'ICAO Emergency Squawk on {PIAWARE_HOST}'),
                slack_sdk.models.blocks.blocks.SectionBlock(text=EMERGENCY_SQUAWK[squawk]),
                slack_sdk.models.blocks.blocks.DividerBlock(),
                slack_sdk.models.blocks.blocks.SectionBlock(fields=[
                    format_slack_field('Aircraft', aircraft),
                    format_slack_field('Callsign', callsign),
                    format_slack_field('Squawk', squawk),
                    format_slack_field('Distance', distance),
                    format_slack_field('Tar1090', f'<http://{PIAWARE_HOST}/tar1090/?icao={aircraft}|Open>'),
                    format_slack_field('SkyAware', f'<http://{PIAWARE_HOST}/skyaware/|Open>')
                ])
            ]

            response = client.chat_postMessage(
                channel = slack_channel,
                text = f'ICAO Emergency Squawk {squawk}',
                blocks = message_blocks
            )

            if bool(response.get('ok', False)) is True:
                return True
            else:
                return False
        except:
            logging.error('Unable to Send Slack Message')
            return False
    else:
        return False


def distance(origin: Position, mode: str = 'max', threshold: int = 120) -> float:
    supported_modes = ['max', 'min']
    if mode not in supported_modes:
        raise ValueError(f'Mode "{mode}" unsupported. Supported: {supported_modes}')

    aircrafts = get_aircrafts(raw=True)
    if mode == 'max':
        aircraft_range = 0
    else:
        aircraft_range = sys.maxsize

    for aircraft in aircrafts['aircraft']:
        if 'lat' in aircraft and 'lon' in aircraft and 'seen_pos' in aircraft:
            if aircraft['seen_pos'] <= threshold:
                distance = haversine_distance(
                    origin,
                    Position(latitude=aircraft['lat'], longitude=aircraft['lon'])
                )
                if mode == 'max':
                    if distance > aircraft_range:
                        aircraft_range = distance
                else:
                    if distance < aircraft_range:
                        aircraft_range = distance

    logging.info(f'{ucfirst(mode)} Aircraft Distance: {round(aircraft_range, 4):,}m')

    return aircraft_range


def process_interrupt(channel: int) -> None:
    if channel == 5:
        logging.info(f'Received Event on Pin {channel}.')
    elif channel == 6:
        logging.info(f'Received Event on Pin {channel} - Clearing Display.')
        clear(clear=True, sleep=False)
    elif channel == 13:
        logging.info(f'Received Event on Pin {channel} - Executing Refresh.')
        refresh(cycle=0)
    elif channel == 19:
        logging.info(f'Received Event on Pin {channel} - Executing Shutdown.')
        shutdown(signal_number=15)
    else:
        raise ValueError(f'Unexpected Pin: {channel}')


def process_shutdown_signal(signal_number: int, frame) -> None:
    shutdown(signal_number)


def shutdown(signal_number: int) -> None:
    signal_name = signal.Signals(signal_number).name

    if signal_name == 'SIGHUP':
        logging.info(f'Received {signal_name}, ignoring.')
        return

    logging.info(f'Shutting down on Signal {signal_number} ({signal_name}).')

    clear(clear=True, sleep=True)
    RPi.GPIO.cleanup()

    logging.info(f'Shutdown on Signal {signal_name} complete.')
    sys.exit(0)


def clear(clear: bool = False, sleep: bool = False):
    epd = epaper.epaper('epd2in7').EPD()
    epd.init()

    if clear:
        epd.Clear(0xFF)

    if sleep:
        epd.sleep()

    return epd


def has_emergency(current_status: str, mode: str = 'slug') -> Union[int, str]:
    supported_modes = ['slug', 'count']
    if mode not in supported_modes:
        raise ValueError(f'Mode "{mode}" unsupported. Supported: {supported_modes}')

    emergency = 0
    aircrafts = get_aircrafts(raw=True)
    if 'aircraft' in aircrafts:
        receiver = get_receiver()
        if 'lat' in receiver and 'lon' in receiver:
            origin = Position(latitude=receiver['lat'], longitude=receiver['lon'])
        else:
            origin = Position(latitude=0.000, longitude=0.000)

        for ac in aircrafts['aircraft']:
            if 'squawk' in ac:
                if any(sq == ac['squawk'] for sq in list(EMERGENCY_SQUAWK.keys())):
                    emergency += 1
                    if 'flight' in ac:
                        flight = trim(ac['flight'])
                    else:
                        flight = 'unknown'

                    if 'lat' in ac and 'lon' in ac:
                        aircraft_distance = f'{round(haversine_distance(pos1=origin, pos2=Position(latitude=ac["lat"], longitude=ac["lon"])) / 1000, 2)} km'
                    else:
                        aircraft_distance = 'unknown'

                    notification_sent = send_slack_notification(aircraft=ac['hex'], callsign=flight, squawk=ac['squawk'], distance=aircraft_distance)

                    logging.warning(f'Aircraft {ac["hex"]} (Callsign {flight}) with ICAO Emergency Squawk {ac["squawk"]} found in {aircraft_distance} distance (Notification sent {notification_sent})')
    else:
        emergency = 666

    if mode == 'slug':
        if emergency >= 1:
            new_status = f'!!! SQUAWK 7x00 (Count: {emergency}) !!!'
        else:
            new_status = current_status

        return new_status
    else:
        return emergency


def refresh(cycle: int = 1) -> int:
    font_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'epd.ttf')
    font_s = ImageFont.truetype(font_path, 11)
    font_m = ImageFont.truetype(font_path, 12)

    try:
        error = False
        status = get_status()
        status_slug = 'OK'

        if 'system_uptime' in status:
            uptime = str(timedelta(seconds = status['system_uptime']))
        else:
            error = True
            uptime = 'Failed API Call'

        if 'time' in status:
            status_time = datetime.fromtimestamp(status['time'] / 1000).strftime('%d.%m.%Y, %H:%M:%S')
        else:
            error = True
            status_time = '!!! Failed to get System Time'

        if 'gps' in status:
            status_gps = status['gps']['message']
        else:
            error = True
            status_gps = '!!! Failed to get GPS Status'

        if 'radio' in status:
            status_radio = status['radio']['message']
        else:
            error = True
            status_radio = '!!! Failed to get Radio Status'

        if 'piaware' in status:
            status_piaware = status['piaware']['message']
        else:
            error = True
            status_piaware = '!!! Failed to get PiAware Status'

        if error is True: status_slug = 'NEEDS ATTENTION'
        status_slug = has_emergency(current_status=status_slug)

        clear_display = False
        if cycle == 1: clear_display = True

        epd = clear(clear=clear_display, sleep=False)

        logging.debug(f'e-Paper Display is {epd.height}x{epd.width} (Height x Width)')

        ep_image = Image.new('1', (epd.height, epd.width), 255)  # 255: clear the frame
        draw = ImageDraw.Draw(ep_image)

        draw.line((0, 88, 264, 88), fill = 0)
        draw.line((132, 88, 132, 176), fill = 0)

        draw.text((4, 4),  f'Status ({status_slug})', font = font_s, fill = 0)
        draw.text((8, 20), status_time, font = font_m, fill = 0)
        draw.text((8, 35), status_piaware, font = font_m, fill = 0)
        draw.text((8, 50), status_gps, font = font_m, fill = 0)
        draw.text((8, 65), status_radio, font = font_m, fill = 0)

        #draw.text((136, 2),  'Header B', font = font_s, fill = 0)
        #draw.text((140, 15), 'Line B.1.2', font = font_m, fill = 0)
        #draw.text((140, 35), 'Line B.2.2', font = font_m, fill = 0)
        #draw.text((140, 55), 'Line B.3.2', font = font_m, fill = 0)

        receiver = get_receiver()
        if 'lat' in receiver and 'lon' in receiver:
            origin = Position(latitude=receiver['lat'], longitude=receiver['lon'])
            logging.info(f'Own {origin}')
        else:
            origin = Position(latitude=0.000, longitude=0.000)
            logging.error(f'Could not get own Location. Own {origin}')

        ths = 120
        draw.text((4, 90),  'Aircrafts', font = font_s, fill = 0)
        draw.text((8, 103), f'Count (all): {get_aircrafts(pos=False, threshold=ths)}', font = font_m, fill = 0)
        draw.text((8, 116), f'Count (w/ pos): {get_aircrafts(pos=True, threshold=ths)}', font = font_m, fill = 0)
        draw.text((8, 129), f'Count (MLAT): {get_aircrafts(pos=True, mode="mlat", threshold=ths)}', font = font_m, fill = 0)
        draw.text((8, 142), f'Min Range: {round(distance(origin=origin, mode="min", threshold=ths) / 1000, 1)} km', font = font_m, fill = 0)
        draw.text((8, 155), f'Max Range: {round(distance(origin=origin, mode="max", threshold=ths) / 1000, 1)} km', font = font_m, fill = 0)

        if 'cpu_temp_celcius' in status:
            status_temp = round(status['cpu_temp_celcius'], 2)
        else:
            status_temp = '-273.15'

        if 'cpu_load_percent' in status:
            status_load = status['cpu_load_percent']
        else:
            status_load = os.cpu_count() * 100

        draw.text((136, 90),  'Operating System', font = font_s, fill = 0)
        draw.text((140, 103), f'Up: {uptime}', font = font_m, fill = 0)
        draw.text((140, 116), f'CPU Load: {status_load}%', font = font_m, fill = 0)
        draw.text((140, 129), f'CPU Temp: {status_temp}°C', font = font_m, fill = 0)
        #draw.text((140, 142), f'Local: {status_temp_local}°C', font = font_m, fill = 0)
        #draw.text((140, 155), f'Line D.3.2', font = font_m, fill = 0)

        draw.text((136, 160),  f'Refresh Cycle: {cycle:,}', font = font_s, fill = 0)

        epd.display(epd.getbuffer(ep_image))
        epd.sleep()
        ep_image.save(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'epd.jpg'))
        new_cycle = cycle + 1

        return new_cycle
    except IOError as e:
        logging.error(e)
        shutdown(signal_number=15)


def process() -> None:
    cycle = 1

    while True:
        logging.info(f'Starting Refresh Cycle {cycle}')

        current_cycle = cycle
        cycle = refresh(cycle=current_cycle)

        i = 0
        sleepy_display = 300
        sleepy_updates = round(sleepy_display / 10)
        logging.info(f'Sleeping for {sleepy_display} seconds after refresh cycle {current_cycle} (Ping every {sleepy_updates}s)')
        while i < sleepy_display:
            time.sleep(1)
            i += 1
            if i % sleepy_updates == 0:
                logging.info(f'Slept {i}/{sleepy_display} seconds after refresh cycle {current_cycle}')


def setup() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logging.info(f'Running on PID: {os.getpid()}')

    if SENTRY_DSN is not None:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=1.0
        )
    else:
        logging.info('Skipping Sentry SDK Configuration. SENTRY_DSN is not set.')

    if PIAWARE_HOST is None:
        raise RuntimeError(f'Environment Variable PIAWARE_HOST is not set.')

    RPi.GPIO.setmode(RPi.GPIO.BCM)

    RPi.GPIO.setup(5, RPi.GPIO.IN, pull_up_down=RPi.GPIO.PUD_UP)
    RPi.GPIO.setup(6, RPi.GPIO.IN, pull_up_down=RPi.GPIO.PUD_UP)
    RPi.GPIO.setup(13, RPi.GPIO.IN, pull_up_down=RPi.GPIO.PUD_UP)
    RPi.GPIO.setup(19, RPi.GPIO.IN, pull_up_down=RPi.GPIO.PUD_UP)

    RPi.GPIO.add_event_detect(5, RPi.GPIO.FALLING, callback=process_interrupt, bouncetime=200)
    RPi.GPIO.add_event_detect(6, RPi.GPIO.FALLING, callback=process_interrupt, bouncetime=200)
    RPi.GPIO.add_event_detect(13, RPi.GPIO.FALLING, callback=process_interrupt, bouncetime=200)
    RPi.GPIO.add_event_detect(19, RPi.GPIO.FALLING, callback=process_interrupt, bouncetime=200)

    signal.signal(signal.SIGINT, process_shutdown_signal)
    signal.signal(signal.SIGINT, process_shutdown_signal)
    signal.signal(signal.SIGQUIT, process_shutdown_signal)
    signal.signal(signal.SIGTERM, process_shutdown_signal)


if __name__ == '__main__':
    setup()
    process()
