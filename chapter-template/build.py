# -*- coding: utf-8 -*-
"""Собирает из глав-markdown читаемый HTML в формате учебника.

Картинки уменьшаются и складываются в docs/img, формулы рендерит MathJax,
подписи вида «*Рис. N. ...*» под картинкой превращаются в figcaption,
ссылки вида «§ 1.4.6» в тексте становятся гиперссылками на разделы.
Раздел чужой книги или статьи пишут «§&nbsp;5.3» — такой номер ссылкой не становится.
"""
import hashlib
import html as htmllib
import json
import os
import re
import shutil

import markdown
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'docs')
IMG = os.path.join(OUT, 'img')
MAX_W = 1600
# краткие справки к ссылкам на имена, места и события: url → {"tip": ...}; всплывают при наведении
GLOSSARY = os.path.join(HERE, 'glossary_rus.json')

CONFIG = json.load(open(os.path.join(HERE, 'book.json'), encoding='utf-8'))
CHAPTERS = [(c['output'], c['source'], c['title']) for c in CONFIG['chapters']]


CSS = open(os.path.join(HERE, 'style.css'), encoding='utf-8').read()

MATHJAX = """
<script>
window.MathJax={tex:{inlineMath:[['$','$']],displayMath:[['$$','$$']],
  processEscapes:true},options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/3.2.2/es5/tex-mml-chtml.min.js"></script>
"""


def prepare_images(md, chapter_slug):
    """Уменьшить картинки, положить в docs/img, переписать пути."""
    os.makedirs(IMG, exist_ok=True)
    seen = {}

    def repl(m):
        alt, src = m.group(1), m.group(2)
        if not os.path.isabs(src):
            src = os.path.join(HERE, src)
        if src in seen:
            return f'![{alt}]({seen[src]})'
        if not os.path.exists(src):
            raise FileNotFoundError(f'Не найден рисунок: {src}')
        name = f'{chapter_slug}_{os.path.basename(src)}'
        dst = os.path.join(IMG, name)
        im = Image.open(src)
        if im.mode in ('RGBA', 'LA', 'P'):        # прозрачный фон иначе станет чёрным
            im = im.convert('RGBA')
            im = Image.alpha_composite(Image.new('RGBA', im.size, (255, 255, 255, 255)), im)
        if im.width > MAX_W:
            im = im.resize((MAX_W, round(im.height * MAX_W / im.width)), Image.LANCZOS)
        im.convert('RGB').save(dst, optimize=True)
        # версия по содержимому: иначе браузер и CDN до 10 минут показывают старую картинку
        rel = f'img/{name}?v={hashlib.md5(open(dst, "rb").read()).hexdigest()[:8]}'
        seen[src] = rel
        return f'![{alt}]({rel})'

    return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', repl, md)


def fold_captions(html):
    """«<p><img></p><p><em>Рис. …</em></p>» -> figure + figcaption."""
    pat = re.compile(
        r'<p>\s*(<img[^>]*>)\s*</p>\s*<p>\s*<em>(Рис\.(?:(?!</em>).)*)</em>\s*</p>',
        re.S)
    html = pat.sub(lambda m: f'<figure>{zoomable(m.group(1))}'
                             f'<figcaption>{m.group(2)}</figcaption></figure>', html)
    # картинка без подписи
    html = re.sub(r'<p>\s*(<img[^>]*>)\s*</p>', lambda m: f'<figure>{zoomable(m.group(1))}</figure>', html)
    # таблицы и выключные формулы прокручиваются сами, а не тянут страницу
    html = re.sub(r'(<table>.*?</table>)', r'<div class="scrollx">\1</div>', html, flags=re.S)
    return html


def zoomable(img_tag):
    """Картинка открывается в полном размере по клику."""
    m = re.search(r'src="([^"]+)"', img_tag)
    return f'<a class="zoom" href="{m.group(1)}" target="_blank" rel="noopener">{img_tag}</a>' if m else img_tag


