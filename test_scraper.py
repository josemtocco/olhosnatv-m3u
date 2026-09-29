import unittest
from scraper import clean_name, normalize_url, render_category_playlists, render_root_m3u

class TestHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_url('//cdn.example/x.m3u8', 'https://site.example/a'), 'https://cdn.example/x.m3u8')

    def test_clean_name(self):
        self.assertEqual(clean_name(' Assistir SBT - Olhos na TV '), 'SBT')

    def test_ssiptv_nested_playlists(self):
        state = {'channels': [{'id':'abc','name':'SBT','logo':'https://x/logo.png','categories':['TVs Abertas','Variedades'],'stream_url':'https://x/live.m3u8'}]}
        cfg = {'github_raw_base':'https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main'}
        files = render_category_playlists(state, cfg)
        self.assertEqual(set(files), {'categoria-tvs-abertas.m3u', 'categoria-variedades.m3u'})
        self.assertIn('group-title="TVs Abertas"', files['categoria-tvs-abertas.m3u'])
        root = render_root_m3u(state, cfg, files)
        self.assertEqual(root.count('type="playlist"'), 2)
        self.assertIn('https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/categoria-tvs-abertas.m3u', root)
        self.assertIn('https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/categoria-variedades.m3u', root)

if __name__ == '__main__':
    unittest.main()
