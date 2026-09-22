# 平台消费数据可信度

评估重点平台消费数据的覆盖、口径和统计可用范围，覆盖智能眼镜、心电监护仪、运动相机等
增长指标来自重点平台数据时的自动校验、质量评分、问题工单、可用范围审批与血缘说明。

## 业务事实

- 消费统计同时覆盖商品和服务零售
- 商品类别和服务类别增速存在明显差异
- 部分指标来自重点平台等补充数据来源
- 平台覆盖范围、类目映射和促销口径一变，汇总结果就可能失去可比性

## 协作角色

- `platform` 数据报送单位（平台数据贡献方）：**只能查看本平台**的批次与问题工单
- `statistician` 统计分析人员：可见全部，并可比较平台来源差异
- `reviewer` 口径审查人员：审批内部参考、汇总趋势范围
- `publisher` 公开发布人员：审批公开发布范围

## 核心规则

1. **只追加批次，不改写历史。** 迟报（late）、撤回（withdrawal）、接口改版（api_revision）、
   历史回补（backfill）都生成独立批次；按接收时间重放出每个平台-报告期的“当前有效批次”，
   旧批次永久保留可追溯。
2. **每次报送随附六项口径与质量声明**：样本覆盖、类目字典（版本+映射表）、去重方法、
   退款处理、价格口径、促销口径，以及质量声明。
3. **口径可比性自动比对。** 覆盖口径、字典版本、去重、退款、价格、促销及映射条目相对上一
   有效批次的任何变化都会告警（`CALIBRE_CHANGED` / `CATEGORY_MAP_CHANGED`）。
4. **异常先核查、不发布。** 同比显著偏离本平台历史（z 值/环比阈值）或同期其他平台中位数时，
   该观测被隔离（quarantine）并生成阻断性工单；核查确认真实后方可解除隔离并重算评分。
5. **分级可用范围审批**，审批绑定具体批次：出现改版/回补/撤回新批次后旧审批自动失效。
   - 内部参考 internal_reference（≥50 分，reviewer）
   - 汇总趋势 aggregate_trend（≥70 分、无未关闭阻断工单，reviewer）
   - 公开发布 public_release（≥85 分、等级 A、无隔离观测、口径稳定，publisher）
6. **采用指标必附血缘与置信说明**：来源平台、生效批次及批次类型、替代关系、口径快照、
   字典版本、质量声明、评分等级、未关闭/阻断工单数、隔离类目、置信等级（高/中/低/不可用）。

## 目录说明

- `contracts/domain.schema.json` 领域资料的字段与基本约束（原有）。
- `contracts/dataset.schema.json` 报送数据集合同：分类表、平台、批次、口径、观测。
- `fixtures/context.json` 原有公开样例。
- `fixtures/sample_dataset.json` 三平台样例，含迟报、撤回、接口改版、历史回补、
  未映射类目、口径变更、异常波动与低覆盖等场景。
- `src/model.py` 角色、批次类型、评分权重、审批门槛、置信等级等领域常量与结构。
- `src/dataset.py` 数据集载入、引用完整性校验、批次重放（有效批次/被替代/被撤回）。
- `src/validation.py` 自动校验：结构、映射、覆盖、及时性、口径稳定、历史/跨平台异常。
- `src/scoring.py` 六维加权质量评分（覆盖/映射/及时性/稳定性/文档/异常）与工单生成。
- `src/approval.py` 可用范围审批登记、门槛判定、血缘与置信说明。
- `src/service.py` 服务门面：角色可见范围、来源对比、核查解除隔离、公开统计出口。

## 快速使用

```python
from src.dataset import load_dataset
from src.model import Role, UsageScope
from src.service import QualityService

svc = QualityService(load_dataset("fixtures/sample_dataset.json"))

# 平台方只能看到本平台问题
svc.visible_tickets(Role.PLATFORM, "P-YUNFAN")

# 统计人员比较同期各平台同一类目来源差异
svc.compare_sources(Role.STATISTICIAN, "2026-08", "G-SMART-GLASSES")

# 异常核查：确认智能眼镜高增长为新品促销真实拉动 → 解除隔离并重算评分
svc.confirm_quarantine("B-YF-202608", "YF-0801", "审查员甲", "新品促销台账已核", True)

# 公开发布审批（须由 publisher）
svc.request_approval("P-YUNFAN", "2026-08", UsageScope.PUBLIC_RELEASE,
                     "发布员乙", Role.PUBLISHER, "2026-09-16")

# 采用指标时取得血缘与置信说明
svc.lineage("P-YUNFAN", "2026-08").to_dict()

# 公开统计出口：未获批/被隔离的观测一律不在其中
svc.public_observations("2026-08")
```

## 本地检查

```
python -m unittest discover -s tests -v
```

样例与测试中不含账号、密钥或连接凭据。
