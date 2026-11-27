import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = next(p for p in (HERE / 'yt_channel_agent.py', HERE.parent / 'scripts/yt_channel_agent.py') if p.exists())
spec = importlib.util.spec_from_file_location('agent', SCRIPT)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class ChannelAgentTests(unittest.TestCase):
    """NLM y YouTube falsos en memoria."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agent.HOME = Path(self.tmp.name)
        agent.CFG, agent.STATE, agent.WEB = agent.HOME / 'c.json', agent.HOME / 's.json', agent.HOME / 'web'
        self.nbs = {'nb1': [{'id': 's0', 'title': 'Viejo', 'video_id': 'OLD00000000'}]}
        self.feed, self.shorts = [], set()
        agent.sources = lambda nb: list(self.nbs[nb])
        agent.rss = lambda ch: self.feed
        agent.is_short = lambda vid: vid in self.shorts
        agent.write_brief = lambda *a: None
        agent.configure_persona = lambda *a: None
        self.ch = {'name': 'Canal', 'channel_id': 'UC', 'notebook_title': 'Canal',
                   'notebooks': [{'id': 'nb1', 'title': 'Canal'}]}
        self.cfg = {**agent.DEFAULTS, 'channels': {'c': self.ch}}
        self.st = {'channels': {}}

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, nb_list=()):
        log = []
        agent.sync_channel('c', self.ch, self.cfg, self.st, log, list(nb_list))
        return self.st['channels']['c']['videos'], log

    def load(self, vid, nb='nb1'):  # youtube-agent / extensión carga un vídeo en NLM
        self.nbs[nb].append({'id': f's-{vid}', 'title': vid, 'video_id': vid})

    def test_first_reconciliation_is_not_news_but_later_loads_are(self):
        vids, _ = self.sync()
        self.assertTrue(vids['OLD00000000']['in_feed'])
        self.load('NEW00000000')
        vids, _ = self.sync()
        self.assertIs(vids['NEW00000000']['in_feed'], False)
        self.assertEqual(self.st['channels']['c']['added_since_brief'], 1)
        vids, _ = self.sync()  # idempotente
        self.assertEqual(self.st['channels']['c']['added_since_brief'], 1)

    def test_shorts_never_reach_the_feed(self):
        self.sync()
        self.feed = [{'vid': 'RSSHORT0000', 'title': 's', 'published': '2026-10-28T10:00:00+00:00', 'short': True}]
        self.shorts = {'OLDSHORT000'}
        self.load('RSSHORT0000')
        self.load('OLDSHORT000')  # fuera de la ventana RSS: se comprueba con is_short
        vids, _ = self.sync()
        self.assertTrue(vids['RSSHORT0000']['in_feed'])
        self.assertTrue(vids['OLDSHORT000']['in_feed'])
        self.assertEqual(vids['RSSHORT0000']['published'], '2026-10-28T10:00:00+00:00')

    def test_pending_rss_video_becomes_news_when_loaded(self):
        self.sync()
        self.feed = [{'vid': 'PEND0000000', 'title': 'p', 'published': '2026-10-28T10:00:00+00:00', 'short': False}]
        vids, _ = self.sync()
        self.assertEqual(vids['PEND0000000']['status'], 'pending')
        self.load('PEND0000000')
        vids, _ = self.sync()
        self.assertEqual((vids['PEND0000000']['status'], vids['PEND0000000']['in_feed']), ('added', False))

    def test_adopts_continuation_notebooks_in_order(self):
        self.nbs['nb3'] = []
        self.nbs['nb2'] = []
        self.load('INNB3000000', 'nb3')
        nb_list = [{'id': 'nb3', 'title': 'Canal III'}, {'id': 'x', 'title': 'Canal Extra'},
                   {'id': 'nb2', 'title': 'Canal II'}, {'id': 'nb1', 'title': 'Canal'}]
        vids, log = self.sync(nb_list)
        self.assertEqual([n['title'] for n in self.ch['notebooks']], ['Canal', 'Canal II', 'Canal III'])
        self.assertEqual(vids['INNB3000000']['notebook'], 'nb3')
        self.assertEqual(sum(line.startswith('📓') for line in log), 2)


class ImplementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agent.WEB = Path(self.tmp.name)
        (agent.WEB / 'editions').mkdir()
        item = {'vid': 'ABCDEFGHIJK', 'slug': 'c', 'title': 'T', 'published': '2026-10-29', 'notebook': 'N',
                'novedad': 'n', 'accionable': 'haz X'}
        (agent.WEB / 'editions' / '2026-10-29.json').write_text(json.dumps(
            {'day': '2026-10-29', 'sections': [{'name': 'Canal', 'items': [item, {**item, 'vid': 'NOACTION000', 'accionable': ''}]}]}))
        self.calls = []
        agent.notify_coordinator = lambda cfg, key, title, text, profile=None: self.calls.append((key, profile, text)) or ''
        self.cfg = {'coordinator': {'profile': 'principal'},
                    'channels': {'c': {'name': 'Canal', 'specialist': 'tengo-un-plan'}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_avisa_solo_al_coordinador_y_nombra_al_especialista(self):
        agent.implement(self.cfg, '2026-10-29', 'ABCDEFGHIJK')
        self.assertEqual([profile for _, profile, _ in self.calls], [None])
        self.assertIn('tengo-un-plan', self.calls[0][2])  # el coordinador decide si se lo pasa al especialista
        self.assertIn('Tú decides', self.calls[0][2])
        agent.implement(self.cfg, '2026-10-29', 'ABCDEFGHIJK')  # idempotente: no se repite
        self.assertEqual(len(self.calls), 1)

    def test_implement_validates_request(self):
        for day, vid in (('2026-10-29', 'NOACTION000'), ('../etc', 'ABCDEFGHIJK'), ('2026-10-29', 'x')):
            with self.assertRaises(ValueError):
                agent.implement(self.cfg, day, vid)

    def test_sin_coordinador_configurado_avisa_en_claro(self):
        with self.assertRaisesRegex(ValueError, 'coordinador'):  # p. ej. la copia de pruebas del Surface
            agent.implement({'channels': {}}, '2026-10-29', 'ABCDEFGHIJK')


class AskTests(unittest.TestCase):
    """Chat por tarjeta: la pregunta va a NotebookLM acotada a la fuente de ese vídeo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agent.WEB = Path(self.tmp.name)
        (agent.WEB / 'editions').mkdir()
        item = {'vid': 'ABCDEFGHIJK', 'slug': 'c', 'title': 'Vídeo de prueba', 'published': '2026-10-29',
                'notebook': 'Canal II', 'novedad': 'n', 'accionable': ''}
        (agent.WEB / 'editions' / '2026-10-29.json').write_text(json.dumps(
            {'day': '2026-10-29', 'sections': [{'name': 'Canal', 'items': [item]}]}))
        self.st = {'channels': {'c': {'videos': {'ABCDEFGHIJK': {'notebook': 'nb1', 'source_id': 's-ABC'}}}}}
        self.calls = []
        agent.query = lambda nb, q, sids=None: self.calls.append((nb, q, sids)) or 'respuesta de NLM'

    def tearDown(self):
        self.tmp.cleanup()

    def test_answers_scoped_to_the_video_source(self):
        out = agent.ask_video({}, self.st, '2026-10-29', 'ABCDEFGHIJK', '¿qué dice del oro?')
        self.assertEqual(out, {'answer': 'respuesta de NLM'})
        nb, q, sids = self.calls[0]
        self.assertEqual((nb, sids), ('nb1', ['s-ABC']))
        self.assertIn('Vídeo de prueba', q)
        self.assertIn('¿qué dice del oro?', q)
        self.assertIn('SOLO en ese vídeo', q)

    def test_falls_back_to_the_notebook_when_the_video_is_unknown(self):
        agent.ask_video({'channels': {'c': {'notebooks': [{'id': 'nb2', 'title': 'Canal II'}]}}},
                        {'channels': {}}, '2026-10-29', 'ABCDEFGHIJK', 'hola')
        self.assertEqual(self.calls[0][0], 'nb2')
        self.assertIsNone(self.calls[0][2])

    def test_rejects_bad_requests(self):
        cases = [('2026-10-29', 'ABCDEFGHIJK', 'x'), ('../etc', 'ABCDEFGHIJK', 'pregunta larga'),
                 ('2026-10-29', 'x', 'pregunta larga'), ('2026-10-29', 'ZZZZZZZZZZZ', 'pregunta larga'),
                 ('2026-10-29', 'ABCDEFGHIJK', 'a' * 501)]
        for day, vid, q in cases:
            with self.assertRaises(ValueError):
                agent.ask_video({}, self.st, day, vid, q)
        self.assertEqual(self.calls, [])  # nada llegó a NotebookLM


