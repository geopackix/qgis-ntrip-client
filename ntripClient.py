#!/usr/bin/env -S python3 -u

import socket
import sys
import datetime
import base64
import time
import os
import ssl
import datetime
import threading
import base64

version=0.51

useragent="ntrip-QGIS-/%.1f" % version


factor=2 # How much the sleep time increases with each failed attempt
maxReconnect=3
maxReconnectTime=30
sleepTime=1 # So the first one is 1 second

class NtripClient(object):
    def __init__(self,
                 buffer=1024,
                 user="",
                 out=sys.stdout,
                 port=2101,
                 caster="",
                 mountpoint="",
                 host=False,
                 lat=0,
                 lon=0,   
                 height=0,
                 ssl=False,
                 verbose=True,
                 V2=False,
                 headerFile=sys.stderr,
                 headerOutput=False,
                 maxConnectTime=0,
                 streams = [],
                 dockwidget = None
                 ):
        self.buffer=buffer
        self.user=base64.b64encode(bytes(user,'utf-8')).decode("utf-8")
        self.out=out
        self.port=port
        self.caster=caster
        self.mountpoint=mountpoint
        self.setPosition(lat, lon, height)
        self.height=height
        self.verbose=verbose
        self.ssl=ssl
        self.host=host
        self.V2=False

        if dockwidget.selectNtripVersion.currentText() == '2':
            self.V2 = True

        self.headerFile=headerFile
        self.headerOutput=headerOutput
        self.maxConnectTime=maxConnectTime
        
        self.serialStreams = list(streams)   # mutable – receiver can be added later
        
        self.socket:socket.socket
        
        self.events = []
        self.rawevents = []
        
        self.connectionState = False # indicates if ntrip connection has been established.
        
        self.dataReceived = 0
        
        self.dockwidget = dockwidget
        
        self.stop_event = threading.Event()
        self.uploadPositionThread = threading.Thread(target=self.positionUploadTask)
        self.uploadPositionThread.daemon = True  # makes the thread a daemon thread
        self.uploadPositionThread.start()   #start timer thread
        
        self.stopNtripConnection = threading.Event()
        self.NtripConnectionThread = threading.Thread(target=self._connection_loop,
                                                      name='NtripConnection', daemon=True)
        self.NtripConnectionThread.start()
        
        
        #Thread which calculates received data length per sencond
        self.stop_countrtcmrxevent = threading.Event()   
        self.countrtcmrxeventThread = threading.Thread(target=self.countRxData)
        self.countrtcmrxeventThread.daemon = True 
        self.countrtcmrxeventThread.start() 
        
        self.sendGGAToCaster = self.dockwidget.cbGGA.isChecked()
        
        self.logData = False
        self.file = None

    ###
    # Write log file
    ###
    
    def on_path_changed(self):
        pass

    def on_cb_changed(self):
        pass
        
    def openFile(self, file_path):
        self.file = open(file_path, 'ab')
        
    def writeToFile(self, data):
        if self.logData and self.file:
            if not self.file.closed:
                self.file.write(data)   
    def closeFile(self):
        if self.file and not self.file.closed:
            self.file.close()    
            
     
    
    def updateLatLon(self,longitude, latitude, height):
        self.setPosition(latitude, longitude, height)

    def setSendGGA(self, value: bool):
        """Update GGA send flag from main thread."""
        self.sendGGAToCaster = bool(value)

    @staticmethod
    def fetch_sourcetable(host, port, user='', password='', timeout=10):
        """Fetch NTRIP sourcetable and return list of mountpoint dicts.
        
        Returns list of {'name': str, 'format': str, 'country': str, 'lat': str, 'lon': str}
        Raises exceptions on connection error.
        """
        import base64
        user_pass = f'{user}:{password}'
        auth = base64.b64encode(user_pass.encode('utf-8')).decode('utf-8')
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Basic {auth}\r\n"
            f"User-Agent: {useragent}\r\n"
            f"Ntrip-Version: Ntrip/1.0\r\n"
            f"\r\n"
        ).encode('utf-8')

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, int(port)))
            s.sendall(request)
            data = b''
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data += chunk
                if len(data) > 2 * 1024 * 1024:  # 2 MB max
                    break
        finally:
            s.close()

        text = data.decode('utf-8', errors='replace')
        # Find body (after \r\n\r\n)
        if '\r\n\r\n' in text:
            body = text.split('\r\n\r\n', 1)[1]
        else:
            body = text

        mountpoints = []
        for line in body.splitlines():
            if line.startswith('STR;'):
                parts = line.split(';')
                if len(parts) >= 2:
                    mp = {
                        'name':    parts[1] if len(parts) > 1 else '',
                        'format':  parts[3] if len(parts) > 3 else '',
                        'country': parts[8] if len(parts) > 8 else '',
                        'lat':     parts[9] if len(parts) > 9 else '',
                        'lon':     parts[10] if len(parts) > 10 else '',
                    }
                    if mp['name']:
                        mountpoints.append(mp)
        return mountpoints


    def registerCorrectionDataEventListener(self,callback):
        self.events.append(callback)

    def triggerCorrectionDataEvents(self,data):
        for e in self.events:
            e(data)
            
    def registerNtripLogListener(self,callback):
        self.rawevents.append(callback)
        
    def triggerRawDataEvents(self,data):
        for e in self.rawevents:
            e(data)
            
    def countReceivedData(self,data):
        self.dataReceived += len(data)

    def resetReceivedData(self):
        self.dataReceived = 0
    
    def emitStatusMessage(self, msg: str):
        """Thread-safe status message emission via Qt signal."""
        try:
            if hasattr(self.dockwidget, 'ntripStatusMessage'):
                self.dockwidget.ntripStatusMessage.emit(msg)
        except Exception:
            pass
        
    def countRxData(self):
        """Thread-safe RTCM byte rate counter using Qt signal."""
        while not self.stop_countrtcmrxevent.is_set():
            rxDataSize = self.dataReceived
            self.resetReceivedData()
            try:
                if hasattr(self.dockwidget, 'rtcmBytesUpdate'):
                    self.dockwidget.rtcmBytesUpdate.emit(rxDataSize)
            except Exception:
                pass
            time.sleep(1)

    def positionUploadTask(self):
        """Periodically push GGA to caster. sendGGAToCaster is updated from main thread."""
        while not self.stop_event.is_set():
            if self.connectionState and self.sendGGAToCaster:
                try:
                    self.socket.sendall(self.getGGABytes())
                except Exception:
                    pass
            time.sleep(5)

    def stopThreads(self):
        """Stop all background threads and close socket. Called from main thread."""
        self.stop_event.set()
        self.stopNtripConnection.set()
        self.stop_countrtcmrxevent.set()

        self.dataReceived = 0
        self.connectionState = False

        # Notify listeners that connection is gone (pass empty bytes, not False)
        try:
            self.triggerCorrectionDataEvents(b'')
        except Exception:
            pass

        try:
            if self.socket:
                self.socket.close()
        except Exception:
            pass

    def setPosition(self, lat, lon, height):

        #cut decimal places
        lat = float(f"{lat:.3f}")
        lon = float(f"{lon:.3f}")

        self.flagN="N"
        self.flagE="E"
        if lon>180:
            lon=(lon-360)*-1
            self.flagE="W"
        elif (lon<0 and lon>= -180):
            lon=lon*-1
            self.flagE="W"
        elif lon<-180:
            lon=lon+360
            self.flagE="E"
        else:
            self.lon=lon
        if lat<0:
            lat=lat*-1
            self.flagN="S"
        self.lonDeg=int(lon)
        self.latDeg=int(lat)
        self.lonMin=(lon-self.lonDeg)*60
        self.latMin=(lat-self.latDeg)*60
        self.height = height

    def getMountPointBytes(self):
        
        # Benutzername und Passwort kodieren
        mountPointString = "GET %s HTTP/1.1\r\nUser-Agent: %s\r\nAuthorization: Basic %s\r\n" % (self.mountpoint, useragent, self.user)

        if self.host or self.V2:
           hostString = "Host: %s:%i\r\n" % (self.caster,self.port)
           mountPointString+=hostString
        if self.V2:
           mountPointString+="Ntrip-Version: Ntrip/2.0\r\n"
        mountPointString+="\r\n"
        
        
        #return bytes(mountPointString,'ascii')
        return (mountPointString.encode('utf-8'))
    
    
    def getMountPointReq(self):
        """Build NTRIP mount point request with required Host header."""
        request = (
            f"GET {self.mountpoint} HTTP/1.1\r\n"
            f"Host: {self.caster}:{self.port}\r\n"
            f"Authorization: Basic {self.user}\r\n"
            f"User-Agent: {useragent}\r\n"
            f"Ntrip-Version: Ntrip/1.0\r\n"
            f"\r\n"
        )
        return request.encode('utf-8')
    
    def getGGABytes(self):
        now = datetime.datetime.utcnow()
        # Format: hhmmss.ss (ss must have leading zero → %05.2f)
        sec = now.second + now.microsecond / 1e6
        ggaString = "GPGGA,%02d%02d%05.2f,%02d%011.8f,%1s,%03d%011.8f,%1s,1,08,1.0,%.3f,M,0.000,M,," % (
            now.hour, now.minute, sec,
            self.latDeg, self.latMin, self.flagN,
            self.lonDeg, self.lonMin, self.flagE,
            self.height)
        checksum = self.calcultateCheckSum(ggaString)
        return bytes("$%s*%s\r\n" % (ggaString, checksum), 'ascii')

    def calcultateCheckSum(self, stringToCheck):
        xsum_calc = 0
        for char in stringToCheck:
            xsum_calc = xsum_calc ^ ord(char)
        return "%02X" % xsum_calc


    def _connection_loop(self):
        """Outer loop: maintains NTRIP connection with auto-reconnect on unexpected drops."""
        while not self.stopNtripConnection.is_set():
            self.readData()
            if self.stopNtripConnection.is_set():
                break
            # Connection dropped unexpectedly – wait 5 s then retry
            self.emitStatusMessage('Verbindung unterbrochen – Neuverbindung in 5 s...')
            for _ in range(50):
                if self.stopNtripConnection.is_set():
                    return
                time.sleep(0.1)

    def readData(self):
        """Connect to NTRIP caster and stream correction data (single attempt)."""
        self.emitStatusMessage(f'Verbinde mit {self.caster}:{self.port}{self.mountpoint}...')

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)
            self.socket.connect((self.caster, self.port))
        except Exception as e:
            self.emitStatusMessage(f'Fehler: Verbindung fehlgeschlagen - {str(e)[:50]}')
            self.connectionState = False
            return

        try:
            # Send request
            self.socket.sendall(self.getMountPointReq())

            # Read HTTP response header (ends with \r\n\r\n)
            header_buf = b''
            while b'\r\n\r\n' not in header_buf:
                if self.stopNtripConnection.is_set():
                    return
                try:
                    chunk = self.socket.recv(4096)
                except socket.timeout:
                    self.emitStatusMessage('Fehler: Timeout beim Verbindungsaufbau')
                    return
                if not chunk:
                    self.emitStatusMessage('Fehler: Server hat Verbindung geschlossen')
                    return
                header_buf += chunk
                if len(header_buf) > 65536:
                    self.emitStatusMessage('Fehler: Response Header zu groß')
                    return

            # Split header from body data received along with header
            header_part, leftover = header_buf.split(b'\r\n\r\n', 1)
            header_text = header_part.decode('utf-8', errors='replace')
            header_lines = header_text.split('\r\n')

            status_line = header_lines[0] if header_lines else ''

            # Check for success
            ok = any(x in status_line for x in ('ICY 200 OK', 'HTTP/1.0 200 OK',
                                                  'HTTP/1.1 200 OK'))
            if not ok:
                # Parse error messages with response
                if '401' in status_line:
                    self.emitStatusMessage(f'Fehler (401): Authentifizierung fehlgeschlagen\nServer: {status_line}')
                    return
                if '404' in status_line:
                    self.emitStatusMessage(f'Fehler (404): Mountpoint nicht gefunden\nServer: {status_line}')
                    return
                if 'SOURCETABLE' in status_line:
                    self.emitStatusMessage(f'Fehler: Mountpoint ungültig\nServer: {status_line}')
                    return
                self.emitStatusMessage(f'Fehler:\nServer: {status_line}')
                return

            # Send GGA position to caster (required by some casters before sending data)
            if self.sendGGAToCaster:
                try:
                    self.socket.sendall(self.getGGABytes())
                except Exception:
                    pass

            self.connectionState = True
            self.socket.settimeout(5)
            self.emitStatusMessage(f'✓ Verbunden mit {self.mountpoint}\nServer: {status_line}')

            # Feed any body data already received along with the header
            if leftover:
                self.countReceivedData(leftover)
                self.triggerCorrectionDataEvents(leftover)
                for stream in self.serialStreams:
                    stream.writeToStream(leftover)

            if self.maxConnectTime > 0:
                EndConnect = datetime.timedelta(seconds=self.maxConnectTime)
                connectTime = datetime.datetime.now()

            # Main data receive loop
            while not self.stopNtripConnection.is_set():
                try:
                    data = self.socket.recv(self.buffer)
                    if not data:
                        break

                    self.countReceivedData(data)
                    if self.logData:
                        self.writeToFile(data)
                    self.triggerCorrectionDataEvents(data)
                    for stream in self.serialStreams:
                        stream.writeToStream(data)

                    if self.maxConnectTime > 0:
                        if datetime.datetime.now() > connectTime + EndConnect:
                            break

                except socket.timeout:
                    # Normal during quiet periods — just keep looping
                    continue
                except socket.error:
                    break
                except Exception:
                    break

        except Exception:
            pass
        finally:
            self.connectionState = False
            try:
                self.socket.close()
            except Exception:
                pass
            # Reset RTCM rate display to 0 when connection drops
            try:
                if hasattr(self.dockwidget, 'rtcmBytesUpdate'):
                    self.dockwidget.rtcmBytesUpdate.emit(0)
            except Exception:
                pass