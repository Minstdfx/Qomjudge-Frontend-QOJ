"""Offline interface demo; never executes or judges submitted code."""
from io import BytesIO
from threading import RLock
from zipfile import ZipFile, ZIP_DEFLATED


DEMO_NOTES = "QOJ VP 离线演示附件\n\n此文件由本项目生成，用于验证附件下载，不来自真实 QOJ 比赛。\n样例题：输入两个整数，输出它们的和。\n".encode("utf-8")

COLORS = ["#ef6461", "#f6b73c", "#6abf69", "#54a8e5", "#ab83d6", "#ef92b5", "#67c9c2", "#bd9a79"]
TITLES = ["A + B Problem", "Balloon Delivery", "Contest Hall", "Dividing the Teams",
          "Evening Walk", "Finding a Route", "Grand Stand", "Hello, World!"]


class DemoClient:
    authenticated = True
    username = "demo-team"

    def __init__(self):
        self.lock = RLock()

    def contest(self, contest_id):
        return {"id": contest_id, "title": "QOJ Practice Contest · 离线演示", "duration_seconds": 18000,
                "attachments": [
                    {"id": "demo-samples", "name": "samples.zip", "type": "attachment", "size": "",
                     "url": "", "external": False},
                    {"id": "demo-notes", "name": "比赛说明.txt", "type": "attachment", "size": f"{len(DEMO_NOTES)} B",
                     "url": "", "external": False}],
                "problems": [{"id": i + 1, "label": chr(65 + i), "title": title, "color": COLORS[i],
                              "time_limit": "1.0 s", "memory_limit": "256 MB"}
                             for i, title in enumerate(TITLES)]}

    def attachment(self, attachment_id):
        if attachment_id == "demo-notes":
            return BytesIO(DEMO_NOTES), "比赛说明.txt", "text/plain"
        if attachment_id == "demo-samples":
            content = BytesIO()
            with ZipFile(content, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("A/sample.in", "3 5\n")
                archive.writestr("A/sample.ans", "8\n")
                archive.writestr("README.txt", DEMO_NOTES)
            content.seek(0)
            return content, "samples.zip", "application/zip"
        raise ValueError("Unknown demo attachment")

    def problem(self, contest_id, problem_id):
        p = next(p for p in self.contest(contest_id)["problems"] if p["id"] == problem_id)
        p.update({"languages": [{"value": "C++17", "label": "C++17"}, {"value": "Python3", "label": "Python 3"},
                                {"value": "Java17", "label": "Java 17"}], "submission_available": True,
                  "statement_html": f'''<!doctype html><html lang="en"><meta charset="utf-8">
                  <style>body{{font:16px/1.7 Georgia,serif;max-width:850px;margin:35px auto;padding:0 28px;color:#263448}}
                  pre{{background:#f3f5f7;padding:16px;border:1px solid #ddd}}.note{{background:#fff4d6;padding:12px}}</style>
                  <h1>{p['label']}. {p['title']}</h1><p>Time limit: 1 second · Memory limit: 256 MB</p>
                  <p class="note">离线演示题面。所有演示提交在等待后显示模拟 AC，不编译或执行代码。</p>
                  <p>Given two integers <i>a</i> and <i>b</i>, print their sum.</p>
                  <h2>Input</h2><p>Two integers a and b (−10<sup>9</sup> ≤ a, b ≤ 10<sup>9</sup>).</p>
                  <h2>Output</h2><p>Print a single integer: a + b.</p>
                  <h2>Sample input</h2><pre>3 5</pre><h2>Sample output</h2><pre>8</pre></html>'''})
        return p

    def submit(self, *args):
        return {"qoj_id": None, "qoj_url": None, "message": "模拟评测，不编译或执行代码。"}

    def clear(self):
        pass
