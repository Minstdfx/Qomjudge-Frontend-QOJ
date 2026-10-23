"""Build an allowlisted source + precompiled UI release; never include runtime data.

Run `npm run build:check` first, then `python scripts/build_release.py`.
The result and its SHA-256 checksum are written to releases/.
"""
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    'app.py', 'ccpc.py', 'cli_auth.py', 'demo.py', 'downloads.py', 'joint_entry.py', 'joint_vp.py', 'participation.py', 'printing.py',
    'qoj.py', 'results.py', 'statements.py', 'storage.py', 'submission_data.py',
    'teams.py', 'requirements.txt', 'package.json', 'package-lock.json',
    'README.md', 'Dockerfile', '.dockerignore', '.gitignore', '.npmrc', '.nvmrc',
    'cli/submit', 'cli/printfile',
)
SOURCE_TREES = {
    'frontend': {'.vue', '.js', '.css', '.html', '.txt'},
    'docs': {'.md', '.txt'},
    'scripts': {'.py', '.mjs'},
    'tests': {'.py'},
    'cli': {'.py', '.cmd'},
}
EXCLUDED = {'.data', '.env', '.git', '.venv', 'node_modules', '__pycache__',
            '.pytest_cache', 'artifacts', 'previews', 'releases', 'frontend-dist'}


def collect_files():
    files = {}

    def include(path, destination):
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f'Not an ordinary project file: {destination}')
        if any(part in EXCLUDED for part in PurePosixPath(destination).parts):
            raise ValueError(f'Excluded release path: {destination}')
        files[destination] = path.read_bytes()

    for name in ROOT_FILES:
        include(ROOT / name, name)
    for name in ('LICENSE', 'LICENSE.md', 'NOTICE'):
        if (ROOT / name).is_file():
            include(ROOT / name, name)
    for directory, extensions in SOURCE_TREES.items():
        for path in sorted((ROOT / directory).rglob('*')):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED for part in relative.parts):
                continue
            if path.is_file() and path.suffix in extensions:
                include(path, relative.as_posix())

    # Package the freshly compiled build, never old assets accumulated in static/.
    build = ROOT / 'frontend-dist'
    entry = build / 'index.html'
    if not entry.is_file():
        raise ValueError('Run npm run build:check before building the release')
    html = entry.read_text(encoding='utf-8')
    assets = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"', html)
    if len(assets) < 2 or 'type="module"' not in html:
        raise ValueError('The frontend build is missing its compiled Vue entry')
    for path in sorted(build.rglob('*')):
        if path.is_file() and path.suffix in {'.html', '.js', '.css', '.txt'}:
            include(path, 'static/' + path.relative_to(build).as_posix())
    for asset in assets + ['/static/math-config.js', '/static/THIRD_PARTY_LICENSES.txt']:
        if asset.lstrip('/') not in files:
            raise ValueError(f'Missing frontend asset: {asset}')
    return files


def add_bytes(archive, name, data):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o755 if name.endswith(('/cli/submit', '/cli/printfile')) else 0o644
    entry.mtime = 0
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ''
    archive.addfile(entry, io.BytesIO(data))


