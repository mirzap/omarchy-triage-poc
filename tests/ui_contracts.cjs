"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

function section(startNeedle, endNeedle) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert(start >= 0 && end > start, `missing section ${startNeedle}`);
  return source.slice(start, end);
}

const context = {
  escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;");
  },
};
const relatedHelpers = section(
  "  function relatedTargetIdentity(",
  "\n  function cancelEnrichmentUnless("
);
const colorizer = section("  function colorizeDiff(", "\n  function cancelPendingDiffScroll(");
const patchHelpers = section("  function mergePatchChunk(", "\n  function takeWithSelected(");
const selectionHelper = section("  function takeWithSelected(", "\n  function escapeHtml(");
vm.runInNewContext(
  `${relatedHelpers}\n${colorizer}\n${patchHelpers}\n${selectionHelper}\n` +
  "globalThis.relatedTargetIdentity = relatedTargetIdentity;" +
  "globalThis.requestContextMatches = requestContextMatches;" +
  "globalThis.colorizeDiff = colorizeDiff;" +
  "globalThis.mergePatchChunk = mergePatchChunk;" +
  "globalThis.filePatchTargets = filePatchTargets;" +
  "globalThis.takeWithSelected = takeWithSelected;",
  context
);

const target = context.relatedTargetIdentity("repo|4|9", 52, "src/a.js");
assert.strictEqual(target, "repo|4|9|52|src/a.js");
assert.strictEqual(context.requestContextMatches("s", 2, "a", "s", 2, "a"), true);
assert.strictEqual(context.requestContextMatches("s", 2, "a", "s", 3, "b"), false);

assert.deepStrictEqual(
  Array.from(context.filePatchTargets([1, 2, 3], 99)),
  [99, 1, 2, 3]
);
const merged = context.mergePatchChunk("abc", 3, {
  patch_offset: 3,
  patch: "def",
  next_patch_offset: 6,
});
assert.strictEqual(merged.raw, "abcdef");
assert.strictEqual(merged.nextOffset, 6);
assert.throws(
  () => context.mergePatchChunk(merged.raw, 6, {
    patch_offset: 3,
    patch: "def",
    next_patch_offset: 6,
  }),
  /offset/
);
const unicodeMerged = context.mergePatchChunk("a😀", 2, {
  patch_offset: 2,
  patch: "β😀",
  next_patch_offset: 4,
  patch_length: 6,
});
assert.strictEqual(unicodeMerged.raw, "a😀β😀");
assert.strictEqual(unicodeMerged.nextOffset, 4);
assert.throws(
  () => context.mergePatchChunk("a😀", 3, {
    patch_offset: 3,
    patch: "x",
    next_patch_offset: 4,
  }),
  /offset/
);

const colored = context.colorizeDiff("@@ -1 +1 @@\n--- old value\n+++ new value");
assert.match(colored, /diff-line del/);
assert.match(colored, /diff-line add/);
assert.doesNotMatch(colored, /diff-line meta/);

