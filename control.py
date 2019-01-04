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
EXTRA_FFPLAY_PARAM = ['-rtsp_transport', 'tcp']
CONTROL_PORT = 9001
UPDATE_INTERVAL = 0.05
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
    inputs = [
        {
            "id": "throttle",
            "type": "axis",
            "desc": "Throttle"
        }, {
            "id": "rudder",
            "type": "axis",
            "desc": "Rudder",
            "trim": True,
            "trim_step": 2 / 64
        }, {
            "id": "aileron",
            "type": "axis",
            "desc": "Aileron",
            "trim": True,
            "trim_step": 2 / 64
        }, {
            "id": "elevator",
            "type": "axis",
            "desc": "Elevator",
            "trim": True,
            "trim_step": 2 / 64
        }, {
            "id": "fly_no_head",
            "type": "toggle",
            "desc": "Headless Mode"
        }, {
            "id": "speed",
            "type": "toggle",
            "desc": "Speed"
        }, {
            "id": "fly_360_roll",
            "type": "once",
            "desc": "3D Flip"
        }, {
            "id": "engine_start",
            "type": "once",
            "desc": "Engine Start"
        }, {
            "id": "fly_down",
            "type": "once",
            "desc": "Automatic Landing"
        }, {
            "id": "fly_up",
            "type": "once",
            "desc": "Automatic Take-Off"
        }, {
            "id": "fly_back",
            "type": "toggle",
            "desc": "Return Home (only in headless mode)"
        }, {
            "id": "stop",
            "type": "push",
            "desc": "Emergency Stop"
        }, {
            "id": "up",
            "type": "push",
            "desc": "Upwards Evasion"
        }, {
            "id": "light",
            "type": "toggle",
            "desc": "Light"
        }
    ]

    throttle = RangedProperty(-1, 1, 0)
    throttle_trim = RangedProperty(-1, 1, 0)
    rudder = RangedProperty(-1, 1, 0)
    rudder_trim = RangedProperty(-1, 1, 0)
    aileron = RangedProperty(-1, 1, 0)
    aileron_trim = RangedProperty(-1, 1, 0)
    elevator = RangedProperty(-1, 1, 0)
    elevator_trim = RangedProperty(-1, 1, 0)

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

    def start_video(self):
        return 'rtsp://{}:{}{}'.format(IP, RTSP_PORT, RTSP_PATH)

    def cleanup(self):
        with open(SETTINGS_PATH, 'w') as f:
            json.dump({k: getattr(self, k) for k in SAVE_SETTINGS}, f)

    def update(self):
        cmd = [
            0xff,
            0x02,
            min(math.floor(self.throttle * 64) + 64, 127),
            min(math.floor(self.rudder * 64) + 64, 127),
            min(math.floor(self.elevator * 64) + 64, 127),
            min(math.floor(self.aileron * 64) + 64, 127),
            min(math.floor(self.throttle_trim * 32) + 32, 63),
            min(math.floor(self.aileron_trim * 32) + 32, 63),
            min(math.floor(self.elevator_trim * 32) + 32, 63),
            min(math.floor(self.rudder_trim * 32) + 32, 63),
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

        def trim(obj, userdata):
            axis_name, value = userdata
            axis_name += '_trim'
            old_value = getattr(self._vehicle, axis_name)
            setattr(self._vehicle, axis_name, old_value + value)

        axis_labels = []
        axis_values = []
        self._bars = {}
        for e in vehicle.inputs:
            if e['type'] != 'axis':
                continue
            axis_labels.append(urwid.Text('{}:'.format(e['desc'])))
            bar = urwid.ProgressBar(
                'progress_normal', 'progress_complete', 50)
            axis_values.append(bar)
            self._bars[e['id']] = bar
            if e.get('trim'):
                axis_labels.append(urwid.Text('{} Trim:'.format(e['desc'])))
                bar_trim = urwid.ProgressBar(
                    'progress_normal', 'progress_complete', 50)
                trim_step = e['trim_step']
                dec = urwid.Button('-', trim, (e['id'], -trim_step))
                inc = urwid.Button('+', trim, (e['id'], +trim_step))
                axis_values.append(urwid.Columns([
                    ('weight', 0, dec),
                    ('weight', 1, bar_trim),
                    ('weight', 0, inc)], min_width=5))
                self._bars['{}_trim'.format(e['id'])] = bar_trim

        labels = urwid.Pile(axis_labels)
        values = urwid.Pile(axis_values)

        labels_minwidth = max(w[0].pack()[0] for w in labels.contents)
        cols = urwid.Columns(
            [(labels_minwidth, labels), values], dividechars=1)

        def press(obj, state, userdata):
            _id, _type = userdata
            if _type == 'once' and getattr(self._vehicle, _id):
                return
            setattr(self._vehicle, _id, state)

        checkboxes = []
        self._checkboxes = {}

        for e in vehicle.inputs:
            if e['type'] == 'axis':
                continue
            ch = urwid.CheckBox(
                '{} ({})'.format(e['desc'], e['id']), on_state_change=press,
                user_data=(e['id'], e['type']))
            checkboxes.append(ch)
            self._checkboxes[e['id']] = ch

        rows = urwid.Pile([cols, *checkboxes])
        top = urwid.AttrMap(urwid.Filler(rows, 'top'), 'bg')
        evl = urwid.AsyncioEventLoop(loop=asyncio.get_event_loop())
        self._loop = urwid.MainLoop(top, palette, event_loop=evl)

    def cleanup(self):
        pass

    def update(self):
        for k, v in self._checkboxes.items():
            v.set_state(getattr(self._vehicle, k))
        for k, v in self._bars.items():
            v.set_completion(round((getattr(self._vehicle, k) + 1) * 50))

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
                if v[1] is None or v[1] < 0:
                    continue
            elif k == 'DEADZONE':
                if v is None or v < 0:
                    continue
            self._map[k] = v
        self._map['DEADZONE'] = self._map.get('DEADZONE', DEFAULT_DEADZONE)

    def cleanup(self):
        pass

    def _normalize_axis(self, value):
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
        return norm_value

    def get_state(self):
        buttons_down = set()
        buttons_up = set()
        active_buttons = set()
        axes = []

        button_events = pygame.event.get([
            pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP, pygame.JOYHATMOTION])
        pygame.event.clear()
        if pygame.joystick.get_count() == 0:
            return buttons_down, buttons_up, active_buttons, axes
        joystick = pygame.joystick.Joystick(0)
        joystick.init()

        for e in button_events:
            if e.type == pygame.JOYBUTTONDOWN:
                buttons_down.add(('btn', e.button))
            elif e.type == pygame.JOYBUTTONUP:
                buttons_up.add(('btn', e.button))
            elif e.type == pygame.JOYHATMOTION:
                prev_hat = self._prev_hats.get(e.hat, (0, 0))
                self._prev_hats[e.hat] = e.value
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
        for i in range(joystick.get_numhats()):
            hat = joystick.get_hat(i)
            if hat[0] == -1:
                active_buttons.add(('hat', i * 4 + 0))
            if hat[0] == 1:
                active_buttons.add(('hat', i * 4 + 1))
            if hat[1] == 1:
                active_buttons.add(('hat', i * 4 + 2))
            if hat[1] == -1:
                active_buttons.add(('hat', i * 4 + 3))
        for i in range(joystick.get_numbuttons()):
            if joystick.get_button(i):
                active_buttons.add(('btn', i))
        for i in range(joystick.get_numaxes()):
            raw_value = joystick.get_axis(i)
            axes.append(self._normalize_axis(raw_value))
        return buttons_down, buttons_up, active_buttons, axes

    def update(self):
        buttons_down, buttons_up, active_buttons, axes = self.get_state()
        for e in self._vehicle.inputs:
            if e['type'] == 'axis':
                axis, invert = self._map.get('{}_axis'.format(e['id']),
                                             (None, None))
                if axis is None or axis >= len(axes):
                    continue
                value = axes[axis]
                if invert:
                    value *= -1
                setattr(self._vehicle, e['id'], value)
                if e.get('trim'):
                    button_dec = self._map.get(
                        '{}_trim_dec_btn'.format(e['id']))
                    button_inc = self._map.get(
                        '{}_trim_inc_btn'.format(e['id']))
                    trim_id = '{}_trim'.format(e['id'])
                    trim_step = e['trim_step']
                    if button_dec in buttons_down:
                        setattr(self._vehicle, trim_id,
                                getattr(self._vehicle, trim_id) - trim_step)
                    if button_inc in buttons_down:
                        setattr(self._vehicle, trim_id,
                                getattr(self._vehicle, trim_id) + trim_step)
            else:
                button = self._map.get('{}_btn'.format(e['id']))
                if e['type'] == 'once':
                    if button in buttons_down:
                        setattr(self._vehicle, e['id'], True)
                elif e['type'] == 'toggle':
                    if button in buttons_down:
                        setattr(self._vehicle, e['id'],
                                not getattr(self._vehicle, e['id']))
                elif e['type'] == 'push':
                    if button in active_buttons:
                        setattr(self._vehicle, e['id'], True)
                    elif button in buttons_up:
                        setattr(self._vehicle, e['id'], False)


class Video:
    def __init__(self, vehicle):
        self._process = None
        self._url = vehicle.start_video()

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
        services.insert(0, Video(vehicle))

    async def update():
        while True:
            [s.update() for s in services]
            await asyncio.sleep(UPDATE_INTERVAL)

    asyncio.ensure_future(update())
    try:
        ui.loop()
    except KeyboardInterrupt:
        pass
    finally:
        [s.cleanup() for s in services]


if __name__ == '__main__':
    main()
