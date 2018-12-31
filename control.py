#!/usr/bin/env python3

# Copyright (c) 2018 Unrud<unrud@outlook.com>
#
# This programe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import asyncio
import collections
import contextlib
import json
import math
import pygame
import socket
import subprocess
import time
import urwid
import weakref

# Aileron: left / right
# Elevator: forward / backward
# Rudder: rotate left / rotate right
# Throttle: up / down

# Example joystick configuration:
# {
#     "DEADZONE": 0.1,
#
#     "_comment": "possible values: AXIS or [AXIS, INVERTED] or null",
#     "rudder_axis": 0,
#     "throttle_axis": [1, true],
#     "aileron_axis": 2,
#     "elevator_axis": [3, true],
#
#     "_comment": "possible values: NR or [\"btn\", NR] or [\"hat\", NR] or null",
#     "fly_down_btn": 6,
#     "fly_up_btn": 3,
#     "engine_start_btn": 1,
#     "stop_btn": 7,
#     "fly_360_roll_btn": 4,
#     "speed_btn": 9,
#     "light_btn": 8,
#     "fly_no_head_btn": 0,
#     "fly_back_btn": 2,
#     "up_btn": 5,
#     "rudder_trim_dec_btn": null,
#     "rudder_trim_inc_btn": null,
#     "throttle_trim_dec_btn": null,
#     "throttle_trim_inc_btn": null,
#     "aileron_trim_dec_btn": ["hat", 0],
#     "aileron_trim_inc_btn": ["hat", 1],
#     "elevator_trim_inc_btn": ["hat", 2],
#     "elevator_trim_dec_btn": ["hat", 3]
# }

IP = '192.168.99.1'
RTSP_PORT = 554
RTSP_PATH = '/11'
FFPLAY_CMD = 'ffplay'
EXTRA_FFPLAY_PARAM = []
CONTROL_PORT = 9001
INTERVAL = 0.05
SETTINGS_PATH = '.control.json'
SAVE_SETTINGS = ['speed', 'throttle_trim', 'rudder_trim', 'elevator_trim',
                 'aileron_trim']
DEFAULT_DEADZONE = 0.1


class RangedProperty(property):
    def __init__(self, min_value, max_value, value):
        super().__init__(self._get, self._set)
        self._min_value = min_value
        self._max_value = max_value
        self._default_value = value
        self._values = {}

    def _get(self, obj):
        return self._values.get(id(obj), self._default_value)

    def _set(self, obj, value):
        # assert self._min_value <= value and value <= self._max_value
        value = min(max(value, self._min_value), self._max_value)
        key = id(obj)
        if key not in self._values:
            weakref.finalize(obj, self._values.pop, key)
        self._values[key] = value


class TimedProperty(property):
    def __init__(self, timeout, default_value):
        super().__init__(self._get, self._set)
        self._default_value = default_value
        self._timeout = timeout
        self._values = {}

    def _get(self, obj):
        start_time, value = self._values.get(id(obj), (0, self._default_value))
        if time.perf_counter() - start_time < self._timeout:
            return value
        return self._default_value

    def _set(self, obj, value):
        key = id(obj)
        if key not in self._values:
            weakref.finalize(obj, self._values.pop, key)
        self._values[key] = (time.perf_counter(), value)


def bits_to_byte(*bits):
    assert len(bits) <= 8
    value = 0
    for i, b in enumerate(bits):
        if b:
            value |= 1 << i
    return value


