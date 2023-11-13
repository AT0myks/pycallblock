import asyncio
import csv
import functools
import logging
import random
import signal
import sqlite3
from argparse import ArgumentParser, ArgumentTypeError
from enum import Enum, auto
from logging.handlers import SysLogHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .modem import Modem, Mode
from .util import set_up_db, wav_duration

__version__ = "1.1.0"

_LOGGER = logging.getLogger("pycallblock")
_LOGGER.addHandler(logging.NullHandler())

SUBDIR_REC = "rec"
SUBDIR_PLAY = "play"


class BlockAction(Enum):
    FAX_MACHINE = auto()
    PLAY_MESSAGE = auto()
    RECORD = auto()
    TRANSMIT = auto()
    DUPLEX = auto()


class FilterType(Enum):
    BLACKLIST = auto()
    WHITELIST = auto()


class Callblock:

    def __init__(self, modem, filter_type, filter_list, workdir, db_cursor, options=None):
        options = options or {}
        self._modem = modem
        self._filter_type = filter_type
        with open(filter_list) as f:
            self._filter_list = dict(csv.reader(f))
        self._workdir = workdir
        self._db_cursor = db_cursor
        self._block_action = options.get("block_action")
        self._block_private = options.get("block_private", False)
        self._audio_file = options.get("audio_file")
        self._voice_duration = options.get("voice_duration", Modem.DEFAULT_MVD)
        self._silence = options.get("silence")
        self._timezone = options.get("timezone")
        self._running = False

    @property
    def running(self):
        return self._running

    async def set_up_modem(self):
        await self._modem.set_caller_id(True)
        if self._block_action is None:
            await self._modem.set_mode(Mode.DATA)
        elif self._block_action == BlockAction.FAX_MACHINE:
            if Mode.FAX_CLASS_2.value in self._modem.supported_modes:
                await self._modem.set_mode(Mode.FAX_CLASS_2)
            else:
                await self._modem.set_mode(Mode.FAX_CLASS_1)
        else:
            await self._modem.set_mode(Mode.VOICE)
            await self._modem.send("AT+VSM=1")

    async def start(self):
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            callback = functools.partial(self.signal_handler, signame)
            loop.add_signal_handler(getattr(signal, signame), callback)
        await self.set_up_modem()
        self._running = True
        self._modem.set_ring_callback(self.ring_callback)
        self._modem.set_call_callback(self.call_callback)
        self._modem.set_mesg_callback(self.mesg_callback)
        self._modem.set_dtmf_callback(self.dtmf_callback)
        self._modem.set_silence_callback(self.silence_callback)
        self._modem.silence_seconds = self._silence
        self._modem.start_event_loop()

    def stop(self):
        self._running = False

    def _log_call(self, call, blocked):
        values = (call.timestamp, call.number, call.name, blocked)
        self._db_cursor.execute("INSERT INTO call_log VALUES (?,?,?,?)", values)
        self._db_cursor.connection.commit()
        _LOGGER.info("Call logged")

    def block_reason(self, call):
        in_list = call.number in self._filter_list
        if call.is_private and self._block_private:
            return "Number is private"
        elif self._filter_type == FilterType.BLACKLIST and in_list:
            return "Contact is on blacklist"
        elif self._filter_type == FilterType.WHITELIST and not in_list:
            return "Contact is not on whitelist"
        return None

    async def block_call(self, call):
        _LOGGER.info(f"Blocking call from {call}")
        _LOGGER.info("Picking up")
        await self._modem.pick_up()
        voice = False
        wav = None
        if self._timezone is not None:
            dt = call.datetime.astimezone(self._timezone)
        else:
            dt = call.datetime
        dtstr = dt.strftime("%Y-%m-%d %H-%M-%S%z")  # Windows compatible file name
        if self._block_action == BlockAction.PLAY_MESSAGE:
            max_duration = wav_duration(self._audio_file)
            voice = await self._modem.start_voice_transmit(max_duration=max_duration)
            if voice:
                await self._modem.send_audio_file(self._audio_file)
        elif self._block_action == BlockAction.RECORD:
            _LOGGER.info(f"Starting recording for {self._voice_duration} seconds max")
            wav = self._workdir / SUBDIR_REC / f"{dtstr} {call.number}.wav"
            voice = await self._modem.start_voice_receive(wav, self._voice_duration)
        elif self._block_action == BlockAction.DUPLEX:
            _LOGGER.info(f"Starting duplex for {self._voice_duration} seconds max")
            wav = self._workdir / SUBDIR_REC / f"{dtstr} {call.number}.wav"
            voice = await self._modem.start_voice_duplex(wav, self._voice_duration)
            if voice and self._audio_file is not None:
                await self._modem.send_audio_file(self._audio_file)
        elif self._block_action == BlockAction.TRANSMIT:
            _LOGGER.info(f"Starting transmit for {self._voice_duration} seconds max")
            voice = await self._modem.start_voice_transmit(self._voice_duration)
            if voice and self._audio_file is not None:
                await self._modem.send_audio_file(self._audio_file)
        elif self._block_action == BlockAction.FAX_MACHINE:
            if self._modem.mode in (Mode.FAX_CLASS_1, Mode.FAX_CLASS_1_0):
                result = await self._modem.read_until_result()  # Read 'OK'
                _LOGGER.debug(f"{result=}")
        elif self._block_action is None:
            # Some modems need a delay between going off and on-hook.
            # See https://github.com/AT0myks/pycallblock/issues/3.
            await asyncio.sleep(1)
        if voice:
            await self._modem.voice_end()
        _LOGGER.info("Hanging up")
        await self._modem.hang_up()
        # A soft reset after a call seems to prevent
        # potential future issues in some cases.
        await self._modem.soft_reset()
        await self.set_up_modem()
        return wav if voice else None

    async def ring_callback(self, event):
        _LOGGER.info("Ringing...")

    async def call_callback(self, call):
        _LOGGER.info(f"Receiving call: {call}")
        reason = self.block_reason(call)
        if reason is not None:
            wav = await self.block_call(call)
            _LOGGER.info(f"{call} blocked because: {reason}")
        else:
            wav = None
        self._log_call(call, reason is not None)
        return wav

    async def mesg_callback(self, event):
        if "MESG" in event.data:
            nb = int(event.data["MESG"][-2:], 16)
            msg = f"Unread messages: {nb}"
        else:
            msg = "No more unread messages"
        _LOGGER.info(msg)
        return msg

    async def dtmf_callback(self, dtmf, call):
        ...

    async def silence_callback(self, call):
        if (file := random_file(self._workdir / SUBDIR_PLAY)) is not None:
            await self._modem.send_audio_file(file)

    def signal_handler(self, signame):
        _LOGGER.info(f"Received {signame}")
        if signame in ("SIGINT", "SIGTERM"):
            _LOGGER.info("Stopping...")
            self.stop()


