#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-
"""
Serial Client – handles serial communication with GNSS receivers.
Parses NMEA sentences (GGA, GSA, GSV) for position, satellite info, DOP.

Design rules:
  - __init__ must NOT access any Qt widgets (called from background thread).
  - All widget updates (RX bytes/s) go via rx_callback – emitted as Qt signal.
  - Qt signal connections (cbSendCorrection) are wired by the caller in the
    main thread, AFTER the object is created.
"""

import threading
import serial
import time
from . import pynmea2


class NtripSerialStream:

    def __init__(self, port: str, baudrate: int,
                 send_correction: bool = True, rx_callback=None):
        """
        Parameters
        ----------
        port            : COM port string, e.g. 'COM7'
        baudrate        : integer baud rate
        send_correction : initial state of "send correction data"
        rx_callback     : callable(bytes_per_sec: int) – called once per second
                          from the RX-counter thread.  Must be thread-safe
                          (emit a Qt signal, not touch widgets directly).
        """
        self.port = port
        self.baudrate = baudrate
        self._send_correction = send_correction
        self._sc_lock = threading.Lock()
        self.rx_callback = rx_callback

        # Open port – raises on failure; caller handles the exception
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=1,
            write_timeout=5,
            inter_byte_timeout=0.1,
        )

        # Counters / file
        self._data_received = 0
        self._data_sent = 0  # RTCM correction bytes sent
        self._data_lock = threading.Lock()
        self.logData = False
        self.file = None

        # Satellite / DOP state
        self.satellites: list = []
        self._gsv_buffer: list = []
        self._gsv_expected: dict = {}  # talker -> expected message count
        self.hdop = 0.0
        self.vdop = 0.0
        self.pdop = 0.0

        # Event listener lists
        self.events: list = []      # GGA position dicts
        self.rawevents: list = []   # raw NMEA bytes
        self.sat_events: list = []  # GSV satellite lists
        self.dop_events: list = []  # GSA DOP dicts

        # Shared stop flag
        self._stop = threading.Event()

        # Reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f'NtripSerial-{port}',
            daemon=True,
        )
        self._reader_thread.start()

        # RX bytes/s counter thread
        self._rx_thread = threading.Thread(
            target=self._rx_counter_loop,
            name=f'NtripSerialRx-{port}',
            daemon=True,
        )
        self._rx_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def send_correction(self):
        with self._sc_lock:
            return self._send_correction

    @send_correction.setter
    def send_correction(self, value: bool):
        with self._sc_lock:
            self._send_correction = value

    def get_stats(self):
        """Get RX/TX statistics."""
        with self._data_lock:
            return {'rx_bytes': self._data_received, 'tx_bytes': self._data_sent}

    def stop(self):
        """Stop all threads and close the serial port."""
        self._stop.set()
        try:
            if self.serial.is_open:
                self.serial.close()
        except Exception:
            pass
        if self.file and not self.file.closed:
            try:
                self.file.close()
            except Exception:
                pass

    # Alias kept for backward compatibility
    def stopSerialStream(self):
        self.stop()

    def writeToStream(self, txdata: bytes):
        """Write RTCM correction data to the serial port with proper error handling."""
        with self._sc_lock:
            if not self._send_correction:
                return
        
        if not txdata or len(txdata) == 0:
            return
        
        try:
            if not self.serial.is_open:
                return
            
            # Write in chunks to avoid buffer overflow
            # RTCM messages are typically 100-300 bytes, use 128-byte chunks
            bytes_written = 0
            chunk_size = 128
            for i in range(0, len(txdata), chunk_size):
                chunk = txdata[i:i+chunk_size]
                try:
                    n = self.serial.write(chunk)
                    bytes_written += n
                except serial.SerialTimeoutException:
                    return
                except serial.SerialException:
                    return
                except Exception:
                    return
            
            self.serial.flush()  # Ensure all data is sent
            
            # Track sent bytes
            with self._data_lock:
                self._data_sent += bytes_written
            
        except Exception:
            pass

    def openFile(self, file_path: str):
        self.file = open(file_path, 'ab')

    def writeToFile(self, data: bytes):
        if self.logData and self.file and not self.file.closed:
            try:
                self.file.write(data)
            except Exception:
                pass

    def closeFile(self):
        if self.file and not self.file.closed:
            try:
                self.file.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------

    def registerEventListener(self, cb):
        self.events.append(cb)

    def registerRawEventListener(self, cb):
        self.rawevents.append(cb)

    def registerSatEventListener(self, cb):
        self.sat_events.append(cb)

    def registerDopEventListener(self, cb):
        self.dop_events.append(cb)

    # ------------------------------------------------------------------
    # Internal threads
    # ------------------------------------------------------------------

    def _reader_loop(self):
        buf = b''
        while not self._stop.is_set():
            try:
                if not self.serial.is_open:
                    break
                chunk = self.serial.read(256)
                if not chunk:
                    continue

                self.writeToFile(chunk)
                with self._data_lock:
                    self._data_received += len(chunk)

                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.rstrip(b'\r')
                    if line:
                        self._fire_raw(line)
                        if line.startswith(b'$'):
                            self._process_nmea(
                                line.decode('ascii', errors='ignore').strip())

            except serial.SerialException:
                # Port closed / device removed
                break
            except Exception:
                pass

    def _rx_counter_loop(self):
        while not self._stop.is_set():
            time.sleep(1)
            with self._data_lock:
                n = self._data_received
                self._data_received = 0
            if self.rx_callback and not self._stop.is_set():
                try:
                    self.rx_callback(n)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # NMEA processing
    # ------------------------------------------------------------------

    def _process_nmea(self, line: str):
        try:
            msg = pynmea2.parse(line)
            if isinstance(msg, pynmea2.types.talker.GGA):
                self._process_gga(msg)
            elif isinstance(msg, pynmea2.types.talker.GSA):
                self._process_gsa(msg)
            elif isinstance(msg, pynmea2.types.talker.GSV):
                self._process_gsv(msg)
            else:
                # Debug: show what sentence types we're getting
                sentence_type = line.split(',')[0] if line else 'unknown'
                # Don't spam log for GLL, RMC, VTG etc
                if 'GGA' not in sentence_type and 'GSV' not in sentence_type and 'GSA' not in sentence_type:
                    pass  # silently ignore
        except pynmea2.ParseError:
            pass
        except Exception:
            pass

    def _process_gga(self, msg):
        try:
            self._fire_events({
                'lat':      msg.latitude,
                'lon':      msg.longitude,
                'alt':      float(msg.altitude)       if msg.altitude       else 0.0,
                'fixtype':  int(msg.gps_qual)         if msg.gps_qual       else 0,
                'num_sats': int(msg.num_sats)         if msg.num_sats       else 0,
                'hdop':     float(msg.horizontal_dil) if msg.horizontal_dil else 0.0,
            })
        except Exception:
            pass

    def _process_gsa(self, msg):
        try:
            pdop = float(msg.pdop) if msg.pdop else 0.0
            hdop = float(msg.hdop) if msg.hdop else 0.0
            vdop = float(msg.vdop) if msg.vdop else 0.0
            self.pdop, self.hdop, self.vdop = pdop, hdop, vdop
            self._fire_dop({'pdop': pdop, 'hdop': hdop, 'vdop': vdop})
        except Exception:
            pass

    def _process_gsv(self, msg):
        try:
            num_messages = int(msg.num_messages)
            msg_num      = int(msg.msg_num)
            talker = getattr(msg, 'talker', 'GP')
            # On first message of a constellation: reset only that constellation's buffer
            if msg_num == 1:
                # Remove existing entries from this talker
                self._gsv_buffer = [s for s in self._gsv_buffer
                                    if not s['prn'].startswith(talker)]
                self._gsv_expected[talker] = num_messages
            for i in range(1, 5):
                prn = getattr(msg, f'sv_prn_num_{i}', None)
                if not prn:
                    continue
                elev = getattr(msg, f'elevation_deg_{i}', None)
                azim = getattr(msg, f'azimuth_{i}',      None)
                snr  = getattr(msg, f'snr_{i}',          None)
                self._gsv_buffer.append({
                    'prn':       f'{talker}{prn}',
                    'elevation': float(elev) if elev else 0.0,
                    'azimuth':   float(azim) if azim else 0.0,
                    'snr':       float(snr)  if snr  else 0.0,
                })
            if msg_num >= self._gsv_expected.get(talker, 1):
                self.satellites = self._gsv_buffer[:]
                self._fire_sat(self.satellites)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fire helpers – call each listener, swallow individual errors
    # ------------------------------------------------------------------

    def _fire_events(self, data):
        for cb in list(self.events):
            try:
                cb(data)
            except Exception:
                pass

    def _fire_raw(self, data):
        for cb in list(self.rawevents):
            try:
                cb(data)
            except Exception:
                pass

    def _fire_sat(self, data):
        for cb in list(self.sat_events):
            try:
                cb(data)
            except Exception:
                pass

    def _fire_dop(self, data):
        for cb in list(self.dop_events):
            try:
                cb(data)
            except Exception:
                pass
