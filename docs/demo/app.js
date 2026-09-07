/* Oria P1 demo renderer: frozen traces only, no network, no innerHTML with data. */
(function () {
  "use strict";

  var scenarioA = window.ORIA_DEMO_SCENARIO_A || { steps: [] };
  var scenarioB = window.ORIA_DEMO_SCENARIO_B || { cases: [], blocked_sample: null };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge " + kind, text);
  }

  function block(title) {
    var section = el("section", "block");
    section.appendChild(el("h3", null, title));
    return section;
  }

  function table(headers, rows) {
    var t = el("table", "data");
    var thead = el("thead");
    var hr = el("tr");
    headers.forEach(function (h) { hr.appendChild(el("th", null, h)); });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr");
      row.forEach(function (cell) {
        var td = el("td");
        if (cell && cell.nodeType) td.appendChild(cell);
        else td.textContent = cell === null || cell === undefined ? "—" : String(cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    return t;
  }

  function kvGrid(pairs) {
    var grid = el("div", "kv-grid");
    pairs.forEach(function (pair) {
      var item = el("div");
      item.appendChild(el("span", "k", pair[0] + "："));
      item.appendChild(el("span", null, pair[1]));
      grid.appendChild(item);
    });
    return grid;
  }

  /* ---------- Tabs ---------- */

  function activateTab(panelId) {
    var button = document.querySelector('.tab[data-panel="' + panelId + '"]');
    if (!button) return;
    document.querySelectorAll(".tab").forEach(function (tab) { tab.classList.remove("active"); });
    document.querySelectorAll(".panel").forEach(function (panel) { panel.classList.remove("active"); });
    button.classList.add("active");
    document.getElementById(panelId).classList.add("active");
  }

  document.getElementById("tabs").addEventListener("click", function (event) {
    var button = event.target.closest(".tab");
    if (!button) return;
    activateTab(button.dataset.panel);
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", "#" + button.dataset.panel);
    }
  });

  /* ---------- Scenario A ---------- */

  var STAGE_COUNT = 10;
  var steps = scenarioA.steps || [];
  var finalStage = steps.length ? steps[steps.length - 1].view.stage_index : 1;

  function renderStepper(activeStage, completedThrough) {
    var stepper = document.getElementById("stage-stepper");
    stepper.textContent = "";
    var names = (scenarioA.stages || []).length === STAGE_COUNT
      ? scenarioA.stages
      : steps.map(function (s) { return s.label; });
    for (var i = 1; i <= STAGE_COUNT; i += 1) {
      var node = el("div", "stage-node");
      node.appendChild(el("span", "stage-no", "第 " + i + " 步"));
      node.appendChild(document.createTextNode(names[i - 1] || ""));
      if (i < activeStage || i <= completedThrough) node.classList.add("done");
      if (i === activeStage) node.classList.add("current");
      stepper.appendChild(node);
    }
  }

  function renderTimeline(selectedIndex) {
    var timeline = document.getElementById("trace-timeline");
    timeline.textContent = "";
    steps.forEach(function (step, index) {
      var item = el("li");
      if (index === selectedIndex) item.classList.add("selected");
      item.appendChild(el("span", "step-label", (index + 1) + ". " + step.label));
      item.appendChild(el("span", "step-action", step.action));
      item.addEventListener("click", function () { selectStep(index); });
      timeline.appendChild(item);
    });
  }

  function addIfPresent(container, condition, builder) {
    if (condition) container.appendChild(builder());
  }

  function rulesBlock(view) {
    var section = block("规则快照摘要（六类公开规则，含生效窗口）");
    section.appendChild(table(
      ["规则类别", "关键内容", "生效窗口", "版本"],
      (view.rule_summary || []).map(function (rule) {
        return [rule.category, rule.key_value, rule.effective_time, rule.source_version];
      })
    ));
    return section;
  }

  function merchantsBlock(view) {
    var matches = view.merchant_matches || {};
    var section = block("硬资格商家预筛 + LLM 软排序（零写工具）");
    section.appendChild(kvGrid([
      ["评估商家数", matches.evaluated_count],
      ["硬资格合格数", matches.matched_count],
    ]));
    section.appendChild(table(
      ["排名", "商家", "硬资格", "推荐理由"],
      (matches.items || []).map(function (m) {
        return [m.llm_rank, m.display_name + "（" + m.merchant_id + "）", m.hard_eligibility, m.recommendation_reason];
      })
    ));
    return section;
  }

  function approvalBlock(view) {
    var a = view.approval_summary;
    var section = block("审批中断（双真实 interrupt，冻结不可变计划）");
    section.appendChild(kvGrid([
      ["审批类型", a.kind],
      ["审批 ID", a.approval_id],
      ["状态", a.status],
      ["说明", a.description],
    ]));
    return section;
  }

  function confirmationBlock(view) {
    var c = view.confirmation_progress;
    var section = block("动态业务确认链（规则动态生成角色序列）");
    section.appendChild(kvGrid([
      ["当前进度", "第 " + c.current_level + " 级 / 共 " + c.total_levels + " 级"],
      ["当前角色", c.current_role],
      ["下一角色", c.next_role || "—"],
    ]));
    return section;
  }

  function enrollmentBlock(view) {
    var section = block("报名商品（双来源汇聚，唯一键约束）");
    section.appendChild(table(
      ["商家", "商品", "来源", "状态"],
      (view.enrollment_items || []).map(function (item) {
        return [item.merchant_id, item.product_ref, (item.sources || []).join(" + "), item.status];
      })
    ));
    return section;
  }

  function couponBlock(view) {
    var c = view.coupon_batch;
    var section = block("券批次（执行账本物化，幂等）");
    section.appendChild(kvGrid([
      ["批次", c.coupon_batch_id],
      ["面额", (c.face_values || []).join("；")],
      ["预算上限", c.budget_cap + " " + c.currency],
      ["状态", c.status],
    ]));
    return section;
  }

  function selectionBlock(view) {
    var s = view.selection_summary || {};
    var section = block("招后选品（异步等待 + 受信结果事件）");
    section.appendChild(kvGrid([
      ["提交商品数", s.submitted_count],
      ["已收决定数", s.received_count],
      ["入选 / 拒绝", (s.selected_count || 0) + " / " + (s.rejected_count || 0)],
    ]));
    if ((view.selection_decisions || []).length) {
      section.appendChild(table(
        ["商品", "决定", "版本"],
        view.selection_decisions.map(function (d) {
          return [d.product_ref, d.decision, d.selection_version];
        })
      ));
    }
    return section;
  }

  function placementBlock(view) {
    var p = view.placement;
    var section = block("C 端投放（审批后发布，结果变化使审批失效）");
    section.appendChild(kvGrid([
      ["渠道", p.channel],
      ["区域", p.region],
      ["入选商品", (p.selected_products || []).join("、")],
      ["状态", p.status],
    ]));
    var note = el("p", null, p.content_example);
    section.appendChild(note);
    return section;
  }

  function notificationBlock(view) {
    var section = block("商家通知（死信可收敛）");
    section.appendChild(table(
      ["商家", "渠道", "状态", "内容"],
      (view.notification_messages || []).map(function (n) {
        return [n.merchant_id, n.channel, n.status, n.message];
      })
    ));
    return section;
  }

  function renderDetail(step) {
    var view = step.view || {};
    var detail = document.getElementById("trace-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, step.label));
    head.appendChild(badge(step.status === "completed" ? "流程已完成" : "等待外部动作", step.status === "completed" ? "ok" : "waiting"));
    head.appendChild(el("span", "stage-tag", "阶段 " + view.stage_index + " / " + view.stage_total));
    detail.appendChild(head);
    detail.appendChild(el("p", "pending-action", "操作：" + step.action + "；当前阶段：" + view.current_stage + "；下一步：" + (view.pending_action || "—")));

    addIfPresent(detail, view.terminal_outcome, function () {
      var section = block("终态");
      section.appendChild(badge("completed: C 端投放与商家通知已闭环", "ok"));
      return section;
    });
    addIfPresent(detail, (view.rule_summary || []).length, function () { return rulesBlock(view); });
    addIfPresent(detail, view.merchant_matches && (view.merchant_matches.items || []).length, function () { return merchantsBlock(view); });
    addIfPresent(detail, view.approval_summary, function () { return approvalBlock(view); });
    addIfPresent(detail, view.confirmation_progress, function () { return confirmationBlock(view); });
    addIfPresent(detail, (view.enrollment_items || []).length, function () { return enrollmentBlock(view); });
    addIfPresent(detail, view.coupon_batch, function () { return couponBlock(view); });
    addIfPresent(detail, view.selection_summary && view.selection_summary.submitted_count, function () { return selectionBlock(view); });
    addIfPresent(detail, view.placement, function () { return placementBlock(view); });
    addIfPresent(detail, (view.notification_messages || []).length, function () { return notificationBlock(view); });
  }

  function selectStep(index) {
    var view = steps[index].view;
    renderStepper(view.stage_index, view.stage_index - 1);
    renderTimeline(index);
    renderDetail(steps[index]);
  }

  /* ---------- Scenario B ---------- */

  var OUTCOME_META = {
    attributed: { label: "归因成立 attributed", kind: "ok", desc: "单一异常环节，给出有边界的观察性归因" },
    conflicting: { label: "冲突保留 conflicting", kind: "waiting", desc: "两个独立异常环节、无共同机制，保留双假设" },
    insufficient: { label: "证据不足弃答 insufficient", kind: "info", desc: "活动数据缺失，明确弃答并请求关键数据" },
    blocked: { label: "契约拦停示例", kind: "bad", desc: "模型试图合并单因，被因果契约两次拦停" },
  };

  var STAGE_LABELS = {
    impression_to_visit: "曝光→访问",
    visit_to_enrollment: "访问→报名",
    enrollment_to_confirmation: "报名→确认",
    confirmation_to_redemption: "确认→核销",
  };

  var caseEntries = (scenarioB.cases || []).map(function (c) { return { type: "case", data: c }; });
  if (scenarioB.blocked_sample) caseEntries.push({ type: "blocked", data: scenarioB.blocked_sample });

  function renderCaseCards(selectedIndex) {
    var cards = document.getElementById("case-cards");
    cards.textContent = "";
    caseEntries.forEach(function (entry, index) {
      var meta = entry.type === "blocked" ? OUTCOME_META.blocked : OUTCOME_META[entry.data.outcome];
      var card = el("button", "case-card" + (index === selectedIndex ? " selected" : ""));
      card.appendChild(el("h3", null, meta.label));
      card.appendChild(el("p", null, meta.desc));
      card.addEventListener("click", function () { selectCase(index); });
      cards.appendChild(card);
    });
  }

  function statsRow(stats) {
    var row = el("div", "stats-row");
    [
      ["模型轮次", stats.model_turns],
      ["工具调用", stats.tool_calls_total],
      ["输入 Token", stats.input_tokens],
      ["输出 Token", stats.output_tokens],
      ["API 请求", stats.request_count],
    ].forEach(function (pair) {
      var item = el("span");
      item.appendChild(document.createTextNode(pair[0] + " "));
      item.appendChild(el("strong", null, pair[1]));
      row.appendChild(item);
    });
    return row;
  }

  function renderCase(caseData) {
    var meta = OUTCOME_META[caseData.outcome];
    var detail = document.getElementById("case-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, meta.label));
    head.appendChild(badge(caseData.outcome, meta.kind));
    head.appendChild(el("span", "stage-tag", "置信度 " + caseData.confidence));
    detail.appendChild(head);
    detail.appendChild(el("p", "pending-action", "问题：" + caseData.question));
    detail.appendChild(statsRow(caseData.stats));

    if (caseData.conclusion_text) {
      var conclusionBlock = block("结论（保留不确定性，不作确定性因果宣称）");
      conclusionBlock.appendChild(el("p", "conclusion-text", caseData.conclusion_text));
      detail.appendChild(conclusionBlock);
    }

    if ((caseData.hypotheses || []).length) {
      var hypBlock = block("候选假设（含不确定性声明）");
      caseData.hypotheses.forEach(function (h) {
        var card = el("div", "hypothesis");
        card.appendChild(el("div", null, h.statement));
        card.appendChild(el("div", "uncertainty", "不确定性：" + h.uncertainty));
        hypBlock.appendChild(card);
      });
      detail.appendChild(hypBlock);
    }

    var assessment = caseData.causal_assessment || {};
    var causalBlock = block("因果审计（先于 outcome 填写的显式契约）");
    var stages = el("div");
    (assessment.anomalous_conversion_stages || []).forEach(function (s) {
      stages.appendChild(el("span", "stage-chip", STAGE_LABELS[s] || s));
    });
    causalBlock.appendChild(stages);
    causalBlock.appendChild(kvGrid([
      ["独立异常环节数", (assessment.anomalous_conversion_stages || []).length],
      ["观察到共同机制", assessment.shared_mechanism_observed ? "是" : "否"],
      ["机制证据条数", assessment.mechanism_evidence_count],
    ]));
    detail.appendChild(causalBlock);

    if ((caseData.requested_data || []).length) {
      var reqBlock = block("弃答：请求的关键数据");
      var list = el("ul");
      caseData.requested_data.forEach(function (item) { list.appendChild(el("li", null, item)); });
      reqBlock.appendChild(list);
      detail.appendChild(reqBlock);
    }

    var evBlock = block("证据引用（全部通过逐值精确校验）");
    evBlock.appendChild(table(
      ["工具", "JSON Pointer", "原值", "支持假设"],
      (caseData.evidence || []).map(function (e) {
        return [
          e.tool_name,
          el("span", "mono", e.data_path),
          JSON.stringify(e.value),
          (e.supports || []).join("、"),
        ];
      })
    ));
    detail.appendChild(evBlock);

    detail.appendChild(el("p", "pending-action", "证据文件：" + caseData.evidence_file));
  }

  function renderBlocked(sample) {
    var detail = document.getElementById("case-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, "契约拦停示例"));
    head.appendChild(badge("schema_validation_failed", "bad"));
    detail.appendChild(head);
    detail.appendChild(el("p", "pending-action", "问题：" + sample.question));

    var attempt = block("模型尝试的输出");
    attempt.appendChild(kvGrid([
      ["尝试的 outcome", sample.attempted_outcome],
      ["模型自报异常环节", (sample.attempted_stages || []).map(function (s) { return STAGE_LABELS[s] || s; }).join("、")],
      ["自报观察到共同机制", sample.shared_mechanism_observed ? "是" : "否"],
    ]));
    detail.appendChild(attempt);

    var blockedBlock = block("拦停结果");
    var note = el("p", "blocked-note",
      "违反规则：" + sample.blocking_rule + "。" + sample.note +
      "（终止原因：" + sample.termination_reason + "）");
    blockedBlock.appendChild(note);
    detail.appendChild(blockedBlock);
  }

  function selectCase(index) {
    renderCaseCards(index);
    var entry = caseEntries[index];
    if (entry.type === "blocked") renderBlocked(entry.data);
    else renderCase(entry.data);
  }

  /* ---------- Boot ---------- */

  if (steps.length) selectStep(steps.length - 1);
  if (caseEntries.length) selectCase(0);
  if (window.location.hash) activateTab(window.location.hash.slice(1));
})();
