# -*- coding: utf-8 -*-
"""
Measurement Module - Point and track measurement with full metric capture.
"""
import time
import math
from datetime import datetime

from qgis.PyQt.QtCore import QObject, pyqtSignal


class PointMeasurement(QObject):
    """Single point measurement with optional epoch averaging."""

    measurement_complete = pyqtSignal(dict)
    progress_update = pyqtSignal(int, int)   # current, total

    def __init__(self):
        super().__init__()
        self.is_measuring = False
        self.samples = []
        self.target_epochs = 1
        self.use_averaging = False

    def start(self, use_averaging=True, epochs=10):
        self.is_measuring = True
        self.samples = []
        self.use_averaging = use_averaging
        self.target_epochs = epochs if use_averaging else 1

    def add_sample(self, lat, lon, height, fixtype,
                   hdop=0.0, vdop=0.0, pdop=0.0, num_sats=0):
        if not self.is_measuring:
            return

        self.samples.append({
            'lat': lat, 'lon': lon, 'height': height,
            'fixtype': fixtype,
            'hdop': hdop, 'vdop': vdop, 'pdop': pdop,
            'num_sats': num_sats,
            'timestamp': datetime.now().isoformat()
        })

        self.progress_update.emit(len(self.samples), self.target_epochs)

        if len(self.samples) >= self.target_epochs:
            self._finish()

    def _finish(self):
        self.is_measuring = False
        if not self.samples:
            return

        n = len(self.samples)

        avg_lat = sum(s['lat'] for s in self.samples) / n
        avg_lon = sum(s['lon'] for s in self.samples) / n
        avg_h   = sum(s['height'] for s in self.samples) / n
        avg_hdop = sum(s['hdop'] for s in self.samples) / n
        avg_vdop = sum(s['vdop'] for s in self.samples) / n
        avg_pdop = sum(s['pdop'] for s in self.samples) / n
        avg_sats = sum(s['num_sats'] for s in self.samples) / n

        # Standard deviations (in degrees, converted to approximate meters)
        if n > 1:
            std_lat = math.sqrt(sum((s['lat'] - avg_lat)**2 for s in self.samples) / n)
            std_lon = math.sqrt(sum((s['lon'] - avg_lon)**2 for s in self.samples) / n)
            std_h   = math.sqrt(sum((s['height'] - avg_h)**2 for s in self.samples) / n)
            # Convert degree std to meters (approx)
            std_lat_m = std_lat * 111320.0
            std_lon_m = std_lon * 111320.0 * math.cos(math.radians(avg_lat))
        else:
            std_lat_m = std_lon_m = std_h = 0.0

        result = {
            'lat': avg_lat,
            'lon': avg_lon,
            'height': avg_h,
            'fixtype': self.samples[-1]['fixtype'],
            'samples': n,
            'averaged': self.use_averaging and n > 1,
            'hdop': avg_hdop,
            'vdop': avg_vdop,
            'pdop': avg_pdop,
            'num_sats': int(round(avg_sats)),
            'std_lat_m': std_lat_m,
            'std_lon_m': std_lon_m,
            'std_h_m': std_h,
            'timestamp': datetime.now().isoformat()
        }
        self.measurement_complete.emit(result)

    def cancel(self):
        self.is_measuring = False
        self.samples = []


class TrackMeasurement(QObject):
    """Track recording with time and/or distance intervals."""

    point_recorded = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.is_recording = False
        self.time_interval = 1.0
        self.distance_interval = 0.0
        self.points = []
        self.last_record_time = 0
        self.last_position = None
        self.point_count = 0
        self.track_id = ''

    def start(self, time_interval=1.0, distance_interval=0.0):
        self.is_recording = True
        self.time_interval = time_interval
        self.distance_interval = distance_interval
        self.points = []
        self.last_record_time = 0
        self.last_position = None
        self.point_count = 0
        self.track_id = datetime.now().strftime('%Y%m%d_%H%M%S')

    def stop(self):
        self.is_recording = False
        return self.points

    def add_position(self, lat, lon, height, fixtype,
                     hdop=0.0, vdop=0.0, pdop=0.0, num_sats=0):
        if not self.is_recording:
            return

        now = time.time()
        should_record = False

        if now - self.last_record_time >= self.time_interval:
            should_record = True

        if self.distance_interval > 0 and self.last_position:
            dist = self._haversine(
                self.last_position['lat'], self.last_position['lon'], lat, lon)
            if dist >= self.distance_interval:
                should_record = True

        if self.last_position is None:
            should_record = True

        if should_record:
            self.point_count += 1
            point = {
                'lat': lat, 'lon': lon, 'height': height,
                'fixtype': fixtype,
                'hdop': hdop, 'vdop': vdop, 'pdop': pdop,
                'num_sats': num_sats,
                'track_id': self.track_id,
                'point_num': self.point_count,
                'timestamp': datetime.now().isoformat()
            }
            self.points.append(point)
            self.last_record_time = now
            self.last_position = point
            self.point_recorded.emit(point)

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
