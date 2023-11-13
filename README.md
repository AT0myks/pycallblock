# pycallblock

<p align="left">
<a><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/pycallblock"></a>
<a href="https://pypi.org/project/pycallblock/"><img alt="PyPI" src="https://img.shields.io/pypi/v/pycallblock"></a>
<a href="https://github.com/AT0myks/pycallblock/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/pycallblock"></a>
</p>

* [Introduction](#introduction)
* [Requirements](#requirements)
* [Installation](#installation)
* [Usage](#usage)
* [Block modes](#block-modes)
* [Extended functionality](#extended-functionality)
* [Notes](#notes)
* [Glossary](#glossary)

## Introduction

This project started in July 2020 after I finally decided
to do something about those daily spam calls.
I found [jcblock](https://sourceforge.net/projects/jcblock/),
which inspired me to make my own version of a call blocker.
So I bought a cheap modem and found a manual online.
I had no idea how all of this worked and it took some time to get used
to the modem and how to properly use it.
Seeing the caller ID show up for the first time in the terminal was very satisfying.
Then I found out that you can also use the modem to record the calls
and even send audio, and you basically get a phone controlled by software.
After a few years of successful blocking and some upgrades,
it's time for this project to be made public so that you can enjoy a silent
phone while the scammers are listening to the audio files you prepared for them.

## Requirements

### Hardware

You need a computer and a USB modem. I use a Raspberry Pi Zero.
In theory it should work with any
[AT](https://en.wikipedia.org/wiki/Hayes_command_set) modem,
but I have not tested anything else than the Conexant based ones I have,
so I cannot guarantee that different models will work.
The modem must support caller ID, and voice mode if you want to use the voice features.
Unfortunately you can't really be sure of the modem's capabilities before you plug it in.

Only hardware modems are supported, this is not compatible with softmodems.

Here are the results of `lsusb` for two modems that I have:
```
0572:1340 Conexant Systems (Rockwell), Inc. USB Modem
0572:1340 Conexant Systems (Rockwell), Inc.
```
They both look like [this](https://www.aliexpress.com/item/32974192089.html).
It used to be easy to find them for pretty cheap on sites
like eBay and AliExpress but it seems like
that's not the case anymore, unfortunately.

If you have a wireless phone I guess you can use a one port modem.
If your phone is wired you'll need a modem with two ports.

Once you plug it in, the modem should show up as something like `/dev/ttyACM0`
(this the default device in pycallblock).

Installing pycallblock will install
[pyserial](https://github.com/pyserial/pyserial)
which comes with 
[`pyserial-ports`](https://pyserial.readthedocs.io/en/latest/tools.html#module-serial.tools.list_ports)
and
[`pyserial-miniterm`](https://pyserial.readthedocs.io/en/latest/tools.html#module-serial.tools.miniterm).
You can use this first command to find serial ports
and the second one to test your device.

Here's the output in my case:
```
$ pyserial-ports -v
/dev/ttyACM0
    desc: USB Modem
    hwid: USB VID:PID=0572:1340 SER=12345678 LOCATION=1-1:1.0
```

### Software

Linux and Python 3.9+.

I did not test it on Windows, but if you can interface with the modem
just like you can do on Linux, and if the code is compatible,
there should be no reason for it not to work.
On Linux you'll probably have to add your user to the `dialout` group
by doing `sudo usermod -a -G dialout youruser` and then logging out and in again.

## Installation

```
pip install pycallblock
```

## Usage

pycallblock comes with a CLI:

```
optional arguments:
  -h, --help                  show this help message and exit
  -V, --version               print version
  --device DEV                the device to use. Default: /dev/ttyACM0
  --logfile FILE              enable logging to the specified file
  --syslog [ADDRESS]          enable logging to syslog. Default: /dev/log
  -p, --block-private         block private numbers. Default: false
  -l, --stderr                log to stderr
  -s SEC, --silence SEC       seconds of silence before triggering the callback. Only for duplex. Must be an int or a float > 0. Default: disabled
  -v, --verbose               increase verbosity
  -a FILE, --audio-file FILE  WAV file for -m, -d and -t modes
  --timezone TIMEZONE         IANA timezone for the file names. Default: UTC
  -b CSV, --blacklist CSV     CSV file with the contacts that will be blocked
  -w CSV, --whitelist CSV     CSV file with the contacts that will be allowed

Block actions:
  Choose what happens when a call is blocked. If nothing is specified, default to instant hangup.

  -f, --fax                   act as a fax machine
  -m, --message               play a message specified with -a
  -r [SEC], --record [SEC]    record the call for SEC seconds. Default: 120
  -d [SEC], --duplex [SEC]    duplex for SEC seconds. Default: 120
  -t [SEC], --transmit [SEC]  transmit for SEC seconds. Default: 120
```

When you run pycallblock for the first time, it will create a `pycallblock`
directory in the user's home.
Inside, the database for logging the calls will be created
along with the `play` and `rec` directories.
The recordings will go in `rec`, and in `play` you can put your WAV files
to be randomly sent in duplex mode.
This is also where you can put your CSV file that contains blocked/allowed contacts.
The expected separator for the CSV file is the comma.
If you don't have a name for a contact, you must still put a comma after the number.
If you want to have a comma in the name, use double quotes around the name.
Example:
```
01234,
56789,Ben
5055034455,"Look, there's a comma"
```
If your block/allow list is empty, every call will be allowed/blocked respectively.

The recordings will be named according to the following format:
`%Y-%m-%d %H-%M-%S%z NUMBER`. The default timezone is UTC.
If you want your files to be named according to your local time,
set a [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
with `--timezone`.
On Windows you might need the [tzdata](https://pypi.org/project/tzdata/) package.

### Recording WAV files

The audio must be unsigned PCM, mono, 8bit, at 8000Hz.
I use [Audacity](https://www.audacityteam.org/) to record the WAV files.
In the bottom left corner set the `Project Rate` to 8000Hz,
and record your audio.
When exporting, choose `Other uncompressed files` then `WAV (Microsoft)`
and `Unsigned 8-bit PCM` and set the output channels to 1.
Then you can put the file in `play` and test it to see if it plays correctly.

It's also possible to use Audacity to convert an existing file by going to
`Tracks -> Resample...`, choosing 8000Hz and exporting as described above.

### Run as a service

See the [example file](https://github.com/AT0myks/pycallblock/blob/main/pycallblock.service).
You can add the `--syslog` argument to the command and then do
`journalctl -t pycallblock` or `journalctl -u pycallblock` to see the logs.
Do not use both `--syslog` and `--stderr` in this case.

## Block modes

### Default behaviour

Instantly hang up.
The caller's device might keep ringing,
but nothing should happen on your end.
Adding a delay before hanging up should help, in case that's what you want.

### Fax machine

The modem will act as a fax machine and play the
[CED tone](https://www.youtube.com/watch?v=6v4GDjenyZE).

### Play an audio file

Play a WAV file and hang up.

### Transmit

Send audio until the timeout is reached or the caller hangs up.
If you want to play an audio file after picking up, use the `-a` option.
In this mode, DTMFs are received.
This means that you could for example execute a certain action
(like playing a WAV file) when a specific button is pressed,
just like [IVR](https://en.wikipedia.org/wiki/Interactive_voice_response).
Not the most interesting mode for blocking calls, but it might have its use cases.

### Record

Record the call until the timeout is reached or the caller hangs up.
DTMFs are received, but because there's no transmit,
you can't send any audio back.

### Duplex

Record + transmit. Recording alone can already give entertaining results,
but sometimes if there's no sound after the robot picks up, it will drop the call.
Like with transmit, you can set an audio file to be played at pick up.
If you enable silence detection with `-s`, a random file from the 
`play` directory will be played after the specified amount of seconds
of silence has been reached.
The recording will contain the audio that is sent.

## Extended functionality

The default functionality should already be enough for most.
But you can also build on top of pycallblock to make your own call blocker.

If you choose to do this, you'll have to write your own code to run the program.
It's only a few lines that you can find at the bottom of
[`__init__.py`](https://github.com/AT0myks/pycallblock/blob/main/pycallblock/__init__.py).

### Callbacks

The main way is by making your own subclass of `Callblock`
and overriding the callbacks.

```py
from pycallblock import Callblock

class MyCallblock(Callblock):

    async def ring_callback(self, event):  # awaited in the modem's event loop
        # Choose what happens when the phone rings

    async def call_callback(self, call):  # awaited in the modem's event loop
        # Choose what happens when you receive a call

    async def mesg_callback(self, event):  # awaited in the modem's event loop
        # Choose what happens when you receive a MESG event

    async def dtmf_callback(self, dtmf, call):  # runs as a task
        # Choose what happens when you receive a DTMF during a call

    async def silence_callback(self, call):  # runs as a task
        # Choose what happens when silence has been detected during a call
```
In case you want to reuse the same CLI and options, two functions are available:
```py
from pycallblock import cli, options_from_args
from pycallblock.modem import Modem

args = cli()
options = options_from_args(args)
async with Modem("/dev/ttyACM0") as modem:
    mycallblock = MyCallblock(modem, ..., options=options)
```

### Also, you can make calls

```py
from pycallblock.modem import Modem, Mode

async with Modem("/dev/ttyACM0") as modem:
    wav = "outgoing.wav"  # Record to a file
    await modem.set_mode(Mode.VOICE)
    await modem.send("AT+VSM=1")
    await modem.start_voice_call("5055034455", wav=wav, max_duration=30)
    await modem.voice_end()
    await modem.hang_up()
```

## Notes

Here are some notes in no particular order.

- there is always one ring before the caller ID is sent
- your phone might have a setting to disable the first ring
- sometimes the phone still rings a few times after the call is blocked
- depending on your hardware, the recordings may contain annoying background noises
- the modem may report "fake" DSCs (this is a limitation of the way the modem works)
- I've had instances where the modem becomes unresponsive and I have to restart the Pi,
or the Pi also crashes and reboots
- sometimes the modem won't send BUSY after the caller hangs up,
in that case the call will last as long as the timeout
- for silence detection, as soon as there is noise the timer is reset
- in the modes where TX is active,
you'll always see one `Received DSC: Transmit Buffer Underrun` in the logs
- each call to `Modem.send_audio_file` puts the audio in a queue.
Each file is played one by one.
If the queue is not empty when the call is ending,
the remaining files in the queue won't be played
- `Callblock.start` installs a signal handler for `SIGINT` and `SIGTERM`.
When one of these signals is received,
the pycallblock instance will be marked as not running
- if a call is in progress when the program is exiting,
the call will terminate as soon as possible
- the modem's green LED turns on while a call is in progress
- the `+VSD` command for silence detection exists but does nothing,
so I implemented a very basic silence detection on my own
- I mostly used [this reference manual](https://web.archive.org/web/20160201002959/http://www.xmodus.ch/Downloads/XM3000S/XM3000S-A00-103.pdf)
- the country codes (unused for now) come from [this one](https://datasheet.octopart.com/CX93010-21Z-Conexant-datasheet-20734837.pdf)

## Glossary
- DCE: [Data circuit-terminating equipment](https://en.wikipedia.org/wiki/Data_circuit-terminating_equipment)
- DLE: Data link escape
- DSC: DLE shielded code.
- DTE: Data terminal equipment
- DTMF: [Dual-tone multi-frequency](https://en.wikipedia.org/wiki/Dual-tone_multi-frequency_signaling)
- PVF: Portable voice format
- RMD: Raw modem data
- RX: Receive
- TX: Transmit
