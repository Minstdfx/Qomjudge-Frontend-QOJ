# 界面来源说明

本说明记录 2026-09-14 的 DOMjudge 风格界面调整，范围为本项目的参赛界面实现和所用界面素材。

## 参考范围

| 来源 | 用途 |
| --- | --- |
| [DOMjudge 官方仓库](https://github.com/DOMjudge/domjudge) | 了解项目和官方许可说明，未引入仓库源码或资源文件。 |
| [DOMjudge 9.0 team manual：界面概览](https://www.domjudge.org/docs/manual/9.0/team.html#overview-of-the-interface) | 查看官方页面示例，参考顶部导航、队伍成绩行、紧凑表格和提交入口等信息组织方式；截图仅用于查看，没有作为本项目资源分发。 |
| [DOMjudge 9.0 team manual：网页提交](https://www.domjudge.org/docs/manual/9.0/team.html#web-interface) | 参考从页面选择题目、语言、源代码并查看评测结果的参赛流程。 |

## 本项目实现

2026-09-15 新增的 `cli/submit` 与 `cli/printfile` 为独立实现；文件名识别、`-p` / `-l` 用法参考 [DOMjudge 官方命令行说明](https://www.domjudge.org/docs/manual/main/team.html#command-line-submit)，没有复制其客户端源码或素材。客户端连接本项目反代的队长授权接口，虚拟打印文件仅保存于反代供队友查看。

参赛页面在本项目中独立实现，2026-09-15 迁移为 Vue 3 单文件组件，由 Vite 编译；组件源码位于 `frontend/src/`。界面保留 QOJ VP 的身份，使用本项目编写的样式、文字标记与几何图形，以及浏览器可用的系统字体。未复制 DOMjudge 的页面模板、CSS、JavaScript，也未使用 DOMjudge 的 Logo、吉祥物、图片、图标、字体文件或截图作为界面素材。

页面结构参考不代表功能完全一致：本项目通过 QOJ 提交和评测，真实比赛的参赛时间与榜单以上游为准，本地维护提交记录和共享打印；仅离线演示使用模拟计时和榜单。它是独立的训练工具，与 QOJ、DOMjudge 官方无隶属关系。

## 许可与其他内容

DOMjudge 官方 README 的 [Copyright & Licensing](https://github.com/DOMjudge/domjudge#copyright--licensing) 声明项目及文档采用 GNU GPL v2 或以后版本，部分文件及所附依赖另有许可。这里记录参考来源，不将 DOMjudge 的许可证直接指定为本项目的许可证；未来若引入其代码或素材，应按相应文件的许可处理。

本项目使用的 Flask、requests、Beautiful Soup、Waitress 等第三方依赖保留各自的许可证与版权声明。本说明仅记录此次界面实现的来源，没有为整个项目新增或变更许可证。

Vue 3 使用 MIT 许可证，随浏览器产物分发的版权和许可原文保存在 `frontend/public/THIRD_PARTY_LICENSES.txt`，构建后可通过 `/static/THIRD_PARTY_LICENSES.txt` 读取。Vite 和 Vue 构建插件仅用于开发构建，依赖版本由 `package-lock.json` 锁定，安装包保留各自许可。

虚拟打印的源代码高亮使用 [Pygments](https://pygments.org/docs/quickstart/)（BSD-2-Clause），由 Python 后端生成转义后的 HTML 与样式，不从外部 CDN 加载脚本。许可证原文保存在 `docs/PYGMENTS_LICENSE.txt`；页面排版独立实现，上传的代码仅作文本展示。

从 QOJ 代理展示的题面、PDF、附件、登录页面及其所需资源来自 QOJ 或相应权利人，区别于本项目编写的参赛界面；代理展示不改变这些内容各自的权利与使用条件。
