const state = {
  conversationId: null,
  graph: { nodes: [], edges: [] },
  animationId: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

document.addEventListener("DOMContentLoaded", () => {
  wireTabs();
  wireChat();
  wireDemoPrompts();
  checkHealth();
  loadGraph();
  $("#ingestBtn").addEventListener("click", syncNow);
  $("#refreshGraphBtn").addEventListener("click", loadGraph);
});

function wireTabs() {
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".tab").forEach((tab) => tab.classList.remove("active"));
      button.classList.add("active");
      $$(".view").forEach((view) => view.classList.remove("active"));
      $(`#${button.dataset.view}View`).classList.add("active");
      if (button.dataset.view === "graph") {
        drawGraph();
      }
    });
  });
}

function wireChat() {
  $("#chatForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#messageInput");
    const message = input.value.trim();
    if (!message) return;
    input.value = "";
    await ask(message);
  });
}

function wireDemoPrompts() {
  $$(".demo-prompts button").forEach((button) => {
    button.addEventListener("click", () => {
      $("#messageInput").value = button.dataset.prompt;
      ask(button.dataset.prompt);
    });
  });
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    $("#healthText").textContent =
      data.status === "ok"
        ? "API online"
        : `Degraded: Neo4j ${data.neo4j}, Qdrant ${data.qdrant}, Postgres ${data.postgres}`;
  } catch {
    $("#healthText").textContent = "API offline";
  }
}

async function syncNow() {
  const button = $("#ingestBtn");
  button.textContent = "Syncing...";
  button.disabled = true;
  try {
    const response = await fetch("/api/ingest/sync-now", { method: "POST" });
    const run = await response.json();
    button.textContent = `Synced ${run.upserted_records} changed`;
    await loadGraph();
  } catch {
    button.textContent = "Sync failed";
  } finally {
    setTimeout(() => {
      button.textContent = "Sync customer records";
      button.disabled = false;
    }, 2200);
  }
}

async function ask(message) {
  $("#answer").textContent = "Retrieving graph context, live documentation, and release evidence";
  $("#answer").classList.add("loading");
  $("#steps").innerHTML = "";
  $("#contradictions").innerHTML = "";
  clearEvidence();
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: state.conversationId }),
    });
    const data = await response.json();
    state.conversationId = data.conversation_id;
    renderAnswer(data);
  } catch (error) {
    $("#answer").textContent = `Request failed: ${error.message}`;
  } finally {
    $("#answer").classList.remove("loading");
  }
}

function renderAnswer(data) {
  $("#conversationId").textContent = `Conversation ${data.conversation_id.slice(0, 8)}`;
  $("#confidencePill").textContent = `Evidence: ${data.confidence}`;
  $("#answer").classList.remove("empty");
  $("#answer").textContent = `${data.concise_answer}\n\nReasoning: ${data.reasoning_summary}`;
  renderSteps(data.retrieval_steps || []);
  renderContradictions([...(data.contradictions || []), ...(data.warnings || []).map((message) => ({ severity: "notice", message }))]);
  renderEvidence("#customerEvidence", data.customer_evidence || []);
  renderEvidence("#productEvidence", data.product_evidence || []);
  renderEvidence("#releaseEvidence", data.release_evidence || []);
  appendHistory(data);
}

function renderSteps(steps) {
  $("#steps").innerHTML = steps
    .map(
      (step) => `
      <div class="step">
        <strong>${escapeHtml(step.name)}</strong>
        <small>${escapeHtml(step.status)}${step.source_type ? ` - ${escapeHtml(step.source_type)}` : ""}</small>
        <p>${escapeHtml(step.detail)}</p>
      </div>`,
    )
    .join("");
}

function renderContradictions(items) {
  $("#contradictions").innerHTML =
    items.length === 0
      ? '<div class="step"><small>No contradictions detected in retrieved evidence.</small></div>'
      : items
          .map(
            (item) => `
            <div class="notice">
              <strong>${escapeHtml(item.severity || "notice")}</strong>
              <p>${escapeHtml(item.message)}</p>
            </div>`,
          )
          .join("");
}

function renderEvidence(selector, evidence) {
  const node = $(selector);
  node.innerHTML =
    evidence.length === 0
      ? '<div class="evidence-card"><small>No evidence returned for this source.</small></div>'
      : evidence
          .map((item) => {
            const link = item.url ? `<a href="${item.url}" target="_blank" rel="noreferrer">Open source</a>` : "";
            return `
              <div class="evidence-card">
                <strong>${escapeHtml(item.title)}</strong>
                <small>${escapeHtml(item.record_id || item.entity_type || item.source_type)} - score ${Number(item.score || 0).toFixed(2)}</small>
                <p>${escapeHtml(item.snippet)}</p>
                ${link}
              </div>`;
          })
          .join("");
}

