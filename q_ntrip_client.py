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
                                  QFileDialog, QVBoxLayout, QSizePolicy,
                                  QDialog, QDialogButtonBox, QFormLayout,
                                  QLabel, QLineEdit, QDoubleSpinBox)

from .resources import *

from qgis.core import (QgsProject, QgsPointXY, QgsFeature, QgsGeometry,
                        QgsVectorLayer, QgsField,
                        QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                        QgsDistanceArea, QgsWkbTypes, QgsLayerTreeLayer)
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


class PunktSpeichernDialog(QDialog):
    """Modal dialog shown after measurement when Auto-Speichern is disabled."""

    def __init__(self, punkt_nr, x, y, h_orth, kommentar, inst_h, crs_label, fixtype=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Punkt speichern')
        self.setMinimumWidth(320)

        form = QFormLayout(self)
        form.setSpacing(6)

        # Fix type with color
        fix_label = QLabel(fix_str(fixtype))
        fix_color = _FIX_COLORS.get(fixtype, '#555555')
        fix_label.setStyleSheet(
            f'font-weight:bold; padding:2px 8px; border-radius:3px;'
            f'background:{fix_color}; color:white;')
        form.addRow('Fix:', fix_label)

        self._edit_nr = QLineEdit(punkt_nr)
        self._edit_nr.setMaxLength(50)
        form.addRow('Punktnr.:', self._edit_nr)

        form.addRow(f'X ({crs_label}):', QLabel(f'{x:.3f}'))
        form.addRow(f'Y ({crs_label}):', QLabel(f'{y:.3f}'))

        self._spin_h = QDoubleSpinBox()
        self._spin_h.setDecimals(4)
        self._spin_h.setRange(-9999.0, 9999.0)
        self._spin_h.setSingleStep(0.001)
        self._spin_h.setValue(h_orth)
        form.addRow('H Antenne [m]:', self._spin_h)

        self._spin_inst_h = QDoubleSpinBox()
        self._spin_inst_h.setDecimals(3)
        self._spin_inst_h.setRange(0.0, 10.0)
        self._spin_inst_h.setSingleStep(0.001)
        self._spin_inst_h.setValue(inst_h)
        form.addRow('Inst.H [m]:', self._spin_inst_h)

        self._lbl_h_boden = QLabel()
        self._lbl_h_boden.setStyleSheet('font-weight:bold;')
        self._update_h_boden()
        form.addRow('H Boden [m]:', self._lbl_h_boden)

        self._spin_h.valueChanged.connect(self._update_h_boden)
        self._spin_inst_h.valueChanged.connect(self._update_h_boden)

        self._edit_kommentar = QLineEdit(kommentar)
        self._edit_kommentar.setMaxLength(200)
        form.addRow('Kommentar:', self._edit_kommentar)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Discard,
            parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        # Map Discard → reject
        buttons.button(QDialogButtonBox.StandardButton.Discard).clicked.connect(self.reject)
        form.addRow(buttons)

    def _update_h_boden(self):
        h_boden = self._spin_h.value() - self._spin_inst_h.value()
        self._lbl_h_boden.setText(f'{h_boden:.4f}')

    @property
    def punkt_nr(self):
        return self._edit_nr.text().strip()

    @property
    def h_orth(self):
        return self._spin_h.value()

    @property
    def inst_h(self):
        return self._spin_inst_h.value()

    @property
    def kommentar(self):
        return self._edit_kommentar.text().strip()


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
        self.track_layer = None   # Track line segments
        self._layer_group = None  # QGIS layer tree group for current session
        self._layer_group_name = ''  # name of the group (for robust lookup)

        # Track segment state
        self._last_track_point = None   # Previous point dict for segment creation
        self._track_segment_count = 0   # Segment counter within current track

        # Projected CRS for E/N coordinates (default UTM Zone 32N)
        self._projected_crs = QgsCoordinateReferenceSystem('EPSG:25832')

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
        # Save only the device port (e.g., 'COM1'), not the full display text
        self.settings.setValue(f'{self.settings_prefix}/serialPort', 
                              self._extract_port_device(self.dockwidget.inputSPort.currentText()))
        self.settings.setValue(f'{self.settings_prefix}/serialBaud', 
                              self.dockwidget.inputSBaud.currentText())
        self.settings.setValue(f'{self.settings_prefix}/receiverType', 
                              self.dockwidget.comboReceiverType.currentText())
        self.settings.setValue(f'{self.settings_prefix}/instrumentSN',
                              self.dockwidget.txtInstrumentSN.text())
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
        self.settings.setValue(f'{self.settings_prefix}/geoidSeparation', 
                              self.dockwidget.spinGeoidSeparation.value())
        
        # Layer-Namen
        self.settings.setValue(f'{self.settings_prefix}/layerName',
                              self.dockwidget.layerName.text())
        self.settings.setValue(f'{self.settings_prefix}/layerNameTrack',
                              self.dockwidget.layerNameTrack.text())
        self.settings.setValue(f'{self.settings_prefix}/projectedCrs',
                              self._projected_crs.authid())
        
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
        self.settings.setValue(f'{self.settings_prefix}/instH',
                              self.dockwidget.spinInstH.value())
        self.settings.setValue(f'{self.settings_prefix}/autoSave',
                              self.dockwidget.cbAutoSave.isChecked())
        
        # No print statement - save silently

    def loadSettings(self):
        """Lädt alle gespeicherten Einstellungen."""
        if not self.dockwidget:
            return
        
        # Serielle Verbindung
        port = self.settings.value(f'{self.settings_prefix}/serialPort', 'COM1')
        if port:
            # port is stored as device only (e.g., 'COM1')
            # try to find a matching entry in the combo
            for i in range(self.dockwidget.inputSPort.count()):
                entry = self.dockwidget.inputSPort.itemText(i)
                if self._extract_port_device(entry) == port:
                    self.dockwidget.inputSPort.setCurrentIndex(i)
                    break
        
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
        
        inst_sn = self.settings.value(f'{self.settings_prefix}/instrumentSN', '')
        self.dockwidget.txtInstrumentSN.setText(str(inst_sn))

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
        
        geoid_s = self.settings.value(f'{self.settings_prefix}/geoidSeparation', 0.0)
        self.dockwidget.spinGeoidSeparation.setValue(float(geoid_s))
        
        # Layer-Namen
        self.dockwidget.layerName.setText(
            self.settings.value(f'{self.settings_prefix}/layerName', 'gnss_punkte'))
        self.dockwidget.layerNameTrack.setText(
            self.settings.value(f'{self.settings_prefix}/layerNameTrack', 'gnss_track'))
        saved_crs = self.settings.value(f'{self.settings_prefix}/projectedCrs', 'EPSG:25832')
        self._projected_crs = QgsCoordinateReferenceSystem(saved_crs)
        if not self._projected_crs.isValid():
            self._projected_crs = QgsCoordinateReferenceSystem('EPSG:25832')
        self.dockwidget.btnSelectCrs.setText(
            f'{self._projected_crs.authid()} – {self._projected_crs.description()}')
        
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

        inst_h = self.settings.value(f'{self.settings_prefix}/instH', 0.0)
        try:
            self.dockwidget.spinInstH.setValue(float(inst_h))
        except (ValueError, TypeError):
            self.dockwidget.spinInstH.setValue(0.0)

        auto_save = self.settings.value(f'{self.settings_prefix}/autoSave', True)
        if isinstance(auto_save, str):
            auto_save = auto_save.lower() in ('true', '1', 'yes')
        else:
            auto_save = bool(auto_save)
        self.dockwidget.cbAutoSave.setChecked(auto_save)

        # Load complete

    def unload(self):
        # Stop session recording so buffered data + protocol are written to disk
        try:
            if self.session_recorder and self.session_recorder.is_recording:
                self.session_recorder.stop()
        except Exception:
            pass
        self._remove_rubber_bands()
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&QNTRIPClient'), action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

    # =========================================================================
    # RECEIVER Connection (independent)
    # =========================================================================

    def _extract_port_device(self, combo_text):
        """Extract the actual device name (e.g., 'COM1') from the combo text.
        
        The combo can contain formatted text like 'COM1 - u-blox GNSS receiver'.
        This extracts just 'COM1'.
        """
        if not combo_text:
            return ''
        # Split on ' - ' and take the first part (the actual port)
        return combo_text.split(' - ')[0].strip()

    def connectReceiver(self):
        """Start a background thread to open the serial port (non-blocking)."""
        if self._receiver_connected or self._receiver_connecting:
            return

        serial_port = self._extract_port_device(self.dockwidget.inputSPort.currentText())
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

            port = self._extract_port_device(self.dockwidget.inputSPort.currentText())
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
        port = self._extract_port_device(self.dockwidget.inputSPort.currentText())
        baud = self.dockwidget.inputSBaud.currentText().strip()
        self.out(f'✓ Receiver verbunden: {port} @ {baud} Baud')

        # Create a dated layer group for this session
        root = QgsProject.instance().layerTreeRoot()
        self._layer_group_name = f'GNSS {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        self._layer_group = root.insertGroup(0, self._layer_group_name)
        self.out(f'Layer-Gruppe erstellt: "{self._layer_group_name}" (valid={self._layer_group is not None})')
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
            # Update session recorder with NTRIP info
            if self.session_recorder and self.session_recorder.is_recording:
                self.session_recorder.set_connection_info(
                    instrument_type=self.dockwidget.comboReceiverType.currentText(),
                    instrument_sn=self.dockwidget.txtInstrumentSN.text().strip(),
                    ntrip_caster=self.dockwidget.comboCaster.currentText(),
                    ntrip_mountpoint=mp,
                    ntrip_host=host,
                    ntrip_port=port,
                    projected_crs=self._projected_crs.authid(),
                    geoid_model='',
                    geoid_separation=(self.dockwidget.spinGeoidSeparation.value()
                                      if self.dockwidget.grpHeightTransform.isChecked() else 0.0),
                )
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
        # File write first (independent of UI signal) so an emit error can't skip it
        if self.session_recorder and self.session_recorder.is_recording:
            raw = data if isinstance(data, bytes) else str(data).encode()
            self.session_recorder.write_nmea(raw + b'\n')
        if line:
            self.dockwidget.nmeaLineReceived.emit(line)

    # --- Main thread handlers (called via Qt signal, safe for UI updates) ---
    def update_gnss_position(self, data):
        """Handle incoming GNSS position from serial stream."""
        lat = data['lat']
        lon = data['lon']
        height = data.get('alt', 0.0)
        fixtype = data.get('fixtype', 0)
        num_sats = data.get('num_sats', self.current_num_sats)
        hdop = data.get('hdop', self.current_hdop)

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
        """Store latest satellite data (display updated by periodic timer)."""
        if not isinstance(satellites, list) or not satellites:
            return
        # Guard: reject mountpoint dicts (have 'name' key, not 'prn')
        if 'prn' not in satellites[0]:
            return
        self.satellite_buffer = satellites

    def _flush_satellite_buffer(self):
        """Called by 1 Hz timer: push buffered data to visualization widgets."""
        if not self.satellite_buffer:
            return
        if self.sky_plot:
            self.sky_plot.set_satellites(self.satellite_buffer)
        if self.snr_chart:
            self.snr_chart.set_satellites(self.satellite_buffer)
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

    def _on_select_crs(self):
        """Open QGIS projection selection dialog and update the projected CRS."""
        from qgis.gui import QgsProjectionSelectionDialog
        dlg = QgsProjectionSelectionDialog(self.dockwidget)
        dlg.setCrs(self._projected_crs)
        if dlg.exec():
            self._projected_crs = dlg.crs()
            self.dockwidget.btnSelectCrs.setText(
                f'{self._projected_crs.authid()} – {self._projected_crs.description()}')
            self.saveSettings()

    def _reset_live_labels(self):
        self.dockwidget.lblLiveLat.setText('Lat: --')
        self.dockwidget.lblLiveLon.setText('Lon: --')
        self.dockwidget.lblLiveH.setText('H: --')
        self.dockwidget.lblLiveAcc.setText('Acc: --')
        self.dockwidget.lblLiveE.setText('E: --')
        self.dockwidget.lblLiveN.setText('N: --')
        self.dockwidget.lblLiveGeoid.setText('')

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

        # Compute and display UTM E/N in header row 3
        try:
            src_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            utm_xform = QgsCoordinateTransform(src_crs, self._projected_crs,
                                               QgsProject.instance())
            utm_pt = utm_xform.transform(QgsPointXY(lon, lat))
            crs_id = self._projected_crs.authid()
            self.dockwidget.lblLiveE.setText(f'E ({crs_id}): {utm_pt.x():.3f}')
            self.dockwidget.lblLiveN.setText(f'N: {utm_pt.y():.3f}')
        except Exception:
            self.dockwidget.lblLiveE.setText('E: --')
            self.dockwidget.lblLiveN.setText('N: --')

        # Geoid undulation display
        if self.dockwidget.grpHeightTransform.isChecked():
            geoid_sep = self.dockwidget.spinGeoidSeparation.value()
            self.dockwidget.lblLiveGeoid.setText(f'Geoid: {geoid_sep:+.3f}m')
        else:
            self.dockwidget.lblLiveGeoid.setText('')

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
        """Scan and populate available serial COM ports with device names."""
        try:
            import serial.tools.list_ports
            import subprocess
            import sys
            
            # Build a cache of friendly names from Device Manager
            friendly_names = {}
            if sys.platform == 'win32':
                try:
                    # PowerShell: Get all COM port device names
                    ps_cmd = (
                        "[System.Collections.Hashtable]$ports = @{}; "
                        "Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue | "
                        "ForEach-Object { "
                        "  if ($_.Name -match '\\((COM\\d+)\\)') { "
                        "    $comport = $Matches[1]; "
                        "    $ports[$comport] = $_.Name.Replace(' (' + $comport + ')', ''); "
                        "  } "
                        "}; "
                        "$ports | ConvertTo-Json"
                    )
                    result = subprocess.run(
                        ['powershell', '-NoProfile', '-Command', ps_cmd],
                        capture_output=True,
                        text=True,
                        timeout=3
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        import json
                        try:
                            friendly_names = json.loads(result.stdout.strip())
                        except:
                            pass
                except Exception:
                    pass
            
            # Build list of "COM1 - Device Name" entries
            port_entries = []
            for p in serial.tools.list_ports.comports():
                device_name = None
                
                # First, try the PowerShell cache
                if p.device in friendly_names:
                    device_name = friendly_names[p.device]
                # Fallback to description (for non-Bluetooth or if PS fails)
                elif p.description and p.description.strip():
                    device_name = p.description
                
                # Build display text
                if device_name:
                    display_text = f"{p.device} - {device_name}"
                else:
                    display_text = p.device
                port_entries.append(display_text)
        except (ImportError, Exception):
            # Fallback: common port names without descriptions
            port_entries = [f'COM{i}' for i in range(1, 13)]

        current = self.dockwidget.inputSPort.currentText()
        self.dockwidget.inputSPort.blockSignals(True)
        self.dockwidget.inputSPort.clear()
        self.dockwidget.inputSPort.addItems(port_entries)
        # Try to restore previous selection (match by device name or full text)
        if current:
            current_device = self._extract_port_device(current)
            for i, entry in enumerate(port_entries):
                if entry == current or self._extract_port_device(entry) == current_device:
                    self.dockwidget.inputSPort.setCurrentIndex(i)
                    break
            else:
                # Selection not found, pick first if available
                if port_entries:
                    self.dockwidget.inputSPort.setCurrentIndex(0)
        elif port_entries:
            self.dockwidget.inputSPort.setCurrentIndex(0)
        self.dockwidget.inputSPort.blockSignals(False)
        self.out(f'COM-Ports aktualisiert: {len(port_entries)} verfügbar')

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

        # Compute projected coordinates for display / modal dialog
        easting, northing = 0.0, 0.0
        try:
            src_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            xform = QgsCoordinateTransform(src_crs, self._projected_crs, QgsProject.instance())
            pt = xform.transform(QgsPointXY(result['lon'], result['lat']))
            easting, northing = round(pt.x(), 3), round(pt.y(), 3)
        except Exception:
            pass

        punkt_nr_used = self.dockwidget.txtPunktNummer.text().strip()
        overrides = {}

        if self.dockwidget.cbAutoSave.isChecked():
            # Auto-save: write directly without modal
            self._write_point_to_layer(result)
        else:
            # Show modal dialog for review / editing before saving
            dlg = PunktSpeichernDialog(
                punkt_nr=punkt_nr_used,
                x=easting,
                y=northing,
                h_orth=result['height'],
                kommentar=self.dockwidget.txtKommentar.text().strip(),
                inst_h=self.dockwidget.spinInstH.value(),
                crs_label=self._projected_crs.authid(),
                fixtype=int(result.get('fixtype', 0)),
                parent=self.dockwidget,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.out(f'Punkt verworfen.')
                return
            overrides = {
                'punkt_nr': dlg.punkt_nr,
                'h_orth': dlg.h_orth,
                'inst_h': dlg.inst_h,
                'kommentar': dlg.kommentar,
            }
            punkt_nr_used = dlg.punkt_nr
            self._write_point_to_layer(result, overrides=overrides)

        # Session protocol — pass full measurement data
        if self.session_recorder and self.session_recorder.is_recording:
            try:
                inst_h = overrides.get('inst_h', self.dockwidget.spinInstH.value())
                geoid_sep = (self.dockwidget.spinGeoidSeparation.value()
                             if self.dockwidget.grpHeightTransform.isChecked() else 0.0)
                h_ant = float(result['height'])
                h_orth_ground = round(h_ant - float(inst_h), 4)
                h_ellips_ground = round(h_orth_ground + float(geoid_sep), 4)
                crs_code = self._projected_crs.authid()
                kommentar = overrides.get('kommentar', self.dockwidget.txtKommentar.text().strip())
                protocol_data = {
                    'lat': float(result['lat']),
                    'lon': float(result['lon']),
                    'height': h_ant,
                    'fixtype': int(result.get('fixtype', 0)),
                    'samples': int(result.get('samples', 1)),
                    'hdop': float(result.get('hdop') or 0.0),
                    'vdop': float(result.get('vdop') or 0.0),
                    'pdop': float(result.get('pdop') or 0.0),
                    'num_sats': int(result.get('num_sats') or 0),
                    'std_lat_m': float(result.get('std_lat_m') or 0.0),
                    'std_lon_m': float(result.get('std_lon_m') or 0.0),
                    'std_h_m': float(result.get('std_h_m') or 0.0),
                    'easting': float(easting),
                    'northing': float(northing),
                    'punkt_nr': str(punkt_nr_used),
                    'kommentar': kommentar,
                    'inst_h': float(inst_h),
                    'geoid_sep': float(geoid_sep),
                    'h_ellips': h_ellips_ground,
                    'h_orth': h_orth_ground,
                    'crs_code': str(crs_code),
                    'caster_name': self.dockwidget.comboCaster.currentText(),
                    'timestamp': str(result.get('timestamp', '')),
                }
                self.session_recorder.record_point_measured(protocol_data)
                self._update_protocol_display()
            except Exception as _exc:
                self.out(f'⚠ Protokoll-Fehler: {_exc}')

        self.out(f'Punkt {punkt_nr_used or self._point_count()}: '
                 f'Lat={lat:.8f} Lon={lon:.8f} H={height:.3f}m '
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
        self._last_track_point = None
        self._track_segment_count = 0
        self.dockwidget.btnStartTrack.setEnabled(False)
        self.dockwidget.btnStopTrack.setEnabled(True)
        self.out(f'Track gestartet (t={t_int}s, d={d_int}m)')

    def _on_stop_track(self):
        points = self.track_measurement.stop()
        self._last_track_point = None
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

    def _next_punkt_nummer(self, current: str) -> str:
        """Return auto-incremented Punktnummer.

        If ``current`` contains a '.' and the suffix is an integer, the suffix
        is incremented.  The new value is chosen as max(existing suffixes for
        the same prefix) + 1, so it is always consistent with what is already
        in the point layer.
        """
        dot_idx = current.rfind('.')
        if dot_idx == -1:
            return current
        prefix = current[:dot_idx]
        suffix = current[dot_idx + 1:]
        if not suffix.lstrip('-').isdigit():
            return current
        # Find the maximum existing suffix for this prefix in the layer
        max_suffix = int(suffix)
        if self.point_layer:
            prefix_lower = prefix.lower()
            for feat in self.point_layer.getFeatures():
                val = feat['PunktNr']
                if val is None:
                    continue
                val_str = str(val)
                dot = val_str.rfind('.')
                if dot == -1:
                    continue
                if val_str[:dot].lower() != prefix_lower:
                    continue
                try:
                    existing = int(val_str[dot + 1:])
                    if existing > max_suffix:
                        max_suffix = existing
                except ValueError:
                    pass
        return f'{prefix}.{max_suffix + 1}'

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
            self.out('ℹ Automatische Aufzeichnung deaktiviert.')
            return
        folder = self.dockwidget.fileSelectorTempFolder.filePath() or self.temp_folder
        rec_nmea = self.dockwidget.cbRecordReceiver.isChecked()
        rec_rtcm = self.dockwidget.cbRecordRTCM.isChecked()

        try:
            self.session_recorder = SessionRecorder(folder)
            self.session_recorder.start(record_nmea=rec_nmea, record_rtcm=rec_rtcm)
            # Set metadata from current connection state
            self.session_recorder.set_connection_info(
                instrument_type=self.dockwidget.comboReceiverType.currentText(),
                instrument_sn=self.dockwidget.txtInstrumentSN.text().strip(),
                ntrip_caster=self.dockwidget.comboCaster.currentText(),
                ntrip_mountpoint=self.dockwidget.inputMp.currentText(),
                ntrip_host=self.dockwidget.inputHost.text().strip(),
                ntrip_port=self.dockwidget.inputPort.text().strip(),
                projected_crs=self._projected_crs.authid(),
                geoid_model='',
                geoid_separation=(self.dockwidget.spinGeoidSeparation.value()
                                  if self.dockwidget.grpHeightTransform.isChecked() else 0.0),
            )
            self.out(f'✓ Aufzeichnung gestartet: {self.session_recorder.session_folder}')
        except Exception as exc:
            self.session_recorder = None
            self.out(f'⚠ Aufzeichnung konnte nicht gestartet werden: {exc}')

    def _on_new_recording(self):
        """Stop the current recording session and immediately start a new one."""
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder.stop()
            self.out('✓ Aufzeichnung beendet.')
        self._start_session_recording()

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

    def _place_layer_in_group(self, layer, at_top=False):
        """Add layer to the project registry and place it inside the session group."""
        # Find target group by name (robust against stale C++ pointer)
        root = QgsProject.instance().layerTreeRoot()
        target_group = None
        if self._layer_group_name:
            target_group = root.findGroup(self._layer_group_name)
        self.out(f'  → Platziere Layer "{layer.name()}" | Gruppe="{self._layer_group_name}" found={target_group is not None}')
        # Official PyQGIS cookbook pattern: addMapLayer(False) + group.addLayer()
        QgsProject.instance().addMapLayer(layer, False)
        if target_group is not None:
            target_group.addLayer(layer)
        else:
            root.addLayer(layer)

    def _move_layer_to_group(self, layer, insert_at_top=False):
        """Move an already-registered layer into the session group."""
        root = QgsProject.instance().layerTreeRoot()
        target_group = None
        if self._layer_group_name:
            target_group = root.findGroup(self._layer_group_name)
        if not target_group:
            return
        node = root.findLayer(layer.id())
        if node and node.parent() is not target_group:
            cloned = node.clone()
            node.parent().removeChildNode(node)
            if insert_at_top:
                target_group.insertChildNode(0, cloned)
            else:
                target_group.addChildNode(cloned)

    def _ensure_point_layer(self):
        """Create the measured-points layer with full attributes if not existing."""
        name = self.dockwidget.layerName.text() or 'gnss_punkte'
        existing = QgsProject.instance().mapLayersByName(name)
        if existing:
            layer = existing[0]
            # Accept the layer if it has all required fields (works for both
            # in-memory and GeoPackage layers, regardless of fid field presence)
            required = {'Zeitstempel', 'Lat', 'Lon', 'Fixtype', 'PunktNr'}
            layer_fields = {f.name() for f in layer.fields()}
            if not required.issubset(layer_fields):
                # Wrong schema - delete it and create new one
                QgsProject.instance().removeMapLayer(layer)
            else:
                self.point_layer = layer
                self._move_layer_to_group(layer, insert_at_top=True)
                return

        crs_code = self._projected_crs.authid()
        layer = QgsVectorLayer('Point?crs=EPSG:4326', name, 'memory')
        layer.dataProvider().addAttributes([
            QgsField('Zeitstempel',   QVariant.String),
            QgsField('Lat',           QVariant.Double),
            QgsField('Lon',           QVariant.Double),
            QgsField('H_ellips',      QVariant.Double),   # ellipsoidal height
            QgsField('H_orth',        QVariant.Double),   # orthometric (geoid)
            QgsField(f'E_{crs_code.replace(":","_")}', QVariant.Double),  # Easting
            QgsField(f'N_{crs_code.replace(":","_")}', QVariant.Double),  # Northing
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
            QgsField('PunktNr',       QVariant.String, len=50),
            QgsField('Kommentar',     QVariant.String, len=200),
        ])
        layer.updateFields()
        self._place_layer_in_group(layer, at_top=True)
        style_path = os.path.join(self.plugin_dir, 'gnssStyle.qml')
        layer.loadNamedStyle(style_path)
        layer.triggerRepaint()
        self.point_layer = layer

    def _ensure_track_layer(self):
        """Create the track layer (LineString segments) with full attributes if not existing."""
        name = self.dockwidget.layerNameTrack.text() or 'gnss_track'
        existing = QgsProject.instance().mapLayersByName(name)
        if existing:
            layer = existing[0]
            # Accept the layer if it has all required fields (works for both
            # in-memory and GeoPackage layers, regardless of fid field presence)
            required = {'Zeitstempel_S', 'Lat_Start', 'Fixtype', 'SegmentNr'}
            layer_fields = {f.name() for f in layer.fields()}
            if not required.issubset(layer_fields):
                # Wrong schema - delete it and create new one
                QgsProject.instance().removeMapLayer(layer)
            else:
                self.track_layer = layer
                self._move_layer_to_group(layer, insert_at_top=False)
                return

        crs_code = self._projected_crs.authid()
        layer = QgsVectorLayer('LineString?crs=EPSG:4326', name, 'memory')
        layer.dataProvider().addAttributes([
            QgsField('Zeitstempel_S', QVariant.String),   # Start-Zeitstempel
            QgsField('Zeitstempel_E', QVariant.String),   # End-Zeitstempel
            QgsField('Lat_Start',     QVariant.Double),
            QgsField('Lon_Start',     QVariant.Double),
            QgsField('Lat_Ende',      QVariant.Double),
            QgsField('Lon_Ende',      QVariant.Double),
            QgsField('H_ellips',      QVariant.Double),   # vom Startpunkt
            QgsField('H_orth',        QVariant.Double),   # vom Startpunkt
            QgsField(f'E_{crs_code.replace(":","_")}', QVariant.Double),
            QgsField(f'N_{crs_code.replace(":","_")}', QVariant.Double),
            QgsField('Fixtype',       QVariant.Int),
            QgsField('FixtypeStr',    QVariant.String),
            QgsField('HDOP',          QVariant.Double),
            QgsField('VDOP',          QVariant.Double),
            QgsField('PDOP',          QVariant.Double),
            QgsField('NumSats',       QVariant.Int),
            QgsField('AntH_m',        QVariant.Double),
            QgsField('GeoidSep_m',    QVariant.Double),
            QgsField('TrackID',       QVariant.String),
            QgsField('SegmentNr',     QVariant.Int),
            QgsField('Laenge_m',      QVariant.Double),
        ])
        layer.updateFields()
        self._place_layer_in_group(layer, at_top=False)
        style_path = os.path.join(self.plugin_dir, 'gnssTrackStyle.qml')
        layer.loadNamedStyle(style_path)
        layer.triggerRepaint()
        self.track_layer = layer

    def _write_point_to_layer(self, result, overrides=None):
        """Add a measured point with full attributes to the point layer.

        ``overrides`` is an optional dict that may contain:
          'punkt_nr'  – Punktnummer string (overrides txtPunktNummer)
          'h_orth'    – orthometric height [m] (overrides result['height'])
          'inst_h'    – instrument/antenna height [m] (overrides spinInstH)
          'kommentar' – comment string
        """
        if not self.point_layer:
            self._ensure_point_layer()

        if overrides is None:
            overrides = {}

        inst_h = overrides.get('inst_h', self.dockwidget.spinInstH.value())
        geoid_sep = (self.dockwidget.spinGeoidSeparation.value()
                     if self.dockwidget.grpHeightTransform.isChecked() else 0.0)
        receiver_type = self.dockwidget.comboReceiverType.currentText()
        caster_name = self.dockwidget.comboCaster.currentText()
        punkt_nr = overrides.get('punkt_nr', self.dockwidget.txtPunktNummer.text().strip())
        kommentar = overrides.get('kommentar', self.dockwidget.txtKommentar.text().strip())

        h_orth = overrides.get('h_orth', result['height'])
        h_orth_ground = round(h_orth - inst_h, 4)       # ground height = antenna height − inst. height
        h_ellips = round(h_orth_ground + geoid_sep, 4)  # ellipsoidal

        # Project to selected CRS
        easting, northing = 0.0, 0.0
        try:
            src_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            xform = QgsCoordinateTransform(src_crs, self._projected_crs, QgsProject.instance())
            pt = xform.transform(QgsPointXY(result['lon'], result['lat']))
            easting, northing = round(pt.x(), 4), round(pt.y(), 4)
        except Exception:
            pass

        crs_code = self._projected_crs.authid()
        fields = self.point_layer.fields()
        feat = QgsFeature(fields)
        feat.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(result['lon'], result['lat'])))
        feat['Zeitstempel'] = result['timestamp']
        feat['Lat'] = result['lat']
        feat['Lon'] = result['lon']
        feat['H_ellips'] = h_ellips
        feat['H_orth'] = h_orth_ground
        e_field = f'E_{crs_code.replace(":","_")}'
        n_field = f'N_{crs_code.replace(":","_")}'
        if fields.indexOf(e_field) >= 0:
            feat[e_field] = easting
        if fields.indexOf(n_field) >= 0:
            feat[n_field] = northing
        feat['Fixtype'] = int(result['fixtype'])
        feat['FixtypeStr'] = fix_str(result['fixtype'])
        feat['HDOP'] = round(result.get('hdop', 0.0), 3)
        feat['VDOP'] = round(result.get('vdop', 0.0), 3)
        feat['PDOP'] = round(result.get('pdop', 0.0), 3)
        feat['NumSats'] = result.get('num_sats', 0)
        feat['Epochen'] = result.get('samples', 1)
        feat['Gemittelt'] = 1 if result.get('averaged') else 0
        feat['StdLat_m'] = round(result.get('std_lat_m', 0.0), 6)
        feat['StdLon_m'] = round(result.get('std_lon_m', 0.0), 6)
        feat['StdH_m'] = round(result.get('std_h_m', 0.0), 6)
        feat['AntH_m'] = inst_h
        feat['GeoidSep_m'] = geoid_sep
        feat['GeoidModell'] = ''
        feat['ReceiverTyp'] = receiver_type
        feat['CasterName'] = caster_name
        feat['PunktNr'] = punkt_nr[:50] if punkt_nr else ''
        if fields.indexOf('Kommentar') >= 0:
            feat['Kommentar'] = kommentar[:200] if kommentar else ''
        ok, _ = self.point_layer.dataProvider().addFeatures([feat])
        if not ok:
            self.out('⚠ Punkt konnte nicht in Layer geschrieben werden.')
            return
        self.point_layer.updateExtents()
        self.point_layer.triggerRepaint()
        # Auto-increment Punktnummer if it has a numeric suffix after '.'
        next_nr = self._next_punkt_nummer(punkt_nr)
        if next_nr != punkt_nr:
            self.dockwidget.txtPunktNummer.setText(next_nr)

    def _write_track_point_to_layer(self, point):
        """Buffer track point and write a line segment to the track layer on each new point."""
        # First point of a track: just store it, no segment to write yet
        if self._last_track_point is None:
            self._last_track_point = point
            return

        if not self.track_layer:
            self._ensure_track_layer()

        p0 = self._last_track_point
        p1 = point

        inst_h = self.dockwidget.spinInstH.value()
        geoid_sep = (self.dockwidget.spinGeoidSeparation.value()
                     if self.dockwidget.grpHeightTransform.isChecked() else 0.0)

        h_ellips = round(p0['height'] - inst_h + geoid_sep, 4)
        h_orth = round(p0['height'] - inst_h, 4)

        # Project start point to selected CRS
        easting, northing = 0.0, 0.0
        try:
            src_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            xform = QgsCoordinateTransform(src_crs, self._projected_crs, QgsProject.instance())
            pt = xform.transform(QgsPointXY(p0['lon'], p0['lat']))
            easting, northing = round(pt.x(), 4), round(pt.y(), 4)
        except Exception:
            pass

        # Build line geometry from start to end
        line_geom = QgsGeometry.fromPolylineXY([
            QgsPointXY(p0['lon'], p0['lat']),
            QgsPointXY(p1['lon'], p1['lat']),
        ])

        # Compute segment length in metres using ellipsoidal distance
        length_m = 0.0
        try:
            da = QgsDistanceArea()
            da.setSourceCrs(QgsCoordinateReferenceSystem('EPSG:4326'),
                            QgsProject.instance().transformContext())
            da.setEllipsoid('WGS84')
            length_m = round(da.measureLength(line_geom), 3)
        except Exception:
            pass

        self._track_segment_count += 1
        crs_code = self._projected_crs.authid()
        trk_fields = self.track_layer.fields()

        feat = QgsFeature(trk_fields)
        feat.setGeometry(line_geom)
        feat['Zeitstempel_S'] = p0['timestamp']
        feat['Zeitstempel_E'] = p1['timestamp']
        feat['Lat_Start'] = p0['lat']
        feat['Lon_Start'] = p0['lon']
        feat['Lat_Ende'] = p1['lat']
        feat['Lon_Ende'] = p1['lon']
        feat['H_ellips'] = h_ellips
        feat['H_orth'] = h_orth
        e_field = f'E_{crs_code.replace(":","_")}'
        n_field = f'N_{crs_code.replace(":","_")}'
        if trk_fields.indexOf(e_field) >= 0:
            feat[e_field] = easting
        if trk_fields.indexOf(n_field) >= 0:
            feat[n_field] = northing
        feat['Fixtype'] = int(p0['fixtype'])
        feat['FixtypeStr'] = fix_str(p0['fixtype'])
        feat['HDOP'] = round(p0.get('hdop', 0.0), 3)
        feat['VDOP'] = round(p0.get('vdop', 0.0), 3)
        feat['PDOP'] = round(p0.get('pdop', 0.0), 3)
        feat['NumSats'] = p0.get('num_sats', 0)
        feat['AntH_m'] = inst_h
        feat['GeoidSep_m'] = geoid_sep
        feat['TrackID'] = p0.get('track_id', '')
        feat['SegmentNr'] = self._track_segment_count
        feat['Laenge_m'] = length_m
        ok, _ = self.track_layer.dataProvider().addFeatures([feat])
        if not ok:
            return
        self.track_layer.updateExtents()
        self.track_layer.triggerRepaint()

        # Current point becomes the start of the next segment
        self._last_track_point = p1

    # =========================================================================
    # UI Helpers
    # =========================================================================

    def out(self, message):
        ts = datetime.now().strftime('%H:%M:%S')
        history = self.dockwidget.output.toPlainText()
        self.dockwidget.output.setPlainText(f'{ts} {message}\n{history}')
        if self.session_recorder and self.session_recorder.is_recording:
            self.session_recorder._add_log(message)

    def _setup_satellite_widgets(self):
        self.sky_plot = SkyPlotWidget()
        self.sky_plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        sky_layout = self.dockwidget.skyPlotWidget.layout()
        if sky_layout is None:
            sky_layout = QVBoxLayout(self.dockwidget.skyPlotWidget)
            sky_layout.setContentsMargins(0, 0, 0, 0)
        sky_layout.addWidget(self.sky_plot)
        self.sky_plot.show()

        self.snr_chart = SnrBarWidget()
        self.snr_chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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
                self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget)

                self._setup_satellite_widgets()
                self._apply_modern_style()
                self._populate_caster_combo()
                self.dockwidget.fileSelectorTempFolder.setFilePath(self.temp_folder)
                
                # Satellite display: periodic 1 Hz timer (NOT single-shot)
                from qgis.PyQt.QtCore import QTimer
                self.satellite_update_timer = QTimer()
                self.satellite_update_timer.setInterval(1000)
                self.satellite_update_timer.timeout.connect(self._flush_satellite_buffer)
                self.satellite_update_timer.start()
                
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

                # CRS selector
                self.dockwidget.btnSelectCrs.clicked.connect(self._on_select_crs)

                # Measurement
                self.dockwidget.btnMeasurePoint.clicked.connect(self._on_measure_point)
                self.point_measurement.measurement_complete.connect(self._on_measurement_complete)
                self.point_measurement.progress_update.connect(self._on_measurement_progress)

                self.dockwidget.btnStartTrack.clicked.connect(self._on_start_track)
                self.dockwidget.btnStopTrack.clicked.connect(self._on_stop_track)
                self.track_measurement.point_recorded.connect(self._on_track_point)

                # Protocol
                self.dockwidget.btnExportProtocol.clicked.connect(self._on_export_protocol)
                self.dockwidget.btnNewRecording.clicked.connect(self._on_new_recording)

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
            error_msg = traceback.format_exc()
            print(error_msg)
            QMessageBox.critical(self.iface.mainWindow(), 
                                "QNTRIPClient Error", 
                                f"Failed to initialize plugin:\n\n{error_msg}")
