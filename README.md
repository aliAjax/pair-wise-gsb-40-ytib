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
- `POST /api/areas/depart`：登记出动（区域由 `assigned` 转为 `active`）
- `POST /api/revisions`：登记海况修订（新海况、漂移参数、生效时刻、`base_plan_version`、幂等编号 `client_token`）
- `GET /api/revisions/{id}`：确认前预览受影响区域与处置计划，只读不落库
- `POST /api/revisions/confirm`：确认应用修订，单事务完成版本校验与全部占用写入
- `POST /api/reassignments/confirm`：接手确认，确认前区域仍归原船负责
- `GET /api/revisions?incident_id=`：历次修订、原船/接手船与当前缺口（页面数据源）

## 海况修订处置

修订按"登记 → 预览 → 确认"流转。确认时：未出动的安排释放原船并重排；已出动的登记改派单，
接手船立即预留但确认前原船负责；找不到接手留下缺口。`plan_version` 乐观并发让后到的提交看到
版本冲突；`client_token` 与单事务写入保证失败重试只得到完整结果，崩溃恢复不留半套占用。
存在待确认改派的资源不能撤回，避免同一艘船接下冲突任务。

代码按层拆分在 `revision/` 包：`models.py`（数据）、`planning.py`（判断，纯函数）、
`store.py`（留存，事务/幂等/恢复）、`service.py`（编排与权限），页面为 `static/index.html`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝；
海况修订覆盖登记-预览-确认全流程、缺口、版本冲突、幂等重试、崩溃回滚与恢复。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