const groupSorters = section("  function compareGroups(", "\n  function sortedAllPrs(");
const pileItemsSource = section("  function pileItems(", "\n  function pileTotal(");
const queueRowsSource = section("  function queueRows(", "\n  function buildQueueItem(");
const sourceGroups = [
  { group_id: "G003", pr_numbers: [3], suggested_decision: "unique", keep: true },
  { group_id: "G002", pr_numbers: [4, 5, 6], suggested_decision: "related-theme", keep: true },
  { group_id: "G010", pr_numbers: [7, 8, 9], suggested_decision: "duplicate", keep: true },
  { group_id: "G001", pr_numbers: [10, 11, 12], suggested_decision: "related-theme", keep: true },
  { group_id: "G099", pr_numbers: [13, 14, 15, 16], suggested_decision: "duplicate", keep: false },
];
const backendPile = ["G003", "G002", "G010", "G001", "G099"];
const sortContext = {
  DECISION_ORDER: { duplicate: 0, "related-theme": 1, unique: 2 },
  state: {
    groups: sourceGroups,
    queue: { needs_you: backendPile },
    queuePile: "needs_you",
    filterQuery: "",
    filterLabel: "",
  },
  groupById(id) { return sourceGroups.find((group) => group.group_id === id) || null; },
  groupMatches(group) { return group.keep; },
};
vm.runInNewContext(
  `const DECISION_ORDER = globalThis.DECISION_ORDER; let queueRowsCache = null;\n` +
  `${groupSorters}\n${pileItemsSource}\n${queueRowsSource}\n` +
  "globalThis.sortedGroups = sortedGroups; globalThis.pileItems = pileItems; " +
  "globalThis.queueRows = queueRows;",
  sortContext
);
const expectedGroupOrder = ["G010", "G001", "G002", "G003"];
assert.deepStrictEqual(
  Array.from(sortContext.sortedGroups()).filter((group) => group.keep).map((group) => group.group_id),
  expectedGroupOrder
);
assert.deepStrictEqual(
  Array.from(sortContext.pileItems("needs_you")).map((group) => group.group_id),
  expectedGroupOrder
);
assert.deepStrictEqual(
  Array.from(sortContext.queueRows()).map((row) => row.group.group_id),
  expectedGroupOrder
);
assert.deepStrictEqual(backendPile, ["G003", "G002", "G010", "G001", "G099"]);
assert.deepStrictEqual(
  sourceGroups.map((group) => group.group_id),
  ["G003", "G002", "G010", "G001", "G099"]
);

const scrollHelpers = section(
  "  function cancelPendingDiffScroll(",
  "\n  async function renderDiffs("
);
const scheduledFrames = new Map();
const scrollCalls = [];
let nextFrame = 1;
const centerPane = {
  scrollTop: 10,
  getBoundingClientRect() { return { top: 0 }; },
  scrollTo(options) { scrollCalls.push(options); },
};
const diffBlock = { getBoundingClientRect() { return { top: 100 }; } };
const scrollContext = {
  diffGen: 7,
  state: { selectedFile: "src/a.js", selectedGroupId: "G001", selectedPr: 12 },
  snapshotKey() { return "acme/widgets|7|9"; },
  requestAnimationFrame(callback) {
    const id = nextFrame++;
    scheduledFrames.set(id, callback);
    return id;
  },
  cancelAnimationFrame(id) { scheduledFrames.delete(id); },
  document: { querySelector(selector) { return selector === ".pane.center" ? centerPane : null; } },
  $(id) { return id === "diffBlock" ? diffBlock : null; },
};
vm.runInNewContext(
  "let diffScrollSerial = 0; let pendingDiffScroll = null; " +
  "let pendingDiffScrollFrame = null; let diffGen = globalThis.diffGen;\n" +
  `${scrollHelpers}\n` +
  "globalThis.beginDiffScroll = beginDiffScroll; " +
  "globalThis.scheduleDiffScroll = scheduleDiffScroll; " +
  "globalThis.cancelPendingDiffScroll = cancelPendingDiffScroll; " +
  "globalThis.cancelDiffScrollForUserIntent = cancelDiffScrollForUserIntent;",
  scrollContext
);

function flushFrames() {
  const callbacks = [...scheduledFrames.values()];
  scheduledFrames.clear();
  callbacks.forEach((callback) => callback());
}

const oneJump = scrollContext.beginDiffScroll("src/a.js");
scrollContext.scheduleDiffScroll(oneJump, 7);
scrollContext.scheduleDiffScroll(oneJump, 7);
assert.strictEqual(scheduledFrames.size, 1, "accepted renders must coalesce to one frame");
flushFrames();
assert.strictEqual(scrollCalls.length, 1);
assert.strictEqual(scrollCalls[0].top, 102);
assert.strictEqual(scrollCalls[0].behavior, "auto");
scrollContext.scheduleDiffScroll(oneJump, 7);
assert.strictEqual(scheduledFrames.size, 0, "a consumed selection must not jump again");

