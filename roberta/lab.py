import dbus
import dbus.service
from fcntl import ioctl
import json
import logging
import os
import socket
import stat
import struct
import time
import thread
import threading
import urllib2
import sys
# ignore failure to make this testable outside of the target platform
try:
    from ev3dev import auto as ev3dev
    from ev3 import Hal
except:
    from test import Hal
from __version__ import version

logger = logging.getLogger('roberta.lab')


# helpers
def getHwAddr(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    info = ioctl(s.fileno(), 0x8927,  struct.pack('256s', ifname[:15]))
    return ':'.join(['%02x' % ord(char) for char in info[18:24]])


def generateToken():
    # note: we intentionally leave '01' and 'IO' out since they can be confused
    # when entering the code
    chars = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    # note: we don't use the random module since it is large
    b = os.urandom(8)
    return ''.join(chars[ord(b[i]) % len(chars)] for i in range(8))


def getBatteryVoltage():
    try:
        return "{0:.3f}".format(ev3dev.PowerSupply().measured_volts)
    except:
        return '0.0'


class Service(dbus.service.Object):
    """OpenRobertab-Lab dbus service

    The status state machines is a follows:

    +-> disconnected
    |   |
    |    v
    +- connected
    |    |
    |    v
    +- registered
    |    ^
    |    v
    +- executing

    """

    def __init__(self, path):
        # passing None for path is only for testing
        if path:
            # needs /etc/dbus-1/system.d/openroberta.conf
            bus_name = dbus.service.BusName('org.openroberta.lab', bus=dbus.SystemBus())
            dbus.service.Object.__init__(self, bus_name, path)
            logger.debug('object registered')
            self.status('disconnected')
        self.hal = Hal(None, None)
        self.hal.clearDisplay()
        self.thread = None
        self.params = {
            'macaddr': '00:00:00:00:00:00',
            'firmwarename': 'ev3dev',
            'menuversion': version.split('-')[0],
        }
        self.updateConfiguration()

    def switchToGfxMode(self):
        logger.info('running on tty: %s' % os.ttyname(sys.stdin.fileno()))
        with open(os.ttyname(sys.stdin.fileno()), 'r') as tty:
            # KDSETMODE = 0x4B3A, GRAPHICS = 0x01
            ioctl(tty, 0x4B3A, 0x01)

    def switchToTxtMode(self):
        with open(os.ttyname(sys.stdin.fileno()), 'r+b') as tty:
            # KDSETMODE = 0x4B3A, TEXT = 0x00
            ioctl(tty, 0x4B3A, 0x00)
            # send Ctrl-L to tty to clear
            tty.write('\033c')

    def updateConfiguration(self):
        # or /etc/os-release
        with open('/proc/version', 'r') as ver:
            self.params['firmwareversion'] = ver.read()

        for iface in [b'wlan', b'usb', b'eth']:
            for ix in range(10):
                try:
                    self.params['macaddr'] = getHwAddr(iface + str(ix))
                    break
                except IOError:
                    pass
        # reusing token is nice for developers, but the server started to reject
        # them
        # self.params['token'] = generateToken()

    @dbus.service.method('org.openroberta.lab', in_signature='s', out_signature='s')
    def connect(self, address):
        logger.debug('connect(%s)' % address)
        if self.thread:
            logger.debug('disconnect() old thread')
            self.thread.running = False
        # start thread, connecting to address
        self.thread = Connector(address, self)
        self.thread.daemon = True
        self.thread.start()
        # TODO: we have to 'wait' until the connection has been established and
        # we got the token
        # - we could defer the "connected" signal and add another method to get
        #   the code
        self.status('connected')
        return self.thread.params['token']

    @dbus.service.method('org.openroberta.lab')
    def disconnect(self):
        logger.debug('disconnect()')
        self.thread.running = False
        self.status('disconnected')
        # end thread, can take up to 15 seconds (the timeout to return)
        # hence we don't join(), when connecting again we create a new thread
        # anyway
        # self.thread.join()
        # self.status('disconnected')
        self.thread = None

    @dbus.service.signal('org.openroberta.lab', signature='s')
    def status(self, status):
        logger.info('status changed: %s' % status)


class HardAbort(threading.Thread):
    """ Test for a 10s back key press and terminate the daemon"""

    def __init__(self, service):
        threading.Thread.__init__(self)
        self.service = service
        self.running = True

    def run(self):
        self.long_press = 0
        while self.running:
            if self.service.hal.isKeyPressed('back'):
                logger.debug('back: %d', self.long_press)
                # if pressed for one sec, hard exit
                if self.long_press > 10:
                    logger.info('--- hard abort ---')
                    thread.interrupt_main()
                    self.running = False
                else:
                    self.long_press += 1
            else:
                self.long_press = 0
            time.sleep(0.1)

    def __enter__(self):
        self.start()

    def __exit__(self, type, value, traceback):
        self.running = False
        if type is not None:  # an exception has occurred
            return False      # reraise the exception


class Connector(threading.Thread):
    """OpenRobertab-Lab network IO thread"""

    def __init__(self, address, service):
        threading.Thread.__init__(self)
        self.address = address
        self.service = service
        self.home = os.path.expanduser("~")
        if service:
            self.params = service.params
        else:
            self.params = {}
        self.params['token'] = generateToken()

        self.registered = False
        self.running = True
        logger.debug('thread created')

    def _fix_code(self, filename, code):
        """Apply hotfixes needed untile server update"""
        with open(filename, 'w') as prog:
            code = code.replace('import Hal,BlocklyMethods',
                                'import Hal\nfrom roberta.BlocklyMethods import BlocklyMethods')
            code = code.replace('import ev3dev', 'from ev3dev import ev3 as ev3dev')
            code = code.replace('ev3dev.color_sensor', 'Hal.makeColorSensor')
            code = code.replace('ev3dev.gyro_sensor', 'Hal.makeGyroSensor')
            code = code.replace('ev3dev.i2c_sensor', 'Hal.makeI2cSensor')
            code = code.replace('ev3dev.infrared_sensor', 'Hal.makeInfraredSensor')
            code = code.replace('ev3dev.light_sensor', 'Hal.makeLightSensor')
            code = code.replace('ev3dev.sound_sensor', 'Hal.makeSoundSensor')
            code = code.replace('ev3dev.touch_sensor', 'Hal.makeTouchSensor')
            code = code.replace('ev3dev.ultrasonic_sensor', 'Hal.makeUltrasonicSensor')
            # https://github.com/OpenRoberta/robertalab-ev3dev/issues/13
            # avoid to pull in 're' module
            code = code.replace('hal.setRegulatedMotorSpeed(\'A,', 'hal.setRegulatedMotorSpeed(\'A\',')
            code = code.replace('hal.setRegulatedMotorSpeed(\'B,', 'hal.setRegulatedMotorSpeed(\'B\',')
            code = code.replace('hal.setRegulatedMotorSpeed(\'C,', 'hal.setRegulatedMotorSpeed(\'C\',')
            code = code.replace('hal.setRegulatedMotorSpeed(\'D,', 'hal.setRegulatedMotorSpeed(\'D\',')
            # https://github.com/OpenRoberta/robertalab-ev3dev/issues/16
            code = code.replace(' || ', ' or ')
            code = code.replace(' && ', ' and ')
            # various codegen bugs
            code = code.replace(' else if ', ' elif ')
            code = code.replace(': return\n', ': return None\n')
            code = code.replace(': return', ': return ')
            # and typos in method names
            code = code.replace('BlocklyMethods.lenght', 'BlocklyMethods.length')
            prog.write(code)

    def _exec_code(self, filename, code, hard_abort):
        result = 0
        # using a new process would be using this, but is slower (4s vs <1s):
        # result = subprocess.call(["python", filename], env={"PYTHONPATH":"$PYTONPATH:."})
        # logger.info('execution result: %d' % result)
        #
        # NOTE: we don't have to keep pinging the server while running
        #   the code - robot is busy until we send push request again
        #   it would be nice though if we could cancel the running program
        try:
            compiled_code = compile(code, filename, 'exec')
            with hard_abort:
                scope = {'__name__': '__main__', 'result': 0}
                exec(compiled_code, scope)
                result = scope['result']
            logger.info('execution finished: result = %d', result)
        except:
            result = 1
            logger.exception("Ooops:")
        return result

    def _request(self, cmd, headers, timeout):
        url = '%s/%s' % (self.address, cmd)
        while True:
            try:
                logger.debug('sending request to: %s' % url)
                req = urllib2.Request(url, headers=headers)
                return urllib2.urlopen(req, json.dumps(self.params), timeout=timeout)
            except urllib2.HTTPError as e:
                if e.code == 404 and '/rest/' not in url:
                    logger.warning("HTTPError(%s): %s, retrying with '/rest'" % (e.code, e.reason))
                    # upstream changed the server path
                    url = '%s/rest/%s' % (self.address, cmd)
                elif e.code == 405 and not url.startswith('https://'):
                    logger.warning("HTTPError(%s): %s, retrying with 'https://'" % (e.code, e.reason))
                    self.address = "https" + self.address[4:]
                    url = "https" + url[4:]
                else:
                    raise e
        return None

    def run(self):
        logger.debug('network thread started')
        # network related locals
        # TODO: change the user agent:
        # https://docs.python.org/2/library/urllib2.html#urllib2.Request
        # default is "Python-urllib/2.7"
        headers = {
            'Content-Type': 'application/json'
        }
        timeout = 15  # seconds

        logger.debug('target: %s' % self.address)
        while self.running:
            if self.registered:
                self.params['cmd'] = 'push'
                timeout = 15
            else:
                self.params['cmd'] = 'register'
                timeout = 330
            self.params['brickname'] = socket.gethostname()
            self.params['battery'] = getBatteryVoltage()

            try:
                # TODO: what about /api/v1/pushcmd
                # TODO: according to https://tools.ietf.org/html/rfc6202
                # we should use keep alive
                # http://stackoverflow.com/questions/1037406/python-urllib2-with-keep-alive
                # http://stackoverflow.com/questions/13881196/remove-http-connection-header-python-urllib2
                # https://github.com/jcgregorio/httplib2
                response = self._request("pushcmd", headers, timeout)
                reply = json.loads(response.read())
                logger.debug('response: %s' % json.dumps(reply))
                cmd = reply['cmd']
                if cmd == 'repeat':
                    if not self.registered:
                        self.service.status('registered')
                        self.service.hal.playFile(2)
                    self.registered = True
                    self.params['nepoexitvalue'] = 0
                elif cmd == 'abort':
                    break
                elif cmd == 'download':
                    self.service.switchToGfxMode()
                    self.service.hal.clearDisplay()
                    self.service.status('executing')
                    # TODO: url is not part of reply :/
                    # TODO: we should receive a digest for the download (md5sum) so that
                    #   we can verify the download
                    logger.debug('download code: %s/download' % self.address)
                    response = self._request("download", headers, timeout)
                    logger.debug('response: %s' % json.dumps(reply))
                    hdr = response.info().getheader('Content-Disposition')
                    # save to $HOME/
                    filename = '%s/%s' % (self.home, hdr.split('=')[1] if hdr else 'unknown')
                    self._fix_code(filename, response.read().decode('utf-8'))
                    os.chmod(filename, stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
                    logger.info('code downloaded to: %s' % filename)
                    with open(filename) as f:
                        code = f.read()
                    # use a long-press of backspace to terminate
                    hard_abort = HardAbort(self.service)
                    hard_abort.daemon = True
                    self.params['nepoexitvalue'] = self._exec_code(filename, code, hard_abort)
                    self.service.hal.clearDisplay()
                    self.service.hal.stopAllMotors()
                    self.service.hal.resetLED()
                    # if the user did wait for a key press, wait for the key for be released
                    # before handing control back (to e.g. brickman)
                    while self.service.hal.isKeyPressed('any'):
                        time.sleep(0.1)
                    self.service.status('registered')
                    self.service.switchToTxtMode()
                elif cmd == 'update':
                    # FIXME:
                    # fetch new files (menu/hal)
                    # then restart:
                    # os.execv(__file__, sys.argv)
                    # check if we need to close files (logger?)
                    pass
                else:
                    logger.warning('unhandled command: %s' % cmd)
            except urllib2.HTTPError as e:
                # e.g. [Errno 404]
                logger.error("HTTPError(%s): %s" % (e.code, e.reason))
                break
            except urllib2.URLError as e:
                # e.g. [Errno 111] Connection refused
                logger.error("URLError: %s: %s" % (self.address, e.reason))
                break
            except socket.timeout:
                pass
            except:
                logger.exception("Ooops:")
        logger.info('network thread stopped')
        if self.service:
            self.service.status('disconnected')
        # don't play if we we just canceled a registration
        if self.registered:
            self.service.hal.playFile(3)
