"""Build a portable dissertation website from explicitly selected Markdown sources.

python build.py --source ../../MyDiser/Диссертация  # authoring: update snapshot
python build.py                                    # reproduce tracked snapshot
python build.py --export latex                     # non-normative text export
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlencode, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / '.build/python'))
try:
    import yaml
    import pypandoc
except ImportError as exc:
    raise SystemExit('Install dependencies: python -m pip install -r requirements.txt') from exc

PANDOC = pypandoc.get_pandoc_path()
STATUSES = {'outline': 'План раздела', 'draft': 'Черновик', 'review': 'На обсуждении', 'ready': 'Текст подготовлен'}


def dump(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes content root: {relative}')
    if 'Рабочие-заметки' in path.parts:
        raise ValueError(f'Working notes cannot be published: {relative}')
    return path


def pandoc(text, *args):
    result = subprocess.run([PANDOC, *args], input=text, text=True, encoding='utf-8',
                            capture_output=True, cwd=HERE)
    if result.returncode or result.stderr.strip():
        raise ValueError(f'Pandoc failed or reported a warning:\n{result.stderr}')
    return result.stdout


def split_source(path):
    raw = path.read_text(encoding='utf-8-sig')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z', raw, re.S)
    if not match:
        raise ValueError(f'Missing YAML metadata: {path}')
    meta = yaml.safe_load(match[1])
    if not isinstance(meta, dict) or not all(meta.get(k) for k in ['id', 'title', 'status']):
        raise ValueError(f'Missing id/title/status in {path}')
    if meta['status'] not in STATUSES:
        raise ValueError(f'Unknown text status in {path}: {meta["status"]}')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', meta['id']):
        raise ValueError(f'Use a stable ASCII section id: {path}')
    return meta, match[2]


def relurl(target, output):
    return posixpath.relpath(target, posixpath.dirname(output) or '.')


def esc(value):
    return html.escape(str(value), quote=True)


def section_count(n):
    return f'{n} ' + ('разделов' if 11<=n%100<=14 else 'раздел' if n%10==1 else 'раздела' if 2<=n%10<=4 else 'разделов')


def feedback(config, output, sid, revision, label):
    address = config['site_url'].rstrip('/') + '/' + output + (f'#{sid}' if sid else '')
    body = f'Раздел: {label}\nАдрес: {address}\nРедакция: {revision}\n\nЗамечание или предложение:\n'
    return config['repository'] + '/issues/new?' + urlencode({'title': f'Обсуждение: {label}', 'body': body})


class Inspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs, self.errors = set(), [], []
        self.h1 = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            if attrs['id'] in self.ids:
                self.errors.append(f'Duplicate id: {attrs["id"]}')
            self.ids.add(attrs['id'])
        self.h1 += tag == 'h1'
        for key in ['href', 'src']:
            if attrs.get(key):
                self.refs.append(attrs[key])
        if tag == 'img' and not attrs.get('alt'):
            self.errors.append('An image needs a nonempty alt description')

    handle_startendtag = handle_starttag


def validate_site(root):
    parsed, errors = {}, []
    for path in sorted(root.rglob('*.html')):
        parser = Inspector()
        raw = path.read_text(encoding='utf-8')
        parser.feed(raw)
        parsed[path.resolve()] = parser
        if parser.h1 != 1:
            errors.append(f'{path.name}: expected one h1, found {parser.h1}')
        errors += [f'{path.name}: {e}' for e in parser.errors]
        visible = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', raw, flags=re.S|re.I)
        for unwanted in ['@@', '\ufffd', 'C:\\Users\\', 'file://', '[[']:
            if unwanted in visible:
                errors.append(f'{path.name}: unwanted content {unwanted!r}')
    refs = 0
    for path, parser in parsed.items():
        for value in parser.refs:
            url = urlsplit(value)
            if url.scheme or url.netloc:
                if url.scheme and url.scheme not in ['https', 'http', 'mailto']:
                    errors.append(f'{path.name}: unsupported URL scheme {value}')
                continue
            refs += 1
            if url.path.startswith('/'):
                errors.append(f'{path.name}: absolute site path breaks /webdiser/: {value}')
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                errors.append(f'{path.name}: missing or escaping local target: {value}')
            elif url.fragment and target in parsed and unquote(url.fragment) not in parsed[target].ids:
                errors.append(f'{path.name}: missing fragment: {value}')
    if errors:
        raise ValueError('Site validation failed:\n' + '\n'.join(errors))
    return dict(html_pages=len(parsed), local_references=refs, broken_references=0)


def install_generated(stage, target):
    """Replace only generated files; never recursively delete an arbitrary directory."""
    if target.parent.resolve() != HERE:
        raise ValueError('Generated output must be an immediate child of the web checkout')
    previous_file = target / '.generated-files.json'
    previous = json.loads(previous_file.read_text(encoding='utf-8')) if previous_file.exists() else []
    generated = []
    for path in sorted(stage.rglob('*')):
        if path.is_file():
            relative = path.relative_to(stage).as_posix()
            destination = inside(target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            generated.append(relative)
    for relative in set(previous) - set(generated):
        stale = inside(target, relative)
        if stale.is_file():
            stale.unlink()
    dump(previous_file, json.dumps(generated, ensure_ascii=False, indent=2) + '\n')


def build(source, do_snapshot, export_format=None):
    source = source.resolve()
    config = yaml.safe_load((source / 'book.yaml').read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('Unsupported book.yaml schema')
    if not config['site_url'].startswith('https://'):
        raise ValueError('site_url must be an absolute HTTPS URL')
    sections, pages, all_ids, outputs, selected = {}, copy.deepcopy(config['pages']), set(), set(), {}
    for p in pages:
        output = p['output']
        if output in outputs or not re.fullmatch(r'(?:chapters/)?[a-z][a-z0-9-]*\.html', output):
            raise ValueError(f'Duplicate or invalid output: {output}')
        outputs.add(output)
        if p['id'] in all_ids:
            raise ValueError(f'Duplicate page id: {p["id"]}')
        all_ids.add(p['id'])
        p['loaded'] = []
        for relative in p['sections']:
            path = inside(source, relative)
            meta, body = split_source(path)
            if meta['id'] in all_ids or path in selected:
                raise ValueError(f'Duplicate section or source: {path}')
            all_ids.add(meta['id'])
            if '[[' in body:
                raise ValueError(f'Use portable Markdown links instead of wikilinks: {relative}')
            data = dict(meta=meta, body=body, path=path, relative=relative, page=p)
            p['loaded'].append(data)
            sections[meta['id']] = data
            selected[path] = data
    public_pages = {p['id']:p for p in pages}
    bibliography = inside(source, config['bibliography'])
    selected_files = {source / 'book.yaml', bibliography, *selected}
    source_hashes = {}
    assets = {}
    asts = {}
    # Read all sources before changing tracked content or HTML.
    for sid, s in sections.items():
        ast = json.loads(pandoc(s['body'], '-f', 'markdown', '-t', 'json'))

        def rewrite(node):
            if isinstance(node, list):
                for value in node:
                    rewrite(value)
            elif isinstance(node, dict):
                if node.get('t') == 'Header':
                    if node['c'][0] < 3:
                        raise ValueError(f'Use ### for subsections; section heading is generated: {s["relative"]}')
                if node.get('t') in ['Image', 'Link']:
                    url = node['c'][-1][0]
                    parts = urlsplit(url)
                    if url.startswith('section:'):
                        key = url[8:]
                        if key not in sections:
                            raise ValueError(f'Unknown section {url}')
                        dest = sections[key]['page']['output']
                        node['c'][-1][0] = relurl(dest, s['page']['output']) + '#' + key
                    elif url.startswith('page:'):
                        key = url[5:]
                        dest = public_pages[key]['output'] if key in public_pages else 'bibliography.html' if key == 'bibliography' else None
                        if dest is None:
                            raise ValueError(f'Unknown page {url}')
                        node['c'][-1][0] = relurl(dest, s['page']['output'])
                    elif not parts.scheme and not parts.netloc and parts.path:
                        local = (s['path'].parent / unquote(parts.path)).resolve()
                        inside(source, local.relative_to(source))
                        if local in selected:
                            dest = selected[local]
                            node['c'][-1][0] = relurl(dest['page']['output'], s['page']['output']) + '#' + (parts.fragment or dest['meta']['id'])
                        else:
                            if local.suffix.lower() in ['.md', '.yaml', '.yml', '.bib']:
                                raise ValueError(f'Unlisted manuscript source cannot be published: {local}')
                            if not local.is_file():
                                raise ValueError(f'Missing resource: {local}')
                            digest = hashlib.sha256(local.read_bytes()).hexdigest()[:12]
                            name = re.sub(r'[^a-zA-Z0-9_.-]', '-', local.name)
                            dest = f'assets/media/{digest}-{name}'
                            assets[dest] = local
                            selected_files.add(local)
                            node['c'][-1][0] = relurl(dest, s['page']['output']) + (f'#{parts.fragment}' if parts.fragment else '')
                for value in node.values():
                    rewrite(value)
        rewrite(ast)
        asts[sid] = ast

    # Hashes describe source content, not a clock reading; unchanged input is reproducible.
    for path in sorted(selected_files):
        source_hashes[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    build_inputs = {}
    for folder in ['templates', 'static']:
        for path in sorted((HERE / folder).rglob('*')):
            if path.is_file():
                build_inputs[path.relative_to(HERE).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ['build.py', 'requirements.txt', 'chapter-template/style.css']:
        build_inputs[name] = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    revision = hashlib.sha256(json.dumps([source_hashes, build_inputs], sort_keys=True).encode()).hexdigest()[:10]

    def booknav(current, prefix):
        links = [f'<a href="{prefix}index.html"' + (' aria-current="page"' if current=='index' else '') + '>Об этой работе</a>']
        for p in pages:
            if p['id'] in ['conclusion', 'abstract-plan']:
                links.append('<div class="nav-separator"></div>')
            label = f'{p["number"]:02d}' if p.get('number') else '·'
            links.append(f'<a href="{prefix}{p["output"]}"' + (' aria-current="page"' if current==p['id'] else '')
                         + f'><span class="nav-num">{label}</span><span>{esc(p["title"])}</span></a>')
        links.append(f'<a href="{prefix}bibliography.html"' + (' aria-current="page"' if current=='bibliography' else '') + '><span class="nav-num">·</span><span>Библиография</span></a>')
        return '\n'.join(links)

    template = (HERE / 'templates/page.html').read_text(encoding='utf-8')
    def wrap(output, current, heading, description, body, toc='', eyebrow='', pagemeta='', prevnext='', layout=''):
        prefix = '../' if output.startswith('chapters/') else ''
        values = dict(TITLE=esc(heading), HEADING=esc(heading), DESCRIPTION=esc(description), PREFIX=prefix,
                      LAYOUT=layout, BOOKNAV=booknav(current,prefix), UPDATED=esc(config['updated']), REVISION=revision,
                      EYEBROW=esc(eyebrow), BODY=body, PAGEMETA=pagemeta, PREVNEXT=prevnext,
                      LOCALTOC=f'<aside class="local-toc" aria-label="Разделы страницы"><div class="nav-label">НА ЭТОЙ СТРАНИЦЕ</div>{toc}</aside>' if toc else '')
        result = template
        for key,value in values.items():
            result = result.replace(f'@@{key}@@', value)
        return '\n'.join(line.rstrip() for line in result.splitlines()) + '\n'

    (HERE / '.build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='site-',dir=HERE / '.build') as temporary:
        stage = Path(temporary) / 'docs'
        stage.mkdir()
        # Preserve template typography and palette; changes live in an explicit extension.
        css = (HERE / 'chapter-template/style.css').read_text(encoding='utf-8') + '\n' + (HERE / 'static/custom.css').read_text(encoding='utf-8')
        dump(stage / 'assets/style.css', css)
        for name in ['site.js', 'favicon.svg']:
            dump(stage / f'assets/{name}', (HERE / f'static/{name}').read_text(encoding='utf-8'))
        for dest,path in assets.items():
            target = stage / dest
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
        export_blocks = []
        for pi,p in enumerate(pages):
            ast = {'pandoc-api-version': next(iter(asts.values()))['pandoc-api-version'], 'meta':{}, 'blocks':[]}
            toc = []
            for i,s in enumerate(p['loaded'],start=1):
                sid,meta = s['meta']['id'],s['meta']
                label = f'{p["number"]}.{i}. {meta["title"]}' if p.get('number') else meta['title']
                badge = f'<span class="badge {meta["status"]}">{STATUSES[meta["status"]]}</span>'
                heading = f'<div class="section-topline"><h2 id="{sid}-heading">{esc(label)}</h2>{badge}</div>'
                discussion = feedback(config,p['output'],sid,revision,label)
                tools = f'<div class="section-tools"><a href="#{sid}">§ Ссылка на раздел</a><a href="{esc(discussion)}">Обсудить раздел ↗</a></div>'
                blocks = [{'t':'RawBlock','c':['html',heading]}, *asts[sid]['blocks'], {'t':'RawBlock','c':['html',tools]}]
                ast['blocks'].append({'t':'Div','c':[[sid,['manuscript-section'],[]],blocks]})
                toc.append(f'<a href="#{sid}">{esc(label)}</a>')
            body = pandoc(json.dumps(ast,ensure_ascii=False),'-f','json','-t','html5','--mathjax','--citeproc',
                          '--bibliography',str(bibliography),'-M','lang=ru-RU','-M','reference-section-title=Литература к странице')
            body = re.sub(r'<(/?)h1(?=[ >])', r'<\1h2', body)
            body = re.sub(r'(<table(?:\s[^>]*)?>.*?</table>)',r'<div class="scrollx">\1</div>',body,flags=re.S)
            heading = f'Глава {p["number"]}. {p["title"]}' if p.get('number') else p['title']
            feedback_url = feedback(config,p['output'],'',revision,heading)
            meta = f'<div class="page-meta"><span class="badge">Рабочая редакция</span><span>{section_count(len(p["loaded"]))}</span><a href="{esc(feedback_url)}">Обсудить страницу ↗</a></div>'
            navigation = []
            for index,kind,arrow in [(pi-1,'prev','←'),(pi+1,'next','→')]:
                if 0<=index<len(pages):
                    other = pages[index]
                    caption = 'Предыдущая страница' if kind=='prev' else 'Следующая страница'
                    navigation.append(f'<a class="{kind}" href="{relurl(other["output"],p["output"])}"><small>{caption}</small>{arrow} {esc(other["title"])}</a>')
            rendered = wrap(p['output'],p['id'],heading,p['description'],body,''.join(toc),
                            'ДОКТОРСКАЯ ДИССЕРТАЦИЯ · РАБОЧИЙ ТЕКСТ',meta,'<div class="prev-next">'+''.join(navigation)+'</div>')
            dump(stage / p['output'],rendered)

        chapters = [p for p in pages if p.get('number')]
        cards = []
        for p in chapters:
            cards.append(f'<a class="chapter-card" href="{p["output"]}"><div class="card-top"><span>ГЛАВА {p["number"]:02d}</span><span class="badge">Черновик</span></div><h3>{esc(p["title"])}</h3><p>{esc(p["description"])}</p><div class="card-bottom"><span>{section_count(len(p["loaded"]))}</span><span>Читать главу →</span></div></a>')
        reading_links = ''.join(f'<a href="{public_pages[key]["output"]}">{text}</a>' for key,text in
                              [('introduction','Начать с введения →'),('conclusion','Карта итогов')] if key in public_pages)
        body = f'''<div class="home-intro"><p>От изображений и геометрических наблюдений — к объектам, структуре сцены и редактируемым моделям. Электронный текст для последовательного чтения и обсуждения.</p></div>
<div class="home-stats"><div><b>{len(chapters):02d}</b>содержательных глав</div><div><b>{len(sections):02d}</b>{section_count(len(sections)).split(' ',1)[1]} текста</div><div><b>01</b>рабочая версия</div></div>
<div class="reading-links">{reading_links}<a href="bibliography.html">Библиография</a></div>
<div class="contents-heading"><h2>Содержание исследования</h2><label class="search-label"><span class="visually-hidden">Найти главу</span><input class="search-input" type="search" id="chapter-search" placeholder="Найти главу…" aria-label="Найти главу"></label></div>
<div class="chapter-grid">{''.join(cards)}</div><p id="search-empty" hidden>По этому запросу главы не найдены.</p>
<div class="discussion-note"><h2>Открыто для обсуждения</h2><p>Это первый черновик. В тексте разделены постановки, планы и результаты, которые ещё требуется подтвердить. У каждого раздела есть постоянная ссылка и кнопка обсуждения через GitHub. Особенно полезны замечания о связности работы, научном ядре и программе экспериментов.</p></div>'''
        dump(stage / 'index.html',wrap('index.html','index',config['title'],config['full_title'],body,
                                      eyebrow='АНТОН КОНУШИН / ДОКТОРСКАЯ ДИССЕРТАЦИЯ',layout='home-layout'))
        bibdoc = '---\nnocite: |\n  @*\n---\n\n'
        bibbody = pandoc(bibdoc,'-f','markdown','-t','html5','--citeproc','--bibliography',str(bibliography),'-M','lang=ru-RU')
        intro = '<div class="discussion-note"><p>Записи перенесены из локального библиографического каталога. Версии и редакционные статусы требуют сверки. Библиографическая запись не является подтверждением опубликованной журнальной версии; оформление пока не нормативное.</p></div>'
        dump(stage / 'bibliography.html',wrap('bibliography.html','bibliography','Библиография',
             'Источники исследования: рабочий каталог публикаций и препринтов.',intro+bibbody,eyebrow='ИСТОЧНИКИ И ВЕРСИИ'))
        dump(stage / '.nojekyll','')
        report = validate_site(stage)
        manifest = dict(schema_version=1, revision=revision, source_files=source_hashes, build_inputs=build_inputs,
                        pandoc_version=str(pypandoc.get_pandoc_version()), **report,
                        sections=len(sections),chapters=len(chapters),publication_status='draft')
        dump(stage / 'build-manifest.json',json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
        if do_snapshot:
            snapshot = Path(temporary) / 'content'
            snapshot.mkdir()
            for path in selected_files:
                target = snapshot / path.relative_to(source)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,target)
            install_generated(snapshot,HERE / 'content')
        install_generated(stage,HERE / 'docs')
        if export_format:
            # Export through the same parser, retaining logical headings and original resource paths.
            whole = []
            for p in pages:
                whole.append(f'# {p["title"]} {{#{p["id"]}}}\n')
                for s in p['loaded']:
                    text=s['body']
                    text=re.sub(r'\]\((section:)([^)]+)\)',r'](#\2)',text)
                    text=re.sub(r'\]\((page:)([^)]+)\)',r'](#\2)',text)
                    def absolute_resource(m):
                        if urlsplit(m[1]).scheme or m[1].startswith('#'):
                            return m[0]
                        local=(s['path'].parent/unquote(m[1])).resolve()
                        return '](' + local.as_posix() + ')'
                    text=re.sub(r'\]\(([^)]+)\)',absolute_resource,text)
                    whole.append(f'## {s["meta"]["title"]} {{#{s["meta"]["id"]}}}\n\n{text}')
            md='\n\n'.join(whole)
            destination=HERE / '.build/exports' / ('dissertation.tex' if export_format=='latex' else 'dissertation.docx')
            destination.parent.mkdir(parents=True,exist_ok=True)
            pandoc(md,'-f','markdown','-t',export_format,'--standalone','--citeproc','--bibliography',str(bibliography),
                   '-M','lang=ru-RU','-M',f'title={config["full_title"]}','-M',f'author={config["author"]}','-o',str(destination))
            print('Non-normative export:',destination)
    print(json.dumps(dict(revision=revision,sections=len(sections),**report),ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,help='Canonical authoring directory; omit to build the Git snapshot')
    parser.add_argument('--check',action='store_true',help='Validate existing docs without rebuilding')
    parser.add_argument('--export',choices=['latex','docx'],dest='export_format')
    args=parser.parse_args()
    if args.check:
        print(json.dumps(validate_site(HERE / 'docs')))
    else:
        build(args.source or HERE / 'content',bool(args.source),args.export_format)


if __name__=='__main__':
    try:
        main()
    except (ValueError,KeyError,FileNotFoundError) as error:
        raise SystemExit(str(error))
