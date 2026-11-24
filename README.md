# 门店经营数据问答到可复核业务结论

本项目把“门店负责人提出一个具体经营问题”到“结论回到决策者并形成可追踪行动”
的流程做成一个本地 Python 应用。输入资料全部为合成数据，标识和数值不对应真实
个人、企业或交易；运行只依赖 Python 3.11–3.14 标准库和 SQLite，不需要网络或
额外服务。

## 解决的问题

固定报表难以回答“为什么变化”，但把自然语言问题直接交给系统也不安全：问题里的
“销售额”“到店转化”未必有明确口径，提问者权限、地区范围和数据敏感性也未必允许
直接执行。应用把这段等待拆成可审计的步骤：

1. 门店负责人登记一个具体经营问题，关联地区、时间范围、用途和决策负责人。
2. 分析人员与提问者澄清问题，从指标目录中选择**该部门适用且状态为 active** 的口径。
3. 数据治理人员按人员、用途、数据集和敏感级别作出 `allow / limit / deny` 决定；
   涉及个人级明细时只能交付聚合结果。
4. 分析人员用指定数据版本生成确定性结论，交付时写明覆盖范围和限制。
5. 业务负责人复核结论是否回答了经营问题；只有复核后选择跟进，才能登记行动。
6. 行动被接受后才形成责任人和期限；完成结果回到原请求，供复盘和审计。
7. 数据迟到或授权撤销时，已交付结论保留当时的口径、数据版本和内容摘要，同时
   新增“受影响范围”记录，并可基于新版本重新分析。

应用只做经营复盘，不执行价格、采购或人员调整；这类决定需走各自审批流程。

## 快速开始

```sh
# 1. 安装为可编辑包（可选；不安装时用 PYTHONPATH=src 运行）
python3 -m pip install -e .

# 2. 运行测试
python3 -m unittest discover -s tests -v

# 3. 生成一份完整演示库
python3 scripts/seed_demo.py demo.db --force

# 4. 查看结果
python3 -m retail_analytics_question_workflow --db demo.db show --as MGR-101 --request RQ-00001
python3 -m retail_analytics_question_workflow --db demo.db show-run --as MGR-101 --run RUN-00002
```

未安装包时，把 `python3 -m retail_analytics_question_workflow` 换成
`PYTHONPATH=src python3 -m retail_analytics_question_workflow`。

## 手工走一遍流程

```sh
export DB=demo.db
export PYTHONPATH=src
M="python3 -m retail_analytics_question_workflow --db $DB"

$M init --materials data/workflow --reset

# 门店负责人登记
$M request-new --as U-101 --region R-N --start 2026-11-03 --end 2026-11-16 \
   --owner MGR-101 --question "北区近两周到店转化为什么下降？"

# 澄清与选口径
$M clarify --as AN-101 --request RQ-00001 --kind question --message "是否需要剔除员工代客下单？"
$M clarify --as U-101 --request RQ-00001 --kind answer --message "关注顾客到店转化。"
$M choose-metric --as AN-101 --request RQ-00001 --metric M-CONV-OPS \
   --clarified-question "北区门店 11-03 至 11-16 顾客到店转化率较前两周变化多少？"

# 数据访问申请与治理决定
$M request-access --as AN-101 --request RQ-00001 --dataset DS-STORE-DAILY \
   --justification "经营诊断需要门店日汇总"
$M decide-access --as GOV-101 --access AC-00001 --decision allow \
   --justification "门店聚合，无个人明细" --output-scope store-aggregate

# 预览、生成、交付
$M preview --as AN-101 --request RQ-00001
$M run --as AN-101 --request RQ-00001
$M deliver --as AN-101 --run RUN-00001 --acknowledge-partial \
   --note "N-301/N-303 在 11-10 至 11-16 数据迟到，结论只覆盖已到数据。"

# 新数据版本补齐后：标注影响并重新分析
$M publish-version --as GOV-101 --version DV-2026-11-24
$M reanalyze --as AN-101 --request RQ-00001 --data-version DV-2026-11-24

# 业务复核与行动
$M review --as MGR-101 --request RQ-00001 --run RUN-00002 \
   --answered yes --follow-up yes --note "下滑集中在施工影响的两家门店。"
$M propose-action --as MGR-101 --request RQ-00001 --decision observe \
   --rationale "施工结束前后持续观察客流与转化恢复"
$M accept-action --as MGR-101 --action ACT-00001 --due 2026-12-10
$M complete-action --as MGR-101 --action ACT-00001 \
   --result-ref RES-DEMO-01 --result-note "施工结束后两周转化恢复至均值附近。"

$M show --as MGR-101 --request RQ-00001
$M audit --as AN-101 --request RQ-00001
```