const manual = scrollContext.beginDiffScroll("src/a.js");
scrollContext.scheduleDiffScroll(manual, 7);
scrollContext.cancelDiffScrollForUserIntent({ type: "wheel" });
assert.strictEqual(scheduledFrames.size, 0, "wheel intent must cancel the pending jump");

const keyIntent = scrollContext.beginDiffScroll("src/a.js");
scrollContext.scheduleDiffScroll(keyIntent, 7);
scrollContext.cancelDiffScrollForUserIntent({ type: "keydown", key: "PageDown" });
assert.strictEqual(scheduledFrames.size, 0, "scroll-key intent must cancel the pending jump");

const stale = scrollContext.beginDiffScroll("src/a.js");
scrollContext.scheduleDiffScroll(stale, 7);
scrollContext.state.selectedFile = "src/b.js";
flushFrames();
assert.strictEqual(scrollCalls.length, 1, "a stale selection must not jump");

const renderMembersSource = section(
  "  function renderMembers(",
  "\n  function paintMembers("
);
const memberWrap = {
  classList: {
    values: new Set(),
    contains(name) { return this.values.has(name); },
    toggle(name, enabled) {
      if (enabled) this.values.add(name);
      else this.values.delete(name);
    },
  },
  style: {},
  scrollTop: 700,
};
const memberList = {
  style: {},
  children: [],
  set innerHTML(_value) { this.children = []; },
  appendChild(child) { this.children.push(child); return child; },
};
const memberContext = {
  Virtualizer: {},
  $(id) { return id === "detailMembersWrap" ? memberWrap : memberList; },
  disposeVirtualizer() {},
  buildMemberLi(number) { return { number }; },
  makeVirtualizer() { return { _willUpdate() {} }; },
  mountVirtualizer(_slot, virtualizer) { return virtualizer; },
  paintMembers() {},
};
vm.runInNewContext(
  "let memberVirtualizer = null; const Virtualizer = globalThis.Virtualizer;\n" +
  `${renderMembersSource}\n` +
  "globalThis.renderMembers = renderMembers;",
  memberContext
);
memberContext.renderMembers({ pr_numbers: Array.from({ length: 50 }, (_, index) => index + 1) });
assert.strictEqual(memberList.style.height, "1800px");
memberContext.renderMembers({ pr_numbers: [1, 2] });
assert.strictEqual(memberWrap.style.maxHeight, "");
assert.strictEqual(memberWrap.style.overflow, "");
assert.strictEqual(memberWrap.scrollTop, 0);
assert.strictEqual(memberList.style.position, "");
assert.strictEqual(memberList.style.height, "");
assert.strictEqual(memberList.children.length, 2);

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.className = "";
    this.children = [];
    this.listeners = {};
    this._innerHTML = "";
    this.textContent = "";
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  prepend(child) { this.children.unshift(child); }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  querySelector(selector) {
    if (selector === ".diff-panel") {
      return this.children.find((child) => child.className === "diff-panel") || null;
    }
    if (selector === ".patch-more") {
      return this.children.find((child) => child.className === "btn patch-more") || null;
    }
    return null;
  }
}

