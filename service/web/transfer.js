/* The transfer application: a real client for this rail.
 *
 * Everything here talks to the rail's own HTTP boundary. The page never holds a
 * key, never computes a balance, never decides that a delivery happened, and
 * never invents an identifier: the wallet signs, the chains answer, and the rail
 * decides. Where a step cannot be completed, it says which step and why.
 */
(function () {
  const A = (window.ASTRAA = window.ASTRAA || {});
  const el = (id) => document.getElementById(id);
  const text = (node, value) => { if (node) node.textContent = value; };
  const show = (node, on) => { if (node) node.classList.toggle("hidden", !on); };

  async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    if (!response.ok) {
      const message = (body && (body.error || (body.errors || [])[0])) || `HTTP ${response.status}`;
      const err = new Error(message);
      err.status = response.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  const post = (path, payload) => api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const short = (value, head = 8, tail = 6) =>
    typeof value === "string" && value.length > head + tail + 2
      ? `${value.slice(0, head)}\u2026${value.slice(-tail)}`
      : (value || "\u2014");

  const usdc = (units) =>
    units === null || units === undefined ? "\u2014" : (Number(units) / 1e6).toFixed(6);

  const link = (explorer, kind, value) =>
    explorer && value ? `${explorer}/${kind}/${value}` : null;

  function linkNode(url, label, className) {
    if (!url) return `<span class="dim">${label}</span>`;
    return `<a class="${className || "mono"}" href="${url}" target="_blank" rel="noreferrer">${label}</a>`;
  }

  /* ------------------------------------------------------------------ wallet */

  const Wallet = {
    state: { connected: false, address: null, chainId: null, domain: null, balances: null,
             wallet: null, error: null },

    available() { return typeof window.ethereum !== "undefined"; },

    async connect() {
      if (!this.available()) throw new Error("No browser wallet found. Install one to sign a burn.");
      if (typeof window.ethers === "undefined") throw new Error("The page's wallet library did not load.");
      const provider = new window.ethers.BrowserProvider(window.ethereum);
      const accounts = await provider.send("eth_requestAccounts", []);
      const signer = await provider.getSigner();
      const network = await provider.getNetwork();
      this.state = {
        connected: true,
        address: accounts[0],
        chainId: Number(network.chainId),
        domain: A.domainForChainId(Number(network.chainId)),
        balances: null,
        wallet: provider,
        signer,
        error: null,
      };
      this.watch();
      await this.refreshBalances();
      return this.state;
    },

    disconnect() {
      this.state = { connected: false, address: null, chainId: null, domain: null,
                     balances: null, wallet: null, signer: null, error: null };
      renderWallet();
    },

    /* A wallet can change accounts or networks behind the page's back. Listening is
       the difference between showing the connected wallet and showing a guess. */
    watch() {
      if (!this.available() || this._watching) return;
      this._watching = true;
      window.ethereum.on("accountsChanged", (accounts) => {
        if (!accounts || !accounts.length) { this.disconnect(); return; }
        this.state.address = accounts[0];
        this.refreshBalances().catch(() => {});
      });
      window.ethereum.on("chainChanged", async () => {
        if (!this.state.connected) return;
        const provider = new window.ethers.BrowserProvider(window.ethereum);
        const network = await provider.getNetwork();
        this.state.chainId = Number(network.chainId);
        this.state.domain = A.domainForChainId(this.state.chainId);
        this.state.wallet = provider;
        this.state.signer = await provider.getSigner();
        this.refreshBalances().catch(() => {});
      });
    },

    async refreshBalances() {
      if (!this.state.address) return null;
      const domain = this.state.domain
        ?? (el("tf-source") ? Number(el("tf-source").value) : null);
      if (domain === null || domain === undefined || Number.isNaN(domain)) return null;
      const body = await api(`/api/balance?address=${this.state.address}&domain=${domain}`);
      this.state.balances = body;
      return body;
    },

    /* Signing on the wrong chain is the one mistake a wallet application must not
       allow, so the chain is checked before every transaction, not after a revert. */
    async requireChain(chainId) {
      if (!this.state.connected) throw new Error("Connect a wallet first.");
      if (this.state.chainId !== chainId) {
        throw new Error(`Your wallet is on chain ${this.state.chainId}; this source chain is `
          + `${chainId}. Switch network and try again.`);
      }
    },

    async switchNetwork(chainId, chain) {
      if (!this.available()) throw new Error("No browser wallet found.");
      const hex = "0x" + Number(chainId).toString(16);
      try {
        await window.ethereum.request({ method: "wallet_switchEthereumChain",
                                        params: [{ chainId: hex }] });
      } catch (error) {
        if (error && (error.code === 4902 || error.code === -32603)) {
          await window.ethereum.request({ method: "wallet_addEthereumChain", params: [{
            chainId: hex,
            chainName: chain.name,
            nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
            rpcUrls: chain.rpc ? [chain.rpc] : [],
            blockExplorerUrls: chain.explorer ? [chain.explorer] : [],
          }] });
        } else {
          throw error;
        }
      }
      const provider = new window.ethers.BrowserProvider(window.ethereum);
      const network = await provider.getNetwork();
      this.state.chainId = Number(network.chainId);
      this.state.domain = A.domainForChainId(this.state.chainId);
      this.state.signer = await provider.getSigner();
      await this.refreshBalances();
    },
  };

  /* ------------------------------------------------------------------ render */

  function renderWallet() {
    const box = el("wallet-card");
    if (!box) return;
    const s = Wallet.state;
    if (!s.connected) {
      box.innerHTML = `
        <div class="kpis">
          <div class="kpi"><span class="k">wallet</span><b class="v">not connected</b></div>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn primary" id="btn-connect">Connect wallet</button>
          <span class="dim" id="wallet-note">${Wallet.available()
            ? "your wallet signs; this page never holds a key" : "no browser wallet detected"}</span>
        </div>
        <div class="row" style="margin-top:10px">
          <span class="note">Test networks only. Connect a wallet that holds nothing you would
            miss: this rail asks for Base, Ethereum, Optimism, Arbitrum and Polygon Sepolia, and
            no request is made until you click.</span>
        </div>`;
      el("btn-connect").addEventListener("click", () => connectWallet());
      return;
    }
    const b = s.balances || {};
    const chain = A.chainForDomain(s.domain);
    box.innerHTML = `
      <div class="kpis">
        <div class="kpi"><span class="k">wallet</span><b class="v mono" title="${s.address}">${short(s.address)}</b></div>
        <div class="kpi"><span class="k">network</span><b class="v">${chain ? chain.name : `chain ${s.chainId}`}</b></div>
        <div class="kpi"><span class="k">gas</span><b class="v">${b.native_balance !== undefined
          ? (b.native_balance / 1e18).toFixed(6) : "\u2014"} ETH</b></div>
        <div class="kpi"><span class="k">token</span><b class="v">${usdc(b.token_balance)} USDC</b></div>
        <div class="kpi"><span class="k">approved</span><b class="v">${usdc(b.allowance)} USDC</b></div>
      </div>
      <div class="row" style="margin-top:14px">
        ${chain && s.chainId !== chain.chain_id ? `<button class="btn primary sm" id="btn-switch">Switch to ${chain.name}</button>` : ""}
        <button class="btn outline sm" id="btn-refresh-wallet">Read balances</button>
        <button class="btn outline sm" id="btn-disconnect">Disconnect</button>
      </div>
      ${chain && s.chainId !== chain.chain_id ? `<p class="note" style="margin-top:10px">Your wallet is on chain ${s.chainId}. A burn on this route must be signed on ${chain.name} (chain ${chain.chain_id}).</p>` : ""}`;
    el("btn-disconnect").addEventListener("click", () => Wallet.disconnect());
    el("btn-refresh-wallet").addEventListener("click", () => Wallet.refreshBalances().then(renderWallet).catch(showError));
    const switcher = el("btn-switch");
    if (switcher) switcher.addEventListener("click", () =>
      Wallet.switchNetwork(chain.chain_id, chain).then(renderWallet).catch(showError));
  }

  function showError(error) {
    const box = el("tf-state");
    if (box) {
      box.className = "note bad";
      box.textContent = error && error.message ? error.message : String(error);
    }
    return error;
  }

  function note(message, cls) {
    const box = el("tf-state");
    if (box) {
      box.className = "note" + (cls ? ` ${cls}` : "");
      box.textContent = message;
    }
  }

  async function connectWallet() {
    try {
      note("asking your wallet\u2026");
      await Wallet.connect();
      renderWallet();
      note("connected: " + short(Wallet.state.address));
      await prepare();
    } catch (error) {
      showError(error);
      renderWallet();
    }
  }

  /* ------------------------------------------------------------------ routes */

  function fillRoutes() {
    const chains = A.chains();
    const source = el("tf-source");
    const destination = el("tf-destination");
    if (!source || !destination) return;
    const options = Object.values(chains).sort((a, b) => a.domain - b.domain);
    source.innerHTML = options.map((c) =>
      `<option value="${c.domain}" data-chain="${c.chain_id}">${c.name}</option>`).join("");
    destination.innerHTML = options.map((c) =>
      `<option value="${c.domain}">${c.name}</option>`).join("");
    if (chains["6"]) source.value = "6";
    destination.value = "0";
    const routes = A.config.routes || [];
    const payable = options.filter((c) =>
      routes.some((r) => Number(r.source_domain) === Number(c.domain) && r.ok));
    if (payable.length && !payable.some((c) => Number(c.domain) === Number(source.value))) {
      source.value = String(payable[0].domain);
    }
    fillDestinations();
  }

  /* Only routes the rail can actually deliver on, and a route that cannot be
     delivered is shown as the reason rather than hidden. */
  function fillDestinations() {
    const destination = el("tf-destination");
    if (!destination) return;
    const source = Number(el("tf-source").value);
    const options = Object.values(A.chains()).sort((a, b) => a.domain - b.domain)
      .filter((c) => Number(c.domain) !== source);
    destination.innerHTML = options.map((c) => {
      const route = (A.config.routes || []).find((r) => Number(r.source_domain) === source
        && Number(r.destination_domain) === Number(c.domain));
      const ok = route ? route.ok : false;
      return `<option value="${c.domain}"${ok ? "" : " data-blocked=\"1\""}>${c.name}`
        + (ok ? "" : " \u2014 not deliverable") + `</option>`;
    }).join("");
    const first = options.find((c) => {
      const route = (A.config.routes || []).find((r) => Number(r.source_domain) === source
        && Number(r.destination_domain) === Number(c.domain));
      return route && route.ok;
    });
    if (first) destination.value = String(first.domain);
  }

  /* ------------------------------------------------------------------ prepare */

  let prepared = null;

  function readForm() {
    const recipient = (el("tf-recipient").value || "").trim()
      || (Wallet.state.address || "");
    return {
      source_domain: Number(el("tf-source").value),
      destination_domain: Number(el("tf-destination").value),
      amount_usdc: Number(el("tf-amount").value || 0),
      recipient,
      caller: (el("tf-caller").value || "").trim() || null,
      address: Wallet.state.address || null,
    };
  }

  function renderChecks(body) {
    const host = el("tf-checks");
    if (!host) return;
    host.innerHTML = (body.checks || []).map((c) => `
      <div class="check ${c.ok ? "ok" : "bad"}">
        <span class="mark">${c.ok ? "\u2713" : "\u2717"}</span>
        <span class="name">${c.check}</span>
        <span class="detail">${c.detail}</span>
      </div>`).join("");
  }

  // An empty field is not a bad address, and saying so is the difference between
  // a message a person can act on and one that reads like a validation bug.
  function this_missing(form) {
    return !(form.recipient || "").trim();
  }

  async function prepare() {
    const form = readForm();
    prepared = null;
    renderActions();
    if (!form.amount_usdc || form.amount_usdc <= 0) {
      note("enter an amount to see the checks this route makes");
      el("tf-checks").innerHTML = "";
      return null;
    }
    note("reading the chains\u2026");
    try {
      const body = await post("/api/transfer/prepare", form);
      prepared = body;
      renderChecks(body);
      const first = (body.errors || [])[0] || "the rail refused this transfer";
      note(body.ok
        ? `ready: ${form.amount_usdc} USDC ${A.nameOf(form.source_domain)} \u2192 ${A.nameOf(form.destination_domain)}`
        : `not ready: ${this_missing(form) ? "connect a wallet, or type the recipient address" : first}`,
        body.ok ? "good" : "bad");
      renderActions();
      return body;
    } catch (error) {
      const body = error.body;
      if (body) {
        prepared = body;
        renderChecks(body);
        note(`not ready: ${(body.errors || [])[0] || error.message}`, "bad");
      } else {
        showError(error);
      }
      renderActions();
      return null;
    }
  }

  function renderActions() {
    const approve = el("btn-approve");
    const burn = el("btn-burn");
    if (!approve || !burn) return;
    const blocked = el("tf-destination").selectedOptions[0]?.dataset.blocked === "1";
    const ready = Boolean(prepared && prepared.ok && Wallet.state.connected && !blocked);
    approve.disabled = !(ready && prepared.needs_approval);
    burn.disabled = !ready || (prepared && prepared.needs_approval);
    const reason = !Wallet.state.connected ? "connect a wallet to sign"
      : blocked ? "this destination is not deliverable from the rail"
      : (prepared && !prepared.ok ? "the checks above must pass first"
      : (prepared && prepared.needs_approval ? "approve the token, then the burn unlocks" : ""));
    text(el("tf-action-note"), reason);
  }

  /* ------------------------------------------------------------------ approve */

  async function approve() {
    try {
      if (!prepared || !prepared.approve) throw new Error("prepare the transfer first");
      await Wallet.requireChain(prepared.source.chain_id);
      const signer = Wallet.state.signer;
      const contract = new window.ethers.Contract(prepared.approve.token,
        ["function approve(address spender, uint256 amount) returns (bool)"], signer);
      note("approve it in your wallet\u2026");
      const tx = await contract.approve(prepared.approve.spender, prepared.approve.amount);
      const url = link(A.explorerOf(prepared.source.domain), "tx", tx.hash);
      text(el("tf-approve-hash"), "");
      el("tf-approve-hash").innerHTML = linkNode(url, tx.hash, "mono");
      note("approval broadcast: waiting for its receipt\u2026");
      const receipt = await tx.wait();
      if (!receipt || receipt.status !== 1) {
        throw new Error(`the approval transaction reverted: ${tx.hash}`);
      }
      // The allowance is reread from the chain: the receipt proves a transaction
      // happened, not that the messenger may now move the tokens.
      const balance = await Wallet.refreshBalances();
      renderWallet();
      prepared.allowance = balance ? balance.allowance : null;
      prepared.needs_approval = Boolean(prepared.amount && (prepared.allowance || 0) < prepared.amount);
      renderActions();
      note(prepared.needs_approval
        ? "the chain still reports an insufficient allowance"
        : "approved: the burn is unlocked", prepared.needs_approval ? "bad" : "good");
    } catch (error) {
      showError(error);
      renderActions();
    }
  }

  /* ------------------------------------------------------------------ burn */

  async function burn() {
    const steps = el("tf-steps");
    steps.innerHTML = "";
    const step = (message, cls) => {
      const item = document.createElement("li");
      item.className = cls || "";
      item.textContent = message;
      steps.appendChild(item);
      return item;
    };
    try {
      const fresh = await post("/api/transfer/prepare", readForm());
      prepared = fresh;
      renderChecks(fresh);
      if (!fresh.ok) throw new Error((fresh.errors || [])[0] || "the rail refused this transfer");
      if (fresh.needs_approval) throw new Error("the allowance is still short; approve first");
      await Wallet.requireChain(fresh.source.chain_id);

      step(`signing on ${fresh.source.name} (chain ${fresh.source.chain_id})`);
      const signer = Wallet.state.signer;
      const messenger = new window.ethers.Contract(fresh.burn.messenger,
        [`function ${fresh.burn.function}`], signer);
      const args = (fresh.burn.arguments || []).map((value) =>
        /^\d+$/.test(String(value)) ? BigInt(value) : value);
      const tx = await messenger[fresh.burn.function.split("(")[0]](...args);
      step("burn broadcast", "good");

      const url = link(A.explorerOf(fresh.source.domain), "tx", tx.hash);
      el("tf-burn-hash").innerHTML = linkNode(url, tx.hash, "mono");
      text(el("tf-burn-hash-label"), "source transaction");
      const receipt = await tx.wait();
      if (!receipt || receipt.status !== 1) {
        throw new Error(`the burn transaction reverted: ${tx.hash}`);
      }
      step("source receipt: success", "good");

      // A successful transaction is not a transfer. The message the destination
      // will be handed has to be in the receipt, or there is nothing to deliver.
      const registration = await post("/api/transfer/register", {
        source_domain: fresh.source.domain,
        burn_tx: tx.hash,
        address: Wallet.state.address,
      });
      step(`transfer ${registration.transfer_id.slice(0, 18)}\u2026 registered`, "good");
      note("in flight: the rail is watching it now");
      await Wallet.refreshBalances();
      renderWallet();
      await track(registration.transfer_id);
      loadMyTransfers().catch(() => {});
    } catch (error) {
      showError(error);
      step("stopped: " + (error.message || error), "bad");
    }
  }

  /* ------------------------------------------------------------------ tracking */

  const TERMINAL = ["DELIVERED", "REFUSED", "FAILED", "STRANDED"];
  let timer = null;

  function renderTracking(state) {
    const host = el("tf-track");
    if (!host) return;
    const d = state.delivered || (state.attempts || [])[0] || {};
    const m = state.message || {};
    const destination = state.destination || {};
    const source = state.source || {};
    const links = state.links || {};
    host.innerHTML = `
      <div class="row" style="align-items:center;gap:12px;margin-bottom:14px">
        <span class="state-chip ${state.state}">${state.state}</span>
        <span class="dim">${state.detail || ""}</span>
      </div>
      <div class="evidence">
        <div class="block">
          <div class="eyebrow">Source</div>
          <dl class="kv">
            <dt>Transaction</dt><dd>${linkNode(links.source, source.source_tx || "\u2014")}</dd>
            <dt>Block</dt><dd class="mono">${source.block || "\u2014"}</dd>
            <dt>Witness</dt><dd>${source.detail || "\u2014"}</dd>
            <dt>Token movement</dt><dd>${(() => {
              const w = ((state.record || {}).witnesses || {});
              const burn = w.burn || {};
              return burn.amount !== undefined
                ? `${usdc(burn.amount)} USDC \u2014 ${burn.witness}`
                : "\u2014";
            })()}</dd>
            <dt>Protocol event</dt><dd>${(() => {
              const ev = (((state.record || {}).witnesses || {}).deposit_for_burn || {});
              return ev.amount !== undefined
                ? `${usdc(ev.amount)} USDC \u2014 ${ev.witness}` : "\u2014";
            })()}</dd>
          </dl>
        </div>
        <div class="block">
          <div class="eyebrow">Protocol</div>
          <dl class="kv">
            <dt>Amount burned</dt><dd class="mono">${usdc(m.amount)} USDC</dd>
            <dt>Protocol fee</dt><dd class="mono">${m.fee_executed !== undefined && m.fee_executed !== null
              ? m.fee_executed + " units" : "\u2014"}</dd>
            <dt>Transfer nonce</dt><dd class="mono">${m.nonce ? short(m.nonce, 12, 8) : "not reported yet"}</dd>
            <dt>Recipient</dt><dd class="mono">${short(m.mint_recipient || m.recipient, 12, 6)}</dd>
            <dt>Caller</dt><dd class="mono">${m.destination_caller ? short(m.destination_caller, 12, 6) : "anyone"}</dd>
          </dl>
        </div>
        <div class="block">
          <div class="eyebrow">Attestation</div>
          <dl class="kv">
            <dt>State</dt><dd>${(state.attestation || {}).state || "\u2014"}</dd>
            <dt>Status</dt><dd>${(state.attestation || {}).status || "\u2014"}</dd>
          </dl>
        </div>
        <div class="block">
          <div class="eyebrow">Destination</div>
          <dl class="kv">
            <dt>Chain</dt><dd>${destination.name || "\u2014"}</dd>
            <dt>Contract</dt><dd>${linkNode(links.contract, short(destination.transmitter, 10, 6))}</dd>
            <dt>Transaction</dt><dd>${linkNode(links.destination, d.transaction_hash || "not yet")}</dd>
            <dt>Receipt</dt><dd>${d.receipt_status || "\u2014"}</dd>
            <dt>Minted</dt><dd class="mono">${d.minted !== undefined && d.minted !== null
              ? usdc(d.minted) + " USDC" : "\u2014"}</dd>
          </dl>
        </div>
        <div class="block">
          <div class="eyebrow">Invariant</div>
          <dl class="kv">
            <dt>Burned</dt><dd class="mono">${usdc(d.burned !== undefined ? d.burned : m.amount)}</dd>
            <dt>Minted</dt><dd class="mono">${usdc(d.minted)}</dd>
            <dt>Fee</dt><dd class="mono">${d.fee !== undefined && d.fee !== null ? d.fee : "\u2014"}</dd>
            <dt>Expected minimum</dt><dd class="mono">${usdc(d.expected_min)}</dd>
            <dt>Result</dt><dd><span class="tag ${d.paired ? "ok" : (state.state === "DELIVERED" ? "no" : "wait")}">${
              state.state === "DELIVERED" ? "PAIRED" : (d.paired === false ? "SHORT" : state.state)}</span></dd>
          </dl>
        </div>
      </div>
      ${state.decision ? `<div class="decision">
        <span class="tag ${state.decision.action === "complete" ? "ok" : "no"}">${state.decision.reason}</span>
        <span class="dim">${state.decision.detail || ""}</span>
        ${state.decision.evidence ? `<div class="why">${state.decision.evidence}</div>` : ""}
      </div>` : ""}
      <div class="row" style="margin-top:16px">
        ${state.execution && state.execution.ready
          ? `<button class="btn primary" id="btn-execute">Execute delivery</button>`
          : ""}
        ${state.state === "REFUSED" || (state.execution && state.execution.refusal)
          ? `<span class="dim">No execution button: the protocol refuses this delivery.</span>` : ""}
        <a class="btn outline sm" href="#my-transfers" id="btn-see-history">My transfers</a>
      </div>`;
    const execute = el("btn-execute");
    if (execute) execute.addEventListener("click", () => executeDelivery(state.transfer_id));
  }

  async function track(transferId) {
    if (timer) clearInterval(timer);
    const poll = async () => {
      try {
        const state = await api(`/api/transfer/${transferId}/evidence`);
        renderTracking(state);
        if (TERMINAL.includes(state.state)) {
          clearInterval(timer);
          timer = null;
          note(`${state.state}: polling stopped`, state.state === "DELIVERED" ? "good" : "bad");
        }
      } catch (error) {
        note(`could not read the transfer: ${error.message}`, "bad");
      }
    };
    text(el("tf-track-id"), transferId);
    await poll();
    if (!timer) timer = setInterval(poll, 10000);
  }

  async function executeDelivery(transferId) {
    const button = el("btn-execute");
    if (button) { button.disabled = true; button.textContent = "broadcasting\u2026"; }
    note("executing the delivery through the rail\u2026");
    try {
      const result = await post(`/api/transfer/${transferId}/execute`, { transfer_id: transferId });
      if (!result.attempted) {
        note(result.reason ? `not executed: ${result.reason}` : "the rail did not execute this",
             "bad");
      } else {
        const attempt = result.attempt || {};
        note(`delivery broadcast: ${attempt.transaction_hash || "no hash returned"}`);
      }
      await track(transferId);
    } catch (error) {
      showError(error);
    } finally {
      if (button) { button.disabled = false; button.textContent = "Execute delivery"; }
    }
  }

  /* ------------------------------------------------------------------ history */

  async function loadMyTransfers() {
    const host = el("my-list");
    if (!host) return;
    if (!Wallet.state.connected) {
      host.innerHTML = `<div class="dim">Connect a wallet to see the transfers it signed.</div>`;
      return;
    }
    const body = await api(`/api/transfers?address=${Wallet.state.address}`);
    const rows = body.transfers || [];
    if (!rows.length) {
      host.innerHTML = `<div class="dim">No transfers yet for ${short(Wallet.state.address)}.</div>`;
      return;
    }
    host.innerHTML = `
      <table>
        <thead><tr><th style="width:150px">Created</th><th style="width:110px">State</th>
          <th style="width:120px">Amount</th><th>Source</th><th>Destination</th></tr></thead>
        <tbody>${rows.map((t) => `
          <tr data-id="${t.transfer_id}">
            <td class="mono dim">${(t.created_at || "").replace("T", " ").replace("Z", "")}</td>
            <td><span class="state-chip ${t.state}">${t.state}</span></td>
            <td class="mono">${usdc(t.minted !== null && t.minted !== undefined ? t.minted : t.amount)} USDC</td>
            <td>${linkNode(t.source_link, short(t.source_tx, 12, 6))}</td>
            <td>${t.destination_link ? linkNode(t.destination_link, short(t.destination_tx, 12, 6))
              : `<span class="dim">${t.detail || "not delivered"}</span>`}</td>
          </tr>`).join("")}</tbody>
      </table>`;
    host.querySelectorAll("tr[data-id]").forEach((row) => {
      row.addEventListener("click", () => {
        A.showPane("transfer");
        track(row.dataset.id).catch(showError);
      });
    });
  }

  /* ------------------------------------------------------------------ wiring */

  A.transfer = {
    init() {
      if (!el("tf-source")) return;
      fillRoutes();
      renderWallet();
      renderActions();
      el("tf-source").addEventListener("change", () => { fillDestinations(); prepare(); });
      el("tf-destination").addEventListener("change", prepare);
      ["tf-amount", "tf-recipient", "tf-caller"].forEach((id) => {
        const node = el(id);
        if (node) node.addEventListener("change", prepare);
      });
      el("btn-approve").addEventListener("click", approve);
      el("btn-burn").addEventListener("click", burn);
      // Nothing touches the wallet until the person in front of the screen asks for it.
      // A page that calls `eth_requestAccounts` on load - even to save a click - is
      // indistinguishable from the pattern wallet warnings exist to catch, and a wallet
      // that warns on a rail like this one is warning correctly.
      loadMyTransfers().catch(() => {});
    },
    track,
    loadMyTransfers,
    wallet: Wallet,
    state: () => Wallet.state,
  };
})();
