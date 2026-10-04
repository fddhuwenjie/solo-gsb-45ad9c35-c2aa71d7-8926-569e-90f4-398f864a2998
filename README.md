# 口述史发布许可校验台

本地运行的口述史发布许可校验工具（Python + Flask + SQLite + 原生 JavaScript，无外部服务）。

录入访谈时长、说话人发言区间，以及每位说话人的授权区间（允许媒介 / 拒绝媒介 / 解禁日期），
再指定发布日期与版本媒介（音频或文字），系统即按发言与授权边界切分时间轴，逐段判定是否可发布，
并以时间码（HH:MM:SS）展示结果。

## 校验规则

1. **切分**：时间轴按访谈首尾、所有发言区间边界、所有授权区间边界切分为基本区间。
2. **覆盖**：每个基本区间必须被该区间内**所有发言人**的有效授权覆盖，缺任何一人即不可发布。
3. **拒绝优先**：任一授权拒绝目标媒介即不可发布；同一发言人允许与拒绝并存记为**授权冲突**，同样按拒绝处理。
4. **缺项**：无授权记录、或授权未涵盖目标媒介，均判为不可发布。
5. **未解禁**：解禁日期晚于发布日期时不可发布（解禁日当天可发布）。
6. **无发言**：无发言区间无需授权，视为可发布。
7. **合并**：相邻且结果（状态 + 原因）一致的区间自动合并为一个区间展示。

## 快速开始（单命令）

### Docker Compose（推荐）

```bash
docker compose up --build
```

### Docker

```bash
docker build -t oral-history-checker . && docker run --rm -p 8000:8000 -v oral_history_data:/data oral-history-checker
```

启动后打开 <http://localhost:8000>。

### 本地运行（无需 Docker）

```bash
pip install -r requirements.txt
python app.py          # 默认监听 8000 端口，可用 PORT 环境变量修改
```

## 使用流程

1. **访谈**：新建访谈（标题 + 时长 HH:MM:SS），可随时修改或删除。
2. **说话人**：添加访谈中出现的说话人。
3. **发言区间**：录入每位说话人的发言起止时间码（HH:MM:SS）。
4. **授权区间**：录入每位说话人的授权起止时间码、允许媒介、拒绝媒介、解禁日期（留空表示立即可用）。
5. **发布校验**：选择发布日期与媒介（音频 / 文字），点击「开始校验」，
   查看时间轴色带（绿=可发布，红=不可发布，灰=无发言）与逐段时间码结果及原因。
6. **补件并复核**：校验结果下方的批次编辑器中逐行录入待补授权
   （说话人、半开时间区间、允许 / 拒绝媒介、解禁日期），先点「试算（不写入）」
   查看合并前后的时间轴对比；确认整条时间轴均可发布后点「补件并写入」一次性落库。
   仍有阻断时不落任何记录，并列出每个阻断区间及原因。
7. **发行凭据**：正式发行前点「校验并封存凭据」—— 系统在同一事务中读取
   时长、说话人、发言与授权并按既有规则校验，仅当整条时间轴全部可发布时
   才保存不可修改的凭据（封存当时的完整输入与校验结果），失败不留记录。
   凭据列表实时标注当前状态（与发行时一致 / 待复核 / 来源缺失），
   点「查阅」可对比发行时结论与当前复核结果。
8. **渠道投放台账**：选择已封存凭据，录入渠道名与投放链接后点「登记投放」——
   系统在同一写事务中复核来源，仅当原访谈仍在、资料与封存时一致且当前整段
   可发布时才保存「投放中」记录及登记时间；否则说明原因且不留记录。
   台账列表 / 详情在一致读取快照中展示投放状态与凭据当前风险：
   资料后来变化即提示「需处理」（即便仍可发布），来源删除则提示「来源缺失」，
   历史链接、登记时间与封存结论始终保留。确认渠道内容已撤下后，
   在详情中填写原因即可标记「已撤下」（保留撤下时间；风险提示仍可查，凭据不改动）。

访谈、说话人、发言与授权记录均可在页面上直接修改、保存或删除；
发行凭据封存后不可修改、不可删除，原访谈删除后仍可查阅。

## 补件并复核（批量补授权）

`POST /api/interviews/<id>/remediate` 针对一个访谈、发布日期和媒介提交一批待补授权。
系统在同一 SQLite 事务（`BEGIN IMMEDIATE`）中：

