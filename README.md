# QOJ → DOMjudge VP

指定一个 QOJ contest ID，启动本地 Web 服务，用类似 DOMjudge team 页面的界面参加 QOJ 原生 VP 或正式比赛。QOJ 管理报名、参赛时间、评测、榜单和封榜；本服务提供独立实现的界面、队长/队员权限与共享打印。

## 启动

需要 Python 3.10+。前端使用 **Vue 3 单文件组件 + Vite**，仓库内 `static/` 已包含编译产物，直接启动 Python 服务即可。只有修改前端源码时需要 Node.js 和 npm。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --contest 1802
```

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py --contest 1802
```

打开 **http://127.0.0.1:8080**。把 `1802` 替换成所需比赛的 ID，例如 `https://qoj.ac/contest/1802` 中的 `1802`。

只检查界面，无需账号或网络：

```powershell
.\.venv\Scripts\python.exe app.py --demo
```

**演示模式不会编译或执行代码**，每次提交会在等待后返回标明为模拟的 AC。

首次打开先在 **Team / 队伍** 创建演示队伍，再回到 **Contest / 参赛** 开始演示。

默认参赛模式为 `vp`；正式比赛可用 `--mode official` 启动，也可在页面的比赛入口切换。切换模式只查询 QOJ 状态，不会自动报名或启动比赛。真实比赛不接受本地赛长、封榜覆盖；`--duration` 仅供 `--demo` 使用，已移除 `--freeze` 命令行参数。局域网访问见下节。

## 终端命令 submit / printfile

在队长网页 **Team → 命令行工具** 生成并下载 `qoj-cli.json`，放到工作目录。先在网页连接 QOJ 并开始 VP / 正式参赛，然后运行：

```sh
python cli/submit.py --config qoj-cli.json A.cpp
python cli/submit.py --config qoj-cli.json -p B -l "C++17" --wait main.cpp
python cli/printfile.py --config qoj-cli.json notes.txt
```

客户端仅需 Python 3.10+，无需安装后端依赖；整个 `cli/` 目录可单独复制到队长电脑。将其加入 PATH 后即可直接运行 `submit A.cpp` 和 `printfile notes.txt`。`submit` 按文件名识别题号、按扩展名识别语言；`--wait` 在取得最终评测结果后停止。`printfile` 只将文件保存到反代服务器，队员在 **Printing** 查看。安装步骤、参数、局域网配置和限制见 [终端工具说明](docs/CLI.md)。

从旧版升级需重启 Python 后端，并在队长网页重新连接 QOJ；已有队伍、提交记录和打印文件保留。

## 前端开发（Vue）

页面源码在 `frontend/src/`：`App.vue` 组织页面，`components/` 包含参赛、题面、提交、榜单、队伍和打印组件，`composables/useContest.js` 管理响应式状态、权限、路由与 API，`styles/app.css` 保留独立实现的界面样式。不要直接修改 `static/assets/` 中的编译文件。

安装 Node.js 22.12+ 或 24 LTS 后，在项目根目录运行：

```powershell
npm.cmd ci
npm.cmd run build
```

`build` 先编译到 `frontend-dist/`，成功后将产物发布到 `static/`。运行中的 Python 服务无需重启，刷新浏览器即可加载新界面；构建失败不会替换现有页面。Linux/macOS 使用 `npm` 替代 `npm.cmd`。

开发时先启动 Python 后端，再打开另一个终端运行：

```powershell
npm.cmd run dev
```

访问 **http://127.0.0.1:5173** 可即时预览源码修改。Vite 将 API 和 QOJ 代理请求转发到 `http://127.0.0.1:8080`；后端端口不同时，可先设置 `$env:QOJ_BACKEND = 'http://127.0.0.1:8081'`。局域网分享使用构建后的 Python 服务端口。

只编译、不替换服务中的界面：`npm.cmd run build:check`。浏览器回归支持 `$env:QOJ_FRONTEND_STATIC = 'frontend-dist'`，配合 `scripts/browser_smoke.py`、`scripts/native_browser_smoke.py`、`scripts/submission_browser_smoke.py`、`scripts/teams_browser_smoke.py` 和 `scripts/account_browser_smoke.py` 在隔离的模拟后端检查编译产物。下载检查会临时绑定本机随机端口并在完成后关闭，不影响现有服务。

