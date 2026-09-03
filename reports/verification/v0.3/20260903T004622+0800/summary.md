# V0.3-T09 DeepSeek 草案/软排序 Live 通过卡

```yaml
run_id: "20260903T004622+0800"
version: "V0.3"
task_id: "V0.3-T09"
depends_on: ["V0.3-T08", "V0.2-T01"]
verification_level: "L"
commit: "e8b9db4 + working tree"
executed_at: "2026-09-03T00:46:22+08:00"
environment: "Darwin 25.6.0 x86_64 / Python 3.11.15 / uv 0.12.6"
provider_model: "DeepSeek / deepseek-v4-flash / Responses"
embedding_model: "BAAI/bge-small-zh-v1.5@a7ec18349c42fc774b0e86af26215e38a10fbe9d / trust_remote_code=false"
config_fingerprint: "sha256:eff6f268aa67a42244bd62f311f6d4be2f8ed5f54216551dc8bdcf9985cbbffb"
dataset_version: "V0.1 bundled synthetic campaign rules and merchants / 1.0.0"
eval_fingerprint: null
request_count: 3
request_ids:
  - "51f36c92-6378-4686-b436-7633781449f2"
  - "522407f2-30df-44d9-b2f0-fe124266a54d"
  - "955079b0-5b3e-43bc-902d-4fcc1f2eeb59"
usage:
  input_tokens: 12640
  output_tokens: 1128
  total_tokens: 13768
  provider_cost: "not reported; Oria projection remained 0.0 and is not treated as billing evidence"
commands:
  - "只读检查用户指定外部 dk.txt 的行数、长度、sk- 前缀和空白，不输出值"
  - "从 dk.txt 做进程级临时 DEEPSEEK_API_KEY 注入并运行 config doctor"
  - "运行真实 DeepSeek + 锁定 BGE 的 oria demo --output json"
  - "对生成报告执行 secret absence、request ID、usage、tool、候选子集和 validation 投影检查"
  - "只读查询 business.db 的 V0.3 写表计数"
artifacts:
  - ".artifacts/verification/v0.3/t09-file-key/reports-tmp/run_0f145520a02645acb35e4667036f0a0b.json（gitignored，sha256:0cefca63a98fc2e5000b83ffd616df8381ade23d53ab1ce06b8a22cc4da3b0ef）"
evidence_refs:
  - "src/oria/demo.py"
  - "src/oria/agent/graph.py"
  - "tests/live/test_t10_deepseek_bge.py"
assertions:
  - "3/3 model turns 返回唯一 request ID，response model 均为 deepseek-v4-flash"
  - "工具轨迹严格为 search_campaign_rules → query_merchants，总调用数 2"
  - "CampaignProposal schema、六类规则、语义证据和 52 个逐字段引用均通过本地校验"
  - "推荐 10 家商户全部来自 EligibilityPolicy 候选集；排除商户未被模型重新加入"
  - "推荐 ID 为 demo-m001、demo-m002、demo-m005 至 demo-m012，eligible count=10"
  - "campaigns、coupon_batches、launch_saga_states、recruitment_publications、tool_executions 计数均为 0"
  - "business_side_effect_free=true，forbidden_business_tables=[]"
  - "完整 API Key 不在生成报告、命令输出或仓库中"
result: "passed"
blocked_by: []
known_limits:
  - "本卡只验证真实 DeepSeek 对本地合成规则/商家数据的只读草案与软排序，不验证任何企业 Adapter"
  - "Provider 未返回可直接采用的成本金额；token usage 已记录，0.0 不作为实际账单成本声明"
  - "此前两张 401 failed 卡作为历史保留，不删除或改写"
```

## 结论

V0.3-T09 已通过。用户指定的外部 `dk.txt` 仅作为密钥数据做进程级注入；未执行文件内容，也未复制密钥到仓库。真实 `deepseek-v4-flash` 完成 3 个模型轮次，调用两个只读工具并生成通过 schema、引用、硬资格子集和零业务副作用校验的活动/券草案预览。

本卡关闭 V0.3 的必需 DeepSeek Live 门禁，但不改变企业 Adapter 的未验证状态。允许声明“V0.3 Core 与必需 DeepSeek 草案/软排序 Live 卡均通过”；仍禁止声明真实券、招商、商品库、选品、C 端投放或 IM 接入通过。
