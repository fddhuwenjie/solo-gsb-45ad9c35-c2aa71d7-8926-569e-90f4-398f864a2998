"use strict";
/* 前端逻辑冒烟测试：node test_frontend.js（无需浏览器与第三方依赖）。
 * 用最小 DOM 桩加载 static/app.js，验证：
 *  1. 时间码解析 / 格式化；
 *  2. 渲染生成的动态行 ID 与各保存函数读取的 ID 一致；
 *  3. 保存发言区间 / 授权时发出的请求体正确；
 *  4. 校验结果渲染包含时间码与状态；
 *  5. 补件批次渲染、试算请求体与试算结果展示；
 *  6. 发行凭据列表渲染、封存请求体、409 阻断展示与查阅复核视图；
 *  7. 渠道投放台账：登记请求体与 409 不留记录提示、列表风险徽章、
 *     查阅封存结论 / 来源缺失、撤下请求体与已撤下展示。 */
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const elements = new Map();
function el(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, value: "", checked: false, textContent: "", innerHTML: "",
      className: "", style: {},
      classList: { toggle() {} },
      addEventListener() {},
    });
  }
  return elements.get(id);
}

const calls = [];
const sandbox = {
  console,
  document: { getElementById: (id) => el(id) },
  window: { addEventListener: () => {} },
  confirm: () => true,
  fetch: async (path, opts) => {
    calls.push({ path, opts });
    const isGet = !opts || !opts.method || opts.method === "GET";
    if (isGet) {
      if (path === "/api/credentials") {
        return { ok: true, status: 200, json: async () => sandbox.__credList || [] };
      }
      if (path === "/api/distributions") {
        return { ok: true, status: 200, json: async () => sandbox.__distList || [] };
      }
      if (path.startsWith("/api/distributions/")) {
        return { ok: true, status: 200, json: async () => sandbox.__distDetail };
      }
      if (path.startsWith("/api/credentials/")) {
        return { ok: true, status: 200, json: async () => sandbox.__credDetail };
      }
      return { ok: true, status: 200, json: async () => sandbox.__test__.state.detail };
    }
    if (path.endsWith("/remediate")) {
      return { ok: true, status: 200, json: async () => sandbox.__remResponse };
    }
    if (path.endsWith("/distributions") && opts && opts.method === "POST") {
      return sandbox.__distCreate();
    }
    if (path.startsWith("/api/distributions/") && path.endsWith("/withdraw")) {
      return sandbox.__distWithdraw();
    }
    if (path.endsWith("/credentials") && opts && opts.method === "POST") {
      return sandbox.__credCreate();
    }
    return { ok: true, status: 200, json: async () => ({}) };
  },
};
vm.createContext(sandbox);
const code = fs.readFileSync("static/app.js", "utf8");
vm.runInContext(
  code + "\nglobalThis.__test__ = { state, render, renderResults, saveSegment, saveLicense, fmt, parseTimecode, renderRemediate, submitRemediate, renderCredentials, createCredential, viewCredential, renderCredentialOptions, renderDistributions, createDistribution, viewDistribution, withdrawDistribution, renderDistributionDetail };",
  sandbox
);
const T = sandbox.__test__;

