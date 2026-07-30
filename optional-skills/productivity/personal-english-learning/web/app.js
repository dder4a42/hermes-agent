const sessionToken = document.querySelector('meta[name="learning-session"]').content;
const headers = {"Content-Type": "application/json", "X-Learning-Session": sessionToken};
const titles = {home: "今日学习", assessment: "基础评估", learn: "新词学习", review: "到期复习", notebook: "生词本", reading: "阅读私教", vocabulary: "词库查询", progress: "学习进度"};
let collections = [];
let assessmentItems = [];
let assessmentIndex = 0;
let reviewItems = [];
let reviewIndex = 0;
let reviewStartedAt = 0;
let learningItems = [];
let learningIndex = 0;
let learningStartedAt = 0;
let lastSearchItems = [];
let reviewPlanLoading = false;
let reviewSubmitting = false;
let learningSubmitting = false;
let todayPlanPromise = null;
let currentLesson = null;
let currentWritingSubmission = null;

async function api(path, options = {}) {
  const controller = new AbortController();
  const {timeoutMs = 15000, ...fetchOptions} = options;
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, {...fetchOptions, signal: controller.signal, headers: {...headers, ...(fetchOptions.headers || {})}});
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请稍后重试；已生成的内容会从缓存恢复。");
    throw new Error("连接失败，请检查 SSH 隧道和服务器状态。");
  } finally {
    window.clearTimeout(timeout);
  }
  const payload = await response.json().catch(() => ({detail: "服务器返回了无效响应"}));
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function notify(message, kind = "info") {
  const node = document.querySelector("#notice");
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { node.hidden = true; }, 4200);
}