class Vehicle:

    throttle = RangedProperty(0, 127, 64)
    throttle_trim = RangedProperty(32, 32, 32)
    rudder = RangedProperty(0, 127, 64)
    rudder_trim = RangedProperty(0, 63, 32)
    aileron = RangedProperty(0, 127, 64)
    aileron_trim = RangedProperty(0, 63, 32)
    elevator = RangedProperty(0, 127, 64)
    elevator_trim = RangedProperty(0, 63, 32)

    hight = False
    _fly_no_head = False
    speed = False
    fly_360_roll = TimedProperty(0.5, False)
    engine_start = TimedProperty(1, False)
    fly_down = TimedProperty(1, False)
    fly_up = TimedProperty(1, False)
    _fly_back = False
    _stop = False
    middle_speed = False
    _up = False
    control_type = False
    product_type = RangedProperty(1, 3, 1)
    light = True

    @property
    def fly_no_head(self):
        return self._fly_no_head

    @fly_no_head.setter
    def fly_no_head(self, value):
        self.fly_back = False
        self._fly_no_head = value

    @property
    def fly_back(self):
        return self.fly_no_head and self._fly_back

    @fly_back.setter
    def fly_back(self, value):
        self._fly_back = value

    @property
    def stop(self):
        return self._stop

    @stop.setter
    def stop(self, value):
        if value:
            self._up = False
        self._stop = value

    @property
    def up(self):
        return self._up

    @up.setter
    def up(self, value):
        if value:
            self._stop = False
        self._up = value

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._prev_packet = [0] * 26

        with contextlib.suppress(FileNotFoundError):
            with open(SETTINGS_PATH) as f:
                j = json.load(f)
            for k in SAVE_SETTINGS:
                if k in j:
                    setattr(self, k, j[k])

    def cleanup(self):
        with open(SETTINGS_PATH, 'w') as f:
            json.dump({k: getattr(self, k) for k in SAVE_SETTINGS}, f)

    def update(self):
        cmd = [
            0xff,
            0x02,
            self.throttle,
            self.rudder,
            self.elevator,
            self.aileron,
            self.throttle_trim,
            self.aileron_trim,
            self.elevator_trim,
            self.rudder_trim,
            bits_to_byte(
                self.hight,
                self.fly_no_head,
                self.speed,
                self.fly_360_roll,
                self.engine_start,
                self.fly_down,
                self.fly_up),
            bits_to_byte(
                self.fly_back,
                self.stop,
                self.middle_speed,
                self.up,
                self.control_type,
                self.product_type & 1,
                self.product_type & 2),
            bits_to_byte(
                self.light)]
        # checksum
        cmd.append(sum(cmd[1:]) & 0x7f)
        packet = [
            0x5b,
            0x52,
            0x74,
            0x3e,
            0x1a,
            0x00,
            0x01,
            (self._prev_packet[7] + 1) & 0xff,
            0xe0,
            0x00,
            0x00,  # placeholder
            0x00,
            *cmd]

        diff = 0
        for a, b in zip(self._prev_packet, packet):
            diff += a - b
        packet[10] = diff % 0xff
        try:
            self._sock.sendto(bytes(packet), (IP, CONTROL_PORT))
        except OSError:
            pass
        else:
            self._prev_packet = packet


