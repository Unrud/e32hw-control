# E32HW Joystick Controller

Control the Eachine E32HW Quad-copter from a computer with a joystick.

## Requirements

  * [Python](https://python.org)
  * [pygame](https://pygame.org)
  * [Urwid](http://urwid.org)
  * a joystick

## Usage

Start the program with:

```
python3 control.py
```

The first available joystick is used. You can configure the button mapping
in the program.

Connect to the WLAN of the quad-copter.

Add the ``--help`` argument for further information.

## Technical details

The quad-copter is controlled by sending UDP packets to ``192.168.99.1``
on port 9001. A packet is send every 50 ms.

The IP camera is available on ``rtsp://192.168.99.1/11``. Viewing the IP
camera will cause the WLAN to stop working after a while.

### Packet format

### Byte 0

``0x5B``

### Byte 1

``0x52``

### Byte 2

``0x74``

### Byte 3

``0x3E``

### Byte 4

``0x1A``

### Byte 5

``0x00``

### Byte 6

``0x01``

### Byte 7

A counter.

```python
previous_packet = [0] * 26

packet[7] = (previous_packet[7] + 1) & 0xFF
```

### Byte 8

``0xE0``

### Byte 9

``0x00``

### Byte 10

Sum of the byte-wise differences between this and the previous
packet modulo ``0xFF``.

```python
previous_packet = [0] * 26

# calculate at the end
packet[10] = 0
diff = 0
for a, b in zip(previous_packet, packet):
    diff += a - b
packet[10] = diff % 0xFF
```

### Byte 11

``0x00``

### Byte 12

``0xFF``

### Byte 13

``0x02``

### Byte 14

Throttle from ``0x00`` to ``0x7F`` (inclusive). Neutral at ``0x40``.

### Byte 15

Rudder from ``0x00`` to ``0x7F`` (inclusive). Neutral at ``0x40``.

### Byte 16

Elevator from ``0x00`` to ``0x7F`` (inclusive). Neutral at ``0x40``.

### Byte 17

Aileron from ``0x00`` to ``0x7F`` (inclusive). Neutral at ``0x40``.

### Byte 18

Throttle trimmer from ``0x00`` to ``0x3F`` (inclusive). Neutral at ``0x20``.

### Byte 19

Aileron trimmer from ``0x00`` to ``0x3F`` (inclusive). Neutral at ``0x20``.

### Byte 20

Elevator trimmer from ``0x00`` to ``0x3F`` (inclusive). Neutral at ``0x20``.

### Byte 21

Rudder trimmer from ``0x00`` to ``0x3F`` (inclusive). Neutral at ``0x20``.

### Byte 22

Flag byte:

#### Bit 0

Always inactive. Called ``hight`` in the app.

#### Bit 1

Active while headless mode is turned on.

#### Bit 2

Active while high speed mode is turned on.

#### Bit 3

Active for 500 ms for "3D Flip".

#### Bit 4

Active for 1000 ms to start and stop engine.

#### Bit 5

Active for 1000 ms for automatic landing.

#### Bit 6

Active for 1000 ms for automatic take-off.

### Byte 23

Flag byte:

#### Bit 0

Active to return home (?). Can only be active while in headless mode in app.

#### Bit 1

Active for emergency stop.

#### Bit 2

Always inactive. Called ``middle speed`` in the app.

#### Bit 3

Active for upwards evasion (?).

#### Bit 4

Always inactive. Called ``control type`` and ``app control`` in the app.

#### Bit 5

Low bit of product type. Product type is ``1`` in the app.

#### Bit 6

High bit of product type.

### Byte 24

Flag byte:

#### Bit 0

Active to turn lights on.

### Byte 25

7 bit sum of the last 12 bytes.

```python
packet[25] = sum(packet[13:25]) & 0x7F
```
