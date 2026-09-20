# 无漏

「无漏」是一套面向**题湖题库（无漏 AI，`www.wulouai.com`）中考数学题目**的治理工作台：把题库页面上的题采下来，交给 AI 给出分类建议，再把确认后的结果移动进既有的中考数学目录体系。

仓库的**主项目是** [`wulou-question-curation-assistant/`](wulou-question-curation-assistant/)——前端 Tampermonkey 脚本对外叫 **AI分类助手**，后端是本机 FastAPI 服务。

```
wulou/
├─ wulou-question-curation-assistant/   ← 主项目：AI分类助手（油猴脚本 + 本地分类服务）
├─ wulou-teacher-pdf-downloader/        ← 配套：当前专题教师版 PDF 批量下载（无漏 AI）
├─ local-materials/                     ← 原始素材：PDF / 图片 / Excel / PPT、目录工作簿、台账
└─ tmp/                                 ← 一次性脚本与中间产物
```

主项目内部的独立文档（首次接触本项目请按顺序读）：

| 文档 | 作用 |
| --- | --- |
| [README.md](wulou-question-curation-assistant/README.md) | 功能全集与逐条行为说明（最详细，改动前必读） |
| [IMPLEMENTATION_PLAN.md](wulou-question-curation-assistant/IMPLEMENTATION_PLAN.md) | 实施规划与目录结构设计 |
| [MODEL_INTEGRATION_RESEARCH.md](wulou-question-curation-assistant/MODEL_INTEGRATION_RESEARCH.md) | 模型选型与两阶段分类方案的调研依据 |
| [PROTOCOL_COMPATIBILITY_RESEARCH.md](wulou-question-curation-assistant/PROTOCOL_COMPATIBILITY_RESEARCH.md) | 三类协议与兼容网关的适配调研 |
| [PERFORMANCE_OPTIMIZATION_REPORT.md](wulou-question-curation-assistant/PERFORMANCE_OPTIMIZATION_REPORT.md) | 性能优化记录 |

---

## 一、AI分类助手：两条分类链路，一个采纳出口

分类这件事，本项目允许两条路走，两条路的产物**都能落到同一个「采纳」出口**上。可以只用其中一条，也可以混用。

### 链路 A：前端直连 AI 实时分类（主要功能）

在前端按已配置的模型设置直接分类，这是本项目的主线。

1. 在题湖题目列表页点右侧「AI分类」入口，打开面板。
2. 点「识别当前页」——脚本采集当前页题卡（题干、答案、排版文本），整批提交给本机服务。
3. 本机服务按**两阶段方案**调模型：
   - 第一轮**专题路由**：先只发全局专题目录，判定这道题属于哪个专题。专题 10 之前按「最晚必备知识点」定位；专题 10「三角形」及之后的 `【大题】`，按**压轴题的最终核心突破口**定位，不因扇形面积、旋转、坐标、代数运算这些辅助步骤而转移。
   - 第二轮**专题内分类**：在命中的专题里选三级、四级目录，同时从全量专题复核一次路由，路由不一致会自动纠正（有重试上限）。
4. 每组结果一返回就写本地缓存并渲染题卡标记，面板每 2 秒轮询同一个作业状态，不需要浏览器长连接、没有前端硬超时。
5. 点「识别当前目录全部题目」会把当前目录范围内**全部分页**先读成一份快照，再作为一个总作业流水化处理（单作业保护上限 1000 题，超过才自动拆分）。后续「当前目录全部采纳」始终按这份快照走，不会因为题目被移出当前目录而跳页、漏题。

模型给出的结论保守优先：没有唯一且明确的既有目录时，一律标记「待人工复核」，不会自动进采纳队列。

### 链路 B：题库下载 → 本地 AI Agent 分类 → JSON 回填

适合整批、离线、要人工把关目录结构的场景。

1. **采题**：在题湖选中二级或三级目录，点「导出所有题库」。脚本遍历该范围全部分页、读取全部可用题目属性，在 `wulou-question-curation-assistant/.local-data/directory-exports/` 生成交接包：
   - `题库全集.jsonl`：去重后的完整题目文本；
   - `目录整理交接清单.json`：当前目录范围、采集缺口、现有专题二三四级目录树、交接说明与 `import_contract` 契约。
   这一步不调模型、不读不写 Excel。
2. **Agent 分类**：由人手动启动 ChatGPT Agent，使用 `math-exam-directory-curation` Skill 读交接包，产出目录方案与逐题归属，生成一份「题号 + E 列知识点编号」的 JSON / JSONL 清单。
3. **回填**：在面板点「导入归类结果」，选这份 JSON 文件。面板先**预演**并列出可归位题数与编号对不上的条目，确认后才写入本机 SQLite（确认框里「保留已有手工修正」默认勾选）。写完后本页立即重新读取，这页题显示为「Skill 归类」建议。
4. 之后按链路 A 同样的方式点「采纳」，才会真正写入题湖。