def random_file(directory):
    try:
        return random.choice(list(directory.iterdir()))
    except IndexError:
        return None


def cli():
    def char_device(string):
        """Type for argparse."""
        if not Path(string).exists():
            raise ArgumentTypeError(f"{string} does not exist")
        if not Path(string).is_char_device():
            raise ArgumentTypeError(f"{string} is not a character device")
        return string

    parser = ArgumentParser(description="Block spam calls with a USB modem.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}", help="print version")
    parser.add_argument("--device", metavar="DEV", type=char_device, default=Modem.DEFAULT_DEV, help="the device to use. Default: %(default)s")
    parser.add_argument("--logfile", type=Path, metavar="FILE", help="enable logging to the specified file")
    parser.add_argument("--syslog", type=Path, metavar="ADDRESS", nargs='?', const="/dev/log", help="enable logging to syslog. Default: %(const)s")
    parser.add_argument("-p", "--block-private", action="store_true", help="block private numbers. Default: false")
    parser.add_argument("-l", "--stderr", action="store_true", help="log to stderr")
    parser.add_argument("-s", "--silence", type=float, metavar="SEC", help="seconds of silence before triggering the callback. Only for duplex. Must be an int or a float > 0. Default: disabled")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase verbosity")
    parser.add_argument("-a", "--audio-file", metavar="FILE", type=Path, help="WAV file for -m, -d and -t modes")
    parser.add_argument("--timezone", type=ZoneInfo, help="IANA timezone for the file names. Default: UTC")
    filter_types = parser.add_mutually_exclusive_group(required=True)
    filter_types.add_argument("-b", "--blacklist", metavar="CSV", type=Path, help="CSV file with the contacts that will be blocked")
    filter_types.add_argument("-w", "--whitelist", metavar="CSV", type=Path, help="CSV file with the contacts that will be allowed")
    dummy = parser.add_argument_group("Block actions", "Choose what happens when a call is blocked. If nothing is specified, default to instant hangup.")
    block_actions = dummy.add_mutually_exclusive_group()
    block_actions.add_argument("-f", "--fax", action="store_true", help="act as a fax machine")
    block_actions.add_argument("-m", "--message", action="store_true", help="play a message specified with -a")
    block_actions.add_argument("-r", "--record", type=int, nargs='?', const=Modem.DEFAULT_MVD, metavar="SEC", help="record the call for %(metavar)s seconds. Default: %(const)s")
    block_actions.add_argument("-d", "--duplex", type=int, nargs='?', const=Modem.DEFAULT_MVD, metavar="SEC", help="duplex for %(metavar)s seconds. Default: %(const)s")
    block_actions.add_argument("-t", "--transmit", type=int, nargs='?', const=Modem.DEFAULT_MVD, metavar="SEC", help="transmit for %(metavar)s seconds. Default: %(const)s")

    args = parser.parse_args()
    if args.message and args.audio_file is None:
        parser.error("no audio file specified for -m")

    return args


