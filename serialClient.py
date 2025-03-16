#!/usr/bin/env -S python3 -u

import threading
import serial
from . import pynmea2

class NtripSerialStream():
    def __init__(self, port, baudrate):
        self.port = port
        self.baudrate = baudrate
        self.serial = serial.Serial(port, baudrate, timeout=1)
        self.buffer = bytearray()
        
        self.stopSerial = threading.Event()
        self.thread = threading.Thread(target=self.runProcess)
        self.thread.daemon = True  # makes the thread a daemon thread

        self.events = []
        self.rawevents = []

        self.sendCorrectionData = True      #indicates weather to send correction data or not
        print('Initialized serialport ' + str(self.port) + ' with baudrate ' + str(self.baudrate))
        self.thread.start()   #start timer thread
        
        

    def runProcess(self):
        while not self.stopSerial.is_set():
            print('run serial read process')
            #self.__resetTestRun()
            self.__read_from_serial()

    def stopSerialStream(self):
        print('stop Serialstream')
        self.stopSerial.set()
        self.serial.close()
        
    
    def writeToStream(self, txdata):
        if self.sendCorrectionData:
            print('write date to stream ' + str(self.port))
            self.serial.write(txdata)
        
        
    def __read_from_serial(self):

        buffer = b''

        while self.serial.is_open:
        #while True:
            try:
                data = self.serial.read(128)
                
                # Füge die gelesenen Daten zum Puffer hinzu
                buffer += data

                while b'\n' in buffer:
                    # Trenne den Puffer an der ersten neuen Zeile
                    line, buffer = buffer.split(b'\n', 1)
                    
                    self.triggerRawEvents(line.rstrip())
                    
                    # Überprüfe, ob die Zeile NMEA-Daten enthält
                    if line.startswith(b'$'):
                        nmea_data = line.decode('ascii', errors='ignore')
                        self.__process_nmea_data(nmea_data)
     
            except Exception as e:
                print(e)
                

    def __process_nmea_data(self, line):
        #try:
            msg = pynmea2.parse(line)
            if isinstance(msg, pynmea2.types.talker.GGA):
                latitude = msg.latitude
                longitude = msg.longitude
                height = msg.altitude
                fixtype = msg.gps_qual
                
                print(f"Latitude: {latitude}, Longitude: {longitude}, Fixtype: {self.__getFixModeString(msg.gps_qual)}")
                self.triggerEvents({'lat': latitude, 'lon': longitude, 'alt':height, 'fixtype': fixtype})
                     

    def __getFixModeString(self, modeNumber):
        if modeNumber == 0:
            return "Fix not valide"
        elif modeNumber == 1:
            return "GPS fix"
        elif modeNumber == 2:
            return "DGPS"
        elif modeNumber == 4:
            return "RTK fixed"
        elif modeNumber == 5:
            return "RTK float"
        else:
            return "Unknown"
            
    def registerEventListener(self,callback):
        self.events.append(callback)
        
    def registerRawEventListener(self,callback):
        self.rawevents.append(callback)

    def triggerEvents(self,data):
        for e in self.events:
            e(data)
            
    def triggerRawEvents(self,data):
        for e in self.rawevents:
            e(data)