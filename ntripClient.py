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

useragent="Ntrip-QGIS-/%.1f" % version


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
                 lat=48.85,
                 lon=9.33,   
                 height=330,
                 ssl=False,
                 verbose=False,
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
        self.setPosition(lat, lon)
        self.height=height
        self.verbose=verbose
        self.ssl=ssl
        self.host=host
        self.V2=False
        self.headerFile=headerFile
        self.headerOutput=headerOutput
        self.maxConnectTime=maxConnectTime
        
        self.serialStreams = streams
        
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
        self.NtripConnectionThread = threading.Thread(target=self.readData)
        self.NtripConnectionThread.daemon = True  # makes the thread a daemon thread
        self.NtripConnectionThread.start()   #start timer thread
        
        
        #Thread which calculates received data length per sencond
        self.stop_countrtcmrxevent = threading.Event()   
        self.countrtcmrxeventThread = threading.Thread(target=self.countRxData)
        self.countrtcmrxeventThread.daemon = True 
        self.countrtcmrxeventThread.start() 
        
        self.sendGGAToCaster = True
        
        self.logData = False
        self.dockwidget.checkBoxRecordNtrip.stateChanged.connect(self.on_cb_changed)
        self.dockwidget.fileSelectorNtripRecord.fileChanged.connect(self.on_path_changed)

    ###
    # Write log file
    ###
    
    def on_path_changed(self):
        path = self.dockwidget.fileSelectorNtripRecord.filePath()   
        #print(f'Log file path has changed to {path}')
        if path:
            self.openFile(path)
        else:
            self.closeFile()

    def on_cb_changed(self):
        cb_val =  self.dockwidget.checkBoxRecordNtrip.isChecked()
        self.logData = cb_val
        
    def openFile(self, file):
        self.file = open(file, 'ab')
        
    def writeToFile(self, data):
        if self.logData:
            if not self.file.closed:
                self.file.write(data)   
    def closeFile(self):
        if not self.file.closed:
            self.file.close()        


    def registerCorrectionDataEventListener(self,callback):
        self.events.append(callback)
        print('registerCorrectionDataEventListener')

    def triggerCorrectionDataEvents(self,data):
        for e in self.events:
            e(data)
            
    def registerNtripLogListener(self,callback):
        self.rawevents.append(callback)
        print('registerCorrectionDataEventListener')
        
    def triggerRawDataEvents(self,data):
        for e in self.rawevents:
            e(data)
            
    def countReceivedData(self,data):
        self.dataReceived += len(data)

    def resetReceivedData(self):
        self.dataReceived = 0
        
    def countRxData(self):
        while not self.stop_countrtcmrxevent.is_set():
            rxDataSize = self.dataReceived
            #print(f'Received {rxDataSize} bytes before reset.')
            self.dockwidget.lblReceivedRTCMData.setText(f'{rxDataSize} bytes/s')
            self.resetReceivedData()
            time.sleep(1) 
    
    def positionUploadTask(self):
        while not self.stop_event.is_set():
            if self.connectionState and self.sendGGAToCaster:
                self.socket.sendall(self.getGGABytes())         # Send GGS string to caster         
            time.sleep(15)   

    def stopThreads(self):
        self.stop_event.set()
        self.stopNtripConnection.set()
        
        self.dataReceived = 0;
        self.dockwidget.lblReceivedRTCMData.setText(f'{ self.dataReceived} bytes/s')
        self.stop_countrtcmrxevent.set()
        
        self.triggerCorrectionDataEvents(False)
        self.connectionState = False
        self.socket.close()
        print('NTRIP client stopped.')

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
        
        # Erstelle die Authentifizierungsinformationen
        auth = self.user

        # Erstelle die HTTP-Anfrage
        request = (
            f"GET {self.mountpoint} HTTP/1.1\r\n"
            #f"Host: {self.host}\r\n"
            f"Authorization: Basic {auth}\r\n"
            f"User-Agent: ntrip-client/1.0\r\n"
            f"\r\n"
        )

        # Sende die Anfrage über den Socket
        return request.encode()
    
    
    

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
        
        print('Connect to NTRIP caster.')
        
        if self.maxConnectTime > 0 :
            EndConnect=datetime.timedelta(seconds=self.maxConnectTime)
        
        #while reconnectTry<=maxReconnect and not self.stopNtripConnection.is_set():
        found_header=False

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        error_indicator = self.socket.connect_ex((self.caster, self.port))
        
        while not self.stopNtripConnection.is_set():
       # with self.socket as s:
       
            s = self.socket
            
            if error_indicator==0:
                sleepTime = 1
                connectTime=datetime.datetime.now()
                data = None

                s.settimeout(1000)
                print(self.getMountPointReq())
                s.sendall(self.getMountPointReq())
                try:
                
                    while not found_header:
                        
                        casterResponse=s.recv(1024) #All the data
                        
                        print(casterResponse)

                        header_lines = None
                        try:
                            header_lines = casterResponse.decode('utf-8').split("\r\n")
                    
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
                                    s.sendall(self.getGGABytes())
                                    self.connectionState = True
                                    data = "Initial data".encode()
                                elif line.find("HTTP/1.0 200 OK")>=0:
                                    #Request was valid
                                    s.sendall(self.getGGABytes())
                                    self.connectionState = True
                                    data = "Initial data".encode()
                                elif line.find("HTTP/1.1 200 OK")>=0:
                                    #Request was valid
                                    s.sendall(self.getGGABytes())
                                    self.connectionState = True
                                    data = "Initial data".encode()
                                
                        except:
                            print('error in header decoding.')
                            
                    data = "Initial data".encode()
                except Exception as e:  
                    print(e)
                    continue
                    
                while data:
            
                    try:
                        data=s.recv(self.buffer)
                        
                        self.countReceivedData(data)
                        
                        if self.logData:
                            self.writeToFile(data)
                        

                        for stream in self.serialStreams:
                            self.triggerCorrectionDataEvents(True)
                            stream.writeToStream(data);
                                
                        if self.maxConnectTime :
                            if datetime.datetime.now() > connectTime+EndConnect:
                                if self.verbose:
                                    print("Connection Time exceeded\n")
                                

                    except socket.timeout:
                        if self.verbose:
                            print('Connection TimedOut\n')
                        data=False
                    except socket.error:
                        if self.verbose:
                            print('Connection Error\n')
                        data=False
                    except Exception as e:
                        data=False
                        print(e)

                
                s.close()
                s=None


            else:
                s.close()
                s=None
                
                print ("Connection error")

                # if reconnectTry < maxReconnect :
                #     print( "%s No Connection to NtripCaster.  Trying again in %i seconds\n" % (datetime.datetime.now(), sleepTime))
                #     time.sleep(sleepTime)
                #     sleepTime *= factor
                #     if sleepTime>maxReconnectTime:
                #         sleepTime=maxReconnectTime
                # reconnectTry += 1


