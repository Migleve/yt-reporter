#!/usr/bin/env python3
"""yt_channel_agent — especialistas por canal de YouTube sobre NotebookLM + Reporter diario.

La ingesta a NotebookLM la hace `youtube-agent` (PCHP). Este script es la capa de conocimiento:
  - reconcilia los cuadernos de cada canal (`X`, `X II`, `X III`…, adopta continuaciones nuevas),
  - detecta los vídeos nuevos (los cargue quien los cargue; sin shorts) y audita duplicados por videoId,
  - mantiene el brief por canal (prompt base del especialista) y responde preguntas vía NLM,
  - Reporter: publica el diario web con botón «Implementar» que avisa al coordinador (handoff a principal).

Datos: $YT_CHANNELS_HOME → channels.json, state.json, <slug>.md (brief), reporter/ (web)
"""
import argparse, fcntl, hashlib, html, http.server, json, os, pwd, re, shutil, subprocess, sys, unicodedata
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get('YT_CHANNELS_HOME') or Path.home() / '.local/share/yt-channels')
CFG, STATE, WEB = HOME / 'channels.json', HOME / 'state.json', HOME / 'reporter'
DEFAULTS = {'brief_every_new': 10, 'brief_max_age_days': 7, 'report_url': '', 'coordinator': None, 'feed_per_channel': 3,
            'channels': {}}
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
VID = re.compile(r'^[A-Za-z0-9_-]{11}$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CITE = re.compile(r'\s*\[\d+(?:\s*[,–-]\s*\d+)*\]')
UA = {'User-Agent': 'Mozilla/5.0'}
NEUTRAL = 'Para esta respuesta no adoptes ningún rol: escribe en español, en tercera persona, como analista. '
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
os.environ['COLUMNS'] = '10000'  # rich no debe partir líneas de las respuestas de nlm

# Lista de fuentes con videoId: `nlm source list --json` devuelve url=null para YouTube,
# así que leemos la respuesta cruda con el propio cliente de nlm.
SOURCES_PY = r'''
import json, re, sys
from notebooklm_tools.cli.utils import get_client
YT = re.compile(r'(?:v=|v\\u003d|youtu\.be/|/shorts/|/live/|embed/)([A-Za-z0-9_-]{11})')
with get_client() as c:
    raw = c.get_notebook(sys.argv[1])
nb = raw[0] if raw and isinstance(raw[0], list) else raw
out = []
for s in (nb[1] or []):
    m = YT.search(json.dumps(s[2] if len(s) > 2 else ''))
    out.append({'id': s[0][0], 'title': s[1], 'video_id': m.group(1) if m else None})
print(json.dumps(out, ensure_ascii=False))
'''


# ── utilidades ──────────────────────────────────────────────
def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p, data):
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)


def run(cmd, timeout=300, env=None):
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f'timeout {timeout}s'
    return p.returncode == 0, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()


NLM_REFRESH = []  # p. ej. ['systemctl','--user','start','nlm-auth.service'] (se configura en channels.json)


