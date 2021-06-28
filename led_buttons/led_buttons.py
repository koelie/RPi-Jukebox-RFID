print("starting")
import time
import signal
import logging
import sys
import functools
import systemd.daemon
from itertools import cycle, chain, repeat
from os import environ
from enum import Enum

from musicpd import MPDClient, ConnectionError, CommandError
from gpiozero import LEDBoard, ButtonBoard
from gpiozero.threads import GPIOThread


log = logging.getLogger(__name__)

print("Imports done")
sys.stdout.flush()

class FancyLEDBoard(LEDBoard):
    def __init__(self, *args, **kwargs):
        self.fps = 25
        super().__init__(*args, **kwargs)

    def swish(self, duration=2, width=0.5, n=None):
        sequence = []
        peak_location = 0
        peak_direction = 1
        time_step = 1 / self.fps
        peak_step = time_step / (duration / 2)
        num_leds = len(self.leds)
        led_positions = [led/(num_leds-1) for led in range(num_leds)]

        while peak_location >= 0:
            sequence.append([
                max(0, 1 - (abs(peak_location - pos) / width))
                for pos in led_positions
            ])
            peak_location += peak_step * peak_direction
            if peak_location > 1:
                peak_location = 2 - peak_location
                peak_direction = -1

        self.run_sequence(sequence, n=n)

    def run_sequence(self, sequence, n=None):
        self._stop_blink()
        self._blink_thread = GPIOThread(self._run_sequence, (sequence, n))
        self._blink_thread.start()

    def _run_sequence(self, sequence, n):
        time_step = 1 / self.fps
        if n is None:
            sequence = cycle(sequence)
        else:
            sequence = chain.from_iterable(repeat(sequence, n))
        with self._blink_lock:
            self._blink_leds = list(self.leds)
            for led in self._blink_leds:
                if led._controller not in (None, self):
                    led._controller._stop_blink(led)
                led._controller = self
        for values in sequence:
            with self._blink_lock:
                if not self._blink_leds:
                    break
                for led, value in zip(self._blink_leds, values):
                    led._write(value)
            if self._blink_thread.stopping.wait(time_step):
                break

    def fade_in(self, duration=1):
        self._stop_blink()
        self._blink_thread = GPIOThread(self._fade_in_device, (duration,))
        self._blink_thread.start()

    def _fade_in_device(self, duration=1):
        time_step = 1 / self.fps
        fade_step = time_step / duration
        level = 0
        while level < 1:
            for led in self.leds:
                if led.value < level:
                    led._write(level)
            level += fade_step
            if self._blink_thread.stopping.wait(time_step):
                break
            if level > 1:
                for led in self.leds:
                    led._write(1)

    def blink_off(self, duration=.2):
        time_step = 1/self.fps
        t = 0
        sequence = []
        while t < duration:
            sequence.append([0.0 for _ in self.leds])
            t += time_step
        sequence.append([1 for _ in self.leds])
        self.run_sequence(sequence, n=1)
        


def autoreconnect(func):
    @functools.wraps(func)
    def wrapper(obj, *args, **kwargs):
        try:
            func(obj, *args, **kwargs)
        except BrokenPipeError:
            log.warning("Broken pipe, attempting reconnect")
            obj.await_mpd()
            log.info("Reconnected")
            func(obj, *args, **kwargs)
        except ValueError:
            log.warning("ValueError, attempting reconnect")
            obj.await_mpd()
            func(obj, *args, **kwargs)
        except ConnectionError:
            log.warning("Disconnected, attempting reconnect")
            obj.await_mpd()
            func(obj, *args, **kwargs)
    return wrapper

class LEDButtonManager():
    def __init__(self):
        log.info("Creating buttons")
        self.leds = FancyLEDBoard(26, 16, 12, 5, 7, pwm=True)
        self.buttons = ButtonBoard(20, 13, 6, 25)

        log.info("Setting up callbacks")
        # hardcoded button / led indices
        self.PLAY = 0
        self.NEXT = 1
        self.VOL_DOWN = 2
        self.VOL_UP = 3
        self.POWER = 4

        self.playing = False
        self.volume_step = 5

        self.buttons[self.PLAY].when_pressed = self.on_play
        self.buttons[self.NEXT].when_pressed = self.on_next
        self.buttons[self.VOL_DOWN].when_pressed = self.on_vol_down
        self.buttons[self.VOL_DOWN].when_held = self.on_vol_down_held
        self.buttons[self.VOL_DOWN].hold_time = .5
        self.buttons[self.VOL_DOWN].hold_repeat = True
        self.buttons[self.VOL_UP].when_pressed = self.on_vol_up
        self.buttons[self.VOL_UP].when_held = self.on_vol_up_held
        self.buttons[self.VOL_UP].hold_time = .5
        self.buttons[self.VOL_UP].hold_repeat = True

        log.info("Starting MPD Client")
        self.mpd = MPDClient()

    @autoreconnect
    def on_play(self):
        self.update_state()
        next_state = "Pause" if self.playing else "Play"
        log.info("Play/Pause pressed, next state: %s", next_state)
        self.mpd.pause() if self.playing else self.mpd.play()
        self.leds.blink_off()
        self.update_state()

    @autoreconnect
    def on_next(self):
        log.info("Next song pressed")
        try:
            self.mpd.next()
        except CommandError:
            log.warning("Not playing")
        self.leds.blink_off()
        self.update_state()

    @autoreconnect
    def on_vol_down(self):
        log.info("Decrease volume pressed")
        self.leds.blink_off(duration=0.1)
        self.mpd.volume(-self.volume_step)

    @autoreconnect
    def on_vol_down_held(self):
        log.info("Decrease volume held")
        self.leds.blink_off(duration=0.1)
        self.mpd.volume(-2*self.volume_step)

    @autoreconnect
    def on_vol_up(self):
        log.info("Increase volume pressed")
        self.leds.blink_off()
        self.mpd.volume(self.volume_step)

    @autoreconnect
    def on_vol_up_held(self):
        log.info("Increase volume held")
        self.leds.blink_off()
        self.mpd.volume(2*self.volume_step)

    def update_state(self):
        status = self.mpd.status()
        log.info("Mpd status: %s", status['state'])
        self.playing = status['state'] == 'play'

    def await_mpd(self):
        try:
            log.info("Attempting disconnect")
            self.mpd.disconnect()
            log.info("Disconnected")
        except ConnectionError:
            pass
        except Exception as exc:
            log.exception("Failed disconnect")
        environ['MPD_TIMEOUT'] = "2"
        while True:
            try:
                log.info("Attempting to connect to MPD")
                self.mpd.connect("/var/run/mpd/socket")
                self.update_state()
                log.info("Connected to MPD")
                return
            except (ConnectionError, FileNotFoundError):
                log.info("No connection to MPD")
                time.sleep(.5)


    def run(self):
        log.info("Start loading animation")
        self.leds.swish(duration=1.5, width=0.3, n=None)
        self.await_mpd()

        log.info("Start normal operation")
        self.leds.fade_in()
        signal.pause()



def cli():
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format='[%(asctime)s: %(levelname)s] %(message)s'
    )
    log.info("Starting input daemon")
    mgr = LEDButtonManager()
    systemd.daemon.notify('READY=1')
    mgr.run()

if __name__ == "__main__":
    print("In main")
    sys.stdout.flush()
    cli()


# DONE
# - catch error of losing mpd connection
# systemd script for buttons

# TODO
# - ripple or other ack effect
# connect last button???
