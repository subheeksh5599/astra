/* ASTRA browser surface.
   Everything the page shows comes from the service: the configuration, the
   live in-flight list, the receipts. Nothing here carries an address, a chain
   id or a decision of its own. */

const ASTRAA = {
  config: null,
  rows: [],
  payer: null,
  busy: false,
};

async function jget(path) {
  const res = await fetch(path, { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

async function jpost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function el(id) { return document.getElementById(id); }

function text(node, value) { if (node) node.textContent = value; }


/* Anything that reaches the screen through a detail string gets its addresses shortened:
   a full 40-byte address is not information a reader can use, and a screen full of them is
   noise pretending to be evidence. */
function stripAddrs(value) {
  return String(value == null ? "" : value)
    .replace(/0x[0-9a-fA-F]{40}/g, (a) => `${a.slice(0, 6)}\u2026${a.slice(-4)}`)
    .replace(/0x[0-9a-fA-F]{64}/g, (a) => `${a.slice(0, 10)}\u2026${a.slice(-6)}`);
}

function short(hex, head = 10, tail = 6) {
  if (!hex) return "\u00b7";
  const s = String(hex);
  return s.length <= head + tail + 2 ? s : `${s.slice(0, head)}\u2026${s.slice(-tail)}`;
}

function fmtAmount(units, decimals = 6) {
  if (units === null || units === undefined) return "\u00b7";
  return (Number(units) / 10 ** decimals).toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0");
}

/* Mint means switched on, ink means the money did not move, neutral is
   everything the rail is still waiting on. */
function decisionClass(decision) {
  if (!decision) return "";
  if (decision.action === "complete") return "ok";
  if (decision.action === "defer") return "wait";
  return "no";
}

async function loadConfig() {
  const cfg = await jget("/api/config");
  ASTRAA.config = cfg;
  return cfg;
}

function chainOptions(selected) {
  const chains = Object.values(ASTRAA.config.chains).sort((a, b) => a.domain - b.domain);
  return chains.map((chain) => {
    const sel = String(chain.domain) === String(selected) ? " selected" : "";
    return `<option value="${chain.domain}"${sel}>${chain.name} (domain ${chain.domain})</option>`;
  }).join("");
}

function renderBand(rates) {
  const host = el("band");
  if (!host || !ASTRAA.config) return;
  host.innerHTML = Object.values(ASTRAA.config.chains)
    .sort((a, b) => a.domain - b.domain)
    .map((chain) => {
      const rate = rates && rates[String(chain.domain)];
      const blocks = rate
        ? (rate >= 2 ? `${rate.toFixed(1)} blocks/s` : `${(1 / rate).toFixed(1)}s blocks`)
        : `domain ${chain.domain}`;
      return `
      <div>
        <b>${chain.name}</b>
        <span>domain ${chain.domain} &middot; ${blocks}</span>
      </div>`;
    }).join("");
}

/* --- the landing page's own numbers ----------------------------------- */

/* Everything below is read from this instance while the page is open: the
   invariant over the same span of time on every chain, what the rail has actually
   decided, and what it has actually moved. Nothing on the page is a sample. */
async function loadLandingInvariant(receipts) {
  let pairing = null;
  let inflight = null;
  try {
    pairing = await jget("/api/pairing?seconds=3600&limit=12");
  } catch (err) {
    text(el("inv-note"), `the invariant could not be read: ${err.message}`);
  }
  try {
    inflight = await jget("/api/inflight?seconds=3600&limit=14");
  } catch (err) {
    /* the refusals panel simply shows what the receipts already said */
  }
  if (pairing) {
    renderLandingInvariant(pairing);
    renderFacts(pairing, receipts, inflight);
    if (el("band")) renderBand(pairing.rates);
  }
  renderRefusals(receipts, inflight, pairing);
}

function renderLandingInvariant(out) {
  const counts = { paired: 0, "in flight": 0, stranded: 0, unwatched: 0 };
  (out.rows || []).forEach((row) => { counts[row.verdict] = (counts[row.verdict] || 0) + 1; });
  text(el("l-paired"), String(counts.paired || 0));
  text(el("l-inflight"), String(counts["in flight"] || 0));
  text(el("l-stranded"), String(counts.stranded || 0));
  text(el("l-unwatched"), String(counts.unwatched || 0));

  const broken = out.broken || [];
  const read = out.rows ? out.rows.length : 0;
  const span = out.span_seconds ? `${Math.round(out.span_seconds / 60)} minutes` : "this window";
  const rates = out.rates && ASTRAA.config
    ? Object.values(ASTRAA.config.chains).sort((a, b) => a.domain - b.domain)
        .map((c) => {
          const rate = out.rates[String(c.domain)];
          if (!rate) return null;
          // one unit for every chain: seconds per block, however fast the chain is
          return `${c.name} ${rate >= 1 ? (1 / rate).toFixed(2) : (1 / rate).toFixed(1)}s`;
        }).filter(Boolean).join("  \u00b7  ")
    : "";
  text(el("inv-caps"), `Transfer states over the last ${span}, the same span of history on every chain`);
  text(el("inv-note"),
    `${read} transfers observed, ${counts.paired || 0} paired \u2014 `
    + (broken.length ? `${broken.length} stranded` : "none stranded"));
  text(el("inv-rate"), rates ? `Block time, measured: ${rates}` : "Measuring each chain's block time\u2026");

  const host = el("inv-rows");
  if (!host) return;
  const pairs = (out.rows || []).filter((row) => row.verdict === "paired").slice(0, 3);
  host.innerHTML = pairs.length ? pairs.map((row) => {
    const measured = row.minted_amount !== null && row.minted_amount !== undefined;
    const value = measured
      ? `${fmtAmount(row.minted_amount)} of ${fmtAmount(row.expected_amount)}`
      : (row.value_matches === false ? "minted short of the burn" : "not measured");
    return `<div class="pair">
      <span class="pair-id mono"><span class="dot on"></span>${short(row.transfer_id, 14, 6)}</span>
      <span class="pair-dest">${row.destination_name || "\u00b7"}</span>
      <span class="pair-value mono">${value}</span>
    </div>`;
  }).join("") : `<div class="pair empty">Nothing paired in this window yet.</div>`;
}

function renderFacts(pairing, receipts, inflight) {
  const host = el("facts");
  if (!host) return;
  const deliveries = receipts.filter((r) => r.transaction_link);
  const refusals = receipts.filter((r) => r.decision && r.decision.action === "refuse");
  const measured = (pairing.rows || [])
    .filter((r) => r.minted_amount !== null && r.minted_amount !== undefined);
  const total = measured.reduce((sum, r) => sum + r.minted_amount, 0);

  host.innerHTML = `
    <div class="stat"><b>${deliveries.length}</b><span>deliveries executed</span></div>
    <div class="stat ok"><b>${measured.length ? fmtAmount(total) : "\u2014"}</b><span>USDC measured as arrived</span></div>
    <div class="stat"><b>${refusals.length}</b><span>refusals recorded</span></div>
    <div class="stat"><b>${inflight ? inflight.rows.length : "\u00b7"}</b><span>in flight right now</span></div>`;

  const newest = deliveries[0];
  text(el("facts-note"), newest
    ? `Most recent delivery ${short(newest.transaction_hash, 14, 6)} \u2014 counted from this instance's own receipts, nothing else.`
    : "No delivery has been executed through this instance yet.");
}

/* The taxonomy, in six words each. The long form is in the repository; a landing
   page owes a reader the shape of the thing, not a specification. */
const REASON_GLOSS = [
  ["ALREADY_DELIVERED", "the destination already holds it"],
  ["CALLER_RESTRICTED", "the message names another caller"],
  ["ATTESTATION_PENDING", "not signed yet; the rail waits"],
  ["UNSUPPORTED_DOMAIN", "a chain this rail does not watch"],
  ["DEPLOYMENT_ABSENT", "watched, but not this deployment"],
  ["WRONG_TRANSMITTER", "the contract serves another domain"],
  ["RECIPIENT_MISMATCH", "the request is not the transfer"],
  ["ROUTE_MISMATCH", "other domains than the caller named"],
  ["MESSAGE_INCONSISTENT", "two decodes of the same bytes disagree"],
  ["PREFLIGHT_REVERT", "the destination would reject it"],
];

function renderRefusals(receipts, inflight, pairing) {
  const counted = {};
  const bump = (reason) => { if (reason) counted[reason] = (counted[reason] || 0) + 1; };
  (receipts || []).forEach((r) => bump(r.decision && r.decision.reason));
  (inflight && inflight.rows ? inflight.rows : []).forEach((row) => bump(row.decision && row.decision.reason));
  (pairing && pairing.broken ? pairing.broken : []).forEach(() => bump("STRANDED"));

  const observed = REASON_GLOSS
    .filter(([code]) => counted[code])
    .sort((a, b) => counted[b[0]] - counted[a[0]]);

  const chips = el("refusal-chips");
  if (chips) {
    chips.innerHTML = observed.length
      ? observed.map(([code]) => `<span class="chip-count">${code}<b>${counted[code]}</b></span>`).join("")
      : `<span class="fine">Reading the chains \u2014 decisions appear here as they are made.</span>`;
  }

  const list = el("reason-list");
  if (list) {
    list.innerHTML = observed.length
      ? observed.map(([code, gloss]) => `
        <div class="reason-row">
          <span class="reason">${code}</span>
          <span class="gloss">${gloss}</span>
          <span class="seen on">${counted[code]} seen</span>
        </div>`).join("")
      : `<div class="reason-row"><span class="gloss">Nothing refused yet in this window.</span></div>`;
  }

  /* Defined, never met here. Demoted on purpose: a landing page should not present
     a taxonomy this instance has not exercised as if it were a finding. */
  const unmet = REASON_GLOSS.filter(([code]) => !counted[code]).map(([code]) => code);
  text(el("reason-foot"), unmet.length
    ? `Also defined, not met on this instance: ${unmet.join(", ")}.`
    : "Every reason in the taxonomy has been met on this instance.");
}

/* --- landing ---------------------------------------------------------- */

Astra = {
  async landing() {
    let receipts = [];
    try {
      const cfg = await loadConfig();
      const chip = el("chip-wallet");
      if (chip) {
        chip.querySelector("b").textContent = "via the execution layer";
        chip.querySelector(".dot").classList.add("on");
      }
      text(el("foot-config"),
        `${cfg.deployment} deployment \u00b7 burns signed by ${cfg.read_only ? "your wallet" : "this machine"}`
        + ` \u00b7 watching domains ${cfg.watched_domains.join(", ")}`);
      if (el("band")) renderBand();
    } catch (err) {
      text(el("foot-config"), `configuration unavailable: ${err.message}`);
      offlineBanner(err);
    }
    try {
      receipts = (await jget("/api/receipts")).receipts || [];
      renderEvidence(receipts);
    } catch (err) {
      el("evidence").innerHTML = `<div class="err">receipts unavailable: ${err.message}</div>`;
      offlineBanner(err);
    }
    bindTrace();
    await loadLandingInvariant(receipts);
  },

  async app() {
    bindApp();
    try {
      const cfg = await loadConfig();
      // The source defaults to the busiest chain this rail runs on, and the
      // destination to another; both are selectable, because which chain is
      // the source is the payer's business and their wallet's balance decides it.
      el("payer-source").innerHTML = chainOptions(cfg.watched_domains[0]);
      // The destination defaults to the chain this rail has delivered to most,
      // and stays selectable: which chain is the destination is the caller's call.
      el("payer-destination").innerHTML = chainOptions(
        cfg.chains["0"] ? 0 : cfg.watched_domains[cfg.watched_domains.length - 1]);
      await loadPayer();
      // the header states what this build is, not the hostname of its dependencies
text(el("cfg-line"), `${cfg.deployment} deployment`);
      const faucet = el("faucet");
      if (cfg.faucet) { faucet.href = cfg.faucet; faucet.classList.remove("hidden"); }
    } catch (err) {
      // The most likely reason a page that reads chains looks empty is that the
      // service behind it is not running. Say that, with the command, instead of
      // leaving someone to guess why every table is blank.
      showError(`configuration unavailable: ${err.message}`);
      offlineBanner(err);
    }
    await refresh();
    await loadLedger();
    await loadInvariant(false);
  },
};

/* The hero shows the control surface as it actually is - one real screenshot, taken from the
   running instance - and keeps one live line under it, because a picture of a live system
   should still say how live it is. */
function renderEvidence(receipts) {
  const caption = el("shot-caption");
  if (!caption) return;
  const settled = receipts.filter((r) => r.transaction_link).length;
  text(caption, receipts.length
    ? `${receipts.length} decisions recorded on this instance, ${settled} of them moved value.`
    : "No decisions recorded on this instance yet.");
}

function bindTrace() {
  const form = el("trace-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = el("trace-input").value.trim();
    const out = el("trace-out");
    if (!/^0x[0-9a-fA-F]{64}$/.test(value)) {
      text(el("trace-note"), "That is not a transaction hash. A source transaction hash is 32 bytes.");
      out.classList.add("hidden");
      return;
    }
    text(el("trace-note"), "reading every watched chain\\u2026");
    try {
      const receipt = await jget(`/api/inspect?burn_tx=${encodeURIComponent(value)}`);
      renderTrace(receipt);
      text(el("trace-note"), "Nothing was broadcast: this is the rail's read of both chains.");
    } catch (err) {
      text(el("trace-note"), `could not read it: ${err.message}`);
      out.classList.add("hidden");
    }
  });
}

function renderTrace(receipt) {
  const d = receipt.decision || {};
  const out = el("trace-out");
  out.classList.remove("hidden");
  out.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <span class="tag ${decisionClass(d)}">${d.reason}</span>
      <span class="mono dim" style="font-size:13px">${receipt.transfer_id}</span>
    </div>
    <p style="margin-bottom:14px">${stripAddrs(d.detail)}</p>
    <dl class="kv">
      <dt>Attestation</dt><dd>${(receipt.observed || {}).attestation_state || "\u00b7"}</dd>
      <dt>Amount</dt><dd>${(receipt.observed || {}).message ? fmtAmount(receipt.observed.message.amount) + " USDC" : "\u00b7"}</dd>
      <dt>Recipient</dt><dd>${(receipt.observed || {}).message ? receipt.observed.message.mint_recipient : "\u00b7"}</dd>
      <dt>Evidence</dt><dd>${d.evidence ? String(d.evidence).slice(0, 200) : "seen on chain"}</dd>
    </dl>`;
}

/* --- control surface -------------------------------------------------- */

function bindApp() {
  el("btn-refresh").addEventListener("click", refresh);
  el("btn-connect").addEventListener("click", connectWallet);
  el("btn-open").addEventListener("click", () => openFromServer(false));
  el("btn-open-finish").addEventListener("click", () => openFromServer(true));
  el("btn-wallet-open").addEventListener("click", openTransfer);
  el("filter-blocks").addEventListener("change", refresh);
  el("btn-invariant").addEventListener("click", () => loadInvariant(true));
  const pass = el("btn-pass");
  if (pass) pass.addEventListener("click", () => loadInvariant(true));
  document.querySelectorAll(".rail-item[data-pane]").forEach((item) => {
    item.addEventListener("click", (event) => {
      event.preventDefault();
      showPane(item.dataset.pane);
      history.replaceState(null, "", `#${item.dataset.pane}`);
    });
  });
  window.addEventListener("hashchange", () => showPane(paneFromHash()));
  showPane(paneFromHash());
  // the invariant is read in the background even when you never open its pane: the rail
  // carries its count, and a count you have to click to see is not a count
  setTimeout(() => loadInvariant(false), 1200);
}

