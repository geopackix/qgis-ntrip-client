# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QNTRIPClient
                                 A QGIS plugin
 NTRIP client for qgis
        copyright            : (C) 2025 by Manuel Hart (Geokoord.com)
        email                : mh@geokoord.com
 ***************************************************************************/
"""
import sys
import os
import configparser
from typing import Optional
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt, QTimer, QVariant
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import QAction, QLineEdit, QVBoxLayout, QWidget
import threading

# Initialize Qt resources from file resources.py
from .resources import *

from qgis.core import QgsProject, QgsPointXY, QgsMarkerSymbol, QgsFeature, QgsGeometry, QgsVectorLayer, QgsField

from tools.mapTool import MapTool
from ntripClient.ntripClient import NtripClient, NtripSerialStream

from datetime import datetime


# Import the code for the DockWidget
from .q_ntrip_client_dockwidget import QNTRIPClientDockWidget
from .q_ntrip_client_infowidget import QNTRIPClientInfoWidget
import os.path


class QNTRIPClient:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        # Save reference to the QGIS interface
        self.iface = iface

        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)

        # initialize locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'QNTRIPClient_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        # Declare instance attributes
        self.actions = []
        self.menu = self.tr(u'&QNTRIPClient')
        # TODO: We are going to let the user set this up in a future iteration
        self.toolbar = self.iface.addToolBar(u'QNTRIPClient')
        self.toolbar.setObjectName(u'QNTRIPClient')

        #print "** INITIALIZING QNTRIPClient"

        self.pluginIsActive = False
        self.dockwidget = None
        
        self.serialStream = None
        self.layer = None
        self.client = None


    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API.

        We implement this ourselves since we do not inherit QObject.

        :param message: String for translation.
        :type message: str, QString

        :returns: Translated version of message.
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('QNTRIPClient', message)


    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None):
        """Add a toolbar icon to the toolbar.

        :param icon_path: Path to the icon for this action. Can be a resource
            path (e.g. ':/plugins/foo/bar.png') or a normal file system path.
        :type icon_path: str

        :param text: Text that should be shown in menu items for this action.
        :type text: str

        :param callback: Function to be called when the action is triggered.
        :type callback: function

        :param enabled_flag: A flag indicating if the action should be enabled
            by default. Defaults to True.
        :type enabled_flag: bool

        :param add_to_menu: Flag indicating whether the action should also
            be added to the menu. Defaults to True.
        :type add_to_menu: bool

        :param add_to_toolbar: Flag indicating whether the action should also
            be added to the toolbar. Defaults to True.
        :type add_to_toolbar: bool

        :param status_tip: Optional text to show in a popup when mouse pointer
            hovers over the action.
        :type status_tip: str

        :param parent: Parent widget for the new action. Defaults None.
        :type parent: QWidget

        :param whats_this: Optional text to show in the status bar when the
            mouse pointer hovers over the action.

        :returns: The action that was created. Note that the action is also
            added to self.actions list.
        :rtype: QAction
        """

        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.toolbar.addAction(action)

        if add_to_menu:
            self.iface.addPluginToMenu(
                self.menu,
                action)

        self.actions.append(action)

        return action


    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""

        #icon_path = ':/plugins/q_ntrip_client/icon.png'
        icon_path = f'{self.plugin_dir}/icon.png'
        #icon_path = 'icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'QNtrip'),
            callback=self.run,
            parent=self.iface.mainWindow())

    #--------------------------------------------------------------------------

    def onClosePlugin(self):
        """Cleanup necessary items here when plugin dockwidget is closed"""

        #print "** CLOSING QNTRIPClient"
        
        self.stopNtripClient()

        # disconnects
        self.dockwidget.closingPlugin.disconnect(self.onClosePlugin)

        # remove this statement if dockwidget is to remain
        # for reuse if plugin is reopened
        # Commented next statement since it causes QGIS crashe
        # when closing the docked window:
        # self.dockwidget = None

        self.pluginIsActive = False


    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""

        #print "** UNLOAD QNTRIPClient"

        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&QNTRIPClient'),
                action)
            self.iface.removeToolBarIcon(action)
        # remove the toolbar
        del self.toolbar

    #--------------------------------------------------------------------------
    def stopNtripClient(self):
        self.serialStream.stopSerialStream()
        self.out(f'Disconnect serial stream.')
        self.posIcon(0)
        self.client.stopThreads()
        self.out(f'Ntrip client stopped.')
        
        

    def startNtripClient(self):
        
        try:
            host,port,mp,user,pw,serial,baud = self.getValuesFromUi()
            self.serialStream = NtripSerialStream(serial,int(baud))
            self.out(f'Connect serial stream.')
            
            ## Temp layer for gnss points
            self.layer = self.create_temp_layer()
            QgsProject.instance().addMapLayer(self.layer)
            
            self.serialStream.registerEventListener(self.update_gnss_position)
            self.serialStream.registerRawEventListener(self.update_gnss)
            
            ntripArgs = {}
            ntripArgs['lat']= 48.6
            ntripArgs['lon']= 9.7
            ntripArgs['height']= 400
            # import ssl
            # ntripArgs['ssl']=True

            ntripArgs['ssl']=False
            ntripArgs['user']=user+":"+pw
            
            ntripArgs['caster']=host
            ntripArgs['host']=host
            ntripArgs['port']=int(port)
            print(f'Mountpoint:{mp}')
            ntripArgs['mountpoint']=mp

            if ntripArgs['mountpoint'][0:1] !="/":
                ntripArgs['mountpoint'] = "/"+ntripArgs['mountpoint']

            ntripArgs['V2']=False
            #serialStream = NtripSerialStream(serial,int(baud))
            ntripArgs['streams'] = [self.serialStream]
        
            self.client = NtripClient(**ntripArgs)
            self.client.registerCorrectionDataEventListener(self.updateRtcmState)
            self.out(f'Connect to NTRIP caster {host}.')
        except Exception as e:
            print(f"Fehler beim Starten des Ntrip Clients: {e}")    
   
    def update_gnss_position(self, data):
        longitude = data['lon']
        latitude = data['lat']
        
        self.set_marker(longitude, latitude, data)
        
        self.posIcon( data['fixtype'])
        
        
    def update_gnss(self, data):
        try:
            self.dockwidget.logReceiver.append(data.decode('utf-8', errors='ignore'))
        except Exception as e:
            print(f"Error decoding data: {e}")
        
    def posIcon(self, fixtype):
        
        #icon
        ft_icon_no = f'{self.plugin_dir}/noPos.png'
        ft_icon_3d = f'{self.plugin_dir}/pos4.png'
        ft_icon_rtkfloat = f'{self.plugin_dir}/pos2.png'
        ft_icon_rtkfix = f'{self.plugin_dir}/pos.png'
        ft_icon_dgps = f'{self.plugin_dir}/pos3.png'
        
        if(fixtype == 1):
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_3d))
        elif(fixtype == 2):
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_dgps))
        elif(fixtype == 4):
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_rtkfix))
        elif(fixtype == 5):
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_rtkfloat))
        else:
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_no)) 
    
    def updateRtcmState(self, rtcmstatus):
        print('update rtcm state')
        self.rtcmIcon(rtcmstatus)
    
    def rtcmIcon(self, receiveCorrections):
        
        
        
        #icon
        rtcm_icon_on = f'{self.plugin_dir}/rtcmOn.png'
        rtcm_icon_off = f'{self.plugin_dir}/rtcmOff.png'
    
        
        if(receiveCorrections == True):
            self.dockwidget.receiveCorrectionsIcon.setPixmap(QPixmap(rtcm_icon_on))
        else:
            self.dockwidget.receiveCorrectionsIcon.setPixmap(QPixmap(rtcm_icon_off)) 
        
     
    def set_marker(self, x, y, data):
        
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        
        longitude = data['lon']
        latitude = data['lat']
        height = data['alt']
        fixtype = data['fixtype']
        
        feature.setAttributes([latitude, longitude, height, fixtype])

        self.layer.dataProvider().addFeature(feature)
        
        self.layer.updateExtents()
        self.layer.triggerRepaint() #re-draw layer
        
    

    def on_info_button_click(self):
        # Hier kommt die Aktion, die ausgeführt werden soll
        print("Info Button wurde gedrückt!")
        self.write_config()
        self.info_window = QNTRIPClientInfoWidget()
        self.info_window.show()

    def getValuesFromUi(self):
        
        host = self.dockwidget.inputHost.text()
        port = self.dockwidget.inputPort.text()
        mp = self.dockwidget.inputMp.text()
        user = self.dockwidget.inputUser.text()
        pw = self.dockwidget.inputPassword.text()
        serial = self.dockwidget.inputSPort.text()
        baud = self.dockwidget.inputSBaud.text()

        return host,port,mp,user,pw,serial,baud
        
    def write_config(self):
        try:
            
            host,port,mp,user,pw,serial,baud = self.getValuesFromUi();
            
            self.config['DEFAULT']['Host'] = host
            self.config['DEFAULT']['Port'] = port
            self.config['DEFAULT']['Mountpoint'] = mp
            self.config['DEFAULT']['User'] = user
            self.config['DEFAULT']['Password'] = pw
            self.config['DEFAULT']['Serialport'] = serial
            self.config['DEFAULT']['SerialBaud'] = baud
            
            with open(self.config_file, 'w') as configfile:
                self.config.write(configfile)
            print("Konfigurationsdatei erfolgreich geschrieben.")
        except Exception as e:
            print(f"Fehler beim Schreiben der Konfigurationsdatei: {e}")
    
    def create_temp_layer(self):
    # Prüfen, ob der Layer "points" bereits existiert
    
        #layername = "gnss_points"
        layername = self.dockwidget.layerName.text() or "gnss_points"
        
        existing_layers = QgsProject.instance().mapLayersByName(layername)
        if existing_layers:
            print("Layer 'points' existiert bereits.")
            return existing_layers[0]
        else:
            # Erstellen eines neuen temporären Layers
            layer = QgsVectorLayer("Point?crs=EPSG:4326", layername, "memory")
            
            #layer.dataProvider().addAttributes([QgsField("lat", QVariant.Float)])
            
            layer.dataProvider().addAttributes([
                QgsField("Lat", QVariant.Double),
                QgsField("Lon", QVariant.Double),
                QgsField("Height", QVariant.Double),
                QgsField("Fixtype", QVariant.Int)
            ])
            layer.updateFields()
            
            
            layer.updateFields()
        
            # Hinzufügen von Feldern zum Layer
            #provider.addAttributes([QgsField("name", QVariant.String)])
            layer.updateFields()
            
            # Hinzufügen des Layers zum Projekt
            QgsProject.instance().addMapLayer(layer)
            print(f"Temporärer Layer {layername} wurde erstellt.")
            return layer

    def out(self, message):
        iso_timestamp = datetime.now().isoformat()
        history = self.dockwidget.output.toPlainText()
        
        self.dockwidget.output.setPlainText( iso_timestamp + ' ' + message + '\n'+ history)


    def on_checkbox_record_Receiver(self):
        #get value of checkbox
        isChecked = self.dockwidget.checkBoxRecordReceiver.isChecked()
        print(f'Reciever isChecked: {isChecked}')
        
    def on_checkbox_record_Ntrip(self):
        #get value of checkbox
        isChecked = self.dockwidget.checkBoxRecordNtrip.isChecked()
        print(f'NTRIP isChecked: {isChecked}')
        
        file = self.dockwidget.fileSelectorNtripRecord.filePath()
        
        if(isChecked):
            print(file) 
        
        


    def run(self):
        """Run method that loads and starts the plugin"""
        
       
        


        if not self.pluginIsActive:
            self.pluginIsActive = True
            
            ## READ DEFAULT Config
            
            # Standardwerte definieren
            default_values = {
                'Host': '',
                'Port': '2101',
                'Mountpoint': '',
                'User': '',
                'Password': '',
                'Serialport': '',
                'SerialBaud': ''
            }
            
            self.config_file = os.path.join(os.path.dirname(__file__),'config.ini')
            self.config = configparser.ConfigParser(defaults=default_values)
            self.config.read(self.config_file)
            
           

            # dockwidget may not exist if:
            #    first run of plugin
            #    removed on close (see self.onClosePlugin method)
            if self.dockwidget == None:
                # Create the dockwidget (after translation) and keep reference
                self.dockwidget = QNTRIPClientDockWidget()

            # connect to provide cleanup on closing of dockwidget
            self.dockwidget.closingPlugin.connect(self.onClosePlugin)

            # show the dockwidget
            # TODO: fix to allow choice of dock location
            self.iface.addDockWidget(Qt.TopDockWidgetArea, self.dockwidget)
            
            
            self.dockwidget.inputHost.setText(self.config ['DEFAULT']['Host'])
            self.dockwidget.inputPort.setText(self.config ['DEFAULT']['Port'])
            self.dockwidget.inputMp.setText(self.config ['DEFAULT']['Mountpoint'])
            self.dockwidget.inputUser.setText(self.config ['DEFAULT']['User'])
            self.dockwidget.inputPassword.setText(self.config ['DEFAULT']['Password'])
            self.dockwidget.inputSPort.setText(self.config ['DEFAULT']['Serialport'])
            self.dockwidget.inputSBaud.setText(self.config ['DEFAULT']['SerialBaud'])
            
            ft_icon_no = f'{self.plugin_dir}/noPos.png'
            self.dockwidget.fixtypeIcon.setPixmap(QPixmap(ft_icon_no))
        
            
            
            self.dockwidget.infoBtn.clicked.connect(self.on_info_button_click)
            self.dockwidget.connectBtn.clicked.connect(self.startNtripClient)
            self.dockwidget.disconnectBtn.clicked.connect(self.stopNtripClient)
            
            self.dockwidget.checkBoxRecordReceiver.stateChanged.connect(self.on_checkbox_record_Receiver)
            self.dockwidget.checkBoxRecordNtrip.stateChanged.connect(self.on_checkbox_record_Ntrip)
            
            self.dockwidget.show()
            
            
            
        
            
            #self.layer.renderer().setSymbol()
            
            #self.timer = QTimer()
            #self.timer.timeout.connect(self.update_marker)
            #self.timer.start(1000)  # Aktualisiere alle 5 Sekunden
            
            
        
