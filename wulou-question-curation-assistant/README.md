# 题湖题库数学题分类助手

本项目由 Tampermonkey 脚本和本地分类服务组成，用于在题湖题库页面上采集大题、展示分类建议，并把题目送入既有的中考数学目录体系。

第一版已经提供：

- 本地服务，默认监听 `127.0.0.1:3232`。
- 目录与分类规则配置校验。
- 题目结果 SQLite 缓存。
- 当前页分类缓存的一键清除；清除后下次识别会重新请求云端模型。
- 实时分类作业：浏览器只提交一次本地任务并轮询单一状态接口，已完成题目会立即显示，不依赖浏览器长连接。
- 一键采纳：按完整目录路径解析题湖内部目录 ID，提交题目当前完整属性表单并回读确认，不触发整页刷新。
- 人工修正：点击题卡的建议分类，逐级选择题湖已有目录后再一键采纳；人工路径独立持久化并保留审计记录。
- 页面刷新后自动从本地缓存恢复已有分类建议，并根据题湖当前目录识别已采纳状态。
- 保守的规则分类器：没有唯一且明确的既有目录时，一律标记为人工复核。
- 油猴脚本的页面采集、右侧折叠面板、当前页批量识别和题卡分类标记。

尚未接入模型时，规则分类器不会把模糊题目自动加入组卷篮；这是有意的保护措施。

分类模型采用“专题路由 → 专题内分类 → 本地确定性校验 + 云端 Batch API”的混合方案。实时分类先只发送全局专题目录，按 Skill 的“最晚必备知识点”定位专题；再按实际命中的专题分组，只发送该专题下的 `【大题】` 目录。每组完成即写入本地缓存并显示题卡标记。完整调研和选型依据见 [MODEL_INTEGRATION_RESEARCH.md](MODEL_INTEGRATION_RESEARCH.md)。本项目不使用本地模型。

实时模型、批处理 JSONL、批处理提交、结果同步导入和目录候选计数均已实现。未设置云模型密钥时，服务会安全回退到保守的规则分类，绝不会擅自向外发送题目。

## 依赖管理

本子项目使用 `uv` 管理全部 Python 依赖，配置和锁文件位于本目录。首次运行：

```powershell
Set-Location .\wulou-question-curation-assistant
uv sync
```

增加、删除或升级依赖时使用 `uv add`、`uv remove` 和 `uv lock --upgrade-package <包名>`，不要直接使用 `pip install` 修改环境。

## 启用云端分类

1. 启动本地服务。
2. 在油猴面板分别填写“目录分类”和“专题路由”的模型、推理强度，以及接口地址和 API 密钥。专题路由模型可留空以复用目录分类模型，但推理强度仍独立：目录分类建议 `high`，专题路由建议 `medium`。密钥只写入 Git 忽略的 `settings.local.yaml`，已保存的密钥不会回传或显示在浏览器。
3. 后续可随时改模型名、接口地址或重新填写密钥覆盖旧值；留空密钥输入框会保留已保存的密钥。
4. 日常处理页面时点击“识别当前页”即可。批处理 JSONL、提交和结果同步能力保留在本地服务中，适合后续一次处理数百题以上的离线任务；为保持面板简洁，默认不在油猴主面板显示。提交动作会在浏览器再次确认，因为它会产生云端费用。

默认适配器使用 OpenAI Responses API 与 Batch API。`base_url` 应填写 API 根路径，例如官方接口是 `https://api.openai.com/v1`；兼容网关若把版本前缀暴露在路径中，也应填写到 `/v1`。目录分类与专题路由的推理强度会分别按 Responses 的 `reasoning.effort` 发送。可选值为 `none`、`low`、`medium`、`high`、`xhigh`、`max`；`max` 适合有人工挑选的高精度复核，不建议作为整页默认值。

整页实时分类没有浏览器端硬超时。一分钟是常态性能目标，模型排队或网络波动时作业会继续在本机后台运行；服务端网络失联保护至少为 600 秒，不作为页面分类的业务时限。
专题路由和专题内分类每批最多 10 道题、各阶段最多并行 3 个云端请求。服务端会把固定 Skill 和目录放在动态题目前；支持精确前缀缓存的兼容网关可复用固定上下文。模型调用结束后会持续把完成题目写入缓存，油猴脚本每 2 秒轮询同一个作业状态并立即渲染新结果。
分类结果以“题目 ID + 当前题湖目录叶子 ID”为主键。题湖的该叶子可以是三级或四级目录：同一题仍在同一目录时，题干、答案、题目图片、目录版本、规则正文哈希、云端提供方协议及模型名用于校验缓存有效性，重新分类会覆盖旧结果；将同一题移到新的三级或四级目录后，会保留为一条独立记录。题湖页面的动态展示文字和页面目录提示不参与缓存键，刷新页面后可以稳定恢复同一道题的建议。

