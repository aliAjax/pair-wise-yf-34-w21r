# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

## 台账：批准、限制与计划版本

每条批准都绑定当时的计划版本快照（版本号、航线、起降时间）和限制快照（与该计划时空重叠的有效限制），形成可追溯的台账。限制一有改动（新增、更新、撤销），系统会对所有已批准和待复核的计划重算：

- 不再满足硬约束或空域/相邻冲突的计划转 `pending_review`（待复核），通知写清扣在哪（哪条限制、什么冲突），运营方据此拿到失效依据。
- 限制撤销后，若计划版本、航线、时间都没动过，自动恢复批准并通知；动过的不自动恢复，需重新提交审核。
- 重算按 run/item 台账逐条提交，失败后只重试没写完的部分：不漏计划，不重复通知。
- 数据库升级前已存在的批准，会在服务启动时按当前关系回填计划快照与限制链接。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区（触发重算）。
- `POST /api/restrictions/{id}/update`：更新限制，需带 `expected_revision`；两个审核员同时修改同一条限制，先提交的算数，后到的见 `revision_conflict`（触发重算）。
- `POST /api/restrictions/{id}/revoke`：撤销限制，需带 `expected_revision`（触发重算）。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
