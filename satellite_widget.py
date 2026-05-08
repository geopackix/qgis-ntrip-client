# -*- coding: utf-8 -*-
"""
Satellite Visualization - Sky plot and SNR bar chart widgets.
"""
import math
from qgis.PyQt.QtCore import Qt, QRectF
from qgis.PyQt.QtGui import QPainter, QColor, QFont, QPen, QBrush
from qgis.PyQt.QtWidgets import QWidget


# Satellite system colors
SAT_COLORS = {
    'GP': QColor(0, 120, 215),    # GPS - blue
    'GL': QColor(220, 50, 50),    # GLONASS - red
    'GA': QColor(0, 180, 80),     # Galileo - green
    'GB': QColor(200, 150, 0),    # BeiDou - gold
    'GQ': QColor(150, 50, 200),   # QZSS - purple
}


def get_sat_color(prn_str):
    """Get color based on satellite system prefix."""
    prefix = prn_str[:2] if len(prn_str) >= 2 else 'GP'
    return SAT_COLORS.get(prefix, QColor(128, 128, 128))


class SkyPlotWidget(QWidget):
    """Polar sky plot showing satellite positions by elevation/azimuth."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.satellites = []  # list of {'prn': str, 'elevation': float, 'azimuth': float, 'snr': float}
        self.setMinimumSize(200, 200)

    def set_satellites(self, satellites):
        """Update satellite data and repaint."""
        self.satellites = satellites
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        size = min(w, h) - 20
        cx = w / 2
        cy = h / 2
        radius = size / 2

        # Background (light)
        painter.fillRect(self.rect(), QColor(245, 245, 245))

        # Draw concentric circles (elevation rings)
        pen = QPen(QColor(180, 180, 180))
        pen.setWidth(1)
        painter.setPen(pen)
        for i in range(1, 4):
            r = radius * i / 3
            painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

        # Draw cross lines (N-S, E-W)
        painter.drawLine(int(cx), int(cy - radius), int(cx), int(cy + radius))
        painter.drawLine(int(cx - radius), int(cy), int(cx + radius), int(cy))

        # Labels
        font = QFont('Arial', 8)
        painter.setFont(font)
        painter.setPen(QColor(80, 80, 80))
        painter.drawText(int(cx - 4), int(cy - radius - 4), "N")
        painter.drawText(int(cx - 4), int(cy + radius + 12), "S")
        painter.drawText(int(cx + radius + 4), int(cy + 4), "E")
        painter.drawText(int(cx - radius - 12), int(cy + 4), "W")

        # Elevation labels
        painter.setPen(QColor(140, 140, 140))
        font.setPointSize(7)
        painter.setFont(font)
        for i, elev in enumerate([60, 30, 0]):
            r = radius * (i + 1) / 3
            painter.drawText(int(cx + 2), int(cy - r + 10), f"{elev}°")

        # Draw satellites
        font.setPointSize(7)
        painter.setFont(font)
        for sat in self.satellites:
            elev = sat.get('elevation', 0)
            azim = sat.get('azimuth', 0)
            snr = sat.get('snr', 0)
            prn = sat.get('prn', '??')

            # Convert elevation (0-90) to radius (outer to center)
            r = radius * (90 - elev) / 90
            # Convert azimuth to angle (0=N, clockwise)
            angle_rad = math.radians(azim - 90)  # -90 because 0 is up (North)

            x = cx + r * math.cos(angle_rad)
            y = cy + r * math.sin(angle_rad)

            # Satellite dot
            color = get_sat_color(prn)
            if snr > 0:
                painter.setBrush(QBrush(color))
            else:
                painter.setBrush(QBrush(QColor(200, 200, 200)))
            painter.setPen(QPen(color.darker(130), 1))

            dot_size = 8 if snr > 30 else 6
            painter.drawEllipse(QRectF(x - dot_size/2, y - dot_size/2, dot_size, dot_size))

            # PRN label
            painter.setPen(QColor(40, 40, 40))
            painter.drawText(int(x + 5), int(y + 3), prn[-2:])

        if not self.satellites:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignCenter, "Keine Satellitendaten")

        painter.end()


class SnrBarWidget(QWidget):
    """Bar chart showing satellite SNR values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.satellites = []
        self.setMinimumSize(200, 150)

    def set_satellites(self, satellites):
        """Update satellite data and repaint."""
        self.satellites = [s for s in satellites if s.get('snr', 0) > 0]
        self.satellites.sort(key=lambda s: s.get('prn', ''))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # Background (light)
        painter.fillRect(self.rect(), QColor(245, 245, 245))

        if not self.satellites:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignCenter, "Keine Satellitendaten")
            painter.end()
            return

        n = len(self.satellites)
        margin_left = 5
        margin_right = 5
        margin_top = 15
        margin_bottom = 25
        bar_area_w = w - margin_left - margin_right
        bar_area_h = h - margin_top - margin_bottom

        bar_width = max(4, min(20, bar_area_w / n - 2))
        spacing = (bar_area_w - n * bar_width) / max(n, 1)

        # Draw threshold lines
        painter.setPen(QPen(QColor(200, 200, 200), 1, Qt.DashLine))
        for threshold in [10, 20, 30, 40, 50]:
            y = margin_top + bar_area_h * (1 - threshold / 55)
            painter.drawLine(int(margin_left), int(y), int(w - margin_right), int(y))
            painter.setPen(QColor(140, 140, 140))
            font = QFont('Arial', 6)
            painter.setFont(font)
            painter.drawText(int(margin_left), int(y - 2), str(threshold))
            painter.setPen(QPen(QColor(200, 200, 200), 1, Qt.DashLine))

        # Draw bars
        font = QFont('Arial', 6)
        painter.setFont(font)
        for i, sat in enumerate(self.satellites):
            snr = min(sat.get('snr', 0), 55)
            prn = sat.get('prn', '??')

            x = margin_left + i * (bar_width + spacing) + spacing / 2
            bar_h = bar_area_h * snr / 55
            y = margin_top + bar_area_h - bar_h

            # Bar color based on SNR
            color = get_sat_color(prn)
            if snr < 15:
                color = QColor(color.red() // 2, color.green() // 2, color.blue() // 2)

            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(x, y, bar_width, bar_h))

            # PRN label
            painter.setPen(QColor(60, 60, 60))
            painter.save()
            painter.translate(x + bar_width / 2, h - 3)
            painter.rotate(-45)
            painter.drawText(0, 0, prn[-2:])
            painter.restore()

        painter.end()