class Ui:
    def __init__(self, vehicle):
        self._vehicle = vehicle
        palette = [
            ('bg', 'black', 'white'),
            ('progress_normal', 'black', 'light gray'),
            ('progress_complete', 'white', 'black')]

        throttle_label = urwid.Text(u'Throttle:')
        throttle_trim_label = urwid.Text(u'Throttle Trim:')
        rudder_label = urwid.Text(u'Rudder:')
        rudder_trim_label = urwid.Text(u'Rudder Trim:')
        aileron_label = urwid.Text(u'Aileron:')
        aileron_trim_label = urwid.Text(u'Aileron Trim:')
        elevator_label = urwid.Text(u'Elevator:')
        elevator_trim_label = urwid.Text(u'Elevator Trim:')

        def trim(obj, userdata):
            axis_name, value = userdata
            axis_name += '_trim'
            old_value = getattr(self._vehicle, axis_name)
            setattr(self._vehicle, axis_name, old_value + value)

        self._throttle = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        self._throttle_trim = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        throttle_trim_dec = urwid.Button('-', trim, ('throttle', -1))
        throttle_trim_inc = urwid.Button('+', trim, ('throttle', +1))
        throttle_trim_box = urwid.Columns([
            ('weight', 0, throttle_trim_dec),
            ('weight', 1, self._throttle_trim),
            ('weight', 0, throttle_trim_inc)], min_width=5)
        self._rudder = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        self._rudder_trim = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        rudder_trim_dec = urwid.Button('-', trim, ('rudder', -1))
        rudder_trim_inc = urwid.Button('+', trim, ('rudder', +1))
        rudder_trim_box = urwid.Columns([
            ('weight', 0, rudder_trim_dec),
            ('weight', 1, self._rudder_trim),
            ('weight', 0, rudder_trim_inc)], min_width=5)
        self._aileron = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        self._aileron_trim = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        aileron_trim_dec = urwid.Button('-', trim, ('aileron', -1))
        aileron_trim_inc = urwid.Button('+', trim, ('aileron', +1))
        aileron_trim_box = urwid.Columns([
            ('weight', 0, aileron_trim_dec),
            ('weight', 1, self._aileron_trim),
            ('weight', 0, aileron_trim_inc)], min_width=5)
        self._elevator = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        self._elevator_trim = urwid.ProgressBar(
            'progress_normal', 'progress_complete', 50)
        elevator_trim_dec = urwid.Button('-', trim, ('elevator', -1))
        elevator_trim_inc = urwid.Button('+', trim, ('elevator', +1))
        elevator_trim_box = urwid.Columns([
            ('weight', 0, elevator_trim_dec),
            ('weight', 1, self._elevator_trim),
            ('weight', 0, elevator_trim_inc)], min_width=5)

        labels = urwid.Pile([
            throttle_label, throttle_trim_label, rudder_label,
            rudder_trim_label, elevator_label, elevator_trim_label,
            aileron_label, aileron_trim_label])
        values = urwid.Pile([
            self._throttle, throttle_trim_box, self._rudder, rudder_trim_box,
            self._elevator, elevator_trim_box,
            self._aileron, aileron_trim_box])

        labels_minwidth = max(w[0].pack()[0] for w in labels.contents)
        cols = urwid.Columns(
            [(labels_minwidth, labels), values], dividechars=1)

        def ch(obj, state, name):
            setattr(self._vehicle, name, state)

        self._fly_no_head = urwid.CheckBox(
            'Headless mode (Fly No Head)', on_state_change=ch,
            user_data='fly_no_head')
        self._speed = urwid.CheckBox(
            'High speed mode (Speed)', on_state_change=ch, user_data='speed')
        self._fly_360_roll = urwid.CheckBox(
            '3D Flip (Fly 360 Roll)', on_state_change=ch,
            user_data='fly_360_roll')
        self._engine_start = urwid.CheckBox(
            'Engine Start', on_state_change=ch, user_data='engine_start')
        self._fly_down = urwid.CheckBox(
            'Automatic landing (Fly Down)', on_state_change=ch,
            user_data='fly_down')
        self._fly_up = urwid.CheckBox(
            'Automatic take-off (Fly Up)', on_state_change=ch,
            user_data='fly_up')
        self._fly_back = urwid.CheckBox(
            'Return home (only in headless mode) (Fly Back)',
            on_state_change=ch, user_data='fly_back')
        self._stop = urwid.CheckBox(
            'Emergency stop (Stop)', on_state_change=ch, user_data='stop')
        self._up = urwid.CheckBox(
            'Upwards evasion (Up)', on_state_change=ch, user_data='up')
        self._light = urwid.CheckBox(
            'Light', on_state_change=ch, user_data='light')

        rows = urwid.Pile([
            cols, self._fly_no_head, self._speed, self._fly_360_roll,
            self._engine_start, self._fly_down, self._fly_up, self._fly_back,
            self._stop, self._up, self._light])
        top = urwid.AttrMap(urwid.Filler(rows, 'top'), 'bg')
        evl = urwid.AsyncioEventLoop(loop=asyncio.get_event_loop())
        self._loop = urwid.MainLoop(top, palette, event_loop=evl)

    def cleanup(self):
        pass

    def update(self):
        self._throttle.set_completion(round(
            self._vehicle.throttle / 127 * 100))
        self._throttle_trim.set_completion(round(
            self._vehicle.throttle_trim / 63 * 100))
        self._rudder.set_completion(round(
            self._vehicle.rudder / 127 * 100))
        self._rudder_trim.set_completion(round(
            self._vehicle.rudder_trim / 63 * 100))
        self._aileron.set_completion(round(
            self._vehicle.aileron / 127 * 100))
        self._aileron_trim.set_completion(round(
            self._vehicle.aileron_trim / 63 * 100))
        self._elevator.set_completion(round(
            self._vehicle.elevator / 127 * 100))
        self._elevator_trim.set_completion(round(
            self._vehicle.elevator_trim / 63 * 100))

        self._fly_no_head.set_state(self._vehicle.fly_no_head)
        self._speed.set_state(self._vehicle.speed)
        self._fly_360_roll.set_state(self._vehicle.fly_360_roll)
        self._engine_start.set_state(self._vehicle.engine_start)
        self._fly_down.set_state(self._vehicle.fly_down)
        self._fly_up.set_state(self._vehicle.fly_up)
        self._fly_back.set_state(self._vehicle.fly_back)
        self._stop.set_state(self._vehicle.stop)
        self._up.set_state(self._vehicle.up)
        self._light.set_state(self._vehicle.light)

    def loop(self):
        self._loop.run()


