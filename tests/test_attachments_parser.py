"""Contest attachment discovery; fixtures contain no account data or solutions."""

import hashlib
import html
import unittest

from qoj import parse_attachments, parse_contest


def attachment_card(links, heading="Attachments"):
    return (f'<div class="card border-success top-buffer-lg">'
            f'<h4 class="card-header bg-success">{heading}</h4>'
            f'<div class="list-group">{links}</div></div>')


class AttachmentParserTests(unittest.TestCase):
    def test_current_qoj_card_retains_revisions_and_classifies_external_tutorial(self):
        # Structure and link labels verified on the public QOJ installation at
        # https://training.qoj.ac/contest/1042 on 2026-09-14. Relative links use
        # qoj.ac in production; the tutorial is explicitly an external link.
        source = attachment_card('''
            <a class="list-group-item list-group-item-action"
               href="/download.php?type=attachments&amp;id=1042&amp;r=0">Attachments</a>
            <a class="list-group-item list-group-item-action"
               href="/download.php?type=attachments&amp;id=1042&amp;r=1">Statements (zh-cn)</a>
            <a class="list-group-item list-group-item-action"
               href="https://pjudge.ac/blog/qingyu/blog/621">Tutorial</a>''')
        files = parse_attachments(source, 1042)
        self.assertEqual([f["name"] for f in files], ["Attachments", "Statements (zh-cn)", "Tutorial"])
        self.assertEqual([f["type"] for f in files], ["attachment", "statement", "tutorial"])
        self.assertEqual([f["external"] for f in files], [False, False, True])
        self.assertEqual(files[0]["url"], "https://qoj.ac/download.php?type=attachments&id=1042&r=0")
        self.assertEqual(files[1]["url"], "https://qoj.ac/download.php?type=attachments&id=1042&r=1")
        self.assertEqual(files[2]["url"], "https://pjudge.ac/blog/qingyu/blog/621")
        self.assertEqual(files[0]["id"], hashlib.sha256(files[0]["url"].encode()).hexdigest()[:24])

    def test_legacy_panel_and_chinese_heading(self):
        source = '''<div class="panel panel-success"><div class="panel-heading">
          <h4 class="panel-title">附件：</h4></div><div class="panel-body">
          <a href="/download.php?type=attachments&amp;id=9&amp;r=2">题面（中文）</a>
          <a href="/download.php?type=attachments&amp;id=9&amp;r=3">官方题解</a>
          </div></div>'''
        files = parse_attachments(source, 9)
        self.assertEqual([f["type"] for f in files], ["statement", "tutorial"])

    def test_heading_list_stops_before_next_section(self):
        source = '''<h3>比赛附件</h3><ul><li><a href="/download.php?type=attachments&amp;id=9">
          Sample code.zip</a></li></ul><h3>Discussion</h3>
          <a href="https://example.org/forum">Answers</a>'''
        files = parse_attachments(source, 9)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "Sample code.zip")

    def test_only_attachment_section_links_are_discovered(self):
        source = '''<nav><a href="/contest/9/attachments">Attachments</a></nav>
          <article><p>A question about <a href="/download.php?type=attachments&amp;id=9">Attachments</a></p></article>
          <div class="card"><h4 class="card-header">Questions about problems</h4>
          <div class="list-group"><a href="https://example.org/example.zip">Example.zip</a></div></div>'''
        self.assertEqual(parse_attachments(source, 9), [])
        self.assertEqual(parse_attachments("<h1>No attachments</h1>", 9), [])

    def test_deduplication_normalization_and_stable_ids(self):
        source = attachment_card('''
          <a href="../download.php?type=attachments&amp;id=9&amp;r=1">PDF</a>
          <a href="https://QOJ.AC:443/download.php?type=attachments&amp;id=9&amp;r=1#ignored">Duplicate</a>
          <a href="http://qoj.ac/download.php?type=attachments&amp;id=9&amp;r=1">Duplicate over HTTP</a>
          <a href="/download.php?type=attachments&amp;id=9&amp;r=2&amp;lang=en">English</a>
          <a href="/download.php?type=attachments&amp;id=9&amp;r=2&amp;lang=zh-cn">Chinese</a>''')
        files = parse_attachments(source, 9)
        self.assertEqual(len(files), 3)
        self.assertEqual(files[0]["name"], "PDF")
        self.assertEqual(len({f["id"] for f in files}), 3)
        self.assertEqual(files, parse_attachments(source, 9))
        self.assertTrue(files[1]["url"].endswith("r=2&lang=en"))

    def test_downloads_must_belong_to_this_contest(self):
        targets = [
            "/download.php?type=attachments&id=10",
            "/download.php?type=attachments&id=9&id=10",
            "/download.php?type=statement&id=9",
            "/download.php?type=attachments&type=statement&id=9",
            "/download.php?type=attachments",
            "/contest/10/attachments/file.zip",
            "/problem/9", "/contest/9/problem/9", "/submission/9",
            "/contest/9/discussion", "/logout", "/user/profile/person",
        ]
        source = attachment_card("".join(f'<a href="{html.escape(url, quote=True)}">Download</a>' for url in targets))
        self.assertEqual(parse_attachments(source, 9), [])

    def test_unsafe_or_ambiguous_urls_are_rejected(self):
        targets = [
            "javascript:alert(1)", "data:text/html,hello", "file:///etc/passwd", "mailto:test@example.org",
            "https://user:secret@example.org/file.zip", "https://@example.org/file.zip",
            "https://qoj.ac:8443/download.php?type=attachments&id=9", "https://[invalid/file.zip",
            "https://qoj.ac\\@example.org/file.zip", "https://example.org/%00file.zip",
            "https://example.org/file%0d%0a.zip", "https://example.org/\x7ffile.zip",
            "https://example.org/\nfile.zip", "https://example.org/\tfile.zip", "#attachments", "",
        ]
        for target in targets:
            with self.subTest(target=target):
                source = attachment_card(f'<a href="{html.escape(target, quote=True)}">File.zip</a>')
                self.assertEqual(parse_attachments(source, 9), [])

    def test_external_urls_remain_external_and_filename_fallback_is_useful(self):
        source = attachment_card('''
          <a href="//FILES.example.org:443/contest%20files.zip"></a>
          <a href="https://example.org/download.php?file=12" download="kit.zip"></a>
          <a href="https://example.org/documents" title="Technical requirements"></a>''')
        files = parse_attachments(source, 9)
        self.assertEqual([f["name"] for f in files], ["contest files.zip", "kit.zip", "Technical requirements"])
        self.assertTrue(all(f["external"] for f in files))
        self.assertEqual(files[0]["url"], "https://files.example.org/contest%20files.zip")

    def test_untrusted_base_tag_cannot_redirect_internal_downloads(self):
        source = '<base href="https://evil.example/">' + attachment_card(
            '<a href="../download.php?type=attachments&amp;id=9">Files</a>')
        files = parse_attachments(source, 9)
        self.assertFalse(files[0]["external"])
        self.assertEqual(files[0]["url"], "https://qoj.ac/download.php?type=attachments&id=9")

    def test_explicit_parser_base_resolves_relative_external_links(self):
        source = attachment_card('<a href="files/examples.zip">Samples</a>')
        files = parse_attachments(source, 9, base_url="https://fixtures.example.org/contest/9/")
        self.assertEqual(files[0]["url"], "https://fixtures.example.org/contest/9/files/examples.zip")
        self.assertTrue(files[0]["external"])

    def test_parse_contest_includes_attachment_metadata_without_needing_a_second_page(self):
        problem = '<h1>Contest</h1><table><tr><td>A</td><td><a href="/contest/9/problem/3">A. Sum</a></td></tr></table>'
        files = attachment_card('<a href="/download.php?type=attachments&amp;id=9&amp;r=5">Inputs.zip</a>')
        result = parse_contest(problem + files, 9)
        self.assertEqual(result["attachments"][0]["name"], "Inputs.zip")
        self.assertEqual(parse_contest(problem, 9)["attachments"], [])


if __name__ == "__main__":
    unittest.main()
