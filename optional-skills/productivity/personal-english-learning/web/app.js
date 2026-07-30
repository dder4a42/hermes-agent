const sessionToken = document.querySelector('meta[name="learning-session"]').content;
const headers = {"Content-Type": "application/json", "X-Learning-Session": sessionToken};
const titles = {home: "今日学习", assessment: "基础评估", review: "词汇复习", vocabulary: "词库查询", progress: "学习进度"};
let collections = [];
let assessmentItems = [];
let assessmentIndex = 0;
let reviewItems = [];
let reviewIndex = 0;
let reviewStartedAt = 0;

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {...headers, ...(options.headers || {})}});
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
}

function selectedCollection() { return document.querySelector("#collection-select").value || null; }
function formatNumber(value) { return new Intl.NumberFormat("zh-CN").format(value || 0); }

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
  const data = await api("/api/plans/today", {method: "POST", body: JSON.stringify({review_limit: 30, new_limit: 8, collection_id: selectedCollection()})});
  reviewItems = [...data.reviews, ...data.new_items]; reviewIndex = 0;
  document.querySelector("#review-empty").hidden = reviewItems.length > 0;
  document.querySelector("#review-card").hidden = reviewItems.length === 0;
  if (!reviewItems.length) notify("今天暂时没有待复习或可加入的新词。", "success");
  renderReview(); await loadStats();
}

function renderReview() {
  const item = reviewItems[reviewIndex]; if (!item) return;
  const recall = item.card_type === "recall";
  document.querySelector("#review-type").textContent = recall ? "主动回忆" : "词义识别";
  document.querySelector("#review-progress").textContent = `${reviewIndex + 1} / ${reviewItems.length}`;
  document.querySelector("#review-prompt-label").textContent = recall ? "根据释义回忆英文词" : "回忆这个词的常见含义";
  document.querySelector("#review-front").textContent = recall ? item.definition_en : item.lemma;
  document.querySelector("#review-answer-main").textContent = recall ? item.lemma : item.definition_en;
  document.querySelector("#review-answer-extra").textContent = `${item.part_of_speech} · ${item.definition_zh || "暂无中文释义"}`;
  document.querySelector("#review-answer").hidden = true;
  document.querySelector("#review-ratings").hidden = true;
  document.querySelector("#reveal-answer").hidden = false;
  reviewStartedAt = performance.now();
}

async function rateReview(rating) {
  const item = reviewItems[reviewIndex]; if (!item) return;
  await api(`/api/reviews/${encodeURIComponent(item.card_id)}`, {method: "POST", body: JSON.stringify({rating, idempotency_key: crypto.randomUUID(), response_time_ms: Math.round(performance.now() - reviewStartedAt)})});
  reviewIndex += 1;
  if (reviewIndex >= reviewItems.length) {
    document.querySelector("#review-card").hidden = true;
    document.querySelector("#review-empty").hidden = false;
    document.querySelector("#review-empty").textContent = `今日 ${reviewItems.length} 张卡片已完成。`;
    notify("今日复习完成。", "success"); await loadStats(); return;
  }
  renderReview();
}

async function searchVocabulary(event) {
  event.preventDefault(); const query = document.querySelector("#search-query").value.trim(); if (!query) return;
  const params = new URLSearchParams({q: query, limit: "30"}); if (selectedCollection()) params.set("collection_id", selectedCollection());
  const data = await api(`/api/vocabulary/search?${params}`);
  const container = document.querySelector("#search-results");
  if (!data.items.length) { container.innerHTML = '<div class="empty-state">没有匹配词义。</div>'; return; }
  container.replaceChildren(...data.items.map(item => {
    const card = document.createElement("article"); card.className = "result-card";
    card.innerHTML = `<div><h3>${escapeHtml(item.lemma)}</h3><span>${escapeHtml(item.part_of_speech)}</span></div><p>${escapeHtml(item.definition_en)}</p><small>识别 ${Math.round(item.recognition_score * 100)}% · 回忆 ${Math.round(item.recall_score * 100)}% · 来源 ${escapeHtml(item.source)}</small>`;
    return card;
  }));
}

async function loadProgress() {
  const data = await api("/api/reports/weekly?days=7");
  const cards = [
    ["评估", data.assessment.total, "次回答"], ["复习", data.reviews.attempts, "次作答"],
    ["阅读", data.reading.documents, "篇材料"], ["写作", data.production.attempts, "次练习"]
  ];
  document.querySelector("#progress-content").replaceChildren(...cards.map(([label, value, suffix]) => {
    const card = document.createElement("article"); card.innerHTML = `<span>${label}</span><strong>${formatNumber(value)}</strong><small>${suffix}</small>`; return card;
  }));
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => showView(button.dataset.view)));
document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => showView(button.dataset.go)));
document.querySelector("#assessment-start").addEventListener("click", () => void startAssessment().catch(error => notify(error.message, "error")));
document.querySelectorAll("[data-assessment]").forEach(button => button.addEventListener("click", () => void answerAssessment(button.dataset.assessment).catch(error => notify(error.message, "error"))));
document.querySelector("#review-start").addEventListener("click", () => void startReview().catch(error => notify(error.message, "error")));
document.querySelector("#reveal-answer").addEventListener("click", event => { event.currentTarget.hidden = true; document.querySelector("#review-answer").hidden = false; document.querySelector("#review-ratings").hidden = false; });
document.querySelectorAll("[data-rating]").forEach(button => button.addEventListener("click", () => void rateReview(button.dataset.rating).catch(error => notify(error.message, "error"))));
document.querySelector("#search-form").addEventListener("submit", event => void searchVocabulary(event).catch(error => notify(error.message, "error")));
document.querySelector("#progress-refresh").addEventListener("click", () => void loadProgress().catch(error => notify(error.message, "error")));
document.addEventListener("keydown", event => {
  if (!document.querySelector("#view-assessment").classList.contains("active")) return;
  const map = {"1": "unknown", "2": "unsure", "3": "known"}; if (map[event.key]) void answerAssessment(map[event.key]).catch(error => notify(error.message, "error"));
});

void loadStats().catch(error => notify(error.message, "error"));
