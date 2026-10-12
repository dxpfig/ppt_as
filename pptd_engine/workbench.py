"""Reusable PPTD browser workbench and an isolated snapshot print server.

The application owns authentication, task publication and edit routes. This
module only supplies scoped HTML, font manifests, and a loopback read-only
server for immutable document snapshots used by Chromium PDF export.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import threading
from urllib.parse import unquote, urlsplit

from .document import load_yaml
from .fonts import FontResolver, document_font_usage

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / 'packages/pptd-runtime/build'
FONT_CACHE = ROOT / 'runtime/fonts'
WEB_DIR = Path(__file__).parent / 'web'


def _url_base(value):
    if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]*', value) or value.startswith('//') or '..' in value.split('/'):
        raise ValueError('Workbench URLs must be safe same-origin paths')
    return value.rstrip('/')


def workbench_html(api_base, runtime_base='/api/studio/pptd-runtime', *, project_id=None, embed=False):
    api_base, runtime_base = _url_base(api_base), _url_base(runtime_base)
    config = json.dumps({'apiBase': api_base, 'projectId': project_id, 'embed': bool(embed)}, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return (WEB_DIR / 'index.html').read_text(encoding='utf-8').replace('__PPTD_RUNTIME__', runtime_base).replace('__PPTD_CONFIG__', config)


def capabilities():
    return {
        'label': '演示文稿工作台',
        'authority': 'PPTD v2 (.pptd + .page + resources)',
        'advanced': {
            'math': 'KaTeX preview; native editable Office Math in PPTX',
            'icons': 'Font Awesome 7 Free SVG preview and editable paths',
            'animations': '22 browser effects and native PowerPoint timing',
            'charts': '13 chart types; editable vectors for types without native Office equivalents',
            'fonts': 'Shared font cache; real EOT embedding with permission and coverage checks',
        },
        'limitations': ['Microsoft PowerPoint device-level animation validation remains manual', 'Not a complete arbitrary PowerPoint template or HTML/CSS importer'],
        'persistence': 'Authoritative PPTD, immutable versions, optimistic revision checks',
        'pdf': 'Chromium print from the same saved snapshot; preserves rendered formulas',
    }


def fonts_for_document(state, font_base='/api/studio/pptd-fonts'):
    font_base = _url_base(font_base)
    document = state['document']
    resolver = FontResolver(FONT_CACHE, document.get('customFonts', []))
    assets, errors = [], []
    pages = state['pages']
    page_usage = {page['path']: document_font_usage(document, [page['data']]) for page in pages}
    element_usage = {}
    for key, chars in document_font_usage(document, [p['data'] for p in pages]).items():
        family, weight, italic = key
        details = {'family': family, 'weight': weight, 'style': 'italic' if italic else 'normal'}
        try:
            asset = resolver.resolve(family, weight, italic)
        except Exception as error:
            affected = [path for path, usage in page_usage.items() if usage.get(key)]
            errors.append({**details, 'error': str(error), 'pages': affected,
                           **({'location': 'document.customFonts'} if not affected else {})})
            continue
        # A declared font can be valid for Latin on one page and lack Chinese
        # glyphs on another. Keep the actual face available and locate coverage
        # failures using the page's characters, not every occurrence of its name.
        assets.append({**asset.public(), 'url': font_base + '/' + asset.path.name})
        try:
            asset.check_coverage(''.join(chars))
        except ValueError:
            for page in pages:
                page_chars = page_usage[page['path']].get(key, set())
                if not page_chars:
                    continue
                try:
                    asset.check_coverage(''.join(page_chars))
                except ValueError as page_error:
                    located = False
                    for element in page['data'].get('elements', []):
                        identity = (page['path'], element.get('elementId'))
                        if identity not in element_usage:
                            element_usage[identity] = document_font_usage(document, [{'elements': [element]}])
                        element_chars = element_usage[identity].get(key, set())
                        if not element_chars:
                            continue
                        try:
                            asset.check_coverage(''.join(element_chars))
                        except ValueError as error:
                            errors.append({**details, 'error': str(error), 'pages': [page['path']], 'elementIds': [element.get('elementId')]})
                            located = True
                    if not located:
                        errors.append({**details, 'error': str(page_error), 'pages': [page['path']]})
    return {'fonts': assets, 'errors': errors}


def safe_resource(root, reference):
    if not isinstance(reference, str) or not reference or any(c in reference for c in ('\\', ':', '\0')):
        raise ValueError('Unsafe resource path')
    relative = PurePosixPath(reference)
    if relative.is_absolute() or '..' in relative.parts or str(relative) != reference or reference == '.':
        raise ValueError('Unsafe resource path')
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError('Resource escapes source directory')
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Linked resources are not permitted')
    return target


def _snapshot_state(source, revision):
    document = load_yaml(source)
    return {'document': document, 'pages': [{'path': p, 'data': load_yaml(safe_resource(source.parent, p))} for p in document['pages']], 'revision': revision, 'versions': []}


@contextmanager
def snapshot_server(source, revision=0):
    """Serve only a supplied immutable source and known runtime/font resources."""
    source = Path(source).resolve()
    state = _snapshot_state(source, revision)
    font_data = fonts_for_document(state, '/fonts')
    markup = workbench_html('/api', '/runtime').encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def send(self, status, raw, mime):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
        def json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')
        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            try:
                if route in ('/', '/index.html'):
                    return self.send(200, markup, 'text/html; charset=utf-8')
                if route == '/api/document':
                    return self.json(200, state)
                if route == '/api/fonts':
                    return self.json(200, font_data)
                if route == '/api/capabilities':
                    return self.json(200, capabilities())
                if route.startswith('/api/assets/'):
                    path = safe_resource(source.parent, route[len('/api/assets/'):])
                elif route.startswith('/runtime/'):
                    path = safe_resource(RUNTIME_DIR, route[len('/runtime/'):])
                elif route.startswith('/fonts/') and re.fullmatch(r'[a-f0-9]{64}\.(ttf|otf)', route[len('/fonts/'):]):
                    path = safe_resource(FONT_CACHE, route[len('/fonts/'):])
                else:
                    return self.json(404, {'error': 'Not found'})
                if not path.is_file():
                    return self.json(404, {'error': 'Resource not found'})
                self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            except (ValueError, OSError) as error:
                self.json(400, {'error': str(error), 'pages': [path for path, usage in page_usage.items() if (family, weight, italic) in usage]})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