# Внутристрочная формула может быть перенесена на следующую строку, но не через
# пустую строку: иначе шаблон склеит два разных абзаца.
MATH_RE = re.compile(
    r'(\$\$.*?\$\$'
    r'|(?<!\$)\$(?!\s)(?:[^\$\n]|\n(?!\s*\n))*?(?<!\s)\$(?!\$))', re.S)


def protect_math(md):
    """Вынуть формулы до markdown: иначе он ест подчёркивания и обратные слэши."""
    store = []

    def stash(m):
        store.append(m.group(0))
        return f'\x00MATH{len(store)-1}\x00'

    return MATH_RE.sub(stash, md), store


def restore_math(html, store):
    """Возвращаем формулы в HTML. Знаки < и > экранируем: иначе «$v<c_y$» парсится как тег."""
    for i, tex in enumerate(store):
        tex = tex.replace('<', '&lt;').replace('>', '&gt;')
        html = html.replace(f'\x00MATH{i}\x00', tex)
    return html


# Ненумерованные заголовки, на которые ссылаются: id не зависит от их порядкового номера.
FIXED_IDS = {
    'Шпаргалка': 'sec-cheatsheet',
    'Задачи для самопроверки': 'sec-exercises',
    'Литература': 'sec-literature',
}


def build_toc(md):
    items = []
    for line in md.split('\n'):
        m = re.match(r'^(#{2,3})\s+(.*?)\s*$', line)
        if not m:
            continue
        level = len(m.group(1))
        title = re.sub(r'[*`$\\]', '', m.group(2)).strip()
        anchor = 'sec-' + re.sub(r'[^a-z0-9]+', '-',
                                 title.lower().replace('ё', 'е')).strip('-')
        used = {a for _, _, a in items}
        if title in FIXED_IDS and FIXED_IDS[title] not in used:
            anchor = FIXED_IDS[title]
        # у ненумерованных кириллических заголовков slug пустой, и они схлопываются
        # в один и тот же id; добавляем порядковый номер, чтобы ссылки не путались
        elif anchor == 'sec-' or anchor in used:
            anchor = f'sec-{len(items) + 1}' if anchor == 'sec-' else f'{anchor}-{len(items) + 1}'
        items.append((level, title, anchor))
    return items


def anchorize(html, items):
    """Проставить id заголовкам в том же порядке, в каком они шли в markdown."""
    it = iter(items)

    def repl(m):
        try:
            _, _, anchor = next(it)
        except StopIteration:
            return m.group(0)
        return f'<{m.group(1)} id="{anchor}">'

    return re.sub(r'<(h[23])>', repl, html)


def section_anchors(items):
    """Номер раздела («1.4.6») -> id, который build_toc дал его заголовку."""
    out = {}
    for _, title, anchor in items:
        m = re.match(r'(\d+(?:\.\d+)+)\.?(?:\s|$)', title)
        if m:
            out.setdefault(m.group(1), anchor)
    return out


