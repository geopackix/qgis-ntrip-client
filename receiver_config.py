# -*- coding: utf-8 -*-
"""
Receiver Configuration - Commands for different GNSS receiver types.
Supports Unicore UM980, u-blox, Septentrio (extensible).
"""


class ReceiverConfig:
    """Base class for receiver configuration."""

    def __init__(self, serial_stream):
        self.serial = serial_stream

    def configure(self):
        """Send configuration commands to receiver. Override in subclass."""
        pass

    def send_command(self, cmd):
        """Send a command string to the receiver."""
        if self.serial and self.serial.serial.is_open:
            self.serial.serial.write((cmd + '\r\n').encode('ascii'))

    def get_name(self):
        return "Generic"


class UM980Config(ReceiverConfig):
    """Unicore UM980 receiver configuration."""

    def get_name(self):
        return "Unicore UM980"

    def configure(self):
        """Configure UM980 for RTK operation with NMEA output."""
        commands = [
            'UNLOGALL',                          # Stop all output
            'GPGGA COM1 1',                      # GGA at 1 Hz
            'GPGSA COM1 1',                      # GSA at 1 Hz
            'GPGSV COM1 1',                      # GSV at 1 Hz
            'GPRMC COM1 1',                      # RMC at 1 Hz
            'GPVTG COM1 1',                      # VTG at 1 Hz
            'GPGST COM1 1',                      # GST at 1 Hz (position accuracy)
            'CONFIG RTK TIMEOUT 60',             # RTK timeout
            'CONFIG SIGNALGROUP 2',              # Multi-constellation
            'SAVECONFIG',                        # Save configuration
        ]
        for cmd in commands:
            self.send_command(cmd)

    def configure_output_rate(self, rate_hz=1):
        """Set NMEA output rate."""
        self.send_command(f'GPGGA COM1 {rate_hz}')
        self.send_command(f'GPGSA COM1 {rate_hz}')
        self.send_command(f'GPGSV COM1 1')  # GSV always 1 Hz
        self.send_command(f'GPRMC COM1 {rate_hz}')

    def configure_rtcm_input(self):
        """Enable RTCM3 input on COM1."""
        self.send_command('CONFIG COM1 RTCM3')

    def reset_to_default(self):
        """Reset receiver to factory defaults."""
        self.send_command('FRESET')


class UbloxConfig(ReceiverConfig):
    """u-blox receiver configuration (basic NMEA commands)."""

    def get_name(self):
        return "u-blox"

    def configure(self):
        """Basic u-blox configuration via NMEA commands."""
        # u-blox typically uses UBX binary protocol, but basic NMEA config:
        commands = [
            '$PUBX,40,GGA,0,1,0,0,0,0',   # GGA on UART1 at 1 Hz
            '$PUBX,40,GSA,0,1,0,0,0,0',   # GSA on UART1
            '$PUBX,40,GSV,0,1,0,0,0,0',   # GSV on UART1
            '$PUBX,40,RMC,0,1,0,0,0,0',   # RMC on UART1
            '$PUBX,40,VTG,0,1,0,0,0,0',   # VTG on UART1
        ]
        for cmd in commands:
            self.send_command(cmd)


class SeptentrioConfig(ReceiverConfig):
    """Septentrio receiver configuration."""

    def get_name(self):
        return "Septentrio"

    def configure(self):
        """Basic Septentrio configuration."""
        commands = [
            'sno, Stream1, COM1, GGA, sec1',
            'sno, Stream2, COM1, GSA, sec1',
            'sno, Stream3, COM1, GSV, sec1',
            'sno, Stream4, COM1, RMC, sec1',
        ]
        for cmd in commands:
            self.send_command(cmd)


class GenericConfig(ReceiverConfig):
    """Generic NMEA receiver - no configuration needed."""

    def get_name(self):
        return "Generic (NMEA)"

    def configure(self):
        """No configuration needed for generic NMEA receivers."""
        pass


def get_receiver_config(receiver_type, serial_stream):
    """Factory function to get appropriate receiver config."""
    configs = {
        'Unicore UM980': UM980Config,
        'u-blox': UbloxConfig,
        'Septentrio': SeptentrioConfig,
        'Generisch (NMEA)': GenericConfig,
    }
    config_class = configs.get(receiver_type, GenericConfig)
    return config_class(serial_stream)