> 两条链路对齐的**唯一凭据是 Excel E 列的知识点编号**。本机目录 ID（形如 `l3-107-0ead8e99f1`）由行号推导、插行就全变，外部无法预知，所以不外传也不接收。编号是**署名制**，本机按 E 列原值**等值反查**，不校验前缀和格式。

### 一键采纳

建议确认无误后：

- 单题：点题卡上的「采纳」/「采纳并移动」。
- 整批：主面板「全部采纳」——当前页全量；在「当前目录范围」工作集下按钮变为「当前目录全部采纳」，按第 5 步的题目快照处理。

采纳时的行为是分开的，不会白跑网络：

- 建议目录**与当前目录一致**：只在本地确认，不读属性页、不发移动请求、不写工作成果台账。
- **需要移动**的题目：替换属性表单里的 `exercise_catalogue_id`，以 **3 路原生异步并发**逐题提交题湖 `POST /exercise/modifyData` 并回读确认。单题失败不中断其余题目，失败的保留为待采纳状态。
- 确认框会事先拆出「仅本地确认」和「并发提交移动」两类数量。
- 只产生真实的目录移动时，才把「原目录路径 → 目标路径 + Stable Code」追加到本机工作成果台账。

「待人工复核」的题目**永不**被一键采纳批量处理；脚本会进入无副作用的「审核优先视图」，把待复核题按题湖原本每页容量排到最前面，逐题人工改目录，改完退出视图恢复原始分页。

### 工作成果与人工修正

- **工作成果**：按 UTC+8 当天统计已分类题目数、涉及专题、每题当天最新一次的 Stable Code 与四层目录「原 → 现」。可「导出今日」生成独立 HTML 报告（可转发、可打印成 PDF），或「今日截图」生成 720px 宽 PNG 长图。
- **人工修正**：点题卡上的分类路径，按「专题 → 二级 → 三级 → 四级」逐级选择题湖已有目录（三级无子目录时三级即叶子）。人工结论与模型缓存**分表存储**，并有独立审计记录。清除本页缓存不会删除人工修正。
- **缓存**：缓存键 = 题目内容 + 目录版本 + 规则正文哈希 + 协议 + 模型名。题目移动后仍复用同一结论，不会因目录变化重复调模型；题卡的「已归位 / 已移动」是结合当前叶子和本机台账算出来的。

---

## 二、模型设置

前端**不直接调模型**。所有模型调用都在本机服务里发生，浏览器只跟 `127.0.0.1:3232` 说话，密钥也只留在本机配置文件里。设置入口在面板右上角「设置 → 模型设置」。

### 1. 模型方案（Profile）

- 可以新建多套方案（例如「GPT」「DeepSeek」「Claude」），各自独立保存**协议、接口地址、API 密钥、目录分类模型、专题选择模型、思考强度、兼容选项**。长按卡片可拖动排序，点选即切换。
- 至少保留一套，不能删到零。方案名不能重名。
- **「处理速度」是全部方案共用的一个全局值**，不属于单套方案。

### 2. 协议

| 选项 | 说明 |
| --- | --- |
| `Responses` | OpenAI Responses API |
| `Chat Completions` | OpenAI 兼容的 Chat Completions |
| `Claude Messages` | Anthropic Messages API，用 `output_config.format` 拿 JSON Schema 结构化结果 |

三种协议都支持实时分类和离线批处理：前两者走 OpenAI Batch，Claude Messages 走原生 Message Batches（按 `custom_id` 导入乱序返回的结果）。

### 3. 接口地址与密钥

- 接口地址填**根地址**即可，例如 `https://api.openai.com` 或 `https://api.anthropic.com`：服务在缺少版本路径时自动补一次 `/v1`，已经写了 `/v1`（或 `/v1beta`）不会重复添加。
- API 密钥只写入 Git 忽略的 `config/settings.local.yaml`，**不回传、不显示在浏览器**。密钥输入框留空即保留已存密钥；页面上只显示「已配置 / 未配置」。
- `chatgpt.com/backend-api/codex` 是 ChatGPT/Codex 的登录态内部接口，**不能**当 API Key 地址填。

### 4. 测试连接（可选）

- 点「测试连接」只请求模型目录 `/models`：不携带模型名、不发模型生成请求、**不消耗推理 token**，只判断「可连接 / 不可连接」。
- 测试成功后返回的模型名会自动成为「目录分类」「专题选择」两个选择框的选项。测试前也可以直接手填模型名。
- 改了协议、接口地址或密钥之后需要重新测试来刷新候选模型。
- 测试先返回任务号，再短轮询取结果，避免浏览器断连漏掉后端结论；详细诊断只留本机服务日志。

