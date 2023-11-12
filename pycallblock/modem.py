import asyncio
import audioop  # Will be removed in 3.13
import json
import logging
import re
import time
import wave
from datetime import datetime, timezone
from enum import Enum, auto
from importlib import resources

from serial_asyncio import open_serial_connection

from .util import wav_duration

_LOGGER = logging.getLogger("pycallblock")

DLE = b'\x10'

with resources.files("pycallblock").joinpath("shielded_codes_to_dte.json").open('r', encoding="utf8") as f:
    SHIELDED_CODES_TO_DTE = json.load(f)


class ResultCode(Enum):
    OK = "OK"
    CONNECT = "CONNECT"  # Intermediate
    RING = "RING"  # Unsolicited
    NO_CARRIER = "NO CARRIER"
    ERROR = "ERROR"
    DIGITAL_LINE_DETECTED = "DIGITAL LINE DETECTED"  # Unsolicited

    def __str__(self):
        return self.value


class Mode(Enum):
    DATA = '0'
    FAX_CLASS_1 = '1'
    FAX_CLASS_1_0 = "1.0"
    FAX_CLASS_2 = '2'
    VOICE = '8'


class State(Enum):
    INITIALIZED = auto()
    WAITING_EVENT = auto()
    VOICE_COMMAND = auto()
    VOICE_RECEIVE = auto()
    VOICE_TRANSMIT = auto()
    VOICE_DUPLEX = auto()


class DTMF(Enum):
    DTMF_0 = '0'
    DTMF_1 = '1'
    DTMF_2 = '2'
    DTMF_3 = '3'
    DTMF_4 = '4'
    DTMF_5 = '5'
    DTMF_6 = '6'
    DTMF_7 = '7'
    DTMF_8 = '8'
    DTMF_9 = '9'
    DTMF_A = 'A'
    DTMF_B = 'B'
    DTMF_C = 'C'
    DTMF_D = 'D'
    DTMF_E = '*'
    DTMF_F = '#'


class Call:

    def __init__(self, number, name, timestamp=None):
        self._timestamp = timestamp or time.time_ns()
        self._number = number
        self._name = name

    def __str__(self):
        nmbr = "Private" if self.is_private else self._number
        if self._name is None:
            return nmbr
        else:
            return nmbr + '/' + self._name

    def __repr__(self):
        return f"{self.__class__.__qualname__}({self._number!r}, {self._name!r}, {self._timestamp!r})"

    @property
    def number(self):
        return self._number

    @property
    def name(self):
        return self._name

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def datetime(self):
        return datetime_from_ns(self._timestamp)

    @property
    def is_private(self):
        return self._number in ('P', 'O')

    @classmethod
    def from_event(cls, event):
        return cls(event.data.get("NMBR"), event.data.get("NAME"), event.timestamp)


class Event:

    def __init__(self, data, bytes_, timestamp=None):
        self._timestamp = timestamp or time.time_ns()
        self._data = data
        self._raw = bytes_

    def __repr__(self):
        return f"{self.__class__.__qualname__}({self._data!r}, {self._raw!r}, {self._timestamp!r})"

    @property
    def data(self):
        return self._data

    @property
    def raw(self):
        return self._raw

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def datetime(self):
        return datetime_from_ns(self._timestamp)

    @property
    def is_ring(self):
        try:
            rc = ResultCode(self._data)
        except ValueError:
            return self._data == Modem.RING.decode()
        else:
            return rc == ResultCode.RING

    @property
    def is_message_event(self):
        return "MSG_WAITING" in self._data

    @property
    def is_call(self):
        return "DATE" in self._data and "TIME" in self._data and not self.is_message_event

    @property
    def is_shielded_code(self):
        return len(self._data) == 2 and self._data.startswith(DLE.decode())

    @classmethod
    def from_bytes(cls, bytes_):
        data = bytes_.strip().decode()
        if " = " not in data:
            return cls(data, bytes_)
        dict_ = {}
        for part in re.split("[\r\n]+", data):
            key, val = part.split(" = ")
            dict_[key] = val
        return cls(dict_, bytes_)