def run_nlm(cmd, timeout=300):
    """Ejecuta un comando de NotebookLM; si la sesión ha caducado, la renueva y reintenta una vez.

    La sesión de NLM en PCHP caduca a menudo (la mantiene el navegador dedicado del PC), así que
    conviene recuperarla sola en lugar de fallar y esperar a que alguien lo mire."""
    ok, out = run(cmd, timeout)
    if not ok and NLM_REFRESH and ('auth' in out.lower() or 'login' in out.lower()):
        try:
            subprocess.run(NLM_REFRESH, capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired:
            pass
        else:
            ok, out = run(cmd, timeout)
    return ok, out


def norm(title):
    t = unicodedata.normalize('NFKD', title or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', t).strip()


def roman(n):
    out = ''
    for v, s in ((50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):
        while n >= v:
            out, n = out + s, n - v
    return out


def clean(text):
    return CITE.sub('', text or '').strip()


# ── NotebookLM ──────────────────────────────────────────────
def nlm_python():
    nlm = shutil.which('nlm') or sys.exit('nlm no está en PATH')
    first = open(os.path.realpath(nlm), 'rb').readline().decode().strip()
    return first[2:].strip() if first.startswith('#!') else sys.executable


def sources(nb):
    """Fuentes del cuaderno. Lanza excepción si falla: un listado vacío falso daría por perdidos vídeos."""
    ok, out = run_nlm([nlm_python(), '-c', SOURCES_PY, nb], 180)
    if not ok:
        raise RuntimeError(f'no puedo listar fuentes de {nb}: {out[-300:]}')
    return json.loads(out)


def notebooks():
    ok, out = run_nlm(['nlm', 'notebook', 'list', '--json'], 120)
    if not ok:
        raise RuntimeError(f'nlm notebook list falló: {out[-300:]}')
    return json.loads(out)


def series(base, nb_list):
    """Cuadernos "<base>", "<base> II", "<base> III"… en orden (convención del usuario y de youtube-agent)."""
    order = {'': 1, **{f' {roman(i)}': i for i in range(2, 60)}}
    found = [(order[n['title'][len(base):]], n) for n in nb_list
             if n['title'].startswith(base) and n['title'][len(base):] in order]
    return [{'id': n['id'], 'title': n['title']} for _, n in sorted(found, key=lambda x: x[0])]


def persona(name):
    return (f'Eres el especialista del canal de YouTube «{name}» y respondes como si fueras su creador: '
            'en primera persona, con su tono, sus tesis y su vocabulario. Básate solo en las fuentes (sus vídeos) '
            'y cita los vídeos concretos. Si el canal no ha tratado algo, dilo; no inventes. '
            'Señala cuándo ha cambiado de opinión con el tiempo. ' + SAID_VS_INFERRED)


# separar lo que dice el creador de lo que deduce la IA (idea vídeo 9ZSb0YBV3qc, el usuario 27/11)
SAID_VS_INFERRED = ('Distingue siempre lo literal de lo deducido: marca «Dice:» lo que el creador afirma en un '
                    'vídeo (con su cita) e «Inferencia:» lo que tú concluyes o extrapolas sin que él lo diga.')


def configure_persona(nb, name):
    ok, out = run(['nlm', 'chat', 'configure', nb, '--goal', 'custom', '--prompt', persona(name)], 120)
    if not ok:
        print(f'⚠️ persona no configurada en {nb}: {out[-200:]}', file=sys.stderr)


def query(nb, question, source_ids=None):
    cmd = ['nlm', 'notebook', 'query', nb, question, '--json', '--timeout', '300']
    if source_ids:
        cmd += ['--source-ids', ','.join(source_ids)]
    ok, out = run_nlm(cmd, 420)
    if not ok:
        raise RuntimeError(f'query falló en {nb}: {out[-300:]}')
    try:
        return clean(json.loads(out).get('answer', out))
    except json.JSONDecodeError:
        return clean(out)


def ask_channel(ch, question, nbs=None):
    """Pregunta a cada cuaderno (orden cronológico). `nlm cross query` hace lo mismo pero solo imprime paneles rich."""
    nbs = nbs or ch['notebooks']
    if len(nbs) == 1:
        return query(nbs[0]['id'], question)
    return '\n\n'.join(f'### Según «{n["title"]}»\n{query(n["id"], question)}' for n in nbs)


# ── YouTube ─────────────────────────────────────────────────
def rss(ch):
    out = []
    ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
    for cid in ch.get('rss_ids') or [ch['channel_id']]:
        req = urllib.request.Request(f'https://www.youtube.com/feeds/videos.xml?channel_id={cid}', headers=UA)
        root = ET.fromstring(urllib.request.urlopen(req, timeout=30).read())
        for e in root.findall('a:entry', ns):
            link = e.find('a:link', ns)
            out.append({'vid': e.findtext('yt:videoId', '', ns), 'title': e.findtext('a:title', '', ns),
                        'published': e.findtext('a:published', '', ns),
                        'short': '/shorts/' in (link.get('href', '') if link is not None else '')})
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_short(vid):
    """youtube.com/shorts/<id> responde 200 si es short y redirige (303) a /watch si no.
    Sin cabeceras: un User-Agent de navegador hace que YouTube redirija antes a consent.youtube.com."""
    req = urllib.request.Request(f'https://www.youtube.com/shorts/{vid}', method='HEAD')
    try:
        return urllib.request.build_opener(_NoRedirect).open(req, timeout=20).status == 200
    except (urllib.error.URLError, TimeoutError):
        return False  # ante la duda, que salga en el diario


# ── comandos ────────────────────────────────────────────────
def cmd_add(a, cfg, st):
    url = (a.url if a.url.startswith('http') else f'https://www.youtube.com/{a.url}').rstrip('/')
    ok, out = run(['yt-dlp', '--flat-playlist', '--no-warnings', '-I', '1', '-J',
                   url if url.endswith('/videos') else url + '/videos'], 180)
    if not ok:
        sys.exit(f'No pude resolver el canal: {out[-300:]}')
    d = json.loads(out)
    name = a.name or d.get('channel') or d.get('uploader')
    slug = a.slug or norm(name).replace(' ', '-')
    base = a.notebook_title or name
    nbs = series(base, notebooks())
    cfg['channels'][slug] = {'name': name, 'channel_id': d.get('channel_id') or d.get('id'), 'url': url,
                             'notebook_title': base, 'notebooks': nbs}
    save(CFG, cfg)
    for n in nbs:
        configure_persona(n['id'], name)
    print(f'Canal «{name}» → slug {slug}; cuadernos: ' + (', '.join(n['title'] for n in nbs) or 'ninguno aún'))
    print('La ingesta la hace youtube-agent: añade también el canal a su config.json.')


def sync_channel(slug, ch, cfg, st, log, nb_list):
    sc = st['channels'].setdefault(slug, {'videos': {}})
    vids = sc['videos']
    first = not vids  # primera reconciliación: lo que ya había no es noticia

    # 1) adoptar continuaciones creadas por youtube-agent ("X III"…)
    known = {n['id'] for n in ch['notebooks']}
    for n in series(ch['notebook_title'], nb_list):
        if n['id'] not in known:
            ch['notebooks'].append(n)
            configure_persona(n['id'], ch['name'])
            save(CFG, cfg)
            log.append(f'📓 {ch["name"]}: adoptado cuaderno «{n["title"]}»')

    # 2) RSS: fecha de publicación y marca de short
    for e in rss(ch):
        r = vids.setdefault(e['vid'], {'title': e['title'], 'status': 'pending'})
        r.setdefault('published', e['published'])
        r['short'] = e['short']

    # 3) NLM manda: lo que aparece en los cuadernos está cargado (youtube-agent, extensión o a mano)
    for n in ch['notebooks']:
        for s in sources(n['id']):
            vid = s['video_id']
            r = vids.setdefault(vid, {'title': s['title']}) if vid else None
            if r is None or r.get('status') == 'added':
                continue
            if 'short' not in r:
                r['short'] = r.get('status') == 'skipped_short' or (not first and is_short(vid))
            r.update(status='added', title=s['title'], notebook=n['id'], source_id=s['id'], added_at=now(),
                     in_feed=first or r['short'])
            if not r['in_feed']:
                sc['added_since_brief'] = sc.get('added_since_brief', 0) + 1
    save(STATE, st)

    # 4) refrescar el brief si hay bastante material nuevo o está viejo
    age = datetime.now(timezone.utc) - datetime.fromisoformat(sc['brief_at']) if sc.get('brief_at') else None
    new = sc.get('added_since_brief', 0)
    if ch['notebooks'] and (age is None or new >= cfg['brief_every_new']
                            or (new and age > timedelta(days=cfg['brief_max_age_days']))):
        write_brief(slug, ch, st)
        save(STATE, st)
        log.append(f'🧠 {ch["name"]}: brief actualizado')


def cmd_sync(a, cfg, st):
    log = []
    try:
        nb_list = notebooks()
    except RuntimeError as e:
        print(f'⚠️ {e}')
        return
    for slug in a.slugs or list(cfg['channels']):
        try:
            sync_channel(slug, cfg['channels'][slug], cfg, st, log, nb_list)
        except Exception as e:  # un canal roto no debe parar el resto
            log.append(f'⚠️ {slug}: {e}')
    if not sys.stdout.isatty():  # cron: solo incidencias
        log = [line for line in log if line.startswith('⚠️')]
    if log:
        print('\n'.join(log))


def write_brief(slug, ch, st):
    q = (NEUTRAL + f'Haz el BRIEF del canal de YouTube «{ch["name"]}» con estos apartados en negrita (no uses '
         'encabezados #): **Quién es y de qué habla**, **Tesis y convicciones principales (con matices)**, '
         '**Marcos o métodos que repite**, **Estilo y vocabulario (cómo habla)**, **Temas recurrentes**, '
         '**Cambios de opinión en el tiempo**, **Recomendaciones accionables que suele dar**. '
         'Máximo 500 palabras, concreto, sin relleno. ' + SAID_VS_INFERRED)
    # ponytail: solo los 3 cuadernos más recientes; el histórico profundo se consulta con `ask`
    body = ask_channel(ch, q, ch['notebooks'][-3:])
    sc = st['channels'].setdefault(slug, {'videos': {}})
    latest = sorted((r for r in sc['videos'].values()
                     if r.get('status') == 'added' and r.get('published') and not r.get('short')),
                    key=lambda r: r['published'], reverse=True)[:10]
    nb_lines = '\n'.join(f'- {n["title"]} (`{n["id"]}`)' for n in ch['notebooks'])
    latest_lines = '\n'.join(f'- {r["published"][:10]} — {r["title"]}' for r in latest)
    (HOME / f'{slug}.md').write_text(
        f'---\ntype: Reference\ntitle: Brief canal YouTube {ch["name"]}\ntimestamp: {now()}\n---\n\n'
        f'# {ch["name"]} — brief del especialista\n\nCanal: {ch["url"]}\n\n{body}\n\n'
        f'## Últimos vídeos cargados\n{latest_lines or "- (sin fechas aún)"}\n\n'
        f'## Base de conocimiento (NotebookLM)\n{nb_lines}\n\n'
        f'Profundizar: `ytc ask {slug} "pregunta"`\n')
    sc['brief_at'], sc['added_since_brief'] = now(), 0


def cmd_brief(a, cfg, st):
    for slug in a.slugs or list(cfg['channels']):
        write_brief(slug, cfg['channels'][slug], st)
        save(STATE, st)
        print(HOME / f'{slug}.md')


def cmd_ask(a, cfg, st):
    print(ask_channel(cfg['channels'][a.slug], a.question))


def cmd_show(a, cfg, st):
    p = HOME / f'{a.slug}.md'
    print(p.read_text() if p.exists() else f'Sin brief aún: ejecuta `ytc brief {a.slug}`')


def cmd_audit(a, cfg, st):
    dupes = 0
    for slug in a.slugs or list(cfg['channels']):
        ch = cfg['channels'][slug]
        groups = {}
        for n in ch['notebooks']:
            for s in sources(n['id']):
                groups.setdefault(s['video_id'] or 't:' + norm(s['title']), []).append((n, s))
        print(f'## {ch["name"]}: {sum(map(len, groups.values()))} fuentes en {len(ch["notebooks"])} cuadernos, '
              f'{len(groups)} vídeos únicos')
        for g in groups.values():
            if len(g) < 2:
                continue
            dupes += len(g) - 1
            print(f'- DUPLICADO ×{len(g)} «{g[0][1]["title"]}»: ' + '; '.join(f'{n["title"]}/{s["id"]}' for n, s in g))
            if a.fix:  # conserva la primera copia (cuaderno más antiguo)
                for n, s in g[1:]:
                    ok, out = run(['nlm', 'source', 'delete', s['id'], '--confirm'], 120)
                    print(f'  {"borrada" if ok else "ERROR borrando"} {n["title"]}/{s["id"]} {"" if ok else out[-150:]}')
    print(f'Total duplicados: {dupes}' + ('' if a.fix or not dupes else ' (usa --fix para borrar las copias)'))


def cmd_summary(a, cfg, st):
    cut = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat()
    for slug in a.slugs or list(cfg['channels']):
        ch = cfg['channels'][slug]
        items = [r for r in st['channels'].get(slug, {}).get('videos', {}).values()
                 if r.get('status') == 'added' and r.get('source_id') and not r.get('short')
                 and max(r.get('published', ''), r.get('added_at', '')) >= cut]
        print(f'# {ch["name"]} — últimos {a.days} días: {len(items)} vídeos')
        for r in sorted(items, key=lambda r: r.get('published', '')):
            print(f'- {r.get("published", "")[:10]} {r["title"]}')
        by_nb = {}
        for r in items:
            by_nb.setdefault(r['notebook'], []).append(r['source_id'])
        for nb, sids in by_nb.items():
            print('\n' + query(nb, NEUTRAL + 'Resume lo que se ha publicado en estos vídeos: temas, tesis '
                               'principales, qué es nuevo o contradice lo dicho antes, y lo aprendido/accionable. '
                               'Agrupa por ideas, no vídeo a vídeo. Máximo 400 palabras.', sids))
        print()


# ── Reporter ────────────────────────────────────────────────
FEED_RE = re.compile(r'###\s*(\d+)\s*\n(.*?)(?=\n###\s*\d+|\Z)', re.S)


def feed_items(ch, items, slug=None):
    """Una query por cuaderno (bloques de 10) que devuelve novedad + accionable por vídeo."""
    by_nb = {}
    for vid, r in items:
        by_nb.setdefault(r['notebook'], []).append((vid, r))
    out = []
    for nb, group in by_nb.items():
        for i in range(0, len(group), 10):
            chunk = group[i:i + 10]
            q = (NEUTRAL + f'Para CADA uno de estos {len(chunk)} vídeos responde EXACTAMENTE con este formato y nada más:\n'
                 '### <número>\nNOVEDAD: <qué cuenta de nuevo o relevante, 2-3 frases>\n'
                 'ACCIONABLE: <algo concreto que el usuario podría implementar en su PC o en su agente personal, o NADA>\n'
                 'Ignora patrocinios, anuncios y códigos de descuento: no son novedad ni accionable.\n\n'
                 'Vídeos:\n' + '\n'.join(f'{k}. {r["title"]}' for k, (_, r) in enumerate(chunk, 1)))
            # Si NotebookLM falla (sesión caducada), la edición NO se publica: mejor fallar en claro que
            # publicar un diario sin resúmenes. El estado queda intacto y el cron lo reintenta.
            parsed = {int(m.group(1)): m.group(2) for m in FEED_RE.finditer(query(nb, q, [r['source_id'] for _, r in chunk]))}
            for k, (vid, r) in enumerate(chunk, 1):
                blk = parsed.get(k, '')
                nov = re.search(r'NOVEDAD:\s*(.+?)(?=\nACCIONABLE:|\Z)', blk, re.S)
                acc = re.search(r'ACCIONABLE:\s*(.+)', blk, re.S)
                acc = acc.group(1).strip() if acc else ''
                out.append({'vid': vid, 'slug': slug, 'title': r['title'], 'published': r.get('published', ''),
                            'notebook': next((n['title'] for n in ch['notebooks'] if n['id'] == nb), nb),
                            'novedad': nov.group(1).strip() if nov else 'NotebookLM no devolvió resumen para este vídeo.',
                            'accionable': '' if acc.upper().startswith('NADA') else acc})
    return out


CSS = '''
:root{--ink:#1d1b16;--paper:#f7f3ea;--rule:#c9bfa8;--accent:#8b1e1e;--muted:#6b6457}
@media (prefers-color-scheme:dark){:root{--ink:#ece6d8;--paper:#171512;--rule:#4a443a;--accent:#e07a5f;--muted:#a39b8b}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 Georgia,"Times New Roman",serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
header{text-align:center;border-bottom:4px double var(--ink);padding-bottom:10px;margin-bottom:6px}
header h1{font-size:clamp(34px,6vw,64px);margin:0;letter-spacing:.5px;font-weight:900}
.dateline{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font:13px/1.4 system-ui,sans-serif;
 text-transform:uppercase;letter-spacing:1px;color:var(--muted);border-bottom:1px solid var(--rule);padding:6px 0;margin-bottom:22px}
.dateline a{color:inherit}
section{margin-bottom:34px}
section h2{font:800 15px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:2px;color:var(--accent);
 border-top:3px solid var(--ink);padding-top:8px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:22px 26px}
article{border-bottom:1px solid var(--rule);padding-bottom:18px}
article img{width:100%;aspect-ratio:16/9;object-fit:cover;filter:saturate(.85);border:1px solid var(--rule)}
article h3{font-size:22px;line-height:1.2;margin:10px 0 6px}article h3 a{color:inherit;text-decoration:none}
article h3 a:hover{text-decoration:underline}
.meta{font:12px/1.4 system-ui,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.act{margin-top:12px;padding:10px 12px;border-left:3px solid var(--accent);background:color-mix(in srgb,var(--accent) 7%,transparent)}
.act b{font:700 12px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:1px;color:var(--accent)}
.chatwrap{margin-top:10px;border-top:1px dashed var(--rule);padding-top:9px}
.chat{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chat input{flex:1;min-width:130px;font:14px system-ui,sans-serif;padding:7px 9px;border:1px solid var(--rule);
 background:transparent;color:var(--ink);border-radius:3px}
.chat button{margin-top:0;font-size:13px;padding:6px 12px}
.log{font:14px/1.5 system-ui,sans-serif}
.log .q{font-weight:700;margin:10px 0 3px}
.log .a{white-space:pre-wrap;margin-bottom:4px}
.log .w{color:var(--muted);font-style:italic;margin-bottom:4px}
button{margin-top:10px;font:600 14px system-ui,sans-serif;padding:8px 14px;border:1px solid var(--accent);background:var(--accent);
 color:#fff;border-radius:3px;cursor:pointer}button[disabled]{opacity:.65;cursor:default;background:transparent;color:var(--accent)}
ul.ed{list-style:none;padding:0;font:16px system-ui,sans-serif}ul.ed li{padding:8px 0;border-bottom:1px solid var(--rule)}
ul.ed a{color:var(--ink);font-weight:600}
@media (max-width:640px){
 body{font-size:16px}
 .wrap{padding:16px 13px 48px}
 header h1{font-size:clamp(26px,9vw,40px);letter-spacing:0}
 .dateline{font-size:11.5px;letter-spacing:.5px}
 .grid{grid-template-columns:1fr;gap:20px}
 article h3{font-size:20px}
 article img{aspect-ratio:16/9}
 .chat input{font-size:16px}  /* 16px evita el zoom automático al enfocar en iOS */
 .chat button{padding:10px 14px;font-size:14px}
 .act{padding:9px 10px}
}
@media print{  /* el PDF del diario: sin chat ni botones, dos columnas y miniaturas pequeñas */
 .chatwrap{display:none}
 .act button{display:none}
 header h1{font-size:34px}
 body{font-size:11pt;background:#fff;color:#000}
 .wrap{padding:0}
 .grid{grid-template-columns:repeat(2,1fr);gap:14px 18px}
 article img{height:120px;border:none}
 article{break-inside:avoid;page-break-inside:avoid}
}
footer{margin-top:30px;font:13px system-ui,sans-serif;color:var(--muted);text-align:center}
'''

JS = '''
const sent=new Set();
fetch('api/sent').then(r=>r.json()).then(d=>{for(const v of d)sent.add(v);mark()}).catch(()=>{});
function mark(){for(const b of document.querySelectorAll('button[data-vid]'))if(sent.has(b.dataset.vid)){b.disabled=true;b.textContent='Enviado a Hermes ✓'}}
document.addEventListener('click',async e=>{const b=e.target.closest('button[data-vid]');if(!b||b.disabled)return;
 b.disabled=true;b.textContent='Enviando…';
 try{const r=await fetch('api/implement',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({day:b.dataset.day,vid:b.dataset.vid})});const d=await r.json();
  if(!r.ok)throw new Error(d.error||r.status);sent.add(b.dataset.vid);mark()}
 catch(err){b.disabled=false;b.textContent='Error: '+err.message+' · reintentar'}});
async function ask(box){const i=box.querySelector('input');const q=i.value.trim();if(q.length<3)return;
 const log=box.closest('.chatwrap').querySelector('.log');
 const qp=document.createElement('div');qp.className='q';qp.textContent='❓ '+q;log.append(qp);
 const ap=document.createElement('div');ap.className='w';ap.textContent='Consultando a NotebookLM…';log.append(ap);
 i.value='';i.disabled=true;const btn=box.querySelector('button');btn.disabled=true;
 try{const r=await fetch('api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({day:box.dataset.day,vid:box.dataset.vid,question:q})});const d=await r.json();
  if(!r.ok)throw new Error(d.error||r.status);ap.className='a';ap.textContent=d.answer}
 catch(err){ap.className='w';ap.textContent='No he podido preguntar: '+err.message}
 i.disabled=false;btn.disabled=false;i.focus()}
document.addEventListener('click',e=>{const b=e.target.closest('.chat button');if(b)ask(b.closest('.chat'))});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('.chat input')){e.preventDefault();ask(e.target.closest('.chat'))}});
'''


def page(title, body, script=''):
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style>'
            f'</head><body><div class="wrap">{body}</div>{f"<script>{script}</script>" if script else ""}</body></html>')


def render_edition(day, sections, thumb='hqdefault'):
    e = html.escape
    total = sum(len(i) for _, i in sections)
    body = [f'<header><h1>El Diario de los Canales</h1></header><div class="dateline"><span>{day}</span>'
            f'<span>{total} vídeos · {len(sections)} canales</span><a href="./">Ediciones anteriores</a></div>']
    for name, items in sections:
        body.append(f'<section><h2>{e(name)}</h2><div class="grid">')
        for it in items:
            url = f'https://www.youtube.com/watch?v={it["vid"]}'
            act = (f'<div class="act"><b>🛠️ Accionable</b><p>{e(it["accionable"])}</p>'
                   f'<button data-day="{day}" data-vid="{it["vid"]}">Implementar</button></div>'
                   if it['accionable'] else '')
            chat = (f'<div class="chatwrap"><div class="log"></div><div class="chat" data-day="{day}" '
                    f'data-vid="{it["vid"]}"><input type="text" placeholder="Pregunta sobre este vídeo…" '
                    f'aria-label="Pregunta sobre este vídeo"><button>Preguntar</button></div></div>')
            body.append(f'<article><a href="{url}" target="_blank" rel="noopener"><img loading="lazy" alt="" '
                        f'src="https://i.ytimg.com/vi/{it["vid"]}/{thumb}.jpg"></a>'
                        f'<h3><a href="{url}" target="_blank" rel="noopener">{e(it["title"])}</a></h3>'
                        f'<div class="meta">{e(it["published"][:10])} · {e(it["notebook"])}</div>'
                        f'<p>{e(it["novedad"])}</p>{act}{chat}</article>')
        body.append('</div></section>')
    body.append('<footer>Reporter · resúmenes generados por NotebookLM a partir de los vídeos. Las cifras y '
                'predicciones son afirmaciones de cada canal, no hechos verificados.</footer>')
    body.append(f"<script>localStorage.setItem('visto-c{day}','1')</script>")  # índice inbox: 🆕 hasta abrirlo
    return page(f'Diario de los Canales — {day}', ''.join(body), JS)


def render_index():
    rows = []
    for p in sorted((WEB / 'editions').glob('*.json'), reverse=True):
        d = json.loads(p.read_text())
        n = sum(len(s['items']) for s in d['sections'])
        rows.append(f'<li><a href="{p.stem}.html">{p.stem}</a> — {n} vídeos · '
                    f'{html.escape(", ".join(s["name"] for s in d["sections"]))}</li>')
    (WEB / 'index.html').write_text(page('El Diario de los Canales', '<header><h1>El Diario de los Canales</h1>'
                                         '</header><div class="dateline"><span>Ediciones</span></div>'
                                         f'<ul class="ed">{"".join(rows)}</ul>'))


def reporter_base(cfg):
    """Base del enlace: https://host/reporter (+ /<clave> si hay clave pública configurada)."""
    base = cfg['report_url'].rstrip('/')
    return f'{base}/{cfg["public_token"]}' if cfg.get('public_token') else base


def announce(cfg, day=None):
    """Regenera el HTML de una edición (por si cambia la maquetación) y devuelve el aviso para Telegram."""
    ed = WEB / 'editions'
    days = sorted(p.stem for p in ed.glob('*.json'))
    if day is None:
        if not days:
            return ''
        day = days[-1]
    path = ed / f'{day}.json'
    if not DAY.match(day) or not path.exists():
        return ''
    edition = load(path, {'day': day, 'sections': []})
    pages = [(s['name'], s['items']) for s in edition['sections']]
    (WEB / f'{day}.html').write_text(render_edition(day, pages))
    # versión para el PDF: miniaturas pequeñas (1 MB en vez de 3,4 MB para el móvil)
    (WEB / f'{day}.print.html').write_text(render_edition(day, pages, thumb='mqdefault'))
    render_index()
    total = sum(len(s['items']) for s in edition['sections'])
    # Mensaje corto a propósito: el detalle (videos, canales, ideas accionables) está en la web.
    return '\n'.join([f'🗞️ Diario de los Canales — {day} · {total} vídeos en {len(edition["sections"])} canales',
                      f'🔗 {reporter_base(cfg)}/{day}.html'])


def notify_coordinator(cfg, key, title, text, profile=None):
    """Handoff durable al coordinador (o al especialista indicado); devuelve el error o ''."""
    co = cfg.get('coordinator')
    if not co:
        return ''
    src = Path(co['dir']) / f'{key}.md'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(text)
    # el notificador resuelve los perfiles: este script corre con HOME de NLM, así que
    # el home real se obtiene del usuario (pwd), no de $HOME.
    env = dict(os.environ, HOME=pwd.getpwuid(os.getuid()).pw_dir)
    ok, out = run([co['hermes_knowledge'], 'handoff', '--profile', profile or co['profile'], '--source', str(src),
                   '--key', key, '--title', title[:190], '--owner-directed'], 120, env)
    return '' if ok else out[-300:]


def cmd_feed(a, cfg, st):
    sections, seen = [], []  # seen: todo lo pendiente, también lo recortado, para que no reaparezca mañana
    n = cfg['feed_per_channel']
    for slug in a.slugs or list(cfg['channels']):
        ch = cfg['channels'][slug]
        items = [(v, r) for v, r in st['channels'].get(slug, {}).get('videos', {}).items()
                 if r.get('status') == 'added' and r.get('in_feed') is False and r.get('source_id')]
        if items:
            seen += [(slug, v) for v, _ in items]
            sections.append((slug, ch['name'], sorted(feed_items(ch, items, slug), key=lambda x: x['published'])[-n:]))
    day = f'{datetime.now():%Y-%m-%d}'
    (WEB / 'editions').mkdir(parents=True, exist_ok=True)
    ed_path = WEB / 'editions' / f'{day}.json'
    if not sections:
        # el usuario quiere cada día la edición fechada de ese día: sin novedades, se arrastra la última.
        prev = sorted(p for p in (WEB / 'editions').glob('*.json') if p.stem < day)
        if ed_path.exists() or not prev:
            return  # cron silencioso
        save(ed_path, dict(load(prev[-1], {'sections': []}), day=day, carried_from=prev[-1].stem))
        print(announce(cfg, day))
        return
    edition = load(ed_path, {'day': day, 'sections': []})  # segunda edición del día: se añade a la primera
    for _, name, items in sections:
        sec = next((s for s in edition['sections'] if s['name'] == name), None)
        if sec is None:
            edition['sections'].append({'name': name, 'items': list(items)})
            continue
        by_vid = {i['vid']: i for i in sec['items']}  # los datos frescos sustituyen a los guardados
        for i in items:
            if i['vid'] in by_vid:
                by_vid[i['vid']].update(i)
            else:
                sec['items'].append(i)
        sec['items'] = sorted(sec['items'], key=lambda x: x['published'])[-n:]
    save(ed_path, edition)
    for slug, vid in seen:
        st['channels'][slug]['videos'][vid]['in_feed'] = True
    save(STATE, st)

    url = f'{cfg["report_url"].rstrip("/")}/{day}.html'
    md = (f'# Reporter — Diario de los Canales {day}\n\nEdición web: {url}\n\n'
          + '\n\n'.join(f'## {n}\n' + '\n'.join(
              f'- [{it["title"]}](https://www.youtube.com/watch?v={it["vid"]}) ({it["published"][:10]}): {it["novedad"]}'
              + (f'\n  - Accionable: {it["accionable"]}' if it['accionable'] else '') for it in i)
              for _, n, i in sections)
          + '\n\nFuente: resúmenes de NotebookLM sobre los vídeos; afirmaciones de cada canal, no hechos verificados.\n')
    key = f'reporter-{day}-{hashlib.sha1(md.encode()).hexdigest()[:8]}'
    err = notify_coordinator(cfg, key, f'Reporter: Diario de los Canales {day}', md)
    out = announce(cfg, day)
    print(out + (f'\n⚠️ aviso al coordinador falló: {err}' if err else ''))


def implement(cfg, day, vid):
    if not cfg.get('coordinator'):
        raise ValueError('el aviso al coordinador no está configurado en esta copia (modo pruebas)')
    if not (DAY.match(day or '') and VID.match(vid or '')):
        raise ValueError('petición inválida')
    sent_path = WEB / 'sent.json'
    sent = load(sent_path, {})
    if vid in sent:
        return sent[vid]
    edition = load(WEB / 'editions' / f'{day}.json', {'sections': []})
    found = next(((s['name'], i) for s in edition['sections'] for i in s['items'] if i['vid'] == vid), None)
    if not found or not found[1]['accionable']:
        raise ValueError('vídeo sin accionable en esa edición')
    name, it = found
    slug = it.get('slug') or next((s for s, c in cfg.get('channels', {}).items() if c['name'] == name), None)
    spec = cfg.get('channels', {}).get(slug or '', {}).get('specialist')
    md = (f'# el usuario quiere implementar: {it["title"]}\n\nPulsado «Implementar» en el Reporter ({day}) '
          f'el {now()}.\n\n- Canal: {name}\n'
          + (f'- Especialista de ese canal: `{spec}` (por si prefieres que lo valore y te proponga un plan)\n' if spec else '')
          + f'- Vídeo: https://www.youtube.com/watch?v={vid}\n'
            f'- Cuaderno NotebookLM: {it["notebook"]}\n- Qué dice: {it["novedad"]}\n'
            f'- Accionable propuesto: {it["accionable"]}\n\n'
            'Tú decides: implementarlo en PCHP o pasárselo al especialista del canal. Si necesitas el detalle del '
            'vídeo, `ytc ask <canal> "..."` o `ytc summary <canal> --days 7`. En cualquier caso, aplica solo tras el '
            'visto bueno del usuario.\n')
    keys = {'coordinator': f'yt-implement-{hashlib.sha1(vid.encode()).hexdigest()[:12]}'}
    err = notify_coordinator(cfg, keys['coordinator'], f'Implementar desde Reporter: {it["title"]}', md)
    if err:
        raise RuntimeError(f'al coordinador: {err}')
    sent[vid] = {'day': day, 'at': now(), 'keys': keys}
    save(sent_path, sent)
    return sent[vid]


def ask_video(cfg, st, day, vid, question):
    """Responde una pregunta sobre un vídeo concreto consultando NotebookLM acotado a su fuente."""
    if not (DAY.match(day or '') and VID.match(vid or '')):
        raise ValueError('petición inválida')
    q = ' '.join((question or '').split())
    if not 3 <= len(q) <= 500:
        raise ValueError('la pregunta debe tener entre 3 y 500 caracteres')
    edition = load(WEB / 'editions' / f'{day}.json', {'sections': []})
    item = next((i for s in edition['sections'] for i in s['items'] if i['vid'] == vid), None)
    if item is None:
        raise ValueError('ese vídeo no está en la edición de ese día')
    rec = next((r for c in st.get('channels', {}).values()
                for v, r in c.get('videos', {}).items() if v == vid), None) or {}
    nb, sid = rec.get('notebook'), rec.get('source_id')
    if not nb:  # respaldo: resolver el cuaderno por su título (ediciones antiguas sin source_id)
        nb = next((n['id'] for c in cfg.get('channels', {}).values() for n in c.get('notebooks', [])
                   if n['title'] == item.get('notebook')), None)
    if not nb:
        raise ValueError('no encuentro ese vídeo en NotebookLM')
    prompt = (NEUTRAL + f'el usuario quiere profundizar en un vídeo concreto del canal: «{item["title"]}». '
              f'Responde basándote SOLO en ese vídeo (indica el minuto si lo tienes) y en español. '
              f'Pregunta del usuario: {q}')
    return {'answer': query(nb, prompt, [sid] if sid else None)}


INBOX_STATE = Path(os.environ.get('YT_INBOX_STATE') or HOME / 'inbox-state')


def inbox_action(vid, action, text=''):
    """Botones del Diario de Inbox Hermes: encola implementar/deshacer; lo ejecuta la revisión de Inbox Hermes."""
    text = ' '.join((text or '').split())[:2000]
    if not VID.match(vid or '') or action not in ('implement', 'undo', 'cancel', 'comment') or (action == 'comment' and not text):
        raise ValueError('petición inválida')
    q = INBOX_STATE / 'acciones.json'
    acts = load(q, {})
    acts[vid] = {'action': action, 'at': now(), 'status': 'pendiente', **({'comentario': text} if text else {})}
    save(q, acts)
    try:  # que corra ya, sin esperar a la siguiente vuelta de 4 h
        subprocess.Popen(os.environ.get('YT_INBOX_HOOK', 'true').split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return acts[vid]


def cmd_serve(a, cfg, st):
    WEB.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB), **kwargs)

        def _route(self):  # Tailscale Serve monta en /reporter; aceptamos la ruta con o sin prefijo
            self.path = self.path.removeprefix('/reporter') or '/'
            tok = ({**DEFAULTS, **load(CFG, {})}).get('public_token') or ''
            if tok:  # enlace privado: sin la clave no se sirve nada
                head, _, rest = self.path.lstrip('/').partition('/')
                if head != tok:
                    return None
                self.path = '/' + rest
            return self.path.split('?', 1)[0]

        def _json(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self._route()
            if path is None:  # sin la clave del enlace privado
                return self.send_error(404)
            if path == '/api/inbox':
                return self._json(200, load(INBOX_STATE / 'acciones.json', {}))
            if path == '/api/sent':
                return self._json(200, list(load(WEB / 'sent.json', {})))
            if path.endswith('.json'):  # ediciones y estado: solo vía HTML/API
                return self.send_error(404)
            super().do_GET()

        def do_POST(self):
            path = self._route()
            if path not in ('/api/implement', '/api/ask', '/api/inbox'):
                return self.send_error(404)
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length') or 0), 8192)) or b'{}')
                cfg = {**DEFAULTS, **load(CFG, {})}  # se relee: el proceso vive días
                NLM_REFRESH[:] = cfg.get('nlm_refresh') or []
                st = load(STATE, {'channels': {}})
                if path == '/api/ask':
                    res = ask_video(cfg, st, req.get('day'), req.get('vid'), req.get('question'))
                    asks = load(WEB / 'asks.json', [])  # registro para el acuse diario por Telegram
                    asks.append({'at': now(), 'day': req.get('day'), 'vid': req.get('vid'), 'q': req.get('question'), 'a': (res.get('answer') or '')[:600]})
                    save(WEB / 'asks.json', asks[-500:])
                    return self._json(200, res)
                if path == '/api/inbox':
                    return self._json(200, inbox_action(req.get('vid'), req.get('action'), req.get('text')))
                return self._json(200, implement(cfg, req.get('day'), req.get('vid')))
            except ValueError as e:
                return self._json(400, {'error': str(e)})
            except Exception as e:
                return self._json(500, {'error': str(e)[:300]})

        def log_message(self, *args):
            pass

    http.server.ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


def cmd_link(a, cfg, st):
    print(announce(cfg, a.day) or '⚠️ Reporter: todavía no hay ninguna edición')


def cmd_status(a, cfg, st):
    for slug, ch in cfg['channels'].items():
        vids = st['channels'].get(slug, {}).get('videos', {})
        added = [r for r in vids.values() if r.get('status') == 'added']
        last = max((r for r in added if r.get('published') and not r.get('short')),
                   key=lambda r: r['published'], default=None)
        print(f'{slug}: {ch["name"]} | cuadernos: {", ".join(n["title"] for n in ch["notebooks"])} | '
              f'{len(added)} en NLM | pendientes diario: {sum(1 for r in added if r.get("in_feed") is False)} | '
              f'último: {last["published"][:16] + " " + last["title"][:60] if last else "-"} | '
              f'brief: {st["channels"].get(slug, {}).get("brief_at", "nunca")}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('add', help='registrar un canal (adopta cuadernos "<título>", "<título> II"… existentes)')
    p.add_argument('url', help='URL del canal o @handle')
    p.add_argument('--name'); p.add_argument('--slug')
    p.add_argument('--notebook-title', help='nombre base de los cuadernos (por defecto el del canal)')
    p = sub.add_parser('sync', help='reconciliar cuadernos, detectar vídeos nuevos, refrescar briefs (cron)')
    p.add_argument('slugs', nargs='*')
    p = sub.add_parser('audit', help='buscar duplicados en los cuadernos del canal')
    p.add_argument('slugs', nargs='*'); p.add_argument('--fix', action='store_true', help='borrar copias duplicadas')
    p = sub.add_parser('ask', help='preguntar al especialista (consulta NLM)')
    p.add_argument('slug'); p.add_argument('question')
    p = sub.add_parser('summary', help='resumen de lo publicado/aprendido en N días')
    p.add_argument('slugs', nargs='*'); p.add_argument('--days', type=int, default=7)
    p = sub.add_parser('brief', help='regenerar el brief (prompt base) del canal')
    p.add_argument('slugs', nargs='*')
    p = sub.add_parser('show', help='imprimir el brief del canal')
    p.add_argument('slug')
    p = sub.add_parser('feed', help='Reporter: publicar la edición web con lo nuevo y avisar al coordinador')
    p.add_argument('slugs', nargs='*')
    p = sub.add_parser('serve', help='servidor web del Reporter (loopback; Tailscale Serve lo publica)')
    p.add_argument('--port', type=int, default=8790)
    p.add_argument('--host', default='127.0.0.1', help='interfaz de escucha (0.0.0.0 solo para pruebas locales)')
    p = sub.add_parser('link', help='aviso con el enlace de la edición del día (regenera su HTML)')
    p.add_argument('--day', help='AAAA-MM-DD; por defecto, la última edición')
    sub.add_parser('status')
    a = ap.parse_args()

    HOME.mkdir(parents=True, exist_ok=True)
    cfg = {**DEFAULTS, **load(CFG, {})}
    st = load(STATE, {'channels': {}})
    global NLM_REFRESH
    NLM_REFRESH = cfg.get('nlm_refresh') or []
    for s in getattr(a, 'slugs', None) or ([a.slug] if a.cmd in ('ask', 'show') else []):
        if s not in cfg['channels']:
            sys.exit(f'Canal desconocido: {s}. Disponibles: {", ".join(cfg["channels"])}')
    if a.cmd in ('add', 'sync', 'audit', 'brief', 'feed', 'link'):
        lock = open(HOME / '.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # otra ejecución en curso: no es un error para el cron
            if sys.stdout.isatty():
                print('Otra ejecución en curso', file=sys.stderr)
            return
    globals()['cmd_' + a.cmd](a, cfg, st)


if __name__ == '__main__':
    main()
