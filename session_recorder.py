# -*- coding: utf-8 -*-
"""
Session Recorder - Automatic recording of receiver and correction data,
and generation of professional measurement protocols (Messprotokoll)
following LGL Baden-Württemberg GNSS surveying standards.

Protocol file is written in APPEND-ONLY mode after the initial header.
Each new entry (log line, point block, stats) is appended immediately.
"""
import os
import json
import math
import shutil
from datetime import datetime

W = 78  # protocol line width


class SessionRecorder:
    """Records GNSS session data (NMEA, RTCM) and writes an append-only measurement protocol."""

    def __init__(self, temp_folder):
        self.temp_folder = temp_folder
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.session_folder = os.path.join(temp_folder, f'session_{self.session_id}')
        self.nmea_file = None
        self.rtcm_file = None
        self.protocol_path = None
        self.is_recording = False
        self.record_nmea = True
        self.record_rtcm = True
        self._instrument_section_written = False

        # Session metadata
        self.meta = {
            'session_id': self.session_id,
            'start_time': None,
            'end_time': None,
            'instrument_type': '',
            'instrument_sn': '',
            'ntrip_caster': '',
            'ntrip_mountpoint': '',
            'ntrip_host': '',
            'ntrip_port': '',
            'projected_crs': '',
            'geoid_model': '',
            'geoid_separation': 0.0,
            'operator': '',
        }

        # Session statistics
        self.stats = {
            'total_nmea_bytes': 0,
            'total_rtcm_bytes': 0,
            'fix_types': {},
            'points_measured': 0,
            'track_points': 0,
        }

        # Measured points (kept in memory for JSON export)
        self.points = []
        # Protocol log entries (kept in memory for JSON export)
        self.log_entries = []

    # ─── Public API ─────────────────────────────────────────────────────────

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

        self.protocol_path = os.path.join(
            self.session_folder, f'protocol_{self.session_id}.txt')
        self.meta['start_time'] = datetime.now().isoformat(timespec='seconds')
        self.is_recording = True

        self._write_header()
        self._add_log("Aufzeichnung gestartet")

    def stop(self):
        """Stop the recording session and append final statistics."""
        self.is_recording = False
        self.meta['end_time'] = datetime.now().isoformat(timespec='seconds')

        if self.nmea_file and not self.nmea_file.closed:
            self.nmea_file.close()
        if self.rtcm_file and not self.rtcm_file.closed:
            self.rtcm_file.close()

        self._add_log("Aufzeichnung beendet")
        self._write_final_stats()

        # Write JSON summary
        stats_path = os.path.join(self.session_folder, f'stats_{self.session_id}.json')
        try:
            with open(stats_path, 'w', encoding='utf-8') as f:
                json.dump({'meta': self.meta, 'stats': self.stats, 'points': self.points},
                          f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def set_connection_info(self, instrument_type='', instrument_sn='',
                            ntrip_caster='', ntrip_mountpoint='',
                            ntrip_host='', ntrip_port='',
                            projected_crs='', geoid_model='',
                            geoid_separation=0.0, operator=''):
        """Set connection/instrument metadata and append the instrument section once."""
        self.meta['instrument_type'] = instrument_type
        self.meta['instrument_sn'] = instrument_sn
        self.meta['ntrip_caster'] = ntrip_caster
        self.meta['ntrip_mountpoint'] = ntrip_mountpoint
        self.meta['ntrip_host'] = ntrip_host
        self.meta['ntrip_port'] = ntrip_port
        self.meta['projected_crs'] = projected_crs
        self.meta['geoid_model'] = geoid_model
        self.meta['geoid_separation'] = geoid_separation
        self.meta['operator'] = operator

        if not self._instrument_section_written:
            self._write_instrument_section()
            self._instrument_section_written = True

        self._add_log(
            f"Verbindung: {instrument_type} (S/N: {instrument_sn}), "
            f"NTRIP: {ntrip_host}:{ntrip_port}/{ntrip_mountpoint}")

    def write_nmea(self, data):
        """Write NMEA data to file."""
        if self.is_recording and self.record_nmea and self.nmea_file and not self.nmea_file.closed:
            try:
                self.nmea_file.write(data)
                self.nmea_file.flush()
                self.stats['total_nmea_bytes'] += len(data)
            except OSError:
                pass

    def write_rtcm(self, data):
        """Write RTCM data to file."""
        if self.is_recording and self.record_rtcm and self.rtcm_file and not self.rtcm_file.closed:
            try:
                self.rtcm_file.write(data)
                self.rtcm_file.flush()
                self.stats['total_rtcm_bytes'] += len(data)
            except OSError:
                pass

    def record_fix_type(self, fixtype):
        """Track fix type statistics."""
        key = str(fixtype)
        self.stats['fix_types'][key] = self.stats['fix_types'].get(key, 0) + 1

    def record_point_measured(self, point_data):
        """Record a measured point: append log entry + detailed block to protocol file.

        Expected keys in point_data:
          lat, lon, height (antenna), fixtype, samples (Epochen),
          hdop, vdop, pdop, num_sats,
          std_lat_m, std_lon_m, std_h_m,
          easting, northing, punkt_nr, kommentar,
          inst_h, geoid_sep, h_ellips, h_orth,
          crs_code, caster_name, timestamp
        """
        self.stats['points_measured'] += 1
        n = int(point_data.get('samples', 1) or 1)
        fixtype = int(point_data.get('fixtype', 0) or 0)

        std_lat = float(point_data.get('std_lat_m', 0.0) or 0.0)
        std_lon = float(point_data.get('std_lon_m', 0.0) or 0.0)
        std_h   = float(point_data.get('std_h_m', 0.0) or 0.0)

        sigma_lage = math.sqrt(std_lat**2 + std_lon**2) if n > 1 else 0.0
        sigma_3d   = math.sqrt(std_lat**2 + std_lon**2 + std_h**2) if n > 1 else 0.0
        dof        = n - 1 if n > 1 else 0

        point_record = {
            'nr':          self.stats['points_measured'],
            'punkt_nr':    str(point_data.get('punkt_nr', '') or ''),
            'timestamp':   str(point_data.get('timestamp', '') or ''),
            'lat':         float(point_data.get('lat', 0.0) or 0.0),
            'lon':         float(point_data.get('lon', 0.0) or 0.0),
            'easting':     float(point_data.get('easting', 0.0) or 0.0),
            'northing':    float(point_data.get('northing', 0.0) or 0.0),
            'crs_code':    str(point_data.get('crs_code', '') or ''),
            'h_antenna':   float(point_data.get('height', 0.0) or 0.0),
            'h_ellips':    float(point_data.get('h_ellips', 0.0) or 0.0),
            'h_orth':      float(point_data.get('h_orth', 0.0) or 0.0),
            'inst_h':      float(point_data.get('inst_h', 0.0) or 0.0),
            'geoid_sep':   float(point_data.get('geoid_sep', 0.0) or 0.0),
            'fixtype':     fixtype,
            'fixtype_str': self._fix_type_str(fixtype),
            'hdop':        float(point_data.get('hdop', 0.0) or 0.0),
            'vdop':        float(point_data.get('vdop', 0.0) or 0.0),
            'pdop':        float(point_data.get('pdop', 0.0) or 0.0),
            'num_sats':    int(point_data.get('num_sats', 0) or 0),
            'epochen':     n,
            'std_lat_m':   std_lat,
            'std_lon_m':   std_lon,
            'std_h_m':     std_h,
            'sigma_lage':  sigma_lage,
            'sigma_3d':    sigma_3d,
            'dof':         dof,
            'kommentar':   str(point_data.get('kommentar', '') or ''),
        }
        self.points.append(point_record)

        punkt_id = point_record['punkt_nr'] or f"#{point_record['nr']}"
        self._add_log(
            f"Punkt {punkt_id} gemessen "
            f"({point_record['fixtype_str']}, {n} Epochen)")
        self._append_point_block(point_record)

    def record_track_point(self):
        """Increment track point counter."""
        self.stats['track_points'] += 1

    def get_protocol_text(self):
        """Return current protocol file content (for UI display)."""
        if self.protocol_path and os.path.exists(self.protocol_path):
            try:
                with open(self.protocol_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception:
                pass
        return ''

    def export_protocol(self, output_path):
        """Copy the current protocol file to output_path."""
        if self.protocol_path and os.path.exists(self.protocol_path):
            try:
                shutil.copy2(self.protocol_path, output_path)
                return
            except Exception:
                pass
        # Fallback: read and write
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(self.get_protocol_text())
        except Exception:
            pass

    # ─── Private: file writers ───────────────────────────────────────────────

    def _append_to_protocol(self, text):
        """Append text to the protocol file."""
        if not self.protocol_path:
            return
        try:
            with open(self.protocol_path, 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass

    def _write_header(self):
        """Write the initial file header (creates / overwrites the file once)."""
        now_str = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        lines = [
            '═' * W,
            f'║{"GNSS-MESSPROTOKOLL":^{W - 2}}║',
            '═' * W,
            '',
            f'  Erstellt          : {now_str}',
            f'  Software          : QGIS NTRIP Client Plugin',
            '',
            f'  Session-ID        : {self.session_id}',
            f'  Datum/Beginn      : {self._fmt_time(self.meta["start_time"])}',
            '',
            '═' * W,
            '',
        ]
        try:
            os.makedirs(os.path.dirname(self.protocol_path), exist_ok=True)
            with open(self.protocol_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
        except Exception:
            pass

    def _write_instrument_section(self):
        """Append the INSTRUMENTARIUM and KORREKTURDATENDIENST sections."""
        lines = [
            '─' * W,
            '  INSTRUMENTARIUM',
            '─' * W,
            f'  Empfänger-Typ     : {self.meta["instrument_type"]}',
            f'  Seriennummer      : {self.meta["instrument_sn"] or "–"}',
            f'  Projektion        : {self.meta["projected_crs"]}',
            f'  Geoid-Modell      : {self.meta["geoid_model"] or "–"}',
        ]
        if self.meta['geoid_separation']:
            lines.append(f'  Geoid-Undulation  : {self.meta["geoid_separation"]:.3f} m')
        lines += [
            '',
            '─' * W,
            '  KORREKTURDATENDIENST',
            '─' * W,
            f'  Caster/Dienst     : {self.meta["ntrip_caster"]}',
            f'  Host:Port         : {self.meta["ntrip_host"]}:{self.meta["ntrip_port"]}',
            f'  Mountpoint        : {self.meta["ntrip_mountpoint"]}',
            '',
            '═' * W,
            '',
        ]
        self._append_to_protocol('\n'.join(lines) + '\n')

    def _add_log(self, message):
        """Append a timestamped log line to the file and keep it in memory."""
        t = datetime.now().strftime('%H:%M:%S')
        self.log_entries.append({'time': t, 'message': message})
        self._append_to_protocol(f'  {t}  {message}\n')

    def _append_point_block(self, pt):
        """Append a compact tabular point measurement block to the protocol file."""
        punkt_id = pt['punkt_nr'] or f"#{pt['nr']}"
        sep = '─' * W
        C = 25  # standard column width for 3-column rows

        def c(label, value, w):
            """Format 'label: value' left-justified to width w."""
            return f'{label}: {value}'.ljust(w)

        lines = [
            '',
            sep,
            f'  Punktmessung  "{punkt_id}"',
            sep,
        ]

        # Row 1: CRS | Zeitpunkt | Fix-Typ  Sats  Epochen
        lines.append(
            f'  {c("CRS", pt["crs_code"], 18)}'
            f'{c("Zeit", pt["timestamp"], 26)}'
            f'Fix: {pt["fixtype_str"]}   '
            f'Sats: {pt["num_sats"]}   '
            f'Epochen: {pt["epochen"]}'
        )

        # Pre-format values to avoid backslash-in-f-string (Python < 3.12)
        v_east       = f'{pt["easting"]:.4f} m'
        v_north      = f'{pt["northing"]:.4f} m'
        v_h_orth_ant = f'{pt["h_antenna"]:.4f} m'   # H (orth.) at antenna phase centre
        v_h_orth     = f'{pt["h_orth"]:.4f} m'       # H (orth./Boden) at ground point
        v_h_ellips   = f'{pt["h_ellips"]:.4f} m'
        v_lat      = f'{pt["lat"]:.9f}\u00b0'
        v_lon      = f'{pt["lon"]:.9f}\u00b0'
        v_inst_h   = f'{pt["inst_h"]:.3f} m'
        v_geoid    = f'{pt["geoid_sep"]:.3f} m'
        v_hdop     = f'{pt["hdop"]:.2f}'
        v_vdop     = f'{pt["vdop"]:.2f}'
        v_pdop     = f'{pt["pdop"]:.2f}'

        # Row 2: East | North | H (orth./Boden)
        lines.append(
            f'  {c("East", v_east, C)}'
            f'{c("North", v_north, C)}'
            f'H (orth./Boden): {v_h_orth}'
        )

        # Row 3: Lat | Lon | H (ellips.)
        lines.append(
            f'  {c("Lat", v_lat, C)}'
            f'{c("Lon", v_lon, C)}'
            f'H (ellips.):     {v_h_ellips}'
        )

        # Row 4: H (orth.) Antenne | Antennenhöhe | Geoid-Und.
        lines.append(
            f'  {c("H (orth.)", v_h_orth_ant, C)}'
            f'{c("Ant.-Höhe", v_inst_h, C)}'
            f'Geoid-Und.:      {v_geoid}'
        )

        # Row 5: HDOP | VDOP | PDOP
        lines.append(
            f'  {c("HDOP", v_hdop, 16)}'
            f'{c("VDOP", v_vdop, 16)}'
            f'PDOP: {v_pdop}'
        )

        # Row 6+: Statistics
        if pt['epochen'] > 1:
            v_slat  = f'{pt["std_lat_m"]:.5f} m'
            v_slon  = f'{pt["std_lon_m"]:.5f} m'
            v_sh    = f'{pt["std_h_m"]:.5f} m'
            v_slage = f'{pt["sigma_lage"]:.5f} m'
            v_s3d   = f'{pt["sigma_3d"]:.5f} m'
            lines.append(
                f'  {c("f", str(pt["dof"]), 12)}'
                f'{c("σ Lat", v_slat, 22)}'
                f'{c("σ Lon", v_slon, 22)}'
                f'σ Höhe: {v_sh}'
            )
            lines.append(
                f'  {c("σ Lage (2D)", v_slage, 30)}'
                f'σ 3D: {v_s3d}'
            )
        else:
            lines.append('  (Einzelepoche – keine Statistik)')

        if pt['kommentar']:
            lines.append(f'  Bemerkung: {pt["kommentar"]}')

        lines.append('')
        self._append_to_protocol('\n'.join(lines) + '\n')

    def _write_final_stats(self):
        """Append statistics summary when the session ends."""
        lines = [
            '',
            '═' * W,
            '  ABSCHLUSSBERICHT',
            '─' * W,
            f'  Datum/Ende        : {self._fmt_time(self.meta["end_time"])}',
            f'  Punkte gemessen   : {self.stats["points_measured"]}',
            f'  Track-Punkte      : {self.stats["track_points"]}',
            f'  NMEA-Daten        : {self._fmt_bytes(self.stats["total_nmea_bytes"])}',
            f'  RTCM-Daten        : {self._fmt_bytes(self.stats["total_rtcm_bytes"])}',
        ]
        if self.stats['fix_types']:
            lines.append('  Fix-Typ-Verteilung:')
            for ft, count in sorted(self.stats['fix_types'].items()):
                lines.append(f'    {self._fix_type_str(int(ft)):<18}: {count}')
        lines += ['', '═' * W, '']
        self._append_to_protocol('\n'.join(lines) + '\n')

    # ─── Static helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _fix_type_str(fixtype):
        types = {0: "Kein Fix", 1: "GPS/GNSS (autonom)", 2: "DGPS/DGNSS",
                 4: "RTK Fixed", 5: "RTK Float"}
        return types.get(fixtype, f"Unbekannt ({fixtype})")

    @staticmethod
    def _fmt_time(iso_str):
        if not iso_str:
            return '–'
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime('%d.%m.%Y %H:%M:%S')
        except (ValueError, TypeError):
            return str(iso_str)

    @staticmethod
    def _fmt_bytes(n):
        if n < 1024:
            return f'{n} B'
        elif n < 1024 * 1024:
            return f'{n / 1024:.1f} KB'
        else:
            return f'{n / (1024 * 1024):.2f} MB'

        self.temp_folder = temp_folder
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.session_folder = os.path.join(temp_folder, f'session_{self.session_id}')
        self.nmea_file = None
        self.rtcm_file = None
        self.protocol_path = None
        self.is_recording = False
        self.record_nmea = True
        self.record_rtcm = True

        # Session metadata
        self.meta = {
            'session_id': self.session_id,
            'start_time': None,
            'end_time': None,
            'instrument_type': '',
            'instrument_sn': '',
            'ntrip_caster': '',
            'ntrip_mountpoint': '',
            'ntrip_host': '',
            'ntrip_port': '',
            'projected_crs': '',
            'geoid_model': '',
            'geoid_separation': 0.0,
            'operator': '',
        }

        # Session statistics
        self.stats = {
            'total_nmea_bytes': 0,
            'total_rtcm_bytes': 0,
            'fix_types': {},
            'points_measured': 0,
            'track_points': 0,
        }

        # Measured points (kept in memory for JSON export)
        self.points = []
        # Protocol log entries (kept in memory for JSON export)
        self.log_entries = []
