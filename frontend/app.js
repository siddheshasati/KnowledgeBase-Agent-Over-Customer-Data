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
  $("#answer").innerHTML = formatAnswerHtml(data.concise_answer || "No grounded answer was produced.", data);
  renderSteps(data.retrieval_steps || []);
  renderContradictions([...(data.contradictions || []), ...(data.warnings || []).map((message) => ({ severity: "notice", message }))]);
  renderEvidence("#customerEvidence", data.customer_evidence || []);
  renderEvidence("#productEvidence", data.product_evidence || []);
  renderEvidence("#releaseEvidence", data.release_evidence || []);
  appendHistory(data);
}

function formatAnswerHtml(answerText, data) {
  const value = String(answerText || "").trim();
  const normalized = value.replace(/^Based on the retrieved evidence,\s*/i, "");
  const parsed = parseStructuredSummary(normalized);

  if (!parsed.title && !parsed.productArea && !parsed.status && !parsed.accounts) {
    return `<div class="answer-plain">${escapeHtml(value || "No grounded answer was produced.")}</div>`;
  }

  const rows = [
    ["Title", parsed.title],
    ["Product area", parsed.productArea],
    ["Status", parsed.status],
    ["Accounts", parsed.accounts],
    ["Mentions", parsed.mentions],
    ["Revenue impact", parsed.revenue],
  ].filter(([, value]) => value && String(value).trim());

  const summary = parsed.summary || value;

  return `
    <div class="answer-structured">
      <div class="answer-header">
        <span class="answer-kicker">Executive summary</span>
        <h3>${escapeHtml(parsed.title || "Customer request summary")}</h3>
      </div>
      <table class="fact-table">
        <tbody>
          ${rows
      .map(
        ([label, cellValue]) => `
                <tr>
                  <th>${escapeHtml(label)}</th>
                  <td>${escapeHtml(String(cellValue))}</td>
                </tr>`,
      )
      .join("")}
        </tbody>
      </table>
      <div class="answer-summary">
        ${escapeHtml(summary)}
      </div>
      ${data && data.reasoning_summary ? `<div class="answer-reasoning">${escapeHtml(data.reasoning_summary)}</div>` : ""}
    </div>
  `;
}

