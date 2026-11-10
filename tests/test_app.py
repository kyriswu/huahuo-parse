"""Contract regressions. Legacy 32-test baseline passed before the API migration."""
import io
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

os.environ['PRELOAD_MODEL'] = 'false'
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import fitz
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

import app.main as main
from app import parsing, vision
from app.result import DocumentResult
from app.storage import LocalStorageBackend
from app.vision import engine, rewrite_images


def snapshot(text='recognized', label='text', bbox=None, width=200, height=200):
    return {'res': {'input_path': 'document', 'page_index': 0, 'page_count': 1,
                    'width': width, 'height': height, 'layout_det_res': {},
                    'model_settings': {'use_layout_detection': True, 'use_doc_preprocessor': False,
                                       'use_ocr_for_image_block': True, 'use_chart_recognition': True,
                                       'use_seal_recognition': True, 'markdown_ignore_labels': []},
                    'parsing_res_list': [{'block_label': label, 'block_content': text,
                                          'block_bbox': bbox or [10, 10, 180, 180]}]}}


def official_restructure(pages):
    from paddlex.inference.pipelines.paddleocr_vl.pipeline import _PaddleOCRVLPipeline
    pipeline = _PaddleOCRVLPipeline.__new__(_PaddleOCRVLPipeline)
    return list(pipeline.restructure_pages(pages, merge_tables=True, relevel_titles=True))


