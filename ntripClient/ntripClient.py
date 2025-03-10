#!/usr/bin/env -S python3 -u
"""
This is inspired by the NtripPerlClient program written by BKG modified by unavco.
"""

import socket
import sys
import datetime
import base64
import time
import os
import ssl
from optparse import OptionParser
import datetime
import threading
import serial
from . import pynmea2


version=0.5
useragent="Q-Ntrip-X-/%.1f" % version


factor=2 # How much the sleep time increases with each failed attempt
maxReconnect=100
maxReconnectTime=1200
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
                 lat=48.85,
                 lon=9.33,   
                 height=330,
                 ssl=False,
                 verbose=False,
                 UDP_Port=None,
                 V2=False,
                 headerFile=sys.stderr,
                 headerOutput=False,
                 maxConnectTime=0,
                 streams = []
                 ):
        self.buffer=buffer
        self.user=base64.b64encode(bytes(user,'utf-8')).decode("utf-8")
        self.out=out
        self.port=port
        self.caster=caster
        self.mountpoint=mountpoint
        self.setPosition(lat, lon)
        self.height=height
        self.verbose=verbose
        self.ssl=ssl
        self.host=host
        self.UDP_Port=UDP_Port
        self.V2=V2
        self.headerFile=headerFile
        self.headerOutput=headerOutput
        self.maxConnectTime=maxConnectTime
        
        self.serialStreams = streams
        
        self.socket=None
        
        self.connectionState = False # indicates if ntrip connection has been established.
        self.uploadPositionThread = threading.Thread(target=self.positionUploadTask)
        self.uploadPositionThread.daemon = True  # makes the thread a daemon thread
        self.uploadPositionThread.start()   #start timer thread

        if UDP_Port:
            self.UDP_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.UDP_socket.bind(('', 0))
            self.UDP_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        else:
            self.UDP_socket=None
    
    def positionUploadTask(self):
        while True:
            if self.connectionState:
                self.socket.sendall(self.getGGABytes())         # Send GGS string to caster         
            time.sleep(15)   

    def setPosition(self, lat, lon):
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

    def getMountPointBytes(self):
        mountPointString = "GET %s HTTP/1.1\r\nUser-Agent: %s\r\nAuthorization: Basic %s\r\n" % (self.mountpoint, useragent, self.user)

        if self.host or self.V2:
           hostString = "Host: %s:%i\r\n" % (self.caster,self.port)
           mountPointString+=hostString
        if self.V2:
           mountPointString+="Ntrip-Version: Ntrip/2.0\r\n"
        #mountPointString+="\r\n"
        
        return bytes(mountPointString,'ascii')

    def getGGABytes(self):
        now = datetime.datetime.utcnow()

        #GGA message must be enrichted with position data from the mower
        ggaString= "GPGGA,%02d%02d%04.2f,%02d%011.8f,%1s,%03d%011.8f,%1s,1,00,0.000,0,M,0,M,1.000," % \
            (now.hour,now.minute,now.second,self.latDeg,self.latMin,self.flagN,self.lonDeg,self.lonMin,self.flagE)
        checksum = self.calcultateCheckSum(ggaString)
        if self.verbose:
            print  ("$%s*%s\r\n" % (ggaString, checksum))
        return bytes("$%s*%s\r\n" % (ggaString, checksum),'ascii')

    def calcultateCheckSum(self, stringToCheck):
        xsum_calc = 0
        for char in stringToCheck:
            xsum_calc = xsum_calc ^ ord(char)
        return "%02X" % xsum_calc

    def readData(self):
        reconnectTry=1
        sleepTime=1
        reconnectTime=0
        
        print('Connect to NTRIP caster.')
        
        if self.maxConnectTime > 0 :
            EndConnect=datetime.timedelta(seconds=self.maxConnectTime)
        
        while reconnectTry<=maxReconnect:
            found_header=False
            
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            #if self.ssl:
                #self.socket=ssl.wrap_socket(self.socket)
                #self.socket=ssl.wrap_socket(self.socket, keyfile=None, certfile=None, server_side=False, cert_reqs=ssl.CERT_NONE, ssl_version=ssl.PROTOCOL_SSLv23, server_hostname=self.caster)
            

            error_indicator = self.socket.connect_ex((self.caster, self.port))
            
            if error_indicator==0:
                sleepTime = 1
                connectTime=datetime.datetime.now()

                self.socket.settimeout(1000)
                self.socket.sendall(self.getMountPointBytes())
                
                while not found_header:
                    
                    casterResponse=self.socket.recv(1024) #All the data
                    
                   
                    print(casterResponse)

                    header_lines = None
                    try:
                        header_lines = casterResponse.decode('utf-8').split("\r\n")
                    except:
                        print('error in header decoding.')

                    for line in header_lines:
                        if line=="":
                            if not found_header:
                                found_header=True
                                if self.verbose:
                                    print("End Of Header"+"\n")
                        else:
                            if self.verbose:
                                print("Header: " + line+"\n")
                        if self.headerOutput:
                            self.headerFile.write(line+"\n")

                    for line in header_lines:
                        
                        print(line)
                        
                        
                        if line.find("SOURCETABLE")>=0:
                            print("Mount point does not exist")
                            #sys.exit(1)
                        elif line.find("401 Unauthorized")>=0:
                            print("Unauthorized request\n")
                            #sys.exit(1)
                        elif line.find("404 Not Found")>=0:
                            print("Mount Point does not exist\n")
                            #sys.exit(2)
                        elif line.find("ICY 200 OK")>=0:
                            print("ICY 200 OK")
                            #Request was valid
                            
                            
                            self.socket.sendall(self.getGGABytes())
                            self.connectionState = True
                        elif line.find("HTTP/1.0 200 OK")>=0:
                            #Request was valid
                            self.socket.sendall(self.getGGABytes())
                            self.connectionState = True
                        elif line.find("HTTP/1.1 200 OK")>=0:
                            #Request was valid
                            self.socket.sendall(self.getGGABytes())
                            self.connectionState = True
                    
                data = "Initial data".encode()
                
                while data:

                    try:
                        data=self.socket.recv(self.buffer)
                        print(data)

                        for s in self.serialStreams:
                            s.writeToStream(data);
                                
                        if self.maxConnectTime :
                            if datetime.datetime.now() > connectTime+EndConnect:
                                if self.verbose:
                                    print("Connection Time exceeded\n")
                                #sys.exit(0)

                    except socket.timeout:
                        if self.verbose:
                            print('Connection TimedOut\n')
                        data=False
                    except socket.error:
                        if self.verbose:
                            print('Connection Error\n')
                        data=False

                
                self.socket.close()
                self.socket=None

                if reconnectTry < maxReconnect :
                    print( "%s No Connection to NtripCaster.  Trying again in %i seconds\n" % (datetime.datetime.now(), sleepTime))
                    #time.sleep(sleepTime)
                    #sleepTime *= factor

                    if sleepTime>maxReconnectTime:
                        sleepTime=maxReconnectTime
                #else:
                    #sys.exit(1)


                reconnectTry += 1
            else:
                self.socket=None
                
                print ("Error indicator: ", error_indicator)

                if reconnectTry < maxReconnect :
                    print( "%s No Connection to NtripCaster.  Trying again in %i seconds\n" % (datetime.datetime.now(), sleepTime))
                    time.sleep(sleepTime)
                    sleepTime *= factor
                    if sleepTime>maxReconnectTime:
                        sleepTime=maxReconnectTime
                reconnectTry += 1


