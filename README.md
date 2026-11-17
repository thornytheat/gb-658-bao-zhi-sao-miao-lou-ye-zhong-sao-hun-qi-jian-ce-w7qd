# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 留存销毁：页面 /retention，接口 /api/retention/receive、/api/retention/destroy、/api/retention/verify

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

## M7 留存与销毁

- 接收（`POST /api/retention/receive`）：接入真实清单 CSV，登记保留期限并签发 HMAC-SHA256 验收凭据，原文与隔离原值逐行入库。
- 销毁（`POST /api/retention/destroy`）：到期逐行清除原文与隔离原值，仅保留行号、原因码、SHA-256 摘要与行级签名等不可还原核验依据；行级检查点支持中断恢复，重试沿用原验收凭据。未到期需 `force=true`。
- 核验（`POST /api/retention/verify`）：责任方提交原件，与留存摘要逐行验真，原件只参与比对、不落库。
- 回读（`GET /api/retention/{retention_id}`）：返回保留状态与原接收数量，销毁后数量仍可追溯。
- 幂等：同请求 ID 换内容返回 409；同内容新请求在销毁后返回已销毁回执，旧清单不得重新落库。
- 边界：不改动重扫采信逻辑，不读取或销毁任何图像文件，销毁对象仅为清单文本与隔离原值。

