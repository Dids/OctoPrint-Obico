# coding=utf-8
import time
import random
import logging
import raven
import re
import os
import platform
from sarge import run, Capture
import tempfile
from io import BytesIO
import struct
import threading
import socket
from contextlib import closing
import backoff
import octoprint
import requests

from .lib.error_stats import error_stats

CAM_EXCLUSIVE_USE = os.path.join(tempfile.gettempdir(), '.using_picam')

PRINTER_SETTINGS_UPDATE_INTERVAL = 60*30.0  # Update printer settings at max 30 minutes interval, as they are relatively static.

_logger = logging.getLogger('octoprint.plugins.obico')


class ExpoBackoff:

    def __init__(self, max_seconds, max_attempts=0):
        self.attempts = 0
        self.max_seconds = max_seconds
        self.max_attempts = max_attempts

    def reset(self):
        self.attempts = 0

    def more(self, e):
        self.attempts += 1
        if self.max_attempts > 0 and self.attempts > self.max_attempts:
            _logger.error('Giving up after %d attempts on error: %s' % (self.attempts, e))
            raise e
        else:
            delay = 2 ** (self.attempts-3)
            if delay > self.max_seconds:
                delay = self.max_seconds
            delay *= 0.5 + random.random()
            _logger.error('Attempt %d - backing off %f seconds: %s' % (self.attempts, delay, e))

            time.sleep(delay)


class OctoPrintSettingsUpdater:

    def __init__(self, plugin):
        self._mutex = threading.RLock()
        self.plugin = plugin
        self.last_asked = 0
        self.printer_metadata = None

    def update_settings(self):
        with self._mutex:
            self.last_asked = 0

    def update_firmware(self, payload):
        with self._mutex:
            self.printer_metadata = payload['data']
            self.last_asked = 0

    def as_dict(self):
        with self._mutex:
            if self.last_asked > time.time() - PRINTER_SETTINGS_UPDATE_INTERVAL:
                return None

        data = dict(
            webcam=dict((k, v) for k, v in self.plugin._settings.effective['webcam'].items() if k in ('flipV', 'flipH', 'rotate90', 'streamRatio')),
            temperature=self.plugin._settings.settings.effective['temperature'],
            agent=dict(name='octoprint_obico', version=self.plugin._plugin_version),
            octoprint_version=octoprint.util.version.get_octoprint_version_string(),
        )
        if self.printer_metadata:
            data['printer_metadata'] = self.printer_metadata

        with self._mutex:
            self.last_asked = time.time()

        return data


class SentryWrapper:

    def __init__(self, plugin):
        self.sentryClient = raven.Client(
            'https://f0356e1461124e69909600a64c361b71@sentry.obico.io/4',
            release=plugin._plugin_version,
            ignore_exceptions = [
                'BrokenPipeError',
                'SSLError',
                'SSLEOFError',
                'ConnectionResetError',
                'ConnectionError',
                'ConnectionRefusedError',
                'WebSocketConnectionClosedException',
                'ReadTimeout',
                'OSError',
            ]
        )
        self.plugin = plugin

    def enabled(self):
        return self.plugin._settings.get(["sentry_opt"]) != 'out' \
            and self.plugin.canonical_endpoint_prefix().endswith('obico.io')

    def captureException(self, *args, **kwargs):
        _logger.exception("Exception")
        if self.enabled():
            self.sentryClient.captureException(*args, **kwargs)

    def user_context(self, *args, **kwargs):
        if self.enabled():
            self.sentryClient.user_context(*args, **kwargs)

    def captureMessage(self, *args, **kwargs):
        if self.enabled():
            self.sentryClient.captureMessage(*args, **kwargs)


def pi_version():
    try:
        with open('/sys/firmware/devicetree/base/model', 'r') as firmware_model:
            model = re.search('Raspberry Pi(.*)', firmware_model.read()).group(1)
            if model:
                return "0" if re.search('Zero', model, re.IGNORECASE) else "3"
            else:
                return None
    except:
        return None


system_tags = None
tags_mutex = threading.RLock()

def get_tags():
    global system_tags, tags_mutex

    with tags_mutex:
        if system_tags:
            return system_tags

    (os, _, ver, _, arch, _) = platform.uname()
    tags = dict(os=os, os_ver=ver, arch=arch)
    try:
        v4l2 = run('v4l2-ctl --list-devices', stdout=Capture())
        v4l2_out = ''.join(re.compile(r"^([^\t]+)", re.MULTILINE).findall(v4l2.stdout.text)).replace('\n', '')
        if v4l2_out:
            tags['v4l2'] = v4l2_out
    except:
        pass

    try:
        usb = run("lsusb | cut -d ' ' -f 7- | grep -vE ' hub| Hub' | grep -v 'Standard Microsystems Corp'", stdout=Capture())
        usb_out = ''.join(usb.stdout.text).replace('\n', '')
        if usb_out:
            tags['usb'] = usb_out
    except:
        pass

    with tags_mutex:
        system_tags = tags
        return system_tags


