/* Omarchy triage dashboard — vanilla JS + SVG force layout + TanStack Virtual */
(function () {
  "use strict";

  const DECISION_ORDER = { duplicate: 0, "related-theme": 1, unique: 2 };
  const TV = window.TanStackVirtual || {};
  const Virtualizer = TV.Virtualizer;
  const elementScroll = TV.elementScroll;
  const observeElementRect = TV.observeElementRect;
  const observeElementOffset = TV.observeElementOffset;
  const measureElement = TV.measureElement;

  const state = {
    groups: [],
    prs: [],
    edges: [],
    group_edges: [],
    rules: [],
    overlap: {},
    source: "",
    repo: "",
    selectedGroupId: null,
    selectedPr: null,
    selectedFile: null,
    showGroupGraph: false,
    leftTab: "queue", // queue | groups | allprs
    queue: {},
    new_pr_numbers: [],
    fileQueue: null,
    related: null,
    relatedKey: "",
    fetching: false,
  };

  let groupVirtualizer = null;
  let allPrVirtualizer = null;
  let memberVirtualizer = null;
  let fetchPollTimer = null;

  const $ = (id) => document.getElementById(id);

  function setStatus(msg) {
    $("status").textContent = msg;
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || res.statusText || "request failed");
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function applyState(data) {
    state.groups = data.groups || [];
    state.prs = data.prs || [];
    state.edges = data.edges || [];
    state.group_edges = data.group_edges || [];
    state.rules = data.rules || [];
    state.overlap = data.overlap || {};
    state.source = data.source || "";
    state.repo = data.repo || "";
    state.queue = data.queue || {};
    state.new_pr_numbers = data.new_pr_numbers || [];
    if (data.source) $("source").value = "gh";
    if (data.repo) $("repo").value = data.repo;
    if (
      state.selectedGroupId &&
      !state.groups.some((g) => g.group_id === state.selectedGroupId)
    ) {
      state.selectedGroupId = null;
      state.selectedPr = null;
      state.selectedFile = null;
    }
    if (!state.selectedGroupId && state.groups.length) {
      const gs = sortedGroups();
      const pick =
        gs.find((g) => {
          const n = (g.pr_numbers || []).length;
          return n >= 2 && n <= 24;
        }) || gs.find((g) => (g.pr_numbers || []).length <= 24);
      state.selectedGroupId = pick ? pick.group_id : null;
      state.selectedFile = null;
    }
    render();
  }

  function ruleFor(groupId) {
    return state.rules.find((r) => r.group_id === groupId) || null;
  }

  function sortedGroups() {
    return state.groups.slice().sort((a, b) => {
      const sa = (a.pr_numbers || []).length;
      const sb = (b.pr_numbers || []).length;
      if (sb !== sa) return sb - sa;
      const da = DECISION_ORDER[a.suggested_decision] ?? 9;
      const db = DECISION_ORDER[b.suggested_decision] ?? 9;
      return da - db;
    });
  }

  function sortedAllPrs() {
    return state.prs.slice().sort((a, b) => (b.number || 0) - (a.number || 0));
  }

  function prByNumber(n) {
    return state.prs.find((p) => p.number === n);
  }

  function makeVirtualizer(scrollEl, count, estimateSize, overscan, onChange) {
    if (!Virtualizer || !scrollEl) return null;
    return new Virtualizer({
      count,
      getScrollElement: () => scrollEl,
      estimateSize,
      overscan,
      scrollToFn: elementScroll,
      observeElementRect,
      observeElementOffset,
      measureElement,
      onChange: () => onChange(),
    });
  }

  function renderLeftHeader() {
    const hdr = $("groupListHeader");
    const q = state.queue || {};
    const c = q.counts || {};
    if (state.leftTab === "allprs") {
      hdr.textContent = state.prs.length + " PRs";
    } else if (state.leftTab === "queue") {
      hdr.textContent =
        (c.needs_you || 0) + " need you · " +
        (c.hardware || 0) + " hw · " +
        (c.upgrade || 0) + " upgrade · " +
        (c.hotspots || 0) + " hotspots";
    } else {
      hdr.textContent = state.groups.length + " groups · " + state.prs.length + " PRs";
    }
  }

  function renderGroupList() {
    const root = $("groupList");
    const groups = sortedGroups();
    renderLeftHeader();

    if (!groups.length) {
      if (groupVirtualizer) {
        try { groupVirtualizer._willUpdate = () => {}; } catch (_) {}
        groupVirtualizer = null;
      }
      root.innerHTML = '<div class="empty">No groups — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      // Fallback without virtualization
      root.innerHTML = "";
      for (const g of groups) root.appendChild(buildGroupCard(g));
      return;
    }

    if (!groupVirtualizer) {
      groupVirtualizer = makeVirtualizer(root, groups.length, () => 78, 8, paintGroupVirtual);
      groupVirtualizer._didMount();
    } else {
      groupVirtualizer.setOptions({
        ...groupVirtualizer.options,
        count: groups.length,
        getScrollElement: () => root,
        estimateSize: () => 78,
        overscan: 8,
        onChange: () => paintGroupVirtual(),
      });
    }
    groupVirtualizer._willUpdate();
    paintGroupVirtual();
  }

  function paintGroupVirtual() {
    const root = $("groupList");
    const groups = sortedGroups();
    if (!groupVirtualizer || !groups.length) return;
    const items = groupVirtualizer.getVirtualItems();
    const total = groupVirtualizer.getTotalSize();
    let inner = root.querySelector(".virt-inner");
    if (!inner) {
      root.innerHTML = "";
      inner = document.createElement("div");
      inner.className = "virt-inner";
      root.appendChild(inner);
    }
    inner.style.height = total + "px";
    // Remove stale absolute children not in current range
    const keep = new Set(items.map((i) => String(i.index)));
    [...inner.querySelectorAll(".virt-item")].forEach((el) => {
      if (!keep.has(el.dataset.index)) el.remove();
    });
    for (const vi of items) {
      let el = inner.querySelector('.virt-item[data-index="' + vi.index + '"]');
      if (!el) {
        el = document.createElement("div");
        el.className = "virt-item";
        el.dataset.index = String(vi.index);
        const g = groups[vi.index];
        const card = buildGroupCard(g);
        el.appendChild(card);
        inner.appendChild(el);
      } else {
        const g = groups[vi.index];
        const card = el.firstChild;
        if (card) {
          card.className =
            "group-card" + (g.group_id === state.selectedGroupId ? " selected" : "");
        }
      }
      el.style.transform = "translateY(" + vi.start + "px)";
      el.style.height = vi.size + "px";
    }
  }

  function buildGroupCard(g) {
    const rule = ruleFor(g.group_id);
    const card = document.createElement("div");
    card.className =
      "group-card" + (g.group_id === state.selectedGroupId ? " selected" : "");
    const firstTitle = (g.title_variants && g.title_variants[0]) || "";
    const decision = g.suggested_decision || "unique";
    card.innerHTML = `
      <div class="row">
        <span class="mono">${escapeHtml(g.group_id)}</span>
        <span class="muted">${(g.pr_numbers || []).length} PRs</span>
      </div>
      <div class="row" style="margin-top:4px">
        <span class="pill ${escapeHtml(decision)}">${escapeHtml(decision)}</span>
        ${
          rule
            ? `<span class="pill ${escapeHtml(rule.decision)}">${escapeHtml(
                rule.decision === "approve" ? "blessed" : "rejected"
              )}</span>`
            : `<span class="muted">unreviewed</span>`
        }
      </div>
      <div class="card-title" title="${escapeHtml(firstTitle)}">${escapeHtml(
      firstTitle
    )}</div>`;
    card.addEventListener("click", () => {
      state.selectedGroupId = g.group_id;
      state.selectedPr = null;
      state.selectedFile = null;
      render();
    });
    return card;
  }

  function renderAllPrList() {
    const root = $("allPrList");
    const prs = sortedAllPrs();
    renderLeftHeader();

    if (!prs.length) {
      allPrVirtualizer = null;
      root.innerHTML = '<div class="empty">No PRs — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      root.innerHTML = "";
      for (const pr of prs) root.appendChild(buildPrRow(pr));
      return;
    }

    if (!allPrVirtualizer) {
      allPrVirtualizer = makeVirtualizer(root, prs.length, () => 36, 8, paintAllPrVirtual);
      allPrVirtualizer._didMount();
    } else {
      allPrVirtualizer.setOptions({
        ...allPrVirtualizer.options,
        count: prs.length,
        getScrollElement: () => root,
        estimateSize: () => 36,
        overscan: 8,
        onChange: () => paintAllPrVirtual(),
      });
    }
    allPrVirtualizer._willUpdate();
    paintAllPrVirtual();
  }

  function paintAllPrVirtual() {
    const root = $("allPrList");
    const prs = sortedAllPrs();
    if (!allPrVirtualizer || !prs.length) return;
    const items = allPrVirtualizer.getVirtualItems();
    const total = allPrVirtualizer.getTotalSize();
    let inner = root.querySelector(".virt-inner");
    if (!inner) {
      root.innerHTML = "";
      inner = document.createElement("div");
      inner.className = "virt-inner";
      root.appendChild(inner);
    }
    inner.style.height = total + "px";
    const keep = new Set(items.map((i) => String(i.index)));
    [...inner.querySelectorAll(".virt-item")].forEach((el) => {
      if (!keep.has(el.dataset.index)) el.remove();
    });
    for (const vi of items) {
      let el = inner.querySelector('.virt-item[data-index="' + vi.index + '"]');
      const pr = prs[vi.index];
      if (!el) {
        el = document.createElement("div");
        el.className = "virt-item";
        el.dataset.index = String(vi.index);
        el.appendChild(buildPrRow(pr));
        inner.appendChild(el);
      } else {
        const row = el.firstChild;
        if (row) {
          row.className =
            "pr-row" + (state.selectedPr === pr.number ? " selected" : "");
        }
      }
      el.style.transform = "translateY(" + vi.start + "px)";
      el.style.height = vi.size + "px";
    }
  }

  function buildPrRow(pr) {
    const row = document.createElement("div");
    row.className = "pr-row" + (state.selectedPr === pr.number ? " selected" : "");
    const gid = pr.group_id || "?";
    const label = pr.label || "";
    row.innerHTML =
      '<span class="num">#' +
      pr.number +
      '</span><span class="meta" title="' +
      escapeHtml(pr.title || "") +
      '">' +
      escapeHtml(pr.title || "") +
      ' <span class="gid">' +
      escapeHtml(gid) +
      "</span> " +
      escapeHtml(label) +
      "</span>";
    row.addEventListener("click", () => {
      state.selectedGroupId = pr.group_id || state.selectedGroupId;
      state.selectedPr = pr.number;
      state.selectedFile = null;
      // Keep All PRs tab visible but refresh detail
      render();
    });
    return row;
  }

  function setLeftTab(tab) {
    state.leftTab = tab;
    const qTab = $("tabQueue");
    const groupsTab = $("tabGroups");
    const allTab = $("tabAllPrs");
    qTab.classList.toggle("active", tab === "queue");
    groupsTab.classList.toggle("active", tab === "groups");
    allTab.classList.toggle("active", tab === "allprs");
    qTab.setAttribute("aria-selected", tab === "queue" ? "true" : "false");
    groupsTab.setAttribute("aria-selected", tab === "groups" ? "true" : "false");
    allTab.setAttribute("aria-selected", tab === "allprs" ? "true" : "false");
    $("queueList").classList.toggle("hidden", tab !== "queue");
    $("groupList").classList.toggle("hidden", tab !== "groups");
    $("allPrList").classList.toggle("hidden", tab !== "allprs");
    renderLeftHeader();
    if (tab === "queue") renderQueue();
    else if (tab === "groups") renderGroupList();
    else renderAllPrList();
  }

  function groupById(id) {
    return state.groups.find((g) => g.group_id === id) || null;
  }

  function renderQueue() {
    const root = $("queueList");
    const q = state.queue || {};
    renderLeftHeader();
    root.innerHTML = "";
    const piles = [
      ["Needs you", q.needs_you || []],
      ["Needs hardware", q.hardware || []],
      ["Can break upgrade", q.upgrade || []],
      ["Known shape", q.known || []],
      ["Junk", q.junk || []],
    ];
    for (const [label, ids] of piles) {
      const h = document.createElement("div");
      h.className = "pile-head";
      h.textContent = label + " · " + ids.length;
      root.appendChild(h);
      if (!ids.length) {
        const empty = document.createElement("div");
        empty.className = "muted pile-empty";
        empty.textContent = "none";
        root.appendChild(empty);
        continue;
      }
      ids.slice(0, 80).forEach((id) => {
        const g = groupById(id);
        if (g) root.appendChild(buildGroupCard(g));
      });
      if (ids.length > 80) {
        const more = document.createElement("div");
        more.className = "muted pile-empty";
        more.textContent = "+" + (ids.length - 80) + " more";
        root.appendChild(more);
      }
    }
    const h = document.createElement("div");
    h.className = "pile-head";
    const spots = q.hotspots || [];
    h.textContent = "File hotspots · " + spots.length;
    root.appendChild(h);
    spots.forEach((hs) => {
      const row = document.createElement("div");
      row.className = "hotspot-row";
      row.innerHTML =
        '<span class="mono">' +
        escapeHtml(hs.path) +
        '</span><span class="muted">' +
        hs.pr_count +
        " PRs</span>";
      row.addEventListener("click", () => {
        openFileQueue(hs.path);
      });
      root.appendChild(row);
    });
  }


  let relatedGen = 0;

  async function loadRelated(pr, path) {
    if (!pr) {
      state.related = null;
      state.relatedKey = "";
      renderRelated();
      return;
    }
    const key = pr + "|" + (path || "");
    if (state.relatedKey === key && state.related && !state.related.loading) {
      renderRelated();
      return;
    }
    const gen = ++relatedGen;
    state.relatedKey = key;
    state.related = { loading: true };
    renderRelated();
    try {
      let url = "/api/related?pr=" + encodeURIComponent(pr);
      if (path) url += "&path=" + encodeURIComponent(path);
      const data = await api(url);
      if (gen !== relatedGen) return;
      state.related = data;
      renderRelated();
    } catch (err) {
      if (gen !== relatedGen) return;
      state.related = { enabled: false, reason: err.message, related: [] };
      renderRelated();
    }
  }

  function renderRelated() {
    const root = $("relatedList");
    if (!root) return;
    const data = state.related;
    if (!data) {
      root.className = "related-list muted";
      root.textContent = "Pick a PR.";
      return;
    }
    if (data.loading) {
      root.className = "related-list muted";
      root.textContent = "Ranking patches…";
      return;
    }
    if (!data.enabled || !(data.related || []).length) {
      root.className = "related-list muted";
      const reason = data.reason || (data.enabled ? "No near-patch neighbors." : "OpenRouter key not set.");
      root.textContent = "";
      if (reason.indexOf("https://") >= 0) {
        const [before, url] = [reason.split("https://")[0], "https://" + reason.split("https://", 1)[1]];
        root.appendChild(document.createTextNode(before));
        const a = document.createElement("a");
        a.href = url.split(" ")[0];
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = a.href;
        root.appendChild(a);
      } else {
        root.textContent = reason;
      }
      return;
    }
    root.className = "related-list";
    root.innerHTML = "";
    items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "related-row";
      row.innerHTML =
        '<a class="mono" href="' +
        escapeHtml(it.html_url || "#") +
        '" target="_blank" rel="noopener">#' +
        it.number +
        '</a><span title="' +
        escapeHtml(it.title || "") +
        '">' +
        escapeHtml(it.title || "") +
        '</span><span class="muted">' +
        escapeHtml(it.group_id || "") +
        '</span><span class="score">' +
        Number(it.score || 0).toFixed(2) +
        "</span>";
      row.addEventListener("click", (ev) => {
        if (ev.target.tagName === "A") return;
        if (it.group_id) state.selectedGroupId = it.group_id;
        state.selectedPr = it.number;
        render();
      });
      root.appendChild(row);
    });
  }

  async function openFileQueue(path) {
    state.selectedFile = path;
    state.fileQueue = { path: path, prs: [], loading: true };
    render();
    try {
      const data = await api("/api/file?path=" + encodeURIComponent(path));
      if (state.selectedFile !== path) return;
      state.fileQueue = data;
      const first = (data.prs || [])[0];
      if (first && first.group_id) state.selectedGroupId = first.group_id;
      render();
    } catch (err) {
      if (state.selectedFile !== path) return;
      state.fileQueue = { path: path, prs: [], error: err.message };
      render();
    }
  }

  function renderFileQueue() {
    const root = $("fileQueue");
    if (!root) return;
    const fq = state.fileQueue;
    const path = state.selectedFile;
    if (!path) {
      root.className = "file-queue muted";
      root.textContent = "Click a hotspot or a file row.";
      return;
    }
    if (fq && fq.loading) {
      root.className = "file-queue muted";
      root.textContent = "Loading PRs on " + path + "…";
      return;
    }
    if (fq && fq.error) {
      root.className = "file-queue muted";
      root.textContent = "error: " + fq.error;
      return;
    }
    const items = (fq && fq.prs) || [];
    const extra = fq && fq.truncated ? " · +" + fq.truncated + " hidden" : "";
    root.className = "file-queue";
    root.innerHTML = "";
    const head = document.createElement("div");
    head.className = "muted";
    head.textContent = path + " · " + ((fq && fq.pr_count) || items.length) + " PRs" + extra;
    root.appendChild(head);
    items.forEach((pr) => {
      const row = document.createElement("div");
      row.className = "hotspot-row";
      if (state.selectedPr === pr.number) row.classList.add("selected");
      row.innerHTML =
        '<a class="mono" href="' +
        escapeHtml(pr.html_url || "#") +
        '" target="_blank" rel="noopener">#' +
        pr.number +
        '</a><span title="' +
        escapeHtml(pr.title || "") +
        '">' +
        escapeHtml(pr.title || "") +
        '</span><span class="muted">' +
        escapeHtml(pr.group_id || "") +
        "</span>";
      row.addEventListener("click", (ev) => {
        if (ev.target.tagName === "A") return;
        if (pr.group_id) state.selectedGroupId = pr.group_id;
        state.selectedPr = pr.number;
        render();
      });
      root.appendChild(row);
    });
  }

  function renderDetail() {
    const empty = $("detailEmpty");
    const detail = $("detail");
    const g = state.groups.find((x) => x.group_id === state.selectedGroupId);
    if (!g) {
      empty.classList.remove("hidden");
      detail.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    detail.classList.remove("hidden");
    renderFileQueue();

    const rule = ruleFor(g.group_id);
    $("detailId").textContent = g.group_id;
    $("detailMeta").textContent = rule
      ? `rule: ${rule.decision} (${rule.rule_id})`
      : "no rule yet";
    const cls = g.card_class || "needs-look";
    const ac = $("agentClass");
    ac.textContent = cls;
    ac.className = "pill " + cls;
    $("agentNote").textContent = g.card_note || (g.title_variants || [])[0] || "";
    const n = (g.pr_numbers || []).length;
    $("agentHint").textContent = n >= 2
      ? n + " PRs, same file-set. Bless once if the shape is safe."
      : "Singleton. Read it or wait for a twin.";

    const decision = g.suggested_decision || "unique";
    const pill = $("detailDecision");
    pill.textContent = decision;
    pill.className = "pill " + decision;

    renderOverlap(g);
    renderDiffs(g);
    const qPr = state.selectedPr || (g.pr_numbers || [])[0];
    loadRelated(qPr, state.selectedFile || null);

    const titles = $("detailTitles");
    titles.innerHTML = "";
    (g.title_variants || []).forEach((t) => {
      const li = document.createElement("li");
      li.textContent = t;
      titles.appendChild(li);
    });

    renderMembers(g);
    drawGraph();
  }

  function renderMembers(g) {
    const wrap = $("detailMembersWrap");
    const members = $("detailMembers");
    const nums = g.pr_numbers || [];
    members.innerHTML = "";
    memberVirtualizer = null;

    const useVirt = nums.length > 40 && Virtualizer;
    wrap.classList.toggle("virt", !!useVirt);

    if (!useVirt) {
      wrap.style.maxHeight = "";
      nums.forEach((num) => members.appendChild(buildMemberLi(num)));
      return;
    }

    // Virtualize long member lists
    wrap.style.maxHeight = "320px";
    wrap.style.overflow = "auto";
    members.style.position = "relative";
    members.style.height = nums.length * 36 + "px";

    memberVirtualizer = makeVirtualizer(wrap, nums.length, () => 36, 8, () => {
      paintMembers(nums);
    });
    memberVirtualizer._didMount();
    memberVirtualizer._willUpdate();
    paintMembers(nums);
  }

  function paintMembers(nums) {
    const members = $("detailMembers");
    if (!memberVirtualizer) return;
    const items = memberVirtualizer.getVirtualItems();
    members.innerHTML = "";
    members.style.height = memberVirtualizer.getTotalSize() + "px";
    for (const vi of items) {
      const li = buildMemberLi(nums[vi.index]);
      li.style.position = "absolute";
      li.style.top = "0";
      li.style.left = "0";
      li.style.right = "0";
      li.style.transform = "translateY(" + vi.start + "px)";
      li.style.height = vi.size + "px";
      members.appendChild(li);
    }
  }

  function buildMemberLi(num) {
    const pr = prByNumber(num) || {
      number: num,
      title: "?",
      user: "?",
      label: "needs-human",
      html_url: "",
    };
    const li = document.createElement("li");
    if (state.selectedPr === num) li.classList.add("selected");
    const labelClass =
      pr.label === "auto:approved-shape" ? "label-auto" : "label-needs";
    const href = pr.html_url || "#";
    li.innerHTML = `
      <a href="${escapeHtml(href)}" target="_blank" rel="noopener">#${num}</a>
      <span title="${escapeHtml(pr.title || "")}">${escapeHtml(pr.title || "")}</span>
      <span class="user muted">${escapeHtml(pr.user || "")}</span>
      <span class="${labelClass}">${escapeHtml(pr.label || "")}</span>`;
    li.addEventListener("click", (ev) => {
      if (ev.target.tagName === "A") return;
      state.selectedPr = num;
      renderDetail();
      drawGraph();
    });
    return li;
  }

  function prFileEntries(pr) {
    const files = (pr && pr.files) || [];
    if (!files.length) {
      return ((pr && pr.paths) || []).map((p) => ({ path: p, patch: "" }));
    }
    if (typeof files[0] === "string") {
      return files.map((p) => ({ path: p, patch: "" }));
    }
    return files.map((f) => ({ path: f.path, patch: f.patch || "" }));
  }

  function patchFor(pr, path) {
    const entry = prFileEntries(pr).find((f) => f.path === path);
    return entry ? entry.patch || "" : "";
  }

  async function renderOverlap(g) {
    const summary = $("overlapSummary");
    const table = $("overlapMatrix");
    table.innerHTML = "";
    let ov = (state.overlap && state.overlap[g.group_id]) || null;
    const n = (g.pr_numbers || []).length;
    if (ov && ov.lazy) {
      summary.textContent =
        "jaccard " +
        (typeof ov.jaccard === "number" ? ov.jaccard.toFixed(2) : "?") +
        " · " +
        (ov.shared_n || 0) +
        " shared · " +
        (ov.unique_n || 0) +
        " unique" +
        (n > 24 ? " · " + n + " PRs (matrix capped)" : "");
      try {
        ov = await api("/api/overlap?group_id=" + encodeURIComponent(g.group_id));
        state.overlap[g.group_id] = ov;
      } catch (err) {
        summary.textContent = "overlap error: " + err.message;
        return;
      }
      if (state.selectedGroupId !== g.group_id) return;
    }
    if (!ov) {
      summary.textContent = "no overlap data";
      return;
    }
    const sharedN = ov.shared_n != null ? ov.shared_n : (ov.shared || []).length;
    const uniqueN = ov.unique_n != null ? ov.unique_n : (ov.unique || []).length;
    const jacc = typeof ov.jaccard === "number" ? ov.jaccard.toFixed(2) : "?";
    let extra = "";
    if (ov.pr_truncated) extra += " · +" + ov.pr_truncated + " PRs hidden";
    if (ov.row_truncated) extra += " · +" + ov.row_truncated + " files hidden";
    summary.textContent =
      "jaccard " + jacc + " · " + sharedN + " shared · " + uniqueN + " unique" + extra;

    const members = ov.pr_numbers || (g.pr_numbers || []).slice(0, 24);
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    const thPath = document.createElement("th");
    thPath.className = "path-col";
    thPath.textContent = "file";
    hr.appendChild(thPath);
    members.forEach((num) => {
      const th = document.createElement("th");
      th.textContent = "#" + num;
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    (ov.matrix || []).forEach((row) => {
      const tr = document.createElement("tr");
      tr.className = "row" + (state.selectedFile === row.path ? " selected" : "");
      const tdPath = document.createElement("td");
      tdPath.className = "path-cell";
      tdPath.innerHTML =
        escapeHtml(row.path) +
        (row.same_patch ? ' <span class="same-label">same</span>' : "");
      tr.appendChild(tdPath);
      members.forEach((num) => {
        const td = document.createElement("td");
        td.className = "cell";
        const hit = !!(row.prs && row.prs[String(num)]);
        const sq = document.createElement("span");
        sq.className = "sq " + (hit ? row.kind || "unique" : "miss");
        td.appendChild(sq);
        tr.appendChild(td);
      });
      tr.addEventListener("click", () => {
        openFileQueue(row.path);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }


  function colorizeDiff(patch) {
    const lines = String(patch || "").split("\n");
    return lines
      .map((line) => {
        const esc = escapeHtml(line);
        if (line.startsWith("+") && !line.startsWith("+++")) {
          return '<span class="add">' + esc + "</span>";
        }
        if (line.startsWith("-") && !line.startsWith("---")) {
          return '<span class="del">' + esc + "</span>";
        }
        if (line.startsWith("@@")) {
          return '<span class="hunk">' + esc + "</span>";
        }
        return esc;
      })
      .join("\n");
  }

  let diffGen = 0;

  async function renderDiffs(g) {
    const root = $("diffPanels");
    root.innerHTML = "";
    const path = state.selectedFile;
    if (!path) {
      root.innerHTML = '<div class="muted diff-hint">Click a file to see patches.</div>';
      return;
    }
    const fqNums = (state.fileQueue && state.fileQueue.prs || []).map((p) => p.number);
    const nums = fqNums.length ? fqNums.slice(0, 24) : (g.pr_numbers || []).slice();
    if (!nums.length) {
      root.innerHTML =
        '<div class="muted diff-hint">No patches for ' +
        escapeHtml(path) +
        ".</div>";
      return;
    }
    const gen = ++diffGen;
    root.innerHTML = '<div class="muted diff-hint">Loading patches…</div>';
    const repo = state.repo || "omacom/omarchy";
    let data;
    try {
      data = await api(
        "/api/patches?repo=" +
          encodeURIComponent(repo) +
          "&prs=" +
          nums.join(",") +
          "&path=" +
          encodeURIComponent(path)
      );
    } catch (err) {
      if (gen !== diffGen) return;
      root.innerHTML =
        '<div class="muted diff-hint">Could not load patches: ' +
        escapeHtml(err.message) +
        "</div>";
      return;
    }
    if (gen !== diffGen) return;
    const items = data.items || [];
    items.forEach((it) => {
      const pr = prByNumber(it.number);
      if (!pr) return;
      const files = prFileEntries(pr);
      const idx = files.findIndex((f) => f.path === path);
      if (idx >= 0) files[idx].patch = it.patch || "";
      else files.push({ path: path, patch: it.patch || "" });
      pr.files = files;
    });
    root.innerHTML = "";
    const withPatch = items.filter((it) => (it.patch || "").length);
    if (!items.length) {
      root.innerHTML =
        '<div class="muted diff-hint">No patches for ' +
        escapeHtml(path) +
        ".</div>";
      return;
    }
    const firstPatch = (items[0] && items[0].patch) || "";
    items.forEach((it) => {
      const pr = prByNumber(it.number) || { number: it.number, user: "" };
      const patch = it.patch || "";
      const same = patch === firstPatch;
      const panel = document.createElement("div");
      panel.className = "diff-panel";
      panel.innerHTML =
        '<div class="diff-panel-head">' +
        '<span><span class="mono">#' +
        pr.number +
        "</span> " +
        escapeHtml(pr.user || "") +
        "</span>" +
        '<span class="diff-tag ' +
        (same ? "same" : "diff") +
        '">' +
        (patch ? (same ? "same hunk" : "different") : "no patch") +
        "</span></div>" +
        '<pre class="diff">' +
        colorizeDiff(patch || "(empty patch)") +
        "</pre>";
      root.appendChild(panel);
    });
    if (!withPatch.length) {
      const hint = document.createElement("div");
      hint.className = "muted diff-hint";
      hint.textContent = "Cached files have no patch for this path (binary or too large for GitHub).";
      root.prepend(hint);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function groupColor(groupId) {
    let h = 0;
    for (let i = 0; i < groupId.length; i++) h = (h * 31 + groupId.charCodeAt(i)) >>> 0;
    const hue = h % 360;
    return `hsl(${hue} 45% 42%)`;
  }

  function neighborhoodForGroup(g) {
    const memberSet = new Set(g.pr_numbers || []);
    const relevant = state.edges.filter(
      (e) => memberSet.has(e.source) || memberSet.has(e.target)
    );
    const outsiderScores = new Map();
    for (const e of relevant) {
      const other = memberSet.has(e.source) ? e.target : e.source;
      if (memberSet.has(other)) continue;
      const prev = outsiderScores.get(other) || 0;
      outsiderScores.set(other, Math.max(prev, e.weight));
    }
    const outsiders = [...outsiderScores.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
      .map(([n]) => n);
    const nodeIds = new Set([...memberSet, ...outsiders]);
    const nodes = [...nodeIds].map((n) => {
      const pr = prByNumber(n) || { number: n, label: "needs-human", group_id: null };
      return {
        id: n,
        member: memberSet.has(n),
        label: pr.label || "needs-human",
        group_id: pr.group_id || (memberSet.has(n) ? g.group_id : null),
        title: pr.title || "",
      };
    });
    const edges = relevant.filter(
      (e) => nodeIds.has(e.source) && nodeIds.has(e.target)
    );
    return { nodes, edges };
  }

  function drawGraph() {
    const svg = $("graphSvg");
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const g = state.groups.find((x) => x.group_id === state.selectedGroupId);
    if (!g) return;

    if (state.showGroupGraph) {
      drawGroupOverview(svg);
      return;
    }

    const { nodes, edges } = neighborhoodForGroup(g);
    runForceAndDraw(svg, nodes, edges, {
      idKey: "id",
      labelFn: (n) => "#" + n.id,
      fillFn: (n) =>
        n.label === "auto:approved-shape" ? "#34d399" : "#60a5fa",
      ringFn: (n) => groupColor(n.group_id || "x"),
      onClick: (n) => {
        state.selectedPr = n.id;
        renderDetail();
      },
      selectedId: state.selectedPr,
    });
  }

  function drawGroupOverview(svg) {
    const nodes = state.groups.map((g) => ({
      id: g.group_id,
      count: (g.pr_numbers || []).length,
      decision: g.suggested_decision || "unique",
    }));
    const edges = state.group_edges || [];
    runForceAndDraw(svg, nodes, edges, {
      idKey: "id",
      labelFn: (n) => n.id,
      fillFn: () => "#a1a1aa",
      ringFn: (n) => groupColor(n.id),
      radiusFn: (n) => 8 + Math.min(18, n.count * 2),
      onClick: (n) => {
        state.selectedGroupId = n.id;
        state.selectedPr = null;
        state.selectedFile = null;
        state.showGroupGraph = false;
        $("groupGraphToggle").checked = false;
        render();
      },
      selectedId: state.selectedGroupId,
    });
  }

  function runForceAndDraw(svg, nodes, edges, opts) {
    const W = 400;
    const H = 280;
    const cx = W / 2;
    const cy = H / 2;
    const n = nodes.length;
    if (!n) {
      const t = svgEl("text", {
        x: cx,
        y: cy,
        "text-anchor": "middle",
        class: "node-label",
      });
      t.textContent = "no edges";
      svg.appendChild(t);
      return;
    }

    nodes.forEach((node, i) => {
      const a = (2 * Math.PI * i) / n - Math.PI / 2;
      const r = 70 + Math.min(40, n * 2);
      node.x = cx + Math.cos(a) * r;
      node.y = cy + Math.sin(a) * r;
      node.vx = 0;
      node.vy = 0;
    });
    const byId = Object.fromEntries(nodes.map((node) => [node[opts.idKey], node]));

    const iterations = 80;
    for (let iter = 0; iter < iterations; iter++) {
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = nodes[i].x - nodes[j].x;
          let dy = nodes[i].y - nodes[j].y;
          let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
          const force = 400 / (dist * dist);
          dx = (dx / dist) * force;
          dy = (dy / dist) * force;
          nodes[i].vx += dx;
          nodes[i].vy += dy;
          nodes[j].vx -= dx;
          nodes[j].vy -= dy;
        }
      }
      for (const e of edges) {
        const a = byId[e.source];
        const b = byId[e.target];
        if (!a || !b) continue;
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const ideal = 90 - Math.min(40, (e.weight || 0.5) * 40);
        const force = (dist - ideal) * 0.05;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        a.vx += dx;
        a.vy += dy;
        b.vx -= dx;
        b.vy -= dy;
      }
      const cooling = 0.85 - (iter / iterations) * 0.5;
      for (const node of nodes) {
        node.vx += (cx - node.x) * 0.01;
        node.vy += (cy - node.y) * 0.01;
        node.x += node.vx * cooling;
        node.y += node.vy * cooling;
        node.vx *= 0.6;
        node.vy *= 0.6;
        node.x = Math.max(18, Math.min(W - 18, node.x));
        node.y = Math.max(18, Math.min(H - 18, node.y));
      }
    }

    for (const e of edges) {
      const a = byId[e.source];
      const b = byId[e.target];
      if (!a || !b) continue;
      const line = svgEl("line", {
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        class: "edge",
        "stroke-width": Math.max(0.8, (e.weight || 0.5) * 2.5),
      });
      svg.appendChild(line);
    }

    for (const node of nodes) {
      const gEl = svgEl("g", {
        class: "node" + (node[opts.idKey] === opts.selectedId ? " selected" : ""),
      });
      const r = opts.radiusFn ? opts.radiusFn(node) : node.member === false ? 7 : 9;
      const ring = svgEl("circle", {
        cx: node.x,
        cy: node.y,
        r: r + 3,
        fill: "none",
        stroke: opts.ringFn(node),
        "stroke-width": 2,
      });
      const circle = svgEl("circle", {
        cx: node.x,
        cy: node.y,
        r: r,
        fill: opts.fillFn(node),
        stroke: node[opts.idKey] === opts.selectedId ? "#f59e0b" : "#18181b",
        "stroke-width": node[opts.idKey] === opts.selectedId ? 2.5 : 1,
      });
      const label = svgEl("text", {
        x: node.x,
        y: node.y + r + 11,
        "text-anchor": "middle",
        class: "node-label",
      });
      label.textContent = opts.labelFn(node);
      gEl.appendChild(ring);
      gEl.appendChild(circle);
      gEl.appendChild(label);
      gEl.addEventListener("click", () => opts.onClick(node));
      svg.appendChild(gEl);
    }
  }

  function svgEl(name, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    return el;
  }

  function render() {
    if (state.leftTab === "allprs") renderAllPrList();
    else if (state.leftTab === "queue") renderQueue();
    else renderGroupList();
    renderDetail();
  }

  async function loadState() {
    setStatus("loading…");
    try {
      const data = await api("/api/state");
      applyState(data);
      const c = (state.queue && state.queue.counts) || {};
      setStatus(
        `${state.prs.length} PRs · ${state.groups.length} groups · ${c.needs_you || 0} need you`
      );
    } catch (err) {
      setStatus("error: " + err.message);
    }
  }

  function formatProgress(p) {
    if (!p) return "fetching…";
    if (p.error) return "error: " + p.error;
    const phase = p.phase || "";
    const done = p.done || 0;
    const total = p.total || 0;
    if (phase === "files" && total) return `files ${done}/${total}`;
    if (phase === "listing") return "listing pulls…";
    if (phase === "pipeline") return p.message || `clustering ${done} PRs`;
    if (phase === "ingest") return p.message || "ingesting…";
    if (p.message) return p.message;
    if (total) return `${phase || "fetch"} ${done}/${total}`;
    return phase || "fetching…";
  }

  async function pollProgressUntilDone() {
    return new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const p = await api("/api/progress");
          setStatus(formatProgress(p));
          if (p.error && !p.running) {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            reject(new Error(p.error));
            return;
          }
          if (p.ready && !p.running) {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            resolve(p);
            return;
          }
          if (!p.running && !p.ready && p.phase === "error") {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            reject(new Error(p.error || "fetch failed"));
            return;
          }
          fetchPollTimer = setTimeout(tick, 400);
        } catch (err) {
          state.fetching = false;
          $("fetchBtn").disabled = false;
          reject(err);
        }
      };
      tick();
    });
  }

  async function doFetch() {
    const source = "gh";
    const repo = $("repo").value.trim() || "omacom/omarchy";
    const limit = Number($("limit").value);
    const limitVal = Number.isFinite(limit) ? limit : 0;
    $("fetchBtn").disabled = true;
    state.fetching = true;
    setStatus(source === "fixtures" ? "starting fixtures…" : "starting gh fetch…");
    try {
      await api("/api/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, repo, limit: limitVal }),
      });
      await pollProgressUntilDone();
      const data = await api("/api/state");
      applyState(data);
      setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
    } catch (err) {
      if (err.status === 409) {
        setStatus(formatProgress(err.data) + " (already running)");
        try {
          await pollProgressUntilDone();
          const data = await api("/api/state");
          applyState(data);
          setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
        } catch (e2) {
          setStatus("fetch error: " + e2.message);
        }
      } else {
        setStatus("fetch error: " + err.message);
      }
    } finally {
      state.fetching = false;
      $("fetchBtn").disabled = false;
      if (fetchPollTimer) {
        clearTimeout(fetchPollTimer);
        fetchPollTimer = null;
      }
    }
  }

  async function doDecide(decision) {
    if (!state.selectedGroupId) return;
    setStatus(decision + "…");
    try {
      const data = await api("/api/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          group_id: state.selectedGroupId,
          decision,
        }),
      });
      applyState(data);
      setStatus(`rule saved: ${decision} ${state.selectedGroupId}`);
    } catch (err) {
      setStatus("decide error: " + err.message);
    }
  }

  $("fetchBtn").addEventListener("click", doFetch);
  $("blessBtn").addEventListener("click", () => doDecide("approve"));
  $("hardwareBtn").addEventListener("click", () => doDecide("hardware"));
  $("upgradeBtn").addEventListener("click", () => doDecide("upgrade"));
  $("rejectBtn").addEventListener("click", () => doDecide("reject"));
  $("groupGraphToggle").addEventListener("change", (e) => {
    state.showGroupGraph = !!e.target.checked;
    drawGraph();
  });
  $("tabQueue").addEventListener("click", () => setLeftTab("queue"));
  $("tabGroups").addEventListener("click", () => setLeftTab("groups"));
  $("tabAllPrs").addEventListener("click", () => setLeftTab("allprs"));

  loadState();
})();
