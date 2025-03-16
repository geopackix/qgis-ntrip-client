#!/usr/bin/env -S python3 -u

import threading
import serial
import os
import time
from . import pynmea2
from qgis.PyQt.QtGui import QIcon, QPixmap



class NtripSerialStream():
    def __init__(self, port, baudrate, dockwidget):
        self.port = port
        self.baudrate = baudrate
        self.serial = serial.Serial(port, baudrate, timeout=1)
        self.buffer = bytearray()

        self.dataReceived = 0
        
        self.stopSerial = threading.Event()
        self.thread = threading.Thread(target=self.runProcess)
        self.thread.daemon = True  # makes the thread a daemon thread

        self.events = []
        self.rawevents = []

        self.sendCorrectionData = True      #indicates weather to send correction data or not
        print('Initialized serialport ' + str(self.port) + ' with baudrate ' + str(self.baudrate))
        self.thread.start()   #start timer thread
        
        self.dockwidget = dockwidget
        
        self.logData = False
        
        #Thread which calculates received data length per sencond
        self.stop_countSerialRxEvent = threading.Event()   
        self.countSerialRxEventThread = threading.Thread(target=self.countRxData)
        self.countSerialRxEventThread.daemon = True 
        self.countSerialRxEventThread.start() 
        
        self.dockwidget.cb_send_correction.stateChanged.connect(self.switchSendCorrectionData)
        self.dockwidget.fileSelectorReceiverRecord.fileChanged.connect(self.on_path_changed)
        self.dockwidget.checkBoxRecordReceiver.stateChanged.connect(self.on_cb_changed)
    
    def on_path_changed(self):
        path = self.dockwidget.fileSelectorReceiverRecord.filePath()   
        #print(f'Log file path has changed to {path}')
        if path:
            self.openFile(path)
        else:
            self.closeFile()
            
    def on_cb_changed(self):
        cb_val =  self.dockwidget.checkBoxRecordReceiver.isChecked()
        self.logData = cb_val
        
    def countReceivedData(self,data):
        self.dataReceived += len(data)

    def resetReceivedData(self):
        self.dataReceived = 0
        
    def countRxData(self):
        while not self.stop_countSerialRxEvent.is_set():
            rxDataSize = self.dataReceived
            #print(f'SERIAL - Received {rxDataSize} Bytes before reset.')
            self.dockwidget.lblReceivedSerialData.setText(f'{rxDataSize} bytes/s')
            self.resetReceivedData()
            time.sleep(1) 
        
    def runProcess(self):
        while not self.stopSerial.is_set():
            #print('run serial read process')
            #self.__resetTestRun()
            self.__read_from_serial()

    def stopSerialStream(self):
        #print('stop Serialstream')
        
        self.dataReceived = 0;
        self.dockwidget.lblReceivedSerialData.setText(f'{self.dataReceived} bytes/s')
        self.stop_countSerialRxEvent.set()
    
        
        self.stopSerial.set()
        self.serial.close()
    
    
    def switchSendCorrectionData(self):

        self.sendCorrectionData = self.dockwidget.cb_send_correction.isChecked()
        #icon
        rtcm_icon_on = f'{os.path.dirname(__file__)}/rtcmOn.png'
        rtcm_icon_off = f'{os.path.dirname(__file__)}/rtcmOff.png'
        
        if self.sendCorrectionData:
            self.dockwidget.receiveCorrectionsIcon.setPixmap(QPixmap(rtcm_icon_on))
        else:
            self.dockwidget.receiveCorrectionsIcon.setPixmap(QPixmap(rtcm_icon_off)) 
        
        
        
    
    def writeToStream(self, txdata):
        if self.sendCorrectionData:
            self.serial.write(txdata)
            
    def openFile(self, file):
        self.file = open(file, 'ab')
        
    def writeToFile(self, data):
        if self.logData:
            if not self.file.closed:
                self.file.write(data)   
    def closeFile(self):
        if not self.file.closed:
            self.file.close()  
        
    def __read_from_serial(self):

        buffer = b''

        while self.serial.is_open:
        #while True:
            try:
                data = self.serial.read(128)
                
                self.writeToFile(data)
                
                # Füge die gelesenen Daten zum Puffer hinzu
                buffer += data
                
                self.countReceivedData(data)

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