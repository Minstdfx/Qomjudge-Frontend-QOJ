"""Offline regressions for readable, escaped virtual print previews."""
import unittest

from bs4 import BeautifulSoup

from printing import CODE_LANGUAGES, render_print


class PrintRenderingTests(unittest.TestCase):
    @staticmethod
    def parse(name, source):
        return BeautifulSoup(render_print(name, source), 'html.parser')

    def test_cpp_has_highlighted_tokens_line_numbers_and_no_remote_assets(self):
        soup = self.parse('solution.CPP', '// 中文注释\nint main() { return 0; }\n')
        self.assertIn('C++', soup.select_one('.metadata').get_text())
        self.assertEqual([n.get_text() for n in soup.select('.linenos')], ['1', '2'])
        self.assertTrue(soup.select('.source .kt'))
        self.assertTrue(soup.select('.source .k'))
        self.assertTrue(soup.select('.source .c1'))
        self.assertFalse(soup.select('script, link, iframe, img'))

    def test_code_whitespace_and_final_newline_are_preserved(self):
        for source in ('\n\n\tint main() {\n    return 0;\n}\n\n', 'int x;', '\n',
                       '/* 多行\n   comment */\nconst char* x = "<script>";',
                       'int x;\r\n\t// CRLF\r\n'):
            with self.subTest(source=source):
                soup = self.parse('sample.cpp', source)
                for line in soup.select('.linenos'):
                    line.decompose()
                self.assertEqual(soup.code.get_text(), source.replace('\r\n', '\n'))

    def test_code_and_filename_markup_are_literal_text(self):
        source = '</code></pre><script>alert(1)</script>\n<img src=x onerror="attack()">\n'
        name = '<svg onload=alert(1)>.cpp'
        soup = self.parse(name, source)
        self.assertEqual(soup.h1.get_text(), name)
        self.assertFalse(soup.select('script, img, svg, [onerror], [onload]'))
        for line in soup.select('.linenos'):
            line.decompose()
        self.assertEqual(soup.code.get_text(), source)

    def test_plain_text_and_markdown_remain_literal(self):
        source = '\n# Heading\n\t<script>not executable</script>\n[link](https://example.invalid)\n\n'
        for name in ('notes.txt', 'notes.md'):
            with self.subTest(name=name):
                soup = self.parse(name, source)
                self.assertEqual(soup.code.get_text(), source)
                self.assertFalse(soup.select('.linenos, .source span, script, a'))

    def test_every_accepted_code_language_has_a_working_local_lexer(self):
        for extension, (_, label) in CODE_LANGUAGES.items():
            with self.subTest(extension=extension):
                soup = self.parse('file' + extension, '\t// source\n42 < 43\n')
                self.assertIn(label, soup.select_one('.metadata').get_text())
                for line in soup.select('.linenos'):
                    line.decompose()
                self.assertEqual(soup.code.get_text(), '\t// source\n42 < 43\n')


if __name__ == '__main__':
    unittest.main()
