# 电厂调度与能源分析与机组分析准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录电力市场基准电价、电厂与变电站设施、送出线路、燃料批次、发电计划和负荷情景，并保留机组巡检传感器统计分析准入流程。系统面向电价连续波动、关键送电送出线路恢复、电量调度和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 电力市场基准电价按结算日和来源修订登记，历史版本不会被覆盖；
- 电厂、储罐、终端与储能站设施建档，送出线路保存日能力、在途时间和损耗规则；
- 送出线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 燃料批次保留电源类型、牌号、数量、单位成本和接收时间，可计算加权燃料库存成本；
- 交易方提名支持载荷级幂等、优先级分配、燃料库存扣减和在途交接；
- 负荷情景保存电价变化、送出线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

机组分析准入子域位于 `plant_science` 包，负责机组巡检传感器的设备构建登记、不可变校准协议、测点分片导入、异常测点复核、统计任务租约、分析准入决定和审计报告。该子域不连接传感器硬件，只处理已经结构化的校准记录。

## 目录

- `src/power_dispatch/`：电价、设施、送出线路、燃料库存、提名、负荷情景、HTTP API 与离线验收；
- `src/plant_science/`：机组巡检传感器校准与统计分析准入；
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m power_dispatch.acceptance --workspace .
```

该命令会在内存数据库中登记六个结算日的峰谷电价，创建电厂、终端和送出线路，完成燃料库存入账、提名分配、送电及负荷情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

机组分析准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m plant_science.acceptance --workspace .
```

## 光电芯片研发协同服务

`src/photon_fab/` 提供光电芯片批次、光谱测量、科学计算、质量审批和审计的离线后台。SQLite 保存完整批次生命周期，角色权限覆盖操作员、工程师、质量人员和管理员；峰值波长、噪声 RMS、响应度、置信区间及良率计算均为确定性本地算法。

工艺版本管理支持 P3.2/P3.3 并行试产：

- 工艺版本登记时校验完整参数集（沉积温度、腔体压力、气体流量、射频功率、刻蚀时间、烘烤温度、曝光剂量）的闭区间取值，参数快照与 SHA-256 摘要一并落库；
- 版本状态机为 `draft -> frozen`；冻结后参数成为不可变历史快照，已绑定批次的版本禁止原地修改，变更只能通过从已冻结父版本派生新版本发布，并保留父版本引用与变更原因；
- 批次只能绑定已冻结版本，绑定不可更改；`GET /lots/{lot_id}/trace` 返回批次绑定版本的完整父版本追溯链、参数快照和工艺/批次双侧事件；
- 登记、派生、绑定支持 `Idempotency-Key` 请求头（或请求体 `idempotency_key`）：同键同载荷重复提交返回同一结果，同键不同载荷返回 `409 conflict`；
- 非法状态转换统一返回 `409 {"error":{"code":"invalid_state"}}`，校验失败为 `422 validation_failed`，不存在为 `404 not_found`。

```bash
PYTHONPATH=src python3 -m photon_fab.acceptance
PYTHONPATH=src python3 -m photon_fab.api --database photon.sqlite3 --port 8080
```

HTTP 健康检查为 `GET /health`，登录、批次、测量、分析和工艺版本请求均支持 JSON；服务不访问外部网络，可在单个 Linux 应用容器中完成验收。工艺相关接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/processes` | 登记新工艺首版（草稿），支持幂等键 |
| GET | `/processes/{process_id}` | 列出工艺线全部版本 |
| GET | `/processes/{process_id}/{version}` | 读取版本历史快照 |
| PUT | `/processes/{process_id}/{version}` | 修改未冻结、未绑定的草稿参数 |
| POST | `/processes/{process_id}/{version}/freeze` | 冻结发布 |
| POST | `/processes/{process_id}/derive` | 从父版本派生新版本（`parent_version`，可选 `new_process_id`），支持幂等键 |
| POST | `/lots/{lot_id}/process-binding` | 批次绑定已冻结版本，支持幂等键 |
| GET | `/lots/{lot_id}/process` | 查询批次当前绑定 |
| GET | `/lots/{lot_id}/trace` | 完整追溯链（版本链、快照、事件） |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、负荷情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。
