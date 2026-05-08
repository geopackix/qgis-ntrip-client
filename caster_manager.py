# -*- coding: utf-8 -*-
"""
Caster Manager - Persistent storage and management of NTRIP caster profiles.
"""
import os
import json


class CasterManager:
    """Manages multiple NTRIP caster connection profiles with persistent JSON storage."""

    def __init__(self, config_dir):
        self.config_file = os.path.join(config_dir, 'casters.json')
        self.casters = []
        self.load()

    def load(self):
        """Load casters from JSON file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.casters = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.casters = []
        else:
            self.casters = []

    def save(self):
        """Save casters to JSON file."""
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.casters, f, indent=2, ensure_ascii=False)

    def add(self, name, host, port, mountpoint, user, password, ntrip_version='1'):
        """Add a new caster profile."""
        caster = {
            'name': name,
            'host': host,
            'port': port,
            'mountpoint': mountpoint,
            'user': user,
            'password': password,
            'ntrip_version': ntrip_version
        }
        self.casters.append(caster)
        self.save()
        return len(self.casters) - 1

    def update(self, index, name, host, port, mountpoint, user, password, ntrip_version='1'):
        """Update an existing caster profile."""
        if 0 <= index < len(self.casters):
            self.casters[index] = {
                'name': name,
                'host': host,
                'port': port,
                'mountpoint': mountpoint,
                'user': user,
                'password': password,
                'ntrip_version': ntrip_version
            }
            self.save()

    def delete(self, index):
        """Delete a caster profile by index."""
        if 0 <= index < len(self.casters):
            del self.casters[index]
            self.save()

    def update_mountpoint(self, index, mountpoint):
        """Persist only the mountpoint field for a caster profile."""
        if 0 <= index < len(self.casters):
            self.casters[index]['mountpoint'] = mountpoint
            self.save()

    def get(self, index):
        """Get a caster profile by index."""
        if 0 <= index < len(self.casters):
            return self.casters[index]
        return None

    def get_names(self):
        """Get list of caster profile names."""
        return [c.get('name', f"Caster {i+1}") for i, c in enumerate(self.casters)]

    def count(self):
        """Return number of stored casters."""
        return len(self.casters)
