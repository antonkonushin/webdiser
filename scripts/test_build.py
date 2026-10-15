"""Integration checks for publication selection, cross-links and reproducibility."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('book_build',ROOT/'build.py')
app=importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class BuildTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.build').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='test-',dir=ROOT/'.build')
        self.root=Path(self.temp.name)
        self.web=self.root/'web'
        self.web.mkdir()
        self.source=self.root/'source'
        self.source.mkdir()
        for folder in ['templates','static']:
            shutil.copytree(ROOT/folder,self.web/folder)
        (self.web/'chapter-template').mkdir()
        shutil.copyfile(ROOT/'chapter-template/style.css',self.web/'chapter-template/style.css')
        for name in ['build.py','requirements.txt']:
            shutil.copyfile(ROOT/name,self.web/name)
        self.original=app.HERE
        app.HERE=self.web
        self.config=app.yaml.safe_load((ROOT/'content/book.yaml').read_text(encoding='utf-8'))
        self.config['pages']=[dict(id='example',number=1,title='Пример',description='Проверка',output='chapters/example.html',sections=['Глава/a.md','Глава/b.md'])]
        self.config['bibliography']='references.bib'
        self.save_config()
        app.dump(self.source/'references.bib','@book{sample,title={Example},author={Example, A.},year={2020}}\n')
        self.section('a','Текст [следующего раздела](section:b).\n\n![Схема](figures/example.svg){#fig-test}\n\n[@sample]')
        self.section('b','Обратная [ссылка](section:a).')
        app.dump(self.source/'Глава/figures/example.svg','<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>')
        app.dump(self.source/'Рабочие-заметки/private.md','PRIVATE_SENTINEL')
        app.dump(self.source/'unlisted.md','UNLISTED_SENTINEL')

    def tearDown(self):
        app.HERE=self.original
        self.temp.cleanup()

    def save_config(self):
        app.dump(self.source/'book.yaml',app.yaml.safe_dump(self.config,allow_unicode=True,sort_keys=False))

    def section(self,sid,body):
        app.dump(self.source/f'Глава/{sid}.md',f'---\nid: {sid}\ntitle: Раздел {sid}\nstatus: draft\n---\n\n{body}\n')

    def build(self,source=None,snapshot=True):
        with contextlib.redirect_stdout(io.StringIO()):
            app.build(source or self.source,snapshot)

    def hashes(self):
        return {p.relative_to(self.web/'docs').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.web/'docs').rglob('*') if p.is_file()}

    def test_selected_snapshot_and_reproducibility(self):
        self.build()
        self.assertFalse((self.web/'content/Рабочие-заметки/private.md').exists())
        self.assertFalse((self.web/'content/unlisted.md').exists())
        self.assertTrue((self.web/'content/Глава/figures/example.svg').is_file())
        text=(self.web/'docs/chapters/example.html').read_text(encoding='utf-8')
        self.assertIn('href="example.html#b"',text)
        self.assertIn('ref-sample',text)
        before=self.hashes()
        self.build(self.web/'content',False)
        self.assertEqual(before,self.hashes())

    def test_bad_reference_does_not_change_existing_output(self):
        self.build()
        before=self.hashes()
        self.section('b','[Ошибка](section:does-not-exist)')
        with self.assertRaisesRegex(ValueError,'Unknown section'):
            self.build()
        self.assertEqual(before,self.hashes())

    def test_private_resource_is_rejected(self):
        self.section('b','[Не публиковать](../Рабочие-заметки/private.md)')
        with self.assertRaisesRegex(ValueError,'Working notes'):
            self.build()
        self.assertFalse((self.web/'docs').exists())

    def test_manifest_path_cannot_escape_source(self):
        self.config['pages'][0]['sections']=['../outside.md']
        self.save_config()
        with self.assertRaisesRegex(ValueError,'escapes content root'):
            self.build()

    def test_removed_section_disappears_from_snapshot(self):
        self.build()
        self.config['pages'][0]['sections']=['Глава/a.md']
        self.save_config()
        self.section('a','Текст без второй секции.')
        self.build()
        self.assertFalse((self.web/'content/Глава/b.md').exists())
        manifest=json.loads((self.web/'docs/build-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['sections'],1)

    def test_fragment_validation(self):
        self.section('b','[Ошибка](#not-here)')
        with self.assertRaisesRegex(ValueError,'missing fragment'):
            self.build()


if __name__=='__main__':
    unittest.main(verbosity=2)