class RefreshTests(unittest.TestCase):
    """Si NotebookLM dice que la sesión caducó, se renueva y se reintenta una sola vez."""

    def setUp(self):
        self.real_run, self.real_sub = agent.run, agent.subprocess
        self.refreshed = []

        class Sub:
            @staticmethod
            def run(cmd, **kwargs):
                self.refreshed.append(cmd)
                return None

        agent.subprocess = Sub
        agent.NLM_REFRESH = ['systemctl', '--user', 'start', 'nlm-auth.service']

    def tearDown(self):
        agent.run, agent.subprocess, agent.NLM_REFRESH = self.real_run, self.real_sub, []

    def test_renueva_y_reintenta_cuando_la_sesion_caduco(self):
        seen = []

        def fake(cmd, timeout=300, env=None):
            seen.append(cmd)
            return (False, 'NotebookLM authentication is no longer valid') if len(seen) == 1 else (True, 'respuesta')
        agent.run = fake
        self.assertEqual(agent.run_nlm(['nlm', 'query']), (True, 'respuesta'))
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.refreshed), 1)

    def test_no_renueva_si_no_es_un_fallo_de_sesion(self):
        agent.run = lambda cmd, timeout=300, env=None: (False, 'timeout 300s')
        self.assertEqual(agent.run_nlm(['nlm', 'query']), (False, 'timeout 300s'))
        self.assertEqual(self.refreshed, [])


