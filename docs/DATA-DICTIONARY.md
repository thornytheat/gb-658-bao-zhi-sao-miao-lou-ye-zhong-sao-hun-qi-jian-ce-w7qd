# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## M7 留存与销毁

- retention_id：由内容 SHA-256 前 16 位派生的稳定留存标识，同内容全生命周期唯一。
- content_digest：完整清单字节的 SHA-256，销毁后仍保留，用于整体验真。
- raw_digest：单行物理原文的 SHA-256；销毁后与 line_no、reason_code、行级 HMAC 签名共同构成不可还原核验依据。
- 验收凭据：RETENTION_ACCEPTANCE（接收签发）、DESTRUCTION_RECEIPT（销毁回执，内嵌原凭据不重新签发）、ROW_VERIFICATION_BASIS（行级核验依据），算法均为 HMAC-SHA256。
- 状态机：ACTIVE → DESTROYING → DESTROYED；DESTROYING 为中断恢复态，重试从 destroy_progress 检查点继续。
- retain_until：接收时间加保留天数（UTC ISO 8601）；未到期销毁须显式 force，回执标注 early。
- received_count：原接收数量 = 采纳行 + 隔离行，销毁后仍可回读，原文不可回读。
- 请求幂等：request_id 与 content_digest 在 request_ledger 绑定；同 ID 换内容 409，同内容新请求在 DESTROYED 态返回 ALREADY_DESTROYED 且不重新落库。
- 核验：提交原件只做摘要比对（original_persisted 恒为 false），verified 要求凭据签名、整体摘要与逐行摘要同时通过。

