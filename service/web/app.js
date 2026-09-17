/* ASTRA browser surface.
   Everything the page shows comes from the service: configuration, the live
   in-flight list, the receipts. The only thing this file hardcodes is an
   approval amount of zero, which is not a value, it is the absence of one. */

const ASTRAA = {
  config: null,
  rows: [],
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

function short(hex, head = 10, tail = 6) {
  if (!hex) return "\u00b7";
  const s = String(hex);
  return s.length <= head + tail + 2 ? s : `${s.slice(0, head)}\u2026${s.slice(-tail)}`;
}

function fmtAmount(units, decimals = 6) {
  if (units === null || units === undefined) return "\u00b7";
  return (Number(units) / 10 ** decimals).toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0");
}

function decisionClass(decision) {
  if (!decision) return "";
  if (decision.action === "complete") return "t";
  if (decision.action === "defer") return "w";
  return "r";
}

async function loadConfig() {
  const cfg = await jget("/api/config");
  ASTRAA.config = cfg;
  return cfg;
}

function chainOptions(selected) {
  const chains = Object.values(ASTRAA.config.chains)
    .sort((a, b) => a.domain - b.domain);
  return chains.map((chain) => {
    const sel = String(chain.domain) === String(selected) ? " selected" : "";
    return `<option value="${chain.domain}"${sel}>${chain.name} (domain ${chain.domain})</option>`;
  }).join("");
}

/* --- landing ---------------------------------------------------------- */

Astra = {
  async landing() {
    try {
      const cfg = await loadConfig();
      const wallet = await jget("/api/inflight?blocks=12&limit=1").catch(() => null);
      text(el("foot-config"),
        `${cfg.deployment} deployment \u00b7 domains ${cfg.watched_domains.join(", ")} \u00b7 executor via execution layer`);
      const chains = Object.values(cfg.chains).length;
      text(el("chip-chains"), `${chains} chains watched`);
      if (el("chip-chains").querySelector("b")) {
        el("chip-chains").querySelector("b").textContent = `${chains} chains watched`;
        el("chip-chains").querySelector("b").previousSibling.classList.add("on");
      }
      if (wallet && wallet.rows) {
        el("chip-inflight").querySelector("b").textContent = String(wallet.rows.length);
      }
    } catch (err) {
      text(el("foot-config"), `configuration unavailable: ${err.message}`);
    }
    try {
      const receipts = await jget("/api/receipts");
      renderEvidence(receipts.receipts || []);
    } catch (err) {
      el("evidence").innerHTML = `<div class="err">receipts unavailable: ${err.message}</div>`;
    }
    const repo = el("link-repo");
    if (repo && repo.dataset.href) repo.href = repo.dataset.href;
  },

  async app() {
    bindApp();
    try {
      const cfg = await loadConfig();
      el("payer-destination").innerHTML = chainOptions(cfg.chains[6] ? 0 : cfg.watched_domains[0]);
      text(el("cfg-line"),
        `${cfg.deployment} deployment \u00b7 attestation via ${new URL(cfg.attestation_base).host} \u00b7 watching domains ${cfg.watched_domains.join(", ")}`);
      const faucet = el("faucet");
      if (cfg.faucet) { faucet.href = cfg.faucet; faucet.classList.remove("hidden"); }
    } catch (err) {
      showError(`configuration unavailable: ${err.message}`);
    }
    await refresh();
    await loadLedger();
  },
};

function renderEvidence(receipts) {
  const host = el("evidence");
  if (!receipts.length) {
    host.innerHTML = `<div class="note">No receipts on this deployment yet. The rail writes one every time it decides, including when it refuses.</div>`;
    return;
  }
  const done = receipts.filter((r) => r.transaction_link);
  host.innerHTML = `
    <table>
      <thead><tr><th style="width:210px">Transfer</th><th style="width:150px">Decision</th>
      <th>Destination transaction</th></tr></thead>
      <tbody>
      ${receipts.slice(0, 6).map((r) => `
        <tr>
          <td class="mono">${short(r.transfer_id, 14, 8)}</td>
          <td><span class="tag ${r.decision && r.decision.action === "refuse" ? "r" : "t"}">${r.decision ? r.decision.reason : "recorded"}</span></td>
          <td class="mono">${r.transaction_link
            ? `<a href="${r.transaction_link}" target="_blank" rel="noreferrer">${short(r.transaction_hash, 14, 8)}</a>`
            : `<span class="dim">no money moved</span>`}</td>
        </tr>`).join("")}
      </tbody>
    </table>
    <p class="note" style="margin-top:12px">${done.length} of ${receipts.length} recorded decisions moved value.</p>`;
}

/* --- control surface -------------------------------------------------- */

function bindApp() {
  el("btn-refresh").addEventListener("click", refresh);
  el("btn-connect").addEventListener("click", connectWallet);
  el("btn-open").addEventListener("click", openTransfer);
  el("filter-blocks").addEventListener("change", refresh);
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
  const blocks = Number(el("filter-blocks").value || 1200);
  const started = Date.now();
  const ticker = setInterval(() => {
    text(el("last-scan"), `reading both chains\u2026 ${Math.round((Date.now() - started) / 1000)}s`);
  }, 1000);
  text(el("last-scan"), "reading both chains\u2026");
  try {
    const body = await jget(`/api/inflight?blocks=${blocks}&limit=14`);
    ASTRAA.rows = body.rows || [];
    renderRows(ASTRAA.rows);
    const age = body.age_seconds ? `, read ${body.age_seconds}s ago` : "";
    text(el("last-scan"), `${ASTRAA.rows.length} in flight over the last ${blocks} source blocks${age}`);
  } catch (err) {
    showError(`could not read the chains: ${err.message}`);
    text(el("last-scan"), "scan failed");
  }
  clearInterval(ticker);
  ASTRAA.busy = false;
  el("btn-refresh").disabled = false;
}

function renderRows(rows) {
  const tbody = el("rows");
  const counts = { complete: 0, defer: 0, refuse: 0 };
  rows.forEach((row) => { if (row.decision && counts[row.decision.action] !== undefined) counts[row.decision.action] += 1; });
  text(el("c-inflight"), String(rows.length));
  text(el("c-ready"), String(counts.complete));
  text(el("c-waiting"), String(counts.defer));
  text(el("c-refused"), String(counts.refuse));

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="dim">Nothing is in flight in this window. Every transfer the source chain announced in it has already arrived.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((row) => {
    const d = row.decision || {};
    const canFinish = d.action === "complete";
    const finishLabel = d.action === "complete" ? "Finish it" : "Try anyway";
    return `
    <tr>
      <td class="mono">
        ${short(row.transfer_id, 12, 6)}
        <div class="why">source block ${row.block}</div>
      </td>
      <td class="mono">${row.amount === null || row.amount === undefined ? "\u00b7" : fmtAmount(row.amount) + " USDC"}<div class="why">${short(row.mint_recipient, 8, 4)}</div></td>
      <td class="mono">${row.destination_name}<div class="why">domain ${row.destination_domain}</div></td>
      <td class="mono">${row.attestation_state}</td>
      <td>
        <span class="tag ${decisionClass(d)}">${d.reason || "unknown"}</span>
        <div class="why">${d.detail || ""}</div>
      </td>
      <td>
        <button class="btn ${canFinish ? "primary" : "ghost"}" data-burn="${row.burn_tx}"
          data-source="${row.source_domain}" data-destination="${row.destination_domain}">${finishLabel}</button>
      </td>
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
  box.innerHTML = `
    <div class="kicker">last decision</div>
    <div class="row" style="margin-bottom:10px">
      <span class="tag ${decisionClass(d)}">${d.reason}</span>
      <span class="mono dim" style="font-size:12px">${receipt.transfer_id}</span>
    </div>
    <p style="margin-bottom:12px">${d.detail || ""}</p>
    <dl class="kv">
      <dt>destination tx</dt>
      <dd>${receipt.transaction_link
        ? `<a href="${receipt.transaction_link}" target="_blank" rel="noreferrer">${receipt.transaction_hash}</a>`
        : "none: no money moved"}</dd>
      <dt>evidence</dt>
      <dd>${d.evidence ? String(d.evidence).slice(0, 220) : "seen on chain"}</dd>
      <dt>took</dt>
      <dd>${receipt.seconds}s</dd>
    </dl>`;
}

async function loadLedger() {
  try {
    const body = await jget("/api/receipts");
    const receipts = body.receipts || [];
    el("ledger").innerHTML = receipts.length ? receipts.map((r) => `
      <tr>
        <td class="mono">${short(r.transfer_id, 12, 6)}</td>
        <td><span class="tag ${r.decision && r.decision.action === "refuse" ? "r" : "t"}">${r.decision ? r.decision.reason : ""}</span></td>
        <td class="mono">${r.transaction_link
          ? `<a href="${r.transaction_link}" target="_blank" rel="noreferrer">${short(r.transaction_hash, 12, 6)}</a>`
          : "<span class=\"dim\">no money moved</span>"}</td>
      </tr>`).join("") : `<tr><td colspan="3" class="dim">No decisions recorded yet.</td></tr>`;
  } catch (err) {
    el("ledger").innerHTML = `<tr><td colspan="3" class="dim">ledger unavailable: ${err.message}</td></tr>`;
  }
}

