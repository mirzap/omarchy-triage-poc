/* Omarchy triage dashboard — local-first vanilla JS + TanStack Virtual */
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
    rules: [],
    overlap: {},
    source: "",
    repo: "",
    selectedGroupId: null,
    selectedPr: null,
    selectedFile: null,
    storeVersion: 0,
    snapshotVersion: 0,
    sync: null,
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
    queuePile: "needs_you",
    examinedMembers: new Set(),
  };

  const QUEUE_PILES = [
    { id: "needs_you", label: "Needs you", hint: "No decision yet — the actual triage work" },
    { id: "hardware", label: "Hardware", hint: "You marked Needs hardware" },
    { id: "upgrade", label: "Upgrade", hint: "You marked Can break upgrade" },
    { id: "known", label: "Known", hint: "Explicitly reviewed revisions" },
    { id: "junk", label: "Junk", hint: "Explicitly rejected revisions" },
    { id: "hotspots", label: "Hotspots", hint: "Files touched by 20+ open PRs" },
  ];

  let groupVirtualizer = null;
  let allPrVirtualizer = null;
  let queueVirtualizer = null;
  let memberVirtualizer = null;
  const virtualizerCleanup = { group: null, all: null, queue: null, member: null };
  let queueRowsCache = null;
  let fetchPollTimer = null;
  let stateLoadGen = 0;
  let snapshotGen = 0;
  let fileQueueGen = 0;
  let relatedGen = 0;
  let bodyGen = 0;
  let overlapGen = 0;
  let diffGen = 0;
  let diffScrollSerial = 0;
  let pendingDiffScroll = null;
  let pendingDiffScrollFrame = null;
  let lastBodyPr = null;
  const bodyCache = {};
  const resourceRequests = new Map();
  const patchPanelState = new WeakMap();
  const indexes = { groups: new Map(), prs: new Map(), rules: new Map() };
  const selectorCache = new Map();
  const decisionRetries = new Map();
  let csrfToken = "";
  let sessionPromise = null;
  let enrichmentRequest = null;

  const $ = (id) => document.getElementById(id);

  let writingUrl = false;
  let alignSidebar = true;

  const URL_DEFAULTS = {
    leftTab: "queue", selectedGroupId: null, selectedPr: null, selectedFile: null,
    selectedUser: null, filterQuery: "", filterLabel: "", queuePile: "needs_you",
  };

  function readUrl() {
    Object.assign(state, URL_DEFAULTS);
    const q = new URLSearchParams(location.search);
    const tab = q.get("tab");
    if (tab === "queue" || tab === "groups" || tab === "allprs") state.leftTab = tab;
    const gid = q.get("group");
    state.selectedGroupId = gid || null;
    const pr = q.get("pr");
    if (pr && /^\d+$/.test(pr)) state.selectedPr = parseInt(pr, 10);
    else state.selectedPr = null;
    const file = q.get("file");
    state.selectedFile = file || null;
    const user = q.get("user");
    state.selectedUser = user || null;
    state.filterQuery = q.get("q") || "";
    state.filterLabel = q.get("label") || "";
    const pile = q.get("pile");
    if (pile && QUEUE_PILES.some((p) => p.id === pile)) state.queuePile = pile;
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
    if ((state.leftTab || "queue") === "queue" && state.queuePile) {
      q.set("pile", state.queuePile);
    }
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

  function renderSyncMeta() {
    const root = $("syncMeta");
    if (!root) return;
    const sync = state.sync;
    if (!sync) {
      root.textContent = state.source === "fixtures" ? "offline fixture workspace" : "no cached sync metadata";
      return;
    }
    const when = sync.fetched_at ? new Date(sync.fetched_at).toLocaleString() : "time unknown";
    const open = sync.open_count == null ? "?" : sync.open_count;
    const downloaded = sync.selected_count == null ? "?" : sync.selected_count;
    root.textContent = "cached " + when + " · " + open + " open PRs · file evidence " +
      downloaded + "/" + open + (sync.evidence_complete ? " complete" : " incomplete") +
      (sync.list_complete === false ? " · open list partial" : " · all-open list complete") +
      (sync.limited ? " · file cap applied" : "") +
      (sync.cache_status ? " · " + sync.cache_status : "");
  }

  async function bootstrapSession(force) {
    if (force) {
      csrfToken = "";
      sessionPromise = null;
    }
    if (csrfToken) return csrfToken;
    if (!sessionPromise) {
      sessionPromise = fetch("/api/session", { headers: { Accept: "application/json" } })
        .then(async (res) => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok || !data.csrf_token) throw new Error(data.error || "session bootstrap failed");
          csrfToken = data.csrf_token;
          return csrfToken;
        })
        .finally(() => { sessionPromise = null; });
    }
    return sessionPromise;
  }

  async function api(path, opts) {
    const options = { ...(opts || {}) };
    const method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      const token = await bootstrapSession(false);
      options.headers = { ...(options.headers || {}), "Content-Type": "application/json",
                          "X-CSRF-Token": token };
    }
    const res = await fetch(path, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || res.statusText || "request failed");
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function requestOnce(kind, identity, path) {
    const key = kind + "|" + identity;
    const existing = resourceRequests.get(key);
    if (existing) return existing.promise;
    for (const [priorKey, entry] of resourceRequests.entries()) {
      if (priorKey.startsWith(kind + "|")) {
        entry.controller.abort();
        resourceRequests.delete(priorKey);
      }
    }
    const controller = new AbortController();
    const promise = api(path, { signal: controller.signal })
      .catch((error) => {
        resourceRequests.delete(key);
        throw error;
      });
    resourceRequests.set(key, { controller, promise });
    return promise;
  }

  function rebuildIndexes() {
    indexes.groups = new Map(state.groups.map((group) => [group.group_id, group]));
    indexes.prs = new Map(state.prs.map((pr) => [pr.number, pr]));
    indexes.rules = new Map(state.rules.map((rule) => [rule.group_id, rule]));
    selectorCache.clear();
  }

  function snapshotKey() {
    return [state.source || "", state.repo || "", state.storeVersion,
      state.snapshotVersion].join("|");
  }

  function invalidateSnapshotCaches() {
    snapshotGen += 1;
    fileQueueGen += 1;
    relatedGen += 1;
    bodyGen += 1;
    overlapGen += 1;
    diffGen += 1;
    state.fileQueue = null;
    state.related = null;
    state.relatedKey = "";
    state.examinedMembers = new Set();
    lastBodyPr = null;
    Object.keys(bodyCache).forEach((key) => delete bodyCache[key]);
    for (const entry of resourceRequests.values()) entry.controller.abort();
    resourceRequests.clear();
    if (enrichmentRequest) enrichmentRequest.controller.abort();
    enrichmentRequest = null;
    selectorCache.clear();
  }

  function applyState(data) {
    invalidateSnapshotCaches();
    state.groups = data.groups || [];
    state.prs = data.prs || [];
    state.rules = data.rules || [];
    state.overlap = data.overlap || {};
    state.source = data.source || "";
    state.repo = data.repo || "";
    state.queue = data.queue || {};
    state.new_pr_numbers = data.new_pr_numbers || [];
    state.storeVersion = Number(data.store_version || 0);
    state.snapshotVersion = Number(data.snapshot_version || 0);
    state.sync = data.sync || null;
    rebuildIndexes();
    queueRowsCache = null;
    if (data.source) {
      $("source").value = ["gh", "github"].includes(data.source) ? "gh" : data.source;
    }
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
    normalizeSelection();
    if (
      !state.selectedGroupId &&
      state.groups.length &&
      !new URLSearchParams(location.search).get("group") &&
      !state.selectedFile
    ) {
      const gs = sortedGroups();
      const pick =
        gs.find((g) => {
          const n = (g.pr_numbers || []).length;
          return n >= 2 && n <= 24;
        }) || gs.find((g) => (g.pr_numbers || []).length <= 24);
      state.selectedGroupId = pick ? pick.group_id : null;
      if (!new URLSearchParams(location.search).get("file")) state.selectedFile = null;
    }
    if (!new URLSearchParams(location.search).get("pile") && state.selectedGroupId) {
      state.queuePile = pileForGroup(state.selectedGroupId);
    }
    setLeftTab(state.leftTab, true);
    render();
    writeUrl(false);
    if (
      state.selectedFile &&
      (!state.fileQueue || state.fileQueue.path !== state.selectedFile)
    ) {
      if (state.selectedGroupId) openFileQueue(state.selectedFile, { fromUrl: true });
      else openHotspot(state.selectedFile, { fromUrl: true });
    }
    renderUserDrawer();
    renderSyncMeta();
  }

  function ruleFor(groupId) {
    return indexes.rules.get(groupId) || null;
  }

  function compareGroups(a, b) {
    const sa = (a.pr_numbers || []).length;
    const sb = (b.pr_numbers || []).length;
    if (sb !== sa) return sb - sa;
    const da = DECISION_ORDER[a.suggested_decision] ?? 9;
    const db = DECISION_ORDER[b.suggested_decision] ?? 9;
    if (da !== db) return da - db;
    const ga = String(a.group_id || "");
    const gb = String(b.group_id || "");
    return ga < gb ? -1 : ga > gb ? 1 : 0;
  }

  function sortedGroups() {
    return state.groups.slice().sort(compareGroups);
  }

  function sortedAllPrs() {
    return state.prs.slice().sort((a, b) => (b.number || 0) - (a.number || 0));
  }

  function prByNumber(n) {
    return indexes.prs.get(n) || null;
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

  function mountVirtualizer(slot, virtualizer) {
    disposeVirtualizer(slot);
    if (!virtualizer) return null;
    const cleanup = virtualizer._didMount();
    virtualizerCleanup[slot] = typeof cleanup === "function" ? cleanup : null;
    return virtualizer;
  }

  function disposeVirtualizer(slot) {
    const cleanup = virtualizerCleanup[slot];
    if (cleanup) cleanup();
    virtualizerCleanup[slot] = null;
    if (slot === "group") groupVirtualizer = null;
    else if (slot === "all") allPrVirtualizer = null;
    else if (slot === "queue") queueVirtualizer = null;
    else if (slot === "member") memberVirtualizer = null;
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
          const groups = visibleGroups();
          const idx = groups.findIndex((g) => g.group_id === gid);
          if (idx >= 0) groupVirtualizer.scrollToIndex(idx, { align: "center" });
        } else if (state.leftTab === "allprs" && allPrVirtualizer && prn) {
          const prs = visibleAllPrs();
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
    hdr.classList.remove("hidden");
    if (state.leftTab === "allprs") {
      const n = visibleAllPrs().length;
      hdr.textContent = hasListFilter() ? n + " of " + state.prs.length + " PRs" : state.prs.length + " PRs";
    } else if (state.leftTab === "queue") {
      paintPileNav();
      const shown = queueRows().filter((r) => r.kind === "group" || r.kind === "hotspot").length;
      const total = pileTotal(state.queuePile);
      if (hasListFilter()) {
        hdr.classList.remove("hidden");
        hdr.textContent = shown + " of " + total + " match";
      } else {
        hdr.textContent = "";
        hdr.classList.add("hidden");
      }
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
      disposeVirtualizer("group");
      root.innerHTML = hasListFilter()
        ? '<div class="empty">No groups match</div>'
        : '<div class="empty">No groups — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      // Fallback without virtualization
      root.innerHTML = "";
      for (const g of groups) root.appendChild(buildGroupCard(g));
      return;
    }

    if (!groupVirtualizer) {
      groupVirtualizer = mountVirtualizer(
        "group", makeVirtualizer(root, groups.length, () => 96, 8, paintGroupVirtual)
      );
    } else {
      groupVirtualizer.setOptions({
        ...groupVirtualizer.options,
        count: groups.length,
        getScrollElement: () => root,
        estimateSize: () => 96,
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
    const keep = new Set(
      items.map((i) => groups[i.index] && groups[i.index].group_id).filter(Boolean)
    );
    [...inner.querySelectorAll(".virt-item")].forEach((el) => {
      if (!keep.has(el.dataset.gid)) el.remove();
    });
    for (const vi of items) {
      const g = groups[vi.index];
      if (!g) continue;
      let el = inner.querySelector('.virt-item[data-gid="' + g.group_id + '"]');
      if (!el) {
        el = document.createElement("div");
        el.className = "virt-item";
        el.dataset.gid = g.group_id;
        el.appendChild(buildGroupCard(g));
        el.dataset.version = groupContentVersion(g);
        inner.appendChild(el);
      } else {
        if (el.dataset.version !== groupContentVersion(g)) {
          el.replaceChildren(buildGroupCard(g));
          el.dataset.version = groupContentVersion(g);
        } else if (el.firstChild) {
          el.firstChild.className = "group-card" +
            (g.group_id === state.selectedGroupId ? " selected" : "");
        }
      }
      el.dataset.index = String(vi.index);
      el.style.transform = "translateY(" + vi.start + "px)";
      el.style.height = vi.size + "px";
    }
  }

  function pillHtml(name, cls) {
    const key = cls || name;
    const tip = labelHelp(key) || labelHelp(name);
    return (
      '<span class="pill ' +
      escapeHtml(key) +
      '" title="' +
      escapeHtml(tip) +
      '">' +
      escapeHtml(name) +
      "</span>"
    );
  }

  function groupCardPills(g) {
    const rule = ruleFor(g.group_id);
    const cls = g.card_class || "needs-look";
    const decision = g.suggested_decision || "unique";
    const parts = [pillHtml(cls)];
    if (decision !== cls) parts.push(pillHtml(decision));
    if (rule) {
      const shown = ruleLabel(rule.decision);
      const key =
        rule.decision === "approve"
          ? "approved"
          : rule.decision === "reject"
            ? "rejected"
            : rule.decision;
      parts.push(pillHtml(shown, key));
    } else {
      parts.push('<span class="muted">unreviewed</span>');
    }
    return parts.join("");
  }

  function groupContentVersion(g) {
    const rule = ruleFor(g.group_id);
    return [state.storeVersion, g.snapshot_digest || "", g.evidence_complete ? 1 : 0,
      rule ? rule.decision : "", rule ? rule.decision_event_id || "" : ""].join(":");
  }

  function prContentVersion(pr) {
    return [state.storeVersion, pr.head_sha || "", pr.content_digest || "", pr.label || "",
      pr.group_id || ""].join(":");
  }

  function buildGroupCard(g) {
    const card = document.createElement("div");
    card.className =
      "group-card" + (g.group_id === state.selectedGroupId ? " selected" : "");
    const firstTitle = (g.title_variants && g.title_variants[0]) || "";
    card.innerHTML = `
      <div class="row">
        <span class="mono">${escapeHtml(g.group_id)}</span>
        <span class="muted">${(g.pr_numbers || []).length} PRs</span>
      </div>
      <div class="pills">${groupCardPills(g)}</div>
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
      disposeVirtualizer("all");
      root.innerHTML = hasListFilter()
        ? '<div class="empty">No PRs match</div>'
        : '<div class="empty">No PRs — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      root.innerHTML = "";
      for (const pr of prs) root.appendChild(buildPrRow(pr));
      return;
    }

    if (!allPrVirtualizer) {
      allPrVirtualizer = mountVirtualizer(
        "all", makeVirtualizer(root, prs.length, () => 36, 8, paintAllPrVirtual)
      );
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
    const keep = new Set(
      items.map((i) => prs[i.index] && String(prs[i.index].number)).filter(Boolean)
    );
    [...inner.querySelectorAll(".virt-item")].forEach((el) => {
      if (!keep.has(el.dataset.pr)) el.remove();
    });
    for (const vi of items) {
      const pr = prs[vi.index];
      if (!pr) continue;
      let el = inner.querySelector('.virt-item[data-pr="' + pr.number + '"]');
      if (!el) {
        el = document.createElement("div");
        el.className = "virt-item";
        el.dataset.pr = String(pr.number);
        el.appendChild(buildPrRow(pr));
        el.dataset.version = prContentVersion(pr);
        inner.appendChild(el);
      } else {
        if (el.dataset.version !== prContentVersion(pr)) {
          el.replaceChildren(buildPrRow(pr));
          el.dataset.version = prContentVersion(pr);
        } else if (el.firstChild) {
          el.firstChild.className = "pr-row" +
            (state.selectedPr === pr.number ? " selected" : "");
        }
      }
      el.dataset.index = String(vi.index);
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
    if (tab !== "queue") disposeVirtualizer("queue");
    if (tab !== "groups") disposeVirtualizer("group");
    if (tab !== "allprs") disposeVirtualizer("all");
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
    paintPileNav();
    renderLeftHeader();
    if (tab === "queue") renderQueue();
    else if (tab === "groups") renderGroupList();
    else renderAllPrList();
    if (!fromUrl) writeUrl(true);
  }

  function groupById(id) {
    return indexes.groups.get(id) || null;
  }

  function normalizeSelection() {
    let changed = false;
    let group = state.selectedGroupId ? groupById(state.selectedGroupId) : null;
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;

    if (state.selectedGroupId && !group) {
      state.selectedGroupId = null;
      state.selectedFile = null;
      group = null;
      changed = true;
    }
    if (state.selectedPr && !pr) {
      state.selectedPr = null;
      changed = true;
    } else if (!group && pr && !state.selectedFile && pr.group_id && groupById(pr.group_id)) {
      state.selectedGroupId = pr.group_id;
      group = groupById(pr.group_id);
      changed = true;
    }
    if (group && state.selectedPr && !(group.pr_numbers || []).includes(state.selectedPr)) {
      state.selectedPr = null;
      changed = true;
    }
    return changed;
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
    "approved",
    "unreviewed",
  ];

  function hasListFilter() {
    return !!(state.filterQuery || "").trim() || !!state.filterLabel;
  }

  function pileHas(pile, groupId) {
    const ids = (state.queue && state.queue[pile]) || [];
    return ids.indexOf(groupId) >= 0;
  }

  function groupMatches(g) {
    if (!g) return false;
    const rule = ruleFor(g.group_id);
    const label = state.filterLabel;
    if (label === "unreviewed") {
      if (rule) return false;
    } else if (label === "approved") {
      // Known pile: explicitly reviewed revisions. Not just a stored suggestion.
      const approved = rule && rule.decision === "approve";
      if (!approved && !pileHas("known", g.group_id)) return false;
    } else if (label === "junk") {
      // Explicit rejection is authoritative; keyword classification is only a visible filter.
      const rejected = rule && rule.decision === "reject";
      if (!rejected && !pileHas("junk", g.group_id) && (g.card_class || "") !== "junk") {
        return false;
      }
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
    const prNumberQuery = q.match(/^#?(\d+)$/);
    if (prNumberQuery) {
      return (g.pr_numbers || []).includes(parseInt(prNumberQuery[1], 10));
    }
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
    const prNumberQuery = q.match(/^#?(\d+)$/);
    if (prNumberQuery) return pr.number === parseInt(prNumberQuery[1], 10);
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
    const key = ["groups", snapshotKey(), state.filterQuery, state.filterLabel].join("|");
    if (!selectorCache.has(key)) selectorCache.set(key, sortedGroups().filter(groupMatches));
    return selectorCache.get(key);
  }

  function visibleAllPrs() {
    const key = ["prs", snapshotKey(), state.filterQuery, state.filterLabel].join("|");
    if (!selectorCache.has(key)) selectorCache.set(key, sortedAllPrs().filter(prMatches));
    return selectorCache.get(key);
  }

  function applyListFilter() {
    queueRowsCache = null;
    selectorCache.clear();
    const inp = $("listFilter");
    if (inp && inp.value !== state.filterQuery) inp.value = state.filterQuery;
    paintLabelFilters();
    paintPileNav();
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

  function pileForGroup(gid) {
    const q = state.queue || {};
    for (const p of QUEUE_PILES) {
      if (p.id === "hotspots") continue;
      if ((q[p.id] || []).indexOf(gid) >= 0) return p.id;
    }
    return "needs_you";
  }

  function pileItems(id) {
    const q = state.queue || {};
    if (id === "hotspots") {
      const qtext = (state.filterQuery || "").trim().toLowerCase();
      return (q.hotspots || []).filter((hs) => {
        if (state.filterLabel) return false;
        if (!qtext) return true;
        return (hs.path || "").toLowerCase().includes(qtext);
      });
    }
    const out = [];
    for (const gid of q[id] || []) {
      const g = groupById(gid);
      if (g && groupMatches(g)) out.push(g);
    }
    return out.sort(compareGroups);
  }

  function pileTotal(id) {
    const q = state.queue || {};
    if (id === "hotspots") return (q.hotspots || []).length;
    return (q[id] || []).length;
  }

  function paintPileNav() {
    const root = $("pileNav");
    if (!root) return;
    root.classList.toggle("hidden", state.leftTab !== "queue");
    if (state.leftTab !== "queue") return;
    if (!root.dataset.ready) {
      root.innerHTML = "";
      QUEUE_PILES.forEach((p) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pile-tab";
        btn.dataset.pile = p.id;
        btn.title = p.hint;
        btn.innerHTML =
          '<span class="pile-name">' +
          p.label +
          '</span><span class="pile-n">0</span>';
        btn.addEventListener("click", () => {
          if (state.queuePile === p.id) return;
          state.queuePile = p.id;
          queueRowsCache = null;
          alignSidebar = true;
          renderQueue();
          writeUrl(true);
        });
        root.appendChild(btn);
      });
      root.dataset.ready = "1";
    }
    [...root.querySelectorAll(".pile-tab")].forEach((btn) => {
      const id = btn.dataset.pile;
      const n = pileItems(id).length;
      btn.classList.toggle("active", id === state.queuePile);
      btn.classList.toggle("empty", n === 0);
      const el = btn.querySelector(".pile-n");
      if (el) el.textContent = String(n);
    });
  }

  function queueRows() {
    if (queueRowsCache) return queueRowsCache;
    const pile = state.queuePile || "needs_you";
    const rows = [];
    if (pile === "hotspots") {
      const spots = pileItems("hotspots");
      if (!spots.length) {
        rows.push({ kind: "empty", key: "e:hotspots", size: 36 });
      } else {
        for (const hs of spots) {
          rows.push({ kind: "hotspot", key: "h:" + hs.path, hotspot: hs, size: 40 });
        }
      }
    } else {
      const groups = pileItems(pile);
      if (!groups.length) {
        rows.push({ kind: "empty", key: "e:" + pile, size: 36 });
      } else {
        for (const g of groups) {
          rows.push({ kind: "group", key: "g:" + g.group_id, group: g, size: 96 });
        }
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
      empty.textContent = "Nothing in this pile";
      return empty;
    }
    if (row.kind === "group") {
      return buildGroupCard(row.group);
    }
    const hs = row.hotspot;
    const el = document.createElement("div");
    el.className =
      "hotspot-row" +
      (!state.selectedGroupId && state.selectedFile === hs.path ? " selected" : "");
    el.innerHTML =
      '<span class="mono">' +
      escapeHtml(hs.path) +
      '</span><span class="muted">' +
      hs.pr_count +
      " PRs</span>";
    el.addEventListener("click", () => {
      openHotspot(hs.path);
    });
    return el;
  }

  function renderQueue() {
    const root = $("queueList");
    const rows = queueRows();
    renderLeftHeader();

    if (!rows.length) {
      disposeVirtualizer("queue");
      root.innerHTML = '<div class="empty">No queue — Fetch</div>';
      return;
    }

    if (!Virtualizer) {
      root.innerHTML = "";
      for (const row of rows) root.appendChild(buildQueueItem(row));
      return;
    }

    if (!queueVirtualizer) {
      queueVirtualizer = mountVirtualizer("queue", makeVirtualizer(
        root,
        rows.length,
        (i) => (queueRows()[i] && queueRows()[i].size) || 40,
        8,
        paintQueueVirtual
      ));
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
        if (row.kind === "group") el.dataset.version = groupContentVersion(row.group);
        inner.appendChild(el);
      } else if (row.kind === "head") {
        const h = el.firstChild;
        if (h) h.textContent = row.label + " · " + row.count;
      } else if (row.kind === "group") {
        const card = el.firstChild;
        if (el.dataset.version !== groupContentVersion(row.group)) {
          el.replaceChildren(buildGroupCard(row.group));
          el.dataset.version = groupContentVersion(row.group);
        } else if (card) {
          card.className =
            "group-card" + (row.group.group_id === state.selectedGroupId ? " selected" : "");
        }
      } else if (row.kind === "hotspot") {
        const card = el.firstChild;
        if (card) {
          card.className =
            "hotspot-row" +
            (!state.selectedGroupId &&
            state.selectedFile === row.hotspot.path
              ? " selected"
              : "");
        }
      }
      el.style.transform = "translateY(" + vi.start + "px)";
      el.style.height = vi.size + "px";
    }
  }

  async function loadRelated(pr, path) {
    if (!pr) {
      relatedGen += 1;
      state.related = null;
      state.relatedKey = "";
      renderRelated();
      return;
    }
    const key = snapshotKey() + "|" + pr + "|" + (path || "");
    cancelEnrichmentUnless(key);
    if (state.relatedKey === key && state.related) {
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
      url += "&limit=5";
      const data = await requestOnce("related", key, url);
      if (gen !== relatedGen) return;
      state.related = data;
      renderRelated();
    } catch (err) {
      if (gen !== relatedGen) return;
      state.related = { enabled: false, reason: err.message, related: [] };
      renderRelated();
    }
  }

  function loadRelatedIfOpen(pr, path) {
    cancelEnrichmentUnless(relatedTargetIdentity(snapshotKey(), pr, path));
    const block = $("relatedBlock");
    if (!block || !block.open) {
      relatedGen += 1;
      state.related = null;
      state.relatedKey = "";
      return;
    }
    loadRelated(pr, path);
  }

  function relatedTargetIdentity(snapshot, pr, path) {
    return snapshot + "|" + (pr || "") + "|" + (path || "");
  }

  function requestContextMatches(expectedSnapshot, expectedGeneration, expectedIdentity,
      currentSnapshot, currentGeneration, currentIdentity) {
    return expectedSnapshot === currentSnapshot && expectedGeneration === currentGeneration &&
      expectedIdentity === currentIdentity;
  }

  function cancelEnrichmentUnless(identity) {
    if (!enrichmentRequest || enrichmentRequest.identity === identity) return;
    enrichmentRequest.controller.abort();
    enrichmentRequest = null;
    setStatus("external ranking cancelled because the selected revision changed");
  }

  function currentRelatedTarget() {
    if (isFileView()) {
      return { pr: state.selectedPr, path: state.selectedFile || null };
    }
    const group = groupById(state.selectedGroupId);
    return {
      pr: state.selectedPr || (group && (group.pr_numbers || [])[0]) || null,
      path: state.selectedFile || null,
    };
  }

  function renderRelated() {
    const root = $("relatedList");
    if (!root) return;
    const data = state.related;
    const disclosure = $("enrichDisclosure");
    const target = currentRelatedTarget();
    const targetPr = target.pr ? prByNumber(target.pr) : null;
    const enrichment = (data && data.enrichment) || null;
    const targetIdentity = relatedTargetIdentity(snapshotKey(), target.pr, target.path);
    const enrichmentBusy = !!(
      enrichmentRequest && enrichmentRequest.identity === targetIdentity
    );
    if (disclosure) {
      disclosure.textContent = "External action only: send normalized patch content from " +
        (state.repo || "this repository") + " for #" + (target.pr || "?") +
        " and up to " + ((enrichment && enrichment.max_candidates) || 24) +
        " candidate revisions to " + ((enrichment && enrichment.provider) || "the configured provider") +
        ((enrichment && enrichment.model) ? " (" + enrichment.model + ")" : "") +
        (enrichment && enrichment.max_total_input_bytes
          ? " · total input cap " + enrichment.max_total_input_bytes + " bytes."
          : ".");
    }
    const enrich = $("enrichBtn");
    if (enrich) {
      enrich.disabled = enrichmentBusy || !targetPr || targetPr.evidence_complete !== true ||
        !enrichment || enrichment.enabled !== true;
      enrich.textContent = enrichmentBusy
        ? "External ranking in progress…"
        : "Send bounded candidates for external ranking";
      enrich.title = enrich.disabled
        ? ((enrichment && enrichment.reason) || "Complete evidence and a configured provider are required")
        :
        "This one action explicitly permits external processing";
    }
    if (!data) {
      root.className = "related-list muted";
      root.textContent = "Pick a PR.";
      return;
    }
    if (data.loading) {
      root.className = "related-list muted";
      root.textContent = "Loading local/cached ranking…";
      return;
    }
    if (!data.enabled || !(data.related || []).length) {
      root.className = "related-list muted";
      root.textContent = data.reason || "No local or cached patch neighbors.";
      return;
    }
    const items = data.related || [];
    root.className = "related-list";
    root.innerHTML = "";
    const provenance = document.createElement("div");
    provenance.className = "related-provenance muted";
    const truncation = data.truncation || {};
    const omitted = truncation.omitted != null
      ? truncation.omitted
      : truncation.omitted_from_rerank;
    const evidence = data.evidence || {};
    provenance.textContent =
      "Advisory " + (data.source || "local") + " ranking" +
      (data.provider ? " · " + data.provider + (data.model ? "/" + data.model : "") : "") +
      (evidence.query_complete === false ? " · query evidence incomplete" : "") +
      (evidence.incomplete ? " · " + evidence.incomplete + " incomplete corpus items" : "") +
      (evidence.unavailable ? " · " + evidence.unavailable + " unavailable" : "") +
      (omitted ? " · " + omitted + " candidates omitted by displayed/bounded ranking" : "");
    root.appendChild(provenance);
    items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "related-row";
      row.innerHTML =
        '<a class="mono" href="' +
        escapeHtml(prUrl(it)) +
        '" target="_blank" rel="noopener">#' +
        it.number +
        '</a><span title="' +
        escapeHtml(it.title || "") +
        '">' +
        escapeHtml(it.title || "") +
        '</span><span class="muted">' +
        escapeHtml(it.group_id || "") +
        '</span><span class="score" title="' +
        escapeHtml(it.score_kind || "advisory ranking signal") + '">' +
        (it.score == null ? "advisory" : "advisory " + Number(it.score).toFixed(2)) +
        "</span>";
      const rowEvidence = document.createElement("div");
      rowEvidence.className = "related-evidence muted";
      const input = it.input_truncation || {};
      const completeness = !it.evidence
        ? " · evidence status unknown"
        : it.evidence.complete === true ? " · complete evidence" : " · incomplete evidence";
      rowEvidence.textContent = (it.score_kind || "local advisory signal") + completeness +
        (input.omitted_bytes ? " · " + input.omitted_bytes + " input bytes omitted" : "") +
        (input.omitted_chunks ? " · " + input.omitted_chunks + " chunks omitted" : "");
      row.appendChild(rowEvidence);
      row.addEventListener("click", (ev) => {
        if (ev.target.tagName === "A") return;
        if (it.group_id) state.selectedGroupId = it.group_id;
        state.selectedPr = it.number;
        render();
      });
      root.appendChild(row);
    });
  }

  function isFileView() {
    return !state.selectedGroupId && !!state.selectedFile;
  }

  function openHotspot(path, opts) {
    const fromUrl = !!(opts && opts.fromUrl);
    state.selectedGroupId = null;
    state.selectedPr = null;
    state.selectedFile = path;
    state.fileQueue = { path: path, prs: [], loading: true, same_patch: [] };
    if (state.queuePile !== "hotspots") {
      state.queuePile = "hotspots";
      queueRowsCache = null;
    }
    if (!fromUrl) writeUrl(true);
    render();
    openFileQueue(path, { fromUrl: true, fileView: true });
  }

  async function openFileQueue(path, opts) {
    const gen = ++fileQueueGen;
    const fromUrl = !!(opts && opts.fromUrl);
    const fileView = !!(opts && opts.fileView) || isFileView();
    const page = (opts && opts.page) || 1;
    const scrollRequest = opts && opts.scroll === false ? null : beginDiffScroll(path);
    state.selectedFile = path;
    state.fileQueue = { path: path, prs: [], loading: true, same_patch: [] };
    const extra = $("fileQueueBlock");
    if (!fromUrl && !fileView && extra && extra.tagName === "DETAILS") extra.open = true;
    if (!fileView) writeUrl(!fromUrl);
    if (fileView) renderFileDetail();
    else {
      renderFileQueue();
      const g = state.groups.find((x) => x.group_id === state.selectedGroupId);
      if (g) renderDiffs(g, { scrollRequest });
    }
    try {
      const identity = snapshotKey() + "|" + path + "|" + page;
      const data = await requestOnce("file", identity,
        "/api/file?path=" + encodeURIComponent(path) + "&page=" + page + "&page_size=40");
      if (gen !== fileQueueGen || state.selectedFile !== path) return;
      state.fileQueue = data;
      if (isFileView()) {
        renderFileDetail({ scrollRequest });
      } else {
        renderFileQueue();
        const g2 = state.groups.find((x) => x.group_id === state.selectedGroupId);
        if (g2) renderDiffs(g2, { scrollRequest });
      }
    } catch (err) {
      if (gen !== fileQueueGen || state.selectedFile !== path) return;
      state.fileQueue = { path: path, prs: [], error: err.message };
      if (isFileView()) renderFileDetail();
      else renderFileQueue();
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
    const extra = fq && fq.truncated ? " · " + fq.truncated + " on later pages" : "";
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
      (same.length
        ? " · this evidence page has " + sameN + " identical complete patches"
        : "");
    root.appendChild(head);
    if (same.length) {
      const box = document.createElement("div");
      box.className = "same-patch-list";
      same.forEach((c) => {
        const row = document.createElement("div");
        row.className = "muted";
        row.textContent =
          "server-verified same complete patch ×" +
          c.count +
          "  #" +
          (c.pr_numbers || []).join(" #");
        box.appendChild(row);
      });
      root.appendChild(box);
    }
    items.forEach((pr) => {
      const row = document.createElement("div");
      row.className = "hotspot-row";
      if (state.selectedPr === pr.number) row.classList.add("selected");
      row.innerHTML =
        '<a class="mono" href="' +
        escapeHtml(prUrl(pr)) +
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
    appendFilePager(root, false);
  }

  function appendFilePager(root, listItem) {
    const fq = state.fileQueue;
    if (!fq || (fq.pages || 1) <= 1) return;
    const nav = document.createElement(listItem ? "li" : "div");
    nav.className = "file-pagination";
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "btn";
    previous.textContent = "Previous 40";
    previous.disabled = (fq.page || 1) <= 1;
    previous.addEventListener("click", () => openFileQueue(fq.path, {
      fromUrl: true, fileView: isFileView(), page: (fq.page || 1) - 1, scroll: false,
    }));
    const next = document.createElement("button");
    next.type = "button";
    next.className = "btn";
    next.textContent = "Next 40";
    next.disabled = !fq.next_page;
    next.addEventListener("click", () => openFileQueue(fq.path, {
      fromUrl: true, fileView: isFileView(), page: fq.next_page, scroll: false,
    }));
    nav.append(previous, next);
    root.appendChild(nav);
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
    duplicate: "duplicate candidate; confirm complete patch evidence",
    "related-theme": "same files, different hunks",
    "needs-look": "unclassified — read it",
    hardware: "hardware keywords",
    "update-path": "upgrade / migrate",
    docs: "docs only",
    cosmetic: "theme / CSS",
    junk: "Junk pile — rejected or noise",
    approved: "you approved these exact reviewed revisions",
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
    "approved",
    "rejected",
    "upgrade",
  ];

  function labelHelp(name) {
    return LABEL_HELP[name] || "";
  }

  function ruleLabel(decision) {
    if (decision === "approve") return "approved";
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

  function renderFileMembers() {
    const wrap = $("detailMembersWrap");
    const members = $("detailMembers");
    members.innerHTML = "";
    disposeVirtualizer("member");
    wrap.classList.remove("virt");
    wrap.style.maxHeight = "";
    wrap.style.overflow = "";
    wrap.scrollTop = 0;
    members.style.position = "";
    members.style.height = "";
    const fq = state.fileQueue;
    if (fq && fq.loading) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "Loading…";
      members.appendChild(li);
      return;
    }
    const items = (fq && fq.prs) || [];
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = fq && fq.error ? fq.error : "No open PRs on this file.";
      members.appendChild(li);
      return;
    }
    const byG = new Map();
    for (const pr of items) {
      const gid = pr.group_id || "?";
      if (!byG.has(gid)) byG.set(gid, []);
      byG.get(gid).push(pr);
    }
    const groups = [...byG.entries()].sort((a, b) => b[1].length - a[1].length);
    for (const [gid, prs] of groups) {
      const g = groupById(gid);
      const head = document.createElement("li");
      head.className = "file-shape";
      const dec = (g && g.suggested_decision) || "";
      const cls = (g && g.card_class) || "";
      const n = g ? (g.pr_numbers || []).length : prs.length;
      head.textContent =
        gid +
        " · " +
        prs.length +
        (n !== prs.length ? " of " + n : "") +
        (dec ? " · " + dec : "") +
        (cls ? " · " + cls : "");
      head.title = "Open this shape";
      head.addEventListener("click", () => {
        if (gid && gid !== "?") state.selectedGroupId = gid;
        state.selectedPr = prs[0].number;
        render();
      });
      members.appendChild(head);
      prs.forEach((pr) => members.appendChild(buildMemberLi(pr.number)));
    }
    appendFilePager(members, true);
  }

  function renderFileDetail(diffOpts) {
    const empty = $("detailEmpty");
    const detail = $("detail");
    empty.classList.add("hidden");
    detail.classList.remove("hidden");
    detail.classList.add("file-view");
    const path = state.selectedFile;
    const fq = state.fileQueue;
    const count = (fq && (fq.pr_count || (fq.prs || []).length)) || 0;
    const same = (fq && fq.same_patch) || [];
    const sameN = same.reduce((n, c) => n + (c.count || 0), 0);
    $("detailKicker").textContent = "Hotspot";
    $("detailTitle").textContent = path || "";
    let meta = "";
    if (fq && fq.loading) meta = "Loading PRs on this file…";
    else if (fq && fq.error) meta = "error: " + fq.error;
    else {
      meta = count + " open PRs touch this file";
      if (same.length) meta += " · this evidence page has " + sameN +
        " identical complete patches";
    }
    $("detailMeta").textContent = meta;
    $("detailPills").innerHTML = "";
    const mh = $("membersHead");
    if (mh) mh.textContent = "PRs on this file" + (count ? " · " + count : "");
    renderFileMembers();
    renderPrBodies({ pr_numbers: ((fq && fq.prs) || []).map((p) => p.number) });
    renderDiffs(null, diffOpts);
    if (state.selectedPr) loadRelatedIfOpen(state.selectedPr, path);
    else {
      loadRelatedIfOpen(null, path);
      renderRelated();
    }
  }

  function renderDetail() {
    normalizeSelection();
    const empty = $("detailEmpty");
    const detail = $("detail");
    const g = state.groups.find((x) => x.group_id === state.selectedGroupId);
    if (!g && state.selectedFile) {
      renderFileDetail();
      return;
    }
    if (!g) {
      loadRelatedIfOpen(null, null);
      renderDiffs(null);
      empty.classList.remove("hidden");
      detail.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    detail.classList.remove("hidden");
    detail.classList.remove("file-view");

    const rule = ruleFor(g.group_id);
    const n = (g.pr_numbers || []).length;
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const firstTitle = (g.title_variants && g.title_variants[0]) || "";
    const cls = g.card_class || "needs-look";
    const decision = g.suggested_decision || "unique";
    renderDecisionWarning(g);

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
          ? "same file-set; review every exact revision before approval."
          : "single-member shape; review its exact revision directly.";
    }

    const pills = $("detailPills");
    pills.innerHTML = "";
    pills.appendChild(makeLabelRow(cls));
    pills.appendChild(makeLabelRow(decision));
    if (rule) {
      pills.appendChild(makeLabelRow(ruleLabel(rule.decision), rule.decision === "approve" ? "approved" : rule.decision));
    }
    fillLabelKey();
    const titles = (g.title_variants || []).filter((t) => t && t !== firstTitle);
    if (titles.length) {
      $("detailMeta").appendChild(
        document.createTextNode(" · " + titles.length + " other titles")
      );
    }

    const mh = $("membersHead");
    if (mh) mh.textContent = "PRs in this shape · " + n;

    renderMembers(g);
    renderPrBodies(g);
    renderOverlap(g);
    renderDiffs(g);
    renderFileQueue();
    const qPr = state.selectedPr || (g.pr_numbers || [])[0];
    loadRelatedIfOpen(qPr, state.selectedFile || null);
  }

  async function loadPrBody(num) {
    const key = snapshotKey() + "|" + num;
    if (bodyCache[key]) return bodyCache[key];
    const gen = snapshotGen;
    const repo = state.repo || "omacom/omarchy";
    const url = "/api/pr?number=" + encodeURIComponent(num) +
      "&repo=" + encodeURIComponent(repo);
    const pending = typeof requestOnce === "function"
      ? requestOnce("body", key, url)
      : api(url);
    bodyCache[key] = pending;
    try {
      const data = await pending;
      if (gen === snapshotGen) bodyCache[key] = data;
      return data;
    } catch (error) {
      delete bodyCache[key];
      throw error;
    }
  }

  function renderPrBodies(g) {
    const root = $("prBody");
    const head = $("prBodyHead");
    const block = $("prBodyBlock");
    if (!root) return;
    const gen = ++bodyGen;
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
          (it.legacy_unverified ? " · unverified legacy cache preview" : "") +
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
    const wasVirtual = wrap.classList.contains("virt");
    members.innerHTML = "";
    disposeVirtualizer("member");

    const useVirt = nums.length > 40 && Virtualizer;
    wrap.classList.toggle("virt", !!useVirt);

    if (!useVirt) {
      wrap.style.maxHeight = "";
      wrap.style.overflow = "";
      if (wasVirtual) wrap.scrollTop = 0;
      members.style.position = "";
      members.style.height = "";
      nums.forEach((num) => members.appendChild(buildMemberLi(num)));
      return;
    }

    // Virtualize long member lists
    wrap.style.maxHeight = "320px";
    wrap.style.overflow = "auto";
    members.style.position = "relative";
    members.style.height = nums.length * 36 + "px";

    memberVirtualizer = mountVirtualizer(
      "member",
      makeVirtualizer(wrap, nums.length, () => 36, 8, () => paintMembers(nums))
    );
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
    const href = prUrl(pr);
    const localRevision = state.source === "fixtures" ? "local fixture" : "missing";
    const identity = ["head " + (pr.head_sha || localRevision),
      "base " + (pr.base_sha || localRevision),
      "content " + (pr.content_digest || "missing")].join(" · ");
    li.innerHTML = `
      <a href="${escapeHtml(href)}" target="_blank" rel="noopener">#${num}</a>
      <span title="${escapeHtml(pr.title || "")}">${escapeHtml(pr.title || "")}</span>
      <button type="button" class="user muted">${escapeHtml(pr.user || "")}</button>
      <span class="${labelClass}">${escapeHtml(pr.label || "")}</span>
      <span class="revision-id" title="${escapeHtml(identity)}">${escapeHtml(identity)}</span>`;
    bindUserLink(li.querySelector(".user"), pr.user);
    li.addEventListener("click", (ev) => {
      if (ev.target.tagName === "A") return;
      state.selectedPr = num;
      state.examinedMembers.add(num);
      render();
    });
    return li;
  }

  function renderDecisionWarning(group) {
    const root = $("decisionWarning");
    if (!root) return;
    const numbers = group.pr_numbers || [];
    const incomplete = numbers.filter((number) => {
      const pr = prByNumber(number);
      const revisionKnown = state.source === "fixtures" || (pr && pr.head_sha && pr.base_sha);
      return !pr || pr.evidence_complete !== true || !revisionKnown || !pr.content_digest;
    });
    const unexamined = numbers.filter((number) => !state.examinedMembers.has(number));
    const complete = group.evidence_complete === true && incomplete.length === 0;
    root.className = "decision-warning " + (complete ? "complete" : "incomplete");
    root.textContent = numbers.length + " exact member revision" + (numbers.length === 1 ? "" : "s") +
      " · snapshot " + (group.snapshot_digest || "identity missing") +
      (unexamined.length ? " · " + unexamined.length + " not individually opened" : " · all opened") +
      (incomplete.length ? " · " + incomplete.length + " incomplete" : " · complete evidence");
    const approve = $("blessBtn");
    approve.disabled = !complete;
    approve.title = complete
      ? "Approve only these repository-bound revisions"
      : "Approval requires complete head, base, and content evidence for every member";
  }

  async function renderOverlap(g, options) {
    const gen = ++overlapGen;
    const summary = $("overlapSummary");
    const table = $("overlapMatrix");
    table.innerHTML = "";
    let ov = (state.overlap && state.overlap[g.group_id]) || null;
    const n = (g.pr_numbers || []).length;
    const memberIndex = (g.pr_numbers || []).indexOf(state.selectedPr);
    const memberPage = (options && options.memberPage) ||
      (memberIndex >= 0 ? Math.floor(memberIndex / 24) + 1 : 1);
    const rowPage = (options && options.rowPage) || 1;
    if (!ov || ov.lazy || options || ov.member_page !== memberPage || ov.row_page !== rowPage) {
      summary.textContent = ov
        ? "jaccard " +
          (typeof ov.jaccard === "number" ? ov.jaccard.toFixed(2) : "?") + " · " +
          (ov.shared_n || 0) + " shared · " + (ov.unique_n || 0) + " unique" +
          (n > 24 ? " · member pages of 24" : "")
        : "Loading overlap facts…";
      try {
        ov = await requestOnce("overlap", snapshotKey() + "|" + g.group_id +
          "|" + memberPage + "|" + rowPage,
          "/api/overlap?group_id=" + encodeURIComponent(g.group_id) +
          "&member_page=" + memberPage + "&member_page_size=24&row_page=" + rowPage +
          "&row_page_size=80");
        if (gen !== overlapGen || state.selectedGroupId !== g.group_id) return;
        state.overlap[g.group_id] = ov;
      } catch (err) {
        if (gen !== overlapGen || state.selectedGroupId !== g.group_id) return;
        summary.textContent = "overlap error: " + err.message;
        return;
      }
    }
    if (!ov) {
      summary.textContent = "no overlap data";
      return;
    }
    const sharedN = ov.shared_n != null ? ov.shared_n : (ov.shared || []).length;
    const uniqueN = ov.unique_n != null ? ov.unique_n : (ov.unique || []).length;
    const jacc = typeof ov.jaccard === "number" ? ov.jaccard.toFixed(2) : "?";
    let extra = "";
    if (ov.pr_truncated) extra += " · " + ov.pr_truncated + " PRs on other member pages";
    if (ov.row_truncated) extra += " · " + ov.row_truncated + " files on other row pages";
    summary.textContent =
      "jaccard " + jacc + " · " + sharedN + " shared · " +
      (ov.partial_n || 0) + " partial · " + uniqueN + " unique" + extra;

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
        (row.same_patch_status === "same"
          ? ' <span class="same-label">same complete patch</span>'
          : "");
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
    if ((ov.member_pages || 1) > 1 || (ov.row_pages || 1) > 1) {
      const nav = document.createElement("caption");
      nav.className = "matrix-pagination";
      const memberBack = document.createElement("button");
      memberBack.type = "button";
      memberBack.className = "btn";
      memberBack.textContent = "Previous members";
      memberBack.disabled = (ov.member_page || 1) <= 1;
      memberBack.addEventListener("click", () => renderOverlap(g, {
        memberPage: (ov.member_page || 1) - 1, rowPage: ov.row_page || 1,
      }));
      const memberNext = document.createElement("button");
      memberNext.type = "button";
      memberNext.className = "btn";
      memberNext.textContent = "Next members";
      memberNext.disabled = !ov.next_member_page;
      memberNext.addEventListener("click", () => renderOverlap(g, {
        memberPage: ov.next_member_page, rowPage: ov.row_page || 1,
      }));
      const rowBack = document.createElement("button");
      rowBack.type = "button";
      rowBack.className = "btn";
      rowBack.textContent = "Previous files";
      rowBack.disabled = (ov.row_page || 1) <= 1;
      rowBack.addEventListener("click", () => renderOverlap(g, {
        memberPage: ov.member_page || 1, rowPage: (ov.row_page || 1) - 1,
      }));
      const rowNext = document.createElement("button");
      rowNext.type = "button";
      rowNext.className = "btn";
      rowNext.textContent = "Next files";
      rowNext.disabled = !ov.next_row_page;
      rowNext.addEventListener("click", () => renderOverlap(g, {
        memberPage: ov.member_page || 1, rowPage: ov.next_row_page,
      }));
      nav.append(memberBack, memberNext, rowBack, rowNext);
      table.prepend(nav);
    }
  }


  function prUrl(pr) {
    const repo = state.repo || "omacom/omarchy";
    const fallback = "https://github.com/" + repo + "/pull/" +
      (pr && pr.number ? pr.number : "");
    try {
      const candidate = new URL((pr && pr.html_url) || fallback);
      if (candidate.protocol === "https:" && candidate.hostname === "github.com") {
        return candidate.href;
      }
    } catch (_) {}
    return fallback;
  }

  function colorizeDiff(patch) {
    const hunkRe = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
    let oldN = 0;
    let newN = 0;
    let sawHunk = false;
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
        sawHunk = true;
        oldN = parseInt(m[1], 10);
        newN = parseInt(m[2], 10);
        row("", "", line, "hunk");
        return;
      }
      if (!sawHunk && (
        line.startsWith("+++") || line.startsWith("---") ||
        line.startsWith("diff ") || line.startsWith("index ")
      )) {
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
  function cancelPendingDiffScroll() {
    if (pendingDiffScrollFrame !== null) {
      cancelAnimationFrame(pendingDiffScrollFrame);
      pendingDiffScrollFrame = null;
    }
    pendingDiffScroll = null;
  }

  function beginDiffScroll(path) {
    cancelPendingDiffScroll();
    const request = {
      id: ++diffScrollSerial,
      path,
      groupId: state.selectedGroupId || null,
      pr: state.selectedPr || null,
      snapshot: snapshotKey(),
    };
    pendingDiffScroll = request;
    return request;
  }

  function diffScrollMatches(request) {
    return pendingDiffScroll === request &&
      state.selectedFile === request.path &&
      (state.selectedGroupId || null) === request.groupId &&
      (state.selectedPr || null) === request.pr &&
      snapshotKey() === request.snapshot;
  }

  function scheduleDiffScroll(request, generation) {
    if (!request || !diffScrollMatches(request)) return;
    if (pendingDiffScrollFrame !== null) cancelAnimationFrame(pendingDiffScrollFrame);
    pendingDiffScrollFrame = requestAnimationFrame(() => {
      pendingDiffScrollFrame = null;
      if (!diffScrollMatches(request) || generation !== diffGen) return;
      pendingDiffScroll = null;
      const pane = document.querySelector(".pane.center");
      const block = $("diffBlock");
      if (!pane || !block) return;
      const paneRect = pane.getBoundingClientRect();
      const blockRect = block.getBoundingClientRect();
      pane.scrollTo({
        top: pane.scrollTop + blockRect.top - paneRect.top - 8,
        behavior: "auto",
      });
    });
  }

  function cancelDiffScrollForUserIntent(event) {
    const scrollKeys = new Set([
      "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " ", "Spacebar",
    ]);
    if (event.type !== "keydown" || scrollKeys.has(event.key)) cancelPendingDiffScroll();
  }

  function scrollToDiff(request, generation) {
    scheduleDiffScroll(request, generation);
  }

  async function renderDiffs(g, opts) {
    const gen = ++diffGen;
    const root = $("diffPanels");
    const path = state.selectedFile;
    const scrollRequest = opts && opts.scrollRequest;
    if (!path) {
      root.innerHTML = '<div class="muted diff-hint">Pick a file above to compare hunks.</div>';
      return;
    }
    const fqNums = ((state.fileQueue && state.fileQueue.prs) || [])
      .map((p) => p.number)
      .filter(Boolean);
    let nums;
    let allNums;
    if (g) {
      const groupSet = new Set(g.pr_numbers || []);
      const selectedGroupPr = groupSet.has(state.selectedPr) ? state.selectedPr : null;
      allNums = g.pr_numbers || [];
      nums = takeWithSelected(allNums, selectedGroupPr, 8);
    } else {
      const selected = prByNumber(state.selectedPr);
      const selectedFilePr =
        selected && (selected.paths || []).includes(path) ? selected.number : null;
      allNums = filePatchTargets(fqNums, selectedFilePr);
      nums = takeWithSelected(allNums, selectedFilePr, 8);
    }
    if (!nums.length) {
      root.innerHTML =
        '<div class="muted diff-hint">' +
        (g
          ? "This shape does not touch " + escapeHtml(path) + "."
          : state.fileQueue && state.fileQueue.loading
            ? "Loading patches…"
            : "No patches for " + escapeHtml(path) + ".") +
        "</div>";
      return;
    }
    const meta = $("diffMeta");
    if (meta) {
      meta.textContent =
        path + " · " + nums.length + (g ? " PRs in this group" : " PRs on this file");
    }
    if (!root.querySelector(".diff-panel")) {
      root.innerHTML = '<div class="muted diff-hint">Loading patches…</div>';
    }
    const repo = state.repo || "omacom/omarchy";
    const pageSize = 8;
    const selectedIndex = allNums.indexOf(state.selectedPr);
    const defaultPage = selectedIndex >= 0 ? Math.floor(selectedIndex / pageSize) + 1 : 1;
    const page = (opts && opts.page) || defaultPage;
    const target = g
      ? "&group_id=" + encodeURIComponent(g.group_id)
      : "&prs=" + allNums.join(",");
    const url = "/api/patches?repo=" + encodeURIComponent(repo) + target +
      "&path=" + encodeURIComponent(path) + "&page=" + page + "&page_size=" + pageSize +
      "&patch_offset=0&patch_limit=6000";
    let data;
    try {
      data = typeof requestOnce === "function"
        ? await requestOnce("patch", snapshotKey() + "|" +
            (g ? g.group_id : allNums.join(",")) + "|" + path + "|" + page, url)
        : await api(url);
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
    root.innerHTML = "";
    const withPatch = items.filter((it) => (it.patch || "").length);
    if (!items.length) {
      root.innerHTML =
        '<div class="muted diff-hint">No patches for ' +
        escapeHtml(path) +
        ".</div>";
      return;
    }
    const comparison = data.comparison || {};
    if (meta) {
      meta.textContent = path + " · page " + (data.page || page) + " of " +
        (data.total_pages || 1) + " · " + (data.total_items || items.length) +
        " target PRs · equality " +
        (comparison.same_complete_patch == null ? "unknown/incomplete" :
          comparison.same_complete_patch ? "same complete patch" : "different complete patches");
    }
    items.forEach((it) => {
      const pr = prByNumber(it.number) || { number: it.number, user: "" };
      const patch = it.patch || "";
      const complete = it.evidence_complete === true;
      const label = !patch
        ? "no patch"
        : !complete
          ? "incomplete preview"
          : it.next_patch_offset != null
            ? "complete source · more chunks"
            : "complete";
      const panel = document.createElement("div");
      panel.className = "diff-panel";
      const href = prUrl(pr);
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
        (complete ? "same" : "diff") +
        '">' +
        label +
        "</span></div>" +
        '<div class="diff-code">' +
        colorizeDiff(patch || "(empty patch)") +
        "</div>";
      patchPanelState.set(panel, {
        raw: patch,
        busy: false,
        generation: gen,
        item: it,
        pageData: data,
        group: g,
      });
      if (it.content_sha256) {
        const digest = document.createElement("div");
        digest.className = "revision-id";
        digest.textContent = "full patch sha256 " + it.content_sha256;
        panel.appendChild(digest);
      } else if ((it.incomplete_reasons || []).length) {
        const reason = document.createElement("div");
        reason.className = "revision-id incomplete-evidence";
        reason.textContent = it.incomplete_reasons.join(" · ");
        panel.appendChild(reason);
      }
      if (it.next_patch_offset != null) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "btn patch-more";
        more.textContent = "Load next patch chunk";
        more.addEventListener("click", () => loadNextPatchChunk(g, data, it, panel));
        panel.appendChild(more);
      }
      root.appendChild(panel);
      bindUserLink(panel.querySelector("[data-user]"), pr.user);
    });
    if ((data.total_pages || 1) > 1) {
      const nav = document.createElement("div");
      nav.className = "diff-pagination";
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "btn";
      previous.textContent = "Previous 8";
      previous.disabled = (data.page || page) <= 1;
      previous.addEventListener("click", () => renderDiffs(g, { page: (data.page || page) - 1 }));
      const next = document.createElement("button");
      next.type = "button";
      next.className = "btn";
      next.textContent = "Next 8";
      next.disabled = !data.next_page;
      next.addEventListener("click", () => renderDiffs(g, { page: data.next_page }));
      nav.append(previous, next);
      root.appendChild(nav);
    }
    scrollToDiff(scrollRequest, gen);
    if (!withPatch.length) {
      const hint = document.createElement("div");
      hint.className = "muted diff-hint";
      hint.textContent = "Cached files have no patch for this path (binary or too large for GitHub).";
      root.prepend(hint);
    }
    if (state.fileQueue && state.fileQueue.path === path) {
      state.fileQueue.same_patch = comparison.same_complete_patch === true && items.length >= 2
        ? [{ pr_numbers: items.map((item) => item.number), count: items.length,
             server_verified: true }]
        : [];
      renderFileQueue();
    }
  }

  async function loadNextPatchChunk(group, pageData, item, panel) {
    const panelState = patchPanelState.get(panel);
    if (!panelState || panelState.busy) return;
    const offset = panelState.item.next_patch_offset;
    if (offset == null) return;
    const button = panel.querySelector(".patch-more");
    panelState.busy = true;
    if (button) {
      button.disabled = true;
      button.textContent = "Loading patch chunk…";
    }
    const repo = state.repo;
    const target = group
      ? "&group_id=" + encodeURIComponent(group.group_id)
      : "&prs=" + (pageData.items || []).map((row) => row.number).join(",");
    const targetPage = group ? (pageData.page || 1) : 1;
    const url = "/api/patches?repo=" + encodeURIComponent(repo) + target +
      "&path=" + encodeURIComponent(item.path) + "&page=" + targetPage +
      "&page_size=" + (pageData.page_size || 8) + "&patch_offset=" + offset +
      "&patch_limit=6000";
    const identity = snapshotKey() + "|chunk|" + item.number + "|" + item.path + "|" + offset;
    try {
      const data = await requestOnce("patch", identity, url);
      if (panelState.generation !== diffGen || patchPanelState.get(panel) !== panelState) return;
      const next = (data.items || []).find((row) => row.number === item.number);
      if (!next) throw new Error("patch chunk was missing from the response");
      const merged = mergePatchChunk(panelState.raw, offset, next);
      panelState.raw = merged.raw;
      item.patch = merged.raw;
      item.next_patch_offset = merged.nextOffset;
      const code = panel.querySelector(".diff-code");
      code.innerHTML = colorizeDiff(panelState.raw || "(empty patch)");
      if (button) {
        if (merged.nextOffset == null) button.remove();
        else button.textContent = "Load next patch chunk";
      }
    } catch (error) {
      if (panelState.generation !== diffGen || patchPanelState.get(panel) !== panelState) return;
      if (button) {
        button.textContent = "Retry patch chunk";
        button.title = error.message || "patch chunk failed";
      }
    } finally {
      if (patchPanelState.get(panel) === panelState) {
        panelState.busy = false;
        if (button && button.isConnected) button.disabled = false;
      }
    }
  }

  function mergePatchChunk(raw, expectedOffset, next) {
    const prefix = String(raw || "");
    const prefixCodePoints = Array.from(prefix).length;
    if (!next || prefixCodePoints !== expectedOffset ||
        next.patch_offset !== expectedOffset || typeof next.patch !== "string") {
      throw new Error("patch chunk offset did not match the requested prefix");
    }
    const endOffset = expectedOffset + Array.from(next.patch).length;
    if ((next.next_patch_offset != null && next.next_patch_offset !== endOffset) ||
        (next.next_patch_offset == null && Number.isInteger(next.patch_length) &&
         next.patch_length !== endOffset)) {
      throw new Error("patch chunk length did not match the server offsets");
    }
    return { raw: prefix + next.patch, nextOffset: next.next_patch_offset };
  }

  function filePatchTargets(numbers, selected) {
    const unique = [...new Set(numbers || [])].filter(Boolean);
    if (selected) return [selected, ...unique.filter((number) => number !== selected)].slice(0, 200);
    return unique.slice(0, 200);
  }

  function takeWithSelected(numbers, selected, limit) {
    const unique = [...new Set(numbers || [])];
    const shown = unique.slice(0, limit);
    if (!selected || shown.includes(selected)) return shown;
    if (shown.length >= limit) shown[shown.length - 1] = selected;
    else shown.push(selected);
    return shown;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function render() {
    normalizeSelection();
    if (state.selectedPr) state.examinedMembers.add(state.selectedPr);
    if (state.leftTab === "allprs") renderAllPrList();
    else if (state.leftTab === "queue") renderQueue();
    else renderGroupList();
    renderDetail();
    writeUrl(true);
    if (state.selectedUser) renderUserDrawer();
  }

  async function loadState() {
    const gen = ++stateLoadGen;
    setStatus("loading…");
    try {
      const data = await api("/api/state");
      if (gen !== stateLoadGen) return;
      applyState(data);
      const c = (state.queue && state.queue.counts) || {};
      setStatus(
        `${state.prs.length} PRs · ${state.groups.length} groups · ${c.needs_you || 0} need you`
      );
    } catch (err) {
      if (gen !== stateLoadGen) return;
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
    stateLoadGen += 1;
    const source = $("source").value;
    const repo = $("repo").value.trim() || "omacom/omarchy";
    const limit = Number($("limit").value);
    const limitVal = Number.isFinite(limit) ? limit : 0;
    const refresh = !!$("refresh").checked;
    $("fetchBtn").disabled = true;
    state.fetching = true;
    setStatus(source === "fixtures" ? "starting fixtures…" : "starting gh fetch…");
    try {
      await api("/api/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, repo, limit: limitVal, refresh }),
      });
      await pollProgressUntilDone();
      const gen = ++stateLoadGen;
      const data = await api("/api/state");
      if (gen !== stateLoadGen) return;
      applyState(data);
      setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
    } catch (err) {
      if (err.status === 409 && err.data && err.data.code === "fetch_busy") {
        setStatus(formatProgress(err.data) + " (already running)");
        try {
          await pollProgressUntilDone();
          const gen = ++stateLoadGen;
          const data = await api("/api/state");
          if (gen !== stateLoadGen) return;
          applyState(data);
          setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
        } catch (e2) {
          setStatus("fetch error: " + e2.message);
        }
      } else if (err.status === 409) {
        setStatus("sync blocked: " + err.message);
      } else {
        setStatus("fetch error: " + err.message);
        try {
          const cached = await api("/api/state");
          applyState(cached);
          setStatus("sync failed; showing the last usable cached snapshot · " + err.message);
        } catch (_) {}
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
    const group = groupById(state.selectedGroupId);
    const membersComplete = group && (group.pr_numbers || []).every((number) => {
      const pr = prByNumber(number);
      const revisionKnown = state.source === "fixtures" || (pr && pr.head_sha && pr.base_sha);
      return pr && pr.evidence_complete === true && revisionKnown && pr.content_digest;
    });
    if (decision === "approve" &&
        (!group || group.evidence_complete !== true || !membersComplete)) {
      setStatus("approval blocked: complete evidence is required for every revision");
      return;
    }
    const gen = ++stateLoadGen;
    const retryIdentity = [snapshotKey(), state.selectedGroupId, decision].join("|");
    let idempotencyKey = decisionRetries.get(retryIdentity);
    if (!idempotencyKey) {
      idempotencyKey = (window.crypto && typeof window.crypto.randomUUID === "function"
        ? window.crypto.randomUUID() :
        "decision-" + Date.now() + "-" + Math.random().toString(16).slice(2));
      decisionRetries.set(retryIdentity, idempotencyKey);
    }
    setStatus(decision + "…");
    try {
      const data = await api("/api/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          group_id: state.selectedGroupId,
          decision,
          repo: state.repo,
          expected_version: state.storeVersion,
          idempotency_key: idempotencyKey,
        }),
      });
      if (gen !== stateLoadGen) return;
      decisionRetries.delete(retryIdentity);
      applyState(data.state);
      setStatus(`decision saved: ${decision} ${group.group_id}`);
    } catch (err) {
      if (gen !== stateLoadGen) return;
      if (err.status === 409) {
        setStatus("decision not saved: evidence changed; reloading the current revisions");
        await loadState();
      } else {
        setStatus("decide error: " + err.message + " · retry keeps the same request key");
      }
    }
  }

  async function doEnrich() {
    const target = currentRelatedTarget();
    if (!target.pr || !state.repo || enrichmentRequest) return;
    const expectedSnapshot = snapshotKey();
    const expectedRelatedGen = relatedGen;
    const expectedVersion = state.storeVersion;
    const identity = relatedTargetIdentity(expectedSnapshot, target.pr, target.path);
    const advertised = state.related && state.related.enrichment;
    const limit = Math.min(24, Math.max(1, Number((advertised && advertised.max_candidates) || 24)));
    const request = { identity, controller: new AbortController() };
    enrichmentRequest = request;
    renderRelated();
    setStatus("sending bounded patch evidence to the configured provider…");
    try {
      const result = await api("/api/enrich", {
        method: "POST", signal: request.controller.signal, body: JSON.stringify({
        repo: state.repo, pr: target.pr, path: target.path || undefined, limit,
        allow_external: true, expected_version: expectedVersion,
      }) });
      const current = currentRelatedTarget();
      if (enrichmentRequest !== request || !requestContextMatches(
        expectedSnapshot, expectedRelatedGen, identity,
        snapshotKey(), relatedGen,
        relatedTargetIdentity(snapshotKey(), current.pr, current.path)
      )) return;
      state.related = result;
      renderRelated();
      setStatus("external ranking cached for this exact repository revision");
    } catch (err) {
      if (enrichmentRequest !== request || err.name === "AbortError") return;
      const current = currentRelatedTarget();
      if (!requestContextMatches(
        expectedSnapshot, expectedRelatedGen, identity,
        snapshotKey(), relatedGen,
        relatedTargetIdentity(snapshotKey(), current.pr, current.path)
      )) return;
      if (err.status === 409) {
        setStatus("enrichment cancelled: repository evidence changed; reloading");
        await loadState();
      } else {
        setStatus("enrichment error: " + err.message);
      }
    } finally {
      if (enrichmentRequest === request) {
        enrichmentRequest = null;
        const current = currentRelatedTarget();
        if (relatedTargetIdentity(snapshotKey(), current.pr, current.path) === identity) {
          renderRelated();
        }
      }
    }
  }

  $("fetchBtn").addEventListener("click", doFetch);
  $("blessBtn").addEventListener("click", () => doDecide("approve"));
  $("hardwareBtn").addEventListener("click", () => doDecide("hardware"));
  $("upgradeBtn").addEventListener("click", () => doDecide("upgrade"));
  $("rejectBtn").addEventListener("click", () => doDecide("reject"));
  $("enrichBtn").addEventListener("click", doEnrich);
  $("tabQueue").addEventListener("click", () => setLeftTab("queue"));
  $("tabGroups").addEventListener("click", () => setLeftTab("groups"));
  $("tabAllPrs").addEventListener("click", () => setLeftTab("allprs"));
  const userClose = $("userDrawerClose");
  if (userClose) userClose.addEventListener("click", closeUserDrawer);
  const relatedBlock = $("relatedBlock");
  if (relatedBlock) {
    relatedBlock.addEventListener("toggle", () => {
      if (!relatedBlock.open) {
        relatedGen += 1;
        state.related = null;
        state.relatedKey = "";
        return;
      }
      const target = currentRelatedTarget();
      loadRelated(target.pr, target.path);
    });
  }
  document.addEventListener("keydown", (e) => {
    cancelDiffScrollForUserIntent(e);
    if (e.key === "Escape" && state.selectedUser) closeUserDrawer();
  });
  const centerPane = document.querySelector(".pane.center");
  if (centerPane) {
    centerPane.addEventListener("wheel", cancelDiffScrollForUserIntent, { passive: true });
    centerPane.addEventListener("touchstart", cancelDiffScrollForUserIntent, { passive: true });
    centerPane.addEventListener("pointerdown", cancelDiffScrollForUserIntent, { passive: true });
  }
  window.addEventListener("popstate", () => {
    if (writingUrl) return;
    alignSidebar = true;
    readUrl();
    if ($("listFilter")) $("listFilter").value = state.filterQuery;
    selectorCache.clear();
    queueRowsCache = null;
    paintLabelFilters();
    normalizeSelection();
    setLeftTab(state.leftTab, true);
    renderDetail();
    writeUrl(false);
    if (state.selectedFile) openFileQueue(state.selectedFile, { fromUrl: true });
    renderUserDrawer();
  });
  window.addEventListener("pagehide", () => {
    cancelPendingDiffScroll();
    ["group", "all", "queue", "member"].forEach(disposeVirtualizer);
    for (const entry of resourceRequests.values()) entry.controller.abort();
    resourceRequests.clear();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) render();
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
  paintPileNav();
  bootstrapSession(false).then(loadState).catch((error) => setStatus("session error: " + error.message));
})();