def not_using_pi_camera():
    try:
        os.remove(CAM_EXCLUSIVE_USE)
    except:
        pass


def using_pi_camera():
    open(CAM_EXCLUSIVE_USE, 'a').close()  # touch CAM_EXCLUSIVE_USE to indicate the intention of exclusive use of pi camera


def get_image_info(data):
    data_bytes = data
    if not isinstance(data, str):
        data = data.decode('iso-8859-1')
    size = len(data)
    height = -1
    width = -1
    content_type = ''

    # handle GIFs
    if (size >= 10) and data[:6] in ('GIF87a', 'GIF89a'):
        # Check to see if content_type is correct
        content_type = 'image/gif'
        w, h = struct.unpack("<HH", data[6:10])
        width = int(w)
        height = int(h)

    # See PNG 2. Edition spec (http://www.w3.org/TR/PNG/)
    # Bytes 0-7 are below, 4-byte chunk length, then 'IHDR'
    # and finally the 4-byte width, height
    elif ((size >= 24) and data.startswith('\211PNG\r\n\032\n')
          and (data[12:16] == 'IHDR')):
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[16:24])
        width = int(w)
        height = int(h)

    # Maybe this is for an older PNG version.
    elif (size >= 16) and data.startswith('\211PNG\r\n\032\n'):
        # Check to see if we have the right content type
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[8:16])
        width = int(w)
        height = int(h)

    # handle JPEGs
    elif (size >= 2) and data.startswith('\377\330'):
        content_type = 'image/jpeg'
        jpeg = BytesIO(data_bytes)
        jpeg.read(2)
        b = jpeg.read(1)
        try:
            while (b and ord(b) != 0xDA):
                while (ord(b) != 0xFF):
                    b = jpeg.read(1)
                while (ord(b) == 0xFF):
                    b = jpeg.read(1)
                if (ord(b) >= 0xC0 and ord(b) <= 0xC3):
                    jpeg.read(3)
                    h, w = struct.unpack(">HH", jpeg.read(4))
                    break
                else:
                    jpeg.read(int(struct.unpack(">H", jpeg.read(2))[0])-2)
                b = jpeg.read(1)
            width = int(w)
            height = int(h)
        except struct.error:
            pass
        except ValueError:
            pass

    return content_type, width, height


@backoff.on_exception(backoff.expo, Exception, max_tries=3, jitter=None)
@backoff.on_predicate(backoff.expo, max_tries=3, jitter=None)
def wait_for_port(host, port):
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        return sock.connect_ex((host, port)) == 0


def wait_for_port_to_close(host, port):
    for i in range(10):   # Wait for up to 5s
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            if sock.connect_ex((host, port)) != 0:  # Port is not open
                return
            time.sleep(0.5)


def server_request(method, uri, plugin, timeout=30, raise_exception=False, **kwargs):
    '''
    Return: A requests response object if it reaches the server. Otherwise None. Connections errors are printed to console but NOT raised
    '''

    endpoint = plugin.canonical_endpoint_prefix() + uri
    try:
        error_stats.attempt('server')
        resp = requests.request(method, endpoint, timeout=timeout, **kwargs)
        if not resp.ok and not resp.status_code == 401:
            error_stats.add_connection_error('server', plugin)

        return resp
    except Exception:
        error_stats.add_connection_error('server', plugin)
        _logger.exception("{}: {}".format(method, endpoint))
        if raise_exception:
            raise


def raise_for_status(resp, with_content=False, **kwargs):
    # puts reponse content into exception
    if with_content:
        try:
            resp.raise_for_status()
        except Exception as exc:
            args = exc.args
            if not args:
                arg0 = ''
            else:
                arg0 = args[0]
            arg0 = "{} {}".format(arg0, resp.text)
            exc.args = (arg0, ) + args[1:]
            exc.kwargs = kwargs

            raise
    resp.raise_for_status()

# TODO: remove once all TSD users have migrated
def migrate_tsd_settings(plugin):
    if plugin.is_configured():
        return
    if plugin._settings.get(['tsd_migrated']):
        return
    tsd_settings = plugin._settings.settings.get(['plugins', ]).get('thespaghettidetective')
    if tsd_settings:
        for k in tsd_settings.keys():
            if k == 'endpoint_prefix' and tsd_settings.get(k) == 'https://app.thespaghettidetective.com':
                continue
            plugin._settings.set([k],tsd_settings.get(k), force=True)

        plugin._settings.set(["tsd_migrated"], 'yes', force=True)
        plugin._settings.save(force=True)
