window.ORIA_FEISHU_REPLAY = {
  "version": 1,
  "source": "real Feishu commands and outbound IMViewModel responses",
  "sourceLabel": "真实飞书群聊运行记录 · Oria 回放界面",
  "boundary": "合成数据 · Mock 模型 / 业务系统 · 演示角色",
  "note": "画面按实际指令及机器人卡片响应重新排版；点击卡片可展开完整内容。",
  "scenes": [
    {
      "label": "一览",
      "title": "在飞书里，走完一次招商流程",
      "text": "从需求追问到双审批、报名、动态确认、选品与投放通知，再到经营异常归因。",
      "fact": "真实群聊 · 20 条指令成功投递",
      "cards": [
        {
          "sourceSequence": 1,
          "command": "帮助",
          "title": "Oria 核心流程导览",
          "summary": "用群内对话运行真实的本地工作流。首版固定华东餐饮、混合报名、10家候选。审批人与商家/销售/经理均为隔离的合成角色；你控制演示步骤，不会获得这些角色的生产权限。没有真实发券、投放或商家通知。",
          "sections": [
            {
              "title": "可用说法",
              "text": "发起招商活动 → 餐饮 → 华东 → 混合报名 → 10家 → 确认发起演示\n规则 / 商家 / 状态 / 恢复演示\n演示批准 / 模拟商家报名 / 模拟报名结束 / 演示确认 / 模拟选品完成\n归因示例 / 证据冲突示例 / 证据不足示例"
            }
          ],
          "fullSections": [
            {
              "title": "可用说法",
              "text": "发起招商活动 → 餐饮 → 华东 → 混合报名 → 10家 → 确认发起演示\n规则 / 商家 / 状态 / 恢复演示\n演示批准 / 模拟商家报名 / 模拟报名结束 / 演示确认 / 模拟选品完成\n归因示例 / 证据冲突示例 / 证据不足示例"
            }
          ],
          "nextStep": "@OriaTest 发起招商活动"
        }
      ]
    },
    {
      "label": "01 · 需求澄清",
      "title": "先问清楚，再确认发起",
      "text": "实际对话逐项补齐类目、区域、报名模式与候选数量；确认后启动核心工作流。",
      "fact": "餐饮 / 华东 / 混合报名 / 10 家",
      "cards": [
        {
          "sourceSequence": 6,
          "command": "10家",
          "title": "请确认活动参数",
          "summary": "华东 · 餐饮 · 混合报名 · 目标10家\n确认后在隔离 SQLite 中创建合成活动并执行规则检索、资格过滤和草案。",
          "sections": [],
          "fullSections": [],
          "nextStep": "@OriaTest 确认发起演示"
        }
      ]
    },
    {
      "label": "02 · 招商审批",
      "title": "草案先停在审批边界",
      "text": "规则检索与商家筛选后进入招商发布审批。演示审批由独立合成角色执行，保留职责分离。",
      "fact": "需求 → 草案 → 第一次审批",
      "cards": [
        {
          "sourceSequence": 7,
          "command": "确认发起演示",
          "title": "2/10 招商发布审批",
          "summary": "规则、候选范围及活动/券草案已冻结, 等待招商发布审批。",
          "sections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案"
            }
          ],
          "nextStep": "@OriaTest 演示批准（由合成审批角色执行，保留职责分离）"
        }
      ]
    },
    {
      "label": "03 · 报名汇聚",
      "title": "把外部事件接回流程",
      "text": "模拟商家报名与自动圈品汇聚；报名结束事件恢复等待中的工作流。",
      "fact": "报名已接收 → 关窗后继续",
      "cards": [
        {
          "sourceSequence": 9,
          "command": "模拟商家报名",
          "title": "4/10 报名与商品圈选",
          "summary": "招商已发布, 等待报名/关窗; 当前正在接收报名。",
          "sections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布"
            }
          ],
          "nextStep": "@OriaTest 模拟商家报名，然后 @OriaTest 模拟报名结束"
        },
        {
          "sourceSequence": 10,
          "command": "模拟报名结束",
          "title": "5/10 动态业务确认",
          "summary": "当前第 1/3 级, 由商家确认, 下一位为销售。",
          "sections": [
            {
              "title": "动态确认链",
              "text": "第1/3级 · 商家"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选"
            },
            {
              "title": "动态确认链",
              "text": "第1/3级 · 商家"
            }
          ],
          "nextStep": "@OriaTest 演示确认（仅当前一级合成角色；逐级确认）"
        }
      ]
    },
    {
      "label": "04 · 动态确认",
      "title": "按规则，逐级确认",
      "text": "本次合成规则生成商家、销售、销售经理三级确认链。每条指令只处理当前一级。",
      "fact": "商家 → 销售 → 销售经理",
      "cards": [
        {
          "sourceSequence": 10,
          "command": "模拟报名结束",
          "title": "5/10 动态业务确认",
          "summary": "当前第 1/3 级, 由商家确认, 下一位为销售。",
          "sections": [
            {
              "title": "动态确认链",
              "text": "第1/3级 · 商家"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选"
            },
            {
              "title": "动态确认链",
              "text": "第1/3级 · 商家"
            }
          ],
          "nextStep": "@OriaTest 演示确认（仅当前一级合成角色；逐级确认）"
        },
        {
          "sourceSequence": 11,
          "command": "演示确认",
          "title": "5/10 动态业务确认",
          "summary": "当前第 2/3 级, 由销售确认, 下一位为销售经理。",
          "sections": [
            {
              "title": "动态确认链",
              "text": "第2/3级 · 销售"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选"
            },
            {
              "title": "动态确认链",
              "text": "第2/3级 · 销售"
            }
          ],
          "nextStep": "@OriaTest 演示确认（仅当前一级合成角色；逐级确认）"
        },
        {
          "sourceSequence": 12,
          "command": "演示确认",
          "title": "5/10 动态业务确认",
          "summary": "当前第 3/3 级, 由销售经理确认。",
          "sections": [
            {
              "title": "动态确认链",
              "text": "第3/3级 · 销售经理"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选"
            },
            {
              "title": "动态确认链",
              "text": "第3/3级 · 销售经理"
            }
          ],
          "nextStep": "@OriaTest 演示确认（仅当前一级合成角色；逐级确认）"
        }
      ],
      "compact": true
    },
    {
      "label": "05 · 选品与二次审批",
      "title": "选品完成，仍需投放审批",
      "text": "逐商品选品决定与完成事件恢复流程，券关联结果随方案进入 C 端发布审批。",
      "fact": "异步等待 → 选品完成 → 第二次审批",
      "cards": [
        {
          "sourceSequence": 14,
          "command": "模拟选品完成",
          "title": "8/10 C 端发布审批",
          "summary": "入选商品、券关联与投放范围已冻结, 等待 C 端发布审批。",
          "sections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选 → 动态业务确认 → 报名商品关联券批次 → 提交并等待招后选品"
            },
            {
              "title": "选品与券关联",
              "text": "提交1件；选中1件；待定0件；关联券1件"
            },
            {
              "title": "模拟投放",
              "text": "渠道：synthetic-home-feed；状态：pending_approval"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选 → 动态业务确认 → 报名商品关联券批次 → 提交并等待招后选品"
            },
            {
              "title": "选品与券关联",
              "text": "提交1件；选中1件；待定0件；关联券1件"
            },
            {
              "title": "模拟投放",
              "text": "渠道：synthetic-home-feed；状态：pending_approval"
            }
          ],
          "nextStep": "@OriaTest 演示批准（由合成审批角色执行，保留职责分离）"
        }
      ]
    },
    {
      "label": "06 · 闭环",
      "title": "完成投放与商家通知",
      "text": "本次核心流程已到 10/10。模拟投放 published，模拟商家通知 sent，两次审批均由不同于发起人的角色决定。",
      "fact": "10/10 完成 · 2 次审批职责分离",
      "cards": [
        {
          "sourceSequence": 15,
          "command": "演示批准",
          "title": "10/10 通知商家并闭环",
          "summary": "全部十个业务步骤已完成。",
          "sections": [
            {
              "title": "模拟投放",
              "text": "渠道：synthetic-home-feed；状态：published"
            },
            {
              "title": "模拟商家通知",
              "text": "demo-m001：sent · 活动 guided-campaign-11d87e9979254e01ae5eef729f4089bc 选品结果: 入选商品 synthetic-product-demo-m001; 未入选商品 无; C 端投放 synthetic-home-feed / 华东。"
            }
          ],
          "fullSections": [
            {
              "title": "已完成阶段",
              "text": "生成规则与活动草案 → 招商发布审批 → 券批次物化与招商发布 → 报名与商品圈选 → 动态业务确认 → 报名商品关联券批次 → 提交并等待招后选品 → C 端发布审批 → C 端投放 → 通知商家并闭环"
            },
            {
              "title": "选品与券关联",
              "text": "提交1件；选中1件；待定0件；关联券1件"
            },
            {
              "title": "模拟投放",
              "text": "渠道：synthetic-home-feed；状态：published"
            },
            {
              "title": "模拟商家通知",
              "text": "demo-m001：sent · 活动 guided-campaign-11d87e9979254e01ae5eef729f4089bc 选品结果: 入选商品 synthetic-product-demo-m001; 未入选商品 无; C 端投放 synthetic-home-feed / 华东。"
            }
          ],
          "nextStep": "@OriaTest 归因示例"
        }
      ]
    },
    {
      "label": "07 · 经营归因",
      "title": "让解释带着可回查证据",
      "text": "同一群里切换到经营异常归因。受限只读工具提供调查数据，本片使用已审阅案例的 Mock 回放。",
      "fact": "有界调查 · 只读工具 · 引用证据",
      "cards": [
        {
          "sourceSequence": 16,
          "command": "归因示例",
          "title": "经营异常归因",
          "summary": "为什么 2026-08-31 华东正餐招商核销转化率明显下降?\n结论类型：attributed\n受控合成变体中，最有证据支持的解释是华东正餐激励活动于 08-30 结束，导致 08-31 后核销/确认骤降；这是基于局部退出、稳定对照和稳定大盘的归因，不外推为真实业务定律。",
          "sections": [
            {
              "title": "有界只读调查",
              "text": "Mock 回放 · 4轮模型 · 3次工具\nquery_funnel → query_activity → query_market_overview"
            },
            {
              "title": "证据1",
              "text": "query_funnel · /rows/1/metrics/redemption_rate\n0.700787"
            },
            {
              "title": "证据2",
              "text": "query_activity · /activities/0/ends_on\n\"2026-08-30\""
            },
            {
              "title": "证据3",
              "text": "query_market_overview · /segments/0/redemption_rate_change\n0.001467"
            }
          ],
          "fullSections": [
            {
              "title": "有界只读调查",
              "text": "Mock 回放 · 4轮模型 · 3次工具\nquery_funnel → query_activity → query_market_overview"
            },
            {
              "title": "证据1",
              "text": "query_funnel · /rows/1/metrics/redemption_rate\n0.700787"
            },
            {
              "title": "证据2",
              "text": "query_activity · /activities/0/ends_on\n\"2026-08-30\""
            },
            {
              "title": "证据3",
              "text": "query_market_overview · /segments/0/redemption_rate_change\n0.001467"
            }
          ],
          "nextStep": "@OriaTest 证据冲突示例，或 @OriaTest 证据不足示例"
        }
      ]
    },
    {
      "label": "08 · 结论边界",
      "title": "保留冲突，证据不足则弃答",
      "text": "证据相互冲突时保留分歧；证据不足时明确弃答，帮助开发者识别 Agent 的能力边界。",
      "fact": "conflicting / insufficient",
      "cards": [
        {
          "sourceSequence": 17,
          "command": "证据冲突示例",
          "title": "经营异常归因",
          "summary": "华东正餐报名和核销同时下降,根因是什么?\n结论类型：conflicting\nFixture confidence is uncalibrated.",
          "sections": [],
          "fullSections": [
            {
              "title": "有界只读调查",
              "text": "Mock 回放 · 2轮模型 · 1次工具\nquery_funnel"
            },
            {
              "title": "证据1",
              "text": "query_funnel · /rows/1/metrics/redemption_rate\n0.707124"
            }
          ],
          "nextStep": "@OriaTest 证据冲突示例，或 @OriaTest 证据不足示例"
        },
        {
          "sourceSequence": 18,
          "command": "证据不足示例",
          "title": "经营异常归因",
          "summary": "华东正餐核销率骤降，但活动数据缺失，根因是什么?\n结论类型：insufficient\nFixture confidence is uncalibrated.",
          "sections": [],
          "fullSections": [
            {
              "title": "有界只读调查",
              "text": "Mock 回放 · 4轮模型 · 3次工具\nquery_funnel → query_activity → query_market_overview"
            }
          ],
          "nextStep": "@OriaTest 证据冲突示例，或 @OriaTest 证据不足示例"
        }
      ]
    },
    {
      "label": "09 · 规则复盘",
      "title": "流程结束后，回查规则版本",
      "text": "实际群聊继续查询六类规则并翻页，查看生效信息与来源版本。此处展示实际回复节选。",
      "fact": "六类规则 · 生效信息 · 来源版本",
      "cards": [
        {
          "sourceSequence": 19,
          "command": "规则",
          "title": "10/10 通知商家并闭环",
          "summary": "全部十个业务步骤已完成。",
          "sections": [
            {
              "title": "基础信息",
              "text": "模板 夏季餐饮活动模板; 类型 商户招募; 商品范围 合格餐饮商品\n生效：2026-07-01T00:00:00+08:00/2026-08-31T23:59:59+08:00\n来源版本：1.0.0"
            }
          ],
          "fullSections": [
            {
              "title": "基础信息",
              "text": "模板 夏季餐饮活动模板; 类型 商户招募; 商品范围 合格餐饮商品\n生效：2026-07-01T00:00:00+08:00/2026-08-31T23:59:59+08:00\n来源版本：1.0.0"
            },
            {
              "title": "招商范围",
              "text": "类目 餐饮; 城市 上海, 杭州; 报名系统 演示报名系统\n生效：2026-07-10T04:00:00+00:00\n来源版本：1.0.0"
            },
            {
              "title": "报名规则",
              "text": "模式 混合模式; 圈品策略 商品圈选策略（合成）\n生效：2026-07-05T00:00:00+08:00/2026-08-15T23:59:59+08:00\n来源版本：1.0.0"
            },
            {
              "title": "优惠档位",
              "text": "基础档、膨胀档; 预算上限 100000.00 CNY\n生效：2026-07-01T00:00:00+08:00/2026-08-31T23:59:59+08:00\n来源版本：1.0.0"
            },
            {
              "title": "确认规则",
              "text": "商家 → 销售 → 销售经理; 超时 拒绝\n生效：2026-07-10T04:00:00+00:00\n来源版本：1.0.0"
            }
          ],
          "nextStep": "@OriaTest 归因示例；更多内容：@OriaTest 第2页（共2页）"
        },
        {
          "sourceSequence": 20,
          "command": "第2页",
          "title": "10/10 通知商家并闭环",
          "summary": "全部十个业务步骤已完成。",
          "sections": [
            {
              "title": "商家端素材",
              "text": "标题 虚构华东餐饮夏季活动; 标签 虚构, 离线演示\n生效：2026-07-01T00:00:00+08:00/2026-08-31T23:59:59+08:00\n来源版本：1.0.0"
            }
          ],
          "fullSections": [
            {
              "title": "商家端素材",
              "text": "标题 虚构华东餐饮夏季活动; 标签 虚构, 离线演示\n生效：2026-07-01T00:00:00+08:00/2026-08-31T23:59:59+08:00\n来源版本：1.0.0"
            }
          ],
          "nextStep": "@OriaTest 归因示例；更多内容：@OriaTest 第2页（共2页）"
        }
      ],
      "rules": true
    }
  ]
};