SEC_NUM = r'\d+(?:\.\d+)+'
SEC_SEP = r'(?:\s*[–—-]\s*|,\s*|\s+(?:и|или)\s+)'
# «§ 1.4.6», «§§ 2.3.7 и 2.4», «§ 1.4.8–1.4.10», «§ 1.4.5–1.4.6, 1.4.8, 1.5»
SEC_REF_RE = re.compile(rf'(§§?)(\s*)({SEC_NUM})((?:{SEC_SEP}{SEC_NUM})*)')
SEC_PART_RE = re.compile(rf'({SEC_SEP})({SEC_NUM})')
# «Tewari et al., STAR 2022, § 3.1» — раздел чужой статьи, а не главы
CITATION_TAIL_RE = re.compile(r'\b(?:19|20)\d\d,\s*$')
# внутри этих элементов ссылки не ставим
NO_LINK_TAGS = {'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'code', 'pre', 'script', 'style', 'nav'}
BROKEN = []    # (глава-markdown, строки, номер, причина) — заполняет link_sections


def link_sections(html, chapter, xref, md, md_name):
    """«§ N.M.K» в тексте -> <a href="[chapterNN.html]#sec-…">.

    Вызывается до restore_math: формулы ещё вынуты, и § внутри TeX не тронется.
    Каждое число диапазона и перечисления ведёт на свой раздел.
    """
    md_refs = [(md.count('\n', 0, m.start()) + 1, re.findall(SEC_NUM, m.group(0)))
               for m in SEC_REF_RE.finditer(md)]
    n = 0

    def target(num):
        ch = int(num.split('.')[0])
        if ch not in xref:
            return None, f'главы {ch} нет в сборке'
        out_name, anchors = xref[ch]
        if num not in anchors:
            return None, 'нет заголовка с таким номером'
        return (f'#{anchors[num]}' if ch == chapter else f'{out_name}#{anchors[num]}'), None

    def link(text, num):
        nonlocal n
        href, why = target(num)
        if href is None:
            lines = sorted({ln for ln, nums in md_refs if num in nums})
            BROKEN.append((md_name, lines, num, why))
            print(f'  ! {md_name}:{",".join(map(str, lines)) or "?"}: § {num} — {why}, ссылка не поставлена')
            return text
        n += 1
        return f'<a href="{href}">{text}</a>'

    def repl_text(text):
        def repl(m):
            if CITATION_TAIL_RE.search(text[:m.start()]):
                print(f'  - {md_name}: «{m.group(0)}» — раздел внешнего источника, пропущено')
                return m.group(0)
            out = link(m.group(1) + m.group(2) + m.group(3), m.group(3))
            for p in SEC_PART_RE.finditer(m.group(4)):
                out += p.group(1) + link(p.group(2), p.group(2))
            return out
        return SEC_REF_RE.sub(repl, text)

    parts = re.split(r'(<!--.*?-->|<[^>]*>)', html, flags=re.S)
    depth = 0
    for i, part in enumerate(parts):
        if i % 2:
            m = re.match(r'<(/?)([a-zA-Z][a-zA-Z0-9]*)', part)
            if m and m.group(2).lower() in NO_LINK_TAGS and not part.endswith('/>'):
                depth += -1 if m.group(1) else 1
                depth = max(depth, 0)
        elif depth == 0 and '§' in part:
            parts[i] = repl_text(part)
    return ''.join(parts), n


def chapter_number(out_name):
    return int(re.search(r'(\d+)', out_name).group(1))


def collect_sections():
    """Номера разделов всех глав: ссылка из главы 1 может вести в главу 2."""
    xref = {}
    for out_name, md_name, _ in CHAPTERS:
        md = open(os.path.join(HERE, md_name), encoding='utf-8').read()
        xref[chapter_number(out_name)] = (out_name, section_anchors(build_toc(md)))
    return xref


def load_glossary():
    if not os.path.exists(GLOSSARY):
        return {}
    return json.load(open(GLOSSARY, encoding='utf-8'))


def gloss_links(html, glossary):
    """Ссылкам на статьи из словаря — класс gloss и текст справки для всплывающей подсказки."""
    n = 0

    def repl(m):
        nonlocal n
        href = htmllib.unescape(m.group(1))
        item = glossary.get(href)
        if not item:
            return m.group(0)
        n += 1
        tip = htmllib.escape(item['tip'], quote=True)
        return f'<a class="gloss" data-tip="{tip}" href="{m.group(1)}"'
    html = re.sub(r'<a href="([^"]+)"', repl, html)
    return html, n



# в подсказке не должно быть сырого TeX: разворачиваем частые команды в юникод
TEX_PLAIN = [(r'\\mathbb\s*R', 'ℝ'), (r'\\mathbf\s*', ''), (r'\\mathcal\s*', ''), (r'\\operatorname', ''),
             (r'\\tilde', ''), (r'\\hat', ''), (r'\\top', 'ᵀ'), (r'\\times', '×'), (r'\\cdot', '·'),
             (r'\\approx', '≈'), (r'\\in\b', '∈'), (r'\\to\b', '→'), (r'\\pm', '±'), (r'\\sqrt', '√'),
             (r'\\le\b', '≤'), (r'\\ge\b', '≥'), (r'\\ne\b', '≠'), (r'\\gamma', 'γ'), (r'\\eta', 'η'),
             (r'\\pi', 'π'), (r'\\sigma', 'σ'), (r'\\lambda', 'λ'), (r'\\rho', 'ρ'), (r'\\delta', 'δ'),
             (r'\\theta', 'θ'), (r'\\omega', 'ω'), (r'\\alpha', 'α'), (r'\\beta', 'β'), (r'\\varphi', 'φ'),
             (r'\\[,;:!]', ' '), (r'\\left|\\right', ''), (r'\\\|', '‖'), (r'\\_', '_'),
             (r'[{}]', ''), (r'\\[a-zA-Z]+', ''), (r'\\+', ''), (r'\s+', ' ')]


def tex_to_plain(t):
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)      # markdown-ссылки — только текстом
    t = t.replace('`', '')
    for pat, rep in TEX_PLAIN:
        t = re.sub(pat, rep, t)
    return t.strip(' ,;')