function parseStructuredSummary(text) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  const match = (pattern, fallback = "") => {
    const found = clean.match(pattern);
    return found && found[1] ? found[1].trim() : fallback;
  };

  const title = match(/Title\s*:\s*([^]+?)(?=\s+(?:Product Area|Status|Accounts Requesting|Accounts:|Mentions:|Est\.? Revenue Impact|Graph context|$))/i);
  const productArea = match(/Product Area\s*:\s*([^]+?)(?=\s+(?:Status|Accounts Requesting|Accounts:|Mentions:|Est\.? Revenue Impact|Graph context|$))/i);
  const status = match(/Status\s*:\s*([^]+?)(?=\s+(?:Accounts Requesting|Accounts:|Mentions:|Est\.? Revenue Impact|Graph context|$))/i);
  const accounts = match(/Accounts Requesting\s*:\s*([^]+?)(?=\s+(?:Mentions:|Est\.? Revenue Impact|Graph context|$))/i) || match(/Accounts\s*:\s*([^]+?)(?=\s+(?:Mentions:|Est\.? Revenue Impact|Graph context|$))/i);
  const mentions = match(/Mentions\s*:\s*([^]+?)(?=\s+(?:Est\.? Revenue Impact|Graph context|$))/i);
  const revenue = match(/Est\.?\s*Revenue Impact\s*:\s*([^]+?)(?=\s+(?:Graph context|$))/i);

  const summary = clean.replace(/\s*(?:Title|Product Area|Status|Accounts Requesting|Accounts:|Mentions:|Est\.? Revenue Impact|Graph context)\s*:/gi, "");

  return {
    title,
    productArea,
    status,
    accounts,
    mentions,
    revenue,
    summary: summary.trim() || clean,
  };
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
          const snippet = (item.snippet || "").replace(/\s+/g, " ").trim();
          const compactSnippet = snippet.length > 220 ? `${snippet.slice(0, 220)}…` : snippet;
          return `
              <div class="evidence-card">
                <strong>${escapeHtml(item.title)}</strong>
                <small>${escapeHtml(item.record_id || item.entity_type || item.source_type)} - score ${Number(item.score || 0).toFixed(2)}</small>
                <p>${escapeHtml(compactSnippet)}</p>
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

async function refreshGraph() {
  try {
    const response = await fetch("/api/graph");
    state.graph = await response.json();
    const summary = state.graph.schema_summary || {};
    const totalNodes = summary.total_nodes ?? (state.graph.nodes || []).length;
    const totalEdges = summary.total_edges ?? (state.graph.edges || []).length;
    const schemaNames = (summary.entity_types || []).length ? summary.entity_types.join(", ") : "knowledge graph";
    $("#graphMeta").textContent = `${totalNodes} entities · ${totalEdges} relationships · schema: ${schemaNames}`;
    drawGraph();
  } catch {
    $("#graphMeta").textContent = "Graph unavailable";
  }
}

async function loadGraph() {
  await refreshGraph();
}

function drawGraph() {
  const canvas = $("#graphCanvas");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(900, Math.round(rect.width * dpr));
  canvas.height = Math.max(520, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  const width = canvas.width / dpr;
  const height = canvas.height / dpr;
  const nodes = layoutGraph(state.graph.nodes || [], state.graph.edges || [], width, height);
  const edges = state.graph.edges || [];

  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#fbfcf8";
  ctx.fillRect(0, 0, width, height);
  drawEdges(ctx, nodes, edges);
  drawNodes(ctx, nodes);

  canvas.onclick = () => {
    refreshGraph();
  };
}

function layoutGraph(rawNodes, edges, width, height) {
  const typeSeeds = {
    Account: { x: width * 0.2, y: height * 0.25 },
    FeatureRequest: { x: width * 0.35, y: height * 0.72 },
    Issue: { x: width * 0.52, y: height * 0.3 },
    Task: { x: width * 0.67, y: height * 0.62 },
    Meeting: { x: width * 0.8, y: height * 0.28 },
    Person: { x: width * 0.25, y: height * 0.8 },
    Plan: { x: width * 0.8, y: height * 0.8 },
    ProductFeature: { x: width * 0.55, y: height * 0.82 },
    Schema: { x: width * 0.5, y: height * 0.5 },
  };

  const nodes = new Map();
  rawNodes.forEach((node, index) => {
    const seed = typeSeeds[node.type] || { x: width * 0.5 + ((index % 7) - 3) * 80, y: height * 0.5 + ((index % 5) - 2) * 70 };
    nodes.set(node.id, {
      ...node,
      x: seed.x + ((Math.random() - 0.5) * 30),
      y: seed.y + ((Math.random() - 0.5) * 30),
      radius: node.type === "Account" ? 12 : node.type === "Schema" ? 10 : 8,
      vx: 0,
      vy: 0,
    });
  });

  const relationMap = new Map();
  edges.forEach((edge) => {
    if (!relationMap.has(edge.source)) relationMap.set(edge.source, []);
    if (!relationMap.has(edge.target)) relationMap.set(edge.target, []);
    relationMap.get(edge.source).push(edge.target);
    relationMap.get(edge.target).push(edge.source);
  });

  for (let step = 0; step < 120; step += 1) {
    const forces = new Map();
    nodes.forEach((node) => forces.set(node.id, { x: 0, y: 0 }));

    nodes.forEach((node) => {
      nodes.forEach((other) => {
        if (node.id === other.id) return;
        const dx = node.x - other.x;
        const dy = node.y - other.y;
        const distSq = dx * dx + dy * dy + 0.0001;
        const force = 260 / distSq;
        const fx = (dx / Math.sqrt(distSq)) * force;
        const fy = (dy / Math.sqrt(distSq)) * force;
        forces.get(node.id).x += fx;
        forces.get(node.id).y += fy;
      });
    });

    edges.forEach((edge) => {
      const from = nodes.get(edge.source);
      const to = nodes.get(edge.target);
      if (!from || !to) return;
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const dist = Math.hypot(dx, dy) || 1;
      const spring = (dist - 150) * 0.02;
      const fx = (dx / dist) * spring;
      const fy = (dy / dist) * spring;
      forces.get(edge.source).x += fx;
      forces.get(edge.source).y += fy;
      forces.get(edge.target).x -= fx;
      forces.get(edge.target).y -= fy;
    });

    nodes.forEach((node) => {
      const force = forces.get(node.id);
      node.x += force.x * 0.18;
      node.y += force.y * 0.18;
      node.x = Math.min(width - 20, Math.max(20, node.x));
      node.y = Math.min(height - 20, Math.max(20, node.y));
    });
  }

  return nodes;
}

function drawEdges(ctx, nodes, edges) {
  ctx.lineWidth = 1.2;
  edges.forEach((edge) => {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return;
    ctx.strokeStyle = "rgba(82, 96, 106, 0.44)";
    ctx.beginPath();
    const midX = (source.x + target.x) / 2;
    const midY = (source.y + target.y) / 2 - 24;
    ctx.moveTo(source.x, source.y);
    ctx.quadraticCurveTo(midX, midY, target.x, target.y);
    ctx.stroke();
  });
}

function drawNodes(ctx, nodes) {
  nodes.forEach((node) => {
    ctx.beginPath();
    ctx.fillStyle = colorFor(node.type);
    ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
    ctx.fill();

    ctx.font = "12px Inter, sans-serif";
    ctx.fillStyle = "#1e2a28";
    const label = (node.label || node.type || "Entity").slice(0, 18);
    ctx.fillText(label, node.x + node.radius + 8, node.y + 4);
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
    Schema: "#1a4b66",
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