1. 复制当前授权，按既有规则（拒绝优先、解禁日期、所有发言人覆盖）试算；
2. 校验整批补件 —— 同一说话人的补件时间区间重叠、同一补件允许与拒绝并存、
   区间越界（超出访谈时长）或说话人不属于该访谈，均令**整批失败**（HTTP 400）；
3. 把整批补件合并后重算；
4. 仅当整条时间轴全部可发布且 `commit` 为真时才一次性写入全部补件；
   否则回滚，不落任何记录（HTTP 409，返回每个阻断区间及原因）。

请求体：

```json
{
  "publication_date": "2026-10-04",
  "medium": "audio",
  "commit": false,
  "supplements": [
    {"speaker_id": 1, "start_seconds": 300, "end_seconds": 600,
     "allow_media": ["audio"], "deny_media": [],
     "embargo_until": null, "note": "补签授权"}
  ]
}
```

- `commit=false`（默认）：仅试算，返回合并前（`before`）与合并后（`after`）的
  时间轴区间、汇总与 `blocked` 阻断列表，不写库。
- `commit=true`：全部可发布才写入，成功结果含 `inserted_license_ids`
  与变更后的区间（`after.intervals`）；否则返回 409 与每个阻断区间及原因。

调用示例（先建访谈、说话人与发言区间，再补件）：

```bash
# 准备数据：600 秒访谈，说话人 #1 发言 0-600，授权仅覆盖 0-300
curl -s -X POST http://localhost:8000/api/interviews \
  -H 'Content-Type: application/json' \
  -d '{"title": "示例访谈", "duration_seconds": 600}'
curl -s -X POST http://localhost:8000/api/interviews/1/speakers \
  -H 'Content-Type: application/json' -d '{"name": "张三"}'
curl -s -X POST http://localhost:8000/api/interviews/1/segments \
  -H 'Content-Type: application/json' \
  -d '{"speaker_id": 1, "start_seconds": 0, "end_seconds": 600}'
curl -s -X POST http://localhost:8000/api/speakers/1/licenses \
  -H 'Content-Type: application/json' \
  -d '{"start_seconds": 0, "end_seconds": 300, "allow_media": ["audio"]}'

# 试算（不写入）：补 300-600 的音频授权
curl -s -X POST http://localhost:8000/api/interviews/1/remediate \
  -H 'Content-Type: application/json' \
  -d '{"publication_date": "2026-10-04", "medium": "audio", "commit": false,
       "supplements": [{"speaker_id": 1, "start_seconds": 300, "end_seconds": 600,
                        "allow_media": ["audio"], "note": "补签授权"}]}'

# 补件并写入：整条时间轴可发布，一次性落库
curl -s -X POST http://localhost:8000/api/interviews/1/remediate \
  -H 'Content-Type: application/json' \
  -d '{"publication_date": "2026-10-04", "medium": "audio", "commit": true,
       "supplements": [{"speaker_id": 1, "start_seconds": 300, "end_seconds": 600,
                        "allow_media": ["audio"], "note": "补签授权"}]}'
```

## 发行凭据（可追溯封存）

正式发行前，可为「访谈 + 发布日期 + 媒介」封存一份**不可修改**的发行凭据。
`POST /api/interviews/<id>/credentials` 在同一 SQLite 事务（`BEGIN IMMEDIATE`）中：

1. 读取访谈时长、说话人、发言区间与授权区间，按既有规则校验整条时间轴；
2. **只有全部区间可发布才落库**：凭据封存当时的完整输入
   （时长 / 说话人 / 发言 / 授权）与校验结果（合并区间 + 汇总）；
3. 仍有阻断区间时回滚，**失败不留任何记录**
   （HTTP 409，返回每个阻断区间及原因）。

凭据不可修改、不可删除（数据库触发器保证），也不随原访谈删除而消失。

查阅凭据（`GET /api/credentials/<id>`）时保留发行时结论，并在一致的读取快照
（同一事务）中比较现有资料、重新校验，得出当前状态：

| 状态 | 含义 |
| --- | --- |
| `valid` 与发行时一致 | 时长、说话人、发言、授权均无变化 |
| `review` 待复核 | 任一资料有变化 —— 即便重新校验仍可发布；`current.blocked` 列出当前阻断区间 |
| `missing` 来源缺失 | 原访谈已删除；封存的发行时结论仍可查看（`current` 为 null） |

