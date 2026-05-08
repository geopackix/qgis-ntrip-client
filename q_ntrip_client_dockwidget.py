# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QNTRIPClientDockWidget
                                 A QGIS plugin
 GNSS NTRIP client for QGIS
        copyright            : (C) 2025 by Manuel Hart (Geokoord.com)
        email                : mh@geokoord.com
 ***************************************************************************/
"""

import os

from qgis.PyQt import QtGui, QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'q_ntrip_client_dockwidget_base.ui'))


class QNTRIPClientDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()
    nmeaLineReceived = pyqtSignal(str)        # Thread-safe NMEA line
    gnssPositionReceived = pyqtSignal(dict)   # Thread-safe position
    dopDataReceived = pyqtSignal(dict)        # Thread-safe DOP
    satDataReceived = pyqtSignal(list)        # Thread-safe satellites
    receiverConnectResult = pyqtSignal(object, str)  # (stream|None, error)
    rxBytesUpdate = pyqtSignal(int)           # RX bytes/s from serial thread
    rtcmBytesUpdate = pyqtSignal(int)         # RTCM bytes/s from NTRIP thread
    ntripStatusMessage = pyqtSignal(str)      # NTRIP connection status message

    def __init__(self, parent=None):
        """Constructor."""
        super(QNTRIPClientDockWidget, self).__init__(parent)
        self.setupUi(self)

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()