### 5. 模型与思考强度

- **目录分类模型**：必填。**专题选择模型**：可留空，留空即复用目录分类模型。
- 两个「思考强度」分别作用于目录分类和专题路由，可选 `none / low / medium / high / xhigh / max`。`max` 适合有人工挑选的高精度复核，不建议当整页默认值。

### 6. 处理速度（并发）

- UI 里的「处理速度 → 同时处理的请求」是**全局并发闸门**，取值 1~5，落盘为 `classifier.pipeline.max_concurrent_requests`。
- 前端所有请求池、服务端所有实时分类作业都受这一个值约束；服务内部仍以每 10 题为一个模型微批（路由与专题内分类各自成批）。
- 重试（`classifier.pipeline.retry_attempts`，1~5）只对网关过载、限流（429/5xx）和连接失败生效，指数退避叠加随机抖动，默认共尝试 3 次。**读超时不重发**——请求已经送达上游，重发既可能重复计费，也要再干等一个完整超时周期；这类失败会把该批题标记为待人工复核。离线批任务的**提交**不做重发（批任务没有幂等键，重发会让同一批题跑两遍、重复计费）。

### 7. 高级连接选项

- **请求兼容方式**：`标准`（默认）或 `兼容模式`。部分网关按客户端特征做访问限制，选「兼容模式」即套用兼容请求标识——不按供应商域名写死规则。
- **自定义请求头**：供应商另有要求时按「每行 `名称: 值`」填。
- 这两项**只作用于当前方案**，并被测试连接、实时分类、离线批处理共用。

### 8. 目录来源

设置面板里这一块**只有一个框**：框里显示当前生效的那份目录工作簿，**点一下**就在这台电脑上弹出系统窗口选文件夹，选完**直接应用进这个框**，没有第二个按钮、也不用填路径。

- **框里的字是缩略过的**，只写「哪个文件夹 · 哪一版」（例如 `导出目录 · ID-3777 · 2026-09-18 v2`），不塞完整路径——工作簿名长达四五十个字符，原样显示会把面板顶出横向滚动条。缩略规则：认出 `YYYY-MM-DD vN` 只留这一段（版本写在文件名尾巴上，必须留）；文件名里有 `ID-xxxx` 就带上，用来区分同一文件夹里的多组工作簿，位置不够就先舍它、绝不切版本号或文件夹名。**完整路径始终挂在鼠标悬停提示上**，框里也有 CSS 省略号兜底。

- **框里的路径是「锚点」的显示结果。** 落盘的 `directory_workbook.path` 记录「哪个文件夹 + 哪个命名前缀」，同一文件夹里「日期 + vN」最大的一版自动生效，不会顶掉手选的那份。
- **选文件夹就够，不用指到具体某一份。** 文件夹里只有一族 → 用它的最新版本；**有多族**（前缀不同，例如带不同题湖课程 ID）→ 服务端不替你猜，就地列出候选行让人点一份；一族都没有 → 状态行说明「文件名要写成 `名称 YYYY-MM-DD vN.xlsx`」。
- **API 层文件名和工作簿路径都收**（便于脚本调用）：`POST /api/v1/settings/directory-workbook` 的 `path` 可以是文件夹也可以是某一份 `.xlsx`；多族返回 `409 ambiguous_folder` 并带 `families`，空文件夹返回 `409 empty_folder`，两种拒绝都**不动配置一个字**。
- **弹窗由本机服务开**（`POST /api/v1/settings/directory-picker`，空请求体）：浏览器开不了本机对话框，也拿不到真实的本机绝对路径，所以由本机进程开系统窗口——tkinter 的 `askdirectory`（在 Windows 上就是系统自己的目录浏览框），缺 tkinter 时回退 Windows 自带的 .NET 对话框。同一时刻只准开一个窗口；窗口开着时前端等 5 分钟，等不到只算「这一轮没结果」，**不等于取消**。
- **失效就地报警**：锚点文件夹找不到、或那一族被搬走时，状态行显示红色原因，服务仍带着上一份能用的快照活着，前端才有机会改回来。

服务启动时按锚点发现最新工作簿，仅当 SHA-256 变化时才原子重建 `.local-data/taxonomy.yaml`；发现失败退回上一份快照并记 `taxonomy_sync_status=failed`，不会带着崩掉的服务硬起。

排障看 `GET /health` 的 `taxonomy_sync_status` / `taxonomy_sync_error` / `directory_workbook`（锚点）/ `directory_workbook_active`（实际生效）；候选清单看 `GET /api/v1/directory-workbooks`。

### 9. 离线批处理（默认隐藏）

