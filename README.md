# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

批准、空域限制和计划版本接成台账：每条批准绑住当时的计划版本、航线、时间和限制快照。限制修改或撤销会触发持久化重算任务，相关批准失效重算，不再满足的计划转待复核并写清扣在哪；限制撤销后版本、航线和时间都没动过的批准自动恢复，动过的重新走审核。限制按版本号乐观并发控制，先提交的算数。重算按条目落库，失败后只重试没写完的部分，通知按去重键幂等，旧数据启动时按当前关系回填台账。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/restrictions/{id}/revise`、`revoke`：按 `expected_revision` 修改或撤销限制，版本不符返回冲突；自动触发相关批准重算。
- `GET /api/restrictions/{id}`：限制详情与历次版本快照。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/ledger`、`GET /api/plans/{id}/ledger`：批准台账，含版本、限制快照和失效依据（运营方可见自己计划）。
- `GET /api/recalc`、`POST /api/recalc/{id}/retry`：重算任务查询与断点重试。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