`python scripts/frontend_dev_smoke.py` 可检查 Vite 开发代理、Cookie、CSRF、题面及模拟提交；它会自动启动并关闭隔离的 Vite 和演示后端。浏览器检查需另行安装 Playwright，并在 Windows 使用已安装的 Microsoft Edge。

## 原生 VP 与正式参赛

1. 先在 **Team / 队伍** 创建队伍作为队长，或凭邀请码作为队员加入。未组队时导航仅显示 **Contest / 参赛** 和 **Team / 队伍**；即使账号已有进行中的比赛，也须完成组队后才能在本界面查看比赛内容。队长连接自己的 QOJ 账号，在比赛入口选择 **虚拟参赛** 或 **正式比赛**。
2. 页面展示 QOJ 当前提供的报名/开始操作。如果正式比赛需要选择时间窗，按页面列出的上游选项选择后提交。已在 QOJ 报名或开始的参赛会自动同步。
3. 未开始时只显示参赛状态、连接和队伍管理。题目、附件、榜单、提交记录、打印文件以及对应代理地址由后端同时限制；页面不会提前缓存比赛内容。
4. 到达 QOJ 本人参赛窗口后自动刷新进入比赛。比赛时刻以 QOJ 为准；本地不能提前结束或重置原生比赛。

QOJ 不同赛制和权限可能返回不同页面；无法确认本人参赛状态、时刻或表单时，会显示原因并保持内容关闭，不会默认为已开赛。上游操作的网络结果不确定时不自动重发，应先刷新状态确认。

报名列表分页时，会跨页合并当前账号的参赛时间、时长与 VP 身份记录。v1.1.1 修复的是已经在 QOJ 开始、但因记录跨页而无法识别的情况；它不能解决 QOJ 没有接受开始请求的问题。

QOJ 的 VP 可能需要两步操作，例如比赛 1802：首页 Start 仅进入 `/contest/1802/virtual/register` 报名页，还没有开赛。v1.2.1 会接住该跳转，在 Contest 页显示“确认 QOJ VP 报名”及上游实际提供的确认按钮或选项；再次确认后，才向该页提交最新表单并核查开赛时间。第一步接口成功只表示已取得报名表单，题目、榜单和提交仍关闭。刷新页面会保留确认阶段，不会自动确认或重发请求；原本一次提交即可开始的比赛继续沿用原流程。

QOJ 即使返回 HTTP 200，也会识别其过期、字段校验失败等错误页并显示原因。第二步仍返回报名表单或无法确认参赛时，会明确报错，不会当作已经开始。已经开始的 VP 可通过“刷新状态”恢复；登录失效时请重新连接 QOJ。升级后需重启 Python 后端并重新连接账号。

## External Standings 与 Training Mode 榜单

默认优先跟随当前比赛提供的 **External Standings** 入口，例如 `https://qoj.ac/results/QOJ3962`，直接读取普通榜单 HTML。上游 `opener` 标记的一血格（包括本队）显示为深绿底白字，`frost` 等待公布格显示为蓝色；原始成绩、罚时和题格文字保持原样。

Training Mode 使用明确的 PC2 入口，例如 `https://qoj.ac/results/QOJ3962?mode=pc2`。当前页面嵌入的 PC2 榜单优先于普通 External Standings 导航链接；无 `mode` 参数的默认链接不会被自动改成 PC2。若页面没有提供 Training 入口，可以显式指定来源：

```powershell
.\.venv\Scripts\python.exe app.py --contest 3962 --mode official --scoreboard pc2 --host 0.0.0.0 --port 8080
```

`--scoreboard pc2` 仅选择榜单来源，不会更改 QOJ 个人设置或报名；VP 也可使用。排名、题数、总罚时和题格文本均保留上游值，PC2 的罚时按分钟显示，`2/14` 表示 2 次提交、第 14 分钟通过；待公布题格保留原文及 pending 状态，不在本地计算解封结果。切回自动识别时去掉该参数或使用 `--scoreboard auto`。

External Standings 的一血完全使用上游标记，不按显示的分钟数重新推算。若 QOJ 没有提供 External 入口，仍兼容原生 ICPC 榜单及其一血规则。PC2 / Training Mode 不推算或显示一血。