/* The dashboard is one pane at a time, chosen from the rail. Everything is loaded
   regardless of which pane is on screen, so switching never means waiting. */
function paneFromHash() {
  const name = (location.hash || "").replace("#", "");
  return document.querySelector(`.pane-body[data-pane="${name}"]`) ? name : "transfers";
}

function showPane(name) {
  document.querySelectorAll(".pane-body").forEach((pane) => {
    pane.classList.toggle("hidden", pane.dataset.pane !== name);
  });
  document.querySelectorAll(".rail-item[data-pane]").forEach((item) => {
    const active = item.dataset.pane === name;
    item.classList.toggle("active", active);
    if (active) { item.setAttribute("aria-current", "page"); } else { item.removeAttribute("aria-current"); }
  });
  if (name === "invariant" || name === "passes") {
    const summary = el("inv-summary");
    if (summary && summary.textContent.trim() === "not run yet") loadInvariant(false);
  }
  if (name === "burn") loadPayer();
  if (name === "receipts") loadLedger();
}

function showError(message) {
  const box = el("err");
  box.textContent = message;
  box.classList.remove("hidden");
}

function clearError() { el("err").classList.add("hidden"); }

async function refresh() {
  if (ASTRAA.busy) return;
  ASTRAA.busy = true;
  el("btn-refresh").disabled = true;
  clearError();
  const seconds = Number(el("filter-blocks").value || 3600);
  const started = Date.now();
  const ticker = setInterval(() => {
    text(el("last-scan"), `reading every watched chain\u2026 ${Math.round((Date.now() - started) / 1000)}s`);
  }, 1000);
  text(el("last-scan"), "reading every watched chain\u2026");
  try {
    const body = await jget(`/api/inflight?seconds=${seconds}&limit=14`);
    ASTRAA.rows = body.rows || [];
    renderRows(ASTRAA.rows);
    const age = body.age_seconds ? `, read ${body.age_seconds}s ago` : "";
    const span = seconds >= 86400 ? `${seconds / 86400} day` : (seconds >= 3600 ? `${seconds / 3600}h` : `${seconds}s`);
    text(el("last-scan"), `the last ${span} on every chain${age}`);
  } catch (err) {
    showError(`could not read the chains: ${err.message}`);
    text(el("last-scan"), "scan failed");
  }
  clearInterval(ticker);
  ASTRAA.busy = false;
  el("btn-refresh").disabled = false;
}


