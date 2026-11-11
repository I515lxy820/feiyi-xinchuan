const state = {
  image: null,
  previewUrl: null,
  runId: null,
  question: null,
  autoRunning: false,
  stopRequested: false,
};

const typeNames = {
  single: "单选题", multiple: "多选题", boolean: "判断题",
  fill: "填空题", essay: "简答题", unknown: "题型待确认",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(payload?.detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

function setBusy(button, busy, busyText = "处理中…") {
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

function switchView(name) {
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  if (name === "mistakes") loadMistakes();
  if (name === "stats") loadStats();
}

function renderOptions(container, options) {
  const entries = Object.entries(options || {});
  if (!entries.length) return;
  const list = node("div", "option-list");
  entries.forEach(([label, value]) => {
    const item = node("div", "option-item");
    item.append(node("span", "option-label", label), node("span", "", value));
    list.append(item);
  });
  container.append(list);
}

function addFeedback(container, attemptId) {
  if (!attemptId) return;
  const area = node("div", "feedback-area");
  const row = node("div", "feedback-row");
  row.append(node("span", "", "这次回答是否正确？"));
  const correct = node("button", "text-button", "正确");
  const wrong = node("button", "text-button", "有误");
  const correction = node("div", "correction-row");
  correction.hidden = true;
  const input = node("input");
  input.type = "text";
  input.placeholder = "填写正确答案或修正说明";
  const submit = node("button", "secondary-button compact", "提交修正");
  correction.append(input, submit);

  async function send(isCorrect) {
    correct.disabled = wrong.disabled = submit.disabled = true;
    try {
      await api(`/api/attempts/${attemptId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ correct: isCorrect, corrected_answer: isCorrect ? "" : input.value, note: "" }),
      });
      toast("反馈已保存，已纳入后续复盘统计");
      if (isCorrect) correction.hidden = true;
    } catch (error) {
      toast(error.message, true);
    } finally {
      correct.disabled = wrong.disabled = submit.disabled = false;
    }
  }
  correct.addEventListener("click", () => send(true));
  wrong.addEventListener("click", () => { correction.hidden = !correction.hidden; input.focus(); });
  submit.addEventListener("click", () => send(false));
  row.append(correct, wrong);
  area.append(row, correction);
  container.append(area);
}

function renderResult(container, data, practice = false) {
  container.replaceChildren();
  container.classList.remove("empty-state", "compact-empty");

  if (practice && data.is_correct !== null) {
    container.append(node("div", `grade-banner ${data.is_correct ? "correct" : "wrong"}`, data.is_correct ? "回答正确" : "回答错误，已加入错题复盘"));
  } else if (practice) {
    container.append(node("div", "grade-banner", "该题型需要人工评分，未计入客观题准确率"));
  }

  const answer = node("div", "answer-block");
  answer.append(node("div", "answer-label", practice ? "MODEL ANSWER" : "RECOMMENDED ANSWER"));
  answer.append(node("div", "answer-value", data.answer || "未能给出答案"));
  const meta = node("div", "answer-meta");
  meta.append(node("span", "", typeNames[data.question_type] || "题型待确认"));
  meta.append(node("span", "", `置信度 ${Math.round(data.confidence * 100)}%`));
  meta.append(node("span", "", `总耗时 ${data.timings.total ?? 0}s`));
  if (data.needs_review) meta.append(node("span", "review-flag", "需要人工复核"));
  answer.append(meta);
  container.append(answer);

  if (practice && data.correct_answer !== null) {
    const standard = node("section", "result-section");
    standard.append(node("h4", "", "标准答案"), node("p", "", data.correct_answer));
    container.append(standard);
  }

  if (data.warnings?.length) {
    const warnings = node("div", "warning-list result-section");
    data.warnings.forEach((warning) => warnings.append(node("div", "", warning)));
    container.append(warnings);
  }

  const explanation = node("section", "result-section");
  explanation.append(node("h4", "", "解题依据"), node("p", "", data.explanation || "暂无解析"));
  container.append(explanation);

  if (data.sources?.length) {
    const sourceSection = node("section", "result-section");
    sourceSection.append(node("h4", "", `联网来源（${data.sources.length}）`));
    const sources = node("div", "source-list");
    data.sources.forEach((source) => {
      const link = node("a", "source-item");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const body = node("div");
      body.append(node("strong", "", source.title), node("small", "", source.snippet || source.url));
      link.append(node("span", "source-rank", String(source.rank).padStart(2, "0")), body);
      sources.append(link);
    });
    sourceSection.append(sources);
    container.append(sourceSection);
  }

  const timingSection = node("section", "result-section");
  const timingText = Object.entries(data.timings || {}).map(([key, value]) => `${key} ${value}s`).join(" · ");
  timingSection.append(node("h4", "", "处理耗时"), node("p", "", timingText));
  container.append(timingSection);
  addFeedback(container, data.attempt_id);
}

function setImage(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    toast("请选择 JPEG、PNG 或 WebP 图片", true);
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    toast("图片不能超过 5 MB", true);
    return;
  }
  state.image = file;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(file);
  $("#preview-image").src = state.previewUrl;
  $("#preview-name").textContent = file.name || "粘贴的截图";
  $("#image-preview").hidden = false;
}

function removeImage() {
  state.image = null;
  $("#image-input").value = "";
  $("#image-preview").hidden = true;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
}

async function solveQuick() {
  const text = $("#question-input").value.trim();
  if (!text && !state.image) {
    toast("请先粘贴题目或上传截图", true);
    return;
  }
  const button = $("#solve-button");
  setBusy(button, true, "正在解题…");
  $("#solve-progress").hidden = false;
  $("#progress-text").textContent = "正在识别题目并生成初答";
  const progressTimer = setTimeout(() => {
    $("#progress-text").textContent = "正在检索来源并复核答案";
  }, 1400);
  const form = new FormData();
  form.append("text", text);
  form.append("search_mode", $("#search-enabled").checked ? "auto" : "off");
  if (state.image) form.append("image", state.image);
  try {
    const result = await api("/api/solve", { method: "POST", body: form });
    renderResult($("#quick-result"), result);
  } catch (error) {
    toast(error.message, true);
  } finally {
    clearTimeout(progressTimer);
    $("#solve-progress").hidden = true;
    setBusy(button, false);
  }
}

async function startRun() {
  const button = $("#start-run");
  setBusy(button, true, "创建中…");
  try {
    const payload = await api("/api/practice/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: $("#practice-topic").value.trim(), wrong_only: $("#wrong-only").checked }),
    });
    state.runId = payload.run_id;
    $("#run-status").textContent = `训练轮次 #${state.runId} 已创建`;
    $("#auto-run").disabled = false;
    $("#next-practice").disabled = false;
    await nextPractice();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function renderPracticeQuestion(question) {
  state.question = question;
  const container = $("#practice-question");
  container.replaceChildren();
  container.classList.remove("empty-state", "compact-empty");
  if (!question) {
    container.classList.add("empty-state", "compact-empty");
    container.append(node("p", "", "本轮题目已全部完成，或当前筛选条件下没有题目。"));
    $("#practice-type").textContent = "本轮完成";
    $("#solve-practice").disabled = true;
    $("#run-status").textContent = `训练轮次 #${state.runId} 已完成`;
    state.autoRunning = false;
    return;
  }
  $("#practice-type").textContent = `${typeNames[question.question_type]} · ${question.topic}`;
  container.append(node("h3", "", question.question_text));
  if (question.has_image) {
    const image = node("img");
    image.src = `/api/practice/${question.id}/image`;
    image.alt = "当前练习题图片";
    container.append(image);
  }
  renderOptions(container, question.options);
  $("#solve-practice").disabled = false;
  const result = $("#practice-result");
  result.replaceChildren(node("p", "", "完成本题后显示模型答案、标准答案和判分结果。"));
  result.classList.add("empty-state", "compact-empty");
}

async function nextPractice() {
  if (!state.runId) return;
  $("#next-practice").disabled = true;
  try {
    const params = new URLSearchParams({
      run_id: state.runId,
      topic: $("#practice-topic").value.trim(),
      wrong_only: $("#wrong-only").checked,
    });
    const question = await api(`/api/practice/next?${params}`);
    renderPracticeQuestion(question);
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("#next-practice").disabled = !state.runId || state.autoRunning;
  }
}

async function solvePractice() {
  if (!state.question || !state.runId) return null;
  const button = $("#solve-practice");
  setBusy(button, true, "作答中…");
  try {
    const params = new URLSearchParams({
      run_id: state.runId,
      search_mode: $("#search-enabled").checked ? "auto" : "off",
    });
    const result = await api(`/api/practice/${state.question.id}/solve?${params}`, { method: "POST" });
    renderResult($("#practice-result"), result, true);
    return result;
  } catch (error) {
    toast(error.message, true);
    return null;
  } finally {
    setBusy(button, false);
  }
}

async function autoPractice() {
  if (state.autoRunning || !state.runId) return;
  state.autoRunning = true;
  state.stopRequested = false;
  $("#auto-run").disabled = true;
  $("#stop-auto").hidden = false;
  $("#next-practice").disabled = true;
  while (!state.stopRequested) {
    if (!state.question) {
      await nextPractice();
      if (!state.question) break;
    }
    const result = await solvePractice();
    if (!result) break;
    state.question = null;
    await nextPractice();
  }
  state.autoRunning = false;
  $("#auto-run").disabled = !state.runId;
  $("#stop-auto").hidden = true;
  $("#next-practice").disabled = !state.runId;
  $("#solve-practice").disabled = !state.question;
  loadMistakes();
  loadStats();
}

async function loadMistakes() {
  const container = $("#mistakes-list");
  try {
    const mistakes = await api("/api/mistakes");
    container.replaceChildren();
    if (!mistakes.length) {
      const empty = node("div", "panel empty-state compact-empty");
      empty.append(node("h3", "", "暂无错题"), node("p", "", "完成模拟训练后，需要复盘的题目会出现在这里。"));
      container.append(empty);
      return;
    }
    mistakes.forEach((item) => {
      const card = node("article", "mistake-card");
      const header = node("div", "mistake-card-header");
      header.append(node("span", "", `${item.topic} · ${typeNames[item.question_type] || item.question_type}`), node("span", "", item.error_category || "待归类"));
      card.append(header, node("h3", "", item.question_text));
      renderOptions(card, item.options);
      const answers = node("div", "mistake-answers");
      answers.append(node("div", "", `模型答案：${item.model_answer || "未作答"}`), node("div", "", `标准答案：${item.corrected_answer || item.correct_answer}`));
      card.append(answers);
      const details = node("details");
      details.append(node("summary", "", "查看参考解析"), node("p", "", item.correct_explanation || item.model_explanation || "暂无解析"));
      card.append(details);
      container.append(card);
    });
  } catch (error) {
    toast(error.message, true);
  }
}

function formatPercent(value) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    const cards = $("#stats-cards");
    cards.replaceChildren();
    const values = [
      ["累计练习作答", stats.overall.attempts],
      ["已判分题目", stats.overall.graded],
      ["练习准确率", formatPercent(stats.overall.accuracy)],
      ["响应耗时", stats.latency_seconds.p50 === null ? "—" : `${stats.latency_seconds.p50}s / ${stats.latency_seconds.p95}s`],
    ];
    values.forEach(([label, value]) => {
      const card = node("div", "stat-card");
      card.append(node("span", "", label), node("strong", "", String(value)));
      cards.append(card);
    });
    const types = $("#type-stats");
    types.replaceChildren();
    const entries = Object.entries(stats.by_type);
    if (!entries.length) types.append(node("p", "", "暂无判分数据"));
    entries.forEach(([type, value]) => {
      const row = node("div", "stat-row");
      const label = node("div");
      label.append(node("span", "", typeNames[type] || type), node("small", "", `${value.correct} / ${value.graded} 题正确`));
      row.append(label, node("strong", "", formatPercent(value.accuracy)));
      types.append(row);
    });
    const runs = $("#run-stats");
    runs.replaceChildren();
    if (!stats.recent_runs.length) runs.append(node("p", "", "暂无训练轮次"));
    stats.recent_runs.forEach((run) => {
      const row = node("div", "stat-row");
      const label = node("div");
      label.append(node("span", "", `轮次 #${run.id}${run.wrong_only ? " · 错题重练" : ""}`), node("small", "", run.topic || "全部主题"));
      row.append(label, node("strong", "", `${run.correct} / ${run.total}`));
      runs.append(row);
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    const chip = $("#connection-status");
    chip.textContent = health.demo_mode ? "演示模式" : health.status === "ok" ? "真实服务已配置" : "配置待完善";
    chip.classList.add(health.demo_mode ? "demo" : "connected");
    $("#demo-banner").hidden = !health.demo_mode;
  } catch (error) {
    $("#connection-status").textContent = "服务未连接";
    toast(error.message, true);
  }
}

$$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$("#question-input").addEventListener("input", (event) => { $("#char-count").textContent = `${event.target.value.length} / 12000`; });
$("#question-input").addEventListener("paste", (event) => {
  const file = [...event.clipboardData.files].find((item) => item.type.startsWith("image/"));
  if (file) setImage(file);
});
$("#image-input").addEventListener("change", (event) => setImage(event.target.files[0]));
$("#remove-image").addEventListener("click", removeImage);
const dropzone = $("#dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => setImage(event.dataTransfer.files[0]));
$("#solve-button").addEventListener("click", solveQuick);
$("#start-run").addEventListener("click", startRun);
$("#next-practice").addEventListener("click", async () => { state.question = null; await nextPractice(); });
$("#solve-practice").addEventListener("click", solvePractice);
$("#auto-run").addEventListener("click", autoPractice);
$("#stop-auto").addEventListener("click", () => { state.stopRequested = true; });
$("#refresh-mistakes").addEventListener("click", loadMistakes);
$("#refresh-stats").addEventListener("click", loadStats);

checkHealth();
