import tempfile
import unittest
from pathlib import Path

import scraper


class ScraperTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(scraper.normalize_url('//cdn.example.com/live.m3u8', 'https://site.example/a'), 'https://cdn.example.com/live.m3u8')
        self.assertEqual(scraper.normalize_url('/video/live.m3u8', 'https://site.example/a'), 'https://site.example/video/live.m3u8')

    def test_media_extraction(self):
        s = scraper.Scraper({
            'source_url': 'https://example.com/',
            'user_agent': 'test',
            'request_timeout': 1,
            'media_timeout': 1,
            'request_delay_seconds': 0,
            'max_resolve_depth': 2,
            'validate_streams': False,
            'allowed_media_extensions': ['.m3u8', '.mp4']
        })
        html = '''<script>var file="https://cdn.example.com/live.m3u8?x=1";</script>'''
        self.assertEqual(s.extract_media_urls(html, 'https://example.com/'), ['https://cdn.example.com/live.m3u8?x=1'])

    def test_render(self):
        state = {'channels': [{
            'id': 'abc', 'name': 'Canal Teste', 'categories': ['TVs Abertas'],
            'logo': 'https://example.com/logo.png', 'stream_url': 'https://example.com/live.m3u8'
        }]}
        out = scraper.render_m3u(state)
        self.assertIn('#EXTM3U', out)
        self.assertIn('group-title="TVs Abertas"', out)
        self.assertIn('https://example.com/live.m3u8', out)


if __name__ == '__main__':
    unittest.main()