## 角色与权限

| 角色 | 人员示例 | 主要动作 |
| --- | --- | --- |
| 门店负责人 `store_manager` | U-101…U-106 | 登记问题（限本人地区）、补充信息、查看本人请求 |
| 分析人员 `analyst` | AN-101、AN-102 | 澄清、选指标、申请数据、预览/生成/交付、重新分析 |
| 数据治理人员 `data_governor` | GOV-101、GOV-102 | 决定数据访问、撤销授权、发布数据版本 |
| 业务负责人 `business_owner` | MGR-101…MGR-106 | 复核结论、登记/接受/完成行动 |

权限不是名字前缀，而是数据库中的角色、部门、地区和个人数据许可字段。
请求只能由登记人、指定决策负责人或分析/治理角色查看。只有指定业务负责人
能复核该请求并接受行动；行动的建议阶段不产生责任和期限。

## 口径、访问与交付规则

- **口径有版本和适用人群**。`net-sales` 同时存在财务口径 `DEF-1`（净额）和商品部
  口径 `DEF-2`（毛额）；`conversion` 也有“含员工代客下单”和“剔除员工”两种。
  选择不在提问者部门适用人群内的指标会被拒绝，`review` 和 `retired` 状态不能交付。
- **访问决定不能放宽策略**。应用按“数据集 + 角色 + 部门 + 用途 + 地区 + 许可”
  匹配策略，取最宽决定。治理人员只能在策略上限内收紧；个人数据集默认 `deny`，
  门店负责人最多到 `limit`，且输出范围只能是门店聚合。
- **覆盖不完整不算零**。门店日数据按 `available_seq` 标记批次；选定版本缺少某店
  某日时会给出 `late-data`、`partial`、`outside-coverage` 或 `no-data`，并列出缺失
  区间和门店。交付时需要显式确认并写说明，`no-data` 直接拒绝。
- **结论可复核**。每条分析保存指标快照（含定义版本）、参数、数据版本、覆盖范围、
  来源数据集和内容 SHA-256 摘要。重新分析生成新记录，不改写旧结论。
- **影响可追踪**。发布补齐数据的版本会为相关已交付结论写入
  `late-data-recovered` 影响；撤销授权会写入 `permission-revoked` 影响，但不会删除
  原结论。业务复盘时可看到哪段时间、哪些门店受到影响。

## 状态流转

```
new → clarifying → ready_for_access → access_pending → ready_for_analysis
    → in_analysis → delivered → follow_up_pending → action_pending
    → action_in_progress → completed

access_pending → blocked（拒绝）
delivered → closed_unanswered（复核认为未回答问题）
follow_up_pending / action_pending → 取消后回到 follow_up_pending
```

非法跳转（例如未授权就分析、未复核就登记行动、重复复核）会返回明确的业务错误。

## 数据与目录

```
data/workflow/reference/   人员、门店、数据集、指标目录、访问策略、经营事件、数据版本
data/workflow/facts/       门店日汇总、会员日明细（合成）
scripts/generate_workflow_materials.py   确定性再生上述资料
scripts/seed_demo.py       生成完整演示库
src/retail_analytics_question_workflow/
    analysis.py   确定性指标计算与覆盖判断
    service.py    状态机、权限、访问决定、交付、行动
    materials.py  资料导入与表结构初始化
    schema.py     SQLite 表结构
    cli.py        命令行
    render.py     中文报告渲染
tests/            资料契约与端到端测试（41 项）
docs/资料契约.md   原始资料字段说明
```

原有的 `data/*.csv`（`requests.csv` 等）是早期基线样例，字段和业务含义与
`data/workflow` 不同；应用不读取它们，`tests/test_materials.py` 继续校验其格式。

## 设计取舍与限制

- 计算是确定性的：同一输入、同一数据版本、同一门店和期间得到相同内容摘要，
  便于复核；不包含统计显著性检验或因果推断。
- 覆盖判断基于门店日粒度。会员日明细与门店日共用批次标记，因此“某天没有会员
  消费”不会被误判为数据缺失。
- 个人数据只在内存中聚合，结论接口不返回会员标识；统计窗口内会员记录少于 10 条的门店值会被抑制。
- 期限到期没有自动升级或提醒；行动由业务负责人人工登记。
- 所有时间戳取自运行环境时钟（UTC），演示资料中的日期是固定合成日期。