```bash
# 封存凭据（沿用上文已补全授权的访谈 #1）
curl -s -X POST http://localhost:8000/api/interviews/1/credentials \
  -H 'Content-Type: application/json' \
  -d '{"publication_date": "2026-10-04", "medium": "audio"}'

# 凭据列表（含当前状态）与凭据详情（发行时结论 + 当前复核）
curl -s http://localhost:8000/api/credentials
curl -s http://localhost:8000/api/credentials/1
```

详情响应要点：

- `sealed.inputs` / `sealed.result`：封存的完整输入与发行时校验结论（不可修改）；
- `status` / `status_label` / `changes`：当前状态与发生变化的资料类别
  （时长 / 说话人 / 发言 / 授权）；
- `current.intervals` / `current.blocked`：按封存的发布日期与媒介对现有资料
  重新校验的结果与当前阻断区间；来源缺失时 `current` 为 null。

## 渠道投放台账

为已封存的发行凭据登记渠道投放。`POST /api/credentials/<id>/distributions`
在同一 SQLite 写事务（`BEGIN IMMEDIATE`）中复核来源，仅当同时满足以下条件时
才保存「投放中」记录与登记时间：

1. 原访谈仍在（否则原因 `source_missing`）；
2. 时长 / 说话人 / 发言 / 授权与封存快照完全一致
   （任何变化即原因 `source_changed`，即便重新校验仍可发布）；
3. 按封存的发布日期与媒介重新校验，整条时间轴全部可发布
   （存在阻断区间即原因 `currently_blocked`）。

任一条件不满足都回滚、**不留任何记录**（HTTP 409，`error` 给出中文原因，
`reason` 为机器可读代码，`credential` 附当前复核视图与阻断区间）。
同一凭据在同一渠道已有「投放中」记录时拒绝重复登记（原因 `duplicate_active`）。
渠道名不能为空，投放链接需为 `http(s)://` 开头的完整链接，否则 HTTP 400。

列表（`GET /api/distributions`）与详情（`GET /api/distributions/<id>`）在
一致的读取快照（同一事务）中展示投放状态与凭据当前风险：

| 凭据风险 | 含义 |
| --- | --- |
| `ok` 与发行时一致 | 来源资料未变化，当前整段可发布 |
| `action` 需处理 | 资料后来有变化 —— 即便仍可发布；`credential.changes` 列出变化类别 |
| `missing` 来源缺失 | 原访谈已删除；历史链接、登记时间与封存结论仍保留（`current` 为 null） |

确认渠道内容已撤下后，`POST /api/distributions/<id>/withdraw` 填写撤下原因
（必填），将记录标记为「已撤下」并保留撤下时间；风险提示仍可查，
**凭据本身不改动**。已撤下的记录不能重复撤下（HTTP 409）。台账记录不可删除，
登记信息（凭据 / 渠道 / 链接 / 登记时间）不可修改（数据库触发器保证）。

```bash
# 登记渠道投放（沿用凭据 #1；同一写事务内复核来源）
curl -s -X POST http://localhost:8000/api/credentials/1/distributions \
  -H 'Content-Type: application/json' \
  -d '{"channel": "音频平台 A", "url": "https://channel.example.com/a/1"}'

# 复核未通过时（示例：资料与封存时不一致）返回 HTTP 409 且不留记录：
# {"created": false, "reason": "source_changed",
#  "error": "资料与封存时不一致（变化：授权），未登记投放记录",
#  "changes": ["授权"], "credential": {...当前风险与阻断区间...}}

# 台账列表（含投放状态与凭据当前风险）与详情
curl -s http://localhost:8000/api/distributions
curl -s http://localhost:8000/api/distributions/1

# 确认渠道内容已撤下：填写原因并标记（保留撤下时间，凭据不改动）
curl -s -X POST http://localhost:8000/api/distributions/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason": "授权方要求撤下"}'
```

详情响应要点：

- `status` / `status_label`：投放状态（投放中 / 已撤下）；
- `channel` / `url` / `registered_at`：登记的渠道、投放链接与登记时间；
- `withdrawn_at` / `withdraw_reason`：撤下时间与原因（投放中为 null）；
- `credential.risk` / `credential.risk_label`：凭据当前风险（需处理 / 来源缺失）；
- `credential.sealed`：封存的完整输入与发行时结论（不可修改，始终保留）；
- `credential.current`：一致快照中按封存日期与媒介重新校验的结果；
  来源缺失时为 null。

## 数据持久化

