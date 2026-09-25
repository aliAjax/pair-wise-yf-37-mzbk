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

- `case`：病例和调查状态；`contact`：接触者随访。
- 病例确认（`confirmed`/`probable`）后可通过`register_exposure`动作登记来源病例、暴露时间和地点，系统据此串出传播链和代际。
- 接触者可通过`release`动作解除（仅限未感染者），解除后其与病例的关联保留，传播链上下游关系仍可查询。

## 接触网络规则

- 同一病例与同一来源病例多次暴露时，只保留最近一次暴露记录。
- 来源病例状态须为`confirmed`/`probable`/`recovered`/`closed`，否则关系不建立并说明原因。
- 暴露时间晚于该病例发病日期时，关系不建立并说明原因。
- 病例的主要传染来源取各来源中最近一次暴露，代际从链首（第1代）向下递增。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/chains`：传播链列表，含链成员、代际和各链接触者（`pending_contacts`为待处理接触者）。
- `GET /api/chains/<case_id>`：单个病例所在传播链及其上、下游病例。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