class FeedLimitTests(unittest.TestCase):
    """El diario recoge solo los últimos N vídeos por canal; el resto no reaparece."""

    def test_solo_los_tres_ultimos(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real = agent.WEB, agent.STATE, agent.feed_items, agent.notify_coordinator, agent.announce
        self.addCleanup(lambda: setattr_all(real))
        agent.WEB, agent.STATE = Path(tmp.name), Path(tmp.name) / 's.json'
        vids = {f'V{i:010d}': {'status': 'added', 'in_feed': False, 'source_id': 's', 'notebook': 'nb'} for i in range(5)}
        st = {'channels': {'c': {'videos': vids}}}
        agent.feed_items = lambda ch, items, slug=None: [
            {'vid': v, 'title': v, 'published': f'2026-11-0{int(v[-1]) + 1}', 'novedad': '', 'accionable': ''} for v, _ in items]
        agent.notify_coordinator = lambda *a: ''
        agent.announce = lambda cfg, day: ''
        cfg = {**agent.DEFAULTS, 'channels': {'c': {'name': 'C'}}}
        agent.cmd_feed(type('A', (), {'slugs': None})(), cfg, st)
        ed = json.loads(next((agent.WEB / 'editions').glob('*.json')).read_text())
        self.assertEqual([i['vid'] for i in ed['sections'][0]['items']], ['V0000000002', 'V0000000003', 'V0000000004'])
        self.assertTrue(all(r['in_feed'] for r in vids.values()))


def setattr_all(real):
    agent.WEB, agent.STATE, agent.feed_items, agent.notify_coordinator, agent.announce = real

if __name__ == '__main__':
    unittest.main()