function clearEvidence() {
  ["#customerEvidence", "#productEvidence", "#releaseEvidence"].forEach((selector) => {
    $(selector).innerHTML = '<div class="evidence-card"><small>Waiting for retrieval.</small></div>';
  });
}

function appendHistory(data) {
  const item = document.createElement("div");
  item.className = "history-item";
  item.innerHTML = `<small>${new Date(data.created_at).toLocaleString()} - ${escapeHtml(data.confidence)}</small><p>${escapeHtml(data.concise_answer)}</p>`;
  $("#historyList").prepend(item);
}

async function loadGraph() {
  try {
    const response = await fetch("/api/graph");
    state.graph = await response.json();
    $("#graphMeta").textContent = `${state.graph.nodes.length} entities and ${state.graph.edges.length} relationships visible`;
    drawGraph();
  } catch {
    $("#graphMeta").textContent = "Graph unavailable";
  }
}

function drawGraph() {
  const canvas = $("#graphCanvas");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(800, rect.width * window.devicePixelRatio);
  canvas.height = Math.max(520, rect.height * window.devicePixelRatio);
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const nodes = layoutNodes(state.graph.nodes || [], width, height);
  const edges = state.graph.edges || [];
  cancelAnimationFrame(state.animationId);
  let tick = 0;

  const render = () => {
    tick += 0.008;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#fbfcf8";
    ctx.fillRect(0, 0, width, height);
    drawTreeLines(ctx, nodes, edges, tick);
    drawNodes(ctx, nodes, tick);
    state.animationId = requestAnimationFrame(render);
  };
  render();
}

function layoutNodes(rawNodes, width, height) {
  const groups = ["Account", "FeatureRequest", "Issue", "Task", "Meeting", "Person", "Plan", "ProductFeature"];
  const grouped = new Map(groups.map((group) => [group, []]));
  rawNodes.forEach((node) => {
    const key = grouped.has(node.type) ? node.type : "ProductFeature";
    grouped.get(key).push(node);
  });
  const nodes = new Map();
  groups.forEach((group, groupIndex) => {
    const list = grouped.get(group) || [];
    const x = (width * (groupIndex + 1)) / (groups.length + 1);
    list.slice(0, 18).forEach((node, index) => {
      const spread = height * 0.76;
      const y = height * 0.12 + ((index + 0.5) / Math.max(list.length, 1)) * spread;
      nodes.set(node.id, { ...node, x, y, radius: group === "Account" ? 10 : 7 });
    });
  });
  return nodes;
}

function drawTreeLines(ctx, nodes, edges, tick) {
  ctx.lineWidth = 1.2 * window.devicePixelRatio;
  edges.forEach((edge, index) => {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return;
    const pulse = 0.35 + 0.28 * Math.sin(tick * 4 + index);
    ctx.strokeStyle = `rgba(82, 96, 106, ${pulse})`;
    ctx.beginPath();
    const midX = (source.x + target.x) / 2;
    ctx.moveTo(source.x, source.y);
    ctx.bezierCurveTo(midX, source.y, midX, target.y, target.x, target.y);
    ctx.stroke();
  });
}

function drawNodes(ctx, nodes, tick) {
  nodes.forEach((node, index) => {
    const wobble = Math.sin(tick * 3 + index) * 2 * window.devicePixelRatio;
    ctx.beginPath();
    ctx.fillStyle = colorFor(node.type);
    ctx.arc(node.x, node.y + wobble, node.radius * window.devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#17211f";
    ctx.font = `${11 * window.devicePixelRatio}px Inter, sans-serif`;
    const label = String(node.label || node.id).slice(0, 24);
    ctx.fillText(label, node.x + 12 * window.devicePixelRatio, node.y + 4 * window.devicePixelRatio + wobble);
  });
}

function colorFor(type) {
  return {
    Account: "#2f7f74",
    FeatureRequest: "#5d8c6a",
    ProductFeature: "#7f9a61",
    Issue: "#c7654d",
    Task: "#52606a",
    Meeting: "#c88a2d",
    Person: "#b58a3c",
    Plan: "#8b9478",
  }[type] || "#52606a";
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