class CommandResponse:

    def __init__(self, cmd, data, result_code, raw):
        self._cmd = cmd
        self._data = data
        self._result_code = result_code
        self._raw = raw

    def __repr__(self):
        return f"{self.__class__.__qualname__}({self._cmd!r}, {self._data!r}, {self._result_code!r}, {self._raw!r})"

    @property
    def cmd(self):
        return self._cmd

    @property
    def data(self):
        return self._data

    @property
    def result_code(self):
        return self._result_code

    @property
    def raw(self):
        return self._raw

    @property
    def is_ok(self):
        return self._result_code == ResultCode.OK

    @classmethod
    def from_bytes(cls, bytes_):
        # Assuming S3 is '\r' and S4 is '\n'.
        split = re.split("[\r\n]+", bytes_.strip().decode())
        cmd = split.pop(0)
        result_code = ResultCode(split.pop())
        data = '\n'.join(split) if split else None
        if data is not None:
            extra = cmd.replace(Modem.CLPREFIX, '').strip("=?") + ": "
            # Some responses begin with the command, so we keep only the data.
            data = data.removeprefix(extra)
        return cls(cmd, data, result_code, bytes_)


class Modem:

    # From Section 2.1 "Alphabet":
    # "Lower-case characters are considered identical to their
    # upper-case equivalents when received by the modem from the DTE."
    # Let's go with everything uppercase.
    CLPREFIX = "AT"  # Command line prefix

    # <DLE> Shielded Codes Sent to the Modem (DCE)
    #   <DLE><ETX> (Data link escape and End of text)
    #   PC to modem: end voice transmit state
    #   modem to PC: end data state
    END_STREAM = DLE + b'\x03'
    #   <DLE>! Receive or Transmit abort
    END_RECORD = DLE + b'\x21'
    #   <DLE>^ Full duplex abort
    END_DUPLEX = DLE + b'\x5e'
    #   <DLE>b BUSY
    BUSY = DLE + b'b'
    #   <DLE>R RING
    RING = DLE + b'R'

    MAX_READ = 1024

    # From Section 2.2.1 "Command Line General Format":
    # "The termination character may be selected by a user option
    # (parameter S3), the default being CR."
    # It is highly unlikely that it is not this character
    # (unless someone has previously set it to something else).
    DEFAULT_S3 = '\r'
    DEFAULT_S4 = '\n'
    DEFAULT_VGT = 128  # Transmit gain
    DEFAULT_VGR = 128  # Receive gain
    DEFAULT_MVD = 120  # Max voice duration, in seconds
    DEFAULT_DEV = "/dev/ttyACM0"

    def __init__(self, device=DEFAULT_DEV, baudrate=230400, s3=DEFAULT_S3, s4=DEFAULT_S4):
        self._device = device
        self._baudrate = baudrate
        self._s3 = s3  # S3 register
        self._s4 = s4  # S4 register
        self._reader = None
        self._writer = None
        self._state = None
        self._running = False
        self._event_loop = None
        self._stop_event = None
        self._background_tasks = set()
        self._silence = False
        self._silence_seconds = None
        self._mode = None
        self._supported_modes = None
        self._last_dtmf = None
        self._write_lock = asyncio.Lock()
        self._play_queue = asyncio.Queue()

    def __repr__(self):
        return f"{self.__class__.__qualname__}({self._device!r})"

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @property
    def serial(self):
        return self._writer.transport.serial if self._writer is not None else None

    @property
    def mode(self):
        return self._mode

    @property
    def supported_modes(self):
        return self._supported_modes

    @property
    def state(self):
        return self._state

    @property
    def silence_seconds(self):
        return self._silence_seconds

    @silence_seconds.setter
    def silence_seconds(self, value):
        self._silence_seconds = value

    @property
    def _header(self):
        return self._s3 + self._s4

    @property
    def _trailer(self):
        return self._s3 + self._s4

    async def open(self):
        self._reader, self._writer = await open_serial_connection(url=self._device, baudrate=self._baudrate, exclusive=True)
        # Sometimes there is data in the buffer when opening,
        # for example when starting the program while the phone is ringing.
        if extra := await self.read(timeout=.2):
            _LOGGER.warning(f"Discarded data at initialization: {extra!r}")
        try:
            await asyncio.wait_for(self.soft_reset(), 1)
        except asyncio.TimeoutError:
            err = "Timeout trying to communicate with device"
            _LOGGER.error(err)
            self._writer.close()
            raise TimeoutError(err) from None
        _LOGGER.debug(f"Device: {self._device!r}")
        _LOGGER.debug(f"{await self.get_masked_firmware_id_code()=}")
        _LOGGER.debug(f"{await self.get_product_manufacturer()=}")
        self._supported_modes = await self.get_supported_modes()
        _LOGGER.debug(f"{self._supported_modes=}")
        self._running = True

    async def close(self):
        self._running = False
        if self._event_loop is not None:
            await self.stop_event_loop()
        await self.soft_reset()
        self._writer.close()
        await self._writer.wait_closed()

    async def read(self, n=None, timeout=None):
        try:
            return await asyncio.wait_for(self._reader.read(n or self.MAX_READ), timeout)
        except asyncio.TimeoutError:
            return b''

    async def read_until_result(self):
        data = b''
        while not any((code.value + self._trailer).encode() in data for code in ResultCode):
            data += await self.read(1)
        return data

    async def read_until_trailer(self, timeout=None):
        """Read until the data ends with trailer or the timeout expires."""
        data = b''

        async def read():
            nonlocal data
            while not data.endswith(self._trailer.encode()):
                data += await self.read()
        try:
            await asyncio.wait_for(read(), timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("Timed out waiting for trailer")
        return data

    async def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    def _commandify(self, cmd):
        """Add the necessary command characters if not present."""
        cmd = cmd.upper()
        if not cmd.startswith(self.CLPREFIX):
            cmd = self.CLPREFIX + cmd
        if not cmd.endswith(self._s3):
            cmd += self._s3
        return cmd

    async def send(self, commands):
        if not isinstance(commands, (list, tuple)):
            commands = [commands]
        commands = [self._commandify(cmd) for cmd in commands]
        responses = []
        await self.write(';'.join(commands))
        for cmd in commands:
            data = (await self.read_until_result()).strip(b';')
            while cmd.encode() not in data:
                # Can catch things like DIGITAL LINE DETECTED.
                _LOGGER.warning(f"Unsolicited: {data}")
                data = (await self.read_until_result()).strip(b';')
            extra, echo, response = data.partition(cmd.encode())
            if extra:
                _LOGGER.warning(f"Ignored data: {extra}")
            res = CommandResponse.from_bytes(echo + response)
            _LOGGER.debug(repr(res))
            responses.append(res)
        return responses if len(responses) > 1 else responses[0]

    async def get(self, cmd):
        return (await self.send(cmd)).data

    async def soft_reset(self):
        await self.send("ATZ")
        self._mode = await self.get_mode()
        self._state = State.INITIALIZED  # Useless state?
        self._s3 = self.DEFAULT_S3
        self._s4 = self.DEFAULT_S4

    async def get_masked_firmware_id_code(self):
        return await self.get("I3")

    async def get_product_manufacturer(self):
        return await self.get("+GMI")

    async def get_supported_modes(self):
        return (await self.get("+FCLASS=?")).split(',')

    async def get_info(self):
        """Get some info about the modem."""
        return {
            "capabilities": (await self.get("+GCAP")).split(','),
            # From https://en.wikipedia.org/wiki/Network_Caller_ID#Modems.
            "chipset_firmware_patch_version": await self.get("-PV"),
            "conexant_id": await self.get("+GMI9"),
            "country_code_current": await self.get_install_country(),
            "country_code_supported": (await self.get("+GCI=?")).strip("()").split(','),
            # Same as +GMR, +FREV? and +FMR?, also called Product Revision.
            # Only I3 and +GMR work in every mode and I3 doesn't need to be parsed.
            "masked_firmware_id_code": await self.get_masked_firmware_id_code(),
            "mode_current": self._mode,
            "mode_supported": self._supported_modes,
            "product_code": await self.get("I0"),
            # Same as +FMI? and +FMFR? but works in every mode.
            "product_manufacturer": await self.get_product_manufacturer(),
            # Same as +FMM? and +FMDL? but works in every mode.
            "product_model": await self.get("+GMM"),
        }

    async def get_mode(self):
        return Mode(await self.get("+FCLASS?"))

    async def set_mode(self, mode):
        """Set the modem mode.

        0  : data (default)
        1  : fax class 1
        1.0: fax class 1.0
        2  : fax class 2
        8  : voice
        Some commands only work in certain modes
        (see tables 4-1, 5-1 and 6-1).
        """
        if (await self.send(f"+FCLASS={mode.value}")).is_ok:
            self._mode = mode
            _LOGGER.info(f"Mode has been set to {mode}")
            return True
        else:
            _LOGGER.error(f"Could not set mode to {mode}")
            return False

    async def get_install_country(self):
        return await self.get("+GCI?")

    async def set_install_country(self, country_code):
        return (await self.send(f"+GCI={country_code}")).is_ok

    async def get_caller_id(self):
        return await self.get("AT+VCID?")

    async def set_caller_id(self, bool_):
        return (await self.send(f"AT+VCID={int(bool_)}")).is_ok

    async def pick_up(self):
        if self._mode in (Mode.FAX_CLASS_1, Mode.FAX_CLASS_1_0, Mode.FAX_CLASS_2):
            await self.send("ATA")
        elif self._mode == Mode.VOICE:
            await self.send("+VLS=1")
        else:
            await self.send("ATH1")

    async def hang_up(self):
        if self._mode == Mode.VOICE:
            await self.send("+VLS=0")
        else:
            await self.send("ATH0")

    def _catch_dtmf(self, code):
        if code == '/':  # Start of DTMF tone shielding
            self._last_dtmf = code
        elif code == '~' and isinstance(self._last_dtmf, DTMF):
            self.create_background_task(self.dtmf_callback(self._last_dtmf), "dtmf_callback")
            self._last_dtmf = None
        elif code in (item.value for item in DTMF) and self._last_dtmf == '/':
            self._last_dtmf = DTMF(code)
        else:
            self._last_dtmf = None

    def _catch_dsc(self, matchobj):
        code = matchobj.group(1)
        _LOGGER.info(f"Received DSC: {get_dsc_label(code.decode())}")
        if code == self.BUSY[1:]:
            self._stop_event = "busy"
        elif code == DLE:
            # Section 5.1.3 "The DTE must recognize <DLE><DLE>
            # and reinsert a single <DLE> in its place."
            return DLE
        else:
            self._catch_dtmf(code.decode())
        # "The DTE must filter the data stream from the DCE,
        # and remove all character pairs beginning with <DLE>."
        return b''

    async def _receive(self, max_duration=DEFAULT_MVD, wav=None):
        sampwidth = 1
        start = time.monotonic()
        audio = b''
        while self._stop_event is None:
            data = await self.read(timeout=.2)
            while data.endswith(DLE):  # Avoid cutting DSCs in half
                data += await self.read()
            new_audio = re.sub(DLE + b"(.)", self._catch_dsc, data)
            # 126 is for 255 RX gain, for 128 RX gain 127 is enough. RX gain 0 is weird.
            self._silence = audioop.rms(new_audio, sampwidth) >= 126
            audio += new_audio
            if time.monotonic() - start >= max_duration:
                self._stop_event = "timeout"
            elif not self._running:
                self._stop_event = "exit"
        if self._state != State.VOICE_TRANSMIT:
            _LOGGER.debug(f"{self._stop_event=}")
            await self.write(self.END_DUPLEX if self._state == State.VOICE_DUPLEX else self.END_RECORD)
            end = await self.read_until_result()
            if self.END_STREAM in end:
                to_write, ok = end.rsplit(self.END_STREAM, maxsplit=1)
            else:
                _LOGGER.warning("END_STREAM not found")
                ok = self._header + ResultCode.OK.value + self._trailer
                to_write = end.removesuffix(ok.encode())
            audio += to_write
            self._state = State.VOICE_COMMAND
        if wav is not None and audio:
            with wave.open(str(wav), "wb") as w:
                # With these settings, 8000 bytes is 1 second of audio.
                w.setnchannels(1)  # mono
                w.setsampwidth(sampwidth)  # 8bit
                w.setframerate(8000)  # 8000Hz
                w.writeframes(audio)

    async def _transmit(self, max_duration=DEFAULT_MVD):
        start = time.monotonic()
        filler = silence(.2)
        minimum = len(silence(.5))
        while self._stop_event is None:
            if self.serial.out_waiting < minimum:
                await self.write(filler)  # Send 200ms of silence
            if time.monotonic() - start >= max_duration:
                self._stop_event = "timeout"
            elif not self._running:
                self._stop_event = "exit"
            await asyncio.sleep(.1)
        if self._state == State.VOICE_TRANSMIT:
            _LOGGER.debug(f"{self._stop_event=}")
            await self.write(self.END_STREAM)
            await asyncio.sleep(.5)  # Wait for RX to finish
            await self.read_until_result()
            self._state = State.VOICE_COMMAND

    async def _silence_detection(self):
        start = time.monotonic()
        while self._stop_event is None:
            sec = self._silence_seconds
            sd_on = isinstance(sec, (int, float)) and sec > 0
            if not self._silence or not sd_on:  # Reset conditions
                start = time.monotonic()
            elif sd_on and time.monotonic() - start > sec:
                self.create_background_task(self.silence_callback(), "silence_callback")
                start = time.monotonic()
            await asyncio.sleep(.1)

    async def start_voice_receive(self, wav=None, max_duration=DEFAULT_MVD, rx_gain=DEFAULT_VGR):
        if self._mode != Mode.VOICE:
            _LOGGER.error("Can't start voice receive when modem isn't in voice mode")
            return False
        await self.send(f"+VGR={rx_gain}")
        if (await self.send("+VRX")).result_code != ResultCode.CONNECT:
            _LOGGER.error("Could not start voice reception")
            return False
        self._state = State.VOICE_RECEIVE
        self._stop_event = None
        self.create_background_task(self._receive(max_duration, wav), "RX")
        return True

    async def start_voice_duplex(self, wav=None, max_duration=DEFAULT_MVD, tx_gain=DEFAULT_VGT, rx_gain=DEFAULT_VGR):
        if self._mode != Mode.VOICE:
            _LOGGER.error("Can't start voice duplex when modem isn't in voice mode")
            return False
        await self.send([f"+VGR={rx_gain}", f"+VGT={tx_gain}"])
        if (await self.send("+VTR")).result_code != ResultCode.CONNECT:
            _LOGGER.error("Could not start voice duplex")
            return False
        self._state = State.VOICE_DUPLEX
        self._stop_event = None
        self.create_background_task(self._receive(max_duration, wav), "RX")
        self.create_background_task(self._transmit(max_duration), "TX")
        self.create_background_task(self._silence_detection(), "SD")
        self.create_background_task(self._play_queue_consumer(), "play_queue")
        return True

    async def start_voice_transmit(self, max_duration=DEFAULT_MVD, tx_gain=DEFAULT_VGT):
        if self._mode != Mode.VOICE:
            _LOGGER.error("Can't start voice transmit when modem isn't in voice mode")
            return False
        await self.send(f"+VGT={tx_gain}")
        if (await self.send("+VTX")).result_code != ResultCode.CONNECT:
            _LOGGER.error("Could not start voice transmit")
            return False
        self._state = State.VOICE_TRANSMIT
        self._stop_event = None
        self.create_background_task(self._receive(max_duration), "RX")
        self.create_background_task(self._transmit(max_duration), "TX")
        self.create_background_task(self._play_queue_consumer(), "play_queue")
        return True

    async def _play_queue_consumer(self):
        while self._stop_event is None or not self._play_queue.empty():
            try:
                audio, duration = await asyncio.wait_for(self._play_queue.get(), 1)
            except asyncio.TimeoutError:
                pass
            else:
                if self._stop_event is None:
                    sec = self._silence_seconds
                    self._silence_seconds = -abs(sec) if sec is not None else None
                    await self.write(audio)
                    await asyncio.sleep(duration)
                    self._silence_seconds = abs(sec) if sec is not None else None
                self._play_queue.task_done()

    async def send_audio_file(self, audio_file):
        if audio_file is None:
            _LOGGER.error("Cannot send audio file: no audio file specified")
            return False
        if self._state not in (State.VOICE_TRANSMIT, State.VOICE_DUPLEX):
            _LOGGER.error(f"Cannot send audio file in state {self._state}")
            return False
        try:
            wav = wave.open(str(audio_file), "rb")
        except FileNotFoundError:
            _LOGGER.error(f"Audio file not found: {audio_file}")
            return False
        except wave.Error as e:
            _LOGGER.error(f"WAV error for file {audio_file}: {e}")
            return False
        else:
            _LOGGER.info(f"Sending audio file: {audio_file}")
            duration = wav_duration(audio_file)
            with wav:
                await self._play_queue.put((wav.readframes(wav.getnframes()), duration))
            return True

    async def start_voice_call(self, number, wav=None, max_duration=DEFAULT_MVD, tx_gain=DEFAULT_VGT, rx_gain=DEFAULT_VGR):
        if self._event_loop is not None:
            _LOGGER.error("Can't call while event loop is running")
            return False
        if self._mode != Mode.VOICE:
            _LOGGER.error("Can't start call when modem isn't in voice mode")
            return False
        # Hide caller ID
        # 141 UK https://www.bt.com/help/landline/calling-features-and-security/how-do-i-withhold-my-telephone-number-
        # *67 US
        await self.send(f"DT{number}")
        return await self.start_voice_duplex(wav, max_duration, tx_gain, rx_gain)

    async def stop_voice(self):
        self._stop_event = "stop"

    async def voice_end(self):
        if self._state in (State.VOICE_RECEIVE, State.VOICE_TRANSMIT, State.VOICE_DUPLEX):
            while self._state != State.VOICE_COMMAND:
                await asyncio.sleep(.1)

    async def ring_callback(self, event):
        ...

    async def call_callback(self, call):
        ...

    async def mesg_callback(self, event):
        ...

    async def dtmf_callback(self, dtmf):
        ...

    async def silence_callback(self):
        ...

    def set_ring_callback(self, func):
        self.ring_callback = func

    def set_call_callback(self, func):
        self.call_callback = func

    def set_mesg_callback(self, func):
        self.mesg_callback = func

    def set_dtmf_callback(self, func):
        self.dtmf_callback = func

    def set_silence_callback(self, func):
        self.silence_callback = func

    def start_event_loop(self):
        if self._event_loop is not None:
            _LOGGER.error("Event loop is already running")
            return

        def done_callback(event_loop_task):
            if event_loop_task._exception is not None:
                self._event_loop = None
                self.start_event_loop()
        self._event_loop = self.create_background_task(self.event_loop(), "event_loop")
        self._event_loop.add_done_callback(done_callback)

    async def stop_event_loop(self):
        if self._event_loop is None:
            _LOGGER.error("Event loop is already stopped")
            return
        if self._state == State.WAITING_EVENT:
            self._event_loop.cancel()
        await self._event_loop
        self._event_loop = None

    async def event_loop(self):
        while self._running:
            _LOGGER.info("Waiting event...")
            self._state = State.WAITING_EVENT
            try:
                bytes_ = await self.read()
            except asyncio.CancelledError:
                break
            if not bytes_.endswith(self._trailer.encode()):
                bytes_ += await self.read_until_trailer(.01)
            try:
                event = Event.from_bytes(bytes_)
            except ValueError:
                _LOGGER.exception(f"Could not parse event data: {bytes_!r}")
                continue
            _LOGGER.debug(repr(event))
            if event.is_ring:
                await self.ring_callback(event)
            elif event.is_call:
                await self.call_callback(Call.from_event(event))
            elif event.is_message_event:
                await self.mesg_callback(event)
            elif event.is_shielded_code:
                _LOGGER.info("Received shielded code: " + get_dsc_label(event.data[1]))
            else:
                _LOGGER.warning(f"Unknown event data: {event.data!r}")

    def create_background_task(self, coro, name=None):
        def done_callback(task):
            try:
                task.result()
            except asyncio.CancelledError:
                _LOGGER.warning(f"Background task {task.get_name()} was cancelled")
            except Exception:
                _LOGGER.exception(f"Exception in background task {task.get_name()}")
            else:
                _LOGGER.debug(f"Background task {task.get_name()} is done")
            self._background_tasks.discard(task)
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(done_callback)
        return task


def get_dsc_label(code: str):
    try:
        return SHIELDED_CODES_TO_DTE[code]
    except KeyError:
        _LOGGER.warning(f"Received unknown DSC: {code}")
        return None


def datetime_from_ns(nanoseconds):
    """Return an aware datetime from a number of nanoseconds since the epoch."""
    return datetime.fromtimestamp(nanoseconds/1000000000, timezone.utc)


def silence(seconds=1, framerate=8000):
    """Return bytes that represent approximately `seconds` seconds of silence.

    Only for 8 bit unsigned audio.
    """
    return b'\x80' * int(seconds * framerate)