const renderDiffsSource = section(
  "  async function renderDiffs(",
  "\n  async function loadNextPatchChunk("
);
const openFileQueueSource = section(
  "  async function openFileQueue(",
  "\n  function renderFileQueue("
);
const diffRoot = new FakeElement("div");
const diffMeta = new FakeElement("div");
const responseItem = {
  number: 12,
  path: "config/example.conf",
  patch: "@@ -1 +1 @@\n-old\n+new\n",
  evidence_complete: false,
  legacy_unverified: true,
  incomplete_reasons: ["unverified legacy cache preview"],
  patch_offset: 0,
  patch_length: 30,
  next_patch_offset: 25,
};
let requestedUrl = "";
Object.assign(context, {
  diffGen: 0,
  patchStates: new WeakMap(),
  state: {
    selectedFile: "config/example.conf",
    selectedPr: 12,
    fileQueue: null,
    repo: "acme/widgets",
  },
  $(id) { return id === "diffPanels" ? diffRoot : diffMeta; },
  document: { createElement(tagName) { return new FakeElement(tagName); } },
  prByNumber(number) { return { number, user: "fixture", paths: ["config/example.conf"] }; },
  prUrl(pr) { return `https://github.com/acme/widgets/pull/${pr.number}`; },
  snapshotKey() { return "acme/widgets|7|9"; },
  requestOnce: async (_kind, _identity, url) => {
    requestedUrl = url;
    return {
      items: [responseItem], page: 2, page_size: 8,
      total_pages: 2, total_items: 12,
      comparison: { same_complete_patch: null },
    };
  },
  scrollToDiff() {},
  bindUserLink() {},
  renderFileQueue() {},
});
vm.runInNewContext(
  `let diffGen = globalThis.diffGen; const patchPanelState = globalThis.patchStates;\n` +
  `${renderDiffsSource}\n` +
  "globalThis.renderDiffs = renderDiffs;",
  context
);

