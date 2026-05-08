# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QNTRIPClient – GNSS NTRIP Client for QGIS
        copyright : (C) 2025 by Manuel Hart (Geokoord.com)
        email     : mh@geokoord.com
 ***************************************************************************/
"""
import sys
import os
import math

# Ensure plugin directory is in path for imports
plugin_dir = os.path.abspath(os.path.dirname(__file__))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt, QVariant
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import (QAction, QInputDialog, QMessageBox,
                                  QFileDialog, QVBoxLayout, QSizePolicy)

from .resources import *

from qgis.core import (QgsProject, QgsPointXY, QgsFeature, QgsGeometry,
                        QgsVectorLayer, QgsField,
                        QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                        QgsWkbTypes)
from qgis.gui import QgsRubberBand

import serial

from .ntripClient import NtripClient
from .serialClient import NtripSerialStream
from .caster_manager import CasterManager
from .measurement import PointMeasurement, TrackMeasurement
from .session_recorder import SessionRecorder
from .receiver_config import get_receiver_config
from .satellite_widget import SkyPlotWidget, SnrBarWidget

from datetime import datetime

from .q_ntrip_client_dockwidget import QNTRIPClientDockWidget
from .q_ntrip_client_infowidget import QNTRIPClientInfoWidget


# Fix type helper
_FIX_STRINGS = {
    0: "Kein Fix", 1: "GPS Fix", 2: "DGPS",
    4: "RTK Fixed", 5: "RTK Float"
}
_FIX_COLORS = {
    0: "#555555", 1: "#FF8800", 2: "#BBBB00",
    4: "#00AA00", 5: "#0088CC"
}


def fix_str(ft):
    return _FIX_STRINGS.get(ft, f"Unbekannt({ft})")


class QNTRIPClient:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.settings = QSettings()
        self.settings_prefix = 'plugins/qgis-ntrip-client'

        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir, 'i18n', f'QNTRIPClient_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u'&QNTRIPClient')
        self.toolbar = self.iface.addToolBar(u'QNTRIPClient')
        self.toolbar.setObjectName(u'QNTRIPClient')

        self.pluginIsActive = False
        self.dockwidget = None

        # Connection state (independent)
        self.serialStream = None
        self.client = None
        self._receiver_connected = False
        self._ntrip_connected = False
        self._receiver_connecting = False   # guard against double-click

        # Current sensor state
        self.current_hdop = 0.0
        self.current_vdop = 0.0
        self.current_pdop = 0.0
        self.current_num_sats = 0
        self.current_fixtype = 0
        self.current_lat = 0.0
        self.current_lon = 0.0
        self.current_height = 0.0

        # Modules
        self.caster_manager = CasterManager(self.plugin_dir)
        self.point_measurement = PointMeasurement()
        self.track_measurement = TrackMeasurement()
        self.session_recorder = None

        # Satellite widgets
        self.sky_plot = None
        self.snr_chart = None

        # Rubber bands for live position
        self.live_rb = None       # Position cross marker
        self.accuracy_rb = None   # Accuracy circle

        # Satellite data batching (update every 3 seconds)
        self.satellite_buffer = []
        self.satellite_update_timer = None

        # Layers
        self.point_layer = None   # Measured points
        self.track_layer = None   # Track points

        # Temp folder
        self.temp_folder = os.path.join(self.plugin_dir, 'temp')
        os.makedirs(self.temp_folder, exist_ok=True)

    def tr(self, message):
        return QCoreApplication.translate('QNTRIPClient', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True,
                   status_tip=None, whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip:
            action.setStatusTip(status_tip)
        if whats_this:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.add_action(
            icon_path, text=self.tr(u'GNSS NTRIP Client'),
            callback=self.run, parent=self.iface.mainWindow())

    def onClosePlugin(self):
        try:
            self.disconnectReceiver()
        except Exception:
            pass
        try:
            self.disconnectNtrip()
        except Exception:
            pass
        self._remove_rubber_bands()
        self.saveSettings()
        self.dockwidget.closingPlugin.disconnect(self.onClosePlugin)
        self.pluginIsActive = False

    def saveSettings(self):
        """Speichert alle Einstellungen in QSettings."""
        if not self.dockwidget:
            return
        
        # Serielle Verbindung
        self.settings.setValue(f'{self.settings_prefix}/serialPort', 
                              self.dockwidget.inputSPort.currentText())
        self.settings.setValue(f'{self.settings_prefix}/serialBaud', 
                              self.dockwidget.inputSBaud.currentText())
        self.settings.setValue(f'{self.settings_prefix}/receiverType', 
                              self.dockwidget.comboReceiverType.currentText())
        self.settings.setValue(f'{self.settings_prefix}/antennaHeight', 
                              self.dockwidget.spinAntennaHeight.value())
        self.settings.setValue(f'{self.settings_prefix}/sendCorrection', 
                              self.dockwidget.cbSendCorrection.isChecked())
        
        # NTRIP Einstellungen
        self.settings.setValue(f'{self.settings_prefix}/ntripHost', 
                              self.dockwidget.inputHost.text())
        self.settings.setValue(f'{self.settings_prefix}/ntripPort', 
                              self.dockwidget.inputPort.text())
        self.settings.setValue(f'{self.settings_prefix}/ntripMountpoint', 
                              self.dockwidget.inputMp.currentText())
        self.settings.setValue(f'{self.settings_prefix}/ntripUser', 
                              self.dockwidget.inputUser.text())
        self.settings.setValue(f'{self.settings_prefix}/ntripPassword', 
                              self.dockwidget.inputPassword.text())
        self.settings.setValue(f'{self.settings_prefix}/ntripVersion', 
                              self.dockwidget.selectNtripVersion.currentText())
        self.settings.setValue(f'{self.settings_prefix}/lastCasterIndex', 
                              self.dockwidget.comboCaster.currentIndex())
        
        # Hoehentransformation
        self.settings.setValue(f'{self.settings_prefix}/heightTransform', 
                              self.dockwidget.grpHeightTransform.isChecked())
        self.settings.setValue(f'{self.settings_prefix}/geoidModel', 
                              self.dockwidget.comboGeoidModel.currentText())
        self.settings.setValue(f'{self.settings_prefix}/geoidSeparation', 
                              self.dockwidget.spinGeoidSeparation.value())
        
        # Layer-Namen
        self.settings.setValue(f'{self.settings_prefix}/layerName', 
                              self.dockwidget.layerName.text())
        self.settings.setValue(f'{self.settings_prefix}/layerNameTrack', 
                              self.dockwidget.layerNameTrack.text())
        
        # Aufzeichnung
        self.settings.setValue(f'{self.settings_prefix}/autoRecord', 
                              self.dockwidget.grpAutoRecord.isChecked())
        self.settings.setValue(f'{self.settings_prefix}/recordReceiver', 
                              self.dockwidget.cbRecordReceiver.isChecked())
        self.settings.setValue(f'{self.settings_prefix}/recordRTCM', 
                              self.dockwidget.cbRecordRTCM.isChecked())
        self.settings.setValue(f'{self.settings_prefix}/tempFolder', 
                              self.dockwidget.fileSelectorTempFolder.filePath())
        
        # Messung
        self.settings.setValue(f'{self.settings_prefix}/averaging', 
                              self.dockwidget.cbAveraging.isChecked())
        self.settings.setValue(f'{self.settings_prefix}/avgEpochs', 
                              self.dockwidget.spinAvgEpochs.value())
        self.settings.setValue(f'{self.settings_prefix}/timeInterval', 
                              self.dockwidget.spinTimeInterval.value())
        self.settings.setValue(f'{self.settings_prefix}/distInterval', 
                              self.dockwidget.spinDistInterval.value())
        
        # No print statement - save silently

    def loadSettings(self):
        """Lädt alle gespeicherten Einstellungen."""
        if not self.dockwidget:
            return
        
        # Serielle Verbindung
        port = self.settings.value(f'{self.settings_prefix}/serialPort', 'COM1')
        if port and self.dockwidget.inputSPort.findText(port) >= 0:
            self.dockwidget.inputSPort.setCurrentText(port)
        
        baud = self.settings.value(f'{self.settings_prefix}/serialBaud', '115200')
        if baud:
            idx = self.dockwidget.inputSBaud.findText(baud)
            if idx >= 0:
                self.dockwidget.inputSBaud.setCurrentIndex(idx)
            else:
                self.dockwidget.inputSBaud.setEditText(baud)
        
        rx_type = self.settings.value(f'{self.settings_prefix}/receiverType', 'Generisch (NMEA)')
        if rx_type:
            idx = self.dockwidget.comboReceiverType.findText(str(rx_type))
            if idx >= 0:
                self.dockwidget.comboReceiverType.setCurrentIndex(idx)
        
        ant_h = self.settings.value(f'{self.settings_prefix}/antennaHeight', 0.0)
        self.dockwidget.spinAntennaHeight.setValue(float(ant_h))
        
        send_corr = self.settings.value(f'{self.settings_prefix}/sendCorrection', True)
        # Convert QSettings string/bool to Python bool (QSettings may store as string)
        if isinstance(send_corr, str):
            send_corr = send_corr.lower() in ('true', '1', 'yes')
        else:
            send_corr = bool(send_corr)
        self.dockwidget.cbSendCorrection.setChecked(send_corr)
        
        # NTRIP Einstellungen
        self.dockwidget.inputHost.setText(
            self.settings.value(f'{self.settings_prefix}/ntripHost', ''))
        self.dockwidget.inputPort.setText(
            self.settings.value(f'{self.settings_prefix}/ntripPort', '2101'))
        
        # Restore last mountpoint
        last_mp = self.settings.value(f'{self.settings_prefix}/ntripMountpoint', '')
        if last_mp:
            self.dockwidget.inputMp.setCurrentText(last_mp)
        
        self.dockwidget.inputUser.setText(
            self.settings.value(f'{self.settings_prefix}/ntripUser', ''))
        self.dockwidget.inputPassword.setText(
            self.settings.value(f'{self.settings_prefix}/ntripPassword', ''))
        
        ntrip_v = self.settings.value(f'{self.settings_prefix}/ntripVersion', '1')
        idx = self.dockwidget.selectNtripVersion.findText(ntrip_v)
        if idx >= 0:
            self.dockwidget.selectNtripVersion.setCurrentIndex(idx)
        
        # Hoehentransformation
        h_trans = self.settings.value(f'{self.settings_prefix}/heightTransform', False)
        if isinstance(h_trans, str):
            h_trans = h_trans.lower() in ('true', '1', 'yes')
        else:
            h_trans = bool(h_trans)
        self.dockwidget.grpHeightTransform.setChecked(h_trans)
        
        geoid_m = self.settings.value(f'{self.settings_prefix}/geoidModel', 'EGM96')
        idx = self.dockwidget.comboGeoidModel.findText(geoid_m)
        if idx >= 0:
            self.dockwidget.comboGeoidModel.setCurrentIndex(idx)
        
        geoid_s = self.settings.value(f'{self.settings_prefix}/geoidSeparation', 0.0)
        self.dockwidget.spinGeoidSeparation.setValue(float(geoid_s))
        
        # Layer-Namen
        self.dockwidget.layerName.setText(
            self.settings.value(f'{self.settings_prefix}/layerName', 'gnss_punkte'))
        self.dockwidget.layerNameTrack.setText(
            self.settings.value(f'{self.settings_prefix}/layerNameTrack', 'gnss_track'))
        
        # Aufzeichnung
        auto_rec = self.settings.value(f'{self.settings_prefix}/autoRecord', False)
        if isinstance(auto_rec, str):
            auto_rec = auto_rec.lower() in ('true', '1', 'yes')
        else:
            auto_rec = bool(auto_rec)
        self.dockwidget.grpAutoRecord.setChecked(auto_rec)
        
        rec_rx = self.settings.value(f'{self.settings_prefix}/recordReceiver', True)
        if isinstance(rec_rx, str):
            rec_rx = rec_rx.lower() in ('true', '1', 'yes')
        else:
            rec_rx = bool(rec_rx)
        self.dockwidget.cbRecordReceiver.setChecked(rec_rx)
        
        rec_rtcm = self.settings.value(f'{self.settings_prefix}/recordRTCM', True)
        if isinstance(rec_rtcm, str):
            rec_rtcm = rec_rtcm.lower() in ('true', '1', 'yes')
        else:
            rec_rtcm = bool(rec_rtcm)
        self.dockwidget.cbRecordRTCM.setChecked(rec_rtcm)
        
        temp_f = self.settings.value(f'{self.settings_prefix}/tempFolder', self.temp_folder)
        self.dockwidget.fileSelectorTempFolder.setFilePath(temp_f)
        
        # Messung
        avg = self.settings.value(f'{self.settings_prefix}/averaging', False)
        if isinstance(avg, str):
            avg = avg.lower() in ('true', '1', 'yes')
        else:
            avg = bool(avg)
        self.dockwidget.cbAveraging.setChecked(avg)
        
        avg_ep = self.settings.value(f'{self.settings_prefix}/avgEpochs', 10)
        self.dockwidget.spinAvgEpochs.setValue(int(avg_ep))
        
        t_int = self.settings.value(f'{self.settings_prefix}/timeInterval', 1.0)
        try:
            self.dockwidget.spinTimeInterval.setValue(float(t_int))
        except (ValueError, TypeError):
            self.dockwidget.spinTimeInterval.setValue(1.0)
        
        d_int = self.settings.value(f'{self.settings_prefix}/distInterval', 0.0)
        try:
            self.dockwidget.spinDistInterval.setValue(float(d_int))
        except (ValueError, TypeError):
            self.dockwidget.spinDistInterval.setValue(0.0)
        
        # Load complete

    def unload(self):
        self._remove_rubber_bands()
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&QNTRIPClient'), action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

    # =========================================================================
    # RECEIVER Connection (independent)
    # =========================================================================

    def connectReceiver(self):
        """Start a background thread to open the serial port (non-blocking)."""
        if self._receiver_connected or self._receiver_connecting:
            return

        serial_port = self.dockwidget.inputSPort.currentText().strip()
        baud        = self.dockwidget.inputSBaud.currentText().strip()
        send_corr   = self.dockwidget.cbSendCorrection.isChecked()   # read on main thread

        if not serial_port:
            self.out('Fehler: Bitte einen COM-Port auswählen.')
            return
        try:
            baud_int = int(baud)
        except (ValueError, TypeError):
            self.out(f'Fehler: Ungültige Baudrate "{baud}".')
            return

        # Update UI immediately (main thread)
        self._receiver_connecting = True
        self.dockwidget.btnConnectReceiver.setEnabled(False)
        self.dockwidget.btnDisconnectReceiver.setEnabled(False)
        self.dockwidget.lblReceiverConnState.setText('● verbinde…')
        self.dockwidget.lblReceiverConnState.setStyleSheet('font-size:10px;color:#FF8800;')
        self.out(f'Verbinde {serial_port} @ {baud_int} Baud…')

        def _rx_cb(n):
            # Thread-safe: emit signal, not touch widget directly
            self.dockwidget.rxBytesUpdate.emit(n)

        def _worker():
            try:
                stream = NtripSerialStream(serial_port, baud_int,
                                           send_correction=send_corr,
                                           rx_callback=_rx_cb)
                self.dockwidget.receiverConnectResult.emit(stream, '')
            except Exception as exc:
                self.dockwidget.receiverConnectResult.emit(None, str(exc))

        import threading as _t
        _t.Thread(target=_worker, daemon=True).start()

    def _on_receiver_connect_result(self, stream, error_msg):
        """Called on main thread via Qt signal when the port-open attempt finishes."""
        self._receiver_connecting = False

        if stream is None:
            self.dockwidget.btnConnectReceiver.setEnabled(True)
            self.dockwidget.btnDisconnectReceiver.setEnabled(False)
            self.dockwidget.lblReceiverConnState.setText('● getrennt')
            self.dockwidget.lblReceiverConnState.setStyleSheet('font-size:10px;color:#888;')

            port = self.dockwidget.inputSPort.currentText().strip()
            if '121' in error_msg or 'Semaphore' in error_msg:
                self.out(f'✗ {port}: Gerät nicht erreichbar (Bluetooth nicht verbunden?)')
                self.out('  → Bluetooth am Gerät einschalten und erneut versuchen')
            elif 'PermissionError' in error_msg or 'Access is denied' in error_msg:
                self.out(f'✗ {port}: Zugriff verweigert – Port von anderer Software belegt')
            else:
                self.out(f'✗ Verbindungsfehler: {error_msg}')
            return

        # Success
        self.serialStream = stream

        # Set initial correction state from checkbox
        self.serialStream.send_correction = self.dockwidget.cbSendCorrection.isChecked()
        
        # Connect cbSendCorrection → stream (main thread, safe)
        self.dockwidget.cbSendCorrection.stateChanged.connect(
            lambda state: self.serialStream and 
            setattr(self.serialStream, 'send_correction', bool(state)))

        # Register thread-bridge callbacks
        self.serialStream.registerEventListener(self._on_serial_position)
        self.serialStream.registerSatEventListener(self._on_serial_satellites)
        self.serialStream.registerDopEventListener(self._on_serial_dop)
        self.serialStream.registerRawEventListener(self._on_serial_nmea_line)

        self._receiver_connected = True
        self._update_receiver_buttons(True)
        port = self.dockwidget.inputSPort.currentText().strip()
        baud = self.dockwidget.inputSBaud.currentText().strip()
        self.out(f'✓ Receiver verbunden: {port} @ {baud} Baud')

        self._ensure_point_layer()
        self._ensure_track_layer()
        self._start_session_recording()

        if self.client:
            self.client.serialStreams = [self.serialStream]

        self.saveSettings()

    def disconnectReceiver(self):
        """Disconnect serial receiver (called from main thread)."""
        if not self._receiver_connected and not self._receiver_connecting:
            return

        # Prevent new callbacks from doing anything
        self._receiver_connected = False
        self._receiver_connecting = False

        # Disconnect the correction-checkbox signal to avoid dangling lambdas
        try:
            self.dockwidget.cbSendCorrection.stateChanged.disconnect()
        except Exception:
            pass

        # Stop the stream (closes port, stops threads)
        stream = self.serialStream
        self.serialStream = None
        if stream:
            try:
                stream.stop()
            except Exception:
                pass

        # Remove serial stream from NTRIP client (don't send corrections to closed port)
        if self.client:
            self.client.serialStreams = []

        # Session recording
        try:
            if self.session_recorder and self.session_recorder.is_recording:
                self.session_recorder.stop()
                self._update_protocol_display()
        except Exception:
            pass

        self._update_receiver_buttons(False)
        self._update_rtk_status(0)
        self._reset_live_labels()
        self._remove_rubber_bands()
        try:
            self.dockwidget.lblReceivedSerialData.setText('RX: 0 B/s')
        except Exception:
            pass
        self.saveSettings()
        self.out('✓ Receiver getrennt.')

    def _update_receiver_buttons(self, connected):
        self.dockwidget.btnConnectReceiver.setEnabled(not connected)
        self.dockwidget.btnDisconnectReceiver.setEnabled(connected)
        if connected:
            self.dockwidget.lblReceiverConnState.setText('● verbunden')
            self.dockwidget.lblReceiverConnState.setStyleSheet(
                'font-size:10px;color:#00AA00;')
        else:
            self.dockwidget.lblReceiverConnState.setText('● getrennt')
            self.dockwidget.lblReceiverConnState.setStyleSheet(
                'font-size:10px;color:#888;')

    # =========================================================================
    # NTRIP Connection (independent)
    # =========================================================================

    def connectNtrip(self):
        """Connect to NTRIP caster (receiver is optional)."""
        if self._ntrip_connected:
            self.out('NTRIP bereits verbunden.')
            return

        host = self.dockwidget.inputHost.text().strip()
        port = self.dockwidget.inputPort.text().strip()
        mp   = self.dockwidget.inputMp.currentText().strip()
        user = self.dockwidget.inputUser.text().strip()
        pw   = self.dockwidget.inputPassword.text()

        if not host or not port or not mp:
            self.out('Fehler: Host, Port und Mountpoint sind erforderlich.')
            return

        try:
            streams = [self.serialStream] if self.serialStream else []

            ntripArgs = {
                'lat': self.current_lat, 'lon': self.current_lon, 'height': self.current_height,
                'ssl': False,
                'user': f'{user}:{pw}',
                'caster': host,
                'host': host,
                'port': int(port),
                'mountpoint': mp if mp.startswith('/') else f'/{mp}',
                'V2': False,
                'streams': streams,
                'dockwidget': self.dockwidget
            }

            self.client = NtripClient(**ntripArgs)
            self.client.registerCorrectionDataEventListener(self._on_rtcm_data)

            # Status messages from NTRIP → update label in UI
            # Disconnect first to avoid duplicate connections on reconnect
            try:
                self.dockwidget.ntripStatusMessage.disconnect()
            except Exception:
                pass
            self.dockwidget.ntripStatusMessage.connect(
                lambda msg: self.dockwidget.lblNtripStatus.setText(msg))

            # Sync GGA-send flag from checkbox to client (main-thread safe)
            try:
                self.dockwidget.cbGGA.stateChanged.connect(
                    lambda state: self.client and self.client.setSendGGA(bool(state)))
            except Exception:
                pass

            self._ntrip_connected = True
            self._update_ntrip_buttons(True)
            # Auto-save the used mountpoint back to the caster profile
            idx = self.dockwidget.comboCaster.currentIndex()
            self.caster_manager.update_mountpoint(idx, mp)
            self.out(f'Verbinde zu {host}:{port}{ntripArgs["mountpoint"]}...')
            self.saveSettings()

        except Exception as e:
            self.out(f'✗ Fehler NTRIP-Verbindung: {e}')

    def disconnectNtrip(self):
        """Disconnect NTRIP caster."""
        if not self._ntrip_connected:
            return
        try:
            self.client.stopThreads()
        except Exception:
            pass
        self.client = None
        self._ntrip_connected = False
        self._update_ntrip_buttons(False)
        try:
            self.dockwidget.lblNtripStatus.setText('Getrennt')
            self.dockwidget.lblReceivedRTCMData.setText('RTCM: 0 B/s')
        except Exception:
            pass
        self.saveSettings()

    def _update_ntrip_buttons(self, connected):
        self.dockwidget.btnConnectNtrip.setEnabled(not connected)
        self.dockwidget.btnDisconnectNtrip.setEnabled(connected)
        if connected:
            self.dockwidget.lblNtripConnState.setText('● verbunden')
            self.dockwidget.lblNtripConnState.setStyleSheet(
                'font-size:10px;color:#00AA00;')
        else:
            self.dockwidget.lblNtripConnState.setText('● getrennt')
            self.dockwidget.lblNtripConnState.setStyleSheet(
                'font-size:10px;color:#888;')

    # =========================================================================
    # Position & Status Updates
    # =========================================================================

    # --- Thread bridge methods (called from serial thread, emit Qt signals) ---
    def _on_serial_position(self, data):
        """Called from serial thread — emits signal to main thread."""
        self.dockwidget.gnssPositionReceived.emit(data)

    def _on_serial_dop(self, dop_data):
        """Called from serial thread — emits signal to main thread."""
        self.dockwidget.dopDataReceived.emit(dop_data)

    def _on_serial_satellites(self, satellites):
        """Called from serial thread — emits signal to main thread."""
        self.dockwidget.satDataReceived.emit(satellites)

    def _on_serial_nmea_line(self, data):
        """Called from serial thread — emits signal to main thread."""
        if isinstance(data, bytes):
            line = data.decode('ascii', errors='ignore').strip()
        else:
            line = str(data).strip()
        if line:
            self.dockwidget.nmeaLineReceived.emit(line)
        # Also feed session recorder if active
        if self.session_recorder and self.session_recorder.is_recording:
            raw = data if isinstance(data, bytes) else str(data).encode()
            self.session_recorder.write_nmea(raw + b'\n')

    # --- Main thread handlers (called via Qt signal, safe for UI updates) ---
    def update_gnss_position(self, data):
        """Handle incoming GNSS position from serial stream."""
        lat = data['lat']
        lon = data['lon']
        height = data.get('alt', 0.0)
        fixtype = data.get('fixtype', 0)
        num_sats = data.get('num_sats', self.current_num_sats)
        hdop = data.get('hdop', self.current_hdop)

        # Antenna height correction
        ant_h = self.dockwidget.spinAntennaHeight.value()
        if ant_h > 0:
            height -= ant_h

        # Height transformation (geoid correction)
        height_orth = height
        if self.dockwidget.grpHeightTransform.isChecked():
            geoid_sep = self.dockwidget.spinGeoidSeparation.value()
            height_orth = height - geoid_sep

        # Store for layer writing
        self.current_num_sats = num_sats
        if hdop > 0:
            self.current_hdop = hdop
        self.current_fixtype = fixtype
        self.current_lat = lat
        self.current_lon = lon
        self.current_height = height

        # Live position rubber band
        self._update_live_position(lon, lat, height_orth, fixtype, self.current_hdop)

        # Update NTRIP position
        if self.client:
            self.client.updateLatLon(lon, lat, height)

        # Update status display
        self._update_rtk_status(fixtype)
        self.dockwidget.lblSatCount.setText(f'Sats:{num_sats}')

        # Feed measurement modules with DOP data
        if self.point_measurement.is_measuring:
            self.point_measurement.add_sample(
                lat, lon, height_orth, fixtype,
                hdop=self.current_hdop, vdop=self.current_vdop,
                pdop=self.current_pdop, num_sats=num_sats)

        if self.track_measurement.is_recording:
            self.track_measurement.add_position(
                lat, lon, height_orth, fixtype,
                hdop=self.current_hdop, vdop=self.current_vdop,
                pdop=self.current_pdop, num_sats=num_sats)

        # Session stats
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder.record_fix_type(fixtype)

    def update_satellites(self, satellites):
        """Buffer satellite data and update map every 3 seconds."""
        self.satellite_buffer = list(satellites) if satellites else []
        
        # Restart 3-second timer
        if self.satellite_update_timer:
            self.satellite_update_timer.stop()
            self.satellite_update_timer.start(3000)
    
    def _flush_satellite_buffer(self):
        """Update satellite visualization with buffered data."""
        if not self.satellite_buffer:
            return
        
        if self.sky_plot:
            self.sky_plot.set_satellites(self.satellite_buffer)
        if self.snr_chart:
            self.snr_chart.set_satellites(self.satellite_buffer)

    def update_dop(self, dop_data):
        self.current_hdop = dop_data.get('hdop', 0.0)
        self.current_vdop = dop_data.get('vdop', 0.0)
        self.current_pdop = dop_data.get('pdop', 0.0)
        self.dockwidget.lblHdop.setText(f'HDOP:{self.current_hdop:.1f}')
        self.dockwidget.lblVdop.setText(f'VDOP:{self.current_vdop:.1f}')
        self.dockwidget.lblPdop.setText(f'PDOP:{self.current_pdop:.1f}')

    def _update_rtk_status(self, fixtype):
        color = _FIX_COLORS.get(fixtype, "#555555")
        text = fix_str(fixtype)
        self.dockwidget.lblRtkStatus.setText(text)
        self.dockwidget.lblRtkStatus.setStyleSheet(
            f'font-weight:bold;padding:2px 6px;background:{color};'
            f'color:white;border-radius:3px;')

    def _reset_live_labels(self):
        self.dockwidget.lblLiveLat.setText('Lat: --')
        self.dockwidget.lblLiveLon.setText('Lon: --')
        self.dockwidget.lblLiveH.setText('H: --')
        self.dockwidget.lblLiveAcc.setText('Acc: --')

    # =========================================================================
    # Live Position on Map (Rubber Bands)
    # =========================================================================

    def _init_rubber_bands(self):
        """Create rubber band objects for live position display."""
        canvas = self.iface.mapCanvas()

        # Position marker (cross)
        self.live_rb = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self.live_rb.setColor(QColor(0, 180, 255, 230))
        self.live_rb.setIconSize(14)
        self.live_rb.setIcon(QgsRubberBand.ICON_CROSS)
        self.live_rb.setWidth(2)

        # Accuracy circle
        self.accuracy_rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.accuracy_rb.setColor(QColor(0, 180, 255, 35))
        self.accuracy_rb.setStrokeColor(QColor(0, 180, 255, 120))
        self.accuracy_rb.setWidth(1)

    def _remove_rubber_bands(self):
        """Remove rubber bands from map canvas."""
        if self.live_rb:
            self.live_rb.reset()
            self.live_rb = None
        if self.accuracy_rb:
            self.accuracy_rb.reset()
            self.accuracy_rb = None

    def _update_live_position(self, lon, lat, height, fixtype, hdop):
        """Move live position marker and accuracy circle on the map."""
        if not self.live_rb:
            self._init_rubber_bands()

        # Transform WGS84 → map CRS
        crs_wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        crs_map = self.iface.mapCanvas().mapSettings().destinationCrs()
        xform = QgsCoordinateTransform(crs_wgs84, crs_map, QgsProject.instance())

        try:
            pt_map = xform.transform(QgsPointXY(lon, lat))
        except Exception:
            return

        # Update position cross
        self.live_rb.reset(QgsWkbTypes.PointGeometry)
        self.live_rb.addPoint(pt_map)

        # Color based on fix type
        color = QColor(_FIX_COLORS.get(fixtype, '#555555'))
        color.setAlpha(220)
        self.live_rb.setColor(color)
        self.live_rb.setStrokeColor(color)

        # Accuracy circle
        accuracy_m = self._estimate_accuracy(fixtype, hdop)
        self._draw_accuracy_circle(lon, lat, accuracy_m, xform)

        # Update live position labels
        self.dockwidget.lblLiveLat.setText(f'Lat: {lat:.8f}')
        self.dockwidget.lblLiveLon.setText(f'Lon: {lon:.8f}')
        self.dockwidget.lblLiveH.setText(f'H: {height:.3f}m')
        self.dockwidget.lblLiveAcc.setText(f'Acc: ±{accuracy_m:.2f}m')

    def _estimate_accuracy(self, fixtype, hdop):
        """Estimate horizontal accuracy in meters from fixtype and HDOP."""
        if hdop <= 0:
            hdop = 1.0
        uere = {0: 999, 1: 3.0, 2: 1.0, 4: 0.02, 5: 0.2}.get(fixtype, 5.0)
        return hdop * uere

    def _draw_accuracy_circle(self, lon, lat, radius_m, xform, n_pts=36):
        """Draw an approximate circle at (lon, lat) with given radius in meters."""
        self.accuracy_rb.reset(QgsWkbTypes.PolygonGeometry)
        if radius_m > 10000:  # Don't draw if > 10 km (no fix / nonsensical)
            return

        lat_r = math.radians(lat)
        lat_deg_per_m = 1.0 / 111320.0
        lon_deg_per_m = 1.0 / (111320.0 * max(0.001, math.cos(lat_r)))

        pts = []
        for i in range(n_pts):
            angle = 2 * math.pi * i / n_pts
            pt_lon = lon + radius_m * lon_deg_per_m * math.cos(angle)
            pt_lat = lat + radius_m * lat_deg_per_m * math.sin(angle)
            try:
                pts.append(xform.transform(QgsPointXY(pt_lon, pt_lat)))
            except Exception:
                return

        self.accuracy_rb.setToGeometry(
            QgsGeometry.fromPolygonXY([pts]), None)

    # =========================================================================
    # Caster Management
    # =========================================================================

    def _populate_caster_combo(self):
        self.dockwidget.comboCaster.blockSignals(True)
        self.dockwidget.comboCaster.clear()
        self.dockwidget.comboCaster.addItems(self.caster_manager.get_names())
        self.dockwidget.comboCaster.blockSignals(False)

    def _on_caster_selected(self, index):
        caster = self.caster_manager.get(index)
        if not caster:
            return
        self.dockwidget.inputHost.setText(caster.get('host', ''))
        self.dockwidget.inputPort.setText(caster.get('port', '2101'))
        self.dockwidget.inputMp.setCurrentText(caster.get('mountpoint', ''))
        self.dockwidget.inputUser.setText(caster.get('user', ''))
        self.dockwidget.inputPassword.setText(caster.get('password', ''))
        nv = caster.get('ntrip_version', '1')
        self.dockwidget.selectNtripVersion.setCurrentIndex(0 if nv == '1' else 1)
        # Persist the selected caster index
        self.settings.setValue(f'{self.settings_prefix}/lastCasterIndex', index)

    def _on_add_caster(self):
        name, ok = QInputDialog.getText(
            self.dockwidget, 'Caster hinzufügen', 'Name:')
        if ok and name:
            self.caster_manager.add(
                name=name,
                host=self.dockwidget.inputHost.text(),
                port=self.dockwidget.inputPort.text(),
                mountpoint=self.dockwidget.inputMp.currentText(),
                user=self.dockwidget.inputUser.text(),
                password=self.dockwidget.inputPassword.text(),
                ntrip_version=self.dockwidget.selectNtripVersion.currentText())
            self._populate_caster_combo()
            self.dockwidget.comboCaster.setCurrentIndex(self.caster_manager.count() - 1)
            self.out(f'Caster "{name}" gespeichert.')

    def _on_edit_caster(self):
        index = self.dockwidget.comboCaster.currentIndex()
        if index < 0:
            return
        name, ok = QInputDialog.getText(
            self.dockwidget, 'Caster bearbeiten', 'Name:',
            text=self.dockwidget.comboCaster.currentText())
        if ok and name:
            self.caster_manager.update(
                index=index, name=name,
                host=self.dockwidget.inputHost.text(),
                port=self.dockwidget.inputPort.text(),
                mountpoint=self.dockwidget.inputMp.currentText(),
                user=self.dockwidget.inputUser.text(),
                password=self.dockwidget.inputPassword.text(),
                ntrip_version=self.dockwidget.selectNtripVersion.currentText())
            self._populate_caster_combo()
            self.dockwidget.comboCaster.setCurrentIndex(index)
            self.out(f'Caster "{name}" aktualisiert.')

    def _on_delete_caster(self):
        index = self.dockwidget.comboCaster.currentIndex()
        if index < 0:
            return
        name = self.dockwidget.comboCaster.currentText()
        if QMessageBox.question(
                self.dockwidget, 'Caster löschen',
                f'Caster "{name}" wirklich löschen?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.caster_manager.delete(index)
            self._populate_caster_combo()
            self.out(f'Caster "{name}" gelöscht.')

    def _on_mountpoint_selected(self, index):
        """When user picks a mountpoint from dropdown, set only the mountpoint name."""
        name = self.dockwidget.inputMp.itemData(index)
        if name:
            self.dockwidget.inputMp.setCurrentText(name)

    # =========================================================================
    # COM Port Refresh
    # =========================================================================

    def _refresh_com_ports(self):
        """Scan and populate available serial COM ports."""
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except ImportError:
            # Fallback: common port names
            ports = [f'COM{i}' for i in range(1, 13)]

        current = self.dockwidget.inputSPort.currentText()
        self.dockwidget.inputSPort.blockSignals(True)
        self.dockwidget.inputSPort.clear()
        self.dockwidget.inputSPort.addItems(ports)
        if current in ports:
            self.dockwidget.inputSPort.setCurrentText(current)
        elif ports:
            self.dockwidget.inputSPort.setCurrentIndex(0)
        self.dockwidget.inputSPort.blockSignals(False)
        self.out(f'COM-Ports aktualisiert: {", ".join(ports) or "keine gefunden"}')

    # =========================================================================
    # Measurement
    # =========================================================================

    def _on_measure_point(self):
        if not self._receiver_connected:
            self.out('Kein Receiver verbunden.')
            return

        if self.point_measurement.is_measuring:
            self.point_measurement.cancel()
            self.dockwidget.btnMeasurePoint.setText('Punkt messen')
            self.dockwidget.lblMeasProgress.setText('Abgebrochen')
            return

        use_avg = self.dockwidget.cbAveraging.isChecked()
        epochs = self.dockwidget.spinAvgEpochs.value()
        self.point_measurement.start(use_averaging=use_avg, epochs=epochs)
        self.dockwidget.btnMeasurePoint.setText('Abbrechen')
        self.dockwidget.lblMeasProgress.setText('Messe...')

    def _on_measurement_complete(self, result):
        self.dockwidget.btnMeasurePoint.setText('Punkt messen')
        lat = result['lat']
        lon = result['lon']
        height = result['height']
        n = result['samples']
        hdop = result['hdop']

        self.dockwidget.lblMeasLat.setText(f'Lat: {lat:.8f}')
        self.dockwidget.lblMeasLon.setText(f'Lon: {lon:.8f}')
        self.dockwidget.lblMeasHeight.setText(f'H: {height:.3f}m')
        self.dockwidget.lblMeasProgress.setText(f'{n} Epochen')

        std_lat = result.get('std_lat_m', 0)
        std_lon = result.get('std_lon_m', 0)
        std_h   = result.get('std_h_m', 0)
        acc_txt = (f'σLat:{std_lat:.4f}m  σLon:{std_lon:.4f}m  σH:{std_h:.4f}m  HDOP:{hdop:.1f}'
                   if n > 1 else f'HDOP:{hdop:.1f}')
        self.dockwidget.lblMeasAccuracy.setText(acc_txt)

        # Write to point layer
        self._write_point_to_layer(result)

        # Session protocol
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder.record_point_measured(result)
            self._update_protocol_display()

        self.out(f'Punkt {self._point_count()}: Lat={lat:.8f} Lon={lon:.8f} H={height:.3f}m '
                 f'Fix={fix_str(result["fixtype"])} n={n}')

    def _on_measurement_progress(self, current, total):
        self.dockwidget.lblMeasProgress.setText(f'{current}/{total}')

    def _on_start_track(self):
        if not self._receiver_connected:
            self.out('Kein Receiver verbunden.')
            return
        t_int = self.dockwidget.spinTimeInterval.value()
        d_int = self.dockwidget.spinDistInterval.value()
        self.track_measurement.start(time_interval=t_int, distance_interval=d_int)
        self.dockwidget.btnStartTrack.setEnabled(False)
        self.dockwidget.btnStopTrack.setEnabled(True)
        self.out(f'Track gestartet (t={t_int}s, d={d_int}m)')

    def _on_stop_track(self):
        points = self.track_measurement.stop()
        self.dockwidget.btnStartTrack.setEnabled(True)
        self.dockwidget.btnStopTrack.setEnabled(False)
        self.out(f'Track gestoppt: {len(points)} Punkte')

    def _on_track_point(self, point):
        self._write_track_point_to_layer(point)
        count = self.track_measurement.point_count
        self.dockwidget.lblTrackPointCount.setText(f'Punkte:{count}')
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder.record_track_point()

    def _point_count(self):
        """Current number of features in the point layer."""
        if self.point_layer:
            return self.point_layer.featureCount()
        return 0

    # =========================================================================
    # Receiver Configuration
    # =========================================================================

    def _on_receiver_config(self):
        if not self._receiver_connected:
            self.out('Fehler: Kein Receiver verbunden.')
            return
        rt = self.dockwidget.comboReceiverType.currentText()
        if QMessageBox.question(
                self.dockwidget, 'Receiver konfigurieren',
                f'Receiver "{rt}" konfigurieren?\n'
                f'Konfigurationsbefehle werden gesendet.',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            cfg = get_receiver_config(rt, self.serialStream)
            cfg.configure()
            self.out(f'Receiver "{rt}" konfiguriert.')

    # =========================================================================
    # Session Recording
    # =========================================================================

    def _start_session_recording(self):
        if not self.dockwidget.grpAutoRecord.isChecked():
            return
        folder = self.dockwidget.fileSelectorTempFolder.filePath() or self.temp_folder
        rec_nmea = self.dockwidget.cbRecordReceiver.isChecked()
        rec_rtcm = self.dockwidget.cbRecordRTCM.isChecked()

        self.session_recorder = SessionRecorder(folder)
        self.session_recorder.start(record_nmea=rec_nmea, record_rtcm=rec_rtcm)

        self.out(f'Aufzeichnung: {self.session_recorder.session_folder}')

    def _on_nmea_log(self, line):
        """Append NMEA line to the log window (main thread, via signal)."""
        try:
            self.dockwidget.txtNmeaLog.appendPlainText(line)
        except Exception:
            pass

    def _on_rtcm_data(self, data):
        if (self.session_recorder and self.session_recorder.is_recording
                and isinstance(data, bytes)):
            self.session_recorder.write_rtcm(data)

    def _update_protocol_display(self):
        if self.session_recorder:
            self.dockwidget.txtProtocol.setPlainText(
                self.session_recorder.get_protocol_text())

    def _on_export_protocol(self):
        if not self.session_recorder:
            self.out('Kein Protokoll vorhanden.')
            return
        path, _ = QFileDialog.getSaveFileName(
            self.dockwidget, 'Protokoll exportieren', '', 'Text (*.txt)')
        if path:
            self.session_recorder.export_protocol(path)
            self.out(f'Protokoll exportiert: {path}')

    # =========================================================================
    # Layer Management (rich attributes)
    # =========================================================================

    def _ensure_point_layer(self):
        """Create the measured-points layer with full attributes if not existing."""
        name = self.dockwidget.layerName.text() or 'gnss_punkte'
        existing = QgsProject.instance().mapLayersByName(name)
        if existing:
            self.point_layer = existing[0]
            return

        layer = QgsVectorLayer('Point?crs=EPSG:4326', name, 'memory')
        layer.dataProvider().addAttributes([
            QgsField('Zeitstempel',   QVariant.String),
            QgsField('Lat',           QVariant.Double),
            QgsField('Lon',           QVariant.Double),
            QgsField('H_ellips',      QVariant.Double),   # ellipsoidal height
            QgsField('H_orth',        QVariant.Double),   # orthometric (geoid)
            QgsField('Fixtype',       QVariant.Int),
            QgsField('FixtypeStr',    QVariant.String),
            QgsField('HDOP',          QVariant.Double),
            QgsField('VDOP',          QVariant.Double),
            QgsField('PDOP',          QVariant.Double),
            QgsField('NumSats',       QVariant.Int),
            QgsField('Epochen',       QVariant.Int),
            QgsField('Gemittelt',     QVariant.Int),      # 0/1 boolean
            QgsField('StdLat_m',      QVariant.Double),
            QgsField('StdLon_m',      QVariant.Double),
            QgsField('StdH_m',        QVariant.Double),
            QgsField('AntH_m',        QVariant.Double),
            QgsField('GeoidSep_m',    QVariant.Double),
            QgsField('GeoidModell',   QVariant.String),
            QgsField('ReceiverTyp',   QVariant.String),
            QgsField('CasterName',    QVariant.String),
            QgsField('PunktNr',       QVariant.Int),
        ])
        layer.updateFields()
        QgsProject.instance().addMapLayer(layer)
        self.point_layer = layer

    def _ensure_track_layer(self):
        """Create the track layer with full attributes if not existing."""
        name = self.dockwidget.layerNameTrack.text() or 'gnss_track'
        existing = QgsProject.instance().mapLayersByName(name)
        if existing:
            self.track_layer = existing[0]
            return

        layer = QgsVectorLayer('Point?crs=EPSG:4326', name, 'memory')
        layer.dataProvider().addAttributes([
            QgsField('Zeitstempel',   QVariant.String),
            QgsField('Lat',           QVariant.Double),
            QgsField('Lon',           QVariant.Double),
            QgsField('H_ellips',      QVariant.Double),
            QgsField('H_orth',        QVariant.Double),
            QgsField('Fixtype',       QVariant.Int),
            QgsField('FixtypeStr',    QVariant.String),
            QgsField('HDOP',          QVariant.Double),
            QgsField('VDOP',          QVariant.Double),
            QgsField('PDOP',          QVariant.Double),
            QgsField('NumSats',       QVariant.Int),
            QgsField('AntH_m',        QVariant.Double),
            QgsField('GeoidSep_m',    QVariant.Double),
            QgsField('TrackID',       QVariant.String),
            QgsField('PunktNr',       QVariant.Int),
        ])
        layer.updateFields()
        QgsProject.instance().addMapLayer(layer)
        self.track_layer = layer

    def _write_point_to_layer(self, result):
        """Add a measured point with full attributes to the point layer."""
        if not self.point_layer:
            self._ensure_point_layer()

        ant_h = self.dockwidget.spinAntennaHeight.value()
        geoid_sep = (self.dockwidget.spinGeoidSeparation.value()
                     if self.dockwidget.grpHeightTransform.isChecked() else 0.0)
        geoid_model = (self.dockwidget.comboGeoidModel.currentText()
                       if self.dockwidget.grpHeightTransform.isChecked() else '')
        receiver_type = self.dockwidget.comboReceiverType.currentText()
        caster_name = self.dockwidget.comboCaster.currentText()
        point_nr = self._point_count() + 1

        h_ellips = result['height'] + geoid_sep  # reverse to get ellipsoidal
        h_orth = result['height']

        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(result['lon'], result['lat'])))
        feat.setAttributes([
            result['timestamp'],
            result['lat'],
            result['lon'],
            h_ellips,
            h_orth,
            result['fixtype'],
            fix_str(result['fixtype']),
            round(result.get('hdop', 0.0), 3),
            round(result.get('vdop', 0.0), 3),
            round(result.get('pdop', 0.0), 3),
            result.get('num_sats', 0),
            result.get('samples', 1),
            1 if result.get('averaged') else 0,
            round(result.get('std_lat_m', 0.0), 6),
            round(result.get('std_lon_m', 0.0), 6),
            round(result.get('std_h_m', 0.0), 6),
            ant_h,
            geoid_sep,
            geoid_model,
            receiver_type,
            caster_name,
            point_nr,
        ])
        ok, _ = self.point_layer.dataProvider().addFeatures([feat])
        if not ok:
            self.out('⚠ Punkt konnte nicht in Layer geschrieben werden.')
            return
        self.point_layer.updateExtents()
        self.point_layer.triggerRepaint()

    def _write_track_point_to_layer(self, point):
        """Add a track point with full attributes to the track layer."""
        if not self.track_layer:
            self._ensure_track_layer()

        ant_h = self.dockwidget.spinAntennaHeight.value()
        geoid_sep = (self.dockwidget.spinGeoidSeparation.value()
                     if self.dockwidget.grpHeightTransform.isChecked() else 0.0)

        h_ellips = point['height'] + geoid_sep
        h_orth = point['height']

        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(point['lon'], point['lat'])))
        feat.setAttributes([
            point['timestamp'],
            point['lat'],
            point['lon'],
            h_ellips,
            h_orth,
            point['fixtype'],
            fix_str(point['fixtype']),
            round(point.get('hdop', 0.0), 3),
            round(point.get('vdop', 0.0), 3),
            round(point.get('pdop', 0.0), 3),
            point.get('num_sats', 0),
            ant_h,
            geoid_sep,
            point.get('track_id', ''),
            point.get('point_num', 0),
        ])
        ok, _ = self.track_layer.dataProvider().addFeatures([feat])
        if not ok:
            return
        self.track_layer.updateExtents()
        self.track_layer.triggerRepaint()

    # =========================================================================
    # UI Helpers
    # =========================================================================

    def out(self, message):
        ts = datetime.now().strftime('%H:%M:%S')
        history = self.dockwidget.output.toPlainText()
        self.dockwidget.output.setPlainText(f'{ts} {message}\n{history}')
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder.add_protocol_entry(message)

    def _setup_satellite_widgets(self):
        self.sky_plot = SkyPlotWidget()
        self.sky_plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        sky_layout = self.dockwidget.skyPlotWidget.layout()
        if sky_layout is None:
            sky_layout = QVBoxLayout(self.dockwidget.skyPlotWidget)
            sky_layout.setContentsMargins(0, 0, 0, 0)
        sky_layout.addWidget(self.sky_plot)
        self.sky_plot.show()

        self.snr_chart = SnrBarWidget()
        self.snr_chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        snr_layout = self.dockwidget.snrChartWidget.layout()
        if snr_layout is None:
            snr_layout = QVBoxLayout(self.dockwidget.snrChartWidget)
            snr_layout.setContentsMargins(0, 0, 0, 0)
        snr_layout.addWidget(self.snr_chart)
        self.snr_chart.show()

    def _apply_modern_style(self):
        """Minimal classic stylesheet – keeps QGIS defaults, only styles RTK badges."""
        # No global override; individual widgets are styled inline via setStyleSheet()
        pass

    def _fetch_mountpoints(self):
        """Fetch available mountpoints from NTRIP caster in background thread."""
        host = self.dockwidget.inputHost.text().strip()
        port = self.dockwidget.inputPort.text().strip()
        user = self.dockwidget.inputUser.text().strip()
        pw   = self.dockwidget.inputPassword.text()

        if not host or not port:
            self.out('Fehler: Host und Port angeben.')
            return

        self.dockwidget.btnFetchMountpoints.setEnabled(False)
        self.out(f'Lade Mountpoints von {host}:{port}...')

        def worker():
            try:
                mps = NtripClient.fetch_sourcetable(host, int(port), user, pw)
                self.dockwidget.satDataReceived.emit(mps)   # reuse signal for passing list
            except Exception as exc:
                self.dockwidget.nmeaLineReceived.emit(f'[FEHLER Mountpoints] {exc}')

        # Use a dedicated signal path: emit via a new approach using QTimer
        import threading
        from qgis.PyQt.QtCore import QTimer

        def on_done(mps):
            self.dockwidget.btnFetchMountpoints.setEnabled(True)
            if not mps:
                self.out('Keine Mountpoints gefunden.')
                return
            current = self.dockwidget.inputMp.currentText()
            self.dockwidget.inputMp.blockSignals(True)
            self.dockwidget.inputMp.clear()
            for mp in mps:
                label = mp['name']
                if mp.get('format'):
                    label += f"  [{mp['format']}]"
                if mp.get('country'):
                    label += f"  {mp['country']}"
                self.dockwidget.inputMp.addItem(label, mp['name'])
            self.dockwidget.inputMp.blockSignals(False)
            # Restore previous value or select first
            if current:
                idx = self.dockwidget.inputMp.findText(current)
                if idx >= 0:
                    self.dockwidget.inputMp.setCurrentIndex(idx)
                else:
                    self.dockwidget.inputMp.setCurrentText(current)
            self.out(f'{len(mps)} Mountpoints geladen.')

        result_holder = []
        error_holder = []

        def bg():
            try:
                mps = NtripClient.fetch_sourcetable(host, int(port), user, pw)
                result_holder.extend(mps)
            except Exception as exc:
                error_holder.append(str(exc))

        def check_done():
            if t.is_alive():
                QTimer.singleShot(200, check_done)
                return
            self.dockwidget.btnFetchMountpoints.setEnabled(True)
            if error_holder:
                self.out(f'Fehler beim Laden: {error_holder[0]}')
            else:
                on_done(result_holder)

        t = threading.Thread(target=bg, daemon=True)
        t.start()
        QTimer.singleShot(200, check_done)

    # =========================================================================
    # Plugin Run
    # =========================================================================

    def run(self):
        try:
            if not self.pluginIsActive:
                self.pluginIsActive = True

                if self.dockwidget is None:
                    self.dockwidget = QNTRIPClientDockWidget()

                self.dockwidget.closingPlugin.connect(self.onClosePlugin)
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)

                self._setup_satellite_widgets()
                self._apply_modern_style()
                self._populate_caster_combo()
                self.dockwidget.fileSelectorTempFolder.setFilePath(self.temp_folder)
                
                # Initialize satellite update timer (3-second buffering)
                from qgis.PyQt.QtCore import QTimer
                self.satellite_update_timer = QTimer()
                self.satellite_update_timer.setSingleShot(True)
                self.satellite_update_timer.timeout.connect(self._flush_satellite_buffer)
                
                # Refresh COM ports first (populate list)
                self._refresh_com_ports()
                
                # Load saved settings (after ports are available)
                self.loadSettings()
                
                # Initialize button states
                self._update_receiver_buttons(False)
                self._update_ntrip_buttons(False)
                
                # Deaktiviere Receiver-Config Button (für zukünftige Verwendung)
                self.dockwidget.btnReceiverConfig.setEnabled(False)

                # Receiver signals
                self.dockwidget.btnConnectReceiver.clicked.connect(self.connectReceiver)
                self.dockwidget.btnDisconnectReceiver.clicked.connect(self.disconnectReceiver)
                self.dockwidget.btnRefreshPorts.clicked.connect(self._refresh_com_ports)
                self.dockwidget.btnReceiverConfig.clicked.connect(self._on_receiver_config)

                # NTRIP signals
                self.dockwidget.btnConnectNtrip.clicked.connect(self.connectNtrip)
                self.dockwidget.btnDisconnectNtrip.clicked.connect(self.disconnectNtrip)
                self.dockwidget.btnFetchMountpoints.clicked.connect(self._fetch_mountpoints)
                # When a mountpoint is selected from dropdown, set only the name part
                self.dockwidget.inputMp.activated.connect(self._on_mountpoint_selected)

                # Caster management
                self.dockwidget.comboCaster.currentIndexChanged.connect(self._on_caster_selected)
                self.dockwidget.btnAddCaster.clicked.connect(self._on_add_caster)
                self.dockwidget.btnEditCaster.clicked.connect(self._on_edit_caster)
                self.dockwidget.btnDeleteCaster.clicked.connect(self._on_delete_caster)

                # Measurement
                self.dockwidget.btnMeasurePoint.clicked.connect(self._on_measure_point)
                self.point_measurement.measurement_complete.connect(self._on_measurement_complete)
                self.point_measurement.progress_update.connect(self._on_measurement_progress)

                self.dockwidget.btnStartTrack.clicked.connect(self._on_start_track)
                self.dockwidget.btnStopTrack.clicked.connect(self._on_stop_track)
                self.track_measurement.point_recorded.connect(self._on_track_point)

                # Protocol
                self.dockwidget.btnExportProtocol.clicked.connect(self._on_export_protocol)

                # Thread-safe GNSS data signals (serial thread → main thread)
                self.dockwidget.gnssPositionReceived.connect(self.update_gnss_position)
                self.dockwidget.dopDataReceived.connect(self.update_dop)
                self.dockwidget.satDataReceived.connect(self.update_satellites)
                self.dockwidget.nmeaLineReceived.connect(self._on_nmea_log)
                self.dockwidget.receiverConnectResult.connect(self._on_receiver_connect_result)
                self.dockwidget.rxBytesUpdate.connect(
                    lambda n: self.dockwidget.lblReceivedSerialData.setText(f'RX: {n} B/s'))
                self.dockwidget.rtcmBytesUpdate.connect(
                    lambda n: self.dockwidget.lblReceivedRTCMData.setText(f'RTCM: {n} B/s'))

                # Restore last selected caster (don't load if no casters exist)
                if self.caster_manager.count() > 0:
                    last_idx = int(self.settings.value(
                        f'{self.settings_prefix}/lastCasterIndex', 0) or 0)
                    last_idx = min(last_idx, self.caster_manager.count() - 1)
                    self.dockwidget.comboCaster.setCurrentIndex(last_idx)
                    self._on_caster_selected(last_idx)

                self.dockwidget.show()

        except Exception as e:
            import traceback
            traceback.print_exc()
