# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录
- `GET /api/incidents/{id}/timeline`

## 海况修订

海况突变后，旧搜索区域与搜救资源安排不再保证可用。协调员登记新海况、漂移参数与生效时刻，**先看受影响区域，再确认应用**。

- `POST /api/areas/activate`：已分配未出动（`assigned`）转为已出动（`active`）
- `POST /api/sea-revisions`：登记修订（海况、漂移方向/速度、生效时刻），状态 `pending`
- `GET /api/sea-revisions/{id}`：预览受影响区域（原船、原因、漂移后中心、接手船或缺口），不落库
- `POST /api/sea-revisions/{id}/apply`：携带事件版本 `expected_version` 确认应用
- `POST /api/revision-assignments/{id}/confirm`：接手船确认接手
- `GET /api/incidents/{id}/sea-revisions`、`GET /api/sea-revisions/{id}/assignments`：历次修订与改派记录

处置规则：

- **未出动**（区域 `assigned`）：释放原安排后按新条件重排；有接手船则改派，无接手船则留缺口（区域 `gap`）。
- **已出动**（区域 `active`）：先登记改派，接手船处于 `reserved` 占用；**接手确认前原船仍负责**，确认后原船解除、接手船负责；找不到接手船则留下缺口并释放原船。
- **并发控制**：应用时校验事件版本，后到的协调员返回 `409` 版本冲突，刷新后重试。
- **失败重试**：应用为单事务，写入失败整体回滚，重试只得到完整结果；重复应用同一修订幂等返回已落库结果，崩溃后不留半套占用。

代码分层：`revision_judgment.py` 为纯判断层（漂移、影响评估、接手船匹配），`revision_store.py` 为留存层（表结构与 SQL，全部在调用方事务内执行），`app.py` 负责事务编排与 HTTP，`static/index.html` 为页面。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