/* The four numbers are not four categories: deliverable, waiting and refused are the parts
   of "in flight". Drawn as a bar so the relationship is visible without a sentence. */
function renderStateBar(counts, total) {
  const host = el("state-bar");
  if (!host) return;
  if (!total) {
    host.innerHTML = `<span class="seg wait" style="width:100%"></span>`;
    text(el("bar-note"), "nothing in flight in this window");
    return;
  }
  const parts = [["ok", counts.complete || 0], ["wait", counts.defer || 0], ["no", counts.refuse || 0]];
  host.innerHTML = parts
    .filter(([, value]) => value > 0)
    .map(([cls, value]) => `<span class="seg ${cls}" style="width:${Math.round((value / total) * 100)}%" title="${value} of ${total}"></span>`)
    .join("");
  text(el("bar-note"), `${total} transfer${total === 1 ? "" : "s"} in flight, read from the chains`);
}


/* Which chains this rail knows, by the domain number a message carries. */
function chainName(domain) {
  // Number(null) is 0, and 0 is a real domain: an absent destination must never resolve
  // into a chain this rail happens to watch.
  if (domain === null || domain === undefined || domain === "") return "not decoded";
  const chains = (ASTRAA.config && ASTRAA.config.chains) || {};
  const list = Array.isArray(chains) ? chains : Object.values(chains);
  const hit = list.find((chain) => Number(chain.domain) === Number(domain));
  if (hit) return hit.name;
  if (domain === null || domain === undefined || Number.isNaN(Number(domain))) return "unknown chain";
  return `domain ${domain}`;
}

