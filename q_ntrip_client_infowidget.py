# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QNTRIPClientInfoWidget
 
"""
import os
from qgis.PyQt import QtGui, QtWidgets, uic

class QNTRIPClientInfoWidget(QtWidgets.QDialog):

  
    def __init__(self, parent=None):
        """Constructor."""
        super(QNTRIPClientInfoWidget, self).__init__()
        uic.loadUi(os.path.join(os.path.dirname(__file__), 'q_ntrip_client_info.ui'), self)