class Joystick:
    def __init__(self, vehicle, joystick_mapping):
        pygame.display.init()
        pygame.joystick.init()
        self._vehicle = vehicle
        self._prev_hats = {}

        self._map = {}
        for k, v in joystick_mapping.items():
            if k.endswith('_axis'):
                if isinstance(v, collections.Iterable):
                    v = tuple(v)
                else:
                    v = (v, False)
                if v[0] is None or v[0] < 0:
                    continue
            elif k.endswith('_btn'):
                if isinstance(v, collections.Iterable):
                    v = tuple(v)
                else:
                    v = ('btn', v)
                # assert v[0] in ('btn', 'hat')
                if v[1] is None or v[1] < 0:
                    continue
            elif k == 'DEADZONE':
                if v is None or v < 0:
                    continue
            # else:
            #     raise AssertionError
            self._map[k] = v
        self._map['DEADZONE'] = self._map.get('DEADZONE', DEFAULT_DEADZONE)
        self._max_axis = max((v[0] for k, v in self._map.items() if
                              k.endswith('_axis')), default=-1)
        self._max_btn = max(
            (v[1] for k, v in self._map.items() if
             k.endswith('_btn') and v[0] == 'btn'), default=-1)
        self._max_hat = max(
            (v[1] for k, v in self._map.items() if
             k.endswith('_btn') and v[0] == 'hat'), default=-1)

    def cleanup(self):
        pass

    def _normalize_axis(self, value):
        # assert -1.0 <= value and value <= 1.0
        value = min(max(value, -1.0), 1.0)
        abs_value = abs(value)
        if abs_value < self._map['DEADZONE']:
            norm_value = 0
        else:
            norm_value = abs_value - self._map['DEADZONE']
            norm_value /= 1 - self._map['DEADZONE']
            norm_value = min(1.0, norm_value)
            if value <= 0:
                norm_value *= -1
        return min(math.floor(norm_value * 64) + 64, 127)

    def update(self):
        button_events = pygame.event.get([
            pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP, pygame.JOYHATMOTION])
        pygame.event.clear()
        for i in range(pygame.joystick.get_count()):
            joystick = pygame.joystick.Joystick(i)
            joystick.init()
            # choose the first joystick that has enough inputs
            if (joystick.get_numaxes() <= self._max_axis or
                    joystick.get_numbuttons() <= self._max_btn or
                    joystick.get_numhats() <= self._max_hat // 4):
                continue

            for axis_name in ('rudder', 'throttle', 'aileron', 'elevator'):
                axis, invert = self._map.get('{}_axis'.format(axis_name),
                                             (None, None))
                if axis is None:
                    continue
                raw_value = joystick.get_axis(axis)
                if invert:
                    raw_value *= -1
                value = self._normalize_axis(raw_value)
                setattr(self._vehicle, axis_name, value)

            buttons_down = set()
            buttons_up = set()
            for e in button_events:
                if e.joy != i:
                    continue
                if e.type == pygame.JOYBUTTONDOWN:
                    buttons_down.add(('btn', e.button))
                elif e.type == pygame.JOYBUTTONUP:
                    buttons_up.add(('btn', e.button))
                elif e.type == pygame.JOYHATMOTION:
                    k = (e.joy, e.hat)
                    prev_hat = self._prev_hats.get(k, (0, 0))
                    self._prev_hats[k] = e.value
                    if prev_hat[0] == -1 and e.value[0] != -1:
                        buttons_up.add(('hat', e.hat * 4 + 0))
                    if prev_hat[0] != -1 and e.value[0] == -1:
                        buttons_down.add(('hat', e.hat * 4 + 0))
                    if prev_hat[0] == 1 and e.value[0] != 1:
                        buttons_up.add(('hat', e.hat * 4 + 1))
                    if prev_hat[0] != 1 and e.value[0] == 1:
                        buttons_down.add(('hat', e.hat * 4 + 1))
                    if prev_hat[1] == 1 and e.value[1] != 1:
                        buttons_up.add(('hat', e.hat * 4 + 2))
                    if prev_hat[1] != 1 and e.value[1] == 1:
                        buttons_down.add(('hat', e.hat * 4 + 2))
                    if prev_hat[1] == -1 and e.value[1] != -1:
                        buttons_up.add(('hat', e.hat * 4 + 3))
                    if prev_hat[1] != -1 and e.value[1] == -1:
                        buttons_down.add(('hat', e.hat * 4 + 3))

            if self._map.get('fly_down_btn') in buttons_down:
                self._vehicle.fly_down = True
            if self._map.get('fly_up_btn') in buttons_down:
                self._vehicle.fly_up = True
            if self._map.get('engine_start_btn') in buttons_down:
                self._vehicle.engine_start = True
            if self._map.get('stop_btn') in buttons_down:
                self._vehicle.stop = True
            if self._map.get('fly_360_roll_btn') in buttons_down:
                self._vehicle.fly_360_roll = True
            if self._map.get('speed_btn') in buttons_down:
                self._vehicle.speed ^= True
            if self._map.get('light_btn') in buttons_down:
                self._vehicle.light ^= True
            if self._map.get('fly_no_head_btn') in buttons_down:
                self._vehicle.fly_no_head ^= True
            if self._map.get('fly_back_btn') in buttons_down:
                self._vehicle.fly_back ^= True
            if self._map.get('up_btn') in buttons_down:
                self._vehicle.up = True

            if self._map.get('rudder_trim_dec_btn') in buttons_down:
                self._vehicle.rudder_trim -= 1
            if self._map.get('rudder_trim_inc_btn') in buttons_down:
                self._vehicle.rudder_trim += 1

            if self._map.get('throttle_trim_dec_btn') in buttons_down:
                self._vehicle.throttle_trim -= 1
            if self._map.get('throttle_trim_inc_btn') in buttons_down:
                self._vehicle.throttle_trim += 1

            if self._map.get('aileron_trim_dec_btn') in buttons_down:
                self._vehicle.aileron_trim -= 1
            if self._map.get('aileron_trim_inc_btn') in buttons_down:
                self._vehicle.aileron_trim += 1
            if self._map.get('elevator_trim_dec_btn') in buttons_down:
                self._vehicle.elevator_trim -= 1
            if self._map.get('elevator_trim_inc_btn') in buttons_down:
                self._vehicle.elevator_trim += 1

            if self._map.get('stop_btn') in buttons_up:
                self._vehicle.stop = False
            if self._map.get('up_btn') in buttons_up:
                self._vehicle.up = False

            break


class Video:
    def __init__(self):
        self._process = None
        self._url = 'rtsp://{}:{}{}'.format(IP, RTSP_PORT, RTSP_PATH)

    def update(self):
        if not self._process or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [FFPLAY_CMD, self._url, *EXTRA_FFPLAY_PARAM],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)

    def cleanup(self):
        if self._process:
            self._process.terminate()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mapping', metavar='JOYSTICK_MAPPING',
                        help='JSON file with button mapping')
    parser.add_argument('--video', action='store_true', default=False,
                        help='open IP camera in media player '
                             '(don\'t use while flying, '
                             'will cause WLAN to stop working)')
    args = parser.parse_args()
    with open(args.mapping) as f:
        mapping = json.load(f)

    vehicle = Vehicle()
    ui = Ui(vehicle)
    services = [Joystick(vehicle, mapping), ui, vehicle]
    if args.video:
        services.insert(0, Video())

    async def update():
        while True:
            [s.update() for s in services]
            await asyncio.sleep(INTERVAL)

    asyncio.ensure_future(update())
    try:
        ui.loop()
    except KeyboardInterrupt:
        pass
    finally:
        [s.cleanup() for s in services]


if __name__ == '__main__':
    main()
