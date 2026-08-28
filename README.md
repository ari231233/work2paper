# papermine

从**横向项目工作**（代码 + 文档）中挖掘**候选论文点**的本地工具。

面向科研领域学生：做了很多横向/工程工作，却不知道哪些能写论文。papermine 把散落在代码、周报、结题报告里的工作，转成一份「研究问题 + 候选创新点 + 可行性评估 + 论文路线图」，帮你看清自己的科研价值。

> 定位：只做「看见 + 评估」，**不代写正文**。这是学术诚信红线，也是产品边界。

> ⚖️ **学术诚信声明**：本工具是「选题 / 研究助理」，只产出候选点、评估与路线图，**不代写论文正文**；所有产出需人工核验与改写。

> 🔒 **隐私承诺**：确定性分析在本地完成；接入 LLM 后，仅将**脱敏后的结构化事实**发送给你配置的 LLM 服务，不发送完整源码与原始数据；可切换本地模型实现完全离线。

> 📐 系统组成、分析流程与数据流向见 [`docs/architecture.md`](docs/architecture.md)；工程规范见 [`docs/engineering.md`](docs/engineering.md)；踩坑与复盘见 [`docs/lessons-learned.md`](docs/lessons-learned.md)。

## 核心链路

```
项目理解 → 问题抽象 → 知识检索 ⇄ 创新点生成 → 可行性评估 → 论文路线规划 → 经验沉淀（自进化）
```

外加一个**自进化层**：每次分析结束后蒸馏经验（去领域化的原则 + 行为策略），跨任务、跨项目积累，越用越懂科研。

## 快速开始

```bash
# 安装核心 CLI（仅代码与纯文本扫描）
pip install -e .

# 如需解析 PDF / DOCX / PPTX 及中英文 OCR
pip install -e ".[documents]"

# 配置 DeepSeek key（可选；不配则走确定性降级）
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 端到端分析（--auto 跳过检查点暂停）
python -m papermine analyze examples/sample-project --auto

# 逐检查点确认、查进度、续跑
python -m papermine analyze examples/sample-project
python -m papermine status <run_id>
python -m papermine resume <run_id>
```

分析结果写入 `~/.papermine/runs/<run_id>/report.md`。

## Windows 安装与打开 Web 客户端（小白教程）

下面的步骤只需要在**第一次使用**时完整执行。安装完成后，再次打开只需要执行最后的“日常启动”步骤。

### 1. 安装三个基础软件

请依次安装：

