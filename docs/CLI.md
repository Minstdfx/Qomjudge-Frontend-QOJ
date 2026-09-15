# 终端工具 submit / printfile

这两个命令随项目的 `cli/` 目录提供，仅需要 Python 3.10+ 和标准库。它们连接本项目反代，使用队长网页上当前队伍的 QOJ 会话，支持 VP 和正式参赛。`printfile` 上传文件供本队在网页查看、下载，不调用实体打印机。

## 准备连接配置

1. 启动新版 Python 后端，在网页 **Team / 队伍** 创建队伍成为队长。
2. 在 **Team → 命令行工具** 点击“生成配置”，下载 `qoj-cli.json`。
3. 把整个 `cli/` 目录复制到队长电脑，将配置放到代码所在的工作目录。
4. 在网页连接 QOJ，并完成报名、开始参赛。命令行不会自动开始或切换比赛。

配置中包含反代地址、比赛 ID、队伍 ID 和独立的终端令牌，不含 QOJ Cookie。持有配置即可代表该队提交或上传文件，请只保存在队长电脑。队员的加入码不能作为命令行凭据，命令行也不会新增第 4 个队伍设备。不同队伍应分别下载自己的配置。

从其他电脑运行客户端时，配置中的 `server_url` 应为可访问的地址，例如 `http://192.168.1.23:8080`。可以先用该地址打开队长网页再下载配置，或只修改配置里的 `server_url`；不要填写 `0.0.0.0`、QOJ 地址或不可达的 `127.0.0.1`。Vite 开发预览请改用 Python 后端的直接访问地址。

## Windows PowerShell

无需安装命令即可运行（在项目根目录）：

```powershell
python cli/submit.py --config qoj-cli.json A.cpp
python cli/printfile.py --config qoj-cli.json notes.txt
```

如需直接使用 `submit` / `printfile`，先在项目根目录执行以下命令，为当前终端添加 PATH。之后可切换到自己的代码目录：

```powershell
$qojCliDir = (Resolve-Path -LiteralPath '.\cli').Path
$env:PATH = "$qojCliDir;$env:PATH"
$env:QOJ_CLI_CONFIG = (Resolve-Path -LiteralPath '.\qoj-cli.json').Path
submit A.cpp
printfile notes.txt
```

`.cmd` 启动器使用 PATH 中的 `python`。如需长期使用，可在 Windows 用户环境变量中添加该 `cli` 目录和 `QOJ_CLI_CONFIG` 的绝对路径。

## Linux / macOS

在项目根目录执行；从发行版 tar.gz 解压时两个命令已带可执行权限：

```sh
chmod +x cli/submit cli/printfile
export PATH="$PWD/cli:$PATH"
export QOJ_CLI_CONFIG="$PWD/qoj-cli.json"
chmod 600 "$QOJ_CLI_CONFIG"
submit A.cpp
printfile notes.txt
```

也可不改 PATH，直接运行 `python3 cli/submit.py --config qoj-cli.json A.cpp`。如需在新终端中继续使用，可将 PATH 和配置路径写入自己的 shell 配置文件，使用固定的绝对路径。

配置查找顺序为：`--config PATH`、环境变量 `QOJ_CLI_CONFIG`、当前目录 `qoj-cli.json`、用户目录 `~/.config/qoj-vp/cli.json`。

## 提交代码

```sh
submit A.cpp
submit -p B main.cpp
submit -p B -l "C++17" main.cpp
submit -p 12345 -l cpp main.cpp
submit --wait --timeout 300 A.cpp
submit --help
```

- 默认从文件名取题号，例如 `A.cpp` → A；`-p` / `--problem` 可指定本场题目字母或 QOJ 题目 ID。
- 默认按扩展名识别该题支持的语言。C++ 有多个候选时优先唯一的 C++17；仍有歧义会列出可用语言并要求 `-l` / `--language` 指定完整值。该参数也支持 `cpp`、`py` 等扩展名。示例中的 `C++17` 请替换为该题实际提供的值。
- 一次仅支持一个 UTF-8 源文件，上限 256 KiB。内容和上传文件名会保存在反代提交记录中，队长可在网页查看。源文件名不符合后端的 ASCII 命名限制时，只调整上传名为 `solution` 加原扩展名，不更改本地文件。
- 默认收到提交回执后退出，显示本地编号以及已确认的 QOJ 编号。编号尚未确认时会明确提示；网络错误或不确定回执均不会自动重发，应先到网页检查。
- 加 `--wait` 后等待最终结果，AC、WA、TLE、MLE、OLE、RE、CE、PE、JE 等终态会立即停止轮询，不显示运行时间和空间。默认最长等待 300 秒，可用 `--timeout` 调整；`--interval` 指定最小等待间隔，实际至少 3 秒并遵守服务器建议。Ctrl+C 只结束本地等待，不撤回已发出的提交。
- 退出码 0 表示请求成功或取得最终评测结果（包括 WA 等）；1 表示操作失败或等待超时；2 表示参数错误；130 表示用户中断。评测是否通过以显示的 verdict 为准。

## 虚拟打印

```sh
printfile notes.txt
printfile A.cpp
printfile document.pdf
printfile --name team-notes.txt notes.txt
printfile --help
```

支持 PDF、UTF-8 文本及常见源码文件；每份最多 2 MiB，每轮最多 100 份、合计 20 MiB。文本不允许包含 NUL 字节，PDF 须带合法文件头。文件保存于反代当前队伍的当前参赛记录，队长和两台队员机可在 **Printing** 查看、下载，其他队伍无法访问。上传失败不会自动重试。

## 更新、撤销与故障排查

- 在 **Team → 命令行工具** 可撤销配置，或重新生成使旧配置立即失效。已有配置无法再次下载，遗失后需重新生成。
- 队长在 Team 解散队伍时，该队所有终端配置立即失效；重新创建队伍后需要重新下载配置。解散会保留原来的提交与打印记录。
- Python 后端重启后令牌和历史记录仍保留，但 QOJ Cookie 仅在内存中，需回到队长网页重新连接 QOJ。
- 未开始、已结束、没有队长权限或 QOJ 未连接时，服务会拒绝提交和上传；CLI 不绕过网页的比赛权限。
- `401`：检查终端配置是否被撤销、是否属于本场比赛，以及队长网页的 QOJ 连接。
- `404`（Team 的配置状态无法读取）：确认已重启新版 Python 后端，而非只刷新前端。
- 拒绝连接：确认服务器仍运行、监听局域网地址、端口正确；参见 README 的局域网访问说明。
- 服务重定向：改用 Python 后端的直接地址。客户端不会跟随重定向传递令牌或重复发送提交。

命令的文件名识别与 `-p` / `-l` 用法参考 [DOMjudge 官方参赛手册](https://www.domjudge.org/docs/manual/main/team.html#command-line-submit)，代码独立实现，没有引入 DOMjudge 源码或美术素材；本工具只实现适合 QOJ 单文件提交的参数，不兼容 DOMjudge 的全部选项。