- 容器内数据库为 `/data/oral_history.db`，Compose / 运行命令已挂载卷 `oral_history_data`，重启不丢数据。
- 本地运行时数据库为 `./oral_history.db`，可用环境变量 `DB_PATH` 指定其他路径。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/interviews` | 访谈列表 |
| POST | `/api/interviews` | 新建访谈 `{title, duration_seconds}` |
| GET | `/api/interviews/<id>` | 访谈详情（含说话人 / 发言 / 授权） |
| PUT / DELETE | `/api/interviews/<id>` | 修改 / 删除访谈 |
| POST | `/api/interviews/<id>/speakers` | 添加说话人 `{name}` |
| PUT / DELETE | `/api/speakers/<id>` | 修改 / 删除说话人 |
| POST | `/api/interviews/<id>/segments` | 添加发言区间 `{speaker_id, start_seconds, end_seconds}` |
| PUT / DELETE | `/api/segments/<id>` | 修改 / 删除发言区间 |
| POST | `/api/speakers/<id>/licenses` | 添加授权 `{start_seconds, end_seconds, allow_media, deny_media, embargo_until, note}` |
| PUT / DELETE | `/api/licenses/<id>` | 修改 / 删除授权 |
| POST | `/api/interviews/<id>/evaluate` | 校验 `{publication_date, medium}`，返回合并后的时间码区间与原因 |
| POST | `/api/interviews/<id>/remediate` | 补件并复核 `{publication_date, medium, commit, supplements}`，同一事务试算 + 满足条件才一次性写入 |
| POST | `/api/interviews/<id>/credentials` | 封存发行凭据 `{publication_date, medium}`：同一事务校验，全部可发布才落库，失败不留记录 |
| GET | `/api/credentials` | 凭据列表（含当前状态：与发行时一致 / 待复核 / 来源缺失） |
| GET | `/api/credentials/<id>` | 凭据详情：发行时结论 + 一致读取快照中的当前复核（变化类别、当前阻断区间） |
| POST | `/api/credentials/<id>/distributions` | 登记渠道投放 `{channel, url}`：同一写事务复核来源，来源缺失 / 资料变化 / 当前不可发布均不留记录（409） |
| GET | `/api/distributions` | 投放台账列表（投放状态 + 凭据当前风险：需处理 / 来源缺失） |
| GET | `/api/distributions/<id>` | 投放详情：链接、登记 / 撤下时间、撤下原因、凭据封存结论与当前风险 |
| POST | `/api/distributions/<id>/withdraw` | 确认撤下 `{reason}`：标记已撤下并保留撤下时间，凭据不改动 |

## 运行测试

```bash
python -m unittest test_evaluator.py -v   # 核心校验逻辑 + 补件批次校验（纯 Python）
python -m unittest test_remediation.py -v # 补件事务编排（纯 Python + SQLite）
python -m unittest test_credentials.py -v # 发行凭据事务编排（纯 Python + SQLite）
python -m unittest test_channels.py -v   # 渠道投放台账事务编排（纯 Python + SQLite）
python -m unittest test_remediate_api.py -v  # 补件接口 HTTP 层（需 Flask，Docker 镜像内可运行）
python -m unittest test_credential_api.py -v # 凭据接口 HTTP 层（需 Flask，Docker 镜像内可运行）
python -m unittest test_channel_api.py -v # 渠道台账接口 HTTP 层（需 Flask，Docker 镜像内可运行）
node test_frontend.js                     # 前端交互冒烟测试（可选，需 Node）
```

## 项目结构

```
app.py               Flask 应用与 REST API（SQLite 持久化）
evaluator.py         核心校验逻辑：切分时间轴、逐段判定、相邻合并、补件批次校验
remediation.py       补件并复核：同一 SQLite 事务中的试算与一次性写入
credentials.py       发行凭据：同一事务中校验并封存，查阅时在一致快照中复核现状
channels.py          渠道投放台账：同一写事务复核来源后登记，快照展示风险，撤下留痕
test_evaluator.py    核心逻辑单元测试
test_remediation.py  补件事务编排单元测试（不依赖 Flask）
test_credentials.py  发行凭据事务编排单元测试（不依赖 Flask）
test_channels.py     渠道投放台账事务编排单元测试（不依赖 Flask）
test_remediate_api.py 补件接口 HTTP 层测试（需 Flask）
test_credential_api.py 凭据接口 HTTP 层测试（需 Flask）
test_channel_api.py  渠道台账接口 HTTP 层测试（需 Flask）
templates/index.html 单页界面
static/app.js        前端交互（原生 JavaScript）
static/style.css     样式
Dockerfile           一体化镜像（gunicorn 运行）
docker-compose.yml   单命令启动
```