function showView(name) {
  document.querySelectorAll(".view").forEach(el => el.classList.toggle("active", el.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
  document.querySelector("#page-title").textContent = titles[name];
  if (name === "progress") void loadProgress();
  if (name === "notebook") void loadNotebook().catch(error => notify(error.message, "error"));
}

function selectedCollection() { return document.querySelector("#collection-select").value || null; }
function formatNumber(value) { return new Intl.NumberFormat("zh-CN").format(value || 0); }

function pronunciationHtml(item) {
  const pronunciation = item?.pronunciations?.[0];
  if (!pronunciation) return '<span class="pronunciation-missing">暂无发音</span>';
  const mode = document.querySelector("#pronunciation-mode").value;
  const parts = [];
  if (mode !== "respelling" && pronunciation.ipa) parts.push(`<span>US /${escapeHtml(pronunciation.ipa)}/</span>`);
  if (mode !== "ipa" && pronunciation.respelling) parts.push(`<strong>${escapeHtml(pronunciation.respelling)}</strong>`);
  return parts.join('<span class="pronunciation-separator">·</span>');
}

function analysisSourceLabel(analysis) {
  if (analysis.source_level === "authoritative") return "权威来源";
  if (analysis.source_level === "deterministic") return "系统分析";
  return `AI 分析 ${Math.round(analysis.confidence * 100)}%`;
}

function lexicalAnalysisHtml(item, compact = false) {
  const lexical = item?.lexical_analysis;
  if (!lexical) return "";
  const family = (lexical.word_family || []).slice(0, compact ? 6 : 12);
  const visibleAnalyses = (lexical.analyses || []).filter(analysis => {
    if (analysis.status !== "available") return false;
    if (analysis.source_level !== "llm_inferred") return true;
    const threshold = analysis.analysis_type === "historical_etymology" ? 0.75 : 0.6;
    return analysis.confidence >= threshold;
  });
  const seenAnalysisTypes = new Set();
  const analyses = visibleAnalyses.filter(analysis => {
    if (seenAnalysisTypes.has(analysis.analysis_type)) return false;
    seenAnalysisTypes.add(analysis.analysis_type); return true;
  });
  const sections = [];
  if (family.length) {
    sections.push(`<section class="lexical-section"><h4>词族</h4><div class="family-chips">${family.map(entry => `<span>${escapeHtml(entry.form)}${entry.part_of_speech ? `<small>${escapeHtml(entry.part_of_speech)}</small>` : ""}</span>`).join("")}</div><small class="analysis-source">OEWN · 权威来源</small></section>`);
  }
  for (const analysis of analyses.slice(0, compact ? 1 : 3)) {
    const title = analysis.analysis_type === "modern_morphology" ? "构词分析" : "词源解读";
    const segments = Array.isArray(analysis.content?.segments) ? analysis.content.segments : [];
    const segmentHtml = segments.length ? `<div class="morpheme-chain">${segments.map(segment => `<span><strong>${escapeHtml(segment.form)}</strong><small>${escapeHtml(segment.meaning || segment.type || "")}</small></span>`).join('<b>+</b>')}</div>` : "";
    const explanation = analysis.explanation_zh || analysis.content?.summary_zh || analysis.content?.summary || "";
    const uncertain = analysis.source_level === "llm_inferred" && analysis.confidence < 0.8 ? " · 可能的分析" : "";
    sections.push(`<section class="lexical-section"><h4>${title}</h4>${segmentHtml}${explanation ? `<p>${escapeHtml(explanation)}</p>` : ""}<small class="analysis-source">${escapeHtml(analysisSourceLabel(analysis))}${uncertain} · ${escapeHtml(analysis.source)}</small></section>`);
  }
  if (!sections.length && !compact && Object.values(lexical.needs_inference || {}).some(Boolean)) {
    sections.push('<p class="analysis-missing">暂无可靠分析；可在需要时由 AI 辅助补全。</p>');
  }
  return sections.length ? `<div class="lexical-analysis">${sections.join("")}</div>` : "";
}

async function loadStats() {
  const data = await api("/api/stats");
  document.querySelector("#metric-due").textContent = formatNumber(data.due_reviews);
  document.querySelector("#metric-assessed").textContent = formatNumber(data.assessed_senses);
  document.querySelector("#metric-tracked").textContent = formatNumber(data.tracked_senses);
  document.querySelector("#metric-vocabulary").textContent = formatNumber(data.vocabulary_senses);
  collections = data.collections;
  const select = document.querySelector("#collection-select");
  const previous = select.value;
  select.replaceChildren(...collections.map(item => {
    const option = document.createElement("option"); option.value = item.id;
    option.textContent = item.kind === "general" ? "通用英语" : item.kind === "academic" ? "学术英语" : item.title;
    return option;
  }));
  if (collections.some(item => item.id === previous)) select.value = previous;
  document.querySelector("#collection-summary").replaceChildren(...collections.map(item => {
    const row = document.createElement("div"); row.innerHTML = `<span>${escapeHtml(item.title)}</span><strong>${formatNumber(item.sense_count)}</strong>`; return row;
  }));
}

function escapeHtml(value) {
  const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML;
}

async function startAssessment() {
  const data = await api("/api/assessment/sample", {method: "POST", body: JSON.stringify({per_band: 5, seed: Date.now(), collection_id: selectedCollection()})});
  assessmentItems = data.items; assessmentIndex = 0;
  document.querySelector("#assessment-empty").hidden = assessmentItems.length > 0;
  document.querySelector("#assessment-card").hidden = assessmentItems.length === 0;
  if (!assessmentItems.length) notify("该集合没有尚未评估的主词义。", "success");
  renderAssessment();
}

function renderAssessment() {
  const item = assessmentItems[assessmentIndex]; if (!item) return;
  document.querySelector("#assessment-lemma").textContent = item.lemma;
  document.querySelector("#assessment-pos").textContent = item.part_of_speech;
  document.querySelector("#assessment-pronunciation").innerHTML = pronunciationHtml(item);
  document.querySelector("#assessment-band").textContent = `频率 ${item.frequency_band}`;
  document.querySelector("#assessment-progress").textContent = `${assessmentIndex + 1} / ${assessmentItems.length}`;
}

async function answerAssessment(response) {
  const item = assessmentItems[assessmentIndex]; if (!item) return;
  await api("/api/assessment/record", {method: "POST", body: JSON.stringify({sense_id: item.sense_id, response, frequency_band: item.frequency_band, event_id: crypto.randomUUID()})});
  assessmentIndex += 1;
  if (assessmentIndex >= assessmentItems.length) {
    document.querySelector("#assessment-card").hidden = true;
    document.querySelector("#assessment-empty").hidden = false;
    document.querySelector("#assessment-empty").textContent = `本轮 ${assessmentItems.length} 个词已完成。`;
    notify("基础评估已记录。", "success"); await loadStats(); return;
  }
  renderAssessment();
}

async function startReview() {
  if (reviewPlanLoading) return;
  reviewPlanLoading = true;
  const button = document.querySelector("#review-start");
  const originalText = button.textContent;
  button.disabled = true; button.textContent = "正在载入…";
  try {
    const data = await ensureTodayPlan();
    reviewItems = data.reviews; reviewIndex = 0;
    document.querySelector("#review-empty").hidden = reviewItems.length > 0;
    document.querySelector("#review-card").hidden = reviewItems.length === 0;
    if (!reviewItems.length) notify("当前没有到期复习。", "success");
    else if (data.reused_plan) notify("已恢复今天尚未完成的到期卡。", "success");
    renderReview(); await loadStats();
  } finally {
    reviewPlanLoading = false;
    button.disabled = false; button.textContent = originalText;
  }
}

async function ensureTodayPlan() {
  if (!todayPlanPromise) {
    todayPlanPromise = api("/api/plans/today", {
      method: "POST",
      body: JSON.stringify({review_limit: 30, new_limit: 8, collection_id: selectedCollection()}),
    }).catch(error => { todayPlanPromise = null; throw error; });
  }
  return todayPlanPromise;
}

async function startLearning() {
  if (reviewPlanLoading) return;
  reviewPlanLoading = true;
  const button = document.querySelector("#learning-start");
  const originalText = button.textContent;
  button.disabled = true; button.textContent = "正在载入…";
  try {
    const data = await ensureTodayPlan();
    learningItems = data.new_items; learningIndex = 0;
    document.querySelector("#learning-empty").hidden = learningItems.length > 0;
    document.querySelector("#learning-card").hidden = learningItems.length === 0;
    if (!learningItems.length) {
      const reason = data.effective_new_limit === 0 ? "复习积压较多，今天暂停增加新词。" : "今天的新词已经完成。";
      notify(reason, "success");
    }
    renderLearning(); await loadStats();
  } finally {
    reviewPlanLoading = false;
    button.disabled = false; button.textContent = originalText;
  }
}

function renderLearning() {
  const item = learningItems[learningIndex]; if (!item) return;
  document.querySelector("#learning-progress").textContent = `${learningIndex + 1} / ${learningItems.length}`;
  document.querySelector("#learning-lemma").textContent = item.lemma;
  document.querySelector("#learning-pos").textContent = item.part_of_speech;
  document.querySelector("#learning-pronunciation").innerHTML = pronunciationHtml(item);
  document.querySelector("#learning-definition-en").textContent = item.definition_en;
  document.querySelector("#learning-definition-zh").textContent = item.definition_zh || "暂无中文释义";
  document.querySelector("#learning-lexical-analysis").innerHTML = lexicalAnalysisHtml(item);
  document.querySelector("#learning-assess").hidden = false;
  document.querySelector("#learning-ratings").hidden = true;
  learningStartedAt = performance.now();
}

async function rateLearning(rating) {
  if (learningSubmitting) return;
  const item = learningItems[learningIndex]; if (!item) return;
  learningSubmitting = true;
  const buttons = [...document.querySelectorAll("[data-learning-rating]")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    await api(`/api/reviews/${encodeURIComponent(item.card_id)}`, {
      method: "POST",
      body: JSON.stringify({rating, idempotency_key: crypto.randomUUID(), response_time_ms: Math.round(performance.now() - learningStartedAt)}),
    });
    todayPlanPromise = null;
    learningIndex += 1;
    if (learningIndex >= learningItems.length) {
      document.querySelector("#learning-card").hidden = true;
      document.querySelector("#learning-empty").hidden = false;
      document.querySelector("#learning-empty").textContent = `今天 ${learningItems.length} 个新词已完成。`;
      notify("新词学习完成，之后会进入到期复习。", "success");
      await loadStats(); return;
    }
    renderLearning();
  } finally {
    learningSubmitting = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

function renderReview() {
  const item = reviewItems[reviewIndex]; if (!item) return;
  const recall = item.card_type === "recall";
  document.querySelector("#review-type").textContent = recall ? "主动回忆" : "词义识别";
  document.querySelector("#review-progress").textContent = `${reviewIndex + 1} / ${reviewItems.length}`;
  document.querySelector("#review-prompt-label").textContent = recall ? "根据释义回忆英文词" : "回忆这个词的常见含义";
  const front = document.querySelector("#review-front");
  front.textContent = recall ? item.definition_en : item.lemma;
  front.classList.toggle("word-front", !recall);
  front.classList.toggle("sentence-front", recall);
  document.querySelector("#review-pronunciation").innerHTML = recall ? "" : pronunciationHtml(item);
  document.querySelector("#review-answer-main").textContent = recall ? item.lemma : item.definition_en;
  document.querySelector("#review-answer-pronunciation").innerHTML = pronunciationHtml(item);
  document.querySelector("#review-answer-extra").textContent = `${item.part_of_speech} · ${item.definition_zh || "暂无中文释义"}`;
  document.querySelector("#review-lexical-analysis").innerHTML = lexicalAnalysisHtml(item, true);
  document.querySelector("#review-answer").hidden = true;
  document.querySelector("#review-ratings").hidden = true;
  document.querySelector("#reveal-answer").hidden = false;
  reviewStartedAt = performance.now();
}

async function rateReview(rating) {
  if (reviewSubmitting) return;
  const item = reviewItems[reviewIndex]; if (!item) return;
  reviewSubmitting = true;
  const ratingButtons = [...document.querySelectorAll("[data-rating]")];
  ratingButtons.forEach(button => { button.disabled = true; });
  try {
    await api(`/api/reviews/${encodeURIComponent(item.card_id)}`, {method: "POST", body: JSON.stringify({rating, idempotency_key: crypto.randomUUID(), response_time_ms: Math.round(performance.now() - reviewStartedAt)})});
    todayPlanPromise = null;
    reviewIndex += 1;
    if (reviewIndex >= reviewItems.length) {
      document.querySelector("#review-card").hidden = true;
      document.querySelector("#review-empty").hidden = false;
      document.querySelector("#review-empty").textContent = `今日 ${reviewItems.length} 张卡片已完成。`;
      notify("今日复习完成。", "success"); await loadStats(); return;
    }
    renderReview();
  } finally {
    reviewSubmitting = false;
    ratingButtons.forEach(button => { button.disabled = false; });
  }
}

async function searchVocabulary(event) {
  event.preventDefault(); const query = document.querySelector("#search-query").value.trim(); if (!query) return;
  const params = new URLSearchParams({q: query, limit: "30"}); if (selectedCollection()) params.set("collection_id", selectedCollection());
  const data = await api(`/api/vocabulary/search?${params}`);
  const container = document.querySelector("#search-results");
  lastSearchItems = data.items;
  if (!data.items.length) { container.innerHTML = '<div class="empty-state">没有匹配词义。</div>'; return; }
  container.replaceChildren(...data.items.map(item => {
    const card = document.createElement("article"); card.className = "result-card";
    card.innerHTML = `<div><h3>${escapeHtml(item.lemma)}</h3><span>${escapeHtml(item.part_of_speech)}</span></div><div class="result-pronunciation pronunciation">${pronunciationHtml(item)}</div><p>${escapeHtml(item.definition_en)}</p><small>识别 ${Math.round(item.recognition_score * 100)}% · 回忆 ${Math.round(item.recall_score * 100)}% · 来源 ${escapeHtml(item.source)}</small>${lexicalAnalysisHtml(item)}`;
    const add = document.createElement("button"); add.className = "secondary notebook-add"; add.textContent = "加入生词本";
    add.addEventListener("click", () => void addToNotebook(item, add).catch(error => notify(error.message, "error")));
    card.append(add);
    return card;
  }));
}

async function addToNotebook(item, button) {
  button.disabled = true;
  try {
    const result = await api(`/api/vocabulary/notebook/${encodeURIComponent(item.sense_id)}`, {method: "POST"});
    button.textContent = result.created || result.reactivated ? "已加入生词本" : "已在生词本中";
    todayPlanPromise = null;
    notify(`${item.lemma} 的当前词义已加入学习队列。`, "success");
    await loadStats();
  } finally {
    button.disabled = false;
  }
}

async function loadNotebook() {
  const data = await api("/api/vocabulary/notebook?limit=100");
  const container = document.querySelector("#notebook-content");
  if (!data.items.length) {
    container.innerHTML = '<div class="empty-state">生词本还是空的。完成基础评估、阅读选词或新词学习后，具体词义会出现在这里。</div>';
    return;
  }
  container.replaceChildren(...data.items.map(item => {
    const card = document.createElement("article"); card.className = "notebook-card";
    const next = item.next_review_at ? new Date(item.next_review_at).toLocaleString("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "未安排";
    const types = item.card_types.map(type => type === "recall" ? "主动回忆" : "词义识别").join(" · ");
    card.innerHTML = `<header><div><h3>${escapeHtml(item.lemma)}</h3><span>${escapeHtml(item.part_of_speech)} · ${escapeHtml(item.definition_zh || item.definition_en)}</span></div><b>${item.learning_status === "mastered" ? "较稳定" : "学习中"}</b></header><p>${escapeHtml(item.definition_en)}</p><div class="mastery-bars"><span>识别 <i style="--score:${Math.round(item.recognition_score * 100)}%"></i><em>${Math.round(item.recognition_score * 100)}%</em></span><span>回忆 <i style="--score:${Math.round(item.recall_score * 100)}%"></i><em>${Math.round(item.recall_score * 100)}%</em></span><span>使用 <i style="--score:${Math.round(item.production_score * 100)}%"></i><em>${Math.round(item.production_score * 100)}%</em></span></div><small>${escapeHtml(types)} · 下次复习 ${escapeHtml(next)}</small>`;
    return card;
  }));
}

async function prepareReading(event) {
  event.preventDefault();
  const button = document.querySelector("#reading-generate");
  const original = button.textContent;
  button.disabled = true; button.textContent = "私教正在备课…";
  try {
    const data = await api("/api/reading/today", {
      method: "POST",
      timeoutMs: 100000,
      body: JSON.stringify({
        level: document.querySelector("#reading-level").value,
        minutes: Number(document.querySelector("#reading-minutes").value),
        topic: document.querySelector("#reading-topic").value,
        collection_id: selectedCollection(),
      }),
    });
    renderReading(data);
    if (data.generation_status === "fallback") notify("模型暂时不可用，已载入经过筛选的原始教练材料。", "info");
    else if (data.cached) notify("已恢复今天生成的阅读课。", "success");
    else notify("今天的个性化阅读已准备好。", "success");
  } finally {
    button.disabled = false; button.textContent = original;
  }
}

function renderReading(lesson) {
  currentLesson = lesson;
  currentWritingSubmission = null;
  document.querySelector("#reading-empty").hidden = true;
  document.querySelector("#reading-lesson").hidden = false;
  document.querySelector("#reading-title").textContent = lesson.title;
  document.querySelector("#reading-why").textContent = lesson.why_this_passage;
  const modeLabels = {tutor_generated: "AI 私教改写", source_adapted: "来源改写", curated_seed: "精选材料"};
  document.querySelector("#reading-badges").replaceChildren(...[
    modeLabels[lesson.source_mode] || lesson.source_mode,
    lesson.level,
    `${document.querySelector("#reading-minutes").value} 分钟`,
  ].map(label => { const node = document.createElement("span"); node.textContent = label; return node; }));
  const source = document.querySelector("#reading-source");
  source.textContent = `${lesson.source.publisher} · 查看来源`;
  source.href = lesson.source.url;
  const targets = document.querySelector("#reading-targets");
  targets.replaceChildren(...(lesson.target_items || []).map(item => {
    const node = document.createElement("span");
    const meaning = item.definition_zh || item.definition_en;
    node.textContent = `${item.lemma} · ${meaning}`;
    return node;
  }));
  targets.hidden = !(lesson.target_items || []).length;
  const passage = document.querySelector("#reading-passage");
  passage.replaceChildren(...lesson.passage.split(/\n+/).filter(Boolean).map(text => {
    const paragraph = document.createElement("p"); paragraph.textContent = text; return paragraph;
  }));
  document.querySelector("#reading-questions").replaceChildren(...lesson.questions.map((question, index) => {
    const card = document.createElement("article");
    const heading = document.createElement("p"); heading.textContent = `${index + 1}. ${question.prompt}`;
    const button = document.createElement("button"); button.className = "secondary"; button.textContent = "显示参考答案";
    const answer = document.createElement("p"); answer.className = "question-answer"; answer.textContent = question.answer; answer.hidden = true;
    button.addEventListener("click", () => { answer.hidden = false; button.hidden = true; });
    card.append(heading, button, answer); return card;
  }));
  document.querySelector("#reading-writing-prompt").textContent = lesson.writing_prompt;
  const summary = document.querySelector("#reading-summary");
  const storageKey = `reading-summary:${lesson.lesson_id}`;
  summary.value = localStorage.getItem(storageKey) || "";
  summary.oninput = () => localStorage.setItem(storageKey, summary.value);
  document.querySelector("#writing-submit").textContent = "提交初稿评阅";
  document.querySelector("#writing-stage-note").textContent = "";
  document.querySelector("#writing-feedback").hidden = true;
}

async function submitWriting() {
  if (!currentLesson) return;
  const text = document.querySelector("#reading-summary").value.trim();
  if (!text) { notify("请先完成一段英文写作。", "error"); return; }
  const button = document.querySelector("#writing-submit");
  const original = button.textContent;
  button.disabled = true; button.textContent = "私教正在评阅…";
  try {
    const result = await api("/api/writing/review", {
      method: "POST",
      timeoutMs: 100000,
      body: JSON.stringify({
        lesson_id: currentLesson.lesson_id,
        text,
        idempotency_key: crypto.randomUUID(),
        parent_submission_id: currentWritingSubmission?.submission_id || null,
      }),
    });
    currentWritingSubmission = result;
    renderWritingFeedback(result);
    button.textContent = "提交修改稿复评";
    document.querySelector("#writing-stage-note").textContent = "请根据提示自行修改上方文本，再提交复评。";
    notify(result.stage === "revision" ? "修改稿反馈已生成。" : "初稿反馈已生成，请先自行修改。", "success");
  } finally {
    button.disabled = false;
    if (!currentWritingSubmission) button.textContent = original;
  }
}

function renderWritingFeedback(result) {
  const feedback = result.feedback;
  const node = document.querySelector("#writing-feedback");
  const categoryLabels = {grammar: "语法", collocation: "搭配", register: "语域", cohesion: "衔接", content: "内容", word_choice: "选词"};
  const issues = feedback.issues.length ? feedback.issues.map(issue => `<article><span>${escapeHtml(categoryLabels[issue.category] || issue.category)}</span><strong>${escapeHtml(issue.excerpt)}</strong><p>${escapeHtml(issue.explanation_zh)}</p><small>修改提示：${escapeHtml(issue.hint_zh)}</small></article>`).join("") : '<p class="feedback-success">没有发现需要优先修改的问题。</p>';
  const strengths = feedback.strengths.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const priorities = feedback.revision_priorities.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const traitLabels = {content: "内容", accuracy: "准确性", cohesion: "连贯性", register: "语域"};
  const traits = Object.entries(feedback.traits).map(([key, value]) => `<div><b>${escapeHtml(traitLabels[key] || key)}</b><p>${escapeHtml(value)}</p></div>`).join("");
  node.innerHTML = `<header><span>${result.stage === "revision" ? "修改稿反馈" : "初稿反馈"}</span><h4>${escapeHtml(feedback.summary_zh)}</h4></header><section><h5>做得好的地方</h5><ul>${strengths}</ul></section><section><h5>需要你修改</h5><div class="writing-issues">${issues}</div>${priorities ? `<ol>${priorities}</ol>` : ""}</section><section><h5>分项观察</h5><div class="trait-grid">${traits}</div></section>`;
  node.hidden = false;
}

async function loadProgress() {
  const data = await api("/api/reports/weekly?days=7");
  const cards = [
    ["评估", data.assessment.total, "次回答"], ["复习", data.reviews.attempts, "次作答"],
    ["阅读", data.reading.documents + data.reading.tutor_lessons, "篇材料"], ["写作", data.writing.submissions + data.production.attempts, "次练习"]
  ];
  document.querySelector("#progress-content").replaceChildren(...cards.map(([label, value, suffix]) => {
    const card = document.createElement("article"); card.innerHTML = `<span>${label}</span><strong>${formatNumber(value)}</strong><small>${suffix}</small>`; return card;
  }));
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => showView(button.dataset.view)));
document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => showView(button.dataset.go)));
document.querySelector("#assessment-start").addEventListener("click", () => void startAssessment().catch(error => notify(error.message, "error")));
document.querySelectorAll("[data-assessment]").forEach(button => button.addEventListener("click", () => void answerAssessment(button.dataset.assessment).catch(error => notify(error.message, "error"))));
document.querySelector("#learning-start").addEventListener("click", () => void startLearning().catch(error => notify(error.message, "error")));
document.querySelector("#learning-assess").addEventListener("click", event => { event.currentTarget.hidden = true; document.querySelector("#learning-ratings").hidden = false; });
document.querySelectorAll("[data-learning-rating]").forEach(button => button.addEventListener("click", () => void rateLearning(button.dataset.learningRating).catch(error => notify(error.message, "error"))));
document.querySelector("#review-start").addEventListener("click", () => void startReview().catch(error => notify(error.message, "error")));
document.querySelector("#reveal-answer").addEventListener("click", event => { event.currentTarget.hidden = true; document.querySelector("#review-answer").hidden = false; document.querySelector("#review-ratings").hidden = false; });
document.querySelectorAll("[data-rating]").forEach(button => button.addEventListener("click", () => void rateReview(button.dataset.rating).catch(error => notify(error.message, "error"))));
document.querySelector("#search-form").addEventListener("submit", event => void searchVocabulary(event).catch(error => notify(error.message, "error")));
document.querySelector("#reading-form").addEventListener("submit", event => void prepareReading(event).catch(error => notify(error.message, "error")));
document.querySelector("#writing-submit").addEventListener("click", () => void submitWriting().catch(error => notify(error.message, "error")));
document.querySelector("#notebook-refresh").addEventListener("click", () => void loadNotebook().catch(error => notify(error.message, "error")));
document.querySelector("#progress-refresh").addEventListener("click", () => void loadProgress().catch(error => notify(error.message, "error")));
document.querySelector("#collection-select").addEventListener("change", () => { todayPlanPromise = null; });
document.querySelector("#pronunciation-mode").addEventListener("change", () => {
  const assessmentItem = assessmentItems[assessmentIndex];
  if (assessmentItem) document.querySelector("#assessment-pronunciation").innerHTML = pronunciationHtml(assessmentItem);
  const reviewItem = reviewItems[reviewIndex];
  if (reviewItem) {
    document.querySelector("#review-pronunciation").innerHTML = reviewItem.card_type === "recall" ? "" : pronunciationHtml(reviewItem);
    document.querySelector("#review-answer-pronunciation").innerHTML = pronunciationHtml(reviewItem);
  }
  const learningItem = learningItems[learningIndex];
  if (learningItem) document.querySelector("#learning-pronunciation").innerHTML = pronunciationHtml(learningItem);
  if (lastSearchItems.length) document.querySelector("#search-form").requestSubmit();
});
document.addEventListener("keydown", event => {
  if (!document.querySelector("#view-assessment").classList.contains("active")) return;
  const map = {"1": "unknown", "2": "unsure", "3": "known"}; if (map[event.key]) void answerAssessment(map[event.key]).catch(error => notify(error.message, "error"));
});

void loadStats().catch(error => notify(error.message, "error"));