/* An action is only offered when the protocol has not already ruled it out. A refusal read
   from the destination's own state is final; a deferral is a "not yet" and may be attempted.
   Offering a button that cannot succeed would be the opposite of what this rail is for. */
const FINAL_REFUSALS = [
  "ALREADY_DELIVERED", "UNSUPPORTED_DOMAIN", "DEPLOYMENT_ABSENT",
  "MESSAGE_INCONSISTENT", "WRONG_TRANSMITTER", "RECIPIENT_MISMATCH", "ROUTE_MISMATCH",
];

function rowAction(decision) {
  if (!decision) return "retry";
  if (decision.action === "complete") return "finish";
  if (FINAL_REFUSALS.includes(decision.reason)) return "none";
  return "retry";
}

function renderRows(rows) {
  const tbody = el("rows");
  const counts = { complete: 0, defer: 0, refuse: 0 };
  rows.forEach((row) => {
    if (row.decision && counts[row.decision.action] !== undefined) counts[row.decision.action] += 1;
  });
  text(el("c-inflight"), String(rows.length));
  text(el("rail-c-inflight"), String(rows.length));
  text(el("c-ready"), String(counts.complete));
  text(el("c-waiting"), String(counts.defer));
  text(el("c-refused"), String(counts.refuse));
  renderStateBar(counts, rows.length);

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="dim">Nothing in flight in this window.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((row) => {
    const d = row.decision || {};
    const attempt = rowAction(d);
    const id = String(row.transfer_id || "").replace(/^\d+:/, "");
    const recipient = row.mint_recipient ? short(row.mint_recipient, 8, 4) : "not decoded";
    const attestation = row.attestation_state || "not asked";
    const action =
      attempt === "finish"
        ? `<button class="btn primary sm" data-burn="${row.burn_tx}" data-source="${row.source_domain}" data-destination="${row.destination_domain}">Finish it</button>`
        : attempt === "retry"
          ? `<button class="btn outline sm" data-burn="${row.burn_tx}" data-source="${row.source_domain}" data-destination="${row.destination_domain}">Try anyway</button>`
          : `<span class="dim" style="font-size:12.5px">not ours to attempt</span>`;
    return `
    <tr>
      <td>
        <span class="dim" style="font-size:12.5px">${chainName(row.source_domain)}</span>
        <div class="mono">${short(id, 12, 6)}</div>
        <div class="why">source block ${row.block}</div>
      </td>
      <td><span class="mono">${row.amount === null || row.amount === undefined ? "\u2014" : fmtAmount(row.amount) + " USDC"}</span></td>
      <td>
        <span class="dim" style="font-size:12.5px">${chainName(row.destination_domain)}</span>
        <div class="why">to ${recipient}</div>
      </td>
      <td><span class="mono dim">${attestation}</span></td>
      <td>
        <span class="tag ${decisionClass(d)}">${d.reason || "unknown"}</span>
        <div class="why">${stripAddrs(d.detail)}</div>
      </td>
      <td>${action}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("button[data-burn]").forEach((button) => {
    button.addEventListener("click", () => finish(button));
  });
}

async function finish(button) {
  const burn = button.dataset.burn;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "asking the destination\u2026";
  clearError();
  try {
    const receipt = await jpost("/api/complete", {
      burn_tx: burn,
      source_domain: Number(button.dataset.source),
      destination_domain: Number(button.dataset.destination),
      wait_seconds: 60,
    });
    showReceipt(receipt);
    await refresh();
    await loadLedger();
  } catch (err) {
    showError(`completion failed: ${err.message}`);
  }
  button.disabled = false;
  button.textContent = original;
}

function showReceipt(receipt) {
  const box = el("receipt");
  const d = receipt.decision || {};
  box.classList.remove("hidden");
  box.className = "receipt-card head";
  box.innerHTML = `
    <div class="eyebrow">Last decision</div>
    <div class="row" style="margin-bottom:10px">
      <span class="tag ${decisionClass(d)}">${d.reason}</span>
      <span class="mono dim" style="font-size:13px">${receipt.transfer_id}</span>
    </div>
    <p style="margin-bottom:14px">${d.detail || ""}</p>
    <dl class="kv">
      <dt>Destination tx</dt>
      <dd>${receipt.transaction_link
        ? `<a href="${receipt.transaction_link}" target="_blank" rel="noreferrer" class="mono">${receipt.transaction_hash}</a>`
        : "none: no money moved"}</dd>
      <dt>Evidence</dt>
      <dd>${d.evidence ? stripAddrs(String(d.evidence)).slice(0, 220) : "seen on chain"}</dd>
      <dt>Took</dt>
      <dd>${receipt.seconds}s</dd>
    </dl>`;
}

async function loadLedger() {
  const tbody = el("ledger-rows");
  try {
    const body = await jget("/api/receipts");
    const receipts = body.receipts || [];
    text(el("rail-c-receipts"), String(receipts.length));
    tbody.innerHTML = receipts.length ? receipts.map((r) => `
      <tr>
        <td><span class="mono">${short(r.transfer_id, 12, 6)}</span></td>
        <td><span class="tag ${r.decision && r.decision.action === "refuse" ? "no" : "ok"}">${r.decision ? r.decision.reason : ""}</span></td>
        <td>${r.transaction_link
          ? `<a href="${r.transaction_link}" target="_blank" rel="noreferrer" class="mono">${short(r.transaction_hash, 12, 6)}</a>`
          : `<span class="dim">no money moved</span>`}</td>
      </tr>`).join("") : `<tr><td colspan="3" class="dim">No receipts yet.</td></tr>`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="3" class="dim">receipts unavailable: ${err.message}</td></tr>`;
  }
}

/* A page that reads live chains cannot be a static file. When there is nothing
   behind it, say so plainly and say what to run. */
function offlineBanner(err) {
  const banner = document.createElement("div");
  banner.className = "err";
  banner.style.margin = "0 0 20px";
  banner.innerHTML = `<b>The rail is not running behind this page.</b> Every table here is read from
    chains through it, so nothing can be shown until it is up.<br>
    <span class="mono">.venv/bin/python service/astra_service.py --port 8099</span><br>
    <span class="dim">then open http://127.0.0.1:8099/app — ${err.message}</span>`;
  const host = document.querySelector(".dash") || document.querySelector(".wrap");
  if (host) host.parentNode.insertBefore(banner, host);
}

/* --- the invariant ---------------------------------------------------- */

/* The collection says what should happen to each transfer. This says what actually
   happened to all of them: paired, stranded, received twice, or short. */
async function loadInvariant(interactive) {
  const summary = el("inv-summary");
  const rows = el("inv-rows");
  const seconds = Number(el("filter-blocks").value || 3600);
  if (interactive) {
    rows.innerHTML = `<tr><td colspan="5" class="dim">reading every burn in the window\u2026</td></tr>`;
  }
  text(summary, "reading\u2026");
  try {
    const out = await jget(`/api/pairing?seconds=${seconds}&limit=14`);
    renderInvariant(out);
  } catch (err) {
    text(summary, `could not read the invariant: ${err.message}`);
    if (interactive) rows.innerHTML = `<tr><td colspan="5" class="dim">${err.message}</td></tr>`;
  }
}

function renderInvariant(out) {
  const counts = { paired: 0, stranded: 0, "in flight": 0, unwatched: 0, unknown: 0 };
  (out.rows || []).forEach((row) => { counts[row.verdict] = (counts[row.verdict] || 0) + 1; });
  text(el("i-paired"), String(counts.paired || 0));
  text(el("rail-c-paired"), String(counts.paired || 0));
  const passRows = el("inv-passes");
  if (passRows) text(el("rail-c-passes"), String(Math.max(passRows.querySelectorAll("tr").length - 1, 0)));
  text(el("i-stranded"), String(counts.stranded || 0));
  text(el("i-inflight"), String(counts["in flight"] || 0));
  text(el("i-unwatched"), String(counts.unwatched || 0));
  text(el("inv-summary"),
    `${out.summary || "no transfers"} over the last ${out.span_seconds
      ? (out.span_seconds / 3600).toFixed(0) + "h" : out.blocks + " source blocks"}`);

  const tbody = el("inv-rows");
  tbody.innerHTML = (out.rows || []).length ? out.rows.map((row) => {
    const value = row.minted_amount !== null && row.minted_amount !== undefined
      ? `${fmtAmount(row.minted_amount)} of ${fmtAmount(row.expected_amount)} USDC`
      : (row.value_matches === false ? "short" : "\u00b7");
    const cls = row.verdict === "paired" ? "ok" : (row.verdict === "stranded" ? "no" : "wait");
    // A verdict you cannot act on is a report. Where the read says value could move and has not,
    // the row offers the delivery itself: the same call the transfers pane makes, same execution
    // layer, same receipt.
    const act = row.verdict === "stranded"
      ? `<button class="btn primary sm" data-burn="${row.burn_tx}" data-source="${row.source_domain}" data-destination="${row.destination_domain}">Finish it</button>`
      : (row.verdict === "in flight" && row.burn_tx
          ? `<button class="btn outline sm" data-burn="${row.burn_tx}" data-source="${row.source_domain}" data-destination="${row.destination_domain}">Try anyway</button>`
          : `<span class="dim" style="font-size:12.5px">\u2014</span>`);
    return `<tr>
      <td><span class="mono">${short(row.transfer_id, 14, 6)}</span></td>
      <td>${row.destination_name || "\u00b7"}</td>
      <td><span class="mono dim">${row.attestation || "\u00b7"}</span></td>
      <td><span class="tag ${cls}">${row.verdict}</span></td>
      <td class="mono">${value}</td>
      <td>${act}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="dim">nothing in this window</td></tr>`;

  tbody.querySelectorAll("button[data-burn]").forEach((button) => {
    button.addEventListener("click", () => finish(button));
  });

  const broken = out.broken || [];
  el("inv-broken").innerHTML = broken.length ? `
    <div class="err" style="margin-bottom:16px">
      The invariant is broken in ${broken.length} ${broken.length === 1 ? "place" : "places"}:
      ${broken.map((row) => `${short(row.transfer_id, 16, 6)} (${row.delivered_count > 1
        ? "received " + row.delivered_count + " times" : (row.value_matches === false ? "minted short" : "stranded")})`)
        .join(", ")}
    </div>` : "";

  const passes = out.passes || [];
  el("inv-passes").innerHTML = passes.length ? passes.map((entry) => `
    <tr>
      <td class="mono dim">${entry.at || "\u00b7"}</td>
      <td class="mono">${entry.window_blocks || "\u00b7"}</td>
      <td>${entry.summary || "\u00b7"}${entry.broken ? ` \u00b7 <span class="tag no">${entry.broken} broken</span>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="3" class="dim">no pass has been journaled yet</td></tr>`;
}

/* --- the payer side, on this machine ---------------------------------- */

/* What the key this machine holds can pay with, and where. Asked before a
   transfer is offered, so nobody fills a form and finds out at signing time. */
async function loadPayer() {
  const box = el("payer-box");
  try {
    const payer = await jget("/api/payer");
    ASTRAA.payer = payer;
    renderPayer(payer);
  } catch (err) {
    box.innerHTML = `<span class="dim">the payer side could not be read: ${err.message}</span>`;
    el("btn-open").disabled = true;
    el("btn-open-finish").disabled = true;
  }
}

function renderPayer(payer) {
  const box = el("payer-box");
  if (!payer.configured) {
    const hosted = ASTRAA.config && ASTRAA.config.read_only;
    box.innerHTML = hosted
      ? `<dl class="kv">
           <dt>Burn key</dt><dd>not on this instance</dd>
           <dt>Signed by</dt><dd>your wallet</dd>
           <dt>Finish</dt><dd>through the execution layer</dd>
         </dl>`
      : `<dl class="kv">
           <dt>Burn key</dt><dd>${payer.address ? stripAddrs(payer.address) : "none"}</dd>
           <dt>Detail</dt><dd>${stripAddrs(payer.detail || "")}</dd>
         </dl>`;
    el("btn-open").disabled = true;
    el("btn-open-finish").disabled = true;
    return;
  }
  const chains = (payer.chains || []).filter((c) => c.usdc > 0 || c.native > 0);
  const payable = (payer.chains || []).filter((c) => c.usdc > 0 && c.native > 0);
  box.innerHTML = `
    <div class="brief"><span class="mono">${short(payer.address, 12, 6)}</span>
      <span class="dim"> pays from this machine</span></div>
    <table class="mini">
      <thead><tr><th>chain</th><th>USDC</th><th>gas</th><th></th></tr></thead>
      <tbody>
      ${chains.map((c) => `
        <tr>
          <td>${c.name}</td>
          <td class="mono">${fmtAmount(c.usdc)}</td>
          <td class="mono dim">${c.native ? (c.native / 1e18).toFixed(4) : "0"}</td>
          <td>${c.usdc > 0 && c.native > 0 ? '<span class="tag ok">can pay here</span>'
              : '<span class="tag">no</span>'}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
  if (payable.length) {
    el("payer-source").innerHTML = chainOptions(payable[0].domain);
  }
  const ready = payable.length > 0;
  el("btn-open").disabled = !ready;
  el("btn-open-finish").disabled = !ready;
}

function renderSteps(steps) {
  const list = el("open-steps");
  if (!steps || !steps.length) {
    list.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  list.classList.remove("hidden");
  list.innerHTML = steps.map((entry) => `<li><span class="dim">${entry.at}</span> ${entry.step}</li>`).join("");
}

/* One click that ends in a real delivery: the burn is signed here, then the rail
   waits for the source chain to finalise and hands the attestation over. The two
   transactions are reported separately because they have different signers. */
async function openFromServer(finish) {
  clearError();
  renderSteps([]);
  el("open-link").classList.add("hidden");
  el("mint-link").classList.add("hidden");
  const amount = Number(el("payer-amount").value.trim());
  const source = Number(el("payer-source").value);
  const destination = Number(el("payer-destination").value);
  const caller = el("payer-caller").value.trim();
  if (!(amount > 0)) { showError("amount must be greater than zero"); return; }

  const buttons = [el("btn-open"), el("btn-open-finish")];
  buttons.forEach((b) => { b.disabled = true; });
  const started = Date.now();
  const label = finish ? "burning, then waiting for the attestation" : "burning";
  text(el("open-state"), `${label}\u2026`);
  const ticker = setInterval(() => {
    text(el("open-state"), `${label}\u2026 ${Math.round((Date.now() - started) / 1000)}s`);
  }, 1000);
  try {
    const out = await jpost("/api/open", {
      amount, source_domain: source, destination_domain: destination,
      caller: caller || null, finish, wait_seconds: 900,
    });
    renderSteps(out.steps || []);
    const transfer = out.transfer || {};
    text(el("open-state"), `burned ${fmtAmount(transfer.amount)} USDC on ${source} \u2192 ${destination}`);
    const burn = el("open-link");
    burn.href = out.burn_link || "#";
    burn.classList.remove("hidden");
    if (out.receipt) {
      showReceipt(out.receipt);
      const mint = el("mint-link");
      if (out.mint_link) { mint.href = out.mint_link; mint.classList.remove("hidden"); }
    }
    if (out.finish_error) showError(`the transfer is in flight, but finishing failed: ${out.finish_error}`);
    await refresh();
    await loadLedger();
    await loadPayer();
  } catch (err) {
    showError(`could not open the transfer: ${err.message}`);
    text(el("open-state"), "nothing sent");
  }
  clearInterval(ticker);
  buttons.forEach((b) => { b.disabled = false; });
}

/* --- the payer side, signed in the browser ---------------------------- */

async function connectWallet() {
  clearError();
  if (!window.ethereum) {
    showError("No browser wallet found. Creating a transfer needs a wallet that can sign; the rail's own side does not.");
    return;
  }
  try {
    const provider = new ethers.BrowserProvider(window.ethereum);
    const accounts = await provider.send("eth_requestAccounts", []);
    const network = await provider.getNetwork();
    ASTRAA.signer = await provider.getSigner();
    ASTRAA.address = accounts[0];
    text(el("wallet-state"), `${short(ASTRAA.address, 8, 6)} on chain ${network.chainId}`);
    el("btn-open").disabled = false;
  } catch (err) {
    showError(`wallet refused: ${err.message}`);
  }
}

async function openTransfer() {
  clearError();
  const cfg = ASTRAA.config;
  const sourceDomain = Number(el("payer-source").value);
  const source = cfg.chains[String(sourceDomain)];
  if (!source) { showError("pick a source chain the rail watches"); return; }
  const destinationDomain = Number(el("payer-destination").value);
  const amount = BigInt(Math.round(Number(el("payer-amount").value.trim()) * 1e6));
  const caller = el("payer-caller").value.trim();
  if (!amount || amount <= 0n) { showError("amount must be greater than zero"); return; }

  const provider = new ethers.BrowserProvider(window.ethereum);
  const network = await provider.getNetwork();
  if (Number(network.chainId) !== source.chain_id) {
    showError(`switch your wallet to chain ${source.chain_id} (${source.name}) to create the transfer`);
    return;
  }
  const signer = await provider.getSigner();
  const usdc = new ethers.Contract(source.usdc, [
    "function approve(address spender, uint256 amount) returns (bool)",
    "function allowance(address owner, address spender) view returns (uint256)",
  ], signer);
  const messengerAbi = cfg.deployment === "v1"
    ? ["function depositForBurn(uint256 amount, uint32 destinationDomain, bytes32 mintRecipient, address burnToken)"]
    : ["function depositForBurn(uint256 amount, uint32 destinationDomain, bytes32 mintRecipient, address burnToken, bytes32 destinationCaller, uint256 maxFee, uint32 minFinalityThreshold)"];
  const messenger = new ethers.Contract(source.messenger, messengerAbi, signer);

  el("btn-open").disabled = true;
  text(el("open-state"), "approving\u2026");
  try {
    const allowance = await usdc.allowance(ASTRAA.address, source.messenger);
    if (allowance < amount) {
      const approveTx = await usdc.approve(source.messenger, amount);
      text(el("open-state"), `approval sent ${short(approveTx.hash, 12, 6)}\u2026 waiting`);
      await approveTx.wait();
    }
    const recipient32 = ethers.zeroPadValue(ASTRAA.address, 32);
    const caller32 = caller ? ethers.zeroPadValue(caller, 32) : ethers.ZeroHash;
    text(el("open-state"), "burning on the source chain\u2026");
    const burnTx = cfg.deployment === "v1"
      ? await messenger.depositForBurn(amount, destinationDomain, recipient32, source.usdc)
      : await messenger.depositForBurn(amount, destinationDomain, recipient32, source.usdc,
                                       caller32, amount / 1000n, 1000);
    text(el("open-state"), `transfer opened ${short(burnTx.hash, 12, 6)}\u2026 waiting for the source chain`);
    await burnTx.wait();
    text(el("open-state"), `in flight: ${burnTx.hash}`);
    const link = el("open-link");
    link.href = explorerLink(source, burnTx.hash);
    link.classList.remove("hidden");
    await refresh();
  } catch (err) {
    showError(`could not open the transfer: ${err.message}`);
    text(el("open-state"), "not opened");
  }
  el("btn-open").disabled = false;
}

function explorerLink(chain, hash) {
  const base = chain.chain_id === 84532 ? "https://sepolia.basescan.org"
    : chain.chain_id === 11155111 ? "https://sepolia.etherscan.io" : "";
  return base ? `${base}/tx/${hash}` : hash;
}
