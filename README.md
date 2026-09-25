# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访；`exposure`：下游确诊病例与来源病例之间的暴露关系。

## 接触网络

病例经实验室确诊后，调查员可登记其来源病例、暴露时间和暴露地点：

- `POST /api/exposures`：登记暴露关系，请求体含 `case_id`（下游确诊病例）、`source_case_id`、`exposure_date`、`location`，`person_id` 缺省取下游病例本人。需要 `investigator` 或 `admin` 角色，支持 `Idempotency-Key`。
- 同一暴露人对同一来源病例的多次暴露只保留**最近一次**：更新更近的登记会覆盖旧记录，更早的登记被记录到 `ignored_register` 而不产生新关系。
- 关系建立失败不会报错，而是落为 `status=not_established` 的暴露记录并在 `data.reasons` 中说明原因：来源病例不是确诊（`confirmed/recovered/closed`）状态、下游病例尚未确诊、暴露时间晚于下游病例发病日期等。未建立的关系不参与串链，但仍可在网络视图中查到原因。
- `GET /api/network`：返回传播链（`chains`，按连通分量划分）、每个成员的代际（根来源为第0代，多来源取最长路径）、上下游病例 ID、关联的待处理/已解除接触者，以及 `pending_contacts` 和 `rejected_exposures`。
- 接触者可执行 `release` 动作（`identified/following → released`，需提交 `outcome`，如“未感染”）解除随访。解除后不再出现在待处理列表，但暴露登记和上下游关系**不删除**，仍可在网络视图中查询。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`cases`/`contacts`/`exposures`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `POST /api/exposures`：登记来源暴露关系（见“接触网络”）。
- `GET /api/network`：传播链、代际、待处理接触者与未建立关系说明。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
