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
    fileQueueOpen: false,
    selectedUser: null,
    related: null,
    relatedKey: "",
    fetching: false,
    filterQuery: "",
    filterLabel: "",
  };

  let groupVirtualizer = null;
  let allPrVirtualizer = null;
  let queueVirtualizer = null;
  let memberVirtualizer = null;
  let queueRowsCache = null;
  let fetchPollTimer = null;

  const $ = (id) => document.getElementById(id);

  let writingUrl = false;
  let alignSidebar = true;

  function readUrl() {
    const q = new URLSearchParams(location.search);
    const tab = q.get("tab");
    if (tab === "queue" || tab === "groups" || tab === "allprs") state.leftTab = tab;
    const gid = q.get("group");
    if (gid) state.selectedGroupId = gid;
    const pr = q.get("pr");
    if (pr && /^\d+$/.test(pr)) state.selectedPr = parseInt(pr, 10);
    else if (q.has("pr")) state.selectedPr = null;
    const file = q.get("file");
    if (file) state.selectedFile = file;
    else if (q.has("file")) state.selectedFile = null;
    const user = q.get("user");
    if (user) state.selectedUser = user;
    else if (q.has("user")) state.selectedUser = null;
    state.filterQuery = q.get("q") || "";
    state.filterLabel = q.get("label") || "";
  }

  function writeUrl(push) {
    const q = new URLSearchParams();
    q.set("tab", state.leftTab || "queue");
    if (state.selectedGroupId) q.set("group", state.selectedGroupId);
    if (state.selectedPr) q.set("pr", String(state.selectedPr));
    if (state.selectedFile) q.set("file", state.selectedFile);
    if (state.selectedUser) q.set("user", state.selectedUser);
    if (state.filterQuery) q.set("q", state.filterQuery);
    if (state.filterLabel) q.set("label", state.filterLabel);
    const next = "?" + q.toString();
    if (next === location.search) return;
    writingUrl = true;
    if (push) history.pushState(null, "", next);
    else history.replaceState(null, "", next);
    writingUrl = false;
  }

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
    queueRowsCache = null;
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
    if (state.selectedPr && !state.prs.some((p) => p.number === state.selectedPr)) {
      state.selectedPr = null;
    }
    if (!state.selectedGroupId && state.groups.length && !new URLSearchParams(location.search).get("group")) {
      const gs = sortedGroups();
      const pick =
        gs.find((g) => {
          const n = (g.pr_numbers || []).length;
          return n >= 2 && n <= 24;
        }) || gs.find((g) => (g.pr_numbers || []).length <= 24);
      state.selectedGroupId = pick ? pick.group_id : null;
      if (!new URLSearchParams(location.search).get("file")) state.selectedFile = null;
    }
    setLeftTab(state.leftTab, true);
    render();
    writeUrl(false);
    if (
      state.selectedFile &&
      (!state.fileQueue || state.fileQueue.path !== state.selectedFile)
    ) {
      openFileQueue(state.selectedFile, { fromUrl: true });
    }
    renderUserDrawer();
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

  function scrollSidebarToSelection() {
    if (!alignSidebar) return;
    const gid = state.selectedGroupId;
    const prn = state.selectedPr;
    requestAnimationFrame(() => {
      try {
        if (state.leftTab === "queue" && queueVirtualizer) {
          const rows = queueRows();
          const idx = rows.findIndex(
            (r) => r.kind === "group" && r.group && r.group.group_id === gid
          );
          if (idx >= 0) queueVirtualizer.scrollToIndex(idx, { align: "center" });
        } else if (state.leftTab === "groups" && groupVirtualizer) {
          const groups = sortedGroups();
          const idx = groups.findIndex((g) => g.group_id === gid);
          if (idx >= 0) groupVirtualizer.scrollToIndex(idx, { align: "center" });
        } else if (state.leftTab === "allprs" && allPrVirtualizer && prn) {
          const prs = sortedAllPrs();
          const idx = prs.findIndex((p) => p.number === prn);
          if (idx >= 0) allPrVirtualizer.scrollToIndex(idx, { align: "center" });
        }
      } catch (_) {}
      alignSidebar = false;
    });
  }

  function renderLeftHeader() {
    const hdr = $("groupListHeader");
    const q = state.queue || {};
    const c = q.counts || {};
    if (state.leftTab === "allprs") {
      const n = visibleAllPrs().length;
      hdr.textContent = hasListFilter() ? n + " of " + state.prs.length + " PRs" : state.prs.length + " PRs";
    } else if (state.leftTab === "queue") {
      hdr.textContent =
        (c.needs_you || 0) + " need you · " +
        (c.hardware || 0) + " hw · " +
        (c.upgrade || 0) + " upgrade · " +
        (c.hotspots || 0) + " hotspots";
      if (hasListFilter()) hdr.textContent += " · filtered";
    } else {
      const n = visibleGroups().length;
      hdr.textContent = hasListFilter()
        ? n + " of " + state.groups.length + " groups"
        : state.groups.length + " groups · " + state.prs.length + " PRs";
    }
  }

  function renderGroupList() {
    const root = $("groupList");
    const groups = visibleGroups();
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
    scrollSidebarToSelection();
  }

  function paintGroupVirtual() {
    const root = $("groupList");
    const groups = visibleGroups();
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
        <span class="pill ${escapeHtml(decision)}" title="${escapeHtml(labelHelp(decision))}">${escapeHtml(decision)}</span>
        ${
          rule
            ? `<span class="pill ${escapeHtml(rule.decision)}" title="${escapeHtml(labelHelp(ruleLabel(rule.decision)) || labelHelp(rule.decision))}">${escapeHtml(
                ruleLabel(rule.decision)
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
    const prs = visibleAllPrs();
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
    scrollSidebarToSelection();
  }

  function paintAllPrVirtual() {
    const root = $("allPrList");
    const prs = visibleAllPrs();
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

  function setLeftTab(tab, fromUrl) {
    if (tab !== state.leftTab) alignSidebar = true;
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
    if (!fromUrl) writeUrl(true);
  }

  function groupById(id) {
    return state.groups.find((g) => g.group_id === id) || null;
  }

  const FILTER_CHIPS = [
    "related-theme",
    "duplicate",
    "unique",
    "needs-look",
    "hardware",
    "update-path",
    "docs",
    "cosmetic",
    "junk",
    "blessed",
    "unreviewed",
  ];

  function hasListFilter() {
    return !!(state.filterQuery || "").trim() || !!state.filterLabel;
  }

  function groupMatches(g) {
    if (!g) return false;
    const rule = ruleFor(g.group_id);
    const label = state.filterLabel;
    if (label === "unreviewed") {
      if (rule) return false;
    } else if (label === "blessed") {
      if (!rule || rule.decision !== "approve") return false;
    } else if (label === "rejected") {
      if (!rule || rule.decision !== "reject") return false;
    } else if (label === "hardware") {
      const marked = rule && rule.decision === "hardware";
      if (g.card_class !== "hardware" && !marked) return false;
    } else if (label === "unique" || label === "duplicate" || label === "related-theme") {
      if ((g.suggested_decision || "unique") !== label) return false;
    } else if (label) {
      if ((g.card_class || "needs-look") !== label) return false;
    }
    const q = (state.filterQuery || "").trim().toLowerCase();
    if (!q) return true;
    if ((g.group_id || "").toLowerCase().includes(q)) return true;
    if ((g.title_variants || []).some((t) => (t || "").toLowerCase().includes(q))) return true;
    const nums = g.pr_numbers || [];
    if (nums.some((n) => String(n).includes(q))) return true;
    for (const n of nums) {
      const pr = prByNumber(n);
      if (!pr) continue;
      if ((pr.user || "").toLowerCase().includes(q)) return true;
      if ((pr.title || "").toLowerCase().includes(q)) return true;
      if ((pr.paths || []).some((p) => (p || "").toLowerCase().includes(q))) return true;
    }
    return false;
  }

  function prMatches(pr) {
    if (!pr) return false;
    if (state.filterLabel) {
      const g = groupById(pr.group_id);
      if (!g || !groupMatches(g)) return false;
      const q = (state.filterQuery || "").trim().toLowerCase();
      if (!q) return true;
    }
    const q = (state.filterQuery || "").trim().toLowerCase();
    if (!q) return !state.filterLabel || groupMatches(groupById(pr.group_id) || { group_id: pr.group_id, pr_numbers: [pr.number], title_variants: [pr.title], suggested_decision: "unique", card_class: "needs-look" });
    if (String(pr.number).includes(q)) return true;
    if ((pr.title || "").toLowerCase().includes(q)) return true;
    if ((pr.user || "").toLowerCase().includes(q)) return true;
    if ((pr.group_id || "").toLowerCase().includes(q)) return true;
    if ((pr.paths || []).some((p) => (p || "").toLowerCase().includes(q))) return true;
    if (state.filterLabel) {
      const g = groupById(pr.group_id);
      return !!(g && groupMatches(g));
    }
    return false;
  }

  function visibleGroups() {
    return sortedGroups().filter(groupMatches);
  }

  function visibleAllPrs() {
    return sortedAllPrs().filter(prMatches);
  }

  function applyListFilter() {
    queueRowsCache = null;
    const inp = $("listFilter");
    if (inp && inp.value !== state.filterQuery) inp.value = state.filterQuery;
    paintLabelFilters();
    if (state.leftTab === "queue") renderQueue();
    else if (state.leftTab === "groups") renderGroupList();
    else renderAllPrList();
    writeUrl(false);
  }

  function paintLabelFilters() {
    const root = $("labelFilters");
    if (!root) return;
    if (!root.dataset.ready) {
      root.innerHTML = "";
      FILTER_CHIPS.forEach((name) => {
        const el = makePill(name);
        el.dataset.label = name;
        el.addEventListener("click", () => {
          state.filterLabel = state.filterLabel === name ? "" : name;
          applyListFilter();
        });
        root.appendChild(el);
      });
      root.dataset.ready = "1";
    }
    [...root.querySelectorAll(".pill")].forEach((el) => {
      el.classList.toggle("on", el.dataset.label === state.filterLabel);
    });
  }

  function queueRows() {
    if (queueRowsCache) return queueRowsCache;
    const q = state.queue || {};
    const rows = [];
    const piles = [
      ["Needs you", q.needs_you || []],
      ["Needs hardware", q.hardware || []],
      ["Can break upgrade", q.upgrade || []],
      ["Known shape", q.known || []],
      ["Junk", q.junk || []],
    ];
    const filtering = hasListFilter();
    for (const [label, ids] of piles) {
      const groups = [];
      for (const id of ids) {
        const g = groupById(id);
        if (g && groupMatches(g)) groups.push(g);
      }
      if (filtering && !groups.length) continue;
      rows.push({ kind: "head", key: "hd:" + label, label: label, count: groups.length, size: 34 });
      if (!groups.length) {
        rows.push({ kind: "empty", key: "e:" + label, size: 30 });
        continue;
      }
      for (const g of groups) {
        rows.push({ kind: "group", key: "g:" + g.group_id, group: g, size: 78 });
      }
    }
    const qtext = (state.filterQuery || "").trim().toLowerCase();
    const spots = (q.hotspots || []).filter((hs) => {
      if (state.filterLabel) return false;
      if (!qtext) return true;
      return (hs.path || "").toLowerCase().includes(qtext);
    });
    if (!filtering || spots.length) {
      rows.push({ kind: "head", key: "hd:hotspots", label: "File hotspots", count: spots.length, size: 34 });
      for (const hs of spots) {
        rows.push({ kind: "hotspot", key: "h:" + hs.path, hotspot: hs, size: 40 });
      }
    }
    queueRowsCache = rows;
    return rows;
  }

  function buildQueueItem(row) {
    if (row.kind === "head") {
      const h = document.createElement("div");
      h.className = "pile-head";
      h.textContent = row.label + " · " + row.count;
      return h;
    }
    if (row.kind === "empty") {
      const empty = document.createElement("div");
      empty.className = "muted pile-empty";
      empty.textContent = "none";
      return empty;
    }
    if (row.kind === "group") {
      return buildGroupCard(row.group);
    }
    const hs = row.hotspot;
    const el = document.createElement("div");
    el.className = "hotspot-row";
    el.innerHTML =
      '<span class="mono">' +
      escapeHtml(hs.path) +
      '</span><span class="muted">' +
      hs.pr_count +
      " PRs</span>";
    el.addEventListener("click", () => {
      openFileQueue(hs.path);
    });
    return el;
  }

  function renderQueue() {
    const root = $("queueList");
    const rows = queueRows();
    renderLeftHeader();

    if (!rows.length) {
      if (queueVirtualizer) {
        try { queueVirtualizer._willUpdate = () => {}; } catch (_) {}
        queueVirtualizer = null;
      }
      root.innerHTML = '<div class="empty">No queue — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      root.innerHTML = "";
      for (const row of rows) root.appendChild(buildQueueItem(row));
      return;
    }

    if (!queueVirtualizer) {
      queueVirtualizer = makeVirtualizer(
        root,
        rows.length,
        (i) => (queueRows()[i] && queueRows()[i].size) || 40,
        8,
        paintQueueVirtual
      );
      queueVirtualizer._didMount();
    } else {
      queueVirtualizer.setOptions({
        ...queueVirtualizer.options,
        count: rows.length,
        getScrollElement: () => root,
        estimateSize: (i) => (queueRows()[i] && queueRows()[i].size) || 40,
        overscan: 8,
        onChange: () => paintQueueVirtual(),
      });
    }
    queueVirtualizer._willUpdate();
    paintQueueVirtual();
    scrollSidebarToSelection();
  }

  function paintQueueVirtual() {
    const root = $("queueList");
    const rows = queueRows();
    if (!queueVirtualizer || !rows.length) return;
    const items = queueVirtualizer.getVirtualItems();
    const total = queueVirtualizer.getTotalSize();
    let inner = root.querySelector(".virt-inner");
    if (!inner) {
      root.innerHTML = "";
      inner = document.createElement("div");
      inner.className = "virt-inner";
      root.appendChild(inner);
    }
    inner.style.height = total + "px";
    const keep = new Set(items.map((vi) => rows[vi.index] && rows[vi.index].key));
    [...inner.querySelectorAll(".virt-item")].forEach((el) => {
      if (!keep.has(el.dataset.key)) el.remove();
    });
    for (const vi of items) {
      const row = rows[vi.index];
      if (!row) continue;
      let el = [...inner.querySelectorAll(".virt-item")].find((n) => n.dataset.key === row.key);
      if (!el) {
        el = document.createElement("div");
        el.className = "virt-item";
        el.dataset.key = row.key;
        el.dataset.index = String(vi.index);
        el.appendChild(buildQueueItem(row));
        inner.appendChild(el);
      } else if (row.kind === "group") {
        const card = el.firstChild;
        if (card) {
          card.className =
            "group-card" + (row.group.group_id === state.selectedGroupId ? " selected" : "");
        }
      }
      el.style.transform = "translateY(" + vi.start + "px)";
      el.style.height = vi.size + "px";
    }
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
    if (state.related && state.related.frozen) {
      renderRelated();
      return;
    }
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
      if (data && data.frozen) data.frozen = true;
      state.related = data;
      if (data && (data.reason || "").indexOf("credits") >= 0) state.related.frozen = true;
      if (data && data.enabled === false) state.related.frozen = true;
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
      root.textContent = data.reason || (data.enabled ? "No near-patch neighbors." : "Embedding key not set.");
      return;
    }
    const items = data.related || [];
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

  async function openFileQueue(path, opts) {
    const fromUrl = !!(opts && opts.fromUrl);
    state.selectedFile = path;
    state.fileQueue = { path: path, prs: [], loading: true, same_patch: [] };
    const extra = $("fileQueueBlock");
    if (!fromUrl && extra && extra.tagName === "DETAILS") extra.open = true;
    writeUrl(!fromUrl);
    renderFileQueue();
    const g = state.groups.find((x) => x.group_id === state.selectedGroupId);
    if (g) renderDiffs(g, { scroll: true });
    try {
      const data = await api("/api/file?path=" + encodeURIComponent(path));
      if (state.selectedFile !== path) return;
      state.fileQueue = data;
      renderFileQueue();
      const g2 = state.groups.find((x) => x.group_id === state.selectedGroupId);
      if (g2) renderDiffs(g2, { scroll: true });
    } catch (err) {
      if (state.selectedFile !== path) return;
      state.fileQueue = { path: path, prs: [], error: err.message };
      renderFileQueue();
    }
  }

  function renderFileQueue() {
    const root = $("fileQueue");
    if (!root) return;
    const toggle = $("fileQueueToggle");
    const fq = state.fileQueue;
    const count = (fq && (fq.pr_count || (fq.prs || []).length)) || 0;
    if (toggle) {
      toggle.textContent = count
        ? "Also on this file · " + count
        : "Also on this file";
    }
    const block = $("fileQueueBlock");
    if (block && block.tagName === "DETAILS" && !block.open) {
      /* keep collapsed; still refresh label */
    }
    const path = state.selectedFile;
    if (!path) {
      root.className = "file-queue muted";
      root.textContent = "Pick a file in the table above.";
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
    const same = (fq && fq.same_patch) || [];
    const sameN = same.reduce((n, c) => n + (c.count || 0), 0);
    head.textContent =
      path +
      " · " +
      ((fq && fq.pr_count) || items.length) +
      " PRs" +
      extra +
      (same.length ? " · " + same.length + " identical hunks (" + sameN + " PRs)" : "");
    root.appendChild(head);
    if (same.length) {
      const box = document.createElement("div");
      box.className = "same-patch-list";
      same.forEach((c) => {
        const row = document.createElement("div");
        row.className = "muted";
        row.textContent =
          "same hunk ×" +
          c.count +
          "  #" +
          (c.pr_numbers || []).join(" #");
        box.appendChild(row);
      });
      root.appendChild(box);
    }
    items.slice(0, 20).forEach((pr) => {
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

  function prsByUser(name) {
    const key = (name || "").toLowerCase();
    return state.prs
      .filter((p) => (p.user || "").toLowerCase() === key)
      .sort((a, b) => (b.number || 0) - (a.number || 0));
  }

  function openUserDrawer(name) {
    if (!name || name === "?") return;
    state.selectedUser = name;
    renderUserDrawer();
    writeUrl(true);
  }

  function closeUserDrawer() {
    if (!state.selectedUser) return;
    state.selectedUser = null;
    renderUserDrawer();
    writeUrl(true);
  }

  function renderUserDrawer() {
    const pane = $("userDrawer");
    const layout = document.querySelector(".layout");
    if (!pane || !layout) return;
    const user = state.selectedUser;
    layout.classList.toggle("drawer-open", !!user);
    pane.classList.toggle("hidden", !user);
    pane.setAttribute("aria-hidden", user ? "false" : "true");
    if (!user) return;
    const prs = prsByUser(user);
    const byG = new Map();
    for (const pr of prs) {
      const gid = pr.group_id || "?";
      if (!byG.has(gid)) byG.set(gid, []);
      byG.get(gid).push(pr);
    }
    const needs = new Set((state.queue && state.queue.needs_you) || []);
    const groups = [...byG.entries()].sort((a, b) => {
      if (b[1].length !== a[1].length) return b[1].length - a[1].length;
      return a[0].localeCompare(b[0]);
    });
    const inQueue = groups.filter(([gid]) => needs.has(gid)).length;
    $("userDrawerName").textContent = user;
    $("userDrawerMeta").textContent =
      prs.length +
      " open PRs · " +
      groups.length +
      " shape" +
      (groups.length === 1 ? "" : "s") +
      (inQueue ? " · " + inQueue + " in queue" : "");
    const gh = $("userDrawerGh");
    gh.href = "https://github.com/" + encodeURIComponent(user);
    const list = $("userDrawerList");
    list.innerHTML = "";
    if (!prs.length) {
      const empty = document.createElement("div");
      empty.className = "muted pile-empty";
      empty.textContent = "No open PRs in the cache.";
      list.appendChild(empty);
      return;
    }
    for (const [gid, members] of groups) {
      const g = groupById(gid);
      const head = document.createElement("button");
      head.type = "button";
      head.className = "pile-head user-shape";
      const n = g ? (g.pr_numbers || []).length : members.length;
      const dec = (g && g.suggested_decision) || "";
      head.textContent =
        gid +
        " · " +
        members.length +
        (n !== members.length ? " of " + n : "") +
        (dec ? " · " + dec : "") +
        (needs.has(gid) ? " · queue" : "");
      head.addEventListener("click", () => {
        if (gid && gid !== "?") state.selectedGroupId = gid;
        state.selectedPr = members[0].number;
        state.selectedFile = null;
        render();
      });
      list.appendChild(head);
      members.forEach((pr) => {
        const row = document.createElement("div");
        row.className = "pr-row" + (state.selectedPr === pr.number ? " selected" : "");
        row.innerHTML =
          '<span class="num">#' +
          pr.number +
          '</span><span class="meta" title="' +
          escapeHtml(pr.title || "") +
          '">' +
          escapeHtml(pr.title || "") +
          "</span>";
        row.addEventListener("click", () => {
          if (pr.group_id) state.selectedGroupId = pr.group_id;
          state.selectedPr = pr.number;
          render();
        });
        list.appendChild(row);
      });
    }
  }

  function bindUserLink(el, name) {
    if (!el || !name || name === "?") return;
    el.classList.add("user-link");
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openUserDrawer(name);
    });
  }

  const LABEL_HELP = {
    unique: "one PR",
    duplicate: "same files, same hunks",
    "related-theme": "same files, different hunks",
    "needs-look": "unclassified — read it",
    hardware: "hardware keywords",
    "update-path": "upgrade / migrate",
    docs: "docs only",
    cosmetic: "theme / CSS",
    junk: "noise",
    blessed: "you trusted this",
    rejected: "you rejected this",
    approve: "you trusted this",
    reject: "you rejected this",
    upgrade: "you marked upgrade risk",
  };
  const LABEL_KEY_ORDER = [
    "related-theme",
    "duplicate",
    "unique",
    "needs-look",
    "hardware",
    "update-path",
    "docs",
    "cosmetic",
    "junk",
    "blessed",
    "rejected",
    "upgrade",
  ];

  function labelHelp(name) {
    return LABEL_HELP[name] || "";
  }

  function ruleLabel(decision) {
    if (decision === "approve") return "blessed";
    if (decision === "reject") return "rejected";
    return decision || "";
  }

  function makePill(text, cls) {
    const el = document.createElement("span");
    el.className = "pill " + (cls || text);
    el.textContent = text;
    const tip = labelHelp(cls || text) || labelHelp(text);
    if (tip) el.title = tip;
    return el;
  }

  function makeLabelRow(name, cls) {
    const row = document.createElement("div");
    row.className = "label-row";
    const key = cls || name;
    row.appendChild(makePill(name, key));
    const gloss = document.createElement("span");
    gloss.className = "label-gloss";
    gloss.textContent = labelHelp(key) || labelHelp(name);
    row.appendChild(gloss);
    return row;
  }

  function fillLabelKey() {
    const root = $("labelKeyAll");
    if (!root || root.dataset.ready) return;
    LABEL_KEY_ORDER.forEach((name) => root.appendChild(makeLabelRow(name)));
    root.dataset.ready = "1";
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

    const rule = ruleFor(g.group_id);
    const n = (g.pr_numbers || []).length;
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const firstTitle = (g.title_variants && g.title_variants[0]) || "";
    const cls = g.card_class || "needs-look";
    const decision = g.suggested_decision || "unique";

    if (pr) {
      $("detailKicker").textContent = g.group_id + " · " + n + " PRs in this shape";
      $("detailTitle").innerHTML =
        '<a class="pr-link" href="' +
        escapeHtml(prUrl(pr)) +
        '" target="_blank" rel="noopener">#' +
        pr.number +
        "</a> " +
        escapeHtml(pr.title || "");
      const meta = $("detailMeta");
      meta.textContent = "";
      if (pr.user) {
        const ub = document.createElement("button");
        ub.type = "button";
        ub.className = "user-link";
        ub.textContent = pr.user;
        bindUserLink(ub, pr.user);
        meta.appendChild(ub);
      }
      meta.appendChild(
        document.createTextNode(rule ? " · marked " + rule.decision : " · unreviewed")
      );
    } else {
      $("detailKicker").textContent = g.group_id + " · " + n + (n === 1 ? " PR" : " PRs");
      $("detailTitle").textContent = firstTitle || g.group_id;
      $("detailMeta").textContent = rule
        ? "marked " + rule.decision
        : n >= 2
          ? "same file-set. Bless once if the shape is safe."
          : "singleton. Read it or wait for a twin.";
    }

    const pills = $("detailPills");
    pills.innerHTML = "";
    pills.appendChild(makeLabelRow(cls));
    pills.appendChild(makeLabelRow(decision));
    if (rule) {
      pills.appendChild(makeLabelRow(ruleLabel(rule.decision), rule.decision === "approve" ? "blessed" : rule.decision));
    }
    fillLabelKey();
    const titles = (g.title_variants || []).filter((t) => t && t !== firstTitle);
    if (titles.length) {
      $("detailMeta").textContent += " · " + titles.length + " other titles";
    }

    const mh = $("membersHead");
    if (mh) mh.textContent = "PRs in this shape · " + n;

    renderMembers(g);
    renderPrBodies(g);
    renderOverlap(g);
    renderDiffs(g);
    renderFileQueue();
    const qPr = state.selectedPr || (g.pr_numbers || [])[0];
    loadRelated(qPr, state.selectedFile || null);
  }

  let bodyGen = 0;
  const bodyCache = {};

  async function loadPrBody(num) {
    if (bodyCache[num]) return bodyCache[num];
    const repo = state.repo || "omacom/omarchy";
    const data = await api(
      "/api/pr?number=" + encodeURIComponent(num) + "&repo=" + encodeURIComponent(repo)
    );
    bodyCache[num] = data;
    return data;
  }

  let lastBodyPr = null;

  function renderPrBodies(g) {
    const root = $("prBody");
    const head = $("prBodyHead");
    const block = $("prBodyBlock");
    if (!root) return;
    const selected = state.selectedPr;
    if (!selected) {
      lastBodyPr = null;
      if (block) block.open = false;
      if (head) head.textContent = "Description";
      root.className = "pr-body muted";
      root.textContent = "Click a PR above.";
      return;
    }
    if (head) head.textContent = "Description · #" + selected;
    if (block && lastBodyPr !== selected) block.open = true;
    lastBodyPr = selected;
    const gen = ++bodyGen;
    root.className = "pr-body muted";
    root.textContent = "Loading description…";
    loadPrBody(selected)
      .catch((err) => ({ number: selected, error: err.message }))
      .then((it) => {
        if (gen !== bodyGen) return;
        root.className = "pr-body";
        root.innerHTML = "";
        const box = document.createElement("div");
        box.className = "pr-body-item";
        const who = document.createElement("div");
        who.className = "who muted";
        const pr = prByNumber(it.number || selected) || it;
        who.textContent =
          "#" +
          selected +
          " " +
          (pr.user || it.user || "") +
          (it.error ? " · " + it.error : "");
        const text = document.createElement("div");
        text.className = "pr-body-text";
        text.textContent = (it.body || "").trim() || "(no description)";
        box.appendChild(who);
        box.appendChild(text);
        root.appendChild(box);
      });
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
      <button type="button" class="user muted">${escapeHtml(pr.user || "")}</button>
      <span class="${labelClass}">${escapeHtml(pr.label || "")}</span>`;
    bindUserLink(li.querySelector(".user"), pr.user);
    li.addEventListener("click", (ev) => {
      if (ev.target.tagName === "A") return;
      state.selectedPr = num;
      renderDetail();
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


  function prUrl(pr) {
    if (pr && pr.html_url) return pr.html_url;
    const repo = state.repo || "omacom/omarchy";
    return "https://github.com/" + repo + "/pull/" + (pr && pr.number ? pr.number : "");
  }

  function colorizeDiff(patch) {
    const hunkRe = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
    let oldN = 0;
    let newN = 0;
    const rows = [];
    function row(oldL, newL, text, cls) {
      rows.push(
        '<div class="diff-line ' +
          cls +
          '"><span class="ln">' +
          (oldL || "") +
          '</span><span class="ln">' +
          (newL || "") +
          '</span><span class="src">' +
          escapeHtml(text) +
          "</span></div>"
      );
    }
    String(patch || "").split("\n").forEach((line) => {
      const m = line.match(hunkRe);
      if (m) {
        oldN = parseInt(m[1], 10);
        newN = parseInt(m[2], 10);
        row("", "", line, "hunk");
        return;
      }
      if (
        line.startsWith("+++") ||
        line.startsWith("---") ||
        line.startsWith("diff ") ||
        line.startsWith("index ")
      ) {
        row("", "", line, "meta");
        return;
      }
      if (line.startsWith("+")) {
        row("", String(newN), line, "add");
        newN += 1;
        return;
      }
      if (line.startsWith("-")) {
        row(String(oldN), "", line, "del");
        oldN += 1;
        return;
      }
      row(oldN ? String(oldN) : "", newN ? String(newN) : "", line, "ctx");
      if (oldN) oldN += 1;
      if (newN) newN += 1;
    });
    return rows.join("");
  }

  let diffGen = 0;

  let lastScrolledFile = null;

  function scrollToDiff() {
    const pane = document.querySelector(".pane.center");
    const block = $("diffBlock");
    if (!pane || !block) return;
    const path = state.selectedFile;
    lastScrolledFile = path;
    requestAnimationFrame(() => {
      const paneRect = pane.getBoundingClientRect();
      const blockRect = block.getBoundingClientRect();
      pane.scrollTo({
        top: pane.scrollTop + blockRect.top - paneRect.top - 8,
        behavior: "smooth",
      });
    });
  }

  async function renderDiffs(g, opts) {
    const root = $("diffPanels");
    const path = state.selectedFile;
    const shouldScroll = !!(opts && opts.scroll);
    if (!path) {
      root.innerHTML = '<div class="muted diff-hint">Pick a file above to compare hunks.</div>';
      return;
    }
    const groupSet = new Set(g.pr_numbers || []);
    const fqNums = ((state.fileQueue && state.fileQueue.prs) || [])
      .map((p) => p.number)
      .filter(Boolean);
    const groupOnFile = fqNums.filter((n) => groupSet.has(n));
    const nums = (groupOnFile.length ? groupOnFile : (g.pr_numbers || [])).slice(0, 8);
    if (!nums.length) {
      root.innerHTML =
        '<div class="muted diff-hint">No patches for ' +
        escapeHtml(path) +
        ".</div>";
      return;
    }
    const gen = ++diffGen;
    const meta = $("diffMeta");
    if (meta) meta.textContent = path + " · " + nums.length + " PRs in this group";
    if (shouldScroll) scrollToDiff();
    if (!root.querySelector(".diff-panel")) {
      root.innerHTML = '<div class="muted diff-hint">Loading patches…</div>';
    }
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
      const href = prUrl(pr);
      const shown = patch && patch.length > 4000 ? patch.slice(0, 4000) + "\n… truncated" : patch;
      panel.innerHTML =
        '<div class="diff-panel-head">' +
        '<a class="pr-link" href="' +
        escapeHtml(href) +
        '" target="_blank" rel="noopener">#' +
        pr.number +
        "</a> " +
        '<button type="button" class="user-link" data-user="' +
        escapeHtml(pr.user || "") +
        '">' +
        escapeHtml(pr.user || "") +
        "</button>" +
        '<span class="diff-tag ' +
        (same ? "same" : "diff") +
        '">' +
        (patch ? (same ? "same hunk" : "different") : "no patch") +
        "</span></div>" +
        '<div class="diff-code">' +
        colorizeDiff(shown || "(empty patch)") +
        "</div>";
      root.appendChild(panel);
      bindUserLink(panel.querySelector("[data-user]"), pr.user);
    });
    if (shouldScroll) scrollToDiff();
    if (!withPatch.length) {
      const hint = document.createElement("div");
      hint.className = "muted diff-hint";
      hint.textContent = "Cached files have no patch for this path (binary or too large for GitHub).";
      root.prepend(hint);
    }
    const byPatch = {};
    items.forEach((it) => {
      const patch = it.patch || "";
      if (!patch) return;
      if (!byPatch[patch]) byPatch[patch] = [];
      byPatch[patch].push(it.number);
    });
    const same = Object.keys(byPatch)
      .map((k) => ({ pr_numbers: byPatch[k], count: byPatch[k].length }))
      .filter((c) => c.count >= 2)
      .sort((a, b) => b.count - a.count);
    if (state.fileQueue && state.fileQueue.path === path) {
      state.fileQueue.same_patch = same;
      renderFileQueue();
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
    writeUrl(true);
    if (state.selectedUser) renderUserDrawer();
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
  $("tabQueue").addEventListener("click", () => setLeftTab("queue"));
  $("tabGroups").addEventListener("click", () => setLeftTab("groups"));
  $("tabAllPrs").addEventListener("click", () => setLeftTab("allprs"));
  const userClose = $("userDrawerClose");
  if (userClose) userClose.addEventListener("click", closeUserDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.selectedUser) closeUserDrawer();
  });
  window.addEventListener("popstate", () => {
    if (writingUrl) return;
    alignSidebar = true;
    readUrl();
    setLeftTab(state.leftTab, true);
    renderDetail();
    if (state.selectedFile) openFileQueue(state.selectedFile, { fromUrl: true });
    renderUserDrawer();
  });

  readUrl();
  const filterInp = $("listFilter");
  if (filterInp) {
    filterInp.value = state.filterQuery || "";
    filterInp.addEventListener("input", () => {
      state.filterQuery = filterInp.value;
      applyListFilter();
    });
  }
  paintLabelFilters();
  loadState();
})();