1. [Git for Windows](https://git-scm.com/download/win)：用于从 GitHub 下载和更新 papermine。安装时保持默认选项即可。
2. [Python](https://www.python.org/downloads/windows/)：需要 Python 3.8 或更高版本。安装界面请勾选 **Add Python to PATH**。
3. [Node.js](https://nodejs.org/)：需要 Node.js 18 或更高版本，建议安装网站提供的 LTS 版本。npm 会随 Node.js 一起安装。

安装完成后，关闭并重新打开 PowerShell。在 Windows 开始菜单中搜索 `PowerShell` 即可打开。逐条执行下面三个命令：

```powershell
git --version
python --version
node --version
npm --version
```

每条命令都能显示版本号，说明环境安装成功。如果 `python` 无法识别，也可以尝试 `py --version`；后续命令中的 `python` 可相应替换为 `py`。

### 2. 下载 papermine

本教程把 papermine 固定安装到 `D:\papermine`，避免把项目代码、Python 虚拟环境和网页依赖放在系统 C 盘。如果希望使用 E 盘、F 盘等其他磁盘，把下面路径开头的大写盘符 `D` 改成对应盘符即可，例如把 `D:\papermine` 改成 `E:\papermine`。

请先确认电脑存在 D 盘，然后在 PowerShell 中执行：

```powershell
Set-Location D:\
git clone https://github.com/ari231233/work2paper.git papermine
Set-Location D:\papermine
```

执行后，项目会固定保存在 `D:\papermine`。以后执行 papermine 命令前，也需要先进入这个文件夹。

### 3. 创建独立的 Python 环境

继续执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

成功后，PowerShell 命令行开头通常会出现 `(.venv)`。这个独立环境可以避免 papermine 的依赖影响电脑上的其他 Python 项目。

接着把 papermine 的导入副本、分析结果、经验和缓存固定保存到 `D:\papermine-data`：

```powershell
[Environment]::SetEnvironmentVariable("PAPERMINE_HOME", "D:\papermine-data", "User")
$env:PAPERMINE_HOME = "D:\papermine-data"
```

第一条命令永久记录数据目录，第二条命令让设置在当前 PowerShell 窗口立即生效。以后新开的 PowerShell 会自动使用该目录。如果改用其他磁盘，请同样修改这两处路径开头的盘符。

如果 PowerShell 提示“禁止运行脚本”，先执行下面这条命令，只为当前 PowerShell 窗口临时允许脚本运行，然后再次激活：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 4. 安装 Python 与网页依赖

确认命令行开头有 `(.venv)`，然后执行：

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[web]"
cd web\frontend
npm ci
npm run build
cd ..\..
```

`npm run build` 可能需要几分钟。看到构建成功的提示后再继续。安装过程中需要联网；如果网络中断，请重新执行失败的那条命令。

`.[web]` 会同时安装 PDF、DOCX、PPTX 解析和本地中英文 OCR。OCR 组件首次下载约数十 MB，所需时间取决于网络速度；它不要求另外安装 Tesseract，也不会把待识别图片上传到第三方服务。

如果使用代理，可在执行安装命令时临时指定代理。例如代理位于本机 7897 端口：

```powershell
python -m pip install --proxy http://127.0.0.1:7897 -e ".[web]"
```

如果官方 Python 下载源速度很慢，可在保持代理的同时使用国内镜像：

```powershell
python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple --proxy http://127.0.0.1:7897 -e ".[web]"
```

### 5. 选择是否配置 DeepSeek API Key

papermine 支持两种使用方式：

#### 方式 A：暂不配置 API Key

不需要做任何额外设置，可以直接进入下一步启动。此时不会调用 LLM，也不会产生模型费用；系统会使用本地确定性规则完成分析，但结果会更模板化，语义理解和创新点评估能力较弱。

#### 方式 B：配置自己的 DeepSeek API Key（完整体验）

API Key 必须由使用者自己提供，调用费用归 Key 的拥有者。可以登录 [DeepSeek 开放平台](https://platform.deepseek.com/) 创建并管理 API Key；如账户没有可用额度，需要按平台提示充值。不要把 Key 发给他人，也不要提交到 GitHub。

在仓库根目录执行：

```powershell
Copy-Item .env.example .env
notepad .env
```

记事本打开后，把这一行：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

改成自己的真实 Key，例如：

```env
DEEPSEEK_API_KEY=你的真实DeepSeek_API_Key
```

保留以下默认配置，然后保存并关闭记事本：

```env
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

`.env` 已被 Git 忽略，不会随正常的 `git add` 或 `git push` 上传。若所在网络必须使用代理，可在 `.env` 中按自己的实际地址增加：

```env
PAPERMINE_PROXY=http://127.0.0.1:你的代理端口
```

### 6. 启动 Web 客户端

确保 PowerShell 当前位于 `papermine` 根目录，并且命令行开头有 `(.venv)`，执行：

```powershell
papermine web
```

程序会同时启动本地后端和网页，并自动打开浏览器。默认地址是：

<http://127.0.0.1:3000>

如果浏览器没有自动打开，请手动复制这个地址到浏览器。启动窗口需要保持开启；关闭 PowerShell 会导致网页服务停止。

如果提示无法识别 `papermine`，改用：

```powershell
python -m papermine web
```

### 7. 导入并分析自己的项目

1. 在网页左侧点击“新建分析”。
2. 选择“选择项目文件夹”或“上传 ZIP”。
3. 等待上传完成，检查导入预览、文件数量和安全排除提示。
4. 确认内容无误后，点击“确认并开始分析”。
5. 分析期间不要关闭网页或启动 Web 的 PowerShell 窗口。
6. 分析结束后会自动进入项目概览，可继续查看创新点、文献证据和论文路线图。

按照本教程安装后，导入项目副本保存在 `D:\papermine-data\imports\`，分析结果保存在 `D:\papermine-data\runs\`。papermine 不会修改用户选择的原始项目目录。敏感文件和常见依赖目录会默认排除。

> 说明：本教程保证 papermine 的项目代码、虚拟环境、网页依赖和主要运行数据位于 D 盘。Git、Python、Node.js 本身以及 pip/npm 的全局缓存仍可能按各自安装程序的默认设置使用少量 C 盘空间。

### 8. 关闭 Web 客户端

回到启动 papermine 的 PowerShell 窗口，按 `Ctrl+C`。前端和后端会一起关闭。浏览器中已经打开的页面随后将无法继续使用，这是正常现象。

### 9. 以后再次打开（日常启动）

打开新的 PowerShell，执行：

```powershell
Set-Location D:\papermine
.\.venv\Scripts\Activate.ps1
papermine web
```

如果最初选择了其他磁盘，把第一条命令开头的 `D` 改成实际盘符。

### 10. 更新到 GitHub 最新版本

先关闭正在运行的 Web 客户端，再执行：

```powershell
Set-Location D:\papermine
git pull origin main
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[web]"
cd web\frontend
npm ci
npm run build
cd ..\..
papermine web
```

### 常见问题

- **`git`、`python`、`node` 或 `npm` 无法识别**：确认对应软件已经安装，关闭所有 PowerShell 窗口后重新打开，再执行版本检查命令。
- **无法激活 `.venv`**：按第 3 步执行临时的 `Set-ExecutionPolicy` 命令；它只影响当前窗口。
- **`papermine` 无法识别**：确认已经激活 `.venv`，或使用 `python -m papermine web`。
- **提示前端依赖未安装**：进入 `web\frontend` 执行 `npm ci`，再回到根目录启动。
- **提示前端尚未构建**：进入 `web\frontend` 执行 `npm run build`。
- **端口 3000 或 8000 被占用**：使用 `papermine web --api-port 8100 --web-port 3100`，然后访问 <http://127.0.0.1:3100>。
- **没有 API Key**：程序仍能运行，并自动使用确定性降级模式；这不是安装失败。
- **配置了 Key 但分析失败**：检查 `.env` 中的 Key、网络连接、DeepSeek 账户余额以及代理设置，然后重新分析。
- **PDF、Word 或 PPT 没有识别出正文**：确认安装命令使用的是 `python -m pip install -e ".[web]"`；旧版 `.doc`、`.ppt` 不在当前支持范围内，请先用 Office 另存为 `.docx`、`.pptx`。
- **首次分析扫描件较慢**：扫描型 PDF 和文档内图片需要在本机逐页 OCR，速度取决于页数、图片尺寸和电脑性能；文本型 PDF 会直接读取文本层，通常更快。
- **D 盘不存在或想安装到其他盘**：把教程命令中路径开头的 `D` 统一改成已有盘符，例如 `E`；不要改路径中的其他内容。
- **想禁止自动打开浏览器**：使用 `papermine web --no-browser`。

默认情况下服务只监听 `127.0.0.1`，不会向局域网或公网开放。

## 输出报告

一份 Markdown 报告，包含：

- **项目叙事 + 研究问题**：把工程任务抽象成可研究问题（含"为何不是纯工程"）
- **候选创新点**：带 novelty 假设 + 文献引用
- **可行性评估**：证据驱动的打分 + verdict（proceed / rework / drop）
- **论文路线图**：论文类型、大纲、实验计划、时间线

## 目录结构

```
papermine/
  cli.py           命令行入口（analyze / resume / status / trace / web）
  web_launcher.py  本地 Web 双服务统一启动与关闭
  orchestrator.py  编排器（状态机 + 检查点 + 回退）
  dossier.py       研究档案（单项目唯一事实源，版本化）
  llm.py           LLM 抽象（DeepSeek + NullProvider 降级）
  experience.py    经验库（自进化层：策略 / effect / 生命周期）
  retrieval.py     文献检索（arXiv / Semantic Scholar / OpenAlex / Crossref / DBLP）
  policy.py        策略注入（结构化 policy + LLM 解释）
  agents/          六个 Agent + ⑦ 经验沉淀
  storage.py       数据目录（~/.papermine）读写
  report.py        Markdown 报告渲染
  extractor/       Python AST 静态分析 + 文档读取
examples/sample-project/   示例横向项目（工业预测性维护）
web/                       FastAPI API + Next.js 科研决策工作台
```

## 已知边界

- 无 DeepSeek key 时走确定性降级，产出较模板化（配 key 后为 LLM 语义级）
- 文献检索走 arXiv、Semantic Scholar、OpenAlex、Crossref 和 DBLP；每个研究方向目标 8 篇、最多 12 篇，并区分高度相关与部分相关
- 公开检索源可能限流或短暂不可用；系统会继续使用其他来源，最终不足 7 篇时明确显示“证据覆盖不足”
- 代码的 AST 静态分析目前仅覆盖 Python；其他代码语言仍以文本与关键词信号为主
- PDF 支持文本层与扫描页中英文 OCR；DOCX/PPTX 支持正文、表格、页眉页脚或演讲者备注及内嵌图片 OCR；旧版 `.doc`/`.ppt` 暂不支持
- 为避免异常文档耗尽内存或长时间无响应，单个文档默认最多抽取 20 万字符、100 页和 60 张内嵌图片；达到预算会保留已识别内容并记录截断提示

## 后续方向

- 团队 / 实验室共享经验库（`scope` 字段已预留）
- 向量检索（M1 当前是 applicability 标签/关键词匹配）
- MCP Server / Skill 外壳（核心引擎已就位）
- F3 结果信号闭环：论文发表结果回填 `effect`

## 开源与贡献

- 许可证：[MIT](LICENSE)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

欢迎提 Issue 与 Pull Request，详见[贡献指南](CONTRIBUTING.md)。
