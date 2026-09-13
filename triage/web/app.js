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
    sourceConfig: "gh",
    limitConfig: 0,
    repo: "",
    // The repo field is an explicit, uncommitted workspace choice.  It must
    // never retarget the displayed snapshot merely because somebody typed.
    repoDraft: "",
    workspaceMode: "single",
    defaultRepo: "",
    workspaces: [],
    workspaceProfiles: {},
    profileStorageState: "unknown", // unknown | available | invalid | unavailable
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
    // File review is independent of the PR decision in both directions: a
    // file write never touches a disposition, and a disposition never marks a
    // file reviewed.
    fileReview: null,
    fileReviewBusy: false,
    bulkReview: null,
    // Client-only identity for the browser action seam.  It is deliberately
    // separate from store/snapshot versions: a view can become stale even
    // when the repository snapshot has not changed.
    viewRevision: 0,
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
  let stateLoadingOwner = 0;
  let syncActive = false;
  let syncOwner = null;
  let lastBodyPr = null;
  const bodyCache = {};
  const resourceRequests = new Map();
  const patchPanelState = new WeakMap();
  const indexes = { groups: new Map(), prs: new Map(), rules: new Map() };
  const selectorCache = new Map();
  const dispositionRetries = new Map();
  const proposalRetries = new Map();
  const fileReviewRetries = new Map();
  let csrfToken = "";
  let sessionPromise = null;
  let enrichmentRequest = null;
  let stateInitialized = false;
  let seamExposed = false;
  let fileReviewGen = 0;
  let prDecisionReturnFocus = null;
  let findingReturnFocus = null;
  let findingTarget = null;
  let bulkReturnFocus = null;
  let workspaceDialogMode = "new";
  let workspaceDialogReturnFocus = null;
  let workspaceDialogSaving = false;

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
    "read_patch", "compare_prs", "find_related", "get_history", "get_file_review",
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
      source: state.source || state.sourceConfig || "",
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
    // An absent URL repo is a genuine setup state. Do not turn the backend's
    // stable Omarchy fallback into an implicit saved workspace.
    const repoHint = q.get("repo") || "";
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
    if (!value || value.length > 140 || /[\u0000-\u001f\u007f]/.test(value)) return false;
    const parts = value.split("/");
    return parts.length === 2 &&
      /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(parts[0]) &&
      /^[A-Za-z0-9_.-]{1,100}$/.test(parts[1]) &&
      parts[1] !== "." && parts[1] !== "..";
  }

  function canonicalWorkspaceRepo(repo) {
    const value = String(repo || "").trim();
    return validWorkspaceRepo(value) ? value.toLowerCase() : "";
  }

  const PROFILE_STORAGE_KEY = "omarchy.triage.workspace-profiles.v1";
  const DEFAULT_WORKSPACE_SOURCE = "gh";
  const MAX_FILE_CAP = 5000;

  function normalizedSource(source) {
    return ["gh", "github"].includes(String(source || "").toLowerCase())
      ? "gh" : String(source || "").toLowerCase() === "fixtures" ? "fixtures" : "";
  }

  function normalizedFileCap(value) {
    if (typeof value === "number") {
      return Number.isSafeInteger(value) && value >= 0 && value <= MAX_FILE_CAP ? value : null;
    }
    if (typeof value !== "string" || !/^\d+$/.test(value.trim())) return null;
    const parsed = Number(value.trim());
    return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= MAX_FILE_CAP ? parsed : null;
  }

  function profileRecord(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const source = normalizedSource(value.source);
    const limit = normalizedFileCap(value.limit);
    if (!source || limit === null) return null;
    return { source, limit };
  }

  function loadWorkspaceProfiles() {
    state.workspaceProfiles = {};
    state.profileStorageState = "available";
    let raw = "";
    try {
      if (!window.localStorage) {
        state.profileStorageState = "unavailable";
        return;
      }
      raw = window.localStorage.getItem(PROFILE_STORAGE_KEY) || "";
    } catch (_) {
      state.profileStorageState = "unavailable";
      return;
    }
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw);
      const rows = parsed && typeof parsed === "object" && parsed.profiles &&
        typeof parsed.profiles === "object" && !Array.isArray(parsed.profiles)
        ? parsed.profiles : parsed;
      if (!rows || typeof rows !== "object" || Array.isArray(rows)) throw new Error("invalid profiles");
      Object.keys(rows).slice(0, 200).forEach((repo) => {
        const canonical = canonicalWorkspaceRepo(repo);
        const profile = profileRecord(rows[repo]);
        if (canonical && profile) state.workspaceProfiles[canonical] = profile;
        else if (repo) state.profileStorageState = "invalid";
      });
    } catch (_) {
      state.workspaceProfiles = {};
      state.profileStorageState = "invalid";
    }
  }

  function persistWorkspaceProfiles() {
    try {
      if (!window.localStorage) throw new Error("storage unavailable");
      const profiles = {};
      Object.keys(state.workspaceProfiles).slice(0, 200).forEach((repo) => {
        const canonical = canonicalWorkspaceRepo(repo);
        const profile = profileRecord(state.workspaceProfiles[repo]);
        if (canonical && profile) profiles[canonical] = profile;
      });
      window.localStorage.setItem(PROFILE_STORAGE_KEY, JSON.stringify({ version: 1, profiles }));
      state.profileStorageState = "available";
      return true;
    } catch (_) {
      state.profileStorageState = "unavailable";
      return false;
    }
  }

  function workspaceProfile(repo) {
    const canonical = canonicalWorkspaceRepo(repo);
    return canonical ? profileRecord(state.workspaceProfiles[canonical]) : null;
  }

  function serverWorkspaceRepos() {
    return state.workspaces.map((item) => {
      // Legacy discovery may still return bare repository strings. Structured
      // rows are saved only when the server confirms initialization.
      if (typeof item === "string") return item;
      return item && item.initialized !== false ? item.repo : "";
    }).map(canonicalWorkspaceRepo).filter(Boolean);
  }

  function savedWorkspaceRepos() {
    const all = new Set(serverWorkspaceRepos());
    Object.keys(state.workspaceProfiles).map(canonicalWorkspaceRepo).filter(Boolean)
      .forEach((repo) => all.add(repo));
    // A fixed --store server can expose its active repository in state even
    // when discovery predates /api/workspaces. An empty draft is never added.
    if (state.workspaceMode === "single" && state.workspaceInitialized && state.repo) {
      const active = canonicalWorkspaceRepo(state.repo);
      if (active) all.add(active);
    }
    return Array.from(all).sort();
  }

  function hasSavedWorkspace() {
    return savedWorkspaceRepos().length > 0;
  }

  function profileStatusText(repo) {
    const profile = workspaceProfile(repo);
    if (state.profileStorageState === "unavailable") {
      return "Browser storage is unavailable; changes apply for this tab only.";
    }
    if (state.profileStorageState === "invalid") {
      return "Saved browser preferences could not be read; saving will replace them.";
    }
    if (profile) return "Saved in this browser for " + canonicalWorkspaceRepo(repo) + ".";
    return workspaceDialogMode === "new"
      ? "No browser profile yet. Create & sync will make the first server snapshot."
      : "No browser profile yet. Save this preference, then Sync when ready.";
  }

  function workspaceDialogOwnsDraft() {
    const dialog = $("workspaceDialog");
    return !!(dialog && dialog.open && !workspaceDialogSaving);
  }

  function applyWorkspaceProfilePreferences() {
    const source = $("source");
    const limit = $("limit");
    const ownsDraft = workspaceDialogOwnsDraft();
    const profile = workspaceProfile(state.repo);
    if (profile) {
      state.sourceConfig = profile.source;
      state.limitConfig = profile.limit;
      if (!ownsDraft) {
        if (source) source.value = profile.source;
        if (limit) limit.value = String(profile.limit);
      }
      return profile;
    }
    // applyState has already put the server's source into state.source. Keep
    // that value when present (notably existing fixture stores); only new or
    // empty workspaces receive the gh/0 defaults.
    state.sourceConfig = normalizedSource(state.source) || DEFAULT_WORKSPACE_SOURCE;
    state.limitConfig = 0;
    if (!ownsDraft) {
      if (source) source.value = state.sourceConfig;
      if (limit) limit.value = String(state.limitConfig);
    }
    return null;
  }

  function renderWorkspaceControls() {
    const active = $("activeWorkspace");
    const saved = hasSavedWorkspace();
    const mobileWorkspaceToggle = $("mobileWorkspaceToggle");
    if (mobileWorkspaceToggle) mobileWorkspaceToggle.textContent = saved ? "Workspaces" : "New workspace";
    if (active) {
      active.textContent = state.workspaceSwitching
        ? "opening workspace: " + (state.repo || state.repoDraft || "?")
        : state.repo && (saved || state.workspaceMode === "single")
          ? "active workspace: " + state.repo +
            (state.workspaceInitialized === false ? " · empty" : "")
          : "No workspace selected";
    }
    const savedControls = $("savedWorkspaceControls");
    if (savedControls) savedControls.hidden = !saved;
    const setupHint = $("workspaceSetupHint");
    if (setupHint) setupHint.classList.toggle("is-visible", !saved);
    const picker = $("workspaceChoices");
    if (!picker) return;
    const prior = picker.value;
    picker.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "choose saved workspace…";
    picker.appendChild(blank);
    const all = savedWorkspaceRepos();
    all.slice(0, 200).forEach((repo) => {
      const option = document.createElement("option");
      option.value = repo;
      option.textContent = repo;
      picker.appendChild(option);
    });
    picker.value = all.includes(state.repo) ? state.repo : all.includes(prior) ? prior : "";
    const sync = $("fetchBtn");
    if (sync) sync.disabled = !saved || !state.repo || state.fetching || state.workspaceSwitching;
    const settings = $("settingsWorkspaceBtn");
    if (settings) settings.disabled = !state.repo || state.workspaceSwitching;
  }

  function setWorkspaceDialogError(message, focusId) {
    const error = $("workspaceDialogError");
    if (error) error.textContent = message || "";
    if (focusId) {
      const field = $(focusId);
      if (field) field.focus();
    }
  }

  function renderWorkspaceDialogHint(repo) {
    const hint = $("workspaceProfileHint");
    if (hint) hint.textContent = profileStatusText(repo);
  }

  function restoreWorkspaceDialogFocus() {
    const target = workspaceDialogReturnFocus;
    workspaceDialogReturnFocus = null;
    if (target && typeof target.focus === "function" && document.contains(target)) {
      target.focus();
    }
  }

  function closeWorkspaceDialog() {
    const dialog = $("workspaceDialog");
    if (dialog && dialog.open) dialog.close("cancel");
    restoreWorkspaceDialogFocus();
  }

  function openWorkspaceDialog(mode) {
    const dialog = $("workspaceDialog");
    const repoInput = $("repo");
    const source = $("source");
    const limit = $("limit");
    if (!dialog || !repoInput || !source || !limit) return;
    workspaceDialogMode = mode === "settings" ? "settings" : "new";
    workspaceDialogReturnFocus = document.activeElement;
    const editing = workspaceDialogMode === "settings";
    const explicitRepo = urlRepoExplicit && validWorkspaceRepo(state.repoDraft)
      ? canonicalWorkspaceRepo(state.repoDraft) : "";
    const explicitlyUnsaved = explicitRepo && state.repo === explicitRepo &&
      state.workspaceInitialized === false && !serverWorkspaceRepos().includes(explicitRepo) &&
      !workspaceProfile(explicitRepo);
    const repo = editing ? canonicalWorkspaceRepo(state.repo) : explicitlyUnsaved ? explicitRepo : "";
    const profile = workspaceProfile(repo);
    repoInput.value = repo;
    repoInput.readOnly = editing;
    source.value = profile ? profile.source :
      (editing && normalizedSource(state.source)) || DEFAULT_WORKSPACE_SOURCE;
    limit.value = profile ? String(profile.limit) : "0";
    const title = $("workspaceDialogTitle");
    if (title) title.textContent = editing ? "Workspace settings" : "New workspace";
    const intro = $("workspaceDialogIntro");
    if (intro) intro.textContent = editing
      ? "Edit this workspace's source and file cap. Save updates this browser profile only; Sync remains explicit."
      : "Choose where this workspace reads from. Create & sync saves the profile in this browser, then starts the first explicit Sync.";
    setWorkspaceDialogError("");
    renderWorkspaceDialogHint(repo);
    const save = $("workspaceSaveBtn");
    const cancel = $("workspaceCancelBtn");
    if (save) { save.disabled = false; save.textContent = editing ? "Save" : "Create & sync"; }
    if (cancel) cancel.disabled = false;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => repoInput.focus());
    else repoInput.focus();
  }

  async function saveWorkspaceDialog(event) {
    if (event) event.preventDefault();
    if (workspaceDialogSaving) return false;
    const repoInput = $("repo");
    const sourceInput = $("source");
    const limitInput = $("limit");
    if (!repoInput || !sourceInput || !limitInput) return false;
    const repo = canonicalWorkspaceRepo(repoInput.value);
    const source = normalizedSource(sourceInput.value);
    const limit = normalizedFileCap(limitInput.value);
    if (!repo) {
      setWorkspaceDialogError("Enter a valid owner/repository name.", "repo");
      return false;
    }
    if (!source) {
      setWorkspaceDialogError("Choose a supported source.", "source");
      return false;
    }
    if (limit === null) {
      setWorkspaceDialogError("File cap must be a whole number from 0 to 5000.", "limit");
      return false;
    }
    if (workspaceDialogMode === "new" && savedWorkspaceRepos().includes(repo)) {
      setWorkspaceDialogError("That workspace already exists. Choose it above or use Settings.", "repo");
      return false;
    }
    if (repo !== state.repo && state.workspaceMode === "single" && state.repo && stateInitialized) {
      setWorkspaceDialogError("Fixed-store mode is bound to " + state.repo + ".");
      return false;
    }
    // Keep the established switch guard, but run it before committing a new
    // profile so Cancel leaves the active workspace and browser preferences
    // untouched. The actual switch then skips the duplicate prompt.
    if (repo !== state.repo && !confirmWorkspaceSwitch()) {
      setWorkspaceDialogError("Workspace switch canceled; active workspace unchanged.");
      return false;
    }
    const save = $("workspaceSaveBtn");
    const cancel = $("workspaceCancelBtn");
    const priorRepo = state.repo;
    state.workspaceProfiles[repo] = { source, limit };
    const persisted = persistWorkspaceProfiles();
    workspaceDialogSaving = true;
    if (save) { save.disabled = true; save.textContent = workspaceDialogMode === "new" ? "Creating…" : "Opening…"; }
    if (cancel) cancel.disabled = true;
    renderWorkspaceDialogHint(repo);
    renderWorkspaceControls();
    const opened = await switchWorkspace(repo, { skipConfirm: true });
    if (!opened || state.repo !== repo || state.workspaceSwitching) {
      workspaceDialogSaving = false;
      if (save) { save.disabled = false; save.textContent = workspaceDialogMode === "new" ? "Create & sync" : "Save"; }
      if (cancel) cancel.disabled = false;
      if (state.repo === priorRepo) {
        setWorkspaceDialogError("Workspace switch canceled; active workspace unchanged.");
      } else {
        setWorkspaceDialogError("Workspace " + repo + " is selected, but its saved state could not be loaded.");
      }
      return false;
    }
    // switchWorkspace may have returned from an empty or already-open target;
    // apply the just-saved profile only after that guard has completed.
    applyWorkspaceProfilePreferences();
    renderSyncMeta();
    renderWorkspaceControls();
    const createAndSync = workspaceDialogMode === "new";
    workspaceDialogSaving = false;
    setStatus(createAndSync
      ? persisted ? "workspace " + repo + " saved locally; starting Sync…"
        : "workspace " + repo + " applies for this tab; starting Sync…"
      : persisted ? "workspace " + repo + " saved locally; Sync is still explicit"
        : "workspace " + repo + " applies for this tab; browser storage is unavailable");
    closeWorkspaceDialog();
    if (createAndSync) await doFetch({ forceRefresh: true });
    return true;
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
    // A canceled Sync belongs to the old repository. Release its loading
    // ownership before the destination load starts; the old promise's
    // finally block is generation-guarded and cannot touch the new workspace.
    syncActive = false;
    syncOwner = null;
    stateLoadingOwner = 0;
    setLoading(false);
    invalidateSnapshotCaches();
    state.repo = target;
    state.repoDraft = target;
    state.source = "";
    state.sourceConfig = DEFAULT_WORKSPACE_SOURCE;
    state.limitConfig = 0;
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
    rebuildIndexes();
    state.selectedGroupId = null;
    state.selectedPr = null;
    state.selectedFile = null;
    state.selectedUser = null;
    state.workspaceInitialized = false;
    state.workspaceLegacy = false;
    state.workspaceSwitching = true;
    stateInitialized = false;
    setLoading(true, "loading");
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
      if (!stateLoadingOwner && !syncActive) setLoading(false);
    }
    return !!loaded;
  }

  function setStatus(msg) {
    const root = $("status");
    if (root) root.textContent = msg;
  }

  function finishStateLoading(owner, token) {
    if (stateLoadingOwner !== owner || token !== workspaceGen || syncActive) return;
    stateLoadingOwner = 0;
    if (!state.workspaceSwitching) setLoading(false);
  }

  function setLoading(active, phase) {
    const loading = !!active;
    state.fetching = loading;
    const button = $("fetchBtn");
    const syncEligible = validWorkspaceRepo(state.repo) && hasSavedWorkspace();
    if (button) {
      button.disabled = loading || state.workspaceSwitching || !syncEligible;
      button.setAttribute("aria-busy", loading ? "true" : "false");
      button.textContent = loading ? "Syncing…" : "Sync";
      button.classList.toggle("is-loading", loading);
    }
    const status = $("status");
    if (status) {
      status.classList.toggle("is-loading", loading);
      status.setAttribute("aria-busy", loading ? "true" : "false");
    }
    if (document.body) {
      document.body.classList.toggle("is-loading", loading);
      if (phase) document.body.dataset.syncPhase = phase;
      else delete document.body.dataset.syncPhase;
    }
  }

  function waitForPaint() {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }
      else setTimeout(resolve, 0);
    });
  }

  function hasUsableSnapshot() {
    return stateInitialized && state.workspaceInitialized === true &&
      (!!state.source || !!state.sync || state.groups.length > 0 || state.prs.length > 0);
  }

  function safeSyncError(error, snapshotAvailable) {
    const data = error && error.data && typeof error.data === "object" ? error.data : {};
    const code = data.code || error && error.code || "";
    if (code === "source_conflict") return "sync blocked: source belongs to another workspace";
    if (code === "repository_conflict" || code === "workspace_conflict") {
      return "sync blocked: repository belongs to another workspace";
    }
    if (code === "fetch_busy") return "Sync is already running";
    if (snapshotAvailable) return "sync failed; previous snapshot preserved. Retry Sync.";
    return "sync failed; no snapshot available. Check connectivity and retry Sync.";
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
    fileManifests.clear();
    if ($("fileNavigatorDialog") && $("fileNavigatorDialog").open) $("fileNavigatorDialog").close();
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
    fileReviewGen += 1;
    state.fileReview = null;
    state.fileReviewBusy = false;
    state.bulkReview = null;
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
    if (data.source && !workspaceDialogOwnsDraft()) {
      $("source").value = ["gh", "github"].includes(data.source) ? "gh" : data.source;
    }
    if (state.repo) {
      if (!workspaceDialogOwnsDraft()) state.repoDraft = state.repo;
      if ($("repo") && !workspaceDialogOwnsDraft()) $("repo").value = state.repo;
    }
    // Apply the backend snapshot first, then overlay only this repository's
    // browser-local source/cap preference. This keeps fixture stores truthful
    // when no profile exists and prevents a prior repo's setting leaking here.
    applyWorkspaceProfilePreferences();
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
    // First render establishes selection while read tools are still gated.
    // Start metadata once the fully applied snapshot is ready, without timers
    // or changing the decision/navigation readiness ordering above.
    if (state.selectedGroupId && state.selectedPr) {
      const entry = currentManifest();
      if (entry && !entry.rows.length && !entry.error && entry.next !== null) loadManifestPage(entry);
      if (state.selectedFile && !currentFileReview()) {
        loadFileReview({
          findings: !!($("fileFindingsBlock") && $("fileFindingsBlock").open),
          drafts: !!($("fileDraftsBlock") && $("fileDraftsBlock").open),
        });
      }
      ensureReviewIndex(state.selectedPr);
    }
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
      parts.push(pillHtml("Legacy rule: " + shown, key));
    }
    const members = g.pr_numbers || [];
    const decided = members.filter((number) => {
      const pr = prByNumber(number);
      return pr && !pr.disposition_stale && pr.disposition && pr.disposition !== "pending";
    }).length;
    parts.push('<span class="muted">' + decided + '/' + members.length + ' PRs decided</span>');
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
      if (hasUnsavedDecisionEdit() && !window.confirm("Discard unsaved PR decision edits?")) return;
      setMobilePane("review");
      changeView(() => {
        state.selectedGroupId = g.group_id;
        state.selectedPr = null;
        state.selectedFile = null;
        state.selectedUser = null;
      });
      if (!window.matchMedia("(max-width: 700px)").matches) setFilesPane(true);
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

  // Saved status is a persisted fact shown wherever a PR is listed. It comes
  // only from the store projection, never from an open editor.
  function savedDecisionLabel(pr) {
    if (pr && pr.disposition && pr.disposition !== "pending") {
      return {
        text: DECISION_NAMES[pr.disposition] || pr.disposition,
        state: pr.disposition === "reject" ? "rejected" : "saved",
      };
    }
    if (pr && pr.disposition_stale === true) {
      return { text: "Stale", state: "stale" };
    }
    return { text: "Not reviewed", state: "pending" };
  }

  function appendSavedDecision(root, pr) {
    const saved = savedDecisionLabel(pr);
    const node = document.createElement("span");
    node.className = "row-decision";
    node.dataset.state = saved.state;
    node.textContent = saved.text;
    node.title = "Saved PR decision";
    root.appendChild(node);
    return node;
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
    appendSavedDecision(row, pr);
    row.addEventListener("click", () => {
      if (state.selectedPr !== pr.number && hasUnsavedDecisionEdit() && !window.confirm("Discard unsaved PR decision edits?")) return;
      setMobilePane("review");
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
        ? prMayContainPath(pr, state.selectedFile)
        : (group.pr_numbers || []).some((number) => {
            const member = prByNumber(number);
            return member && prMayContainPath(member, state.selectedFile);
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
      setMobilePane("review");
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
    if ($("reviewFiles")) $("reviewFiles").replaceChildren();
    if ($("fileReviewBar")) {
      $("fileReviewBar").replaceChildren();
      $("fileReviewBar").hidden = true;
    }
    if ($("fileFindingsBlock")) $("fileFindingsBlock").hidden = true;
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
    if ($("navigatorFilesBtn")) $("navigatorFilesBtn").disabled = !g;
    if (!g) {
      if ($("fileNavigatorDialog") && $("fileNavigatorDialog").open) $("fileNavigatorDialog").close();
      setFilesPane(false);
      if ($("fileNavigatorList")) $("fileNavigatorList").replaceChildren();
    }
    if (g) {
      changeView(() => {
        if (!state.selectedPr) state.selectedPr = (g.pr_numbers || [])[0] || null;
        const selected = prByNumber(state.selectedPr);
        if (!state.selectedFile && selected) state.selectedFile = (selected.paths || [])[0] || null;
      });
      renderReviewFiles(g);
    }
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
        : state.repo && state.workspaceInitialized === false && hasSavedWorkspace()
          ? "Workspace " + state.repo + " is empty. Use Sync to fetch it explicitly."
          : state.repo && state.workspaceInitialized === false
            ? "Save this workspace profile to begin."
          : state.repo
            ? "No group selected in " + state.repo
            : "Open a workspace to begin";
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
      $("detailKicker").textContent = g.group_id + " · " + n + " PRs with shared files";
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
        document.createTextNode(" · " + (pr.path_count ?? (pr.paths || []).length) + " files · " +
          (pr.disposition || "pending").replace(/_/g, " "))
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
    if (mh) mh.textContent = "Group · " + n + " PRs";

    renderMembers(g);
    renderPrBodies(g);
    if ($("overlapBlock").open) renderOverlap(g);
    renderDiffs(g);
    renderFileQueue();
    // Switching file or PR always rebuilds this panel from the selected
    // target, so no file ever shows another file review state.
    renderFileReviewPanels();
    if (state.selectedPr && state.selectedFile && !currentFileReview()) {
      loadFileReview({
        reset: true,
        findings: !!($("fileFindingsBlock") && $("fileFindingsBlock").open),
        drafts: !!($("fileDraftsBlock") && $("fileDraftsBlock").open),
      });
    }
    if (state.selectedPr) ensureReviewIndex(state.selectedPr);
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
    if (block && lastBodyPr !== selected) block.open = false;
    lastBodyPr = selected;
    if (block && !block.open) {
      root.textContent = "Open to read this PR’s description.";
      return;
    }
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
    appendSavedDecision(li, pr);
    bindUserLink(li.querySelector(".user"), pr.user);
      li.addEventListener("click", (ev) => {
        if (ev.target.tagName === "A") return;
        if (state.selectedPr !== num && hasUnsavedDecisionEdit() && !window.confirm("Discard unsaved PR decision edits?")) return;
        setMobilePane("review");
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
    const legacy = $("legacyGroupRule");
    const rule = ruleFor(group.group_id);
    if (legacy) {
      // Historic group-only rules are shown read-only and clearly labelled.
      // They are never converted into individual PR decisions behind the
      // maintainer back.
      legacy.hidden = !rule;
      legacy.textContent = rule
        ? "Legacy group-only history: this group was marked " + rule.decision +
          (rule.actor ? " by " + rule.actor : "") +
          (rule.decided_at ? " on " + rule.decided_at : "") +
          ". It wrote no individual PR decisions and is displayed read-only."
        : "";
    }
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
    const selectedIncomplete = incomplete.includes(state.selectedPr);
    root.textContent = selectedIncomplete
      ? "Incomplete evidence for #" + state.selectedPr + ". Review the missing content before deciding."
      : "Complete evidence for #" + state.selectedPr +
        (incomplete.length ? " · " + incomplete.length + " group members have incomplete evidence." : " · Decisions are local; nothing is sent to GitHub.");
    const identity = $("revisionDetails");
    if (identity) identity.textContent = "Snapshot " + (group.snapshot_digest || "missing") +
      " · " + unexamined.length + " members not individually opened · " + agentOpened.length +
      " opened by agent (not human-attested). " + numbers.map((number) => {
        const member = prByNumber(number) || {};
        return "#" + number + " head " + (member.head_sha || "missing") +
          " base " + (member.base_sha || "missing") + " content " + (member.content_digest || "missing");
      }).join("; ");
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
    const count = $("proposalCount");
    if (count) count.textContent = rows.length ? " · " + rows.length : "";
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
  const FILE_REVIEW_FINDING_PAGE = 20;
  const FILE_REVIEW_DRAFT_PAGE = 5;
  const FILE_REVIEW_DRAFT_FINDING_PAGE = 20;

  function fileReviewKey(pr, path) {
    return [snapshotKey(), pr || 0, path || ""].join("|");
  }

  function emptyFileReview(pr, path) {
    return {
      key: fileReviewKey(pr, path), pr, path,
      revision: null, coverage: null, file: null, manifestComplete: true,
      findings: [], nextFindingPage: 1, findingCount: 0, findingsOpened: false,
      drafts: [], nextDraftPage: 1, draftCount: 0, draftsOpened: false,
      draftId: "", draftFindings: [], nextDraftFindingPage: 1, draftFindingCount: 0,
      loading: false, error: "",
    };
  }

  function currentFileReview() {
    const pr = state.selectedPr;
    const path = state.selectedFile;
    if (!pr || !path) return null;
    const key = fileReviewKey(pr, path);
    if (!state.fileReview || state.fileReview.key !== key) return null;
    return state.fileReview;
  }

  async function fetchFileReview(args) {
    const response = await readTool("get_file_review", {
      repo: state.repo,
      expected_store_version: state.storeVersion,
      expected_snapshot_version: state.snapshotVersion,
      ...args,
    });
    if (!response || response.ok !== true) {
      const message = response && response.error && response.error.message;
      throw new Error(message || "Could not load file review");
    }
    const context = response.context || {};
    if (context.repo !== state.repo || context.store_version !== state.storeVersion ||
        context.snapshot_version !== state.snapshotVersion) {
      throw new Error("Snapshot changed. Reload before reviewing this file.");
    }
    return response.data;
  }

  function applyFileSummary(entry, data) {
    entry.revision = data.revision || null;
    entry.coverage = data.coverage || null;
    entry.manifestComplete = data.manifest_complete !== false;
    const match = (data.files || []).find((row) => row.path === entry.path);
    if (match) entry.file = match;
    // Any manifest page we already paid for also feeds the navigator index,
    // so selecting a file beyond the loaded pages still shows its status.
    mergeReviewIndex(entry.pr, data);
    return data;
  }

  // The manifest, finding bodies, and drafts are three independently paged
  // lists.  Nothing here assumes a single response contains everything.
  async function loadFileReview(options) {
    const pr = state.selectedPr;
    const path = state.selectedFile;
    if (!pr || !path || !state.repo || !stateInitialized) {
      state.fileReview = null;
      return null;
    }
    const opts = options || {};
    const key = fileReviewKey(pr, path);
    let entry = state.fileReview && state.fileReview.key === key
      ? state.fileReview : null;
    if (!entry || opts.reset) {
      entry = emptyFileReview(pr, path);
      state.fileReview = entry;
    }
    if (entry.loading) return entry;
    const gen = ++fileReviewGen;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    entry.loading = true;
    entry.error = "";
    renderFileReviewPanels();
    const alive = () => gen === fileReviewGen &&
      workspaceCurrent(workspaceRepo, workspaceToken) &&
      state.fileReview === entry && fileReviewKey(state.selectedPr, state.selectedFile) === key;
    try {
      const args = { pr, path, page: opts.page || 1, page_size: 50 };
      // Bodies are opt-in: a manifest-only refresh must not drag finding or
      // draft text into a response that nothing is going to show.
      if (!opts.findings) args.finding_page_size = 1;
      if (!opts.drafts) args.draft_page_size = 1;
      if (opts.findings) {
        args.finding_page = entry.nextFindingPage || 1;
        args.finding_page_size = FILE_REVIEW_FINDING_PAGE;
      }
      if (opts.drafts) {
        args.draft_page = entry.nextDraftPage || 1;
        args.draft_page_size = FILE_REVIEW_DRAFT_PAGE;
      }
      if (opts.draftId) {
        args.draft_id = opts.draftId;
        args.draft_finding_page = opts.draftFindingPage || 1;
        args.draft_finding_page_size = FILE_REVIEW_DRAFT_FINDING_PAGE;
      }
      const data = await fetchFileReview(args);
      if (!alive()) return null;
      applyFileSummary(entry, data);
      if (!entry.file && data.focus_page && data.focus_page !== data.page) {
        // Large manifests page the focused file off the first page.  The
        // follow-up must keep the same page_size, because focus_page was
        // computed for that size; a different size points at another file.
        const focused = await fetchFileReview({
          pr, path, page: data.focus_page, page_size: 50,
          finding_page_size: 1, draft_page_size: 1,
        });
        if (!alive()) return null;
        applyFileSummary(entry, focused);
      }
      if (opts.findings) {
        entry.findings = (opts.appendFindings ? entry.findings : []).concat(
          data.findings || []
        );
        entry.findingCount = Number(data.finding_count || 0);
        entry.nextFindingPage = data.next_finding_page || null;
        entry.findingsOpened = true;
      }
      if (opts.drafts) {
        entry.drafts = (opts.appendDrafts ? entry.drafts : []).concat(data.drafts || []);
        entry.draftCount = Number(data.draft_count || 0);
        entry.nextDraftPage = data.next_draft_page || null;
        entry.draftsOpened = true;
      }
      if (opts.draftId) {
        entry.draftId = opts.draftId;
        entry.draftFindings = (opts.appendDraftFindings ? entry.draftFindings : [])
          .concat(data.draft_findings || []);
        entry.draftFindingCount = Number(data.draft_finding_count || 0);
        entry.nextDraftFindingPage = data.next_draft_finding_page || null;
      }
      return entry;
    } catch (error) {
      if (!alive()) return null;
      entry.error = error.message || "Could not load file review";
      return null;
    } finally {
      if (gen === fileReviewGen && state.fileReview === entry) {
        entry.loading = false;
        renderFileReviewPanels();
      }
    }
  }

  function exactPrEvidence(number) {
    const cached = bodyCache[snapshotKey() + "|" + number];
    return cached && typeof cached.then !== "function" ? cached : null;
  }

  function addText(parent, className, text) {
    const node = document.createElement("span");
    node.className = className;
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function coverageSentence(coverage) {
    if (!coverage) return "";
    const total = Number(coverage.files_total || 0);
    const agent = Number(coverage.agent_inspected || 0);
    return "You reviewed " + Number(coverage.human_reviewed || 0) + "/" + total +
      " files · Agent inspected " + agent + "/" + total + " files" +
      (coverage.missing_patch ? " · " + coverage.missing_patch + " without complete patch" : "");
  }

  function renderFileReviewBar() {
    const root = $("fileReviewBar");
    if (!root) return;
    root.replaceChildren();
    const group = groupById(state.selectedGroupId);
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const path = state.selectedFile;
    if (!group || !pr || !path) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const entry = currentFileReview();
    const file = entry && entry.file;
    addText(root, "file-review-title", "File review");
    addText(root, "file-review-state", path.split("/").pop());
    const mark = document.createElement("button");
    mark.type = "button";
    mark.className = "btn";
    const reviewed = !!(file && file.human && file.human.reviewed);
    const canMark = !!file && file.can_mark_reviewed === true;
    mark.textContent = reviewed ? "Reviewed — undo" : "Mark file reviewed";
    mark.setAttribute("aria-pressed", String(reviewed));
    mark.disabled = !!state.fileReviewBusy || !entry || entry.loading ||
      (!reviewed && !canMark);
    mark.title = reviewed
      ? "Clear your file review for this exact revision"
      : canMark
        ? "Records that you looked at this exact file revision. Not approval."
        : "This file has no complete patch evidence; add a finding instead";
    mark.onclick = () => toggleFileReviewed(!reviewed);
    root.appendChild(mark);
    const add = document.createElement("button");
    add.type = "button";
    add.className = "btn";
    add.textContent = "Add finding";
    add.disabled = !!state.fileReviewBusy;
    add.onclick = () => openFindingDialog();
    root.appendChild(add);
    const counts = (file && file.findings) || {};
    const findings = addText(root, "file-review-state",
      Number(counts.open || 0) + " open · " + Number(counts.total || 0) + " findings" +
      (counts.stale_open ? " · " + counts.stale_open + " open at an older revision" : ""));
    findings.classList.toggle("blocked", Number(counts.open || 0) > 0);
    const human = addText(root, "file-review-state",
      reviewed
        ? "You reviewed this revision" + (file.human.actor ? " · " + file.human.actor : "")
        : file && file.human && file.human.stale_history
          ? "Reviewed at an older revision — needs re-review"
          : "Not reviewed by you");
    human.classList.toggle("reviewed", reviewed);
    if (file && file.human && file.human.stale_history && !reviewed) {
      human.classList.add("blocked");
    }
    const agentStatus = (file && file.agent && file.agent.status) || "";
    addText(root, "file-review-state",
      agentStatus
        ? "Agent " + agentStatus + (file.agent.actor ? " · " + file.agent.actor : "")
        : "No agent coverage recorded");
    if (file && file.can_mark_reviewed === false) {
      addText(root, "file-review-state blocked",
        "No complete patch — cannot be marked reviewed");
    }
    const spacer = document.createElement("span");
    spacer.className = "spacer";
    root.appendChild(spacer);
    addText(root, "file-review-state", entry && entry.error
      ? entry.error
      : entry && entry.loading ? "Loading file review…"
        : coverageSentence(entry && entry.coverage));
  }

  function findingRow(finding, options) {
    const opts = options || {};
    const row = document.createElement("div");
    row.className = "finding-row";
    row.dataset.status = finding.status || (opts.draft ? "draft" : "open");
    row.dataset.stale = String(finding.stale === true);
    const head = document.createElement("div");
    head.className = "finding-head";
    const severity = addText(head, "severity", finding.severity || "");
    severity.dataset.severity = finding.severity || "";
    addText(head, "finding-title", finding.title || "");
    row.appendChild(head);
    const origin = opts.draft
      ? "agent draft"
      : finding.origin === "agent_draft" ? "adopted agent draft" : "you";
    const provenance = [
      finding.path + (finding.line ? ":" + finding.line : ""),
      "by " + (finding.author || opts.actor || "agent") + " (" + origin + ")",
      finding.adopted_by ? "accepted by " + finding.adopted_by : "",
      opts.draft ? "status " + (finding.status || "pending") : "status " + (finding.status || "open"),
      finding.stale ? "evidence is from an older revision" : "",
      finding.about_missing_patch ? "about missing patch evidence" : "",
    ].filter(Boolean).join(" · ");
    addText(row, "finding-meta", provenance);
    const detail = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Details";
    detail.appendChild(summary);
    const body = document.createElement("div");
    body.className = "finding-body";
    for (const [label, value] of [
      ["Explanation", finding.explanation], ["Evidence", finding.evidence],
      ["Suggested fix", finding.suggested_fix], ["Hunk", finding.hunk],
    ]) {
      if (!value) continue;
      const block = document.createElement("p");
      block.textContent = label + ": " + value;
      body.appendChild(block);
    }
    if (!body.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No further detail was recorded.";
      body.appendChild(empty);
    }
    detail.appendChild(body);
    row.appendChild(detail);
    const actions = document.createElement("div");
    actions.className = "finding-actions";
    (opts.actions || []).forEach(([label, handler, disabled, title]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn";
      button.textContent = label;
      button.disabled = !!disabled;
      if (title) button.title = title;
      button.onclick = handler;
      actions.appendChild(button);
    });
    if (actions.childElementCount) row.appendChild(actions);
    return row;
  }

  function renderFileFindings() {
    const block = $("fileFindingsBlock");
    const list = $("fileFindingsList");
    const meta = $("fileFindingsMeta");
    const more = $("fileFindingsMore");
    const summary = $("fileFindingsSummary");
    if (!block || !list) return;
    const entry = currentFileReview();
    const file = entry && entry.file;
    const counts = (file && file.findings) || {};
    const total = Number(counts.total || 0) + Number(counts.stale_open || 0);
    if (summary) {
      summary.textContent = "Findings for this file" + (total ? " · " + total : "");
    }
    block.hidden = !state.selectedFile || !state.selectedPr;
    if (block.hidden || !block.open) {
      // Bodies stay collapsed until requested so the diff is never buried in
      // long technical text.
      list.replaceChildren();
      if (more) more.hidden = true;
      if (meta) meta.textContent = "";
      return;
    }
    if (meta) {
      meta.textContent = entry && entry.error
        ? entry.error
        : entry && entry.loading ? "Loading findings…"
          : entry && entry.findingsOpened
            ? entry.findings.length + " of " + entry.findingCount + " loaded"
            : "Open to load findings.";
    }
    const fragment = document.createDocumentFragment();
    (entry ? entry.findings : []).forEach((finding) => {
      const busy = !!state.fileReviewBusy;
      const actions = [];
      if (finding.status === "open") {
        actions.push(["Resolve", () => findingAction(finding.finding_id, "resolve"), busy]);
        actions.push(["Dismiss", () => findingAction(finding.finding_id, "dismiss"), busy]);
      } else {
        actions.push(["Reopen", () => findingAction(finding.finding_id, "reopen"), busy]);
      }
      fragment.appendChild(findingRow(finding, { actions }));
    });
    if (!fragment.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = entry && entry.loading
        ? "Loading findings…" : "No findings recorded for this file revision.";
      fragment.appendChild(empty);
    }
    list.replaceChildren(fragment);
    if (more) {
      more.hidden = !entry || !entry.nextFindingPage;
      more.disabled = !entry || entry.loading;
      more.textContent = entry && entry.loading ? "Loading…" : "Load more findings";
    }
  }

  function renderAgentFileDrafts() {
    const block = $("fileDraftsBlock");
    const list = $("fileDraftsList");
    const meta = $("fileDraftsMeta");
    const more = $("fileDraftsMore");
    const count = $("fileDraftsCount");
    if (!block || !list) return;
    const entry = currentFileReview();
    if (count) count.textContent = entry && entry.draftCount ? " · " + entry.draftCount : "";
    if (!block.open) {
      list.replaceChildren();
      if (more) more.hidden = true;
      if (meta) meta.textContent = "";
      return;
    }
    if (meta) {
      meta.textContent = !state.selectedPr || !state.selectedFile
        ? "Select a pull request and file first."
        : entry && entry.error ? entry.error
          : entry && entry.loading ? "Loading agent drafts…"
            : entry && entry.draftsOpened
              ? entry.drafts.length + " of " + entry.draftCount + " drafts loaded"
              : "Open to load agent drafts.";
    }
    const fragment = document.createDocumentFragment();
    (entry ? entry.drafts : []).forEach((draft) => {
      const row = document.createElement("div");
      row.className = "draft-row";
      row.dataset.stale = String(draft.stale === true);
      const summary = draft.coverage_summary || {};
      addText(row, "draft-meta", [
        "Agent file review",
        "by " + (draft.actor || "agent"),
        draft.pending + " pending · " + draft.accepted + " accepted · " + draft.dismissed + " dismissed",
        "coverage: " + Number(summary.inspected || 0) + " inspected, " +
          Number(summary.skipped || 0) + " skipped, " + Number(summary.missing || 0) + " missing",
        draft.stale ? "written against an older revision — re-run the agent before adopting" : "",
      ].filter(Boolean).join(" · ")).title = [draft.draft_id, draft.created_at].filter(Boolean).join(" · ");
      const actions = document.createElement("div");
      actions.className = "draft-actions";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "btn";
      const active = entry && entry.draftId === draft.draft_id;
      open.textContent = active ? "Hide findings" : "Show " + draft.finding_count + " findings";
      open.disabled = !!(entry && entry.loading);
      open.onclick = () => {
        if (active) {
          entry.draftId = "";
          entry.draftFindings = [];
          entry.nextDraftFindingPage = 1;
          renderFileReviewPanels();
          return;
        }
        entry.draftFindings = [];
        entry.nextDraftFindingPage = 1;
        loadFileReview({ draftId: draft.draft_id, draftFindingPage: 1 });
      };
      actions.appendChild(open);
      row.appendChild(actions);
      if (active) {
        entry.draftFindings.forEach((finding) => {
          const busy = !!state.fileReviewBusy || draft.stale === true ||
            finding.status !== "pending";
          const title = draft.stale === true
            ? "This draft targets an older revision; adopting is blocked"
            : "";
          row.appendChild(findingRow(finding, {
            draft: true, actor: draft.actor,
            actions: [
              ["Adopt as finding",
                () => adoptDraftFinding(draft.draft_id, finding.draft_finding_id, "accept"),
                busy, title],
              ["Dismiss suggestion",
                () => adoptDraftFinding(draft.draft_id, finding.draft_finding_id, "dismiss"),
                busy, title],
            ],
          }));
        });
        if (entry.nextDraftFindingPage) {
          const nextButton = document.createElement("button");
          nextButton.type = "button";
          nextButton.className = "btn";
          nextButton.textContent = "Load more drafted findings";
          nextButton.disabled = !!entry.loading;
          nextButton.onclick = () => loadFileReview({
            draftId: draft.draft_id, draftFindingPage: entry.nextDraftFindingPage,
            appendDraftFindings: true,
          });
          row.appendChild(nextButton);
        }
      }
      fragment.appendChild(row);
    });
    if (!fragment.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = entry && entry.loading
        ? "Loading agent drafts…"
        : "No agent file-review draft exists for this pull request revision.";
      fragment.appendChild(empty);
    }
    list.replaceChildren(fragment);
    if (more) {
      more.hidden = !entry || !entry.nextDraftPage;
      more.disabled = !entry || entry.loading;
      more.textContent = entry && entry.loading ? "Loading…" : "Load more drafts";
    }
  }

  function renderFileReviewPanels() {
    renderFileReviewBar();
    renderFileFindings();
    renderAgentFileDrafts();
  }

  // Per-path review status for the file navigator.  Pages are loaded on
  // demand and never inferred: a path with no loaded row shows no claim, and
  // every remaining page stays reachable through an explicit load-more.
  const reviewIndex = { key: "", generation: 0, byPath: new Map(), next: 1,
    total: 0, loaded: 0, loading: false, error: "" };

  function reviewIndexFor(path) {
    const pr = state.selectedPr;
    if (!pr || reviewIndex.key !== snapshotKey() + "|" + pr) return null;
    return reviewIndex.byPath.get(path) || null;
  }

  function mergeReviewIndex(pr, data) {
    const key = snapshotKey() + "|" + pr;
    if (reviewIndex.key !== key) return;
    (data.files || []).forEach((row) => reviewIndex.byPath.set(row.path, row));
    reviewIndex.loaded = reviewIndex.byPath.size;
    if (data.file_count != null) reviewIndex.total = Number(data.file_count || 0);
  }

  function reviewIndexStatus() {
    if (reviewIndex.error) return "review status unavailable";
    if (reviewIndex.loading) return "loading review status…";
    if (!reviewIndex.total) return "";
    if (!reviewIndex.next) return "review status loaded for all " + reviewIndex.total;
    return "review status loaded for " + reviewIndex.loaded + "/" + reviewIndex.total;
  }

  function resetReviewIndex(key) {
    reviewIndex.key = key;
    // A new generation abandons any in-flight page; the old request cannot
    // leave this index stuck loading for the newly selected target.
    reviewIndex.generation += 1;
    reviewIndex.byPath = new Map();
    reviewIndex.next = 1;
    reviewIndex.total = 0;
    reviewIndex.loaded = 0;
    reviewIndex.loading = false;
    reviewIndex.error = "";
  }

  async function ensureReviewIndex(pr, options) {
    if (!pr || !state.repo || !stateInitialized) return;
    const opts = options || {};
    const key = snapshotKey() + "|" + pr;
    if (reviewIndex.key !== key) resetReviewIndex(key);
    const page = opts.page || reviewIndex.next;
    if (reviewIndex.loading || !page) return;
    const generation = ++reviewIndex.generation;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    reviewIndex.loading = true;
    reviewIndex.error = "";
    try {
      const data = await fetchFileReview({
        pr, page, page_size: 50, finding_page_size: 1, draft_page_size: 1,
      });
      if (reviewIndex.generation !== generation || reviewIndex.key !== key ||
          !workspaceCurrent(workspaceRepo, workspaceToken)) {
        return;
      }
      (data.files || []).forEach((row) => reviewIndex.byPath.set(row.path, row));
      reviewIndex.loaded = reviewIndex.byPath.size;
      reviewIndex.total = Number(data.file_count || 0);
      // Follow the manifest order so load-more always advances and the tail
      // stays reachable; a focused page never rewinds that pointer.
      if (!opts.page || opts.page === reviewIndex.next) {
        reviewIndex.next = data.next_page || null;
      }
    } catch (error) {
      if (reviewIndex.generation === generation && reviewIndex.key === key) {
        reviewIndex.error = error.message || "review status unavailable";
      }
    } finally {
      if (reviewIndex.generation === generation && reviewIndex.key === key) {
        reviewIndex.loading = false;
        const entry = currentManifest();
        if (entry) renderFileNavigator(entry);
      }
    }
  }

  function renderGroupProgress(group) {
    const root = $("groupProgress");
    if (!root) return;
    if (!group) {
      root.textContent = "";
      return;
    }
    const numbers = group.pr_numbers || [];
    // Progress counts saved individual decisions for the current revision
    // only. Unsaved edits never count, and the three buckets are disjoint so
    // a stale decision is not also counted as never reviewed.
    let decided = 0;
    let stale = 0;
    numbers.forEach((number) => {
      const pr = prByNumber(number);
      if (!pr) return;
      if (pr.disposition && pr.disposition !== "pending" && !pr.disposition_stale) decided += 1;
      else if (pr.disposition_stale === true) stale += 1;
    });
    const never = Math.max(0, numbers.length - decided - stale);
    root.textContent = decided + "/" + numbers.length +
      " PRs have a saved decision for their current revision" +
      (stale ? " · " + stale + " decided at an older revision" : "") +
      (never ? " · " + never + " never reviewed" : "");
  }

  async function fileReviewWrite(url, body, describe) {
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    const expected = currentViewContext();
    const identity = JSON.stringify([workspaceRepo, expected.store_version,
      expected.snapshot_version, url, body]);
    let idempotencyKey = fileReviewRetries.get(identity);
    if (!idempotencyKey) {
      idempotencyKey = makeIdempotencyKey("file-review");
      fileReviewRetries.set(identity, idempotencyKey);
      while (fileReviewRetries.size > 64) {
        fileReviewRetries.delete(fileReviewRetries.keys().next().value);
      }
    }
    state.fileReviewBusy = true;
    renderFileReviewPanels();
    try {
      const data = await api(url, {
        method: "POST",
        body: JSON.stringify({
          repo: state.repo,
          expected_store_version: expected.store_version,
          expected_snapshot_version: expected.snapshot_version,
          idempotency_key: idempotencyKey,
          actor: "human",
          ...body,
        }),
      });
      fileReviewRetries.delete(identity);
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return null;
      state.fileReviewBusy = false;
      // A file write never changes a PR decision, but the refreshed state
      // carries updated per-file counters for the navigator and PR header.
      if (data.state) applyState(data.state);
      await loadFileReview({
        reset: true,
        findings: !!($("fileFindingsBlock") && $("fileFindingsBlock").open),
        drafts: !!($("fileDraftsBlock") && $("fileDraftsBlock").open),
      });
      render();
      setStatus(describe);
      return data;
    } catch (error) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return null;
      state.fileReviewBusy = false;
      const entry = currentFileReview();
      if (entry) entry.error = error.message || "file review write failed";
      if (error.status === 409) {
        setStatus("file review not saved: the revision or snapshot changed");
        await loadState();
      } else {
        setStatus("file review save could not be confirmed: " + error.message);
      }
      renderFileReviewPanels();
      return null;
    }
  }

  async function toggleFileReviewed(reviewed) {
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const path = state.selectedFile;
    if (!pr || !path || state.fileReviewBusy) return null;
    // Capture the workspace before awaiting: the same PR number and path can
    // exist in another repository, and a stale command must not land there.
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    state.fileReviewBusy = true;
    renderFileReviewPanels();
    const evidence = await loadPrBody(pr.number).catch(() => null);
    state.fileReviewBusy = false;
    if (!workspaceCurrent(workspaceRepo, workspaceToken) ||
        state.selectedPr !== pr.number || state.selectedFile !== path) {
      renderFileReviewPanels();
      return null;
    }
    return fileReviewWrite("/api/file-reviews", {
      pr: pr.number, path, reviewed: !!reviewed,
      revision: revisionRefFromPr(pr, evidence),
    }, reviewed
      ? "marked " + path + " reviewed for this revision (the PR decision is unchanged)"
      : "cleared your file review for " + path);
  }

  async function findingAction(findingId, action) {
    if (!findingId || state.fileReviewBusy) return null;
    return fileReviewWrite(
      "/api/file-findings/" + encodeURIComponent(findingId) + "/" + action,
      {}, "finding " + action + "d",
    );
  }

  async function adoptDraftFinding(draftId, draftFindingId, action) {
    if (!draftId || !draftFindingId || state.fileReviewBusy) return null;
    return fileReviewWrite(
      "/api/file-drafts/" + encodeURIComponent(draftId) + "/findings/" +
      encodeURIComponent(draftFindingId) + "/" + action,
      {},
      action === "accept"
        ? "adopted the drafted finding (no file was marked reviewed, no PR decision changed)"
        : "dismissed the drafted suggestion (no PR was rejected)",
    );
  }

  function closeFindingDialog() {
    const dialog = $("findingDialog");
    findingTarget = null;
    if (dialog && dialog.open) dialog.close();
  }

  function openFindingDialog() {
    const dialog = $("findingDialog");
    const path = state.selectedFile;
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    if (!dialog || !path || !pr) return;
    findingReturnFocus = document.activeElement;
    const entry = currentFileReview();
    const missing = !!(entry && entry.file && entry.file.can_mark_reviewed === false);
    // The dialog owns its target and the exact revision it displayed from the
    // moment it opens. A later navigation or background refresh can never
    // redirect this submission or rebind the typed text to a newer revision.
    findingTarget = {
      pr: pr.number, path, repo: state.repo,
      workspaceToken: workspaceGen,
      revision: (entry && entry.revision) ||
        revisionRefFromPr(pr, exactPrEvidence(pr.number)),
      context: currentViewContext(),
      submitting: false,
    };
    $("findingScope").textContent = "Finding for " + path + " in PR #" + state.selectedPr +
      " at the currently displayed revision." +
      (missing ? " This file has no complete patch; a finding about the missing evidence is expected." : "");
    $("findingError").textContent = "";
    ["findingTitle", "findingLine", "findingHunk", "findingExplanation",
      "findingEvidence", "findingFix"].forEach((id) => {
      if ($(id)) $(id).value = "";
    });
    if ($("findingSeverity")) $("findingSeverity").value = missing ? "major" : "minor";
    dialog.showModal();
    if ($("findingTitle")) $("findingTitle").focus();
  }

  async function submitFinding() {
    const target = findingTarget;
    const error = $("findingError");
    if (!target || target.submitting) return;
    const path = target.path;
    if (!path) return;
    if (!workspaceCurrent(target.repo, target.workspaceToken)) {
      if (error) error.textContent = "The workspace changed; nothing was saved.";
      return;
    }
    // Refuse a changed snapshot instead of rebinding typed text to a newer
    // revision. The dialog keeps its content so nothing has to be retyped.
    const live = currentViewContext();
    if (live.repo !== target.context.repo ||
        live.store_version !== target.context.store_version ||
        live.snapshot_version !== target.context.snapshot_version) {
      if (error) {
        error.textContent = "The revision changed while this finding was open. " +
          "Nothing was saved; close and re-open Add finding to record it " +
          "against the current revision.";
      }
      return;
    }
    const title = String(($("findingTitle") || {}).value || "").trim();
    if (!title) {
      if (error) error.textContent = "A finding needs a short title.";
      if ($("findingTitle")) $("findingTitle").focus();
      return;
    }
    const rawLine = String(($("findingLine") || {}).value || "").trim();
    let line = null;
    if (rawLine) {
      line = Number(rawLine);
      if (!Number.isSafeInteger(line) || line <= 0) {
        if (error) error.textContent = "Line must be a positive whole number.";
        if ($("findingLine")) $("findingLine").focus();
        return;
      }
    }
    target.submitting = true;
    const saveButton = $("findingSaveBtn");
    if (saveButton) saveButton.disabled = true;
    const saved = await fileReviewWrite("/api/file-findings", {
      pr: target.pr, path, line,
      severity: String(($("findingSeverity") || {}).value || "minor"),
      title,
      explanation: String(($("findingExplanation") || {}).value || ""),
      evidence: String(($("findingEvidence") || {}).value || ""),
      suggested_fix: String(($("findingFix") || {}).value || ""),
      hunk: String(($("findingHunk") || {}).value || ""),
      // The revision captured when this dialog opened, never a newer one.
      revision: target.revision,
    }, "recorded a finding on " + path + " (the PR decision is unchanged)");
    target.submitting = false;
    if (saveButton) saveButton.disabled = false;
    if (saved) {
      closeFindingDialog();
      const block = $("fileFindingsBlock");
      if (block && !block.open) block.open = true;
      if (state.selectedPr === target.pr && state.selectedFile === target.path) {
        loadFileReview({ findings: true });
      }
    } else if (error) {
      error.textContent = "The save could not be confirmed. Check the status message; retrying unchanged details is safe.";
    }
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

  const DECISION_NAMES = { keep: "Keep", duplicate: "Duplicate", reject: "Reject",
    needs_hardware: "Needs hardware", upgrade: "Upgrade risk", pending: "Pending" };

  function revisionComplete(pr) {
    return !!pr && pr.evidence_complete === true && !!pr.content_digest &&
      (state.source === "fixtures" || !!(pr.head_sha && pr.base_sha));
  }

  function renderPrDecisionBadge(pr) {
    const badge = $("prDecisionBadge");
    const button = $("prDecisionBtn");
    if (button) {
      button.disabled = !pr;
      button.textContent = pr ? "Decide PR #" + pr.number : "Decide PR";
    }
    if (!badge) return;
    if (!pr) {
      badge.dataset.state = "pending";
      badge.textContent = "No pull request selected";
      badge.title = "";
      return;
    }
    const saved = pr.disposition && pr.disposition !== "pending" ? pr.disposition : "";
    const stale = pr.disposition_stale === true ? (pr.stale_disposition || {}) : null;
    // Saved status is a persisted fact. Unsaved editing in the dialog never
    // changes this badge, the work list, or group progress.
    if (saved) {
      badge.dataset.state = saved === "reject" ? "rejected" : "saved";
      badge.textContent = DECISION_NAMES[saved] || saved;
      badge.title = [
        "saved " + (pr.disposition_at || "at an unknown time"),
        pr.disposition_actor ? "by " + pr.disposition_actor : "",
        pr.disposition_source ? "source " + pr.disposition_source : "",
        pr.duplicate_of ? "duplicate of #" + pr.duplicate_of : "",
        pr.disposition_reason ? "reason: " + pr.disposition_reason : "",
      ].filter(Boolean).join(" · ");
    } else if (stale) {
      badge.dataset.state = "stale";
      badge.textContent = "Stale — needs re-review";
      badge.title = "Decided " + (DECISION_NAMES[stale.disposition] || stale.disposition) +
        " at an older revision" + (stale.actor ? " by " + stale.actor : "") +
        (stale.decided_at ? " on " + stale.decided_at : "") +
        ". That decision no longer applies to the current revision.";
    } else {
      badge.dataset.state = "pending";
      badge.textContent = "Not reviewed";
      badge.title = "No decision has been saved for this pull request revision.";
    }
  }

  function decisionCoverageSummary(pr) {
    const counts = (pr && pr.review_counts) || {};
    const total = Number(pr && (pr.path_count ?? (pr.paths || []).length)) || 0;
    const reviewed = Number(counts.files_reviewed || 0);
    const open = Number(counts.findings_open || 0);
    return {
      total, reviewed, open,
      agent: Number(counts.agent_inspected || 0),
      incompleteCoverage: total > 0 && reviewed < total,
    };
  }

  function renderDispositionPanel(group, pr) {
    const valid = !!(group && pr && (group.pr_numbers || []).includes(pr.number));
    renderPrDecisionBadge(valid ? pr : null);
    renderGroupProgress(group);
    const dialog = $("prDecisionDialog");
    if (!valid) {
      if (dialog && dialog.open) dialog.close();
      return;
    }
    const draft = ensureDispositionDraft(pr);
    const files = (pr.path_count ?? (pr.paths || []).length) || 0;
    const title = $("prDecisionTitle");
    if (title) {
      title.textContent = "Overall decision for PR #" + pr.number + " · all " +
        files + " changed files";
    }
    const scope = $("prDecisionScope");
    if (scope) {
      scope.textContent = "This decision covers the whole pull request at the " +
        "currently displayed revision. Marking individual files reviewed never " +
        "changes it. Local only; nothing is sent to GitHub.";
    }
    const summary = decisionCoverageSummary(pr);
    const summaryRoot = $("prDecisionSummary");
    if (summaryRoot) {
      summaryRoot.replaceChildren();
      const lines = [
        ["You reviewed " + summary.reviewed + "/" + summary.total + " files", false],
        ["Agent inspected " + summary.agent + "/" + summary.total + " files " +
          "(separate coverage, not your review)", false],
        [summary.open
          ? summary.open + " unresolved finding" + (summary.open === 1 ? "" : "s")
          : "No unresolved findings", summary.open > 0],
        [revisionComplete(pr)
          ? "Revision evidence is complete"
          : "Revision evidence is incomplete — Keep and Duplicate are blocked", !revisionComplete(pr)],
      ];
      lines.forEach(([text, warn]) => {
        const line = document.createElement("div");
        line.className = warn ? "warn-line" : "";
        line.textContent = text;
        summaryRoot.appendChild(line);
      });
    }
    const chips = $("decisionChips");
    if (chips) {
      chips.replaceChildren();
      ["keep", "duplicate", "reject", "needs_hardware", "upgrade", "pending"].forEach((value) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn decision-chip";
        button.textContent = DECISION_NAMES[value];
        button.setAttribute("aria-pressed", String(draft.disposition === value));
        const complete = revisionComplete(pr);
        button.disabled = !!state.dispositionBusy || (!complete && ["keep", "duplicate"].includes(value));
        if (!complete && ["keep", "duplicate"].includes(value)) {
          // Missing evidence blocks Keep and Duplicate outright. No checkbox
          // acknowledgement can substitute for the evidence itself.
          button.title = "Requires complete evidence for this revision";
        }
        button.addEventListener("click", () => {
          draft.disposition = value;
          draft.editing = true;
          if (value !== "duplicate") draft.duplicate_of = null;
          renderDispositionPanel(group, pr);
        });
        chips.appendChild(button);
      });
    }
    const label = $("dispositionPrLabel");
    if (label) label.textContent = "#" + pr.number + (pr.disposition && pr.disposition !== "pending"
      ? " · current " + pr.disposition : " · not yet decided");
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
        option.textContent = "#" + number + " — " + ((prByNumber(number) || {}).title || "");
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
    // Keep with unresolved findings or incomplete human coverage needs an
    // explicit acknowledgement so nothing silently implies "all checked".
    const needsAck = draft.disposition === "keep" &&
      (summary.open > 0 || summary.incompleteCoverage);
    const ackWrap = $("decisionAckWrap");
    const ack = $("decisionAck");
    if (ackWrap) ackWrap.hidden = !needsAck;
    if (ack) {
      if (!needsAck) ack.checked = false;
      ack.onchange = () => renderDispositionPanel(group, pr);
    }
    const ackText = $("decisionAckText");
    if (ackText && needsAck) {
      ackText.textContent = "I am keeping PR #" + pr.number + " with " +
        (summary.open ? summary.open + " unresolved finding" +
          (summary.open === 1 ? "" : "s") : "incomplete file review coverage") +
        (summary.open && summary.incompleteCoverage
          ? " and " + (summary.total - summary.reviewed) +
            (summary.total - summary.reviewed === 1 ? " file" : " files") + " I have not reviewed" : "") +
        ". Marking files reviewed is not approval and I am not claiming everything was checked.";
    }
    const save = $("dispositionSaveBtn");
    if (save) {
      save.disabled = !!state.dispositionBusy || (needsAck && !(ack && ack.checked));
      save.textContent = state.dispositionBusy ? "Saving…" : "Save PR decision";
      save.onclick = saveSelectedDisposition;
    }
    const next = $("dispositionNextBtn");
    if (next) {
      next.disabled = !!state.dispositionBusy || (needsAck && !(ack && ack.checked));
      next.onclick = saveDispositionAndNext;
    }
    const cancel = $("dispositionCancelBtn");
    if (cancel) {
      cancel.disabled = !!state.dispositionBusy;
      cancel.onclick = () => closePrDecisionDialog(true);
    }
    const hint = $("dispositionHint");
    if (hint) {
      hint.textContent = (pr.disposition_revision
        ? "Bound to the currently displayed exact revision. A changed revision must be reviewed again."
        : "For this PR revision only. Other group members are unchanged.") +
        " Duplicate records a pull-request-level relation to another PR; it does not claim any file is equivalent, and it does not Keep that other PR.";
    }
  }

  function closePrDecisionDialog(discard) {
    const dialog = $("prDecisionDialog");
    if (discard) state.dispositionDraft = null;
    if (dialog && dialog.open) dialog.close();
  }

  const MAX_BULK_REVIEW_ITEMS = 200;

  function bulkCandidates(group) {
    return (group && group.pr_numbers || []).map(prByNumber).filter(Boolean);
  }

  function bulkGaps(pr) {
    const summary = decisionCoverageSummary(pr);
    const gaps = [];
    if (summary.open) {
      gaps.push(summary.open + " unresolved finding" + (summary.open === 1 ? "" : "s"));
    }
    if (summary.incompleteCoverage) {
      gaps.push((summary.total - summary.reviewed) + " files you have not reviewed");
    }
    return gaps;
  }

  function resetBulkConfirmation() {
    const confirm = $("bulkReviewConfirm");
    // Any change to the scope or the decision invalidates a prior tick.
    if (confirm) confirm.checked = false;
  }

  function renderBulkReview() {
    const group = groupById(state.bulkReview ? state.bulkReview.groupId : state.selectedGroupId);
    const list = $("bulkReviewList");
    const preview = $("bulkReviewPreview");
    const save = $("bulkReviewSaveBtn");
    const confirm = $("bulkReviewConfirm");
    const decision = $("bulkReviewDecision");
    const scope = $("bulkReviewScope");
    if (!list || !preview || !group || !state.bulkReview) return;
    const chosen = state.bulkReview.selected;
    const target = decision ? decision.value : "";
    if (scope) {
      scope.textContent = "Saves one individual decision per chosen pull request " +
        "in " + group.group_id + ". Each row keeps its own revision binding; no " +
        "group rule is written and nothing is sent to GitHub.";
    }
    const rows = bulkCandidates(group);
    const fragment = document.createDocumentFragment();
    rows.forEach((pr) => {
      const row = document.createElement("label");
      row.className = "bulk-review-row";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = chosen.has(pr.number);
      box.onchange = () => {
        if (box.checked) chosen.add(pr.number);
        else chosen.delete(pr.number);
        resetBulkConfirmation();
        renderBulkReview();
      };
      row.appendChild(box);
      const text = document.createElement("span");
      const current = pr.disposition && pr.disposition !== "pending"
        ? pr.disposition : pr.disposition_stale === true ? "stale" : "pending";
      text.textContent = "#" + pr.number + " " + (pr.title || "") +
        " — now: " + current + (target ? " → " + target : "");
      row.appendChild(text);
      fragment.appendChild(row);
    });
    list.replaceChildren(fragment);
    const selected = rows.filter((pr) => chosen.has(pr.number));
    const overwrite = selected.filter(
      (pr) => pr.disposition && pr.disposition !== "pending"
    );
    const blocked = target === "keep"
      ? selected.filter((pr) => !revisionComplete(pr)) : [];
    const gapped = target === "keep"
      ? selected.filter((pr) => bulkGaps(pr).length) : [];
    const tooMany = selected.length > MAX_BULK_REVIEW_ITEMS;
    preview.replaceChildren();
    const lines = [
      [selected.length + " of " + rows.length + " pull requests will change", false],
      [overwrite.length
        ? "Overwrites " + overwrite.length + " saved decision" +
          (overwrite.length === 1 ? "" : "s") + ": " +
          overwrite.map((pr) => "#" + pr.number + " " + pr.disposition).join(", ")
        : "No saved decision will be overwritten", overwrite.length > 0],
      [blocked.length
        ? "Blocked: " + blocked.map((pr) => "#" + pr.number).join(", ") +
          " lack complete revision evidence for keep"
        : "", blocked.length > 0],
      [gapped.length
        ? "Keeping with known gaps: " + gapped.map((pr) =>
            "#" + pr.number + " (" + bulkGaps(pr).join(", ") + ")").join("; ")
        : "", gapped.length > 0],
      [tooMany
        ? "Choose at most " + MAX_BULK_REVIEW_ITEMS + " pull requests per batch"
        : "", tooMany],
    ];
    lines.forEach(([text, warn]) => {
      if (!text) return;
      const line = document.createElement("div");
      if (warn) line.className = "overwrite";
      line.textContent = text;
      preview.appendChild(line);
    });
    const confirmText = $("bulkReviewConfirmText");
    if (confirmText) {
      confirmText.textContent = gapped.length
        ? "I reviewed the preview above and want to overwrite these saved " +
          "decisions. I am keeping " +
          gapped.map((pr) => "#" + pr.number).join(", ") +
          " with known unresolved findings or unreviewed files, and I am not " +
          "claiming everything was checked."
        : "I reviewed the preview above and want to overwrite these saved decisions.";
    }
    if (save) {
      save.disabled = !!state.bulkReview.busy || !selected.length || !target ||
        blocked.length > 0 || tooMany || !(confirm && confirm.checked);
      save.textContent = state.bulkReview.busy ? "Saving…" : "Save PR decisions";
    }
  }

  function openBulkReviewDialog() {
    const group = groupById(state.selectedGroupId);
    const dialog = $("bulkReviewDialog");
    if (!dialog || !group) {
      setStatus("select a group before starting a bulk PR review");
      return;
    }
    bulkReturnFocus = document.activeElement;
    // The dialog owns its target from the moment it opens: group, repository,
    // and the exact view context it previewed.
    state.bulkReview = {
      selected: new Set(), busy: false, groupId: group.group_id,
      repo: state.repo, workspaceToken: workspaceGen,
      context: currentViewContext(), retries: new Map(),
    };
    if ($("bulkReviewDecision")) $("bulkReviewDecision").value = "";
    if ($("bulkReviewReason")) $("bulkReviewReason").value = "";
    if ($("bulkReviewConfirm")) $("bulkReviewConfirm").checked = false;
    if ($("bulkReviewError")) $("bulkReviewError").textContent = "";
    renderBulkReview();
    dialog.showModal();
    if ($("bulkReviewDecision")) $("bulkReviewDecision").focus();
  }

  async function saveBulkReview() {
    const error = $("bulkReviewError");
    const batch = state.bulkReview;
    if (!batch || batch.busy) return;
    // Apply to the target captured when the dialog opened, never to whatever
    // is selected now.
    const group = groupById(batch.groupId);
    if (!group || !workspaceCurrent(batch.repo, batch.workspaceToken)) {
      if (error) error.textContent = "The workspace changed; nothing was saved.";
      return;
    }
    const expected = batch.context;
    const live = currentViewContext();
    if (live.repo !== expected.repo ||
        live.store_version !== expected.store_version ||
        live.snapshot_version !== expected.snapshot_version ||
        live.view_revision !== expected.view_revision) {
      if (error) {
        error.textContent = "The snapshot changed after this preview was built. " +
          "Nothing was saved; close and review the new state.";
      }
      return;
    }
    const target = String(($("bulkReviewDecision") || {}).value || "");
    const reason = String(($("bulkReviewReason") || {}).value || "").trim();
    const selected = bulkCandidates(group)
      .filter((pr) => batch.selected.has(pr.number));
    if (!selected.length || !target) {
      if (error) error.textContent = "Choose a decision and at least one pull request.";
      return;
    }
    if (selected.length > MAX_BULK_REVIEW_ITEMS) {
      if (error) {
        error.textContent = "Choose at most " + MAX_BULK_REVIEW_ITEMS +
          " pull requests per batch.";
      }
      return;
    }
    if (!reason || reason.length > 1000) {
      if (error) error.textContent = "A reason of 1-1000 characters is required.";
      if ($("bulkReviewReason")) $("bulkReviewReason").focus();
      return;
    }
    const confirm = $("bulkReviewConfirm");
    if (!(confirm && confirm.checked)) {
      if (error) {
        error.textContent = "Tick the confirmation to acknowledge the preview " +
          "before overwriting these decisions.";
      }
      return;
    }
    if (target === "keep" && selected.some((pr) => !revisionComplete(pr))) {
      if (error) {
        error.textContent = "Keep needs complete revision evidence for every " +
          "chosen pull request. Nothing was saved.";
      }
      return;
    }
    const workspaceToken = batch.workspaceToken;
    const workspaceRepo = batch.repo;
    batch.busy = true;
    renderBulkReview();
    if (error) error.textContent = "";
    try {
      const items = [];
      for (const pr of selected) {
        const evidence = await loadPrBody(pr.number);
        if (!workspaceCurrent(workspaceRepo, workspaceToken) ||
            state.bulkReview !== batch) {
          return;
        }
        items.push({
          pr: pr.number, disposition: target, reason,
          revision: revisionRefFromPr(pr, evidence), duplicate_of: null,
        });
      }
      const after = currentViewContext();
      if (after.store_version !== expected.store_version ||
          after.snapshot_version !== expected.snapshot_version ||
          after.view_revision !== expected.view_revision) {
        throw Object.assign(new Error("the snapshot changed while preparing this batch"),
          { status: 409 });
      }
      // One atomic save through the existing disposition writer: every row is
      // an individual PR decision sharing one event, not a group rule.
      const identity = JSON.stringify(items);
      if (!batch.retries.has(identity)) {
        batch.retries.set(identity, makeIdempotencyKey("bulk-review"));
      }
      const data = await api("/api/dispositions", {
        method: "POST",
        body: JSON.stringify({
          repo: expected.repo, group_id: group.group_id, items,
          expected_store_version: expected.store_version,
          expected_snapshot_version: expected.snapshot_version,
          idempotency_key: batch.retries.get(identity),
          actor: "human",
        }),
      });
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return;
      batch.busy = false;
      if (data.state) applyState(data.state);
      render();
      const dialog = $("bulkReviewDialog");
      if (dialog && dialog.open) dialog.close();
      setStatus("saved " + items.length + " individual PR decisions in one batch event");
    } catch (err) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return;
      batch.busy = false;
      if (state.bulkReview !== batch) return;
      if (err.status === 409) {
        if (error) error.textContent = "Nothing was saved: the snapshot or a revision changed. Reload and try again.";
        await loadState();
      } else if (error) {
        error.textContent = "The save could not be confirmed. Retry the unchanged batch or reload to check its status: " + err.message;
      }
      renderBulkReview();
    }
  }

  function openPrDecisionDialog() {
    const group = groupById(state.selectedGroupId);
    const pr = state.selectedPr ? prByNumber(state.selectedPr) : null;
    const dialog = $("prDecisionDialog");
    if (!dialog || !group || !pr) return;
    prDecisionReturnFocus = document.activeElement;
    const error = $("prDecisionError");
    if (error) error.textContent = "";
    renderDispositionPanel(group, pr);
    dialog.showModal();
    const reason = $("dispositionReason");
    if (reason) reason.focus();
  }

  async function saveDispositionAndNext() {
    const repo = state.repo;
    const token = workspaceGen;
    const selected = state.selectedPr;
    const pending = visibleGroups().flatMap((group) => group.pr_numbers || [])
      .filter((number) => {
        const pr = prByNumber(number);
        return pr && (!pr.disposition || pr.disposition === "pending");
      });
    const index = pending.indexOf(selected);
    const candidates = pending.slice(index + 1).concat(pending.slice(0, Math.max(0, index)))
      .filter((number) => number !== selected);
    const result = await saveSelectedDisposition();
    if (!result || !result.ok || !workspaceCurrent(repo, token) || state.selectedPr !== selected) return;
    const next = candidates.map(prByNumber).find((pr) => pr && (!pr.disposition || pr.disposition === "pending"));
    if (!next) { setStatus("Decision saved. No more pending PRs in this view."); return; }
    changeView(() => {
      state.selectedGroupId = next.group_id;
      state.selectedPr = next.number;
      state.selectedFile = (next.paths || [])[0] || null;
    });
    render();
  }

  const fileManifests = new Map();
  let fileManifestScope = "";
  let filesPaneActive = false;
  let filesDialogFocus = null;
  let filesCompare = { key: "", pr: null };

  function manifestScope() {
    return JSON.stringify([state.repo, state.snapshotVersion, state.storeVersion]);
  }

  function prMayContainPath(pr, path) {
    if ((pr.paths || []).includes(path)) return true;
    const entry = fileManifestScope === manifestScope() && fileManifests.get(pr.number);
    if (entry && entry.rows.some((file) => file.path === path || file.previous_path === path)) return true;
    if (entry && entry.next === null) return false;
    // UI summaries are capped. An unseen path is not proof of absence; the
    // patch endpoint remains authoritative for URL/agent-selected paths.
    return !!pr.paths_truncated || Number(pr.path_count) > (pr.paths || []).length;
  }

  function currentManifest() {
    const scope = manifestScope();
    if (scope !== fileManifestScope) {
      fileManifests.clear();
      fileManifestScope = scope;
    }
    if (!state.selectedPr) return null;
    let entry = fileManifests.get(state.selectedPr);
    if (!entry) {
      entry = { pr: state.selectedPr, scope, rows: [], total: null, next: 1,
        loading: false, error: "", query: "", scroll: 0 };
      fileManifests.set(entry.pr, entry);
      while (fileManifests.size > 4) fileManifests.delete(fileManifests.keys().next().value);
    }
    return entry;
  }

  async function loadManifestPage(entry) {
    if (!entry || entry.loading || entry.next === null) return;
    entry.loading = true;
    entry.error = "";
    const groupId = state.selectedGroupId;
    const page = entry.next;
    const visible = () => entry.scope === manifestScope() &&
      fileManifests.get(entry.pr) === entry && state.selectedPr === entry.pr &&
      state.selectedGroupId === groupId;
    renderFileNavigator(entry);
    try {
      const response = await readTool("get_pr", {
        repo: state.repo, pr: entry.pr, file_page: page, file_page_size: 80,
        expected_store_version: state.storeVersion,
        expected_snapshot_version: state.snapshotVersion,
      });
      if (entry.scope !== manifestScope() || fileManifests.get(entry.pr) !== entry) return;
      if (!response.ok) throw new Error(response.error && response.error.message || "Could not load files");
      const data = response.data;
      const context = response.context;
      if (!context || context.repo !== state.repo ||
          context.store_version !== state.storeVersion || context.snapshot_version !== state.snapshotVersion) {
        throw new Error("Snapshot changed. Reload the workspace before loading more files.");
      }
      const seen = new Set(entry.rows.map((file) => file.path));
      for (const file of data.files || []) {
        if (validRepoPath(file.path) && !seen.has(file.path)) {
          entry.rows.push(file);
          seen.add(file.path);
        }
      }
      entry.total = data.file_count;
      entry.next = data.next_file_page || null;
    } catch (error) {
      entry.error = error.message || "Could not load files";
    } finally {
      entry.loading = false;
      if (visible()) {
        renderFileNavigator(entry);
        const count = $("reviewFiles") && $("reviewFiles").querySelector(".file-nav-count");
        if (count) count.textContent = manifestCount(entry);
      }
    }
  }

  function manifestCount(entry) {
    if (entry.error) return entry.rows.length ? entry.rows.length + " loaded · Could not load more files" : "Could not load files";
    return entry.total === null ? "Loading files…" : entry.rows.length + " / " + entry.total + " loaded";
  }

  function setFilesPane(active) {
    filesPaneActive = active;
    const dialog = $("fileNavigatorDialog");
    const inDialog = !!(dialog && dialog.open);
    const layout = document.querySelector(".layout");
    if (layout) layout.classList.toggle("files-active", active);
    if ($("worklistPane")) $("worklistPane").hidden = active && !inDialog;
    if ($("fileNavigator")) $("fileNavigator").hidden = !active && !inDialog;
    for (const [id, selected] of [["navigatorWorklistBtn", !active], ["navigatorFilesBtn", active]]) {
      const button = $(id);
      if (button) {
        button.classList.toggle("active", selected);
        button.setAttribute("aria-pressed", String(selected));
      }
    }
  }

  function openFilesNavigator() {
    const entry = currentManifest();
    if (!entry) return;
    if (window.matchMedia("(max-width: 700px)").matches) {
      const dialog = $("fileNavigatorDialog");
      if (!dialog || dialog.open) return;
      filesDialogFocus = document.activeElement;
      $("fileNavigatorDialogBody").appendChild($("fileNavigator"));
      $("fileNavigator").hidden = false;
      $("fileNavigatorDialogTitle").textContent = "Files · #" + entry.pr;
      dialog.showModal();
      $("fileNavigatorSearch").focus();
    } else setFilesPane(true);
    renderFileNavigator(entry);
  }

  function renderFileNavigator(entry) {
    const list = $("fileNavigatorList");
    if (!list || !entry || entry.pr !== state.selectedPr || entry.scope !== manifestScope()) return;
    $("fileNavigatorTitle").textContent = "Files · #" + entry.pr;
    if ($("fileNavigatorDialogTitle")) $("fileNavigatorDialogTitle").textContent = "Files · #" + entry.pr;
    $("fileNavigatorSearch").value = entry.query;
    const query = entry.query.trim().toLocaleLowerCase();
    const rows = entry.rows.filter((file) => file.path.toLocaleLowerCase().includes(query) ||
      (file.previous_path || "").toLocaleLowerCase().includes(query));
    $("fileNavigatorStatus").textContent = entry.error ||
      (manifestCount(entry) + (query ? " · " + rows.length + (rows.length === 1 ? " match" : " matches") + " in loaded files" : "") +
      (entry.next && entry.total !== null ? " · " + Math.max(0, entry.total - entry.rows.length) + " remaining" : "") +
      (entry.loading && entry.rows.length ? " · Loading…" : "") +
      (reviewIndexStatus() ? " · " + reviewIndexStatus() : ""));
    const groups = new Map();
    for (const file of rows) {
      const parts = file.path.split("/");
      const directory = parts.length === 1 ? "Repository root" : parts.slice(0, parts.length > 2 ? 2 : 1).join("/") + "/";
      if (!groups.has(directory)) groups.set(directory, []);
      groups.get(directory).push(file);
    }
    const fragment = document.createDocumentFragment();
    for (const [directory, files] of groups) {
      const heading = document.createElement("div");
      heading.className = "file-nav-directory";
      heading.textContent = directory + " · " + files.length;
      fragment.appendChild(heading);
      for (const file of files) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "file-nav-row" + (file.path === state.selectedFile ? " active" : "");
        button.setAttribute("aria-pressed", String(file.path === state.selectedFile));
        const add = (className, text) => {
          const span = document.createElement("span"); span.className = className;
          span.textContent = text; button.appendChild(span);
        };
        add("file-nav-name", file.path.split("/").pop());
        add("file-nav-parent", file.previous_path ? file.previous_path + " → " + file.path : file.path);
        add("file-nav-status", ({ added: "A", modified: "M", removed: "D", deleted: "D", renamed: "R" })[file.status] || file.status || "M");
        add("file-nav-meta", (file.additions == null ? "" : "+" + file.additions) + " " +
          (file.deletions == null ? "" : "−" + file.deletions));
        if (!file.patch_available) add("file-nav-gap", "No patch");
        else if (!file.patch_complete) add("file-nav-gap", "Partial patch");
        const review = reviewIndexFor(file.path);
        if (review) {
          const counts = review.findings || {};
          const human = review.human || {};
          const agent = review.agent || {};
          add("file-nav-review" + (human.reviewed ? " reviewed" : ""),
            human.reviewed ? "You reviewed"
              : human.stale_history ? "Re-review needed" : "Not reviewed");
          if (agent.status) add("file-nav-agent", "Agent " + agent.status);
          if (counts.open) add("file-nav-findings", counts.open + " open");
          else if (counts.total) add("file-nav-findings", counts.total + " findings");
        }
        button.onclick = () => {
          const group = groupById(state.selectedGroupId);
          const wasDialogOpen = $("fileNavigatorDialog").open;
          changeView(() => { state.selectedFile = file.path; });
          if (wasDialogOpen) $("fileNavigatorDialog").close();
          renderReviewFiles(group);
          if (!wasDialogOpen) {
            const selected = list.querySelector('[aria-pressed="true"]');
            if (selected) selected.focus({ preventScroll: true });
          }
          renderDiffs(group);
          loadRelatedIfOpen(state.selectedPr, file.path);
          renderFileReviewPanels();
          loadFileReview({
            reset: true,
            findings: !!($("fileFindingsBlock") && $("fileFindingsBlock").open),
            drafts: !!($("fileDraftsBlock") && $("fileDraftsBlock").open),
          });
          if ($("fileQueueBlock").open) openFileQueue(file.path, { scroll: false });
          writeUrl(true);
        };
        fragment.appendChild(button);
      }
    }
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = entry.loading ? "Loading files…" : query ? "No matching loaded files." : "No file metadata available.";
      fragment.appendChild(empty);
    }
    list.replaceChildren(fragment);
    list.scrollTop = entry.scroll;
    const more = $("fileNavigatorMore");
    more.hidden = entry.next === null;
    more.disabled = entry.loading;
    more.textContent = entry.loading ? "Loading…" : entry.error ? "Retry loading files" : "Load more files";
    more.onclick = () => loadManifestPage(entry);
    const reviewMore = $("fileNavigatorReviewMore");
    if (reviewMore) {
      reviewMore.hidden = !reviewIndex.next || reviewIndex.key !== snapshotKey() + "|" + entry.pr;
      reviewMore.disabled = reviewIndex.loading;
      reviewMore.textContent = reviewIndex.loading
        ? "Loading review status…"
        : reviewIndex.error
          ? "Retry loading review status"
          : "Load review status for more files";
      reviewMore.onclick = () => ensureReviewIndex(entry.pr);
    }
    list.onscroll = () => { entry.scroll = list.scrollTop; };
    $("fileNavigatorSearch").oninput = (event) => {
      entry.query = event.target.value;
      entry.scroll = 0;
      renderFileNavigator(entry);
    };
  }

  function renderReviewFiles(group) {
    const root = $("reviewFiles");
    if (!root) return;
    root.replaceChildren();
    const entry = currentManifest();
    const key = JSON.stringify([state.repo, state.selectedGroupId, state.selectedPr]);
    if (filesCompare.key !== key) filesCompare = { key, pr: null };
    state.fileComparePr = filesCompare.pr;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "btn file-nav-open";
    open.textContent = "Files";
    open.onclick = openFilesNavigator;
    root.appendChild(open);
    const path = document.createElement("span");
    path.className = "file-nav-path";
    path.textContent = state.selectedFile || "Select a changed file";
    root.appendChild(path);
    const count = document.createElement("span");
    count.className = "file-nav-count";
    count.textContent = entry ? manifestCount(entry) : "No PR selected";
    root.appendChild(count);
    const compare = document.createElement("select");
    compare.className = "btn file-nav-compare";
    compare.setAttribute("aria-label", "Compare selected PR with");
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "Compare with…";
    compare.appendChild(none);
    ((group && group.pr_numbers) || []).filter((number) => number !== state.selectedPr).forEach((number) => {
      const option = document.createElement("option");
      option.value = String(number);
      option.textContent = "#" + number;
      compare.appendChild(option);
    });
    compare.value = filesCompare.pr ? String(filesCompare.pr) : "";
    const layout = document.createElement("select");
    layout.className = "btn diff-layout-choice";
    layout.setAttribute("aria-label", "Comparison layout");
    layout.title = "Auto uses two columns when the diff area is at least 740px wide";
    for (const [value, label] of [["auto", "Auto layout"], ["side", "Side by side"], ["stacked", "Stacked"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      layout.appendChild(option);
    }
    layout.value = document.documentElement.dataset.diffLayout || "auto";
    layout.hidden = !filesCompare.pr;
    layout.onchange = () => {
      document.documentElement.dataset.diffLayout = layout.value;
      try { window.localStorage.setItem("triage.diff-layout.v1", layout.value); } catch (_) {}
    };
    compare.onchange = () => {
      filesCompare.pr = Number(compare.value) || null;
      state.fileComparePr = filesCompare.pr;
      layout.hidden = !filesCompare.pr;
      renderDiffs(group);
    };
    if (compare.options.length > 1) root.append(compare, layout);
    setFilesPane(filesPaneActive);
    renderFileNavigator(entry);
    if (stateInitialized && entry && !entry.rows.length && !entry.error && entry.next !== null) loadManifestPage(entry);
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
    // Accepting can record Keep for PRs that still have open file findings or
    // unreviewed files. That needs an explicit, named acknowledgement; it does
    // not weaken the backend evidence gate, and it adopts no file drafts.
    const gapped = proposalKeepGaps(rows);
    const ackWrap = $("proposalAckWrap");
    const ack = $("proposalAck");
    const ackText = $("proposalAckText");
    if (ackWrap) ackWrap.hidden = !gapped.length;
    if (ack) {
      if (!gapped.length) ack.checked = false;
      ack.disabled = locked;
      ack.onchange = () => {
        state.proposalError = "";
        renderProposalPanel();
      };
    }
    if (ackText && gapped.length) {
      ackText.textContent = "Accepting Keeps " +
        gapped.map((item) => "#" + item.pr + " (" + item.gaps.join(", ") + ")").join("; ") +
        ". I am not claiming those files were checked, and accepting adopts no " +
        "agent file findings.";
    }
    const ackBlocked = gapped.length > 0 && !(ack && ack.checked);
    if (accept) { accept.disabled = locked || ackBlocked; accept.textContent = state.proposalAction === "accept" ? "Accepting…" : "Accept proposal"; accept.onclick = () => transitionProposal("accept"); }
    if (edit) { edit.disabled = locked; edit.textContent = state.proposalAction === "edit" ? "Saving…" : "Save edits"; edit.onclick = () => transitionProposal("edit"); }
    if (reject) { reject.disabled = locked; reject.textContent = state.proposalAction === "reject" ? "Rejecting…" : "Reject proposal"; reject.onclick = () => transitionProposal("reject"); }
    const hint = $("proposalHint");
    if (hint) hint.textContent = state.proposalLoading ? "Loading proposal…" : state.proposalError ||
      (proposal.status === "accepted" ? "Accepted; recorded exact human dispositions." :
        proposal.status === "rejected" ? "Rejected; no dispositions were applied." :
          "Edit the bounded items if needed, then explicitly accept or reject. " +
          "Rejecting rejects this proposal, not the pull requests, and accepting " +
          "adopts no agent file findings.");
  }

  // Which proposed Keeps land on a PR that still has open findings or files
  // nobody reviewed. Counters come from the store projection, never guessed.
  function proposalKeepGaps(rows) {
    const gapped = [];
    (rows || []).forEach((item) => {
      if (item.disposition !== "keep") return;
      const pr = prByNumber(item.pr);
      if (!pr) return;
      const gaps = bulkGaps(pr);
      if (gaps.length) gapped.push({ pr: item.pr, gaps });
    });
    return gapped;
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
    if (action === "accept") {
      const gapped = proposalKeepGaps(proposalEditRows(proposal));
      const ack = $("proposalAck");
      if (gapped.length && !(ack && ack.checked)) {
        state.proposalError = "Acknowledge the listed Keep gaps before accepting: " +
          gapped.map((item) => "#" + item.pr).join(", ");
        renderProposalPanel();
        return actionError("acknowledgement_required", state.proposalError, false);
      }
    }
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
      const slot = $("prDecisionError");
      if (slot) slot.textContent = "A reason of 1-1000 characters is required.";
      if ($("dispositionReason")) $("dispositionReason").focus();
      return false;
    }
    const errorSlot = $("prDecisionError");
    if (errorSlot) errorSlot.textContent = "";
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
      closePrDecisionDialog(true);
      setStatus("saved per-PR disposition for #" + selectedPr);
      return { ok: true, dispositions: data.dispositions || [], context: currentViewContext() };
    } catch (error) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return false;
      state.dispositionBusy = false;
      renderDispositionPanel(groupById(state.selectedGroupId), prByNumber(state.selectedPr));
      const slot = $("prDecisionError");
      if (error.status === 409) {
        if (slot) slot.textContent = "Nothing was saved: the revision or snapshot changed. Reload and decide again.";
        setStatus("per-PR disposition not saved: revision or snapshot changed");
        await loadState();
      } else {
        if (slot) slot.textContent = "The PR decision save could not be confirmed: " + error.message;
        setStatus("per-PR disposition error: " + error.message);
      }
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
      const pane = document.querySelector(".review-scroll") || document.querySelector(".pane.center");
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
      if (selectedGroupPr) {
        allNums = [selectedGroupPr];
        const comparison = opts && Object.prototype.hasOwnProperty.call(opts, "comparePr")
          ? opts.comparePr : state.fileComparePr;
        if (groupSet.has(comparison) && comparison !== selectedGroupPr) allNums.push(comparison);
        nums = allNums;
      }
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
    const focused = g && state.selectedPr && (g.pr_numbers || []).includes(state.selectedPr);
    const target = g && !focused
      ? "&group_id=" + encodeURIComponent(g.group_id)
      : "&prs=" + allNums.join(",");
    const url = "/api/patches?repo=" + encodeURIComponent(repo) + target +
      "&path=" + encodeURIComponent(path) + "&page=" + page + "&page_size=" + pageSize +
      "&patch_offset=0&patch_limit=6000";
    let data;
    try {
      data = typeof requestOnce === "function"
        ? await requestOnce("patch", snapshotKey() + "|" +
            (g && !focused ? g.group_id : allNums.join(",")) + "|" + path + "|" + page, url)
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
      meta.textContent = "Reviewing #" + state.selectedPr + " · " + path +
        (items.length > 1 ? " · " + (comparison.same_complete_patch == null ? "comparison incomplete" :
          comparison.same_complete_patch ? "same complete patch" : "different patches") : "");
    }
    items.sort((a, b) => Number(b.number === state.selectedPr) - Number(a.number === state.selectedPr));
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
        const details = document.createElement("details");
        details.className = "patch-identity";
        const summary = document.createElement("summary");
        summary.textContent = "Patch identity";
        const digest = document.createElement("div");
        digest.className = "revision-id";
        digest.textContent = "full patch sha256 " + it.content_sha256;
        details.append(summary, digest);
        panel.appendChild(details);
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

  async function openTarget(target, ifContext) {
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
      if (!["group_id", "pr", "path", "panel"].includes(key)) {
        return actionError("invalid_request", "unknown target field: " + key, false);
      }
    }
    const panel = Object.prototype.hasOwnProperty.call(value, "panel") ? value.panel : null;
    if (panel !== null && !["diff", "file_review", "agent_file_findings"].includes(panel)) {
      return actionError("invalid_request", "panel is not a supported review panel.", false);
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
      if (!pathBelongs) {
        // Agent targets must be verified before changing the decision target.
        // A capped summary is not permission to accept an arbitrary path.
        const candidates = (pr ? [pr] : (group.pr_numbers || []).map(prByNumber))
          .filter((member) => member && (member.paths_truncated || Number(member.path_count) > (member.paths || []).length));
        let found = false;
        const revision = state.viewRevision;
        const scope = manifestScope();
        try {
          for (const member of candidates) {
            let page = 1;
            while (page) {
              const response = await readTool("get_pr", { repo: state.repo, pr: member.number,
                file_page: page, file_page_size: 80, expected_store_version: state.storeVersion,
                expected_snapshot_version: state.snapshotVersion });
              if (revision !== state.viewRevision || scope !== manifestScope()) {
                return actionError("stale_context", "View changed while checking this path.", true);
              }
              if (!response.ok) return actionError("not_found", "Could not verify this path.", true);
              found = (response.data.files || []).some((file) => file.path === path || file.previous_path === path);
              if (found) break;
              page = response.data.next_file_page;
            }
            if (found) break;
          }
        } catch (error) {
          return actionError("not_found", error.message || "Could not verify this path.", true);
        }
        if (!found) return actionError("invalid_request", "path does not belong to the selected PR.", false);
      }
    }
    const changed = changeView(() => {
      state.selectedGroupId = groupId;
      state.selectedPr = pr ? pr.number : null;
      state.selectedFile = path;
      state.selectedUser = null;
    });
    if (path) state.fileQueue = { path, prs: [], loading: true, same_patch: [] };
    if (pr) markAgentOpened(pr.number);
    setMobilePane("review");
    render();
    // Navigation may open a review panel, but opening one is never a review,
    // an adoption, or a decision.
    if (panel === "file_review" && $("fileFindingsBlock")) {
      $("fileFindingsBlock").open = true;
    }
    if (panel === "agent_file_findings" && $("fileDraftsBlock")) {
      $("fileDraftsBlock").open = true;
      setContextSidebar(true);
    }
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
      setContextSidebar(true);
      setMobilePane("context");
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
      "read_patch", "compare_prs", "find_related", "get_history", "get_file_review",
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

  // Draft-only agent write. There is deliberately no browser seam for marking
  // a file reviewed, adopting a drafted finding, or saving a PR decision:
  // those stay behind explicit human controls in the dashboard.
  async function draftFileReview(payload, ifContext) {
    if (!stateInitialized) {
      return actionError("not_ready", "the current repository snapshot is still loading.", true);
    }
    const input = actionInput(payload, ifContext, "draft");
    const guard = checkViewContext(input.context);
    if (!guard.ok) return guard;
    const value = input.value;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return actionError("invalid_request", "draft must be an object.", false);
    }
    for (const key of Object.keys(value)) {
      if (!["pr", "findings", "coverage", "provenance", "idempotency_key"].includes(key)) {
        return actionError("invalid_request", "unknown draft field: " + key, false);
      }
    }
    if (!Number.isSafeInteger(value.pr) || value.pr <= 0) {
      return actionError("invalid_request", "pr must be a positive integer.", false);
    }
    const pr = prByNumber(value.pr);
    if (!pr) return actionError("not_found", "PR is not in the current snapshot.", false);
    for (const key of ["findings", "coverage"]) {
      if (value[key] !== undefined && !Array.isArray(value[key])) {
        return actionError("invalid_request", key + " must be an array.", false);
      }
    }
    if (value.provenance !== undefined &&
        (!value.provenance || typeof value.provenance !== "object" ||
         Array.isArray(value.provenance))) {
      return actionError("invalid_request", "provenance must be an object.", false);
    }
    const key = value.idempotency_key;
    if (key !== undefined &&
        (typeof key !== "string" || key.length < 8 || key.length > 128)) {
      return actionError("invalid_request", "idempotency_key must be 8-128 characters.", false);
    }
    const expected = guard.context;
    const workspaceToken = workspaceGen;
    const workspaceRepo = state.repo;
    try {
      const evidence = await loadPrBody(value.pr);
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      const recheck = checkViewContext(input.context);
      if (!recheck.ok) return recheck;
      const data = await api("/api/file-reviews/draft", {
        method: "POST",
        body: JSON.stringify({
          repo: state.repo,
          pr: value.pr,
          revision: revisionRefFromPr(pr, evidence),
          findings: value.findings || [],
          coverage: value.coverage || [],
          provenance: value.provenance || {},
          expected_store_version: expected.store_version,
          expected_snapshot_version: expected.snapshot_version,
          idempotency_key: key || makeIdempotencyKey("file-draft"),
          actor: "agent",
        }),
      });
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      // A draft changes no review or decision, but it does advance the store
      // version; refresh so later guarded calls see the current context.
      await loadState();
      if (state.selectedPr === value.pr && state.selectedFile) {
        await loadFileReview({
          reset: true,
          drafts: !!($("fileDraftsBlock") && $("fileDraftsBlock").open),
        });
      }
      setStatus("an agent drafted file findings for #" + value.pr +
        " · nothing was reviewed or decided");
      return { ok: true, draft: data.draft || null, context: currentViewContext() };
    } catch (error) {
      if (!workspaceCurrent(workspaceRepo, workspaceToken)) return staleViewError();
      if (error.data && typeof error.data === "object" && error.data.ok === false) {
        return error.data;
      }
      return actionError(
        error.status === 409 ? "stale_snapshot" : "invalid_request",
        error.message || "the draft could not be saved.",
        error.status === 409,
      );
    }
  }

  function exposeTriageApp() {
    if (seamExposed) return;
    seamExposed = true;
    // Stable, intentionally narrow browser seam. Do not add api/session or
    // mutation helpers here: WebMCP may only read through readTool and may
    // change the current display through the guarded view actions.  The one
    // write is draftFileReview, which can only create an agent draft: it
    // cannot mark a file reviewed, adopt a draft, or decide a pull request.
    window.TriageApp = Object.freeze({
      getViewContext: () => currentViewContext(),
      setFilters,
      openTarget,
      showProposal,
      readTool,
      draftFileReview,
    });
    if (typeof window.dispatchEvent === "function" && typeof window.CustomEvent === "function") {
      window.dispatchEvent(new window.CustomEvent("triage:ready", {
        detail: {
          capabilities: ["read_tool", "get_view_context", "set_filters", "open_target",
            "show_proposal", "propose_file_review"],
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

  function startupWorkspaceTarget(discovery) {
    if (urlRepoExplicit) {
      return validWorkspaceRepo(state.repoDraft) ? canonicalWorkspaceRepo(state.repoDraft) : "";
    }
    const defaultRepo = canonicalWorkspaceRepo(state.defaultRepo);
    // A fixed --store server has one implicit binding, so its discovered
    // active repository remains compatible with the legacy startup flow.
    if ((!discovery || discovery.mode === "single") && defaultRepo) return defaultRepo;
    // In multi mode the backend's default is only a hint. It is a startup
    // target when that exact repository is actually discovered, never merely
    // because the server advertises omacom/omarchy as its fallback.
    return defaultRepo && serverWorkspaceRepos().includes(defaultRepo) ? defaultRepo : "";
  }

  function showWorkspaceSetup() {
    state.repo = "";
    state.repoDraft = "";
    state.source = "";
    state.sourceConfig = DEFAULT_WORKSPACE_SOURCE;
    state.limitConfig = 0;
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
    state.workspaceInitialized = false;
    state.workspaceSwitching = false;
    stateInitialized = false;
    renderWorkspaceControls();
    renderDetail();
    setLoading(false);
    setStatus(hasSavedWorkspace()
      ? "Choose a saved workspace or create a new one."
      : "Create a workspace to begin.");
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
    // State reads can overlap (for example, a retry racing a stale load).
    // The newest non-Sync read owns the loading indicator; an older response
    // may never clear the newer owner's spinner.
    const ownsLoading = token === workspaceGen && !syncActive;
    if (ownsLoading) {
      stateLoadingOwner = gen;
      setLoading(true, "loading");
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
      if (ownsLoading) finishStateLoading(gen, token);
      return true;
    } catch (err) {
      if (gen !== stateLoadGen || token !== workspaceGen) return false;
      setStatus("error loading " + target + "; retry to load this workspace");
      if (ownsLoading) finishStateLoading(gen, token);
      return false;
    }
  }

  function formatProgress(p) {
    if (!p) return "fetching…";
    if (p.error) return safeSyncError(p, p.snapshot_available === true);
    const phase = p.phase || "";
    const done = p.done || 0;
    const total = p.total || 0;
    if (phase === "files" && total) return `files ${done}/${total}`;
    if (phase === "listing") return "listing pulls…";
    if (phase === "reconciliation" || phase === "reconciling") {
      return p.message || "reconciling revisions…";
    }
    if (phase === "grouping") return p.message || `grouping ${done} PRs`;
    if (phase === "building_queue" || phase === "build_queue") {
      return p.message || "building review queue…";
    }
    if (phase === "saving") return p.message || "saving triage snapshot…";
    if (phase === "loading") return p.message || "loading saved triage state…";
    if (phase === "rendering") return p.message || "rendering triage state…";
    if (phase === "error") return safeSyncError(p, p.snapshot_available === true);
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
            finish(reject, new Error(p.error));
            return;
          }
          if (p.ready && !p.running) {
            finish(resolve, p);
            return;
          }
          if (!p.running && !p.ready && p.phase === "error") {
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
          finish(reject, err);
        }
      };
      tick();
    });
  }

  async function doFetch(options) {
    if (syncActive) {
      setStatus("Sync is already running");
      return;
    }
    // Sync always targets the committed active workspace. The dialog's repo,
    // source and cap fields are drafts and must not retarget or reconfigure a
    // running workspace until Save completes.
    const requestedRaw = state.repo || "";
    const requestedRepo = canonicalWorkspaceRepo(requestedRaw);
    if (!requestedRepo) {
      setStatus("Open or create a workspace before Sync");
      return;
    }
    if (state.workspaceSwitching) {
      setStatus("workspace is still opening; Sync will remain explicit");
      return;
    }
    if (requestedRepo !== state.repo || !stateInitialized) {
      const opened = await switchWorkspace(requestedRepo);
      if (!opened || state.repo !== requestedRepo || state.workspaceSwitching) return;
    }
    const token = workspaceGen;
    stateLoadGen += 1;
    const source = normalizedSource(state.sourceConfig) || DEFAULT_WORKSPACE_SOURCE;
    const repo = state.repo;
    const limitVal = normalizedFileCap(state.limitConfig);
    if (limitVal === null) {
      setStatus("workspace file cap is invalid; open Settings to correct it");
      return;
    }
    const refresh = !!(options && options.forceRefresh) || !!$("refresh").checked;
    const priorSnapshot = hasUsableSnapshot();
    const owner = { repo, token };
    syncOwner = owner;
    syncActive = true;
    setLoading(true, "listing");
    setStatus(source === "fixtures" ? "starting fixtures…" : "starting gh fetch…");

    const loadFetchedState = async () => {
      if (!workspaceCurrent(repo, token)) return false;
      setLoading(true, "loading");
      setStatus("loading synced triage state…");
      const gen = ++stateLoadGen;
      const data = await api(scopedGet("/api/state", repo));
      if (gen !== stateLoadGen || !workspaceCurrent(repo, token)) return false;
      setLoading(true, "rendering");
      setStatus("rendering synced triage state…");
      applyState(data);
      if (gen !== stateLoadGen || !workspaceCurrent(repo, token)) return false;
      await waitForPaint();
      if (gen !== stateLoadGen || !workspaceCurrent(repo, token)) return false;
      setStatus(`${state.prs.length} PRs · ${state.groups.length} groups`);
      return true;
    };

    try {
      await api("/api/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, repo, limit: limitVal, refresh }),
      });
      if (!workspaceCurrent(repo, token)) return;
      await pollProgressUntilDone(repo, token);
      if (!workspaceCurrent(repo, token)) return;
      await loadFetchedState();
    } catch (err) {
      if (err.code === "stale_workspace" || !workspaceCurrent(repo, token)) return;
      if (err.status === 409 && err.data && err.data.code === "fetch_busy") {
        if (err.data.repo && err.data.repo !== repo) {
          setStatus("Sync is busy for another workspace; this workspace was not fetched");
          return;
        }
        setStatus(formatProgress(err.data) + " (already running)");
        try {
          await pollProgressUntilDone(repo, token);
          if (!workspaceCurrent(repo, token)) return;
          await loadFetchedState();
        } catch (e2) {
          if (e2.code !== "stale_workspace" && workspaceCurrent(repo, token)) {
            setStatus(safeSyncError(e2, priorSnapshot));
          }
        }
      } else if (err.status === 409) {
        setStatus(safeSyncError(err, priorSnapshot));
      } else {
        if (!priorSnapshot) {
          setStatus(safeSyncError(err, false));
        } else {
          // Preserve the visible prior state. A best-effort reload can repair
          // a view that was changed by another local action, but it may not
          // turn an empty/new workspace into a claimed cached snapshot.
          try {
            if (!workspaceCurrent(repo, token)) return;
            setLoading(true, "loading");
            const cached = await api(scopedGet("/api/state", repo));
            if (!workspaceCurrent(repo, token)) return;
            setLoading(true, "rendering");
            applyState(cached);
            if (!workspaceCurrent(repo, token)) return;
            await waitForPaint();
            if (!workspaceCurrent(repo, token)) return;
            setStatus("sync failed; showing the last usable cached snapshot");
          } catch (_) {
            if (workspaceCurrent(repo, token)) setStatus(safeSyncError(err, true));
          }
        }
      }
    } finally {
      const ownsSync = syncOwner === owner;
      if (ownsSync) {
        syncOwner = null;
        syncActive = false;
      }
      if (ownsSync && workspaceCurrent(repo, token)) {
        stateLoadingOwner = 0;
        setLoading(false);
      }
      if (fetchPollTimer && workspaceCurrent(repo, token)) {
        clearTimeout(fetchPollTimer);
        fetchPollTimer = null;
      }
    }
  }

  const workspaceChoices = $("workspaceChoices");
  if (workspaceChoices) workspaceChoices.addEventListener("change", async () => {
    const target = workspaceChoices.value;
    try {
      if (target) await switchWorkspace(target);
    } catch (error) {
      setStatus("Could not open workspace: " + error.message);
    } finally {
      // Restore the active selection if the user cancels unsaved-edit discard.
      // Loading another workspace never starts a GitHub Sync.
      renderWorkspaceControls();
    }
  });

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

  const workspaceDialog = $("workspaceDialog");
  function setMobilePane(pane) {
    if (pane === "list" && window.matchMedia("(max-width: 700px)").matches) setFilesPane(false);
    document.body.dataset.mobilePane = pane;
    document.querySelectorAll(".mobile-navigation [data-mobile-pane]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mobilePane === pane));
    });
  }
  setMobilePane("list");
  if ($("navigatorWorklistBtn")) $("navigatorWorklistBtn").onclick = () => setFilesPane(false);
  if ($("navigatorFilesBtn")) $("navigatorFilesBtn").onclick = openFilesNavigator;
  if ($("fileNavigatorClose")) $("fileNavigatorClose").onclick = () => $("fileNavigatorDialog").close();
  if ($("fileNavigatorDialog")) $("fileNavigatorDialog").addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // Search inputs otherwise consume Escape to clear the query before the
    // native dialog can close. Keep the user's query and use normal teardown.
    event.preventDefault();
    event.stopPropagation();
    $("fileNavigatorDialog").close();
  }, true);
  if ($("fileNavigatorDialog")) $("fileNavigatorDialog").addEventListener("close", () => {
    document.querySelector(".pane.left").appendChild($("fileNavigator"));
    setFilesPane(filesPaneActive);
    if (filesDialogFocus && filesDialogFocus.isConnected) filesDialogFocus.focus({ preventScroll: true });
    else if ($("reviewFiles")) {
      const open = $("reviewFiles").querySelector(".file-nav-open");
      if (open) open.focus({ preventScroll: true });
    }
    filesDialogFocus = null;
  });
  document.querySelectorAll(".mobile-navigation [data-mobile-pane]").forEach((button) => {
    button.addEventListener("click", () => setMobilePane(button.dataset.mobilePane));
  });
  const mobileWorkspaceToggle = $("mobileWorkspaceToggle");
  if (mobileWorkspaceToggle) mobileWorkspaceToggle.addEventListener("click", () => {
    if (!hasSavedWorkspace()) { openWorkspaceDialog("new"); return; }
    const open = document.body.classList.toggle("workspace-menu-open");
    mobileWorkspaceToggle.setAttribute("aria-expanded", String(open));
  });
  const contextToggle = $("contextToggle");
  let contextDrawerTrigger = null;
  function closeContextDrawer(restoreFocus = false) {
    $("detail").classList.remove("context-drawer-open");
    const trigger = contextDrawerTrigger;
    contextDrawerTrigger = null;
    document.querySelectorAll("[data-context-section]").forEach((button) => button.setAttribute("aria-expanded", "false"));
    if (restoreFocus && trigger?.isConnected) trigger.focus({ preventScroll: true });
  }
  function setContextSidebar(open, persist = false) {
    closeContextDrawer();
    document.documentElement.dataset.contextSidebar = open ? "shown" : "hidden";
    if (contextToggle) {
      contextToggle.setAttribute("aria-expanded", String(open));
      contextToggle.textContent = open ? "Hide sidebar" : "Show sidebar";
    }
    if (persist) {
      try { window.localStorage.setItem("triage.context-sidebar.v1", open ? "shown" : "hidden"); }
      catch (_) { /* The toggle still works for this session. */ }
    }
  }
  setContextSidebar(document.documentElement.dataset.contextSidebar === "shown");
  if (contextToggle) contextToggle.addEventListener("click", () => {
    setContextSidebar(document.documentElement.dataset.contextSidebar !== "shown", true);
  });
  document.querySelectorAll("[data-context-section]").forEach((button) => {
    button.setAttribute("aria-controls", "reviewContext");
    button.addEventListener("click", () => {
      const section = $(button.dataset.contextSection);
      if (!section) return;
      if (contextDrawerTrigger === button) { closeContextDrawer(true); return; }
      closeContextDrawer();
      contextDrawerTrigger = button;
      $("detail").classList.add("context-drawer-open");
      button.setAttribute("aria-expanded", "true");
      $("contextDrawerTitle").textContent = button.getAttribute("aria-label") || "Context";
      if (section.tagName === "DETAILS") section.open = true;
      const panel = $("reviewContext");
      panel.scrollTop += section.getBoundingClientRect().top - panel.getBoundingClientRect().top - 60;
      const target = section.querySelector("summary, button, a") || section;
      if (target === section) section.tabIndex = -1;
      target.focus({ preventScroll: true });
    });
  });
  $("contextDrawerClose").addEventListener("click", () => closeContextDrawer(true));
  document.addEventListener("pointerdown", (event) => {
    if (contextDrawerTrigger && !event.target.closest("#reviewContext, .context-rail")) closeContextDrawer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !contextDrawerTrigger || document.querySelector("dialog[open]")) return;
    event.preventDefault();
    event.stopPropagation();
    closeContextDrawer(true);
  }, true);
  const newWorkspaceButton = $("newWorkspaceBtn");
  const settingsWorkspaceButton = $("settingsWorkspaceBtn");
  const workspaceCancelButton = $("workspaceCancelBtn");
  const workspaceForm = $("workspaceForm");
  if (newWorkspaceButton) newWorkspaceButton.addEventListener("click", () => openWorkspaceDialog("new"));
  if (settingsWorkspaceButton) settingsWorkspaceButton.addEventListener("click", () => openWorkspaceDialog("settings"));
  if (workspaceCancelButton) workspaceCancelButton.addEventListener("click", closeWorkspaceDialog);
  if (workspaceForm) workspaceForm.addEventListener("submit", saveWorkspaceDialog);
  if (workspaceDialog) workspaceDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!workspaceDialogSaving) closeWorkspaceDialog();
  });
  if (workspaceDialog) workspaceDialog.addEventListener("close", () => {
    if (!workspaceDialogSaving) restoreWorkspaceDialogFocus();
  });

  $("fetchBtn").addEventListener("click", doFetch);
  const prDecisionButton = $("prDecisionBtn");
  if (prDecisionButton) prDecisionButton.addEventListener("click", openPrDecisionDialog);
  const prDecisionForm = $("prDecisionForm");
  if (prDecisionForm) prDecisionForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSelectedDisposition();
  });
  const prDecisionDialog = $("prDecisionDialog");
  if (prDecisionDialog) {
    prDecisionDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closePrDecisionDialog(true);
    });
    prDecisionDialog.addEventListener("close", () => {
      const trigger = prDecisionReturnFocus;
      prDecisionReturnFocus = null;
      if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
    });
  }
  const findingForm = $("findingForm");
  if (findingForm) findingForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitFinding();
  });
  const findingCancel = $("findingCancelBtn");
  if (findingCancel) findingCancel.addEventListener("click", closeFindingDialog);
  const findingDialog = $("findingDialog");
  if (findingDialog) {
    findingDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeFindingDialog();
    });
    findingDialog.addEventListener("close", () => {
      findingTarget = null;
      const trigger = findingReturnFocus;
      findingReturnFocus = null;
      if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
    });
  }
  const bulkReviewButton = $("bulkReviewBtn");
  if (bulkReviewButton) bulkReviewButton.addEventListener("click", openBulkReviewDialog);
  const bulkReviewForm = $("bulkReviewForm");
  if (bulkReviewForm) bulkReviewForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveBulkReview();
  });
  const bulkReviewCancel = $("bulkReviewCancelBtn");
  if (bulkReviewCancel) bulkReviewCancel.addEventListener("click", () => {
    const dialog = $("bulkReviewDialog");
    if (dialog && dialog.open) dialog.close();
  });
  const bulkReviewDialog = $("bulkReviewDialog");
  if (bulkReviewDialog) {
    bulkReviewDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      if (!(state.bulkReview && state.bulkReview.busy)) bulkReviewDialog.close();
    });
    bulkReviewDialog.addEventListener("close", () => {
      state.bulkReview = null;
      const trigger = bulkReturnFocus;
      bulkReturnFocus = null;
      if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
    });
  }
  const bulkReviewDecision = $("bulkReviewDecision");
  if (bulkReviewDecision) bulkReviewDecision.addEventListener("change", () => {
    resetBulkConfirmation();
    renderBulkReview();
  });
  const bulkReviewConfirmBox = $("bulkReviewConfirm");
  if (bulkReviewConfirmBox) bulkReviewConfirmBox.addEventListener("change", renderBulkReview);
  const findingsDisclosure = $("fileFindingsBlock");
  if (findingsDisclosure) findingsDisclosure.addEventListener("toggle", () => {
    const entry = currentFileReview();
    if (findingsDisclosure.open && (!entry || !entry.findingsOpened)) {
      loadFileReview({ findings: true });
    } else {
      renderFileFindings();
    }
  });
  const findingsMore = $("fileFindingsMore");
  if (findingsMore) findingsMore.addEventListener("click", () => {
    const entry = currentFileReview();
    if (entry && entry.nextFindingPage) loadFileReview({ findings: true, appendFindings: true });
  });
  const draftsDisclosure = $("fileDraftsBlock");
  if (draftsDisclosure) draftsDisclosure.addEventListener("toggle", () => {
    const entry = currentFileReview();
    if (draftsDisclosure.open && (!entry || !entry.draftsOpened)) {
      loadFileReview({ drafts: true });
    } else {
      renderAgentFileDrafts();
    }
  });
  const draftsMore = $("fileDraftsMore");
  if (draftsMore) draftsMore.addEventListener("click", () => {
    const entry = currentFileReview();
    if (entry && entry.nextDraftPage) loadFileReview({ drafts: true, appendDrafts: true });
  });
  $("enrichBtn").addEventListener("click", doEnrich);
  $("tabQueue").addEventListener("click", () => setLeftTab("queue"));
  $("tabGroups").addEventListener("click", () => setLeftTab("groups"));
  $("tabAllPrs").addEventListener("click", () => setLeftTab("allprs"));
  const userClose = $("userDrawerClose");
  if (userClose) userClose.addEventListener("click", closeUserDrawer);
  const relatedBlock = $("relatedBlock");
  const overlapDisclosure = $("overlapBlock");
  if (overlapDisclosure) overlapDisclosure.addEventListener("toggle", () => {
    const group = groupById(state.selectedGroupId);
    if (overlapDisclosure.open && group) renderOverlap(group);
  });
  const bodyDisclosure = $("prBodyBlock");
  if (bodyDisclosure) bodyDisclosure.addEventListener("toggle", () => {
    renderPrBodies(groupById(state.selectedGroupId) || {});
  });
  const fileQueueDisclosure = $("fileQueueBlock");
  if (fileQueueDisclosure) fileQueueDisclosure.addEventListener("toggle", () => {
    if (fileQueueDisclosure.open && state.selectedFile &&
        (!state.fileQueue || state.fileQueue.path !== state.selectedFile)) {
      openFileQueue(state.selectedFile, { scroll: false });
    }
  });
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
    if (e.key === "Escape" && workspaceDialog && workspaceDialog.open) return;
    if (e.key === "Escape" && state.selectedUser) closeUserDrawer();
  });
  const centerPane = document.querySelector(".review-scroll") || document.querySelector(".pane.center");
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
    const requestedRepo = requested.get("repo") ||
      (canonicalWorkspaceRepo(state.defaultRepo) &&
       savedWorkspaceRepos().includes(canonicalWorkspaceRepo(state.defaultRepo))
        ? canonicalWorkspaceRepo(state.defaultRepo) : "");
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
      syncActive = false;
      syncOwner = null;
      stateLoadingOwner = 0;
      if (cancelFetchPoll) cancelFetchPoll();
      else if (fetchPollTimer) clearTimeout(fetchPollTimer);
      fetchPollTimer = null;
      cancelFetchPoll = null;
      fetchPollOwner = null;
      state.fetching = false;
      state.workspaceSwitching = false;
      setLoading(false);
      persistResume();
      invalidateSnapshotCaches();
      state.repo = "";
      state.viewRevision = Number.isSafeInteger(state.viewRevision)
        ? state.viewRevision + 1 : 1;
      bootstrapSession(true).then(async () => {
        const discovery = await loadWorkspaces({ token });
        if (token !== workspaceGen) return;
        readUrl();
        const target = startupWorkspaceTarget(discovery);
        if (target) {
          state.repoDraft = target;
          if ($("repo")) $("repo").value = target;
          await loadState(target, { token, unscoped: !discovery });
        } else {
          showWorkspaceSetup();
        }
      }).catch((error) => setStatus("session error: " + error.message));
    }
  });

  loadWorkspaceProfiles();
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
    const target = startupWorkspaceTarget(discovery);
    if (!target) {
      showWorkspaceSetup();
      return;
    }
    state.repoDraft = target;
    if ($("repo")) $("repo").value = target;
    await loadState(target, { token: workspaceGen, unscoped: !discovery });
  }).catch((error) => setStatus("session error: " + error.message));
})();