@contextmanager
def server_for(handler):
    server = HTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(main, 'validate_remote_url'), patch.object(main, '_validate_connected_peer'), patch.object(main, '_resolved_addresses', return_value=['127.0.0.1']):
            yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'STORAGE_LOCAL_ROOT': str(self.root / 'storage'), 'PRELOAD_MODEL': 'false'})
        self.env.start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def result(self):
        return DocumentResult('test', LocalStorageBackend(str(self.root / 'storage')))

    def test_requires_both_inputs_and_rejects_old_strategy_flags(self):
        with patch.object(main, '_download_document') as download:
            for payload in ({'file_url': 'https://example.com/a.pdf'}, {'file_type': 'pdf'},
                            {'url': 'https://example.com/a.pdf'},
                            {'file_url': 'https://example.com/a.pdf', 'file_type': 'pdf', 'max_pages': 1}):
                self.assertEqual(self.client.post('/api/v1/parse', json=payload).status_code, 422)
            download.assert_not_called()

    def test_rejects_ssrf_targets_and_non_http(self):
        for url in ('http://127.0.0.1/a', 'http://169.254.169.254/a', 'http://100.64.0.1/a', 'http://[::1]/a', 'file:///etc/passwd'):
            with self.subTest(url=url), self.assertRaises(main.URLRejected):
                main.validate_remote_url(url)

    def test_signed_path_query_and_declared_type_are_preserved(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                self.wfile.write(b'hello signed request')
            def log_message(self, *_): pass
        signed = '/r%2Ffoo/%7E?Signature=a%2Fb%2Bc%3D&Expires=42&name=two+words'
        with server_for(Handler) as base:
            path, _, ext = main._download_document(base + signed, 'txt')
            self.assertEqual(Path(path).read_bytes(), b'hello signed request')
            Path(path).unlink()
        self.assertEqual(seen, [signed])
        self.assertEqual(ext, '.txt')

    def test_stream_size_limit_and_cleanup(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'1234567890')
            def log_message(self, *_): pass
        with server_for(Handler) as base, patch.object(main, 'MAX_FILE_SIZE', 4), patch.object(main.tempfile, 'tempdir', str(self.root)):
            with self.assertRaises(main.DownloadTooLarge): main._download_document(base, 'txt')
        self.assertFalse(list(self.root.glob('huahuo-download-*')))

    def test_stream_failures_keep_gateway_error_and_cleanup(self):
        for failure, expected in [('timeout', 504), ('truncated', 502)]:
            release = threading.Event()
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.send_header('Content-Length', '100'); self.end_headers()
                    self.wfile.write(b'%PDF-'); self.wfile.flush()
                    if failure == 'timeout': release.wait(timeout=1)
                    self.close_connection = True
                def log_message(self, *_): pass
            try:
                with server_for(Handler) as base, patch.object(main, 'DOWNLOAD_TIMEOUT', 0.1), patch.object(main.tempfile, 'tempdir', str(self.root)):
                    response = self.client.post('/api/v1/parse', json={'file_url': base, 'file_type': 'pdf'})
                    self.assertEqual(response.status_code, expected, response.text)
                    self.assertFalse(list(self.root.glob('huahuo-download-*')))
            finally: release.set()

    def test_expired_link_has_safe_error(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.send_response(403); self.end_headers()
            def log_message(self, *_): pass
        with server_for(Handler) as base:
            response = self.client.post('/api/v1/parse', json={'file_url': base + '/a?Signature=SECRET', 'file_type': 'pdf'})
        self.assertEqual(response.status_code, 502)
        self.assertIn('expired', response.json()['detail'])
        self.assertNotIn('SECRET', response.text)

    def test_type_mismatch_checks_office_container_not_just_zip_signature(self):
        from docx import Document
        path = self.root / 'document.docx'
        Document().save(path)
        main._check_signature(str(path), '.docx')
        for declared in ('.xlsx', '.pptx', '.pdf', '.png'):
            with self.assertRaises(main.UnsupportedDocument): main._check_signature(str(path), declared)

    def test_all_101_pdf_pages_are_consumed_and_rendered_files_released(self):
        path = self.root / '101.pdf'
        with fitz.open() as pdf:
            for _ in range(101): pdf.new_page(width=100, height=100)
            pdf.save(path)
        calls = []
        def page(image_path, location, result):
            self.assertTrue(Path(image_path).is_file())
            calls.append((image_path, location['page_number']))
            return snapshot(f"PAGE {location['page_number']}"), {}
        with patch.object(engine, 'page', side_effect=page), patch.object(engine, 'restructure', side_effect=official_restructure):
            result = main._parse_document(str(path), path.name, '.pdf')
        self.assertEqual(len(calls), 101)
        self.assertEqual(result['metadata']['parsed_page_count'], 101)
        self.assertIn('## 第 101 页', result['markdown'])
        self.assertTrue(all(not Path(file).exists() for file, _ in calls))
        self.assertEqual(result['metadata']['pages'][100]['blocks'][0]['bbox'], [10, 10, 180, 180])

    def test_pdf_partial_failure_reports_exact_page(self):
        path = self.root / 'sample.pdf'
        with fitz.open() as pdf:
            for _ in range(3): pdf.new_page()
            pdf.save(path)
        def page(_, location, result):
            if location['page_number'] == 2: raise RuntimeError('failure')
            return snapshot(str(location['page_number'])), {}
        with patch.object(engine, 'page', side_effect=page), patch.object(engine, 'restructure', side_effect=official_restructure):
            result = main._parse_document(str(path), path.name, '.pdf')
        self.assertEqual(result['metadata']['status'], 'partial')
        self.assertEqual(result['metadata']['issues'][0]['location']['page_number'], 2)
        self.assertEqual(result['metadata']['parsed_page_count'], 2)

    def test_official_table_merge_keeps_all_sources(self):
        table = '<table><tr><td>名称</td><td>金额</td></tr><tr><td>ITEM</td><td>1</td></tr></table>'
        pages = [snapshot(table.replace('ITEM', 'A'), 'table', [0, 150, 200, 200]), snapshot(table.replace('ITEM', 'B'), 'table', [0, 0, 200, 40])]
        result = self.result()
        with patch.object(engine, 'restructure', side_effect=official_restructure):
            text = parsing.reconstruct(pages, [{}, {}], result, [{'page_number': 1}, {'page_number': 2}])
        self.assertEqual(result.metadata['tables'][0]['status'], 'merged')
        self.assertEqual([source['page_number'] for source in result.metadata['tables'][0]['sources']], [1, 2])
        from bs4 import BeautifulSoup
        cells = [cell.get_text() for cell in BeautifulSoup(text[0], 'html.parser').find_all('td')]
        self.assertIn('A', cells); self.assertIn('B', cells)
        self.assertIn('第 1、2 页', text[0])

    def test_ambiguous_table_merge_restores_both_segments(self):
        first = '<table><tr><td>A</td><td>B</td></tr></table>'
        second = '<table><tr><td>C</td><td>D</td></tr></table>'
        result = self.result()
        with patch.object(engine, 'restructure', side_effect=official_restructure):
            text = parsing.reconstruct([snapshot(first, 'table'), snapshot(second, 'table')], [{}, {}], result, [{'page_number': 1}, {'page_number': 2}])
        self.assertIn('A', text[0]); self.assertIn('C', text[1])
        self.assertEqual(result.metadata['tables'][0]['status'], 'pending_review')

    def test_html_table_image_references_are_rewritten(self):
        source = '<table><tr><td><img src="imgs/figure.png">recognized figure words</td></tr></table>'
        self.assertIn('documents/test/asset', rewrite_images(source, {'imgs/figure.png': 'documents/test/asset'}))
        self.assertNotIn('<img', rewrite_images(source, {'imgs/figure.png': None}))
        self.assertIn('recognized figure words', rewrite_images(source, {'imgs/figure.png': None}))

    def test_page_snapshot_keeps_footer_image_path_without_render_arrays(self):
        from paddlex.inference.pipelines.paddleocr_vl.result import PaddleOCRVLBlock, PaddleOCRVLResult
        from app.vision import VisualEngine
        block = PaddleOCRVLBlock('footer_image', [0, 180, 200, 200], 'footer words')
        block.image = {'path': 'imgs/footer.png', 'img': Image.new('RGB', (20, 10), 'red')}
        data = snapshot()['res']; data['parsing_res_list'] = [block]; data['imgs_in_doc'] = []
        raw = PaddleOCRVLResult(data)
        class Pipeline:
            def predict_iter(self, *args, **kwargs):
                self.options = kwargs
                yield raw
        visual = VisualEngine(); visual.pipeline = Pipeline(); result = self.result()
        page, mapping = visual.page('not-read-by-fake.png', {'page_number': 1}, result)
        self.assertIsNone(page['res']['parsing_res_list'][0]['image']['img'])
        self.assertEqual(visual.pipeline.options['markdown_ignore_labels'], [])
        self.assertTrue(visual.pipeline.options['use_queues'])
        rebuilt = official_restructure([page])[0].markdown['markdown_texts']
        self.assertIn(mapping['imgs/footer.png'], rewrite_images(rebuilt, mapping))

    def test_merged_table_images_are_resolved_before_cross_page_merge(self):
        from paddlex.inference.pipelines.paddleocr_vl.result import PaddleOCRVLBlock, PaddleOCRVLResult
        from app.vision import VisualEngine
        result = self.result()
        snapshots, maps = [], []
        for page_number, color, box in [(1, 'red', [0, 150, 200, 200]), (2, 'blue', [0, 0, 200, 40])]:
            block = PaddleOCRVLBlock('table', box, '<table><tr><td>Header</td></tr><tr><td><img src="imgs/same-name.png"></td></tr></table>')
            data = snapshot()['res']; data['parsing_res_list'] = [block]
            data['imgs_in_doc'] = [{'path': 'imgs/same-name.png', 'img': Image.new('RGB', (20, 20), color)}]
            raw = PaddleOCRVLResult(data)
            class Pipeline:
                def predict_iter(self, *args, **kwargs): yield raw
            visual = VisualEngine(); visual.pipeline = Pipeline()
            page, mapping = visual.page('fake.png', {'page_number': page_number}, result)
            snapshots.append(page); maps.append(mapping)
        with patch.object(engine, 'restructure', side_effect=official_restructure):
            text = '\n'.join(parsing.reconstruct(snapshots, maps, result, [{'page_number': 1}, {'page_number': 2}]))
        self.assertNotEqual(maps[0]['imgs/same-name.png'], maps[1]['imgs/same-name.png'])
        self.assertIn(maps[0]['imgs/same-name.png'], text)
        self.assertIn(maps[1]['imgs/same-name.png'], text)
        self.assertNotIn('imgs/same-name.png', text)
        self.assertEqual(result.metadata['tables'][0]['status'], 'pending_review')

    def test_pdf_only_publishes_chart_and_captioned_figure_with_one_reference(self):
        from paddlex.inference.pipelines.paddleocr_vl.result import PaddleOCRVLBlock, PaddleOCRVLResult

        def image_block(label, bbox, text, name, color):
            block = PaddleOCRVLBlock(label, bbox, text)
            block.image = {'path': f'imgs/{name}.png', 'img': Image.new('RGB', (20, 20), color)}
            return block

        def page_result(blocks, extras=()):
            data = snapshot()['res']
            pixels = np.zeros((200, 200, 3), dtype=np.uint8)
            pixels[..., 2] = 255
            data.update(parsing_res_list=blocks, imgs_in_doc=list(extras))
            data['doc_preprocessor_res'] = {'output_img': pixels}
            return PaddleOCRVLResult(data)

        path = self.root / 'assets.pdf'
        with fitz.open() as pdf:
            for _ in range(2):
                pdf.new_page(width=100, height=100)
            pdf.save(path)

        figure_box = [20, 45, 80, 105]
        chart_box = [110, 40, 190, 100]
        caption = PaddleOCRVLBlock('figure_title', [20, 106, 80, 115], '图 1 接线示意图')
        cells = [{'path': f'imgs/cell-{i}.png', 'img': Image.new('RGB', (2, 2), 'black')} for i in range(1000)]
        table_top = PaddleOCRVLBlock('table', [0, 150, 200, 200],
            '<table><tr><td>Item</td></tr><tr><td>第一行 <img src="imgs/cell-999.png"></td></tr></table>')
        table_bottom = PaddleOCRVLBlock('table', [0, 0, 200, 35],
            '<table><tr><td>Item</td></tr><tr><td>第二行 <img src="imgs/cell-999.png"></td></tr></table>')
        first = page_result([
            image_block('header_image', [0, 0, 50, 12], '页眉 OCR', 'logo', 'red'),
            image_block('image', [5, 15, 95, 36], '截图 OCR', 'screenshot', 'blue'),
            image_block('image', figure_box, '接线标签 OCR', 'figure', 'green'),
            caption,
            image_block('seal', [110, 110, 152, 138], '印章 OCR', 'seal', 'yellow'),
            PaddleOCRVLBlock('chart', chart_box),
            table_top,
        ], cells)
        second = page_result([
            table_bottom,
            image_block('image', figure_box, '重复图仍保留 OCR', 'figure', 'green'),
            caption,
            image_block('chart', chart_box, '', 'repeated-chart', 'red'),
        ], cells[-1:])
        second['parsing_res_list'][-1].image['img'] = Image.new('RGB', (80, 60), 'red')
        pipeline = Mock()
        pipeline.predict_iter.side_effect = [iter([first]), iter([second])]
        pipeline.restructure_pages.side_effect = lambda pages, **_: official_restructure(pages)
        with patch.object(engine, 'pipeline', pipeline), patch.dict(os.environ, {'PDF_RENDER_DPI': '144'}), \
                patch.object(vision, '_save_image', wraps=vision._save_image) as save:
            parsed = parsing.parse_document(str(path), path.name, '.pdf', self.result().storage, 'test')

        markdown, metadata = parsed['markdown'], parsed['metadata']
        self.assertEqual(save.call_count, 4)  # Two figures and two charts; no incidental crops encoded.
        self.assertEqual(len(metadata['assets']), 2)
        for asset in metadata['assets']:
            self.assertEqual(markdown.count(asset['uri']), 1)
            self.assertTrue(self.result().storage.resolve(asset['object_key']).is_file())
        self.assertEqual(metadata['assets'][0]['location']['bbox'], figure_box)
        self.assertEqual([source['page_number'] for source in metadata['assets'][0]['locations']], [1, 2])
        self.assertEqual(metadata['assets'][1]['location']['bbox'], chart_box)
        self.assertEqual([source['page_number'] for source in metadata['assets'][1]['locations']], [1, 2])
        with Image.open(self.result().storage.resolve(metadata['assets'][1]['object_key'])) as chart:
            self.assertEqual(chart.size, (80, 60))
            self.assertEqual(chart.getpixel((0, 0)), (255, 0, 0))
        self.assertIn('![原始图表]', markdown)
        for text in ('页眉 OCR', '截图 OCR', '接线标签 OCR', '印章 OCR', '重复图仍保留 OCR', '第一行', '第二行'):
            self.assertIn(text, markdown)
        self.assertNotIn('imgs/', markdown)
        self.assertNotIn('imgs/', repr(metadata['pages']))
        self.assertEqual(metadata['tables'][0]['status'], 'merged')
        self.assertEqual([source['page_number'] for source in metadata['tables'][0]['sources']], [1, 2])
        self.assertEqual(metadata['status'], 'complete')

    def test_cuda_initialization_fails_clearly_with_cpu_paddle(self):
        try:
            import paddle
        except (ImportError, NameError) as exc:
            cause = exc
            while cause is not None:
                if 'libcuda.so.1' in str(cause):
                    self.skipTest('Paddle GPU import requires the host NVIDIA driver; unavailable on the Mac build VM')
                cause = cause.__cause__ or cause.__context__
            raise
        from app.errors import ModelUnavailable
        from app.vision import VisualEngine
        with patch.dict(os.environ, {'PADDLEOCR_BACKEND': 'cuda'}), patch.object(paddle, 'is_compiled_with_cuda', return_value=False):
            with self.assertRaisesRegex(ModelUnavailable, 'requires paddlepaddle-gpu'):
                VisualEngine().initialize()

    def test_missing_nvidia_driver_has_actionable_startup_error(self):
        import builtins
        from app.errors import ModelUnavailable
        original_import = builtins.__import__

        def import_without_driver(name, *args, **kwargs):
            if name == 'paddle':
                raise NameError('Paddle secondary import failure') from ImportError('libcuda.so.1: cannot open shared object file')
            return original_import(name, *args, **kwargs)

        with patch.dict(os.environ, {'PADDLEOCR_BACKEND': 'cuda'}), patch('builtins.__import__', side_effect=import_without_driver):
            with self.assertRaisesRegex(ModelUnavailable, 'libcuda.so.1.*NVIDIA driver.*--gpus all'):
                vision.VisualEngine().initialize()

    def test_captionless_ppt_pdf_visuals_and_pasted_slides_are_kept(self):
        from paddlex.inference.pipelines.paddleocr_vl.result import PaddleOCRVLBlock, PaddleOCRVLResult
        from app.vision import VisualEngine
        cases = [
            (1600, 900, [80, 160, 850, 820]),   # Captionless diagram on a wide slide.
            (1200, 1700, [80, 500, 1120, 1150]),  # Slide screenshot pasted in a portrait PDF.
        ]
        for width, height, box in cases:
            with self.subTest(size=(width, height)):
                visual = PaddleOCRVLBlock('image', box, '无图题示意图中的文字')
                visual.image = {'path': 'imgs/visual.png', 'img': Image.new('RGB', (100, 100), 'blue')}
                logo = PaddleOCRVLBlock('image', [10, 10, 80, 50], '小型标志文字')
                logo.image = {'path': 'imgs/logo.png', 'img': Image.new('RGB', (40, 20), 'red')}
                data = snapshot(width=width, height=height)['res']
                data.update(parsing_res_list=[logo, visual], imgs_in_doc=[])
                raw = PaddleOCRVLResult(data)
                pipeline = Mock(); pipeline.predict_iter.return_value = iter([raw])
                engine = VisualEngine(); engine.pipeline = pipeline
                result = self.result(); result.metadata['file_type'] = 'pdf'
                page, mapping = engine.page('fake.png', {'page_number': 1}, result)
                markdown = rewrite_images(official_restructure([page])[0].markdown['markdown_texts'], mapping)
                self.assertEqual(len(result.metadata['assets']), 1)
                self.assertEqual(result.metadata['assets'][0]['location']['bbox'], box)
                self.assertIn(result.metadata['assets'][0]['uri'], markdown)
                self.assertIn('无图题示意图中的文字', markdown)
                self.assertIn('小型标志文字', markdown)

    def test_artifact_commit_failure_never_reports_success(self):
        class Storage:
            def put(self, *args, **kwargs): raise OSError('failed')
        result = DocumentResult('test', Storage())
        with self.assertRaises(main.DownloadError): result.finish()

    def test_local_artifact_download_and_path_confinement(self):
        result = self.result(); result.parts.append('你好'); output = result.finish()
        key = output['metadata']['markdown_object_key']
        response = self.client.get('/artifacts/' + key)
        self.assertEqual(response.status_code, 200); self.assertEqual(response.text, '你好\n')
        with self.assertRaises(ValueError): LocalStorageBackend(str(self.root)).resolve('../secret')

    def test_api_temp_cleanup_on_success_and_failure(self):
        for failed in (False, True):
            path = self.root / 'sample.txt'; path.write_text('hello')
            with patch.object(main, '_download_document', return_value=(str(path), path.name, '.txt')):
                if failed:
                    with patch.object(main, '_parse_document', side_effect=main.DownloadError('failed')):
                        response = self.client.post('/api/v1/parse', json={'file_url': 'https://example.com/a', 'file_type': 'txt'})
                else: response = self.client.post('/api/v1/parse', json={'file_url': 'https://example.com/a', 'file_type': 'txt'})
            self.assertEqual(response.status_code, 502 if failed else 200)
            self.assertFalse(path.exists())

    def test_existing_native_format_samples(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        marker = 'Compatibility format verification'
        document = Document(); document.add_paragraph(marker); document.save(self.root / 'sample.docx')
        presentation = Presentation(); slide = presentation.slides.add_slide(presentation.slide_layouts[1]); slide.shapes.title.text = marker; presentation.save(self.root / 'sample.pptx')
        workbook = Workbook(); workbook.active.append(['title']); workbook.active.append([marker]); workbook.save(self.root / 'sample.xlsx')
        for extension, content in [('txt', marker), ('md', '# ' + marker), ('html', '<p>' + marker + '</p>'), ('csv', 'id,title\n1,' + marker)]:
            (self.root / ('sample.' + extension)).write_text(content)
        for path in self.root.glob('sample.*'):
            result = main._parse_document(str(path), path.name, path.suffix)
            self.assertIn(marker, result['markdown']); self.assertEqual(result['metadata']['status'], 'complete')

    def test_xlsx_native_formulas_cached_values_and_merges(self):
        import xlsxwriter
        path = self.root / 'formulas.xlsx'
        workbook = xlsxwriter.Workbook(path); sheet = workbook.add_worksheet('预算')
        sheet.merge_range('A1:B1', '预算表'); sheet.write('A2', 10); sheet.write_formula('B2', '=A2*2', None, 20); workbook.close()
        result = main._parse_document(str(path), path.name, '.xlsx')
        data = result['metadata']['sheets'][0]
        self.assertEqual(data['merged_ranges'], ['A1:B1'])
        formula = next(cell for cell in data['cells'] if cell['cell'] == 'B2')
        self.assertEqual(formula['formula'], '=A2*2'); self.assertEqual(formula['cached_value'], 20)
        self.assertIn('A1:B2', result['markdown']); self.assertIn('colspan="2"', result['markdown'])

    def test_xlsx_missing_formula_cache_is_explicit(self):
        from openpyxl import Workbook
        path = self.root / 'cache.xlsx'; workbook = Workbook(); workbook.active['A1'] = '=1+1'; workbook.save(path)
        result = main._parse_document(str(path), path.name, '.xlsx')
        self.assertIn('计算结果缺失', result['markdown']); self.assertEqual(result['metadata']['status'], 'partial')
        self.assertEqual(result['metadata']['issues'][0]['location']['cell'], 'A1')

    def test_docx_embedded_picture_uses_visual_path_at_paragraph(self):
        from docx import Document
        document = Document(); document.add_heading('章节', 1); document.add_paragraph('条目', style='List Bullet')
        image = io.BytesIO(); Image.new('RGB', (100, 100), 'white').save(image, format='PNG'); image.seek(0)
        document.add_paragraph().add_run().add_picture(image); path = self.root / 'images.docx'; document.save(path)
        result = self.result()
        with patch('app.parsing.recognize_office_image', return_value='视觉来源文本') as visual:
            parsing.parse_document(str(path), path.name, '.docx', result.storage, 'test')
            self.assertEqual(visual.call_count, 1); self.assertEqual(visual.call_args.args[1]['paragraph'], 3)

    def test_legacy_xls_keeps_original_cached_value_and_reads_formula_and_image(self):
        import struct
        import olefile
        import xlsxwriter
        from app.office import libreoffice
        source = self.root / 'legacy-source.xlsx'
        image = io.BytesIO(); Image.new('RGB', (100, 60), 'red').save(image, format='PNG'); image.seek(0)
        workbook = xlsxwriter.Workbook(source); sheet = workbook.add_worksheet('Legacy')
        sheet.write('A1', 10); sheet.write_formula('B1', '=A1*2', None, 20)
        sheet.insert_image('A4', 'image.png', {'image_data': image}); workbook.close()
        path = libreoffice(source, self.root, 'xls')
        with olefile.OleFileIO(path, write_mode=True) as compound:
            data = bytearray(compound.openstream('Workbook').read()); offset = 0; changed = False
            while offset + 4 <= len(data):
                code, size = struct.unpack_from('<HH', data, offset)
                if code == 0x0006 and size >= 22:
                    row, column = struct.unpack_from('<HH', data, offset + 4)
                    if (row, column) == (0, 1):
                        data[offset + 10:offset + 18] = struct.pack('<d', 1234.5); changed = True
                offset += 4 + size
            self.assertTrue(changed)
            compound.write_stream('Workbook', bytes(data))
        main._check_signature(str(path), '.xls')
        with patch('app.parsing.recognize_office_image', return_value='legacy screenshot') as visual:
            result = main._parse_document(str(path), path.name, '.xls')
        formula = next(cell for cell in result['metadata']['sheets'][0]['cells'] if cell['cell'] == 'B1')
        self.assertEqual(formula['cached_value'], 1234.5)
        self.assertIn('A1*2', formula['formula'])
        self.assertEqual(visual.call_count, 1)
        self.assertEqual(result['metadata']['status'], 'complete')

    def test_pptx_chart_notes_and_slide_order(self):
        from pptx import Presentation
        from pptx.chart.data import ChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches
        path = self.root / 'chart.pptx'; presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6]); data = ChartData(); data.categories = ['A', 'B']; data.add_series('金额', [1, 2])
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(5), Inches(3), data)
        slide.notes_slide.notes_text_frame.text = 'Speaker note'; presentation.slides.add_slide(presentation.slide_layouts[6]); presentation.save(path)
        result = main._parse_document(str(path), path.name, '.pptx')
        self.assertIn('Speaker note', result['markdown']); self.assertLess(result['markdown'].index('幻灯片 1'), result['markdown'].index('幻灯片 2'))
        self.assertEqual(result['metadata']['slides'][0]['shapes'][0]['chart']['series'][0]['values'], [1.0, 2.0])

    def test_docx_numbering_vertical_merges_and_readable_math(self):
        from docx import Document
        from docx.oxml import OxmlElement
        document = Document()
        document.add_paragraph('First', style='List Number'); document.add_paragraph('Second', style='List Number')
        table = document.add_table(rows=2, cols=2); table.cell(0, 0).text = 'Merged'; table.cell(0, 0).merge(table.cell(1, 0))
        paragraph = document.add_paragraph('Equation: ')
        math = OxmlElement('m:oMath'); fraction = OxmlElement('m:f')
        for tag, text in [('m:num', 'x'), ('m:den', 'y')]:
            parent = OxmlElement(tag); run = OxmlElement('m:r'); value = OxmlElement('m:t'); value.text = text
            run.append(value); parent.append(run); fraction.append(parent)
        math.append(fraction); paragraph._p.append(math)
        path = self.root / 'word.docx'; document.save(path)
        result = main._parse_document(str(path), path.name, '.docx')
        self.assertIn('1. First', result['markdown']); self.assertIn('2. Second', result['markdown'])
        self.assertIn('rowspan="2"', result['markdown']); self.assertIn(r'\frac{x}{y}', result['markdown'])

    def test_grouped_pptx_uses_real_libreoffice_slide_render(self):
        from pptx import Presentation
        from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
        from pptx.util import Inches
        presentation = Presentation(); slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        group = slide.shapes.add_group_shape()
        group.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1)).text = 'Group context'
        slide._element.set('show', '0')
        presentation.slides.add_slide(presentation.slide_layouts[6])
        path = self.root / 'groups.pptx'; presentation.save(path)
        with patch('app.parsing.recognize_office_image', return_value='视觉来源文本') as visual:
            result = main._parse_document(str(path), path.name, '.pptx')
        self.assertEqual(visual.call_args.args[1], {'slide_number': 1, 'scope': 'whole_slide'})
        self.assertTrue(visual.call_args.args[0].startswith(b'\x89PNG'))
        with Image.open(io.BytesIO(visual.call_args.args[0])) as rendered:
            self.assertTrue(any(low < 240 for low, high in rendered.convert('RGB').getextrema()))
        self.assertIn('幻灯片 1 整页视觉上下文', result['markdown'])
        self.assertTrue(result['metadata']['slides'][0]['hidden'])
        self.assertEqual(result['metadata']['status'], 'complete')


if __name__ == '__main__': unittest.main()