三级目录没有四级子目录时，三级本身就是最终分类目录；只有实际存在四级子目录时才要求四级唯一匹配。每次新分类还会保存经过脱敏的模型文本输入快照：题干、答案、可用的备用文本或 LaTeX，以及排版解析警告；不保存页面 DOM、图片 URL、令牌或 API 密钥。待复核题目的分类框中可展开“查看模型输入”，直接对照模型实际读取的文本。

## 启动本地服务

1. 双击 [start-server.cmd](D:\Coding\wulou\wulou-question-curation-assistant\scripts\start-server.cmd) 即可启动。

2. 或在 PowerShell 启动：

   ```powershell
   .\wulou-question-curation-assistant\scripts\start-server.ps1
   ```

3. 浏览器访问或在终端请求：

   ```text
   http://127.0.0.1:3232/health
   ```

服务只能监听本机回环地址 `127.0.0.1`，不需要设置访问令牌。保存云端模型设置时会自动创建 Git 忽略的 `settings.local.yaml`。

## 安装油猴脚本

1. 在 Tampermonkey 新建脚本。
2. 将 [wulou-question-curation-assistant.user.js](userscript/wulou-question-curation-assistant.user.js) 的完整内容粘贴并保存。
3. 登录题湖题库，进入题目列表页面并刷新。脚本会自动连接本地服务，读取已持久化的云端模型、接口地址和密钥状态。
4. 点击页面右侧的“分类”入口。脚本会读取题湖页面标题作为弱提示；云端模型仍会从全部专题中按“最晚必备知识点”重新判断，再点击“识别当前页”。

建议分类确认无误后，点击题卡上的“一键采纳”。脚本只替换该题属性表单中的 `exercise_catalogue_id`，提交成功后重新读取题目属性；只有回读目录与建议目录一致时才显示“已采纳”。目录路径缺失、重名或无法与题湖目录树唯一匹配时不会提交。

发现模型建议不正确时，直接点击题卡中的分类路径。选择器会按“专题 → 二级目录 → 三级目录 → 四级目录”逐级展示题湖已有目录；三级目录没有子目录时可直接作为最终路径。选择完成后，题卡会显示“人工修改”，点击“一键采纳”即可写入题湖。人工路径仅在题湖回读成功后保存到 SQLite：它与模型缓存分表存储，并按“题目 ID + 修改前题湖目录叶子 ID”区分语境；每次人工采纳都会追加审计记录。清除本页缓存不会删除人工修正。

如果需要重新判断当前页面的题目，可点击“清除当前页缓存”并确认；操作只删除该页题目在本机的分类缓存，不会影响其他题目或云端设置。

脚本只读取当前登录权限已经允许访问的题目。加入组卷篮和写入 Excel 不会在当前版本自动执行。

## 配置文件

- `config/taxonomy.example.yaml`：目录树示例。实际运行时应复制为本地版本或从已确认的 Excel 导出。
- `config/classification-rules.yaml`：从 `math-exam-directory-curation` Skill 提炼的可执行规则快照。
- `config/basket-profiles.example.yaml`：12 个组卷篮映射示例。
- `config/settings.local.yaml`：云端模型名、接口地址、API 密钥和本机缓存位置，不纳入 Git。

## 验证

```powershell
uv run --project .\wulou-question-curation-assistant python -m unittest discover -s .\wulou-question-curation-assistant\tests\server -v
node --test .\wulou-question-curation-assistant\tests\userscript\userscript.test.cjs
```

## 当前边界

- 新三级、四级目录只能作为候选提出，必须通过数量、旧目录对照、语义审核和人工确认。
- Excel 写入必须生成新版本，不能覆盖基准工作簿。
- `SF` 编号遵循 `ZCSQG<YYYYMMDD>SF<NN>`；只有通过审核的新增知识实体才能占用编号。

## Excel 目录预检

先只读检查工作簿，不会修改任何文件：

```powershell
uv run --project .\wulou-question-curation-assistant python .\wulou-question-curation-assistant\scripts\preview-excel-update.py <基准工作簿路径> '专题一 实数' --output .\excel-preview.json
```

`server/excel_sync.py` 的写入函数仅接受人工审核为 `approved` 的明确方案，并拒绝覆盖基准或已存在的目标文件；在尚未提供实际基准工作簿前，不会生成正式 Excel 版本。

从确认的目录工作簿导出真实分类目录后，将 `settings.local.yaml` 的 `taxonomy_path` 改为该文件：

```powershell
uv run --project .\wulou-question-curation-assistant python .\wulou-question-curation-assistant\scripts\export-taxonomy.py <基准工作簿路径> --output .\wulou-question-curation-assistant\.local-data\taxonomy.yaml
```

导出器严格按工作簿列层级读取：A 列是一级专题，B 列是二级目录，C 列是三级目录，D 列是四级目录；`【大题】`固定是二级目录。形如 `12.4 一般角的三角函数值` 的 B 列标题不会被提升为一级专题。导出格式版本包含在 `taxonomy_version` 中，解析规则变化会自动使旧分类缓存失效。