SYM_SKIP = {'', '-', '—'}


def notation_symbols(md):
    """Словарь «формула → смысл» из таблицы раздела «Обозначения» в конце главы."""
    m = re.search(r'\n## Обозначения\b(.*)$', md, re.S)
    if not m:
        return {}
    syms = {}
    for row in re.findall(r'^\|(.+)\|\s*$', m.group(1), re.M):
        cells = [c.strip() for c in row.split('|')]
        if len(cells) < 2 or cells[0].startswith('---') or cells[0] == 'Обозначение':
            continue
        tip = tex_to_plain(cells[1].replace('$', ''))
        if not tip or tip in SYM_SKIP:
            continue
        for tex in re.findall(r'\$([^$]+)\$', cells[0]):
            keys = {tex.strip()}
            for sep in ('=', ':', r'\\in'):
                parts = re.split(sep, tex, maxsplit=1)
                if len(parts) == 2 and parts[0].strip():
                    keys.add(parts[0].strip())
            for k in keys:
                k = k.strip().rstrip(',')
                if 0 < len(k) <= 28 and k not in SYM_SKIP and not k.isdigit():
                    syms.setdefault(re.sub(r'\s+', '', k), tip)
    return syms


def mark_symbols(html, syms, anchor):
    """Первое в каждом разделе ## упоминание обозначения — ссылка на раздел «Обозначения» с подсказкой."""
    if not syms:
        return html, 0
    used, n = set(), 0
    out, pos = [], 0
    for m in re.finditer(r'<h2[ >]|\$\$.*?\$\$|\$[^$\n]+\$', html, re.S):
        token = m.group(0)
        out.append(html[pos:m.start()]); pos = m.end()
        if token.startswith('<h2'):
            if 'id="%s"' % anchor in html[m.start():m.start() + 200]:
                out.append(html[m.start():]); return ''.join(out), n
            used = set(); out.append(token); continue
        if token.startswith('$$'):
            out.append(token); continue
        key = re.sub(r'\s+', '', token[1:-1])
        tip = syms.get(key)
        if tip is None or key in used or '<' in token or '&' in token:
            out.append(token); continue
        used.add(key); n += 1
        out.append('<a class="sym" href="#%s" data-tip="%s">%s</a>'
                   % (anchor, htmllib.escape(tip, quote=True), token))
    out.append(html[pos:])
    return ''.join(out), n