各题表头显示 **通过队伍数 / 提交次数**。External Standings 优先使用上游表头汇总，PC2 使用其 `Submitted/1st Yes/Total Yes` 汇总行；没有汇总时只累加当前可见榜单，未知计数显示 `—`，不推算封榜结果。提交次数会累计同队的多次尝试，不是提交过该题的队伍数量。统计口径参考 [DOMjudge 官方实现](https://github.com/DOMjudge/domjudge/blob/main/webapp/src/Utils/Scoreboard/Scoreboard.php#L247-L254)，数值以 QOJ 当前公开的记录为准。

读取使用队长在反代中连接的 QOJ 会话。原生参赛状态确认开始后才请求榜单，队员通过本地 API 共享结果；读取失败时显示错误并清除旧榜，不会改读其他公开榜单。PC2 Name 若不包含可核实的 QOJ 用户名，仍可显示完整榜单，但个人成绩摘要可能无法自动匹配。

## 同一局域网的另一台电脑访问

以下在运行服务的 Windows 电脑上操作，两台电脑应连接同一个允许设备互访的局域网。先在项目目录启动服务，并保持此 PowerShell 窗口运行：

```powershell
.\.venv\Scripts\python.exe app.py --contest 1802 --host 0.0.0.0 --port 8081
```

只演示界面时，把 `--contest 1802` 换成 `--demo`。在另一个 PowerShell 窗口运行：

```powershell
ipconfig
```

找到当前连接的 **Wi-Fi / 无线局域网适配器** 或 **以太网适配器**下的 **IPv4 地址**，例如 `192.168.1.23`。另一台电脑在浏览器打开 **`http://192.168.1.23:8081`**，将示例 IP 换成实际地址。使用连接同一路由器的物理网卡地址；VPN、WSL、Docker、VMware 等虚拟网卡地址通常不适用。

- `127.0.0.1` 表示正在打开浏览器的那台电脑自身。默认监听它时，其他电脑无法访问。
- `0.0.0.0` 是服务监听所有 IPv4 网卡的设置；浏览器应访问服务器的实际 IP。
- 每位队长在 **Team / 队伍** 创建独立队伍、连接自己的 QOJ 账号，让另两台电脑凭加入码共享本次参赛。未组队的浏览器只显示 Contest 和 Team 入口；每支队伍看到对应账号的 QOJ 原生榜单视图。

如果另一台电脑打不开，依次排查：

1. 在服务器自己打开 `http://127.0.0.1:8081/healthz`，再打开 `http://192.168.1.23:8081/healthz`。第一个失败时检查服务窗口报错及端口；仅第二个失败时检查实际 IP 和 `--host 0.0.0.0`。
2. 在另一台 Windows 电脑的 PowerShell 中运行以下命令。`TcpTestSucceeded : True` 表示 TCP 端口可达；失败时继续检查防火墙和路由器设置。该命令含义见 [Microsoft Test-NetConnection 文档](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection)。

   ```powershell
   Test-NetConnection 192.168.1.23 -Port 8081
   ```

3. 检查服务器的 Windows 防火墙。**程序不会自动修改防火墙**。如果它确实阻止访问，可按下面的方法手动添加规则。
4. 确认两台设备没有连接到禁止互访的访客 Wi-Fi，路由器未启用 AP / 客户端隔离；VPN 路由也可能影响访问。校园或公司网络若限制设备互访，请联系网络管理员。服务器重新联网后 IP 可能改变，需要重新运行 `ipconfig`。

可选防火墙配置：先在 Windows「设置 → 网络和 Internet → 当前网络属性」确认网络配置文件。下面的规则只适用于 **Private / 专用网络**；只有确认是自己信任的家庭或训练局域网，才将它设为专用网络。公用或受单位管理的网络应联系管理员处理。

确有需要时，保持服务运行，以管理员身份打开另一个 PowerShell，执行一次下面的命令。它读取 TCP 8081 的实际监听程序路径，以兼容 Windows 虚拟环境的 Python 启动器：

```powershell
$qojVpListener = @(Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
if ($qojVpListener.Count -ne 1) { throw '请先确认 QOJ VP 已单独监听 8081 端口。' }
$qojVpPython = (Get-Process -Id $qojVpListener[0] -ErrorAction Stop).Path
if (-not $qojVpPython) { throw '无法获取监听程序路径，请确认正在使用管理员 PowerShell。' }
New-NetFirewallRule -Name 'QOJ-VP-8081-LAN' -DisplayName 'QOJ VP 8081 (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8081 -Program $qojVpPython -RemoteAddress LocalSubnet -Profile Private
```

此规则仅允许该程序在专用网络接收本地子网发往 TCP 8081 的连接；端口或实际 Python 程序路径改变后需同步调整规则。参数依据 [Microsoft New-NetFirewallRule 文档](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)。不再共享时，可在管理员 PowerShell 中移除此规则：

```powershell
Remove-NetFirewallRule -Name 'QOJ-VP-8081-LAN'
```

## 参赛流程

1. 打开页面，点击连接 QOJ。在新标签页打开代理登录页面，按 QOJ 原有流程登录（包括可能的两步验证），回到 VP 页面刷新连接。
2. 也可以导入已登录 QOJ 的 Cookie：在 QOJ 页面按 F12，打开 Network，刷新，选中发往 `qoj.ac` 的页面请求，复制 Request Headers 中 **Cookie 的值**，粘贴到连接对话框。不要复制响应中的 `Set-Cookie`。
3. 在比赛入口选择原生 VP 或正式比赛，完成 QOJ 提供的参赛操作；等待本人比赛窗口开始。
4. 在题目页阅读题面；点击 Submit 选择题目和 QOJ 支持的语言，上传源码文件或粘贴代码。识别 C++ 时默认选择上游可用的最新标准（优先 C++26）；手动选择和旧草稿已保存的语言会保留。终端 `submit` 采用相同的最新标准优先策略。
5. 提交记录自动刷新评测状态，也可点击 **Submissions → 刷新提交记录** 手动更新；刷新期间按钮显示加载状态，完成后提示结果。概览和榜单同步 QOJ 的名次、题数及罚时。
6. 开赛后点击顶部 **Attachments / 附件**（概览页也有快捷入口），查看并下载当前比赛提供的附件、整套题面等文件。列表沿用当前 QOJ 参赛权限；比赛未开始时不能下载。

QOJ 当前比赛页需要登录。没有权限访问的比赛、隐藏题目和上游的验证码/验证限制仍然有效。如果原生代理登录遇到浏览器验证，先在 QOJ 完成正常登录，再导入 Cookie。本站仅接受 QOJ 允许的访问。

上游更新 Cookie 时，同一名称和路径的导入值会被替换；删除 Cookie 时也会清除对应导入值，避免 `qoj.ac` 与 `.qoj.ac` 两种域写法导致重复发送旧会话。不同路径的 Cookie 保持独立。升级此修复后需重启后端并重新连接 QOJ。

题目页自动识别 HTML 和 PDF 题面。PDF 通过校验后的专用入口使用浏览器阅读器显示，可选择“单独打开 PDF”；普通 HTML 保留独立的内容保护。加载失败时会显示权限、网络或格式错误，队长、队员均可点击“重新加载”重新读取当前题面。

PDF 首次成功下载后保存在服务进程的内存缓存中，同队机器重复打开时直接复用，并支持本地 Range 分段读取。缓存总预算 128 MiB、单份最多 32 MiB、有效期 6 小时；更大文件仍流式读取。题目“重新加载”、切换账号/模式或权限失效会清除对应缓存，服务重启后重新下载。缓存不落盘，每次读取仍检查当前参赛权限。

## 比赛附件

附件列表读取 QOJ 比赛页实际提供的 Attachments 区域，显示文件名称和类型；无法确定的文件大小显示 `—`。点击“刷新附件”可重新读取上游列表。

- QOJ 站内下载使用当前浏览器对应的 QOJ 登录会话，经本服务流式转发。保留文件内容、上游文件名（包含中文）及 Range 下载响应；ZIP、PDF 和源码附件不会经过题面或 JavaScript 改写。
- 外部附件或题解链接标为“打开外部链接”，直接打开原站；本服务不向外站转发 QOJ Cookie。
- 没有附件时显示空列表；登录失效、访问被拒绝或读取失败会明确提示，不会当作“没有附件”。如果 QOJ 返回登录页或普通错误网页，下载接口会报错，不会把该网页伪装成附件。
- 下载使用浏览器原生流程，错误会在新标签页显示，VP 页面保持打开。本功能不批量下载、不解压附件；包内可能包含题解，按文件名称选择所需内容。
- `--demo` 中提供本项目生成的 `samples.zip` 和 `比赛说明.txt`，可离线验证下载；这些演示文件不来自 QOJ。

服务端更新后需要重启服务再刷新浏览器，以加载新的附件 API。真实附件仍取决于账号权限及 QOJ 当前页面；无账号的本地测试使用模拟响应验证下载完整性。

## 三台机器组队与虚拟打印

1. 三台机器访问**同一个服务地址**。队长在自己的浏览器连接 QOJ，打开 **Team / 队伍**，填写队伍名和设备名并创建队伍。创建前已经开始的 VP、提交记录和计时会保留。
2. 队长将加入码交给另外两名队员。队员在各自的 **Team / 队伍** 页输入加入码及设备名加入，不需要输入或导入 QOJ Cookie。
3. 队长在比赛入口操作 QOJ 原生参赛，无需再次填写队伍名称。Team 名称用于本地设备和打印管理，原生榜单的队伍名仍由 QOJ 决定。三台机器共享同一 QOJ 账号的参赛状态和可见内容。
4. 队长打开 **Printing / 打印**，上传文件或粘贴代码创建打印记录。队员在打印页查看或下载，列表会自动刷新。

| 功能 | 队长机 | 队员机（最多两台） |
| --- | --- | --- |
| 开赛后的题目、QOJ 榜单、虚拟打印查看与下载 | ✓ | ✓ |
| 连接、断开 QOJ 账号 | ✓ | — |
| 选择参赛模式、报名/开始原生参赛、提交代码及查看提交记录 | ✓ | — |
| 比赛附件、原生 QOJ 代理操作页面 | ✓ | — |
| 创建虚拟打印，管理加入码、移除队员设备 | ✓ | — |
| 退出队伍、释放队员名额 | — | ✓ |

角色和名额由服务器检查。队伍最多有 **1 台队长机 + 2 台队员机**；加入码只授予队员权限，不可申请队长权限。设备按浏览器会话登记，刷新或重启浏览器仍占原名额；无痕窗口、不同浏览器或清除 Cookie 后会被视为新设备。关闭页面不会自动释放名额，队员可主动退出，或由队长移除设备。移除会立即撤销后续访问并自动更换加入码；手动更换加入码不影响已加入的队员。队长会话请保留在原浏览器中，目前不提供队长转移功能。

队长可在 **Team → 解散队伍** 确认解散当前本地队伍。所有设备的队伍关系随即解除，旧加入码及命令行配置立即失效；队员刷新后只显示 Contest 和 Team。此操作保留历史参赛、提交源码及打印文件，也不会取消或结束 QOJ 上的比赛。原队长可继续保留当前 QOJ 连接，并重新创建队伍；重新组队后须生成新的终端配置。其他队伍不受影响。

打印文件保存在 `--data-dir` 下的 `prints/`，元数据保存在 `vp.sqlite3`。支持 UTF-8 文本、常见源码文件和 PDF，每份最多 **2 MiB**，每轮 VP 最多 **100 份、合计 20 MiB**。源码按文件扩展名进行语法高亮并显示行号，优先使用 Consolas 与微软雅黑字体；保留缩进与空行，普通文本仍按原文显示。渲染使用本地 Pygments，不会编译或执行源码，下载文件保持原始内容。虚拟打印不调用实体打印机。

文件按队伍、QOJ 账号和本轮参赛隔离：其他队伍无法读取；结束后本轮仍可查看，新一轮参赛显示新文件列表，旧文件保留在服务器上。备份时应一起备份 `vp.sqlite3`、`prints/` 和 `server-secret.key`；这些数据包含队伍凭证或共享源码。服务重启后队伍、设备和打印记录会恢复，QOJ 会话仍需由队长重新连接一次。

交互参考用户提供的 [OMS 说明手册第 3–4 页](https://icpc.pku.edu.cn/docs/2026-08/77ea6405152140158e9730cac73e04ed.pdf)，界面与代码为独立实现，没有复制其美术素材。只读限制作用于本服务，浏览器以外的本机编辑器或其他网站不由此服务控制。

## 参赛规则与数据

- 界面要求先创建或加入队伍，再开放比赛内容。已有独立会话的账号与参赛进度会在创建队伍后继续使用；加入队伍后三台机器共享队长的 QOJ 会话和 VP，队员拿不到上游 Cookie。不同队伍的账号和打印文件相互隔离。
- 真实模式展示当前 QOJ 账号可见的原生榜单，包含上游返回的选手/队伍。名次、题数、罚时、题目格原文均来自 QOJ，不用本地提交重排榜单。
- QOJ 封榜后，名次、题数、罚时以及 `?x` 等待公开的提交次数按上游榜单显示。何时封榜与解封由该场比赛和 QOJ 决定，本服务不推算或绕过封榜结果。
- 计时与是否允许提交取自 QOJ 本人的参赛状态，刷新或关闭浏览器不会暂停。结束后拒绝新的比赛提交，已提交的评测仍可继续刷新。
- 本地记录仅用于本服务发出的提交日志及虚拟打印，按 QOJ 账号、参赛模式和上游开始时刻隔离。模式切换不会创建新一轮；同一参赛在服务重启后重新连接账号可恢复本地记录。
- 升级前的本地 VP 历史仍保存在 SQLite；原生模式按 QOJ 已确认的场次绑定记录。
- SQLite 和本地会话签名密钥保存在 `.data/`，可用 `--data-dir PATH` 更改。浏览器保留会话 Cookie 时，服务重启后可继续当前 VP。
- QOJ Cookie 仅保存在服务进程内存中，重启后需要队长重新连接。提交源码、文件名和用于确认编号的凭据保存在 SQLite 私有表中；队长可在当前参赛的提交记录中查看、复制和下载代码，队员没有源码接口权限。源码不会混入状态列表或榜单响应。升级前未保存源码的记录会明确提示无法查看。主动创建的虚拟打印内容仍作为文件保存在服务器；界面草稿暂存在当前浏览器标签页的 `sessionStorage`，按队伍和 VP 分开。
- 提交列表目前记录从本服务发出的提交；榜单直接读取 QOJ，因此通过 QOJ 网页发出的比赛提交也会反映到上游榜单。参赛操作会真正注册/开始 QOJ 原生参赛，请使用自己的账号并确认时间窗。
- 提交 POST 不自动重试。发送前保存源码指纹、账号、题目、提交前编号及时间范围；不能立即确认编号时显示“编号待确认”，后续只读取列表及源码进行核对，不会重新提交。确认后保存 QOJ 编号并读取判定；兼容 QOJ 原始用户名节点、无语言列的列表及 `AC ✓`、`100 ✓` 等结果。
- 最终 AC、WA、TLE、MLE、OLE、RE、CE、PE、JE 结果保存后不再查询该提交，重启也保持终态；兼容 QOJ Training Mode 的 TL、ML 等简称。遇到 429 时，同一 QOJ 连接的全部请求至少冷却 60 秒，并遵循更长的 Retry-After；服务端错误至少暂停 30 秒再读取。正式比赛和 VP 均突出显示最终判定，同义的原始结果不再重复显示，额外错误说明仍保留。统一不展示运行时间和内存；表格中的 Elapsed 是提交时距开赛的时间。

界面面向 ICPC 赛制。题目需要的特殊提交格式（例如输出答案、多文件或交互专用模板）若无法由普通源码表单表达，会提示不支持。离线 `--demo` 保留模拟计时、模拟 AC 和本地示例榜单，与真实 QOJ 模式分离。

上游机制参考 [QOJ 官方参赛指南](https://qoj.ac/blog/ucupannouncements/blog/701)、[QOJ 更新日志](https://qoj.ac/blog/qingyu/blog/631)。适配器只解析页面及表单数据，不执行上游内嵌脚本，不复制 QOJ/DOMjudge 的界面代码或美术素材。

## 部署

应用使用 Waitress 多线程服务。只运行一个应用进程：QOJ 登录会话在进程内存中，不适合直接开启多个 WSGI worker。

Docker：

```bash
docker build -t qoj-vp .
docker run --rm -p 8080:8080 -v qoj-vp-data:/app/.data qoj-vp --contest 1802 --host 0.0.0.0
```

共享到公网时，在前面配置 HTTPS 和访问控制；Cookie 具有账号权限，只在你信任的服务器上连接账号。HTTPS 部署可添加 `--secure-cookie`。如果 HTTPS 在前置代理终止，需要将传给应用的 `Host` 与实际访问地址保持一致，并让应用识别正确的 HTTPS scheme（参见下文）。

自定义部署入口可通过 `from app import create_app` 创建应用，再针对**受信任的单层反向代理**设置 `app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)`（从 `werkzeug.middleware.proxy_fix` 导入 `ProxyFix`），并限制后端仅允许该代理访问。默认命令行服务不信任任何客户端提供的转发头。

依赖 `requests` 的标准 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量配置上游网络代理。`GET /healthz` 可用于本地健康检查。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

可选浏览器验证：在安装了 `playwright` 和 Chromium 的 Python 环境中运行 `python scripts/browser_smoke.py http://127.0.0.1:8080`（该地址须运行 `--demo`）。脚本验证附件下载（含 ZIP 内容和中文文件名）、开赛、题面、提交、恢复、榜单及移动端，并将截图保存到 `artifacts/`。它只向明确标识为演示的实例发送测试代码。

原生模式浏览器回归可运行 `python scripts/native_browser_smoke.py`，需要 Playwright 和 Microsoft Edge。它把真实 Flask 应用接到浏览器内存路由，使用模拟上游验证 VP、正式比赛时间窗、`?x` 榜单、双角色门禁、字段选择和缓存清除；不启动监听服务，也不登录或报名真实 QOJ 比赛。

源码与提交结果浏览器回归：`python scripts/submission_browser_smoke.py`。同样使用内存路由，验证源码查看、复制、下载、角色限制、终态显示和手机布局，不向 QOJ 提交测试代码。

组队浏览器验证：`python scripts/teams_browser_smoke.py http://127.0.0.1:8080`。脚本建立四个独立浏览器会话，验证队长及两名队员、额外设备拒绝、上传与粘贴打印、队员预览下载、设备撤销及移动端。也只允许在 `--demo` 实例运行。

核心文件：

| 文件 | 作用 |
| --- | --- |
| `app.py` | 命令行启动、Web API、会话及代理路由 |
| `results.py` | QOJ PC2 results HTML 榜单解析 |
| `submission_data.py` | 原始提交记录、源码容器及确认指纹解析 |
| `qoj.py` | QOJ 页面解析、Cookie、源码提交和评测结果 |
| `downloads.py` | 附件原始字节流、文件名及下载响应头转发 |
| `teams.py` | 队伍、设备权限、三设备名额及加入码持久化 |
| `printing.py` | 每轮 VP 的虚拟打印文件、额度及隔离存储 |
| `statements.py` | PDF 题面来源校验、文件类型验证与流式显示 |
| `storage.py` | SQLite 持久化及 ICPC 计分 |
| `demo.py` | 不访问上游的离线演示 |
| `frontend/src/` | Vue 组件、共享状态、界面样式 |
| `frontend/public/` | 题面 MathJax 配置及前端依赖许可声明 |
| `static/` | Python 服务使用的已编译界面，无需运行 Node.js |
| `tests/` | 计分、会话、时限和上游解析测试 |

页面适配参考 [QOJ 登录页](https://qoj.ac/login)、[QOJ 题目页](https://qoj.ac/problem/2) 和 [UOJ 源码](https://github.com/UniversalOJ/UOJ-System)。界面结构与交互参考 [DOMjudge team manual](https://www.domjudge.org/docs/manual/9.0/team.html)，使用独立编写的 Vue 组件及样式，没有复制 DOMjudge 源码，也没有使用其图片、Logo、图标或字体文件。来源、参考范围和许可说明见 [界面来源说明](docs/UI_PROVENANCE.md)。这是独立实现，不是 QOJ 或 DOMjudge 官方项目。

上游网页结构可能变化。遇到登录失效、页面无法解析或提交身份无法确认时，应用会显示错误，不会生成虚假的真实评测结果。完整的真实提交链路需使用你自己的 QOJ 账号验证；离线演示与测试不向 QOJ 提交代码。
