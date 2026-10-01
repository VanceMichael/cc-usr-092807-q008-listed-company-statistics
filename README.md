# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

平台在通用协同能力之上扩展了**上市公司月报子域**：统一公司身份、跨市场证券、上市退市事件、控股性质/行业/省域分类、股本变化、收盘估值、募资与来源批次按时间串联；月报冻结数据水位与分类版本，晚到材料只出受影响指标的勘误版本，不覆盖原公告。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行通用协同演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

`demo` 命令同时演出上市公司月报全流程：境内首发+赴港上市、月末迁址的民企 → 冻结并发布八月月报 → 一周后交易所补来股本更正 → 仅制造业市值等受影响指标出勘误版 → 同批重送不累计、编号冲突进隔离区、未公开主体不可推断。

月报相关查询命令：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-reports --period 2026-08
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-quarantine
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 blockers --period 2026-08
# 从指标（比例/增量）钻取到公司、证券事件、来源水位与更正链
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 \
    explain --period 2026-08 --metric manufacturing_market_cap --edition 2
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 \
    company-timeline 91330000MADEMO001A
```

## 上市公司月报模型

| 关注点 | 实现位置 | 说明 |
| --- | --- | --- |
| 公司统一身份 | `companies.py` | 统一社会信用代码为自然键；名称/注册地址按生效时间保留全部版本 |
| 跨市场证券与归并 | `companies.py` | 同一主体各市场证券独立成行；`merge_companies` 归并后各市场事实保留 |
| 分类版本 | `taxonomy.py` | 行业/控股性质/省域三轴，版本须 `confirmed` 才能冻结；归属可 `tentative` |
| 市场事实 | `market_facts.py` | 上市退市、股本、估值、募资追加写入；同键更正在水位内取最新版本 |
| 来源批次 | `sources.py` | 批次水位、同批重送幂等回放、编号冲突隔离、截止周期与缺批次阻断 |
| 记录分发 | `ingestion.py` | 批次记录 → 公司/证券/事实/分类写入，单条失败用 SAVEPOINT 隔离 |
| 月报与勘误 | `monthly.py` | 冻结水位+分类版本；晚到批次只出受影响指标的 errata；发布职责分离；`explain` 全链路钻取 |

关键规则：

- **冻结**：月报记录截止时刻、已到达批次（含摘要）与三套分类的确认版本；原报发布后不可二次冻结。
- **勘误**：晚到批次到达后生成 `errata` 版本，只包含数值真正变化的指标行（带原值与更正理由），原月报保持 `published` 不被覆盖。
- **幂等与隔离**：同批原样重送直接回放、不再次累计；同批同编号不同内容进 `source_quarantine`，事实不被污染。
- **职责分离**：批次录入人与冻结人进入 `report_contributors`，均不能发布同一份月报。
- **未公开隔离**：未公开公司与不存在公司对普通查询返回同一错误，无法推断存在性。
- **恢复续跑**：缺批次或待确认分类时截止周期保持 `waiting` 并登记阻断项；材料补齐后以同一截止重试。
- **可追溯**：任一历史版本的任一指标（含比例的分子分母）都可下钻到具体公司、证券、事实编号、来源批次/记录号和完整更正链。