「导出批处理 / 提交云端批处理 / 同步批处理结果」三个按钮保留但不显示在常用面板里——它只适合数百题以上的离线任务。每个批任务会绑定创建时的模型方案，任务完成前切换方案不影响该任务的提交与导入；**不要删除仍有待同步任务的方案**。提交动作会产生云端费用，浏览器会二次确认。

---

## 三、上手

### 0. 环境

- Python ≥ 3.13、[uv](https://docs.astral.sh/uv/)；前端脚本另外只需要一个装了 Tampermonkey 的浏览器。
- 依赖用 `uv` 管理（`pyproject.toml` + `uv.lock`），**不要**用 `pip install` 直接改环境。

```powershell
Set-Location .\wulou-question-curation-assistant
uv sync
```

### 1. 启动本地服务

```powershell
# 方式一：双击
.\wulou-question-curation-assistant\scripts\start-server.cmd

# 方式二：PowerShell
.\wulou-question-curation-assistant\scripts\start-server.ps1
```

默认监听 `127.0.0.1:3232`（`config/settings.example.yaml`），服务**只绑回环地址**，不需要访问令牌。

**全新机器开箱即起，但目录来源是空的**：出厂配置里 `directory_workbook` 整块是注释掉的，`.local-data/` 也还不存在，所以服务**不会**自动认任何一份目录工作簿——它只会拿 `config/taxonomy.example.yaml` 铺一份兜底快照，`taxonomy_sync_status=disabled`，等你到前端「设置 → 目录来源」点框选文件夹。这一点别搞错：脚本里没有任何硬编码路径，克隆到哪都不会自己找到 `local-materials`；那个目录只被当作**点框弹窗时的默认落点**（项目文件夹旁边的 `local-materials`）。自检：浏览器打开或终端请求

```text
http://127.0.0.1:3232/health
```

### 2. 装 AI分类助手

1. Tampermonkey 里新建脚本。
2. 把 [wulou-question-curation-assistant.user.js](wulou-question-curation-assistant/userscript/wulou-question-curation-assistant.user.js) 的**完整内容**粘贴进去保存。
3. 登录题湖题库，打开题目列表页并刷新；脚本会自动连本地服务，读取已保存的模型设置。
4. 点页面右侧「AI分类」入口 → 「设置」里配好模型方案 → 回来点「识别当前页」。

### 3. 配置文件

| 文件 | 说明 |
| --- | --- |
| `config/settings.local.yaml` | 模型名、接口地址、API 密钥、并发、缓存位置、目录工作簿锚点。Git 忽略，首次保存设置时自动生成 |
| `config/taxonomy.example.yaml` | 目录树示例（实际运行读 `.local-data/taxonomy.yaml`） |
| `config/classification-rules.yaml` | 从 `math-exam-directory-curation` Skill 提炼的可执行规则快照 |
| `config/basket-profiles.example.yaml` | 12 个组卷篮映射示例 |
| `schemas/*.json` | 题目、分类结果、目录方案的 JSON Schema |

---

## 四、验证

```powershell
uv run --project .\wulou-question-curation-assistant python -m unittest discover -s .\wulou-question-curation-assistant\tests\server -v
node --test .\wulou-question-curation-assistant\tests\userscript\userscript.test.cjs
```

改动服务端或前端脚本后，这两条都要跑过再交。

---

## 五、硬约定与边界

**架构**

1. 前端**不调 LLM**，模型调用全部发生在 `server/`；密钥只存 `config/settings.local.yaml`。
2. 对题湖的**唯一写操作**是 `POST /exercise/modifyData`；脚本只读当前登录权限允许访问的题目。
3. 服务只监听 `127.0.0.1`。

**跨端对齐**

4. 本机与外部 Skill 之间只用 **Excel E 列知识点编号**对齐；题湖目录 ID 不进 taxonomy，`cache_key` 由服务端算。
5. **E 列编号只做等值比对，禁止格式校验**——编号是署名制，现存前缀五花八门（`ZCSQG`、`ZCSZKH`、`ZCSQGLQ`……）全是合法编号。
6. Excel 写入必须**生成新版本**，绝不覆盖基准工作簿；新增或推翻目录后的逐题归类由 Skill 产出、人工审核，页面只负责导出与回填。

**数据安全**

7. 服务只保存脱敏后的模型输入快照（题干、答案、备用文本/LaTeX、排版解析警告），不保存页面 DOM、图片 URL、令牌或密钥。
8. 题目结果、人工修正、移动台账分表存储，审计语义不混；编号对不上的条目逐条列出原因并跳过，不做整批作废。

**未做的事（有意为之）**

9. 未配密钥时，服务安全回退到保守的规则分类器，**绝不擅自向外发送题目**；模糊题目不会被自动加入组卷篮。
10. 加入组卷篮、写入 Excel 不会在页面上自动执行。
