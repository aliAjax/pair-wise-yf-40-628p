# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 实验室报告与消毒证明

- `lab_report`（consignment，角色`lab`/`admin`）：提交`{"report_id","result","reported_at"}`，`result`为`positive`或`negative`。
  - 阳性：该批次、下游批次（`parent_id`链）及接触链上所有设施一起停运，停运来源记入各对象的`holds`。
  - 同一`report_id`的重复报告直接忽略，只保留首次结论。
  - 阴性：只释放该批次`holds`造成的停运，其他批次造成的停运保持不变；全部来源解除后对象恢复停运前状态。
  - 持有`holds`的批次除`destroy`外不能执行其他动作，须由阴性报告解除。
- `disinfect`（consignment，角色`quarantine`/`admin`）：提交`{"certificate_date","covered_facility_ids"}`。
  - 需存在阳性结论，且证明日期晚于阳性结果日期。
  - 证明必须覆盖该批次停运的全部设施；缺任一场地则保留停运，错误信息列明缺失场地。
  - 校验通过后解除这些设施的停运，批次本身的检疫状态不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
