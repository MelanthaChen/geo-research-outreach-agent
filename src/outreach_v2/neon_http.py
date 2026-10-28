"""Neon SQL-over-HTTPS for bounded administration where TCP is unavailable.

Runtime uses ordinary PostgreSQL transactions. Never log connection strings.
"""
from __future__ import annotations
from urllib.parse import urlparse
import httpx

class NeonHTTP:
    def __init__(self, url):
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.hostname.endswith('.neon.tech'):
            raise ValueError('Only the configured Neon host is allowed')
        self.client = httpx.Client(base_url=f'https://{parsed.hostname}', timeout=90,
            headers={'Neon-Connection-String': url, 'Neon-Raw-Text-Output':'true'})
    def query(self, sql, params=None):
        response = self.client.post('/sql', json={'query':sql,'params':params or []})
        if response.status_code != 200:
            raise RuntimeError(f'Neon query failed HTTP {response.status_code}: '+response.json().get('message','database error'))
        return response.json()
    def batch(self, queries):
        response = self.client.post('/sql', json={'queries':queries})
        if response.status_code != 200:
            raise RuntimeError(f'Neon transaction failed HTTP {response.status_code}')
        return response.json()
