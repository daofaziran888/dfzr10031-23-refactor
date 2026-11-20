# 数据分析实验与数据应用服务平台

面向指标口径管理、分析实验设计、OLAP查询、数据看板构建与数据API发布的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编号、指标名称、计算公式 |
| 维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度类型 |
| 数据集管理 | `dataset` | 分析数据集 | 数据集编号、数据集名称、来源类型 |
| 分析实验 | `experiment` | 实验方案 | 方案编号、实验名称、分析数据集 |
| OLAP查询 | `query_builder` | 查询模板 | 模板编号、模板名称、查询语句 |
| 数据看板 | `dashboard` | 看板配置 | 看板编号、看板名称、包含图表 |
| 图表配置 | `chart` | 图表 | 图表编号、图表名称、图表类型 |
| 数据API发布 | `data_service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 取数参数绑定 | `fetch_service` | 取数模板 / 报表 | 模板编号、参数定义、数据范围、空值含义 |
| 智能告警 | `alert` | 告警规则 | 规则编号、告警名称、监控指标 |
| 分析用户组 | `user_group` | 用户组 | 组编号、组名称、组描述 |
| 数据快照 | `snapshot` | 快照记录 | 快照编号、数据集名称、快照时间 |
| 任务调度 | `scheduler` | 调度任务 | 任务编号、任务名称、调度类型 |
| 缓存管理 | `cache` | 缓存配置 | 缓存编号、缓存目标、缓存策略 |
| 分析管道 | `pipeline` | 分析节点 | 节点编号、输入数据集、分析算子 |
| 分析模型 | `model` | 模型配置 | 模型编号、模型名称、模型类型 |
| 数据集市 | `data_mart` | 集市表 | 表编号、表名称、主题域 |
| 数据导出 | `export` | 导出任务 | 任务编号、导出对象、导出格式 |
| 数据审计 | `audit` | 审计记录 | 记录编号、操作类型、操作人 |

## 取数参数绑定契约

OLAP 查询列表与数据 API 列表原来各写了一遍“把查询串转成过滤条件”。现在这段规则
收敛在 `backend/app/services/parameter_binding.py`，两处列表都改用它，历史行为不变。

- **列表语义不变**：`keyword` / `status` 为空串或未传时不参与过滤（纯空白仍按历史参与），
  扩展字段 `strip()` 后为空则不参与；字符串 `"0"` 仍然是有效过滤值。
- **参数绑定清单**：`POST /api/fetch/parameter-bindings` 为每个条件保留输入原文、
  业务类型（`string` / `number` / `boolean`）和存在形态（`missing` / `empty_string` /
  `zero` / `value`）。未填写、空字符串、数值零是三种不同的输入，请求指纹也不同。
- **空值含义由模板记录**：参数定义里用 `empty_means: all | empty` 写明空串是
  “不筛选”还是“字段为空”，结果里会带上数据范围（`scope`）。
- **范围冲突**：同一变量被绑到不同数据范围时返回 `PARAM_SCOPE_CONFLICT`，不会当成
  同一次请求复用；请求指纹包含范围。
- **覆盖计算**：`POST /api/fetch/data_service/{id}/coverage` 只抽取查询语句实际引用
  的条件，返回覆盖的商品分组、空值记录数（按字段拆分）和待复核的含混模板。
- **跨服务策略**：`POST /api/fetch/compare` 默认比较 `all`（空串一律不筛选，返回全组）
  与 `defer`（暂缓含未确认空值含义的模板）。`confirm` 策略下必须先给出确认才能执行；
  `local` 策略沿用各模板记录的局部含义并在结果里标注范围。含未确认模板的报表在
  `POST /api/fetch/reports/{id}/deliver` 会被拦下。
- **报表快照**：交付时冻结参数、模板定义版本与覆盖结果。之后模板定义更正只追加
  重算记录（`recalculations`），交付结果仍可通过 `GET /api/fetch/reports/{id}` 回查，
  用 `POST .../recalculate` 可按当时参数重算并与交付结果对照。

回归与演示：

```bash
cd backend
pip install -r requirements-dev.txt
python3 -m pytest tests -q
python3 scripts/verify_fetch_contract.py
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