class NtripSerialStream():
    def __init__(self, port, baudrate):
        self.port = port
        self.baudrate = baudrate
        self.serial = serial.Serial(port, baudrate, timeout=1)
        self.buffer = bytearray()
        self.thread = threading.Thread(target=self.runProcess)
        self.thread.daemon = True  # makes the thread a daemon thread

        self.events = []

        self.sendCorrectionData = True      #indicates weather to send correction data or not
        print('Initialized serialport ' + str(self.port) + ' with baudrate ' + str(self.baudrate))
        self.thread.start()   #start timer thread
        

    def runProcess(self):
        #self.__resetTestRun()
        self.__read_from_serial();

    def stopSerialStream(self):
        self.serial.close()
        #self.thread.stop()
    
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
                    
                    # Überprüfe, ob die Zeile NMEA-Daten enthält
                    if line.startswith(b'$'):
                        nmea_data = line.decode('ascii', errors='ignore')
                        self.__process_nmea_data(nmea_data)
     
            except:
                continue

    def __process_nmea_data(self, line):
        #try:
            msg = pynmea2.parse(line)
            if isinstance(msg, pynmea2.types.talker.GGA):
                latitude = msg.latitude
                longitude = msg.longitude
                
                print(f"Latitude: {latitude}, Longitude: {longitude}, Fixtype: {self.__getFixModeString(msg.gps_qual)}")
                self.triggerEvents({'lat': latitude, 'lon': longitude})
                     

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

    def triggerEvents(self,data):
        for e in self.events:
            e(data)