def options_from_args(args):
    if args.fax:
        block_action = BlockAction.FAX_MACHINE
    elif args.message:
        block_action = BlockAction.PLAY_MESSAGE
    elif args.record is not None:
        block_action = BlockAction.RECORD
    elif args.duplex is not None:
        block_action = BlockAction.DUPLEX
    elif args.transmit is not None:
        block_action = BlockAction.TRANSMIT
    else:
        block_action = None
    return {
        "audio_file": args.audio_file,
        "block_action": block_action,
        "block_private": args.block_private,
        "filter_list": args.blacklist or args.whitelist,
        "filter_type": FilterType.BLACKLIST if args.blacklist else FilterType.WHITELIST,
        "voice_duration": args.record or args.duplex or args.transmit,
        "silence": args.silence,
        "timezone": args.timezone
    }


async def _main():
    args = cli()

    _LOGGER.setLevel(logging.DEBUG if args.verbose > 0 else logging.INFO)

    if args.stderr:
        hdlr_print = logging.StreamHandler()
        hdlr_print.setLevel(logging.DEBUG)
        hdlr_print.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        _LOGGER.addHandler(hdlr_print)
    if args.syslog is not None:
        hdlr_syslog = SysLogHandler(address=str(args.syslog))
        hdlr_syslog.setLevel(logging.DEBUG)
        hdlr_syslog.setFormatter(logging.Formatter("%(name)s[%(process)d]: %(message)s"))
        hdlr_syslog.append_nul = False
        _LOGGER.addHandler(hdlr_syslog)
    if args.logfile is not None:
        hdlr_file = logging.FileHandler(args.logfile)
        hdlr_file.setLevel(logging.DEBUG)
        hdlr_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        _LOGGER.addHandler(hdlr_file)

    options = options_from_args(args)

    _LOGGER.info(f"pycallblock {__version__} is starting")

    _LOGGER.info(f"Audio file: {options['audio_file']}")
    _LOGGER.info(f"Block action: {options['block_action']}")
    _LOGGER.info(f"Block private numbers: {options['block_private']}")
    _LOGGER.info(f"Filter list: {options['filter_list']}")
    _LOGGER.info(f"Filter type: {options['filter_type']}")
    _LOGGER.info(f"Max voice duration: {options['voice_duration']}")
    _LOGGER.info(f"Silence seconds: {options['silence']}")
    _LOGGER.info(f"Timezone: {options['timezone']}")

    workdir = Path.home() / "pycallblock"
    workdir.mkdir(exist_ok=True)
    (workdir / SUBDIR_REC).mkdir(exist_ok=True)
    (workdir / SUBDIR_PLAY).mkdir(exist_ok=True)

    db = sqlite3.connect(workdir / "pycallblock.db")
    set_up_db(db)
    async with Modem(args.device) as modem:
        pycallblock = Callblock(
            modem,
            options["filter_type"],
            options["filter_list"],
            workdir,
            db.cursor(),
            options
        )
        await pycallblock.start()
        while pycallblock.running:
            await asyncio.sleep(.1)
    db.close()
    _LOGGER.info("pycallblock stopped")


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
