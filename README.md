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
- 批次可带 `parent_id` 指向上游批次，构成传播链；场地通过 `consignment_id` 或 `consignment_ids` 关联其接触过的批次。

## 实验室报告与停运规则

批次检验（`inspect`）后由 `lab` 角色提交 `lab_report`，字段为 `report_id`、`result`（`positive`/`negative`）、`result_date`：

- **阳性报告**：批次进入 `lab_positive`，阳性批次、全部下游批次（沿 `parent_id` 递归）以及这些批次接触过的全部场地（含与其他批次共享的场地）一起停运，场地进入 `stopped`。
- **重复报告**：同一 `report_id` 再次提交只保留首次结论，批次与场地状态、版本均不变，审计中标记 `duplicate`。
- **阴性报告**：批次进入 `lab_negative`，只移除该批次对各场地造成的停运归因；若场地仍被其他阳性批次停运，则保持 `stopped`，全部归因解除后才恢复到停运前状态。

`quarantine` 角色对 `lab_positive` 批次提交 `disinfect`（字段 `certificate_id`、`certificate_date`、`covered_facilities`）：

- 证书日期必须**晚于**阳性结果日期，否则拒绝且不改变停运。
- `covered_facilities` 必须覆盖整条接触链（阳性时快照 + 当前仍由该批次停运的场地）；缺任一场地则拒绝、保留全部停运，错误信息列明缺失场地 ID 与名称。
- 全覆盖时批次进入 `released`，并解除该批次造成的停运（其他批次归因仍在的场地继续停运）。

批次新增状态：`lab_positive`、`lab_negative`。`destroy`/`recheck` 也允许从 `lab_positive` 发起。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
