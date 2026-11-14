"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: null, user: null, runs: [], run: null, plan: null, session: null, tab: null, epoch: 0};
const sections = ["trigger", "diagnosis", "steps", "rollback", "contacts", "pitfalls", "rationale"];
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function notice(text = "", error = false) { $("notice").textContent = text; $("notice").dataset.error = String(error); }
function action(fn) { return async (event) => {
  event?.preventDefault();
  const button = event?.currentTarget;
  if (button?.tagName === "BUTTON") button.disabled = true;
  try { await fn(event); } catch (error) { notice(error.message || "The request could not finish. Try again.", true); }
  finally { if (button?.tagName === "BUTTON") button.disabled = false; }
}; }
function button(text, fn, cls = "") { const n = el("button", text, cls); n.type = "button"; n.addEventListener("click", action(fn)); return n; }
function field(label, type = "input", value = "") {
  const box = el("div"), id = `field-${crypto.randomUUID()}`;
  const name = el("label", label); name.htmlFor = id;
  const input = el(type); input.id = id; input.value = value;
  box.append(name, input); return {box, input};
}
async function response(path, options = {}) {
  const epoch = state.epoch;
  const result = await fetch(path, {...options, headers: {"Authorization": `Bearer ${state.token}`, ...(options.body ? {"Content-Type": "application/json"} : {})}});
  if (epoch !== state.epoch) throw new Error("The workspace session changed.");
  if (!result.ok) {
    const body = await result.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${result.status}).`);
  }
  return result;
}
async function api(path, method = "GET", body) {
  return (await response(path, {method, ...(body === undefined ? {} : {body: JSON.stringify(body)})})).json();
}
async function stream(path, body, saved) {
  const epoch = state.epoch;
  const result = await response(path, {method: "POST", ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  saved?.(result.headers.get("X-EKI-Message-ID"));
  if (!result.body) throw new Error("Saved. Reopen the interview to resume processing.");
  const reader = result.body.getReader(), decoder = new TextDecoder();
  let buffer = "", terminal = false;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (epoch !== state.epoch) { await reader.cancel(); throw new Error("The workspace session changed."); }
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done}).replace(/\r/g, "");
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const data = block.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const event = JSON.parse(data);
        if (event.type === "error") throw new Error(event.message);
        if (event.type === "delivery") notice(`Answer ${event.status}. Processing the saved message…`);
        else if (event.type === "note") notice(`Recorded ${event.section}${event.confidential ? " confidentially" : ""}.`);
        else if (event.type === "recovered") notice("Resuming your saved conversation…");
        else if (event.type === "question" || event.type === "done") terminal = true;
      }
      if (done) break;
    }
    if (!terminal) throw new Error("The connection ended. Reopen the interview to check delivery and resume.");
    notice("Conversation saved.");
  } finally { reader.releaseLock(); }
}
function heading(title, context) { $("title").textContent = title; $("context").textContent = context || "WORKSPACE"; }
function content(...nodes) { $("content").replaceChildren(...nodes); }
function tabs(active) {
  state.tab = active; const nav = $("tabs"); nav.replaceChildren();
  if (!state.run) return;
  const items = state.run.can_manage ? [["plans", "Interview plans", showPlans], ["review", "Review entries", showReview], ["knowledge", "Approved knowledge", showKnowledge]] : [["knowledge", "Approved knowledge", showKnowledge]];
  for (const [key, label, fn] of items) {
    const n = button(label, fn, key === active ? "active" : "");
    if (key === active) n.setAttribute("aria-current", "page"); nav.append(n);
  }
}
async function loadRuns() {
  state.runs = await api("/runs"); $("runs").replaceChildren();
  for (const run of state.runs) {
    const n = button("", () => chooseRun(run), `run${state.run?.id === run.id ? " active" : ""}`);
    n.append(el("strong", run.expert_name), el("small", `${run.since.slice(0,10)} — ${run.until.slice(0,10)}`), el("small", run.can_manage ? "Expert workspace" : "Approved knowledge")); $("runs").append(n);
  }
  if (!state.runs.length) $("runs").append(el("p", "No handovers have been shared with this identity.", "muted"));
}
async function chooseRun(run) {
  state.run = run; state.session = null; state.plan = null;
  await loadRuns(); heading(`${run.expert_name}’s handover`, "KNOWLEDGE WORKSPACE");
  await (run.can_manage ? showPlans() : showKnowledge());
}
async function showPlans() {
  if (!state.run) return;
  tabs("plans"); heading("A plan for the conversation", state.run.expert_name);
  const plans = await api(`/runs/${state.run.id}/plans`), box = el("div", null, "stack");
  const top = el("div", null, "row between"); top.append(el("p", "Review the topics before starting the interview.", "muted"), button("Draft a new plan", async () => {
    notice("Drafting questions from the selected knowledge gaps…");
    const plan = await api(`/runs/${state.run.id}/plan`, "POST"); await showPlan(plan.id); notice("Draft ready for review.");
  }, "primary")); box.append(top);
  for (const plan of plans) {
    const card = el("div", null, "card row between");
    const label = el("div"); label.append(el("h3", plan.status === "approved" ? "Interview in progress or completed" : "Plan ready for review"), el("p", `Plan ${plan.id.slice(0,8)}`, "fine"));
    card.append(label, button(plan.session_id ? "Open conversation →" : "Review plan →", () => plan.session_id ? showSession(plan.session_id) : showPlan(plan.id)));
    box.append(card);
  }
  if (!plans.length) box.append(el("div", "No interview plans yet. Draft one from this handover’s mapped knowledge gaps.", "empty"));
  content(box);
}
async function showPlan(id) {
  const plan = await api(`/plans/${id}`); state.plan = plan; tabs("plans"); heading("Shape your interview", "PLAN REVIEW");
  const box = el("div"); box.append(el("p", "Keep the topics that matter. Every drafted question starts with a source.", "muted"));
  const editable = plan.status === "draft";
  for (const [index, topic] of plan.topics.entries()) {
    const card = el("article", null, "card"); card.append(el("p", `TOPIC ${String(index+1).padStart(2,"0")}`, "topic-number"), el("h2", topic.label), el("p", topic.reason, "muted"));
    for (const question of topic.questions) {
      card.append(el("p", question.text));
      if (question.quote) card.append(el("blockquote", `${question.quote}\n— ${question.artifact_id}`));
    }
    if (editable) {
      const tools = el("div", null, "toolbar");
      const move = async (delta) => { const order = plan.topics.map(t => t.topic_id); [order[index],order[index+delta]] = [order[index+delta],order[index]]; await api(`/plans/${id}`, "PATCH", {order}); await showPlan(id); };
      if (index) tools.append(button("↑ Move up", () => move(-1)));
      if (index < plan.topics.length-1) tools.append(button("↓ Move down", () => move(1)));
      tools.append(button("Remove topic", async () => { await api(`/plans/${id}`, "PATCH", {remove:[topic.topic_id]}); await showPlan(id); }, "danger")); card.append(tools);
    }
    box.append(card);
  }
  if (editable) {
    const add = el("form", null, "card"); add.append(el("h3", "Something else only you know?"));
    const label = field("Topic name"), note = field("What should we ask about?", "textarea"); label.input.required = note.input.required = true;
    const submit = el("button", "Add topic"); submit.type = "submit";
    add.append(label.box, note.box, el("p"), submit); add.addEventListener("submit", action(async () => { await api(`/plans/${id}`, "PATCH", {add:[{label:label.input.value,note:note.input.value}]}); await showPlan(id); })); box.append(add);
    const approve = button("Approve plan & start interview →", async () => { const view = await api(`/plans/${id}/approve`, "POST", {approved_by:state.user.name}); await showSession(view.id); }, "primary"); approve.disabled = !plan.topics.length; box.append(approve);
  }
  content(box);
}
async function showSession(id) {
  const view = await api(`/sessions/${id}`), pending = await api(`/sessions/${id}/messages`);
  state.session = view; tabs("plans"); heading(view.status === "finished" ? "The conversation is captured" : "Let’s capture the details", "EXPERT INTERVIEW");
  const box = el("div"), checks = el("div", null, "checklist");
  for (const [section,status] of Object.entries(view.checklist)) { const n = el("span", `${section}: ${status}`); n.dataset.state = status; checks.append(n); } box.append(checks);
  for (const turn of view.turns) {
    const n = el("div", null, `turn ${turn.speaker}`); n.id = `turn-${turn.ordinal}`;
    n.append(el("small", `${turn.speaker === "expert" ? "YOU" : "INTERVIEWER"} · TURN ${turn.ordinal}${turn.kind === "correction" ? ` · CORRECTS ${turn.corrects}` : ""}`), el("div", turn.text)); box.append(n);
  }
  for (const receipt of pending) {
    const n = el("div", null, "delivery"); n.append(el("p", `Your answer is saved. Delivery: ${receipt.status}.`), button("Resume saved answer", async () => {
      try { await stream(`/sessions/${id}/messages/${receipt.id}/resume`); } finally { await showSession(id); }
    }, "primary")); box.append(n);
  }
  if (view.status === "finished") box.append(button("Review drafted entries →", () => showReview(id), "primary"));
  if (view.waiting && !pending.length) {
    const q = el("div", null, "question"); q.append(el("p", "CURRENT QUESTION", "eyebrow"), el("h3", view.waiting.question)); box.append(q);
    const form = el("form", null, "card"), answer = field("Your answer", "textarea"); answer.input.required = true;
    const privacy = el("label"), check = el("input"); check.type = "checkbox"; privacy.append(check, document.createTextNode("Confidential — keep this answer out of the runbook and model context"));
    const send = el("button", "Save answer & continue →", "primary"); send.type = "submit";
    const sendMessage = async (actionName) => {
      let accepted = false;
      send.disabled = true;
      try { await stream(`/sessions/${id}/messages`, {message_id:crypto.randomUUID(), question_turn:view.waiting.turn, action:actionName, text:answer.input.value, confidential:actionName === "answer" && check.checked}, () => { accepted = true; answer.input.value = ""; }); }
      finally { if (accepted) await showSession(id); else send.disabled = false; }
    };
    form.append(answer.box, privacy, el("div", null, "toolbar")); form.lastChild.append(send, button("Skip this question", () => sendMessage("skip")));
    form.addEventListener("submit", action(() => sendMessage("answer"))); box.append(form);
  } else if (view.status !== "finished" && !pending.length) {
    box.append(button("Recover conversation", async () => { try { await stream(`/sessions/${id}/resume`); } finally { await showSession(id); } }, "primary"));
  }
  const correction = el("details", null, "card"); correction.append(el("summary", "Correct an earlier answer"));
  const form = el("form"), target = field("Your answer’s turn number"), text = field("Corrected answer", "textarea"); target.input.type = "number"; target.input.min = "1"; target.input.required = text.input.required = true;
  const save = el("button", "Save correction"); save.type = "submit"; form.append(target.box, text.box, el("p"), save);
  form.addEventListener("submit", action(async () => { await stream(`/sessions/${id}/messages`, {message_id:crypto.randomUUID(), action:"correct",corrects:Number(target.input.value),text:text.input.value}, () => { text.input.value = ""; }); await showSession(id); })); correction.append(form); box.append(correction); content(box);
}
async function showReview(sessionId) {
  tabs("review"); heading("Your knowledge, in your words", "REVIEW & APPROVE");
  if (typeof sessionId !== "string") sessionId = state.session?.id;
  if (!sessionId) {
    const plans = await api(`/runs/${state.run.id}/plans`), box = el("div", null, "stack");
    for (const plan of plans.filter(p => p.session_id)) box.append(button(`Review interview ${plan.id.slice(0,8)} →`, () => showReview(plan.session_id)));
    if (!box.children.length) box.append(el("div", "Complete an interview to create draft entries.", "empty")); content(box); return;
  }
  const entries = await api(`/sessions/${sessionId}/entries`), layout = el("div", null, "review-layout"), list = el("div", null, "review-list"), detail = el("div");
  for (const entry of entries) { const n = button(entry.title, () => showEntry(entry.id, detail), "entry-link"); n.append(el("small", `${entry.status} · v${entry.version} · ${entry.unsupported_count} claims to check`)); list.append(n); }
  layout.append(list, detail); content(layout);
  if (entries.length) await showEntry(entries[0].id, detail);
  else detail.append(el("div", "Drafts will appear after the interview is complete.", "empty"));
}
async function showEntry(id, target = null) {
  const entry = await api(`/entries/${id}`), card = el("article", null, "card"), manager = Boolean(entry.decisions);
  card.append(el("div", `${entry.status.toUpperCase()} · VERSION ${entry.version}`, "eyebrow"), el("h2", entry.title));
  if (entry.rejection_reason) card.append(el("p", `Review note: ${entry.rejection_reason}`, "muted"));
  for (const section of sections) {
    const part = el("section", null, "section"); part.append(el("h3", section));
    const claims = el(section === "steps" ? "ol" : "div");
    for (const [index,claim] of entry[section].claims.entries()) {
      const n = el(section === "steps" ? "li" : "div", claim.text, "claim");
      for (const cite of claim.citations) {
        const label = cite.kind === "interview_turn" ? `Turn ${cite.turn_index}` : cite.artifact_id;
        if (manager && cite.kind === "interview_turn") n.append(button(label, async () => {
          const view = await api(`/sessions/${cite.session_id}`), turn = view.turns.find(t => t.ordinal === cite.turn_index);
          let source = n.querySelector(".source-panel"); if (!source) { source = el("div", null, "source-panel"); n.append(source); }
          source.textContent = turn ? turn.text : "Source turn unavailable.";
        }, "source")); else n.append(el("span", label, "source"));
      }
      const flag = entry.unsupported?.find(u => u.section === section && u.claim_index === index);
      if (flag) n.append(el("p", `Check against the source: ${flag.terms.join(", ")}`, "warning")); claims.append(n);
    }
    part.append(claims); if (entry[section].open_question) part.append(el("p", `Open question: ${entry[section].open_question}`, "open")); card.append(part);
  }
  const tools = el("div", null, "toolbar");
  if (manager) {
    if (entry.status === "draft") tools.append(button("Approve entry", async () => { await api(`/entries/${id}/approve`, "POST", {approved_by:state.user.name}); await showEntry(id,target); notice("Entry approved. It is now available to permitted readers."); }, "primary"));
    tools.append(button("Edit entry", () => editEntry(entry, target)));
    if (entry.status === "draft") {
      const reason = field("Reason for rejection");
      tools.append(reason.box, button("Reject entry", async () => { if (!reason.input.value.trim()) throw new Error("Add a reason for rejection."); await api(`/entries/${id}/reject`, "POST", {rejected_by:state.user.name,reason:reason.input.value}); await showEntry(id,target); }, "danger"));
    }
  }
  if (entry.status === "approved") tools.append(button("Download Markdown", () => download(`/entries/${id}/markdown`, "runbook-entry.md")));
  card.append(tools);
  if (target) target.replaceChildren(card); else content(card);
}
function editEntry(entry, target) {
  const form = el("form", null, "card"); form.append(el("h2", "Refine this entry"), el("p", "Separate claims or procedure steps with a blank line. Saving returns the entry to draft for review.", "muted"));
  const inputs = {};
  for (const section of sections) {
    const claims = field(`${section}: claims`, "textarea", entry[section].claims.map(c => c.text).join("\n\n")), open = field(`${section}: open question`, "input", entry[section].open_question || "");
    inputs[section] = {claims:claims.input,open:open.input}; form.append(claims.box,open.box);
  }
  const save = el("button", "Save draft", "primary"); save.type = "submit"; form.append(el("p"),save,button("Cancel", () => showEntry(entry.id,target)));
  form.addEventListener("submit", action(async () => {
    const changed = Object.fromEntries(sections.map(s => [s,{claims:inputs[s].claims.value.split(/\n\s*\n/).map(t => t.trim()).filter(Boolean),open_question:inputs[s].open.value.trim() || null}]));
    await api(`/entries/${entry.id}`, "PATCH", {edited_by:state.user.name,sections:changed}); await showEntry(entry.id,target); notice("Edits saved as a cited expert turn. Review and approve when ready.");
  }));
  if (target) target.replaceChildren(form); else content(form);
}
async function download(path, name) {
  const blob = await (await response(path)).blob(), url = URL.createObjectURL(blob), link = el("a");
  link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}
async function showKnowledge() {
  tabs("knowledge"); heading("Knowledge your team can use", "APPROVED RUNBOOK");
  const box = el("div"), form = el("form", null, "search-row"), label = el("label", "Search or ask across your permitted approved entries"); label.htmlFor = "knowledge-query";
  const query = el("input"); query.id = "knowledge-query"; query.placeholder = "How do I roll back month-end reconciliation?"; query.required = true;
  const results = el("div");
  async function search(ask) {
    if (!query.value.trim()) throw new Error("Enter a question or search term.");
    const data = await api(ask ? "/runbook/ask" : "/runbook/search", "POST", ask ? {question:query.value} : {query:query.value}); results.replaceChildren();
    if (ask) {
      const card = el("div", null, "card"); card.append(el("h3", data.status === "answered" ? "From the approved runbook" : "The runbook does not cover this yet"), el("p", data.answer || "There is not enough approved evidence to answer this question."));
      for (const evidence of data.evidence || []) card.append(button(`${evidence.id} · ${evidence.title} / ${evidence.section}`, () => showEntry(evidence.entry_id), "source")); results.append(card);
    } else {
      for (const hit of data.results) { const card = el("div", null, "card"); card.append(button(hit.title, () => showEntry(hit.id)),el("p",hit.snippet,"muted")); results.append(card); }
      if (!data.results.length) results.append(el("p","No matching approved entries.","empty"));
    }
  }
  const submit = el("button","Search"); submit.type = "submit"; form.append(label,query,submit,button("Ask runbook",() => search(true),"primary")); form.addEventListener("submit",action(() => search(false)));
  box.append(form,el("p","Answers use approved claims with their citations. Search covers all handovers shared with you.","fine"),results);
  const all = await api(`/runbook/entries?run_id=${state.run.id}`), list = el("div",null,"stack");
  for (const entry of all) { const card = el("div",null,"card row between"); card.append(el("div",entry.title),button("Read entry →",() => showEntry(entry.id))); list.append(card); }
  if (!all.length) list.append(el("div","No approved entries in this handover yet.","empty"));
  box.append(el("h2","In this handover"),list);
  if (all.length) box.append(button("Download handover as Markdown",() => download(`/runbook/export?run_id=${state.run.id}`,"runbook.md")));
  content(box);
}
$("login").addEventListener("submit",action(async () => {
  state.token = $("token").value.trim(); state.user = await api("/runbook/identity"); $("token").value = "";
  $("identity").textContent = state.user.name; $("welcome").hidden = true; $("app").hidden = false; $("logout").hidden = false;
  await loadRuns(); if (state.runs.length) await chooseRun(state.runs[0]); notice();
}));
$("logout").addEventListener("click",() => {
  state.epoch++; Object.assign(state,{token:null,user:null,runs:[],run:null,plan:null,session:null});
  $("identity").textContent = ""; $("token").value = ""; $("runs").replaceChildren(); $("content").replaceChildren(); $("tabs").replaceChildren();
  $("app").hidden = true; $("logout").hidden = true; $("welcome").hidden = false; notice("Signed out.");
} );
$("refresh-runs").addEventListener("click",action(loadRuns));
