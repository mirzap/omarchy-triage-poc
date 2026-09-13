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
    proposals: [],
    rules: [],
    overlap: {},
    source: "",
    repo: "",
    // The repo field is an explicit, uncommitted workspace choice.  It must
    // never retarget the displayed snapshot merely because somebody typed.
    repoDraft: "",
    workspaceMode: "single",
    defaultRepo: "",
    workspaces: [],
    workspaceInitialized: false,
    workspaceLegacy: false,
    workspaceSwitching: false,
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
    // Agent navigation is useful context but is never a human examination
    // attestation.  Keep it separate so the review warning stays truthful.
    agentOpenedMembers: new Set(),
    proposal: null,
    proposalKey: "",
    proposalLoading: false,
    proposalError: "",
    proposalAction: "",
    proposalEdits: null,
    dispositionDraft: null,
    dispositionBusy: false,
    // Client-only identity for the browser action seam.  It is deliberately
    // separate from store/snapshot versions: a view can become stale even
    // when the repository snapshot has not changed.
    viewRevision: 0,
    nextDecision: "",
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
  let cancelFetchPoll = null;
  let fetchPollOwner = null;
  let stateLoadGen = 0;
  let workspaceGen = 0;
  let snapshotGen = 0;
  let fileQueueGen = 0;
  let relatedGen = 0;
  let bodyGen = 0;
  let overlapGen = 0;
  let proposalGen = 0;
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
  const dispositionRetries = new Map();
  const proposalRetries = new Map();
  let csrfToken = "";
  let sessionPromise = null;
  let enrichmentRequest = null;
  let stateInitialized = false;
  let seamExposed = false;

  const $ = (id) => document.getElementById(id);

  let writingUrl = false;
  let alignSidebar = true;

  const URL_DEFAULTS = {
    leftTab: "queue", selectedGroupId: null, selectedPr: null, selectedFile: null,
    selectedUser: null, filterQuery: "", filterLabel: "", queuePile: "needs_you",
  };

  const RESUME_FIELDS = [
    "leftTab", "selectedGroupId", "selectedPr", "selectedFile", "selectedUser",
    "filterQuery", "filterLabel", "queuePile",
  ];
  const READ_TOOL_NAMES = [
    "get_workspace", "list_groups", "search_prs", "get_group", "get_pr",
    "read_patch", "compare_prs", "find_related", "get_history",
  ];
  let resumeRepo = "";
  let urlViewFields = new Set();
  let urlRepoExplicit = false;

  function storageKey(repo) {
    return "omarchy.triage.resume:" + encodeURIComponent(String(repo || ""));
  }

  function readResume(repo) {
    if (!repo) return null;
    try {
      const raw = window.localStorage.getItem(storageKey(repo));
      if (!raw) return null;
      const value = JSON.parse(raw);
      return value && typeof value === "object" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function applyResume(value, onlyIfMissing) {
    if (!value || typeof value !== "object") return;
    RESUME_FIELDS.forEach((field) => {
      if (onlyIfMissing && urlViewFields.has(field)) return;
      if (!Object.prototype.hasOwnProperty.call(value, field)) return;
      const candidate = value[field];
      if (field === "leftTab" && !["queue", "groups", "allprs"].includes(candidate)) return;
      if (field === "queuePile" && !QUEUE_PILES.some((pile) => pile.id === candidate)) return;
      if (["selectedGroupId", "selectedFile", "selectedUser", "filterQuery", "filterLabel"].includes(field) &&
          candidate !== null && typeof candidate !== "string") return;
      if (field === "selectedPr" && candidate !== null &&
          (!Number.isSafeInteger(candidate) || candidate <= 0)) return;
      if (field === "filterQuery" && candidate && Array.from(candidate).length > 256) return;
      if (field === "selectedGroupId" && candidate && candidate.length > 256) return;
      if (field === "selectedFile" && candidate &&
          (candidate.length > 1024 || candidate.startsWith("/") || candidate.includes("\\") ||
           candidate.split("/").some((part) => part === ".."))) return;
      if (field === "selectedUser" && candidate && candidate.length > 256) return;
      if (field === "filterLabel" && candidate && !FILTER_CHIPS.includes(candidate)) return;
      state[field] = candidate;
    });
  }

  function persistResume() {
    const repo = state.repo || "";
    if (!repo) return;
    const value = {};
    RESUME_FIELDS.forEach((field) => { value[field] = state[field]; });
    try {
      window.localStorage.setItem(storageKey(repo), JSON.stringify(value));
      resumeRepo = repo;
    } catch (_) {
      // Private browsing and storage-disabled contexts should not break triage.
    }
  }

  function viewStateKey() {
    return RESUME_FIELDS.map((field) => JSON.stringify(state[field])).join("|");
  }

  function markViewChanged(before) {
    if (before === viewStateKey()) return false;
    state.viewRevision = Number.isSafeInteger(state.viewRevision)
      ? state.viewRevision + 1
      : 1;
    persistResume();
    return true;
  }

  function changeView(mutator) {
    const before = viewStateKey();
    mutator();
    const changed = markViewChanged(before);
    if (changed) {
      state.nextDecision = "";
      const select = $("saveNextDecision");
      if (select) select.value = "";
    }
    return changed;
  }

  function currentViewContext() {
    const group = state.selectedGroupId ? groupById(state.selectedGroupId) : null;
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const filters = {
      tab: state.leftTab,
      q: state.filterQuery || "",
      label: state.filterLabel || "",
      pile: state.queuePile || "needs_you",
    };
    return {
      repo: state.repo || "",
      source: state.source || ($("source") && $("source").value) || "",
      store_version: Number(state.storeVersion || 0),
      snapshot_version: Number(state.snapshotVersion || 0),
      view_revision: Number(state.viewRevision || 0),
      selected: {
        group_id: state.selectedGroupId || null,
        pr: state.selectedPr || null,
        path: state.selectedFile || null,
        user: state.selectedUser || null,
      },
      // Keep these flat fields for small WebMCP clients that do not want to
      // understand the nested display object.
      selected_group_id: state.selectedGroupId || null,
      selected_pr: state.selectedPr || null,
      selected_path: state.selectedFile || null,
      filters,
      group_snapshot_digest: group && group.snapshot_digest || null,
      pr_content_digest: pr && pr.content_digest || null,
    };
  }

  function staleViewError() {
    return {
      ok: false,
      error: {
        code: "stale_view",
        message: "The browser view changed; retrieve get_view_context and retry.",
        retryable: true,
        context: currentViewContext(),
      },
    };
  }

  function checkViewContext(ifContext) {
    if (!ifContext || typeof ifContext !== "object" || Array.isArray(ifContext)) {
      return {
        ok: false,
        error: {
          code: "invalid_request",
          message: "if_context is required for this browser action.",
          retryable: false,
          context: currentViewContext(),
        },
      };
    }
    const current = currentViewContext();
    if (typeof ifContext.repo !== "string" || !ifContext.repo || ifContext.repo.length > 256 ||
        !Number.isSafeInteger(ifContext.store_version) || ifContext.store_version < 0 ||
        !Number.isSafeInteger(ifContext.snapshot_version) || ifContext.snapshot_version < 0 ||
        !Number.isSafeInteger(ifContext.view_revision) || ifContext.view_revision < 0) {
      return {
        ok: false,
        error: {
          code: "invalid_request",
          message: "if_context must include repo and integer store/snapshot/view revisions.",
          retryable: false,
          context: current,
        },
      };
    }
    if (ifContext.repo !== current.repo ||
        ifContext.store_version !== current.store_version ||
        ifContext.snapshot_version !== current.snapshot_version ||
        ifContext.view_revision !== current.view_revision) {
      return staleViewError();
    }
    for (const key of ["group_snapshot_digest", "pr_content_digest"]) {
      if (Object.prototype.hasOwnProperty.call(ifContext, key) &&
          (ifContext[key] !== null && typeof ifContext[key] !== "string" ||
           ifContext[key] !== current[key])) {
        return staleViewError();
      }
    }
    return { ok: true, context: current };
  }

  function readUrl() {
    Object.assign(state, URL_DEFAULTS);
    urlViewFields = new Set();
    const q = new URLSearchParams(location.search);
    urlRepoExplicit = q.has("repo") && !!q.get("repo");
    const repoHint = q.get("repo") || "omacom/omarchy";
    state.repoDraft = repoHint;
    if ($("repo")) $("repo").value = repoHint;
    resumeRepo = repoHint;
    applyResume(readResume(repoHint), false);
    const has = (key, field) => q.has(key) && (urlViewFields.add(field || key), true);
    const tab = q.get("tab");
    if (has("tab", "leftTab")) state.leftTab = tab === "groups" || tab === "allprs" ? tab : "queue";
    if (has("group", "selectedGroupId")) state.selectedGroupId = q.get("group") || null;
    if (has("pr", "selectedPr")) {
      const pr = q.get("pr");
      state.selectedPr = pr && /^\d+$/.test(pr) ? parseInt(pr, 10) : null;
    }
    if (has("file", "selectedFile")) state.selectedFile = q.get("file") || null;
    if (has("user", "selectedUser")) state.selectedUser = q.get("user") || null;
    if (has("q", "filterQuery")) state.filterQuery = Array.from(q.get("q") || "").slice(0, 256).join("");
    if (has("label", "filterLabel")) {
      const label = q.get("label") || "";
      state.filterLabel = FILTER_CHIPS.includes(label) ? label : "";
    }
    if (has("pile", "queuePile")) {
      const pile = q.get("pile");
      state.queuePile = QUEUE_PILES.some((p) => p.id === pile) ? pile : "needs_you";
    }
  }

  function writeUrl(push) {
    const q = new URLSearchParams();
    const activeRepo = state.repo || state.repoDraft || "";
    if (activeRepo) q.set("repo", activeRepo);
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

  function scopedGet(path, repo) {
    if (!repo) return path;
    return path + (path.includes("?") ? "&" : "?") +
      "repo=" + encodeURIComponent(repo);
  }

  function validWorkspaceRepo(repo) {
    if (typeof repo !== "string") return false;
    const value = repo.trim();
    if (!value || value.length > 256 || /[\u0000-\u001f\u007f]/.test(value)) return false;
    const parts = value.split("/");
    return parts.length === 2 && parts.every((part) => part !== "." && part !== ".." &&
      /^[A-Za-z0-9_.-]+$/.test(part));
  }

  function canonicalWorkspaceRepo(repo) {
    const value = String(repo || "").trim();
    return validWorkspaceRepo(value) ? value.toLowerCase() : "";
  }

  function renderWorkspaceControls() {
    const active = $("activeWorkspace");
    if (active) {
      active.textContent = state.workspaceSwitching
        ? "opening workspace: " + (state.repo || state.repoDraft || "?")
        : state.repo
          ? "active workspace: " + state.repo +
            (state.workspaceInitialized === false ? " · empty" : "")
          : "active workspace: none";
    }
    const picker = $("workspaceChoices");
    if (!picker) return;
    const prior = picker.value;
    picker.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = state.workspaces.length ? "choose saved workspace…" :
      state.workspaceMode === "single" ? "fixed store" : "no saved workspaces";
    picker.appendChild(blank);
    const repos = state.workspaces.map((item) =>
      typeof item === "string" ? item : item && item.repo
    ).filter((repo) => validWorkspaceRepo(repo));
    const all = [...new Set(repos.concat([state.repo, state.repoDraft].filter(validWorkspaceRepo)))];
    all.slice(0, 200).forEach((repo) => {
      const option = document.createElement("option");
      option.value = repo;
      option.textContent = repo;
      picker.appendChild(option);
    });
    picker.value = all.includes(prior) ? prior : "";
  }

  function hasUnsavedDecisionEdit() {
    const draft = state.dispositionDraft;
    const pr = draft && draft.pr ? prByNumber(draft.pr) : null;
    if (!draft || !pr) return false;
    return draft.disposition !== (DISPOSITION_VALUES.includes(pr.disposition) ? pr.disposition : "pending") ||
      String(draft.reason || "") !== String(pr.disposition_reason || "") ||
      (draft.duplicate_of || null) !== (pr.duplicate_of || null);
  }

  function hasUnsavedProposalEdit() {
    const proposal = state.proposal;
    const edits = state.proposalEdits;
    if (!proposal || !edits || !Array.isArray(edits.items)) return false;
    const normalize = (item) => ({
      pr: item.pr,
      disposition: item.disposition || "pending",
      reason: item.reason || "",
      duplicate_of: item.duplicate_of || null,
      revision: item.revision || null,
      duplicate_of_revision: item.duplicate_of_revision || null,
    });
    const original = {
      canonical_pr: proposal.canonical_pr || null,
      items: (proposal.items || []).map(normalize),
    };
    const current = {
      canonical_pr: edits.canonical_pr || null,
      items: edits.items.map(normalize),
    };
    const rejectReason = $("proposalRejectReason");
    return JSON.stringify(original) !== JSON.stringify(current) ||
      !!(rejectReason && String(rejectReason.value || "").trim());
  }

  function confirmWorkspaceSwitch() {
    const warnings = [];
    if (hasUnsavedDecisionEdit()) warnings.push("per-PR decision edits");
    if (hasUnsavedProposalEdit()) warnings.push("proposal edits");
    if (!warnings.length) return true;
    if (typeof window.confirm !== "function") return false;
    return window.confirm("Switch workspace and discard unsaved " + warnings.join(" and ") + "?");
  }

  async function switchWorkspace(repo, options) {
    const rawTarget = String(repo || "").trim();
    const target = canonicalWorkspaceRepo(rawTarget);
    if (!target) {
      setStatus("workspace must be owner/repo using safe repository characters");
      return false;
    }
    if (target === state.repo && stateInitialized && !state.workspaceSwitching) {
      state.repoDraft = target;
      renderWorkspaceControls();
      return true;
    }
    if (state.workspaceMode === "single" && state.repo && target !== state.repo && stateInitialized) {
      setStatus("fixed-store mode is bound to " + state.repo + "; Sync or restart with workspace mode to change it");
      if ($("repo")) $("repo").value = state.repo;
      state.repoDraft = state.repo;
      renderWorkspaceControls();
      return false;
    }
    if (!(options && options.skipConfirm) && !confirmWorkspaceSwitch()) return false;
    const token = ++workspaceGen;
    stateLoadGen += 1;
    if (cancelFetchPoll) cancelFetchPoll();
    else if (fetchPollTimer) clearTimeout(fetchPollTimer);
    fetchPollTimer = null;
    state.fetching = false;
    const fetchButton = $("fetchBtn");
    if (fetchButton) fetchButton.disabled = false;
    invalidateSnapshotCaches();
    state.repo = target;
    state.repoDraft = target;
    state.source = "";
    state.groups = [];
    state.prs = [];
    state.proposals = [];
    state.rules = [];
    state.overlap = {};
    state.queue = {};
    state.new_pr_numbers = [];
    state.storeVersion = 0;
    state.snapshotVersion = 0;
    state.sync = null;
    state.selectedGroupId = null;
    state.selectedPr = null;
    state.selectedFile = null;
    state.selectedUser = null;
    state.workspaceInitialized = false;
    state.workspaceLegacy = false;
    state.workspaceSwitching = true;
    stateInitialized = false;
    urlViewFields = new Set();
    resumeRepo = target;
    applyResume(readResume(target), false);
    state.viewRevision = Number.isSafeInteger(state.viewRevision) ? state.viewRevision + 1 : 1;
    const repoInput = $("repo");
    if (repoInput) repoInput.value = target;
    if ($("listFilter")) $("listFilter").value = state.filterQuery || "";
    paintLabelFilters();
    paintPileNav();
    renderWorkspaceControls();
    if (options && options.fromUrl) {
      setLeftTab(state.leftTab, true);
      renderDetail();
    } else {
      render();
    }
    setStatus("opening workspace " + target + "…");
    const loaded = await loadState(target, { token });
    if (token === workspaceGen) {
      state.workspaceSwitching = false;
      renderWorkspaceControls();
      if (!loaded) render();
      else if (!state.selectedGroupId && !state.selectedFile) renderDetail();
    }
    return !!loaded;
  }

  function setStatus(msg) {
    const root = $("status");
    if (root) root.textContent = msg;
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
    proposalGen += 1;
    state.fileQueue = null;
    state.related = null;
    state.relatedKey = "";
    state.examinedMembers = new Set();
    state.agentOpenedMembers = new Set();
    state.proposal = null;
    state.proposalKey = "";
    state.proposalLoading = false;
    state.proposalError = "";
    state.proposalAction = "";
    state.proposalEdits = null;
    state.dispositionDraft = null;
    state.dispositionBusy = false;
    state.nextDecision = "";
    const saveNextDecision = $("saveNextDecision");
    if (saveNextDecision) saveNextDecision.value = "";
    lastBodyPr = null;
    Object.keys(bodyCache).forEach((key) => delete bodyCache[key]);
    for (const entry of resourceRequests.values()) entry.controller.abort();
    resourceRequests.clear();
    if (enrichmentRequest) enrichmentRequest.controller.abort();
    enrichmentRequest = null;
    selectorCache.clear();
  }

  function applyState(data) {
    const viewBefore = viewStateKey();
    invalidateSnapshotCaches();
    state.groups = data.groups || [];
    state.prs = data.prs || [];
    state.proposals = Array.isArray(data.proposals) ? data.proposals.slice(0, 200) : [];
    state.rules = data.rules || [];
    state.overlap = data.overlap || {};
    state.source = data.source || "";
    state.repo = data.repo || state.repo || "";
    state.queue = data.queue || {};
    state.new_pr_numbers = data.new_pr_numbers || [];
    state.storeVersion = Number(data.store_version || 0);
    state.snapshotVersion = Number(data.snapshot_version || 0);
    state.sync = data.sync || null;
    const workspace = data.workspace && typeof data.workspace === "object" ? data.workspace : null;
    state.workspaceMode = workspace && (workspace.mode === "multi" || workspace.mode === "single")
      ? workspace.mode : state.workspaceMode;
    state.workspaceInitialized = workspace && typeof workspace.initialized === "boolean"
      ? workspace.initialized : !!(state.groups.length || state.prs.length || state.sync);
    state.workspaceLegacy = !!(workspace && workspace.legacy);
    // A browser can be pointed at a different repository after a reload.
    // Rehydrate only the fields that were not explicit in the URL.
    if (state.repo && state.repo !== resumeRepo) {
      resumeRepo = state.repo;
      applyResume(readResume(state.repo), true);
    }
    rebuildIndexes();
    queueRowsCache = null;
    if (data.source) {
      $("source").value = ["gh", "github"].includes(data.source) ? "gh" : data.source;
    }
    if (state.repo) {
      state.repoDraft = state.repo;
      if ($("repo")) $("repo").value = state.repo;
    }
    if ($("listFilter")) $("listFilter").value = state.filterQuery || "";
    paintLabelFilters();
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
    renderWorkspaceControls();
    // Keep a corrupt or no-longer-valid saved target from recurring after the
    // next reload.  IDs are normalized above before this write.
    if (stateInitialized) markViewChanged(viewBefore);
    stateInitialized = true;
    persistResume();
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

  function markHumanExamined(number) {
    if (!Number.isSafeInteger(number) || number <= 0) return;
    state.examinedMembers.add(number);
    state.agentOpenedMembers.delete(number);
  }

  function markAgentOpened(number) {
    if (!Number.isSafeInteger(number) || number <= 0) return;
    if (!state.examinedMembers.has(number)) state.agentOpenedMembers.add(number);
  }

  function revisionRefFromPr(pr, evidence) {
    const source = evidence || pr || {};
    return {
      pr_number: Number(pr && pr.number || source.number || 0),
      head_sha: source.head_sha || (pr && pr.head_sha) || "",
      base_sha: source.base_sha || (pr && pr.base_sha) || "",
      additions: source.additions == null ? (pr && pr.additions != null ? pr.additions : null) : source.additions,
      deletions: source.deletions == null ? (pr && pr.deletions != null ? pr.deletions : null) : source.deletions,
      content_digest: source.content_digest || (pr && pr.content_digest) || "",
      evidence_complete: source.evidence_complete === true || (pr && pr.evidence_complete === true),
      source: source.evidence_source || source.source || (pr && pr.evidence_source) || state.source || "",
      cache_snapshot_id: source.cache_snapshot_id || (pr && pr.cache_snapshot_id) || "",
    };
  }

  function makeIdempotencyKey(prefix) {
    const uuid = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2);
    return String(prefix || "triage") + "-" + uuid;
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
      changeView(() => {
        state.selectedGroupId = g.group_id;
        state.selectedPr = null;
        state.selectedFile = null;
        state.selectedUser = null;
      });
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
      changeView(() => {
        state.selectedGroupId = pr.group_id || state.selectedGroupId;
        state.selectedPr = pr.number;
        state.selectedFile = null;
        state.selectedUser = null;
      });
      markHumanExamined(pr.number);
      // Keep All PRs tab visible but refresh detail
      render();
    });
    return row;
  }

  function setLeftTab(tab, fromUrl) {
    if (tab !== "queue" && tab !== "groups" && tab !== "allprs") tab = "queue";
    if (tab !== state.leftTab) alignSidebar = true;
    if (!fromUrl && tab !== state.leftTab) {
      const before = viewStateKey();
      state.leftTab = tab;
      markViewChanged(before);
    }
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
    } else if (!group && pr && pr.group_id && groupById(pr.group_id)) {
      state.selectedGroupId = pr.group_id;
      group = groupById(pr.group_id);
      changed = true;
    }
    if (group && state.selectedPr && !(group.pr_numbers || []).includes(state.selectedPr)) {
      state.selectedPr = null;
      changed = true;
    }
    if (state.selectedFile && !validRepoPath(state.selectedFile)) {
      state.selectedFile = null;
      changed = true;
    } else if (state.selectedFile && group) {
      const pathBelongs = pr
        ? (pr.paths || []).includes(state.selectedFile)
        : (group.pr_numbers || []).some((number) => {
            const member = prByNumber(number);
            return member && (member.paths || []).includes(state.selectedFile);
          });
      if (!pathBelongs) {
        state.selectedFile = null;
        changed = true;
      }
    } else if (state.selectedFile && !state.prs.some((item) =>
      (item.paths || []).includes(state.selectedFile))) {
      state.selectedFile = null;
      changed = true;
    }
    if (state.selectedUser && !state.prs.some((item) => item.user === state.selectedUser)) {
      state.selectedUser = null;
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
          changeView(() => {
            state.filterLabel = state.filterLabel === name ? "" : name;
          });
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
          changeView(() => { state.queuePile = p.id; });
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
      let url = "/api/related?pr=" + encodeURIComponent(pr) +
        "&repo=" + encodeURIComponent(state.repo);
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
        changeView(() => {
          if (it.group_id) state.selectedGroupId = it.group_id;
          state.selectedPr = it.number;
          state.selectedFile = null;
          state.selectedUser = null;
        });
        markHumanExamined(it.number);
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
    changeView(() => {
      state.selectedGroupId = null;
      state.selectedPr = null;
      state.selectedFile = path;
      state.selectedUser = null;
      if (state.queuePile !== "hotspots") state.queuePile = "hotspots";
    });
    state.fileQueue = { path: path, prs: [], loading: true, same_patch: [] };
    queueRowsCache = null;
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
        "/api/file?path=" + encodeURIComponent(path) + "&page=" + page +
          "&page_size=40&repo=" + encodeURIComponent(state.repo));
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
        changeView(() => {
          if (pr.group_id) state.selectedGroupId = pr.group_id;
          state.selectedPr = pr.number;
          state.selectedUser = null;
        });
        markHumanExamined(pr.number);
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
    changeView(() => { state.selectedUser = name; });
    renderUserDrawer();
    writeUrl(true);
  }

  function closeUserDrawer() {
    if (!state.selectedUser) return;
    changeView(() => { state.selectedUser = null; });
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
        changeView(() => {
          if (gid && gid !== "?") state.selectedGroupId = gid;
          state.selectedPr = members[0].number;
          state.selectedFile = null;
          state.selectedUser = null;
        });
        markHumanExamined(members[0].number);
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
          changeView(() => {
            if (pr.group_id) state.selectedGroupId = pr.group_id;
            state.selectedPr = pr.number;
            state.selectedFile = null;
            state.selectedUser = null;
          });
          markHumanExamined(pr.number);
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
        changeView(() => {
          if (gid && gid !== "?") state.selectedGroupId = gid;
          state.selectedPr = prs[0].number;
          state.selectedUser = null;
        });
        markHumanExamined(prs[0].number);
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
    renderProposalChooser(null);
    renderDispositionPanel(null, null);
    renderProposalPanel();
    empty.classList.add("hidden");
    detail.classList.remove("hidden");
    detail.classList.add("file-view");
    const path = state.selectedFile;
    const fq = state.fileQueue;
    const saveNext = $("saveNextBtn");
    if (saveNext) saveNext.disabled = true;
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
      renderProposalChooser(null);
      renderDispositionPanel(null, null);
      renderProposalPanel();
      renderFileDetail();
      return;
    }
    if (!g) {
      renderProposalChooser(null);
      renderDispositionPanel(null, null);
      renderProposalPanel();
      loadRelatedIfOpen(null, null);
      renderDiffs(null);
      empty.classList.remove("hidden");
      detail.classList.add("hidden");
      empty.textContent = state.workspaceSwitching
        ? "Opening " + (state.repo || "workspace") + "…"
        : state.repo && state.workspaceInitialized === false
          ? "Workspace " + state.repo + " is empty. Use Sync to fetch it explicitly."
          : state.repo
            ? "No group selected in " + state.repo
            : "Open a workspace to begin";
      const saveNext = $("saveNextBtn");
      if (saveNext) saveNext.disabled = true;
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
    renderProposalChooser(g);
    renderDispositionPanel(g, pr);
    renderProposalPanel();

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
        changeView(() => {
          state.selectedPr = num;
          state.selectedUser = null;
        });
      markHumanExamined(num);
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
    const agentOpened = numbers.filter((number) =>
      state.agentOpenedMembers.has(number) && !state.examinedMembers.has(number)
    );
    const complete = group.evidence_complete === true && incomplete.length === 0;
    root.className = "decision-warning " + (complete ? "complete" : "incomplete");
    root.textContent = numbers.length + " exact member revision" + (numbers.length === 1 ? "" : "s") +
      " · snapshot " + (group.snapshot_digest || "identity missing") +
      (unexamined.length ? " · " + unexamined.length + " not individually opened" : " · all opened") +
      (agentOpened.length ? " · " + agentOpened.length + " opened by agent (not attested)" : "") +
      (incomplete.length ? " · " + incomplete.length + " incomplete" : " · complete evidence");
    const approve = $("blessBtn");
    approve.disabled = !complete;
    approve.title = complete
      ? "Approve only these repository-bound revisions"
      : "Approval requires complete head, base, and content evidence for every member";
    const saveNext = $("saveNextBtn");
    if (saveNext) {
      saveNext.disabled = !group;
      saveNext.title = group
        ? "Save the explicitly chosen decision, then move within this filtered pending scope"
        : "Select a group first";
    }
  }

  function renderProposalChooser(group) {
    const root = $("proposalChooser");
    if (!root) return;
    if (!group) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const gid = group.group_id;
    const picker = $("proposalPicker");
    const input = $("proposalIdInput");
    const open = $("proposalOpenBtn");
    const scope = $("proposalChooserScope");
    const hint = $("proposalChooserHint");
    const rows = (Array.isArray(state.proposals) ? state.proposals : [])
      .filter((proposal) => proposal && proposal.group_id === gid &&
        typeof proposal.proposal_id === "string" && proposal.proposal_id)
      .sort((left, right) => String(right.updated_at || right.created_at || "")
        .localeCompare(String(left.updated_at || left.created_at || "")));
    if (scope) scope.textContent = gid + " · latest 200 workspace proposals max";
    if (picker) {
      const prior = picker.value;
      picker.innerHTML = "";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "choose a proposal…";
      picker.appendChild(blank);
      rows.forEach((proposal) => {
        const option = document.createElement("option");
        option.value = proposal.proposal_id;
        option.textContent = proposal.proposal_id + " · " + (proposal.status || "draft");
        picker.appendChild(option);
      });
      if (rows.some((proposal) => proposal.proposal_id === prior)) picker.value = prior;
      picker.onchange = () => {
        if (input) input.value = picker.value || "";
      };
    }
    if (root.dataset.group !== gid) {
      root.dataset.group = gid;
      if (input) input.value = "";
    }
    if (hint) hint.textContent = rows.length
      ? "Select a recent draft or paste an ID; opening never accepts or applies it."
      : "No recent proposal is listed for this group; paste an ID from the agent or proposal log.";
    if (open) {
      open.disabled = false;
      open.onclick = async () => {
        const id = String((input && input.value || picker && picker.value || "")).trim();
        if (!id) {
          setStatus("choose a recent proposal or paste its ID first");
          return;
        }
        const result = await showProposal(id, currentViewContext());
        if (!result || result.ok !== true) {
          const message = result && result.error && result.error.message;
          setStatus("proposal could not be opened" + (message ? ": " + message : ""));
        }
      };
    }
  }

  const DISPOSITION_VALUES = ["pending", "keep", "duplicate", "reject", "needs_hardware", "upgrade"];

  function exactPrEvidence(number) {
    const cached = bodyCache[snapshotKey() + "|" + number];
    return cached && typeof cached.then !== "function" ? cached : null;
  }

  function ensureDispositionDraft(pr) {
    if (!pr) return null;
    if (!state.dispositionDraft || state.dispositionDraft.pr !== pr.number) {
      state.dispositionDraft = {
        pr: pr.number,
        disposition: DISPOSITION_VALUES.includes(pr.disposition) ? pr.disposition : "pending",
        reason: pr.disposition_reason || "",
        duplicate_of: pr.duplicate_of || null,
      };
    }
    return state.dispositionDraft;
  }

  function renderDispositionPanel(group, pr) {
    const root = $("prDispositionPanel");
    if (!root) return;
    if (!group || !pr || !(group.pr_numbers || []).includes(pr.number)) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const draft = ensureDispositionDraft(pr);
    const label = $("dispositionPrLabel");
    if (label) label.textContent = "#" + pr.number + (pr.disposition && pr.disposition !== "pending"
      ? " · current " + pr.disposition : " · not yet decided");
    const select = $("dispositionSelect");
    if (select) {
      select.value = DISPOSITION_VALUES.includes(draft.disposition) ? draft.disposition : "pending";
      select.onchange = () => {
        draft.disposition = select.value;
        renderDispositionPanel(group, pr);
      };
    }
    const canonical = $("dispositionCanonical");
    if (canonical) {
      canonical.innerHTML = "";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "choose…";
      canonical.appendChild(none);
      (group.pr_numbers || []).forEach((number) => {
        if (number === pr.number) return;
        const option = document.createElement("option");
        option.value = String(number);
        option.textContent = "#" + number;
        canonical.appendChild(option);
      });
      canonical.value = draft.duplicate_of ? String(draft.duplicate_of) : "";
      canonical.disabled = draft.disposition !== "duplicate";
      canonical.onchange = () => { draft.duplicate_of = canonical.value ? Number(canonical.value) : null; };
    }
    const canonicalWrap = $("canonicalWrap");
    if (canonicalWrap) canonicalWrap.hidden = draft.disposition !== "duplicate";
    const reason = $("dispositionReason");
    if (reason) {
      reason.value = draft.reason || "";
      reason.oninput = () => { draft.reason = reason.value; };
    }
    const save = $("dispositionSaveBtn");
    if (save) {
      save.disabled = !!state.dispositionBusy;
      save.textContent = state.dispositionBusy ? "Saving…" : "Save per-PR disposition";
      save.onclick = saveSelectedDisposition;
    }
    const hint = $("dispositionHint");
    if (hint) hint.textContent = pr.disposition_revision
      ? "Bound to the currently displayed exact revision. A changed revision must be reviewed again."
      : "This decision applies only to this PR revision; it does not decide its siblings.";
  }

  function proposalGroup() {
    const proposal = state.proposal;
    return proposal && proposal.group_id ? groupById(proposal.group_id) : null;
  }

  function proposalEditRows(proposal) {
    if (!proposal || !Array.isArray(proposal.items)) return [];
    if (!state.proposalEdits || state.proposalEdits.proposal_id !== proposal.proposal_id) {
      state.proposalEdits = {
        proposal_id: proposal.proposal_id,
        canonical_pr: proposal.canonical_pr || null,
        items: proposal.items.map((item) => ({
          pr: item.pr,
          disposition: DISPOSITION_VALUES.includes(item.disposition) ? item.disposition : "pending",
          reason: item.reason || "",
          duplicate_of: item.duplicate_of || null,
          revision: item.revision,
          duplicate_of_revision: item.duplicate_of_revision || null,
        })),
      };
    }
    return state.proposalEdits.items;
  }

  function renderProposalPanel() {
    const root = $("proposalBlock");
    if (!root) return;
    const proposal = state.proposal;
    const group = proposalGroup();
    if (!proposal || !group || state.selectedGroupId !== proposal.group_id) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const summary = $("proposalSummary");
    if (summary) summary.textContent = (state.proposalLoading ? "Loading agent proposal… · " : "Agent proposal · " +
      (proposal.status || "draft") + " · ") + proposal.proposal_id;
    const meta = $("proposalMeta");
    if (meta) {
      const provenance = proposal.provenance || {};
      const context = proposal.context || {};
      meta.textContent = "Proposal is advisory; a human must inspect and confirm it" +
        (provenance.actor ? " · actor " + provenance.actor : "") +
        (context.group_snapshot_digest ? " · snapshot-bound" : " · snapshot binding unavailable");
    }
    const rows = proposalEditRows(proposal);
    const itemsRoot = $("proposalItems");
    if (itemsRoot) {
      itemsRoot.innerHTML = "";
      const locked = state.proposalLoading || !["draft", "edited"].includes(proposal.status);
      rows.forEach((item) => {
        const row = document.createElement("div");
        row.className = "proposal-item";
        const number = document.createElement("strong");
        number.className = "mono";
        number.textContent = "#" + item.pr;
        row.appendChild(number);
        const revision = document.createElement("div");
        revision.className = "revision-id";
        const exact = item.revision || {};
        revision.textContent = "head " + (exact.head_sha || "missing") +
          " · base " + (exact.base_sha || "missing") +
          " · content " + (exact.content_digest || "missing") +
          (exact.evidence_complete === true ? " · complete" : " · incomplete");
        row.appendChild(revision);
        const dispositionLabel = document.createElement("label");
        dispositionLabel.textContent = "disposition";
        const disposition = document.createElement("select");
        DISPOSITION_VALUES.forEach((value) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value === "needs_hardware" ? "needs hardware" :
            value === "upgrade" ? "can break upgrade" : value;
          dispositionLabel.appendChild(option);
        });
        disposition.value = item.disposition;
        disposition.disabled = locked;
        disposition.addEventListener("change", () => {
          item.disposition = disposition.value;
          if (item.disposition !== "duplicate") {
            item.duplicate_of = null;
            item.duplicate_of_revision = null;
          }
          renderProposalPanel();
        });
        row.appendChild(dispositionLabel);
        const canonicalLabel = document.createElement("label");
        canonicalLabel.textContent = "canonical PR";
        const canonical = document.createElement("select");
        const none = document.createElement("option");
        none.value = "";
        none.textContent = "choose…";
        canonical.appendChild(none);
        (group.pr_numbers || []).forEach((numberValue) => {
          if (numberValue === item.pr) return;
          const option = document.createElement("option");
          option.value = String(numberValue);
          option.textContent = "#" + numberValue;
          canonical.appendChild(option);
        });
        canonical.value = item.duplicate_of ? String(item.duplicate_of) : "";
        canonical.disabled = locked || item.disposition !== "duplicate";
        canonical.addEventListener("change", () => {
          item.duplicate_of = canonical.value ? Number(canonical.value) : null;
          item.duplicate_of_revision = null;
        });
        canonicalLabel.appendChild(canonical);
        row.appendChild(canonicalLabel);
        const reasonLabel = document.createElement("label");
        reasonLabel.className = "proposal-reason";
        reasonLabel.textContent = "reason";
        const reason = document.createElement("textarea");
        reason.rows = 2;
        reason.maxLength = 1000;
        reason.value = item.reason || "";
        reason.disabled = locked;
        reason.addEventListener("input", () => { item.reason = reason.value; });
        reasonLabel.appendChild(reason);
        row.appendChild(reasonLabel);
        itemsRoot.appendChild(row);
      });
    }
    const canonical = $("proposalCanonical");
    if (canonical) {
      canonical.innerHTML = "";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "none";
      canonical.appendChild(none);
      (group.pr_numbers || []).forEach((number) => {
        const option = document.createElement("option");
        option.value = String(number);
        option.textContent = "#" + number;
        canonical.appendChild(option);
      });
      canonical.value = state.proposalEdits.canonical_pr ? String(state.proposalEdits.canonical_pr) : "";
      canonical.disabled = !["draft", "edited"].includes(proposal.status);
      canonical.onchange = () => { state.proposalEdits.canonical_pr = canonical.value ? Number(canonical.value) : null; };
    }
    const rejectReason = $("proposalRejectReason");
    const accept = $("proposalAcceptBtn");
    const edit = $("proposalEditBtn");
    const reject = $("proposalRejectBtn");
    const locked = state.proposalLoading || !["draft", "edited"].includes(proposal.status) || !!state.proposalAction;
    if (rejectReason) rejectReason.disabled = locked;
    if (accept) { accept.disabled = locked; accept.textContent = state.proposalAction === "accept" ? "Accepting…" : "Accept proposal"; accept.onclick = () => transitionProposal("accept"); }
    if (edit) { edit.disabled = locked; edit.textContent = state.proposalAction === "edit" ? "Saving…" : "Save edits"; edit.onclick = () => transitionProposal("edit"); }
    if (reject) { reject.disabled = locked; reject.textContent = state.proposalAction === "reject" ? "Rejecting…" : "Reject proposal"; reject.onclick = () => transitionProposal("reject"); }
    const hint = $("proposalHint");
    if (hint) hint.textContent = state.proposalLoading ? "Loading proposal…" : state.proposalError ||
      (proposal.status === "accepted" ? "Accepted; recorded exact human dispositions." :
        proposal.status === "rejected" ? "Rejected; no dispositions were applied." :
          "Edit the bounded items if needed, then explicitly accept or reject.");
  }

  async function proposalItemsForWrite(action) {
    const proposal = state.proposal;
    const edits = state.proposalEdits;
    const group = proposalGroup();
    if (!proposal || !edits || !group) throw new Error("proposal is not open in the selected group");
    if (action === "reject") return null;
    const items = [];
    for (const edit of edits.items) {
      const reason = String(edit.reason || "").trim();
      if (!reason || reason.length > 1000) throw new Error("each proposal item needs a reason of 1-1000 characters");
      const pr = prByNumber(edit.pr);
      if (!pr || !(group.pr_numbers || []).includes(edit.pr)) throw new Error("proposal PR is not in the selected group");
      const revision = edit.revision || revisionRefFromPr(pr, await loadPrBody(edit.pr));
      const item = { pr: edit.pr, revision, disposition: edit.disposition, reason };
      if (edit.disposition === "duplicate") {
        if (!edit.duplicate_of || edit.duplicate_of === edit.pr || !(group.pr_numbers || []).includes(edit.duplicate_of)) {
          throw new Error("duplicate items need another canonical PR in this group");
        }
        const target = prByNumber(edit.duplicate_of);
        if (!target) throw new Error("canonical PR is not in the current snapshot");
        item.duplicate_of = edit.duplicate_of;
        item.duplicate_of_revision = edit.duplicate_of_revision ||
          revisionRefFromPr(target, await loadPrBody(edit.duplicate_of));
      }
      items.push(item);
    }
    return items;
  }

  async function transitionProposal(action) {
    const proposal = state.proposal;
    if (!proposal || !["accept", "edit", "reject"].includes(action) || state.proposalAction) return false;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    const expected = currentViewContext();
    const identity = [snapshotKey(), proposal.proposal_id, action,
      JSON.stringify(state.proposalEdits || {}), $("proposalRejectReason") && $("proposalRejectReason").value || ""].join("|");
    let idempotencyKey = proposalRetries.get(identity);
    if (!idempotencyKey) {
      idempotencyKey = makeIdempotencyKey("proposal");
      proposalRetries.set(identity, idempotencyKey);
    }
    state.proposalAction = action;
    renderProposalPanel();
    try {
      const items = await proposalItemsForWrite(action);
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      if (currentViewContext().view_revision !== expected.view_revision ||
          currentViewContext().store_version !== expected.store_version ||
          state.proposalKey !== proposal.proposal_id) {
        state.proposalAction = "";
        renderProposalPanel();
        return false;
      }
      const body = {
        repo: state.repo,
        actor: "human",
        expected_store_version: expected.store_version,
        expected_snapshot_version: expected.snapshot_version,
        idempotency_key: idempotencyKey,
      };
      if (action === "reject") {
        const reason = String($("proposalRejectReason") && $("proposalRejectReason").value || "").trim();
        if (!reason || reason.length > 1000) throw new Error("a rejection reason of 1-1000 characters is required");
        body.reason = reason;
        if (state.proposalEdits && state.proposalEdits.canonical_pr) body.canonical_pr = state.proposalEdits.canonical_pr;
      } else {
        body.items = items;
        if (state.proposalEdits && state.proposalEdits.canonical_pr) body.canonical_pr = state.proposalEdits.canonical_pr;
      }
      const data = await api("/api/proposals/" + encodeURIComponent(proposal.proposal_id) + "/" + action, {
        method: "POST", body: JSON.stringify(body),
      });
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      if (currentViewContext().store_version !== expected.store_version && action !== "reject") {
        // The transition response is still authoritative; apply its returned
        // state below rather than letting another render overwrite it.
      }
      proposalRetries.delete(identity);
      state.proposalAction = "";
      if (data.state) applyState(data.state);
      state.proposal = data.proposal || proposal;
      state.proposalKey = state.proposal.proposal_id;
      render();
      const hint = action === "accept" ? "proposal accepted" : action === "edit" ? "proposal edits saved" : "proposal rejected";
      setStatus(hint + " · human confirmation recorded");
      return { ok: true, proposal: state.proposal, context: currentViewContext() };
    } catch (error) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      state.proposalAction = "";
      let failureMessage;
      if (error.status === 409) {
        state.proposalError = "proposal is stale; reload the current snapshot before retrying";
        setStatus("proposal not saved: " + error.message);
        failureMessage = state.proposalError;
        await loadState();
      } else {
        state.proposalError = error.message || "proposal action failed";
        setStatus("proposal error: " + state.proposalError);
        failureMessage = state.proposalError;
      }
      renderProposalPanel();
      return { ok: false, error: { code: error.status === 409 ? "stale_snapshot" : "invalid_request", message: failureMessage, retryable: error.status === 409 } };
    }
  }

  async function saveSelectedDisposition() {
    const group = groupById(state.selectedGroupId);
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const draft = ensureDispositionDraft(pr);
    if (!group || !pr || !draft || !(group.pr_numbers || []).includes(pr.number) || state.dispositionBusy) return false;
    const reason = String(draft.reason || "").trim();
    if (!reason || reason.length > 1000) {
      setStatus("per-PR disposition needs a reason of 1-1000 characters");
      return false;
    }
    const expected = currentViewContext();
    const selectedPr = pr.number;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    const identity = [snapshotKey(), group.group_id, selectedPr, draft.disposition,
      draft.duplicate_of || "", reason].join("|");
    let idempotencyKey = dispositionRetries.get(identity);
    if (!idempotencyKey) {
      idempotencyKey = makeIdempotencyKey("disposition");
      dispositionRetries.set(identity, idempotencyKey);
    }
    state.dispositionBusy = true;
    renderDispositionPanel(group, pr);
    try {
      const evidence = await loadPrBody(selectedPr);
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return false;
      if (currentViewContext().view_revision !== expected.view_revision || state.selectedPr !== selectedPr) {
        state.dispositionBusy = false;
        renderDispositionPanel(groupById(state.selectedGroupId), prByNumber(state.selectedPr));
        return false;
      }
      const item = {
        pr: selectedPr,
        revision: revisionRefFromPr(pr, evidence),
        disposition: draft.disposition,
        reason,
        duplicate_of: null,
      };
      if (draft.disposition === "duplicate") {
        const canonical = Number(draft.duplicate_of);
        if (!canonical || canonical === selectedPr || !(group.pr_numbers || []).includes(canonical)) throw new Error("duplicate needs another canonical PR in this group");
        const canonicalPr = prByNumber(canonical);
        if (!canonicalPr) throw new Error("canonical PR is not in the current snapshot");
        item.duplicate_of = canonical;
        item.duplicate_of_revision = revisionRefFromPr(canonicalPr, await loadPrBody(canonical));
      }
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return false;
      if (currentViewContext().view_revision !== expected.view_revision || state.selectedPr !== selectedPr) {
        state.dispositionBusy = false;
        renderDetail();
        return false;
      }
      const data = await api("/api/dispositions", {
        method: "POST",
        body: JSON.stringify({
          repo: state.repo,
          group_id: group.group_id,
          items: [item],
          expected_store_version: expected.store_version,
          expected_snapshot_version: expected.snapshot_version,
          idempotency_key: idempotencyKey,
          actor: "human",
        }),
      });
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return false;
      dispositionRetries.delete(identity);
      state.dispositionBusy = false;
      if (data.state) applyState(data.state);
      render();
      setStatus("saved per-PR disposition for #" + selectedPr);
      return { ok: true, dispositions: data.dispositions || [], context: currentViewContext() };
    } catch (error) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return false;
      state.dispositionBusy = false;
      renderDispositionPanel(groupById(state.selectedGroupId), prByNumber(state.selectedPr));
      if (error.status === 409) {
        setStatus("per-PR disposition not saved: revision or snapshot changed");
        await loadState();
      } else setStatus("per-PR disposition error: " + error.message);
      return false;
    }
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
          "&repo=" + encodeURIComponent(state.repo) +
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
        changeView(() => { state.selectedFile = row.path; });
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
    if (state.leftTab === "allprs") renderAllPrList();
    else if (state.leftTab === "queue") renderQueue();
    else renderGroupList();
    renderDetail();
    writeUrl(true);
    if (state.selectedUser) renderUserDrawer();
  }

  function actionError(code, message, retryable) {
    return {
      ok: false,
      error: {
        code,
        message,
        retryable: !!retryable,
        context: currentViewContext(),
      },
    };
  }

  function validRepoPath(path) {
    return typeof path === "string" && path.length > 0 && path.length <= 1024 &&
      !path.startsWith("/") && !path.includes("\\") &&
      !path.split("/").some((part) => !part || part === "." || part === "..");
  }

  function actionInput(value, context, key) {
    if (value && typeof value === "object" &&
        Object.prototype.hasOwnProperty.call(value, key)) {
      return { value: value[key], context: value.if_context };
    }
    return {
      value,
      context: context && context.if_context && !context.repo ? context.if_context : context,
    };
  }

  function setFilters(filters, ifContext) {
    if (!stateInitialized) return actionError("not_ready", "the current repository snapshot is still loading.", true);
    const input = actionInput(filters, ifContext, "filters");
    const guard = checkViewContext(input.context);
    // Guard first: a stale request must not touch state, DOM, URL, or storage.
    if (!guard.ok) return guard;
    const value = input.value;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return actionError("invalid_request", "filters must be an object.", false);
    }
    const allowed = new Set(["tab", "q", "label", "pile"]);
    for (const key of Object.keys(value)) {
      if (!allowed.has(key)) return actionError("invalid_request", "unknown filter: " + key, false);
    }
    if (Object.prototype.hasOwnProperty.call(value, "tab") &&
        !["queue", "groups", "allprs"].includes(value.tab)) {
      return actionError("invalid_request", "tab must be queue, groups, or allprs.", false);
    }
    if (Object.prototype.hasOwnProperty.call(value, "q") &&
        (typeof value.q !== "string" || Array.from(value.q).length > 256)) {
      return actionError("invalid_request", "q must be at most 256 characters.", false);
    }
    if (Object.prototype.hasOwnProperty.call(value, "label") &&
        (typeof value.label !== "string" || (value.label && !FILTER_CHIPS.includes(value.label)))) {
      return actionError("invalid_request", "label is not a supported triage filter.", false);
    }
    if (Object.prototype.hasOwnProperty.call(value, "pile") &&
        !QUEUE_PILES.some((pile) => pile.id === value.pile)) {
      return actionError("invalid_request", "pile is not a supported queue pile.", false);
    }
    const changed = changeView(() => {
      if (Object.prototype.hasOwnProperty.call(value, "tab")) state.leftTab = value.tab;
      if (Object.prototype.hasOwnProperty.call(value, "q")) state.filterQuery = value.q;
      if (Object.prototype.hasOwnProperty.call(value, "label")) state.filterLabel = value.label;
      if (Object.prototype.hasOwnProperty.call(value, "pile")) state.queuePile = value.pile;
    });
    if (changed) {
      queueRowsCache = null;
      selectorCache.clear();
    }
    if (Object.prototype.hasOwnProperty.call(value, "tab")) setLeftTab(state.leftTab, true);
    applyListFilter();
    return { ok: true, context: currentViewContext() };
  }

  function openTarget(target, ifContext) {
    if (!stateInitialized) return actionError("not_ready", "the current repository snapshot is still loading.", true);
    const input = actionInput(target, ifContext, "target");
    const guard = checkViewContext(input.context);
    // Validate the complete target before mutating selection or starting any
    // asynchronous file request.
    if (!guard.ok) return guard;
    const value = input.value;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return actionError("invalid_request", "target must be an object.", false);
    }
    for (const key of Object.keys(value)) {
      if (!["group_id", "pr", "path"].includes(key)) {
        return actionError("invalid_request", "unknown target field: " + key, false);
      }
    }
    const hasGroup = Object.prototype.hasOwnProperty.call(value, "group_id");
    const hasPr = Object.prototype.hasOwnProperty.call(value, "pr");
    const hasPath = Object.prototype.hasOwnProperty.call(value, "path");
    if (!hasGroup && !hasPr && !hasPath) {
      return actionError("invalid_request", "target needs group_id, pr, or path.", false);
    }
    if (hasGroup && value.group_id !== null && typeof value.group_id !== "string") {
      return actionError("invalid_request", "group_id must be a string.", false);
    }
    if (hasPr && value.pr !== null && (!Number.isSafeInteger(value.pr) || value.pr <= 0)) {
      return actionError("invalid_request", "pr must be a positive integer.", false);
    }
    if (hasPath && value.path !== null && !validRepoPath(value.path)) {
      return actionError("invalid_request", "path must be repository-relative.", false);
    }
    let groupId = hasGroup ? value.group_id : (hasPr ? null : state.selectedGroupId);
    let prNumber = hasPr ? value.pr : (hasGroup ? null : state.selectedPr);
    let path = hasPath ? value.path : null;
    let group = groupId ? groupById(groupId) : null;
    let pr = prNumber ? prByNumber(prNumber) : null;
    if (groupId && !group) return actionError("not_found", "group is not in the current snapshot.", false);
    if (prNumber && !pr) return actionError("not_found", "PR is not in the current snapshot.", false);
    if (pr && !group) {
      groupId = pr.group_id || null;
      group = groupId ? groupById(groupId) : null;
    }
    if (pr && (!group || !(group.pr_numbers || []).includes(pr.number))) {
      return actionError("invalid_request", "selected PR does not belong to the selected group.", false);
    }
    if (!group) return actionError("invalid_request", "a group is required for this target.", false);
    if (path) {
      const pathBelongs = pr
        ? (pr.paths || []).includes(path)
        : (group.pr_numbers || []).some((number) => {
            const member = prByNumber(number);
            return member && (member.paths || []).includes(path);
          });
      if (!pathBelongs) return actionError("invalid_request", "path does not belong to the selected PR.", false);
    }
    const changed = changeView(() => {
      state.selectedGroupId = groupId;
      state.selectedPr = pr ? pr.number : null;
      state.selectedFile = path;
      state.selectedUser = null;
    });
    if (path) state.fileQueue = { path, prs: [], loading: true, same_patch: [] };
    if (pr) markAgentOpened(pr.number);
    render();
    if (path) openFileQueue(path, { fromUrl: true, fileView: false });
    return { ok: true, changed, context: currentViewContext() };
  }

  async function showProposal(proposalId, ifContext) {
    if (!stateInitialized) return actionError("not_ready", "the current repository snapshot is still loading.", true);
    const input = actionInput(proposalId, ifContext, "proposal_id");
    const guard = checkViewContext(input.context);
    if (!guard.ok) return guard;
    if (typeof input.value !== "string" || !input.value || input.value.length > 256 ||
        input.value.includes("/") || input.value.includes("\\") ||
        Array.from(input.value).some((char) => char.charCodeAt(0) < 32)) {
      return actionError("invalid_request", "proposal_id must be a bounded string.", false);
    }
    const id = input.value;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    const gen = ++proposalGen;
    state.proposalLoading = true;
    state.proposalError = "";
    // This is a display-only side effect after the initial fresh-context
    // guard. The fetched proposal itself is applied only if the view remains
    // the same when the request completes.
    renderProposalPanel();
    try {
      const data = await requestOnce("proposal", state.repo + "|" + id,
        "/api/proposals/" + encodeURIComponent(id) + "?repo=" + encodeURIComponent(state.repo));
      const current = checkViewContext(input.context);
      if (!workspaceCurrent(workspaceRepo, workspaceToken) || !current.ok || gen !== proposalGen) {
        return workspaceCurrent(workspaceRepo, workspaceToken) && !current.ok
          ? current : staleViewError();
      }
      if (!data || !data.proposal || typeof data.proposal !== "object") {
        return actionError("not_found", "proposal response was empty.", false);
      }
      const proposalGroupId = data.proposal.group_id;
      if (typeof proposalGroupId !== "string" || !groupById(proposalGroupId)) {
        return actionError("not_found", "proposal group is not in the current snapshot.", false);
      }
      state.proposal = data.proposal;
      state.proposalKey = id;
      state.proposalLoading = false;
      state.proposalError = "";
      state.proposalEdits = null;
      const rejectReason = $("proposalRejectReason");
      if (rejectReason) rejectReason.value = "";
      changeView(() => {
        state.selectedGroupId = proposalGroupId;
        state.selectedPr = null;
        state.selectedFile = null;
        state.selectedUser = null;
      });
      renderDetail();
      const proposalPicker = $("proposalPicker");
      const proposalIdInput = $("proposalIdInput");
      if (proposalPicker) proposalPicker.value = id;
      if (proposalIdInput) proposalIdInput.value = id;
      const block = $("proposalBlock");
      if (block) block.open = true;
      return { ok: true, proposal: data.proposal, context: currentViewContext() };
    } catch (error) {
      if (gen !== proposalGen || !workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      state.proposalLoading = false;
      state.proposalError = error.message || "proposal could not be loaded";
      renderProposalPanel();
      if (error.status === 404) return actionError("not_found", state.proposalError, false);
      return actionError("proposal_unavailable", state.proposalError, true);
    } finally {
      // A navigation can make the original context stale without starting a
      // replacement request. Clear only this request's loading state; an
      // overlapping proposal request owns a newer generation and is left
      // untouched.
      if (gen === proposalGen && workspaceCurrent(workspaceRepo, workspaceToken) &&
          state.proposalLoading) {
        state.proposalLoading = false;
        renderProposalPanel();
      }
    }
  }

  async function readTool(operation, args) {
    if (!stateInitialized) {
      const error = new Error("the current repository snapshot is still loading");
      error.code = "not_ready";
      throw error;
    }
    const allowed = [
      "get_workspace", "list_groups", "search_prs", "get_group", "get_pr",
      "read_patch", "compare_prs", "find_related", "get_history",
    ];
    if (!allowed.includes(operation)) {
      const error = new Error("unsupported read operation");
      error.code = "invalid_request";
      throw error;
    }
    if (args === undefined) args = {};
    if (!args || typeof args !== "object" || Array.isArray(args)) {
      const error = new Error("args must be an object");
      error.code = "invalid_request";
      throw error;
    }
    return api("/api/tools/read", {
      method: "POST",
      body: JSON.stringify({ operation, args }),
    });
  }

  function exposeTriageApp() {
    if (seamExposed) return;
    seamExposed = true;
    // Stable, intentionally narrow browser seam. Do not add api/session or
    // mutation helpers here: WebMCP may only read through readTool and may
    // change the current display through the guarded view actions.
    window.TriageApp = Object.freeze({
      getViewContext: () => currentViewContext(),
      setFilters,
      openTarget,
      showProposal,
      readTool,
    });
    if (typeof window.dispatchEvent === "function" && typeof window.CustomEvent === "function") {
      window.dispatchEvent(new window.CustomEvent("triage:ready", {
        detail: {
          capabilities: ["read_tool", "get_view_context", "set_filters", "open_target", "show_proposal"],
          read_operations: READ_TOOL_NAMES.slice(),
        },
      }));
    }
  }

  async function loadWorkspaces(options) {
    const token = options && Number.isSafeInteger(options.token) ? options.token : workspaceGen;
    if (token !== workspaceGen) return null;
    try {
      const data = await api("/api/workspaces");
      if (token !== workspaceGen) return null;
      if (!data || typeof data !== "object") throw new Error("workspace discovery returned no data");
      if (data.mode === "multi" || data.mode === "single") state.workspaceMode = data.mode;
      if (typeof data.default_repo === "string" && validWorkspaceRepo(data.default_repo)) {
        state.defaultRepo = data.default_repo;
      }
      state.workspaces = Array.isArray(data.workspaces) ? data.workspaces.slice(0, 200) : [];
      renderWorkspaceControls();
      return data;
    } catch (error) {
      if (token !== workspaceGen) return null;
      // Older fixed-store servers have no discovery route. Keep the ordinary
      // single-workspace UI usable and let /api/state provide its metadata.
      state.workspaceMode = "single";
      state.workspaces = [];
      state.defaultRepo = state.defaultRepo || state.repoDraft || "omacom/omarchy";
      renderWorkspaceControls();
      return null;
    }
  }

  function workspaceCurrent(repo, token) {
    return token === workspaceGen && repo === state.repo;
  }

  async function loadState(repoOverride, options) {
    const target = String(repoOverride || state.repo || state.repoDraft ||
      state.defaultRepo || "omacom/omarchy").trim();
    const token = options && Number.isSafeInteger(options.token) ? options.token : workspaceGen;
    const gen = ++stateLoadGen;
    if (token !== workspaceGen) return false;
    if (!state.repo && !stateInitialized) {
      state.repo = target;
      state.repoDraft = target;
      renderWorkspaceControls();
    }
    setStatus("loading " + target + "…");
    try {
      const statePath = options && options.unscoped ? "/api/state" : scopedGet("/api/state", target);
      const data = await api(statePath);
      if (gen !== stateLoadGen || !workspaceCurrent(target, token)) return false;
      applyState(data);
      exposeTriageApp();
      const c = (state.queue && state.queue.counts) || {};
      setStatus(
        `${state.prs.length} PRs · ${state.groups.length} groups · ${c.needs_you || 0} need you`
      );
      return true;
    } catch (err) {
      if (gen !== stateLoadGen || token !== workspaceGen) return false;
      setStatus("error loading " + target + ": " + err.message);
      return false;
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

  async function pollProgressUntilDone(repo, token) {
    const stalePollError = () => Object.assign(
      new Error("workspace changed while Sync was running"), { code: "stale_workspace" }
    );
    if (!workspaceCurrent(repo, token)) return Promise.reject(stalePollError());
    if (fetchPollOwner) {
      return Promise.reject(new Error("another Sync poll is already active"));
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      const owner = { timer: null };
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        if (fetchPollOwner === owner) {
          if (owner.timer !== null) clearTimeout(owner.timer);
          if (fetchPollTimer === owner.timer) fetchPollTimer = null;
          if (cancelFetchPoll === cancel) cancelFetchPoll = null;
          fetchPollOwner = null;
        }
        callback(value);
      };
      const cancel = () => finish(reject, stalePollError());
      fetchPollOwner = owner;
      cancelFetchPoll = cancel;
      const tick = async () => {
        if (settled) return;
        if (!workspaceCurrent(repo, token)) {
          cancel();
          return;
        }
        try {
          const p = await api(scopedGet("/api/progress", repo));
          if (settled) return;
          if (!workspaceCurrent(repo, token)) {
            cancel();
            return;
          }
          setStatus(formatProgress(p));
          if (p.error && !p.running) {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            finish(reject, new Error(p.error));
            return;
          }
          if (p.ready && !p.running) {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            finish(resolve, p);
            return;
          }
          if (!p.running && !p.ready && p.phase === "error") {
            state.fetching = false;
            $("fetchBtn").disabled = false;
            finish(reject, new Error(p.error || "fetch failed"));
            return;
          }
          if (!settled && fetchPollOwner === owner) {
            owner.timer = setTimeout(tick, 400);
            fetchPollTimer = owner.timer;
          }
        } catch (err) {
          if (settled) return;
          if (!workspaceCurrent(repo, token)) {
            cancel();
            return;
          }
          state.fetching = false;
          $("fetchBtn").disabled = false;
          finish(reject, err);
        }
      };
      tick();
    });
  }

  async function doFetch() {
    const draft = $("repo") && $("repo").value.trim();
    const requestedRaw = draft || state.defaultRepo || "omacom/omarchy";
    const requestedRepo = canonicalWorkspaceRepo(requestedRaw);
    if (!requestedRepo) {
      setStatus("workspace must be owner/repo using safe repository characters");
      return;
    }
    if (state.workspaceSwitching) {
      setStatus("workspace is still opening; Sync will remain explicit");
      return;
    }
    if (requestedRepo !== state.repo || !stateInitialized) {
      const opened = await switchWorkspace(requestedRepo);
      if (!opened || state.repo !== requestedRepo) return;
    }
    const token = workspaceGen;
    stateLoadGen += 1;
    const source = $("source").value;
    const repo = state.repo;
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
      if (!workspaceCurrent(repo, token)) return;
      await pollProgressUntilDone(repo, token);
      const gen = ++stateLoadGen;
      const data = await api(scopedGet("/api/state", repo));
      if (gen !== stateLoadGen || !workspaceCurrent(repo, token)) return;
      applyState(data);
      setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
    } catch (err) {
      if (err.code === "stale_workspace" || !workspaceCurrent(repo, token)) return;
      if (err.status === 409 && err.data && err.data.code === "fetch_busy") {
        if (err.data.repo && err.data.repo !== repo) {
          setStatus("Sync is busy for " + err.data.repo + "; this workspace was not fetched");
          return;
        }
        setStatus(formatProgress(err.data) + " (already running)");
        try {
          await pollProgressUntilDone(repo, token);
          const gen = ++stateLoadGen;
          const data = await api(scopedGet("/api/state", repo));
          if (gen !== stateLoadGen || !workspaceCurrent(repo, token)) return;
          applyState(data);
          setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
        } catch (e2) {
          if (e2.code !== "stale_workspace" && workspaceCurrent(repo, token)) {
            setStatus("fetch error: " + e2.message);
          }
        }
      } else if (err.status === 409) {
        setStatus("sync blocked: " + err.message);
      } else {
        setStatus("fetch error: " + err.message);
        try {
          const cached = await api(scopedGet("/api/state", repo));
          if (!workspaceCurrent(repo, token)) return;
          applyState(cached);
          setStatus("sync failed; showing the last usable cached snapshot · " + err.message);
        } catch (_) {}
      }
    } finally {
      if (workspaceCurrent(repo, token)) {
        state.fetching = false;
        $("fetchBtn").disabled = false;
      }
      if (fetchPollTimer && workspaceCurrent(repo, token)) {
        clearTimeout(fetchPollTimer);
        fetchPollTimer = null;
      }
    }
  }

  const repoInput = $("repo");
  const openWorkspaceButton = $("openWorkspaceBtn");
  const workspaceChoices = $("workspaceChoices");
  async function openDraftWorkspace() {
    const target = repoInput && repoInput.value.trim();
    await switchWorkspace(target);
  }
  if (openWorkspaceButton) openWorkspaceButton.addEventListener("click", openDraftWorkspace);
  if (repoInput) repoInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      openDraftWorkspace();
    }
  });
  if (workspaceChoices) workspaceChoices.addEventListener("change", () => {
    if (repoInput && workspaceChoices.value) repoInput.value = workspaceChoices.value;
  });

  function pendingScopeGroups() {
    if (state.leftTab === "queue" && state.queuePile === "hotspots") return [];
    const source = state.leftTab === "queue"
      ? pileItems(state.queuePile || "needs_you")
      : visibleGroups();
    return source.filter((group) =>
      pileHas("needs_you", group.group_id) || !ruleFor(group.group_id)
    );
  }

  async function doDecide(decision, options) {
    const advance = !!(options && options.advance);
    if (!state.selectedGroupId) return false;
    const group = groupById(state.selectedGroupId);
    const membersComplete = group && (group.pr_numbers || []).every((number) => {
      const pr = prByNumber(number);
      const revisionKnown = state.source === "fixtures" || (pr && pr.head_sha && pr.base_sha);
      return pr && pr.evidence_complete === true && revisionKnown && pr.content_digest;
    });
    if (decision === "approve" &&
        (!group || group.evidence_complete !== true || !membersComplete)) {
      setStatus("approval blocked: complete evidence is required for every revision");
      return false;
    }
    const pendingBefore = advance ? pendingScopeGroups().map((item) => item.group_id) : [];
    const currentIndex = pendingBefore.indexOf(state.selectedGroupId);
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
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
      if (gen !== stateLoadGen || !workspaceCurrent(workspaceRepo, workspaceToken)) return;
      decisionRetries.delete(retryIdentity);
      applyState(data.state);
      let nextGroupId = null;
      if (advance) {
        const ordered = currentIndex >= 0
          ? pendingBefore.slice(currentIndex + 1).concat(pendingBefore.slice(0, currentIndex))
          : pendingBefore;
        nextGroupId = ordered.find((groupId) => {
          const candidate = groupById(groupId);
          return candidate && (pileHas("needs_you", groupId) || !ruleFor(groupId));
        }) || null;
        if (nextGroupId) {
          changeView(() => {
            state.selectedGroupId = nextGroupId;
            state.selectedPr = null;
            state.selectedFile = null;
            state.selectedUser = null;
          });
        }
      }
      render();
      setStatus(`decision saved: ${decision} ${group.group_id}` +
        (advance ? (nextGroupId ? " · next pending group" : " · no more pending groups in this filter") : ""));
      return true;
    } catch (err) {
      if (gen !== stateLoadGen || !workspaceCurrent(workspaceRepo, workspaceToken)) return;
      if (err.status === 409) {
        setStatus("decision not saved: evidence changed; reloading the current revisions");
        await loadState();
      } else {
        setStatus("decide error: " + err.message + " · retry keeps the same request key");
      }
      return false;
    }
  }

  async function doSaveAndNext() {
    const select = $("saveNextDecision");
    const decision = select && select.value;
    if (!["approve", "hardware", "upgrade", "reject"].includes(decision)) {
      setStatus("choose a decision before saving and moving to the next pending group");
      return;
    }
    const saved = await doDecide(decision, { advance: true });
    if (saved) {
      state.nextDecision = "";
      if (select) select.value = "";
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
  const saveNextButton = $("saveNextBtn");
  if (saveNextButton) saveNextButton.addEventListener("click", doSaveAndNext);
  const saveNextDecision = $("saveNextDecision");
  if (saveNextDecision) saveNextDecision.addEventListener("change", (event) => {
    state.nextDecision = event.target.value || "";
  });
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
    const requestedSearch = location.search;
    const requested = new URLSearchParams(requestedSearch);
    const requestedRepo = requested.get("repo") || state.defaultRepo || "omacom/omarchy";
    if (requestedRepo !== state.repo && validWorkspaceRepo(requestedRepo)) {
      const target = requestedRepo;
      const priorRepo = state.repo;
      const repoInput = $("repo");
      if (repoInput) repoInput.value = target;
      state.repoDraft = target;
      switchWorkspace(target, { fromUrl: true }).then((opened) => {
        if (!opened || state.repo !== target) {
          if (state.repo === priorRepo) writeUrl(false);
          return;
        }
        // The switch intentionally clears stale selection first. Restore the
        // exact browser URL after the destination snapshot arrives so URL
        // fields win over that repository's saved resume.
        writingUrl = true;
        history.replaceState(null, "", requestedSearch || "?");
        writingUrl = false;
        readUrl();
        if ($("listFilter")) $("listFilter").value = state.filterQuery;
        selectorCache.clear();
        queueRowsCache = null;
        paintLabelFilters();
        normalizeSelection();
        setLeftTab(state.leftTab, true);
        render();
        if (state.selectedFile) openFileQueue(state.selectedFile, { fromUrl: true });
        renderUserDrawer();
      });
      return;
    }
    const before = viewStateKey();
    readUrl();
    if ($("listFilter")) $("listFilter").value = state.filterQuery;
    selectorCache.clear();
    queueRowsCache = null;
    paintLabelFilters();
    normalizeSelection();
    markViewChanged(before);
    setLeftTab(state.leftTab, true);
    renderDetail();
    writeUrl(false);
    if (state.selectedFile) openFileQueue(state.selectedFile, { fromUrl: true });
    renderUserDrawer();
  });
  window.addEventListener("pagehide", () => {
    cancelPendingDiffScroll();
    if (cancelFetchPoll) cancelFetchPoll();
    else if (fetchPollTimer) clearTimeout(fetchPollTimer);
    fetchPollTimer = null;
    ["group", "all", "queue", "member"].forEach(disposeVirtualizer);
    for (const entry of resourceRequests.values()) entry.controller.abort();
    resourceRequests.clear();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      // A restored page may retain a stale server snapshot and a stale set of
      // WebMCP registrations. Force a fresh context before accepting actions.
      const token = ++workspaceGen;
      stateLoadGen += 1;
      stateInitialized = false;
      persistResume();
      invalidateSnapshotCaches();
      state.repo = "";
      state.viewRevision = Number.isSafeInteger(state.viewRevision)
        ? state.viewRevision + 1 : 1;
      bootstrapSession(true).then(async () => {
        const discovery = await loadWorkspaces({ token });
        if (token !== workspaceGen) return;
        readUrl();
        await loadState(state.repo || state.repoDraft || state.defaultRepo, {
          token, unscoped: !discovery,
        });
      }).catch((error) => setStatus("session error: " + error.message));
    }
  });

  readUrl();
  const filterInp = $("listFilter");
  if (filterInp) {
    filterInp.value = state.filterQuery || "";
    filterInp.addEventListener("input", () => {
      changeView(() => { state.filterQuery = filterInp.value; });
      applyListFilter();
    });
  }
  paintLabelFilters();
  paintPileNav();
  renderWorkspaceControls();
  bootstrapSession(false).then(async () => {
    const token = workspaceGen;
    const discovery = await loadWorkspaces({ token });
    if (token !== workspaceGen) return;
    const target = urlRepoExplicit
      ? state.repoDraft
      : (state.defaultRepo || state.repoDraft || "omacom/omarchy");
    if (discovery && discovery.default_repo && !urlRepoExplicit) state.repoDraft = target;
    if ($("repo")) $("repo").value = target;
    await loadState(target, { token: workspaceGen, unscoped: !discovery });
  }).catch((error) => setStatus("session error: " + error.message));
})();