async function main() {
  // 1. 时间码
  assert.strictEqual(T.parseTimecode("01:00:00"), 3600);
  assert.strictEqual(T.parseTimecode("1:30"), 90);
  assert.strictEqual(T.parseTimecode("90"), 90);
  assert.ok(Number.isNaN(T.parseTimecode("abc")));
  assert.strictEqual(T.fmt(3661), "01:01:01");

  // 2. 渲染与动态 ID 一致性
  T.state.currentId = 1;
  T.state.detail = {
    id: 1, title: "示例", duration_seconds: 3600,
    speakers: [{ id: 1, name: "张三" }, { id: 2, name: "李四" }],
    segments: [{ id: 10, speaker_id: 1, start_seconds: 0, end_seconds: 600 }],
    licenses: [{
      id: 20, speaker_id: 2, start_seconds: 0, end_seconds: 3600,
      allow_media: ["audio"], deny_media: ["text"],
      embargo_until: "2027-01-01", note: "",
    }],
  };
  T.render();
  const segHtml = elements.get("segments-body").innerHTML;
  for (const id of ["seg-10-sp", "seg-10-start", "seg-10-end"]) {
    assert.ok(segHtml.includes(`id="${id}"`), `发言区间缺少 ${id}`);
  }
  const licHtml = elements.get("licenses-body").innerHTML;
  for (const id of ["lic-20-start", "lic-20-end",
    "lic-20-allow-audio", "lic-20-allow-text",
    "lic-20-deny-audio", "lic-20-deny-text",
    "lic-20-emb", "lic-20-note"]) {
    assert.ok(licHtml.includes(`id="${id}"`), `授权行缺少 ${id}`);
  }
  assert.ok(elements.get("speakers-body").innerHTML.includes('id="sp-name-1"'));

  // 3. 保存发言区间 -> 正确的 PUT 请求体
  el("seg-10-sp").value = "2";
  el("seg-10-start").value = "00:01:00";
  el("seg-10-end").value = "00:02:00";
  await T.saveSegment(10);
  const segPut = calls.filter((c) => c.opts && c.opts.method === "PUT").pop();
  assert.deepStrictEqual(JSON.parse(segPut.opts.body),
    { speaker_id: 2, start_seconds: 60, end_seconds: 120 });

  // 4. 保存授权 -> 正确的 PUT 请求体
  el("lic-20-start").value = "00:00:00";
  el("lic-20-end").value = "01:00:00";
  el("lic-20-allow-audio").checked = true;
  el("lic-20-allow-text").checked = false;
  el("lic-20-deny-audio").checked = false;
  el("lic-20-deny-text").checked = true;
  el("lic-20-emb").value = "2027-01-01";
  el("lic-20-note").value = "备注";
  await T.saveLicense(20);
  const licPut = calls.filter((c) => c.opts && c.opts.method === "PUT").pop();
  assert.deepStrictEqual(JSON.parse(licPut.opts.body), {
    start_seconds: 0, end_seconds: 3600,
    allow_media: ["audio"], deny_media: ["text"],
    embargo_until: "2027-01-01", note: "备注",
  });

  // 5. 校验结果渲染
  T.renderResults({
    publication_date: "2026-09-23", medium: "audio",
    summary: { duration_seconds: 3600, publishable_seconds: 3000, blocked_seconds: 600 },
    intervals: [{
      start_seconds: 0, end_seconds: 600,
      start_timecode: "00:00:00", end_timecode: "00:10:00",
      status: "blocked", status_label: "不可发布",
      speakers: ["张三"], reasons: ["张三：缺少覆盖该区间的授权记录（缺项）"],
    }],
  });
  assert.ok(elements.get("results-body").innerHTML.includes("00:00:00 – 00:10:00"));
  assert.ok(elements.get("timeline").innerHTML.includes("blocked"));
  assert.ok(elements.get("results-summary").innerHTML.includes("2026-09-23"));

  // 6. 补件批次：渲染、试算请求体与试算结果展示
  T.state.remBatch = [{
    speaker_id: 1, start: "", end: "",
    allow: ["audio"], deny: [], embargo: "", note: "",
  }];
  T.renderRemediate();
  const remHtml = elements.get("remediate-body").innerHTML;
  for (const id of ["rem-0-sp", "rem-0-start", "rem-0-end",
    "rem-0-allow-audio", "rem-0-allow-text",
    "rem-0-deny-audio", "rem-0-deny-text", "rem-0-emb", "rem-0-note"]) {
    assert.ok(remHtml.includes(`id="${id}"`), `补件行缺少 ${id}`);
  }
  // 模拟用户编辑（桩 DOM 不解析 innerHTML，直接给元素赋值）
  el("rem-0-sp").value = "2";
  el("rem-0-start").value = "00:05:00";
  el("rem-0-end").value = "00:10:00";
  el("rem-0-allow-audio").checked = true;
  el("rem-0-note").value = "补签授权";
  el("pub-date").value = "2026-10-04";
  el("medium").value = "audio";
  sandbox.__remResponse = {
    interview_id: 1, medium: "audio", publication_date: "2026-10-04",
    commit_requested: false, committed: false, supplement_count: 1,
    before: { intervals: [], summary: { duration_seconds: 3600, blocked_seconds: 600, publishable_seconds: 3000 } },
    after: {
      intervals: [{
        start_seconds: 0, end_seconds: 3600,
        start_timecode: "00:00:00", end_timecode: "01:00:00",
        status: "ok", status_label: "可发布", publishable: true,
        speakers: ["李四"], reasons: [],
      }],
      summary: { duration_seconds: 3600, blocked_seconds: 0, publishable_seconds: 3600 },
    },
    blocked: [],
  };
  await T.submitRemediate(false);
  const remPost = calls.filter((c) => c.path.endsWith("/remediate")).pop();
  assert.ok(remPost, "应发出补件试算请求");
  assert.deepStrictEqual(JSON.parse(remPost.opts.body), {
    publication_date: "2026-10-04", medium: "audio", commit: false,
    supplements: [{
      speaker_id: 2, start_seconds: 300, end_seconds: 600,
      allow_media: ["audio"], deny_media: [],
      embargo_until: null, note: "补签授权",
    }],
  });
  const remResultHtml = elements.get("remediate-result").innerHTML;
  assert.ok(remResultHtml.includes("试算通过"), "试算结果应提示通过");
  assert.ok(remResultHtml.includes("00:00:00 – 01:00:00"), "试算结果应含变更后区间");

  // 7. 发行凭据：列表渲染、封存请求体、409 阻断展示与查阅复核视图
  T.state.credentials = [{
    id: 5, interview_id: 1, interview_title: "示例",
    medium: "audio", publication_date: "2026-10-04",
    created_at: "2026-10-04 08:00:00",
    status: "review", status_label: "待复核", changes: ["授权"],
  }];
  T.renderCredentials();
  const credHtml = elements.get("credentials-body").innerHTML;
  assert.ok(credHtml.includes("badge review"), "凭据列表应含待复核徽章");
  assert.ok(credHtml.includes("变化：授权"), "凭据列表应标注变化类别");

  // 封存成功：请求体正确，提示已封存
  el("pub-date").value = "2026-10-04";
  el("medium").value = "audio";
  sandbox.__credCreate = () => ({
    ok: true, status: 201,
    json: async () => ({
      id: 6, interview_id: 1, interview_title: "示例",
      medium: "audio", publication_date: "2026-10-04",
      created_at: "2026-10-04 09:00:00", created: true,
      result: { intervals: [], summary: { duration_seconds: 3600, blocked_seconds: 0, publishable_seconds: 3600 } },
    }),
  });
  await T.createCredential();
  const credPost = calls.filter(
    (c) => c.path.endsWith("/credentials") && c.opts && c.opts.method === "POST").pop();
  assert.ok(credPost, "应发出封存凭据请求");
  assert.deepStrictEqual(JSON.parse(credPost.opts.body),
    { publication_date: "2026-10-04", medium: "audio" });
  assert.ok(elements.get("credential-detail").innerHTML.includes("凭据 #6 已封存"));

  // 封存失败（409）：展示阻断区间，不落记录
  sandbox.__credCreate = () => ({
    ok: false, status: 409,
    json: async () => ({
      interview_id: 1, medium: "audio", publication_date: "2026-10-04",
      created: false, error: "仍有 1 个不可发布区间，未保存凭据",
      intervals: [],
      summary: { duration_seconds: 3600, blocked_seconds: 600, publishable_seconds: 3000 },
      blocked: [{
        start_seconds: 0, end_seconds: 600,
        start_timecode: "00:00:00", end_timecode: "00:10:00",
        status: "blocked", status_label: "不可发布",
        speakers: ["张三"], reasons: ["张三：缺少覆盖该区间的授权记录（缺项）"],
      }],
    }),
  });
  await assert.rejects(T.createCredential(), /未保存凭据/);
  const failHtml = elements.get("credential-detail").innerHTML;
  assert.ok(failHtml.includes("00:00:00 – 00:10:00"), "409 应展示阻断区间");

  // 查阅：待复核 + 发行时结论 + 当前阻断区间
  sandbox.__credDetail = {
    id: 5, interview_id: 1, interview_title: "示例",
    medium: "audio", publication_date: "2026-10-04",
    created_at: "2026-10-04 08:00:00",
    status: "review", status_label: "待复核", changes: ["授权"],
    sealed: {
      inputs: { duration_seconds: 3600, speakers: [], segments: [], licenses: [] },
      result: {
        intervals: [{
          start_seconds: 0, end_seconds: 3600,
          start_timecode: "00:00:00", end_timecode: "01:00:00",
          status: "ok", status_label: "可发布", publishable: true,
          speakers: ["张三"], reasons: [],
        }],
        summary: { duration_seconds: 3600, blocked_seconds: 0, publishable_seconds: 3600 },
      },
    },
    current: {
      inputs: {},
      intervals: [],
      summary: { duration_seconds: 3600, blocked_seconds: 600, publishable_seconds: 3000 },
      blocked: [{
        start_seconds: 600, end_seconds: 1200,
        start_timecode: "00:10:00", end_timecode: "00:20:00",
        status: "blocked", status_label: "不可发布",
        speakers: ["张三"], reasons: ["张三：许可#1 明确拒绝音频媒介（拒绝优先）"],
      }],
    },
  };
  await T.viewCredential(5);
  const detailHtml = elements.get("credential-detail").innerHTML;
  assert.ok(detailHtml.includes("待复核"), "查阅应显示待复核");
  assert.ok(detailHtml.includes("发行时结论"), "查阅应保留发行时结论");
  assert.ok(detailHtml.includes("00:10:00 – 00:20:00"), "查阅应列出当前阻断区间");

  // 查阅：来源缺失（原访谈已删除，历史凭据仍可查看）
  sandbox.__credDetail = {
    ...sandbox.__credDetail,
    status: "missing", status_label: "来源缺失", changes: [], current: null,
  };
  await T.viewCredential(5);
  assert.ok(elements.get("credential-detail").innerHTML.includes("来源缺失"),
    "原访谈删除后应显示来源缺失");

  // 8. 渠道投放台账
  // 8.1 凭据下拉选项来自凭据列表
  T.state.credentials = [
    { id: 5, interview_title: "示例", medium: "audio", publication_date: "2026-10-04",
      status: "valid", status_label: "与发行时一致", changes: [] },
    { id: 7, interview_title: "另一访谈", medium: "text", publication_date: "2026-11-01",
      status: "review", status_label: "待复核", changes: ["时长"] },
  ];
  T.renderCredentialOptions();
  const optHtml = elements.get("dist-credential").innerHTML;
  assert.ok(optHtml.includes('value="5"'), "凭据下拉应含凭据 #5");
  assert.ok(optHtml.includes("另一访谈"), "凭据下拉应渲染标题");
  assert.ok(optHtml.includes("待复核"), "凭据下拉应显示凭据状态");

  // 8.2 列表渲染：投放中 / 已撤下 + 风险徽章（需处理、来源缺失）
  T.state.distributions = [
    {
      id: 11, status: "active", status_label: "投放中",
      channel: "音频平台 A", url: "https://a.example.com/1",
      registered_at: "2026-10-04 10:00:00", withdrawn_at: null, withdraw_reason: "",
      credential: { id: 5, interview_title: "示例", medium: "audio",
        publication_date: "2026-10-04", created_at: "2026-10-04 09:00:00",
        status: "review", status_label: "待复核", changes: ["授权"],
        risk: "action", risk_label: "需处理" },
    },
    {
      id: 12, status: "withdrawn", status_label: "已撤下",
      channel: "平台 B", url: "https://b.example.com/2",
      registered_at: "2026-10-04 11:00:00",
      withdrawn_at: "2026-10-05 12:00:00", withdraw_reason: "授权方要求撤下",
      credential: { id: 7, interview_title: "另一访谈", medium: "text",
        publication_date: "2026-11-01", created_at: "2026-11-01 08:00:00",
        status: "missing", status_label: "来源缺失", changes: [],
        risk: "missing", risk_label: "来源缺失" },
    },
  ];
  T.renderDistributions();
  const listHtml = elements.get("distributions-body").innerHTML;
  assert.ok(listHtml.includes("badge dist-active"), "列表应含投放中徽章");
  assert.ok(listHtml.includes("badge dist-withdrawn"), "列表应含已撤下徽章");
  assert.ok(listHtml.includes("badge action"), "列表应含需处理风险徽章");
  assert.ok(listHtml.includes("来源缺失"), "列表应显示来源缺失");
  assert.ok(listHtml.includes("https://a.example.com/1"), "列表应含投放链接");
  assert.ok(listHtml.includes("授权方要求撤下"), "列表应显示撤下原因");
  assert.ok(listHtml.includes("2026-10-05 12:00:00"), "列表应显示撤下时间");
  // 已撤下记录不显示撤下按钮，投放中显示
  assert.ok(listHtml.includes("withdrawDistribution(11)"), "投放中应可撤下");
  assert.ok(!listHtml.includes("withdrawDistribution(12)"), "已撤下不应再可撤下");

  // 8.3 登记成功：请求体正确，随后查阅详情
  el("dist-credential").value = "5";
  el("dist-channel").value = "音频平台 A";
  el("dist-url").value = "https://a.example.com/1";
  sandbox.__distCreate = () => ({
    ok: true, status: 201,
    json: async () => distDetailActive(),
  });
  sandbox.__distDetail = distDetailActive();
  sandbox.__distList = [distDetailActive()];
  await T.createDistribution();
  const distPost = calls.filter(
    (c) => c.path.endsWith("/distributions") && c.opts && c.opts.method === "POST").pop();
  assert.ok(distPost, "应发出登记投放请求");
  assert.strictEqual(distPost.path, "/api/credentials/5/distributions");
  assert.deepStrictEqual(JSON.parse(distPost.opts.body),
    { channel: "音频平台 A", url: "https://a.example.com/1" });
  assert.ok(elements.get("distribution-detail").innerHTML.includes("投放 #11"),
    "登记成功后应展示详情");

  // 8.4 详情：需处理（资料变化但仍可发布）+ 封存结论 + 撤下输入
  let dhtml = elements.get("distribution-detail").innerHTML;
  assert.ok(dhtml.includes("需处理"), "详情应提示需处理");
  assert.ok(dhtml.includes("封存发行结论"), "详情应保留封存结论");
  assert.ok(dhtml.includes("dist-withdraw-reason"), "投放中应提供撤下原因输入");
  assert.ok(dhtml.includes('id="dist-withdraw-reason"'), "撤下原因输入应可填写");

  // 8.5 登记被拒（409，资料变化）：说明原因且不留记录
  sandbox.__distCreate = () => ({
    ok: false, status: 409,
    json: async () => ({
      created: false, reason: "source_changed",
      error: "资料与封存时不一致（变化：授权），未登记投放记录",
      changes: ["授权"],
      credential: {
        id: 5, interview_title: "示例", medium: "audio",
        publication_date: "2026-10-04", created_at: "2026-10-04 09:00:00",
        status: "review", status_label: "待复核", changes: ["授权"],
        risk: "action", risk_label: "需处理",
        sealed: { inputs: {}, result: { intervals: [], summary: {} } },
        current: { blocked: [] },
      },
    }),
  });
  await assert.rejects(T.createDistribution(), /未登记投放记录/);
  const distFailHtml = elements.get("distribution-detail").innerHTML;
  assert.ok(distFailHtml.includes("未登记投放记录"), "409 应说明原因");
  assert.ok(distFailHtml.includes("需处理"), "409 应显示凭据当前风险");

  // 8.6 撤下：请求体含原因，成功后展示已撤下与撤下时间，凭据风险仍可查
  el("dist-withdraw-reason").value = "授权方要求撤下";
  sandbox.__distWithdraw = () => ({
    ok: true, status: 200,
    json: async () => distDetailWithdrawn(),
  });
  sandbox.__distDetail = distDetailWithdrawn();
  sandbox.__distList = [distDetailWithdrawn()];
  await T.withdrawDistribution(11);
  const wdPost = calls.filter((c) => c.path.endsWith("/withdraw")).pop();
  assert.ok(wdPost, "应发出撤下请求");
  assert.strictEqual(wdPost.path, "/api/distributions/11/withdraw");
  assert.deepStrictEqual(JSON.parse(wdPost.opts.body),
    { reason: "授权方要求撤下" });
  const wdHtml = elements.get("distribution-detail").innerHTML;
  assert.ok(wdHtml.includes("已撤下"), "撤下后应显示已撤下状态");
  assert.ok(wdHtml.includes("2026-10-05 12:00:00"), "撤下后应保留撤下时间");
  assert.ok(wdHtml.includes("授权方要求撤下"), "撤下后应显示撤下原因");
  assert.ok(wdHtml.includes("需处理"), "撤下后风险提示仍可查");
  assert.ok(!wdHtml.includes("dist-withdraw-reason"), "已撤下不应再显示撤下输入");

  // 8.7 来源缺失详情：历史链接、时间与封存结论仍保留
  sandbox.__distDetail = {
    ...distDetailWithdrawn(),
    credential: {
      ...distDetailWithdrawn().credential,
      status: "missing", status_label: "来源缺失", changes: [],
      risk: "missing", risk_label: "来源缺失", current: null,
    },
  };
  await T.viewDistribution(11);
  const missHtml = elements.get("distribution-detail").innerHTML;
  assert.ok(missHtml.includes("来源缺失"), "详情应显示来源缺失");
  assert.ok(missHtml.includes("https://a.example.com/1"), "来源缺失时历史链接仍保留");
  assert.ok(missHtml.includes("2026-10-04 10:00:00"), "来源缺失时登记时间仍保留");
  assert.ok(missHtml.includes("封存发行结论"), "来源缺失时封存结论仍保留");

  function distDetailActive() {
    return {
      id: 11, credential_id: 5, channel: "音频平台 A",
      url: "https://a.example.com/1",
      status: "active", status_label: "投放中",
      registered_at: "2026-10-04 10:00:00", withdrawn_at: null, withdraw_reason: "",
      credential: {
        id: 5, interview_id: 1, interview_title: "示例", medium: "audio",
        publication_date: "2026-10-04", created_at: "2026-10-04 09:00:00",
        status: "review", status_label: "待复核", changes: ["授权"],
        risk: "action", risk_label: "需处理",
        sealed: {
          inputs: { duration_seconds: 3600, speakers: [], segments: [], licenses: [] },
          result: {
            intervals: [{
              start_seconds: 0, end_seconds: 3600,
              start_timecode: "00:00:00", end_timecode: "01:00:00",
              status: "ok", status_label: "可发布", publishable: true,
              speakers: ["甲"], reasons: [],
            }],
            summary: { duration_seconds: 3600, blocked_seconds: 0, publishable_seconds: 3600 },
          },
        },
        current: {
          inputs: {}, intervals: [],
          summary: { duration_seconds: 3600, blocked_seconds: 0, publishable_seconds: 3600 },
          blocked: [],
        },
      },
    };
  }
  function distDetailWithdrawn() {
    return {
      ...distDetailActive(),
      status: "withdrawn", status_label: "已撤下",
      withdrawn_at: "2026-10-05 12:00:00", withdraw_reason: "授权方要求撤下",
    };
  }

  console.log("test_frontend.js: 全部通过");
}

main().catch((e) => { console.error(e); process.exit(1); });
