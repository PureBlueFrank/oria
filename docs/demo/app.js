/* Oria demo renderer: frozen traces only, no network, no innerHTML with data. */
(function () {
  "use strict";

  var scenarioA = window.ORIA_DEMO_SCENARIO_A || { steps: [] };
  var scenarioB = window.ORIA_DEMO_SCENARIO_B || { cases: [], blocked_sample: null };
  var steps = scenarioA.steps || [];
  var selectedStepIndex = steps.length ? steps.length - 1 : 0;
  var selectedCaseIndex = 0;
  var activePanel = "scenario-a";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function displayValue(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }

  function badge(text, kind) {
    return el("span", "badge " + kind, text);
  }

  function block(title) {
    var section = el("section", "block");
    section.appendChild(el("h3", null, title));
    return section;
  }

  function dataTable(headers, rows, label) {
    var wrapper = el("div", "table-scroll");
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "region");
    wrapper.setAttribute("aria-label", label + "（可横向滚动）");

    var table = el("table", "data");
    table.appendChild(el("caption", "sr-only", label));
    var thead = el("thead");
    var headingRow = el("tr");
    headers.forEach(function (heading) {
      var th = el("th", null, heading);
      th.scope = "col";
      headingRow.appendChild(th);
    });
    thead.appendChild(headingRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tableRow = el("tr");
      row.forEach(function (cell) {
        var td = el("td");
        if (cell && cell.nodeType) td.appendChild(cell);
        else td.textContent = displayValue(cell);
        tableRow.appendChild(td);
      });
      tbody.appendChild(tableRow);
    });
    table.appendChild(tbody);
    wrapper.appendChild(table);
    return wrapper;
  }

  function kvGrid(pairs) {
    var grid = el("div", "kv-grid");
    pairs.forEach(function (pair) {
      var item = el("div");
      item.appendChild(el("span", "k", pair[0]));
      item.appendChild(el("span", null, displayValue(pair[1])));
      grid.appendChild(item);
    });
    return grid;
  }

  function statusDot(kind) {
    var dot = el("i", "status-dot " + kind);
    dot.setAttribute("aria-hidden", "true");
    return dot;
  }

  function announce(message) {
    document.getElementById("selection-status").textContent = message;
  }

  function replaceHash(value) {
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", value);
    } else {
      window.location.hash = value.slice(1);
    }
  }

  /* ---------- Tabs and deep links ---------- */

  var allowedPanels = {
    "scenario-a": true,
    "scenario-b": true,
    evidence: true,
  };

  function currentHash(panelId) {
    if (panelId === "scenario-a") return "#scenario-a/step-" + (selectedStepIndex + 1);
    if (panelId === "scenario-b") {
      var currentEntry = caseEntries[selectedCaseIndex];
      var slug = currentEntry && currentEntry.type === "case" ? currentEntry.data.case_id : "blocked";
      return "#scenario-b/" + slug;
    }
    return "#evidence";
  }

  function activateTab(panelId, updateHash) {
    if (!allowedPanels[panelId]) return;
    activePanel = panelId;
    document.querySelectorAll(".tab").forEach(function (tab) {
      var selected = tab.dataset.panel === panelId;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll(".panel").forEach(function (panel) {
      var selected = panel.id === panelId;
      panel.classList.toggle("active", selected);
      panel.hidden = !selected;
    });
    if (updateHash) replaceHash(currentHash(panelId));
  }

  var tabs = document.getElementById("tabs");
  tabs.addEventListener("click", function (event) {
    var button = event.target.closest(".tab");
    if (!button) return;
    activateTab(button.dataset.panel, true);
  });

  tabs.addEventListener("keydown", function (event) {
    var button = event.target.closest(".tab");
    if (!button) return;
    var tabButtons = Array.prototype.slice.call(tabs.querySelectorAll(".tab"));
    var index = tabButtons.indexOf(button);
    var nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabButtons.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabButtons.length) % tabButtons.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabButtons.length - 1;
    else return;
    event.preventDefault();
    tabButtons[nextIndex].focus();
    tabButtons[nextIndex].click();
  });

  /* ---------- Scenario A ---------- */

  function renderStepper(selectedIndex) {
    var stepper = document.getElementById("stage-stepper");
    stepper.textContent = "";
    steps.forEach(function (step, index) {
      var state = index < selectedIndex ? "done" : index === selectedIndex ? "current" : "queued";
      var stateLabel = state === "done" ? "已完成" : state === "current" ? "当前步骤" : "等待";
      var node = el("button", "stage-node " + state);
      node.type = "button";
      node.dataset.stepIndex = String(index);
      node.setAttribute("aria-label", "第 " + (index + 1) + " 步，" + step.label + "，" + stateLabel);
      if (index === selectedIndex) node.setAttribute("aria-current", "step");

      var number = el("span", "stage-no");
      number.appendChild(statusDot(state === "done" ? "complete" : state));
      number.appendChild(document.createTextNode("STEP " + String(index + 1).padStart(2, "0")));
      node.appendChild(number);
      node.appendChild(el("span", "stage-label", step.label));
      node.addEventListener("click", function () { selectStep(index, true); });
      stepper.appendChild(node);
    });
  }

  function renderTimeline(selectedIndex) {
    var timeline = document.getElementById("trace-timeline");
    timeline.textContent = "";
    steps.forEach(function (step, index) {
      var item = el("li", index === selectedIndex ? "selected" : null);
      var button = el("button", "timeline-button");
      button.type = "button";
      button.dataset.stepIndex = String(index);
      button.setAttribute("aria-pressed", index === selectedIndex ? "true" : "false");

      var topline = el("span", "timeline-topline");
      topline.appendChild(el("span", "step-label", String(index + 1).padStart(2, "0") + " · " + step.label));
      topline.appendChild(el("span", "snapshot-state", step.status === "completed" ? "CLOSED" : "PAUSED"));
      button.appendChild(topline);
      button.appendChild(el("span", "step-action", step.action));
      button.addEventListener("click", function () { selectStep(index, true); });
      item.appendChild(button);
      timeline.appendChild(item);
    });
  }

  function addIfPresent(container, condition, builder) {
    if (condition) container.appendChild(builder());
  }

  function rulesBlock(view) {
    var section = block("规则快照摘要（六类公开规则，含生效窗口）");
    section.appendChild(dataTable(
      ["规则类别", "关键内容", "生效窗口", "版本"],
      (view.rule_summary || []).map(function (rule) {
        return [rule.category, rule.key_value, rule.effective_time, rule.source_version];
      }),
      "规则快照摘要"
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
    section.appendChild(dataTable(
      ["排名", "商家", "硬资格", "推荐理由"],
      (matches.items || []).map(function (merchant) {
        return [
          merchant.llm_rank,
          merchant.display_name + "（" + merchant.merchant_id + "）",
          merchant.hard_eligibility,
          merchant.recommendation_reason,
        ];
      }),
      "商家预筛与软排序"
    ));
    return section;
  }

  function approvalBlock(view) {
    var approval = view.approval_summary;
    var section = block("审批中断（双真实 interrupt，冻结不可变计划）");
    section.appendChild(kvGrid([
      ["审批类型", approval.kind],
      ["审批 ID", approval.approval_id],
      ["状态", approval.status],
      ["说明", approval.description],
    ]));
    return section;
  }

  function confirmationBlock(view) {
    var progress = view.confirmation_progress;
    var section = block("动态业务确认链（规则动态生成角色序列）");
    section.appendChild(kvGrid([
      ["当前进度", "第 " + progress.current_level + " 级 / 共 " + progress.total_levels + " 级"],
      ["当前角色", progress.current_role],
      ["下一角色", progress.next_role || "—"],
    ]));
    return section;
  }

  function enrollmentBlock(view) {
    var section = block("报名商品（双来源汇聚，唯一键约束）");
    section.appendChild(dataTable(
      ["商家", "商品", "来源", "状态"],
      (view.enrollment_items || []).map(function (item) {
        return [item.merchant_id, item.product_ref, (item.sources || []).join(" + "), item.status];
      }),
      "报名商品"
    ));
    return section;
  }

  function couponBlock(view) {
    var coupon = view.coupon_batch;
    var section = block("券批次（执行账本物化，幂等）");
    section.appendChild(kvGrid([
      ["批次", coupon.coupon_batch_id],
      ["面额", (coupon.face_values || []).join("；")],
      ["预算上限", coupon.budget_cap + " " + coupon.currency],
      ["状态", coupon.status],
    ]));
    return section;
  }

  function selectionBlock(view) {
    var summary = view.selection_summary || {};
    var section = block("招后选品（异步等待 + 受信结果事件）");
    section.appendChild(kvGrid([
      ["提交商品数", summary.submitted_count],
      ["已收决定数", summary.received_count],
      ["入选 / 拒绝", (summary.selected_count || 0) + " / " + (summary.rejected_count || 0)],
    ]));
    if ((view.selection_decisions || []).length) {
      section.appendChild(dataTable(
        ["商品", "决定", "版本"],
        view.selection_decisions.map(function (decision) {
          return [decision.product_ref, decision.decision, decision.selection_version];
        }),
        "选品决定"
      ));
    }
    return section;
  }

  function placementBlock(view) {
    var placement = view.placement;
    var section = block("C 端投放（审批后发布，结果变化使审批失效）");
    section.appendChild(kvGrid([
      ["渠道", placement.channel],
      ["区域", placement.region],
      ["入选商品", (placement.selected_products || []).join("、")],
      ["状态", placement.status],
    ]));
    section.appendChild(el("p", "pending-action", placement.content_example));
    return section;
  }

  function notificationBlock(view) {
    var section = block("商家通知（死信可收敛）");
    section.appendChild(dataTable(
      ["商家", "渠道", "状态", "内容"],
      (view.notification_messages || []).map(function (message) {
        return [message.merchant_id, message.channel, message.status, message.message];
      }),
      "商家通知"
    ));
    return section;
  }

  function renderDetail(step, index) {
    var view = step.view || {};
    var detail = document.getElementById("trace-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, step.label));
    head.appendChild(badge(step.status === "completed" ? "流程已完成" : "等待外部动作", step.status === "completed" ? "ok" : "waiting"));
    head.appendChild(el("span", "stage-tag", "SNAPSHOT " + String(index + 1).padStart(2, "0") + " / " + steps.length));
    head.appendChild(el("span", "stage-tag", "阶段 " + view.stage_index + " / " + view.stage_total));
    detail.appendChild(head);
    detail.appendChild(el(
      "p",
      "pending-action",
      "操作：" + step.action + "；当前阶段：" + displayValue(view.current_stage) + "；下一步：" + displayValue(view.pending_action)
    ));

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

  function selectStep(index, updateHash) {
    if (!steps.length || index < 0 || index >= steps.length) return;
    selectedStepIndex = index;
    renderStepper(index);
    renderTimeline(index);
    renderDetail(steps[index], index);
    if (updateHash && activePanel === "scenario-a") replaceHash(currentHash("scenario-a"));
    announce("已选择场景 A 第 " + (index + 1) + " 个检查点：" + steps[index].label);
  }

  document.getElementById("stage-stepper").addEventListener("keydown", function (event) {
    var button = event.target.closest(".stage-node");
    if (!button) return;
    var stageButtons = Array.prototype.slice.call(document.querySelectorAll(".stage-node"));
    var index = stageButtons.indexOf(button);
    var nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = Math.min(index + 1, stageButtons.length - 1);
    else if (event.key === "ArrowLeft") nextIndex = Math.max(index - 1, 0);
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = stageButtons.length - 1;
    else return;
    event.preventDefault();
    stageButtons[nextIndex].click();
    document.querySelectorAll(".stage-node")[nextIndex].focus();
  });

  document.getElementById("trace-timeline").addEventListener("keydown", function (event) {
    var button = event.target.closest(".timeline-button");
    if (!button || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    var timelineButtons = Array.prototype.slice.call(document.querySelectorAll(".timeline-button"));
    var index = timelineButtons.indexOf(button);
    var nextIndex = event.key === "ArrowDown"
      ? Math.min(index + 1, timelineButtons.length - 1)
      : Math.max(index - 1, 0);
    event.preventDefault();
    timelineButtons[nextIndex].click();
    document.querySelectorAll(".timeline-button")[nextIndex].focus();
  });

  /* ---------- Scenario B ---------- */

  var OUTCOME_META = {
    attributed: { label: "归因成立", code: "attributed", kind: "ok", desc: "单一异常环节，给出有边界的观察性归因" },
    conflicting: { label: "冲突保留", code: "conflicting", kind: "waiting", desc: "两个独立异常环节、无共同机制，保留双假设" },
    insufficient: { label: "证据不足弃答", code: "insufficient", kind: "info", desc: "活动数据缺失，明确弃答并请求关键数据" },
    blocked: { label: "契约拦停", code: "blocked", kind: "bad", desc: "模型试图合并单因，被因果契约两次拦停" },
  };

  var STAGE_LABELS = {
    impression_to_visit: "曝光→访问",
    visit_to_enrollment: "访问→报名",
    enrollment_to_confirmation: "报名→确认",
    confirmation_to_redemption: "确认→核销",
  };

  var caseEntries = (scenarioB.cases || []).map(function (caseData) {
    return { type: "case", data: caseData };
  });
  if (scenarioB.blocked_sample) caseEntries.push({ type: "blocked", data: scenarioB.blocked_sample });

  function caseMeta(entry) {
    if (entry.type === "blocked") return OUTCOME_META.blocked;
    return OUTCOME_META[entry.data.outcome] || { label: entry.data.outcome, code: entry.data.outcome, kind: "neutral", desc: "" };
  }

  function renderCaseCards(selectedIndex) {
    var cards = document.getElementById("case-cards");
    cards.textContent = "";
    caseEntries.forEach(function (entry, index) {
      var meta = caseMeta(entry);
      var card = el("button", "case-card" + (index === selectedIndex ? " selected" : ""));
      card.type = "button";
      card.dataset.caseIndex = String(index);
      card.setAttribute("aria-pressed", index === selectedIndex ? "true" : "false");

      var topline = el("span", "case-card-topline");
      topline.appendChild(badge(meta.code, meta.kind));
      topline.appendChild(el("span", "case-id", entry.type === "case" ? entry.data.case_id : "contract-stop"));
      card.appendChild(topline);
      card.appendChild(el("h3", null, meta.label));
      card.appendChild(el("p", null, meta.desc));
      card.addEventListener("click", function () { selectCase(index, true); });
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
      item.appendChild(document.createTextNode(pair[0]));
      item.appendChild(el("strong", null, displayValue(pair[1])));
      row.appendChild(item);
    });
    return row;
  }

  function renderCase(caseData) {
    var meta = OUTCOME_META[caseData.outcome] || caseMeta({ type: "case", data: caseData });
    var detail = document.getElementById("case-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, meta.label));
    head.appendChild(badge(caseData.outcome, meta.kind));
    head.appendChild(el("span", "stage-tag", "置信度 " + caseData.confidence));
    head.appendChild(el("span", "stage-tag", caseData.case_id));
    detail.appendChild(head);
    detail.appendChild(el("p", "pending-action", "问题：" + caseData.question));
    detail.appendChild(statsRow(caseData.stats || {}));

    if (caseData.conclusion_text) {
      var conclusion = block("结论（保留不确定性，不作确定性因果宣称）");
      conclusion.appendChild(el("p", "conclusion-text", caseData.conclusion_text));
      if (caseData.confidence_explanation) {
        conclusion.appendChild(el("p", "confidence-note", "置信度说明：" + caseData.confidence_explanation));
      }
      detail.appendChild(conclusion);
    }

    if ((caseData.hypotheses || []).length) {
      var hypotheses = block("候选假设（含不确定性声明）");
      caseData.hypotheses.forEach(function (hypothesis) {
        var card = el("div", "hypothesis");
        card.appendChild(el("div", null, hypothesis.statement));
        card.appendChild(el("div", "uncertainty", "不确定性：" + hypothesis.uncertainty));
        hypotheses.appendChild(card);
      });
      detail.appendChild(hypotheses);
    }

    var assessment = caseData.causal_assessment || {};
    var causal = block("因果审计（先于 outcome 填写的显式契约）");
    var stageList = el("div");
    (assessment.anomalous_conversion_stages || []).forEach(function (stage) {
      stageList.appendChild(el("span", "stage-chip", STAGE_LABELS[stage] || stage));
    });
    causal.appendChild(stageList);
    causal.appendChild(kvGrid([
      ["独立异常环节数", (assessment.anomalous_conversion_stages || []).length],
      ["观察到共同机制", assessment.shared_mechanism_observed ? "是" : "否"],
      ["机制证据条数", assessment.mechanism_evidence_count],
    ]));
    detail.appendChild(causal);

    if ((caseData.requested_data || []).length) {
      var requested = block("弃答：请求的关键数据");
      var list = el("ul");
      caseData.requested_data.forEach(function (item) { list.appendChild(el("li", null, item)); });
      requested.appendChild(list);
      detail.appendChild(requested);
    }

    var evidence = block("证据引用（全部通过逐值精确校验）");
    evidence.appendChild(dataTable(
      ["工具", "JSON Pointer", "原值", "支持假设"],
      (caseData.evidence || []).map(function (item) {
        return [
          item.tool_name,
          el("span", "mono", item.data_path),
          JSON.stringify(item.value),
          (item.supports || []).join("、"),
        ];
      }),
      "逐值校验证据"
    ));
    detail.appendChild(evidence);
    detail.appendChild(el("p", "pending-action mono", "证据文件：" + caseData.evidence_file));
  }

  function renderBlocked(sample) {
    var detail = document.getElementById("case-detail");
    detail.textContent = "";

    var head = el("div", "detail-head");
    head.appendChild(el("h2", null, "契约拦停"));
    head.appendChild(badge("schema_validation_failed", "bad"));
    detail.appendChild(head);
    detail.appendChild(el("p", "pending-action", "问题：" + sample.question));

    var attempt = block("模型尝试的输出");
    attempt.appendChild(kvGrid([
      ["尝试的 outcome", sample.attempted_outcome],
      ["模型自报异常环节", (sample.attempted_stages || []).map(function (stage) { return STAGE_LABELS[stage] || stage; }).join("、")],
      ["自报观察到共同机制", sample.shared_mechanism_observed ? "是" : "否"],
    ]));
    detail.appendChild(attempt);

    var stopped = block("拦停结果");
    stopped.appendChild(el(
      "p",
      "blocked-note",
      "违反规则：" + sample.blocking_rule + "。" + sample.note + "（终止原因：" + sample.termination_reason + "）"
    ));
    detail.appendChild(stopped);
  }

  function selectCase(index, updateHash) {
    if (!caseEntries.length || index < 0 || index >= caseEntries.length) return;
    selectedCaseIndex = index;
    renderCaseCards(index);
    var entry = caseEntries[index];
    if (entry.type === "blocked") renderBlocked(entry.data);
    else renderCase(entry.data);
    if (updateHash && activePanel === "scenario-b") replaceHash(currentHash("scenario-b"));
    announce("已选择场景 B 案例：" + caseMeta(entry).label);
  }

  document.getElementById("case-cards").addEventListener("keydown", function (event) {
    var button = event.target.closest(".case-card");
    if (!button) return;
    var caseButtons = Array.prototype.slice.call(document.querySelectorAll(".case-card"));
    var index = caseButtons.indexOf(button);
    var nextIndex = index;
    var columns = window.getComputedStyle(document.getElementById("case-cards"))
      .gridTemplateColumns.split(" ").filter(Boolean).length || 1;
    if (event.key === "ArrowRight") nextIndex = Math.min(index + 1, caseButtons.length - 1);
    else if (event.key === "ArrowLeft") nextIndex = Math.max(index - 1, 0);
    else if (event.key === "ArrowDown") nextIndex = Math.min(index + columns, caseButtons.length - 1);
    else if (event.key === "ArrowUp") nextIndex = Math.max(index - columns, 0);
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = caseButtons.length - 1;
    else return;
    event.preventDefault();
    caseButtons[nextIndex].click();
    document.querySelectorAll(".case-card")[nextIndex].focus();
  });

  function syncFromHash() {
    var rawHash = window.location.hash ? window.location.hash.slice(1) : "";
    try {
      rawHash = decodeURIComponent(rawHash);
    } catch (error) {
      rawHash = "";
    }

    if (rawHash.indexOf("evidence-") === 0) {
      activateTab("evidence", false);
      var anchor = document.getElementById(rawHash);
      if (anchor) anchor.scrollIntoView({ behavior: "instant", block: "start" });
      return;
    }

    var parts = rawHash.split("/");
    var panelId = allowedPanels[parts[0]] ? parts[0] : "scenario-a";
    if (panelId === "scenario-a" && parts[1]) {
      var match = /^step-(\d+)$/.exec(parts[1]);
      if (match) {
        var stepIndex = Number(match[1]) - 1;
        if (stepIndex >= 0 && stepIndex < steps.length) selectStep(stepIndex, false);
      }
    }
    if (panelId === "scenario-b" && parts[1]) {
      var caseIndex = caseEntries.findIndex(function (entry) {
        return parts[1] === "blocked"
          ? entry.type === "blocked"
          : entry.type === "case" && entry.data.case_id === parts[1];
      });
      if (caseIndex >= 0) selectCase(caseIndex, false);
    }
    activateTab(panelId, false);
  }

  /* ---------- Boot ---------- */

  if (steps.length) selectStep(selectedStepIndex, false);
  else document.getElementById("trace-detail").appendChild(el("p", "pending-action", "暂无冻结 Trace 数据。"));

  if (caseEntries.length) selectCase(0, false);
  else document.getElementById("case-detail").appendChild(el("p", "pending-action", "暂无冻结归因案例。"));

  syncFromHash();
  window.addEventListener("load", syncFromHash);
  window.addEventListener("hashchange", syncFromHash);
})();