def render(md_path, title, chapter=None, xref=None):
    slug = os.path.splitext(os.path.basename(md_path))[0].replace('_chapter_rus', '')
    md = open(md_path, encoding='utf-8').read()
    md = prepare_images(md, slug)
    items = build_toc(md)

    safe, store = protect_math(md)
    body = markdown.markdown(
        safe, extensions=['tables', 'sane_lists', 'smarty', 'fenced_code'],
        extension_configs={'smarty': {'smart_dashes': False}})
    # подписи сворачиваем до ссылок: шаблон подписи не пускает теги внутрь <em>;
    # ссылки ставим до возврата формул, чтобы не искать § внутри TeX
    body = fold_captions(body)
    n_links = 0
    if xref is not None:
        body, n_links = link_sections(body, chapter, xref, md, os.path.basename(md_path))
    body = restore_math(body, store)
    body = anchorize(body, items)
    body, render.n_gloss = gloss_links(body, load_glossary())
    syms = notation_symbols(md)
    sym_anchor = next((anc for lvl, t, anc in items if t.startswith('Обозначения')), None)
    body, render.n_sym = mark_symbols(body, syms, sym_anchor) if sym_anchor else (body, 0)
    # первый h1 убираем из потока, он уходит в шапку
    body = re.sub(r'^<h1>.*?</h1>', '', body, count=1, flags=re.S)

    toc = '\n'.join(
        f'<a class="h{lvl}" href="#{a}">{t}</a>' for lvl, t, a in items)

    render.n_links = n_links
    return f"""<!doctype html>
<html lang="ru">
<title>{htmllib.escape(title)}</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style>
{MATHJAX}
<div class="wrap">
<nav><a class="home" href="index.html">← к оглавлению</a>{toc}</nav>
<main>
<div class="course">{htmllib.escape(CONFIG['subtitle'])}</div>
<h1>{htmllib.escape(title)}</h1>
{body}
</main>
</div>
"""


INDEX = """<!doctype html>
<html lang="ru">
<title>{book_title}</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{css}
.wrap{{grid-template-columns:minmax(0,760px)}}
.card{{display:block;padding:22px 24px;border:1px solid var(--line);border-radius:10px;
  margin-bottom:16px;text-decoration:none;color:inherit}}
.card:hover{{border-color:var(--blue);background:var(--soft)}}
.card b{{display:block;font:600 20px/1.3 system-ui,sans-serif;color:var(--navy);margin-bottom:6px}}
.card span{{color:var(--muted);font:15px/1.5 system-ui,sans-serif}}
</style>
<div class="wrap"><main>
<div class="course">{subtitle}</div>
<h1>{book_title}</h1>
<p class="lead">{description}</p>
{cards}
</main></div>
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(IMG, exist_ok=True)
    xref = collect_sections()
    cards = []
    for out_name, md_name, title in CHAPTERS:
        html = render(os.path.join(HERE, md_name), title, chapter_number(out_name), xref)
        open(os.path.join(OUT, out_name), 'w', encoding='utf-8').write(html)
        n_fig = html.count('<figure>')
        n_sec = html.count('<h2 ') + html.count('<h3 ')
        print(f'{out_name}: {len(html)/1000:.0f} КБ, разделов {n_sec}, рисунков {n_fig}, '
              f'ссылок на разделы {render.n_links}, справок {render.n_gloss}, обозначений {render.n_sym}')
        head = title.split('. ', 1)
        cards.append(f'<a class="card" href="{out_name}"><b>{head[0]}</b>'
                     f'<span>{head[1] if len(head) > 1 else ""}</span></a>')
    open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8').write(
        INDEX.format(css=CSS, cards='\n'.join(cards), book_title=htmllib.escape(CONFIG['title']), subtitle=htmllib.escape(CONFIG['subtitle']), description=htmllib.escape(CONFIG['description'])))
    total = sum(os.path.getsize(os.path.join(IMG, f)) for f in os.listdir(IMG))
    print(f'index.html готов; картинок {len(os.listdir(IMG))}, {total/1e6:.1f} МБ')
    if BROKEN:
        raise SystemExit(f'Сборка содержит ссылки без цели: {len(BROKEN)}. Исправьте номера разделов.')


if __name__ == '__main__':
    main()
