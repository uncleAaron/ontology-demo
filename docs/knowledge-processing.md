# 知识加工：当前实现

当前在原文片段检索之上加入单来源、人工审核的知识加工。文档导入和索引不调用模型；文档页点击“加工当前原文”才创建持久化任务。服务端必须配置模型，界面展示正常流程的请求次数估算；失败重试会增加调用。

## 加工与恢复

Schema 5 新增 processing_jobs、knowledge_candidates、task_derived_refs，保留旧原文、任务及证据。启动仅迁移和恢复队列，不自动为所有文档创建加工任务。

幂等键为 document_id、published_revision、source_generation、pipeline_version=v1。分段/提示/schema 改变时需升级 pipeline_version。重复创建返回原任务；取消后再次发起恢复原检查点；失败须显式重试，完成后不重复生成。

Worker 每次认领一个阶段/批次，租约60秒；写回校验所有者 token、租约与来源 generation。超期运行记录重新排队，同阶段最多3次尝试。成功批次清零尝试计数；普通失败记录安全错误，等待手动重试。取消使所有者失效，无法终止已发出的供应商请求，但迟到结果不能写入。

分析阶段每批最多6个约1200字符的片段，最多3条关键观察，每条绑定片段ID与6～200字符逐字引文。批次成功后保存分析与 cursor，重启继续下一批。扫描所有片段不等于保留每个细节，模型仍可能遗漏信息。

生成阶段使用完整已校验分析，最多6条候选，必须有摘要。kind 支持 summary/faq/fact/entity/relation；标题最多120字符，正文600字符，至多3个引文。程序校验每个引文属于本次分析和对应来源原文，全部通过后在同一事务中保存候选。实体/关系候选仅为知识页面，没有创建业务对象或确认图边。

输入遵循上下文预算，放不下完整来源/分析时失败，不丢弃引用继续生成。格式错误、输出截断、超时均不保存半成品候选。分析检查点仍保留，可调高预算后重试。没有供应商费用回执或真实 token 计量。

## 发布与取证

候选默认 needs_review。审核需 expected_generation、expected_source_generation、理由；事务中检查候选状态及当前有效原文。批准成为 active，拒绝成为 rejected，已发布知识可以 withdrawn；每次操作递增候选 generation 并留审计。逐字引文存在不证明正文含义正确，审核须对照原文条件和范围。

自动问答预检索至多8个原文片段与3条已发布知识；模型可调用 search_knowledge/read_knowledge 补充取证。已发布知识按中文词项/英文小写关键词召回，标题权重10、正文1，在限制结果数前排序。指定文档约束同时应用两个通道。

知识检索必须满足候选 active、来源 active、发布修订相同、来源 generation 相同。来源修改立即暂停派生知识，即使后续批准原文，也需基于新的 generation 重新加工。知识引用记录候选 ID/generation 和来源修订，任务执行中及交付前检查；知识撤回会阻断旧引用并在历史任务显示 knowledge_unavailable。

## API

| 接口 | 用途 |
|---|---|
| POST /api/documents/{id}/process | expected_generation，创建或复用加工任务 |
| GET /api/documents/{id}/processing | 最近20个任务和候选，含 truncated |
| GET /api/processing-jobs/{id} | 状态、检查点、来源有效性、候选 |
| POST /api/processing-jobs/{id}/retry | 失败阶段显式重试 |
| POST /api/processing-jobs/{id}/cancel | 取消未完成任务 |
| POST /api/knowledge/{id}/review | approve/reject/withdraw、两项 generation、reason |
| GET /api/knowledge?q=...&document_id=... | 检索有效已发布知识，最多5条 |

固定本地演示身份不是企业权限管理。当前不包含跨来源支持、知识编辑修订、冲突检测、自动图边确认、自动定时加工、真实模型质量评测。这些仍属于后续里程碑。

## 验证

本地自动化99项通过，包括长文批次覆盖、重启恢复、幂等创建、引用失配原子失败、检查点重试、取消和租约迟到结果、重试耗尽、来源变化、旧审核阻断、知识撤回后的在途交付与历史警告、项目作用域及关键词排序。

三组 DOM/API 联测通过：既有来源维护、长文问答、知识加工→原文核对→审核发布→检索→撤回。使用本地模拟模型和临时数据库，不代表 DeepSeek 或其他真实供应商兼容性验收；未完成真实浏览器视觉和 Docker 验证。