def main():
    files = collect_files()
    version = json.loads(files['package.json'])['version']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z.+-]{0,63}', version):
        raise ValueError('Invalid release version in package.json')
    name = f'Domjudge-Frontend-QOJ-v{version}'
    files['RELEASE.md'] = f'''# Domjudge-Frontend-QOJ v{version}

此包包含后端源码、Vue 源码、已编译前端、文档及离线测试。
不包含运行数据、账号凭据、日志、截图、虚拟环境或 node_modules。

## 本版更新

- 新增可选的 CCPC 动态榜单：通过 `--scoreboard-style ccpc --ccpc-unlock 50` 开启，按不同 AC 队伍数逐步揭晓题号；支持 `--ccpc-unlock-overrides A=10,C=20` 为指定题目设置阈值。匿名成绩块在后端移除题目对应关系，已通过题按公开的通过时间排列，保留 QOJ 的排名、过题数、罚时、一血和封榜提交。默认普通榜单不变。
- CCPC 揭晓记录持久化，刷新或重启不会重新隐藏；识别到 QOJ 封榜阶段或参赛结束时公开全部题号，同时保留上游尚未解封的评测结果。无法识别封榜时点时，可用 `--ccpc-reveal-after 240` 指定从本队实际开赛起 240 分钟公开题号。阈值统计包含当前 QOJ 榜单中的历史队伍，不额外叠加 20% 条件；本队概览和提交记录仍显示真实题号。
- 新增多队联合 VP：各队长使用自己的 QOJ 账号，加入同一联合组织、读取报名选项并明确授权后，由组织者并发发起开赛。最多 20 队，沿用各队实际 QOJ 计时及上游榜单，显示已确认的开赛时间差。单队失败不影响其他队伍；不确定的开赛请求只核查状态，不自动重发。服务重启保留记录，但需要重新准备授权。
- 新增启动参数 `--show-unofficial` / `--no-show-unofficial`。明确指定时优先读取 QOJ 原生榜单，使开关生效；未指定时保留现有榜单来源选择。
- 更新前端构建安全要求：推荐 Node.js 24.22.1 LTS，支持范围为 `^22.23.4 || ^24.22.1 || ^26.12.1`；安装依赖、开发及构建入口会检查版本。Docker 构建阶段同步更新，Vite 升级至 8.3.5。直接运行本发布包的 Python 服务无需 Node.js。
- 保留 PDF 题面缓存、虚拟打印代码高亮、最新 C++ 标准自动选择、手动刷新提交记录及此前的提交修复。

## 从旧版升级

1. 停止旧服务并备份数据目录，保留原有 `.data/`（或 `--data-dir` 指定的目录）。不要用新空目录替换原数据；队伍、提交源码和虚拟打印文件会继续使用这些数据。
2. 更新程序文件后，在服务使用的 Python 环境中运行 `python -m pip install -r requirements.txt`，确保依赖完整。
3. 使用原比赛参数及数据目录重新启动 Python 后端，在队长网页重新连接 QOJ，再刷新浏览器加载新版界面。发布包已经包含编译后的前端，无需运行 npm。

已有 QOJ VP 可通过刷新状态恢复。对于两步 VP，点击 Start 后仍需在 Contest 页确认报名，确认开赛前比赛内容保持关闭。保留 v1.2.2 的参赛规则提示，以及两步报名、Cookie 更新、队伍解散、终端提交等已有功能。

## 启动

需要 Python 3.10+，先安装 requirements.txt 中的依赖。直接运行不需要 Node.js。

```sh
python -m pip install -r requirements.txt
python app.py --demo
```

打开 http://127.0.0.1:8080 后，先在 Team 创建测试队伍，再回到 Contest 开始离线演示。
未创建或加入队伍时只显示 Contest 与 Team；已有上游参赛进度也须先完成组队。
真实 VP：

```sh
python app.py --contest 1802 --mode vp --host 0.0.0.0 --port 8080
```

将 1802 替换为目标比赛 ID。虚拟环境、局域网访问、账号连接、队伍权限和前端开发方法见 README.md。
前端依赖许可见 static/THIRD_PARTY_LICENSES.txt，Pygments 许可见 docs/PYGMENTS_LICENSE.txt，界面来源见 docs/UI_PROVENANCE.md。

CCPC 动态榜单：

```sh
python app.py --contest 1802 --scoreboard-style ccpc --ccpc-unlock 50
```

多队联合 VP 在 Contest 页创建与加入。需要展示非正式参赛者时可加 `--show-unofficial`。这两项功能都继续遵循每个 QOJ 账号可见的榜单，不拼接各队隐藏成绩。CCPC 视图隐藏本服务公共榜单中的题目对应关系，直接访问 QOJ 原榜仍可能看到题号。

## 终端提交与虚拟打印

包含 `cli/submit` 和 `cli/printfile`（Windows 使用同名 `.cmd`）。客户端仅需 Python 3.10+，可把整个 cli 目录复制到队长电脑。
在队长网页 Team → 命令行工具生成并下载 qoj-cli.json，开始参赛后可运行：

```sh
python cli/submit.py --config qoj-cli.json A.cpp
python cli/printfile.py --config qoj-cli.json notes.txt
```

PATH 配置、指定题号/语言、等待结果与权限说明见 docs/CLI.md。升级后请重启 Python 后端，原有 QOJ 连接需在队长网页重新建立。

## 重新构建发布包

使用 Node.js 24.22.1 或更新的 24.x LTS 补丁版本；其他支持版本及安全下限见 README.md。

```sh
npm ci
npm run build:check
python scripts/build_release.py
```

RELEASE.json 包含每个发布文件的大小与 SHA-256，压缩包旁附整体 SHA-256 校验文件。
'''.encode('utf-8')
    manifest = {'name': name, 'version': version, 'frontend_precompiled': True,
                'files': [{'path': path, 'size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}
                          for path, data in sorted(files.items())]}
    files['RELEASE.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output = ROOT / 'releases'
    output.mkdir(exist_ok=True)
    destination = output / f'{name}.tar.gz'
    temporary = destination.with_name(destination.name + '.tmp')
    with temporary.open('wb') as raw:
        with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for path, data in sorted(files.items()):
                    add_bytes(archive, f'{name}/{path}', data)
    temporary.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = output / f'{destination.name}.sha256'
    checksum.write_text(f'{digest}  {destination.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(destination), 'files': len(files),
                      'bytes': destination.stat().st_size, 'sha256': digest,
                      'checksum': str(checksum)}))


if __name__ == '__main__':
    main()