(async () => {
  // Manifest paging is independent of the capped UI path summary. A response
  // from an old snapshot must not populate or repaint the replacement view.
  const manifestState = { repo: "acme/widgets", storeVersion: 7, snapshotVersion: 9,
    selectedPr: 12, selectedGroupId: "G001" };
  const manifestRequests = [];
  let manifestPaints = 0;
  const manifestContext = { state: manifestState, $() { return null; },
    validRepoPath(path) { return typeof path === "string"; },
    renderFileNavigator() { manifestPaints++; },
    readTool(tool, args) { return new Promise(resolve => manifestRequests.push({ tool, args, resolve })); } };
  vm.runInNewContext(section("  const fileManifests =", "\n  function manifestCount(") +
    "\nglobalThis.currentManifest = currentManifest; globalThis.loadManifestPage = loadManifestPage; " +
    "globalThis.prMayContainPath = prMayContainPath;", manifestContext);
  const manifest = manifestContext.currentManifest();
  const firstPage = manifestContext.loadManifestPage(manifest);
  assert.strictEqual(manifestRequests[0].args.expected_store_version, 7);
  assert.strictEqual(manifestRequests[0].args.expected_snapshot_version, 9);
  manifestRequests[0].resolve({ ok: true, context: { repo: "acme/widgets", store_version: 7, snapshot_version: 9 },
    data: { files: [{ path: "src/beyond-500.ts" }], file_count: 610, next_file_page: 2 } });
  await firstPage;
  assert.strictEqual(manifestContext.prMayContainPath({ number: 12, paths: [] }, "src/beyond-500.ts"), true);
  const nextPage = manifestContext.loadManifestPage(manifest);
  assert.strictEqual(manifestRequests[1].args.file_page, 2);
  manifestState.snapshotVersion = 10;
  const replacement = manifestContext.currentManifest();
  const paintsBeforeStale = manifestPaints;
  manifestRequests[1].resolve({ ok: true, context: { repo: "acme/widgets", store_version: 7, snapshot_version: 9 },
    data: { files: [{ path: "src/stale.ts" }], file_count: 610, next_file_page: null } });
  await nextPage;
  assert.strictEqual(replacement.rows.length, 0);
  assert.strictEqual(manifestPaints, paintsBeforeStale);
  replacement.next = null;
  assert.strictEqual(manifestContext.prMayContainPath({ number: 12, paths: [], paths_truncated: true }, "src/absent.ts"), false);

  const group = { group_id: "G001", pr_numbers: Array.from({ length: 12 }, (_, i) => i + 1) };
  await context.renderDiffs(group);
  // The workbench requests the selected revision directly, including members
  // beyond the old eight-member page, and compares only on explicit request.
  assert.match(requestedUrl, /prs=12&/);
  assert.match(requestedUrl, /page=1&/);
  await context.renderDiffs(group, { comparePr: 3 });
  assert.match(requestedUrl, /prs=12,3&/);
  const panel = diffRoot.children.find((child) => child.className === "diff-panel");
  assert(panel, "nonempty patch response must append a diff panel");
  assert.match(panel.innerHTML, /\+new/);
  const panelState = context.patchStates.get(panel);
  assert.strictEqual(panelState.item, responseItem);
  assert.strictEqual(panelState.item.next_patch_offset, 25);
  const more = panel.querySelector(".patch-more");
  assert(more && typeof more.listeners.click === "function");

  const flowRoot = new FakeElement("div");
  const flowMeta = new FakeElement("div");
  const flowPane = {
    scrollTop: 10,
    getBoundingClientRect() { return { top: 0 }; },
    scrollTo(options) { flowScrollCalls.push(options); },
  };
  const flowBlock = { getBoundingClientRect() { return { top: 100 }; } };
  const flowFrames = new Map();
  const flowScrollCalls = [];
  const fileResolvers = [];
  let flowFrameId = 1;
  const flowGroup = { group_id: "G001", pr_numbers: [12] };
  const flowState = {
    selectedFile: null,
    selectedGroupId: "G001",
    selectedPr: 12,
    fileQueue: null,
    repo: "acme/widgets",
    groups: [flowGroup],
  };
  const flowContext = {
    state: flowState,
    snapshotKey() { return "acme/widgets|7|9"; },
    requestAnimationFrame(callback) {
      const id = flowFrameId++;
      flowFrames.set(id, callback);
      return id;
    },
    cancelAnimationFrame(id) { flowFrames.delete(id); },
    document: {
      querySelector(selector) { return selector === ".pane.center" ? flowPane : null; },
      createElement(tagName) { return new FakeElement(tagName); },
    },
    $(id) {
      if (id === "diffPanels") return flowRoot;
      if (id === "diffMeta") return flowMeta;
      if (id === "diffBlock") return flowBlock;
      return null;
    },
    requestOnce(kind) {
      if (kind === "file") {
        return new Promise((resolve) => fileResolvers.push(resolve));
      }
      return Promise.resolve({
        items: [{
          number: 12, path: "src/a.js", patch: "@@ -1 +1 @@\n-old\n+new\n",
          evidence_complete: true, next_patch_offset: null,
        }],
        page: 1, page_size: 8, total_pages: 1, total_items: 1,
        comparison: { same_complete_patch: null },
      });
    },
    isFileView() { return false; },
    writeUrl() {},
    renderFileDetail() {},
    renderFileQueue() {},
    prByNumber(number) { return { number, user: "fixture", paths: ["src/a.js"] }; },
    prUrl(pr) { return `https://github.com/acme/widgets/pull/${pr.number}`; },
    takeWithSelected: context.takeWithSelected,
    filePatchTargets: context.filePatchTargets,
    colorizeDiff: context.colorizeDiff,
    escapeHtml: context.escapeHtml,
    bindUserLink() {},
  };
  vm.runInNewContext(
    "let fileQueueGen = 0; let diffGen = 0; let diffScrollSerial = 0; " +
    "let pendingDiffScroll = null; let pendingDiffScrollFrame = null; " +
    "const patchPanelState = new WeakMap();\n" +
    `${scrollHelpers}\n${renderDiffsSource}\n${openFileQueueSource}\n` +
    "globalThis.openFileQueue = openFileQueue; " +
    "globalThis.cancelDiffScrollForUserIntent = cancelDiffScrollForUserIntent;",
    flowContext
  );
  function flushFlowFrames() {
    const callbacks = [...flowFrames.values()];
    flowFrames.clear();
    callbacks.forEach((callback) => callback());
  }

  const firstOpen = flowContext.openFileQueue("src/a.js");
  await new Promise((resolve) => setImmediate(resolve));
  flushFlowFrames();
  assert.strictEqual(flowScrollCalls.length, 1, "file selection must jump once after diff commit");
  fileResolvers.shift()({ path: "src/a.js", prs: [{ number: 12 }] });
  await firstOpen;
  await new Promise((resolve) => setImmediate(resolve));
  flushFlowFrames();
  assert.strictEqual(flowScrollCalls.length, 1, "late file data must not request another jump");

  const cancelledOpen = flowContext.openFileQueue("src/a.js");
  flowContext.cancelDiffScrollForUserIntent({ type: "wheel" });
  await new Promise((resolve) => setImmediate(resolve));
  flushFlowFrames();
  fileResolvers.shift()({ path: "src/a.js", prs: [{ number: 12 }] });
  await cancelledOpen;
  await new Promise((resolve) => setImmediate(resolve));
  flushFlowFrames();
  assert.strictEqual(flowScrollCalls.length, 1, "manual intent must suppress all delayed jumps");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

// WebMCP registration and draft cancellation contracts use a deliberately
// tiny mock of the current document.modelContext API. No browser automation or
// live backend is needed to verify the narrow app seam.
(async () => {
  const webmcpSource = fs.readFileSync(
    path.join(__dirname, "..", "triage", "web", "webmcp.js"), "utf8"
  );
  const readNames = [
    "get_workspace", "list_groups", "search_prs", "get_group", "get_pr",
    "read_patch", "compare_prs", "find_related", "get_history", "get_file_review",
    "list_proposals", "get_proposal", "get_file_review_history",
  ];
  const definitions = readNames.map((name) => ({
    name, inputSchema: { type: "object", properties: {}, additionalProperties: false },
  }));
  const registered = [];
  const statusNode = { textContent: "" };
  const modelContext = {
    registerTool(definition) {
      registered.push(definition);
      return Promise.resolve();
    },
  };
  let conflictContext = null;
  let draftCalls = [];
  let firstDraftResolve = null;
  let currentAppPr = 12;
  let currentAppGroup = "G001";
  const app = {
    getViewContext() { return { repo: "acme/widgets", store_version: 4, snapshot_version: 9, view_revision: 2 }; },
    readTool() { return Promise.resolve({ ok: true }); },
    setFilters(_filters, ifContext) {
      conflictContext = ifContext;
      return { ok: false, error: { code: "stale_view", retryable: true } };
    },
    openTarget() { return { ok: true }; },
    showProposal() { return { ok: true }; },
    draftProposal(payload) {
      if (currentAppGroup === null) assert.strictEqual(payload.group_id, "G001");
      return Promise.resolve({ ok: true, proposal: { proposal_id: "proposal-1" }, payload });
    },
    draftFileReview(payload) {
      if (currentAppPr === null) {
        assert.strictEqual(payload.pr, 12);
        assert.strictEqual(payload.revision.source, "fixtures");
        assert.strictEqual(payload.idempotency_key, "stable-draft-key-12");
      }
      draftCalls.push(payload);
      if (draftCalls.length === 1) {
        return new Promise((resolve) => { firstDraftResolve = resolve; });
      }
      return Promise.resolve({ ok: true, draft: { draft_id: "draft-1" }, context: { repo: "acme/widgets", store_version: 4, snapshot_version: 9, view_revision: 2 } });
    },
  };
  const listeners = {};
  const window = {
    TriageApp: app,
    crypto: { randomUUID() { return "unused"; } },
    addEventListener(type, listener) { listeners[type] = listener; },
    CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
  };
  const document = {
    modelContext,
    getElementById(id) { return id === "agentToolsStatus" ? statusNode : null; },
  };
  const webContext = {
    window, document, AbortController, fetch: async () => ({
      ok: true, json: async () => ({ tools: definitions }),
    }),
    console, setTimeout, clearTimeout,
  };
  vm.runInNewContext(webmcpSource, webContext);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepStrictEqual(
    registered.map((tool) => tool.name),
    readNames.concat(["get_view_context", "set_filters", "open_target", "show_proposal",
      "propose_triage", "propose_file_review"]),
    "all shared reads, view actions, and both draft actions must register"
  );
  assert.doesNotMatch(registered.map((tool) => tool.name).join(","),
    /accept|reject|adopt|reviewed|decision/i,
    "human-only mutators must remain absent"
  );
  const proposalTool = registered.find((tool) => tool.name === "propose_triage");
  const fileTool = registered.find((tool) => tool.name === "propose_file_review");
  assert.strictEqual(proposalTool.annotations.untrustedContentHint, true);
  assert.strictEqual(fileTool.annotations.untrustedContentHint, true);
  assert.match(proposalTool.description, /overall PR disposition recommendation/);
  assert.match(proposalTool.description, /never accepts, applies, or decides/);
  assert.match(fileTool.description, /Draft file findings/);
  assert.strictEqual(registered.find((tool) => tool.name === "show_proposal").annotations.untrustedContentHint, true);
  const fileDraftSchema = fileTool.inputSchema.properties.draft;
  assert.strictEqual(fileDraftSchema.properties.findings.minItems, 1);
  assert.strictEqual(fileDraftSchema.properties.coverage.minItems, 1);
  assert(fileDraftSchema.properties.revision.required.includes("source"));
  assert.strictEqual(fileDraftSchema.properties.revision.properties.source.minLength, 1);
  assert(fileDraftSchema.anyOf.some((branch) => branch.required.includes("findings")));
  assert(fileDraftSchema.anyOf.some((branch) => branch.required.includes("coverage")),
    "file drafts must contain non-empty findings or coverage");
  currentAppGroup = null;
  const proposal = await proposalTool.execute({
    draft: {
      group_id: "G001",
      items: [{ pr: 12, disposition: "keep", reason: "reviewed", revision: {
        head_sha: "", base_sha: "", content_digest: "digest-12", source: "fixtures",
      } }],
      idempotency_key: "stable-proposal-key-12",
    },
    if_context: app.getViewContext(),
  }, { signal: new AbortController().signal });
  assert.strictEqual(proposal.ok, true);
  assert.strictEqual(proposal.proposal.proposal_id, "proposal-1",
    "overall proposal drafts must return their proposal identity");

  const filtersTool = registered.find((tool) => tool.name === "set_filters");
  const stale = { repo: "acme/widgets", store_version: 3, snapshot_version: 9, view_revision: 1 };
  const conflict = await filtersTool.execute({ filters: { tab: "queue" }, if_context: stale }, {});
  assert.strictEqual(conflict.ok, false);
  assert.strictEqual(conflict.error.code, "stale_view");
  assert.strictEqual(conflictContext, stale, "view actions must pass their exact context to the app seam");

  const draftInput = {
    draft: {
      pr: 12,
      revision: { head_sha: "", base_sha: "", content_digest: "digest-12", source: "fixtures" },
      findings: [], coverage: [{ path: "src/a.js", status: "inspected" }],
      idempotency_key: "stable-draft-key-12",
    },
    if_context: app.getViewContext(),
  };
  // The app's mutable current group/PR may disappear between agent reads and
  // a same-key replay. The draft seam must still forward the original payload
  // so the backend can perform idempotency lookup first.
  currentAppPr = null;
  const aborter = new AbortController();
  const first = fileTool.execute(draftInput, { signal: aborter.signal });
  aborter.abort();
  firstDraftResolve({ ok: true, draft: { draft_id: "draft-1" }, context: app.getViewContext() });
  const cancelled = await first;
  assert.strictEqual(cancelled.ok, true);
  assert.strictEqual(cancelled.cancellation.code, "cancelled_after_commit");
  assert.strictEqual(cancelled.cancellation.recoverable, true);
  const retried = await fileTool.execute(draftInput, { signal: new AbortController().signal });
  assert.strictEqual(retried.ok, true);
  assert.strictEqual(draftCalls.length, 2);
  assert.strictEqual(draftCalls[0].idempotency_key, "stable-draft-key-12");
  assert.strictEqual(draftCalls[1].idempotency_key, "stable-draft-key-12",
    "a retry must reuse the caller key rather than generating a duplicate key");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
