# -*- coding: utf-8 -*-
"""
Session Recorder - Automatic recording of receiver and correction data,
and generation of measurement protocols.
"""
import os
import json
from datetime import datetime


class SessionRecorder:
    """Records GNSS session data (NMEA, RTCM) and generates a measurement protocol."""

    def __init__(self, temp_folder):
        self.temp_folder = temp_folder
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.session_folder = os.path.join(temp_folder, f'session_{self.session_id}')
        self.nmea_file = None
        self.rtcm_file = None
        self.protocol_entries = []
        self.is_recording = False
        self.record_nmea = True
        self.record_rtcm = True

        # Session statistics
        self.stats = {
            'start_time': None,
            'end_time': None,
            'total_nmea_bytes': 0,
            'total_rtcm_bytes': 0,
            'fix_types': {},
            'points_measured': 0,
            'track_points': 0,
        }

    def start(self, record_nmea=True, record_rtcm=True):
        """Start a recording session."""
        self.record_nmea = record_nmea
        self.record_rtcm = record_rtcm

        os.makedirs(self.session_folder, exist_ok=True)

        if record_nmea:
            nmea_path = os.path.join(self.session_folder, f'receiver_{self.session_id}.nmea')
            self.nmea_file = open(nmea_path, 'ab')

        if record_rtcm:
            rtcm_path = os.path.join(self.session_folder, f'rtcm_{self.session_id}.rtcm3')
            self.rtcm_file = open(rtcm_path, 'ab')

        self.stats['start_time'] = datetime.now().isoformat()
        self.is_recording = True

        self.add_protocol_entry("Session gestartet")

    def stop(self):
        """Stop the recording session and finalize protocol."""
        self.is_recording = False
        self.stats['end_time'] = datetime.now().isoformat()

        if self.nmea_file and not self.nmea_file.closed:
            self.nmea_file.close()
        if self.rtcm_file and not self.rtcm_file.closed:
            self.rtcm_file.close()

        self.add_protocol_entry("Session beendet")
        self._write_protocol()

    def write_nmea(self, data):
        """Write NMEA data to file."""
        if self.is_recording and self.record_nmea and self.nmea_file and not self.nmea_file.closed:
            self.nmea_file.write(data)
            self.stats['total_nmea_bytes'] += len(data)

    def write_rtcm(self, data):
        """Write RTCM data to file."""
        if self.is_recording and self.record_rtcm and self.rtcm_file and not self.rtcm_file.closed:
            self.rtcm_file.write(data)
            self.stats['total_rtcm_bytes'] += len(data)

    def record_fix_type(self, fixtype):
        """Track fix type statistics."""
        key = str(fixtype)
        self.stats['fix_types'][key] = self.stats['fix_types'].get(key, 0) + 1

    def record_point_measured(self, point_data):
        """Record a measured point to the protocol."""
        self.stats['points_measured'] += 1
        lat = point_data.get('lat', 0)
        lon = point_data.get('lon', 0)
        h = point_data.get('height', 0)
        fix = point_data.get('fixtype', 0)
        samples = point_data.get('samples', 1)
        self.add_protocol_entry(
            f"Punkt {self.stats['points_measured']}: "
            f"Lat={lat:.8f}, Lon={lon:.8f}, H={h:.3f}m, "
            f"Fix={self._fix_type_str(fix)}, Epochen={samples}"
        )

    def record_track_point(self):
        """Increment track point counter."""
        self.stats['track_points'] += 1

    def add_protocol_entry(self, message):
        """Add a timestamped entry to the protocol."""
        entry = {
            'time': datetime.now().isoformat(),
            'message': message
        }
        self.protocol_entries.append(entry)

    def get_protocol_text(self):
        """Generate human-readable protocol text."""
        lines = []
        lines.append("=" * 60)
        lines.append("GNSS Messprotokoll")
        lines.append("=" * 60)
        lines.append(f"Session: {self.session_id}")
        lines.append(f"Start: {self.stats.get('start_time', '--')}")
        lines.append(f"Ende: {self.stats.get('end_time', '--')}")
        lines.append(f"NMEA Daten: {self.stats['total_nmea_bytes']} Bytes")
        lines.append(f"RTCM Daten: {self.stats['total_rtcm_bytes']} Bytes")
        lines.append(f"Gemessene Punkte: {self.stats['points_measured']}")
        lines.append(f"Track-Punkte: {self.stats['track_points']}")
        lines.append("")
        lines.append("Fix-Typ Verteilung:")
        for ft, count in self.stats['fix_types'].items():
            lines.append(f"  {self._fix_type_str(int(ft))}: {count}")
        lines.append("")
        lines.append("-" * 60)
        lines.append("Protokoll-Einträge:")
        lines.append("-" * 60)
        for entry in self.protocol_entries:
            lines.append(f"  [{entry['time']}] {entry['message']}")
        lines.append("=" * 60)
        return '\n'.join(lines)

    def _write_protocol(self):
        """Write protocol to file."""
        protocol_path = os.path.join(self.session_folder, f'protocol_{self.session_id}.txt')
        with open(protocol_path, 'w', encoding='utf-8') as f:
            f.write(self.get_protocol_text())

        # Also write stats as JSON
        stats_path = os.path.join(self.session_folder, f'stats_{self.session_id}.json')
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=2, ensure_ascii=False)

    def export_protocol(self, output_path):
        """Export protocol to a specific file."""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(self.get_protocol_text())

    @staticmethod
    def _fix_type_str(fixtype):
        """Convert fix type number to string."""
        types = {
            0: "Kein Fix",
            1: "GPS Fix",
            2: "DGPS",
            4: "RTK Fixed",
            5: "RTK Float"
        }
        return types.get(fixtype, f"Unbekannt ({fixtype})")
