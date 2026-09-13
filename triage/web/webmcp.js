/* Optional WebMCP adapter for the local triage dashboard. */
(function () {
  "use strict";

  // Keep this list closed.  The metadata endpoint is discovery only; it must
  // never be allowed to add arbitrary browser tools.
  const READ_TOOL_NAMES = [
    "get_workspace", "list_groups", "search_prs", "get_group", "get_pr",
    "read_patch", "compare_prs", "find_related", "get_history", "get_file_review",
    "list_proposals", "get_proposal", "get_file_review_history",
  ];
  const VIEW_TOOL_NAMES = [
    "get_view_context", "set_filters", "open_target", "show_proposal",
  ];
  // The only browser writes. They can create agent drafts and nothing else:
  // there is no tool here for marking a file reviewed, adopting a drafted
  // finding, or saving a pull-request decision.
  const DRAFT_TOOL_NAMES = ["propose_triage", "propose_file_review"];
  const TOOL_COUNT = READ_TOOL_NAMES.length + VIEW_TOOL_NAMES.length +
    DRAFT_TOOL_NAMES.length;

  // Descriptions are application-authored and deliberately do not include
  // titles, descriptions, patches, or other data returned by the backend.
  const READ_DESCRIPTIONS = Object.freeze({
    get_workspace: "Read the active local triage workspace summary.",
    list_groups: "List complete, paginated triage groups from the active snapshot.",
    search_prs: "Search complete, paginated pull requests in the active snapshot.",
    get_group: "Read a triage group and its revision-bound members.",
    get_pr: "Read one pull request and its revision-bound file manifest.",
    read_patch: "Read a bounded patch chunk for one pull request file.",
    compare_prs: "Compare bounded patch evidence for pull requests sharing a path.",
    find_related: "Read cached, advisory related-patch results for a pull request.",
    get_history: "Read revision-bound triage history from the local workspace.",
    get_file_review: "Read per-file human review state, separate agent inspection coverage, findings, and agent drafts for one pull-request revision. Manifest, findings, and drafts page independently.",
    list_proposals: "Discover overall PR recommendation drafts in the active workspace.",
    get_proposal: "Read one overall PR recommendation draft and its event history.",
    get_file_review_history: "Read append-only file-review events and their revision status.",
  });

  const FILTER_LABELS = [
    "unreviewed", "approved", "rejected", "hardware", "unique", "duplicate",
    "related-theme", "needs-look", "docs", "cosmetic", "update-path", "junk",
  ];
  const QUEUE_PILES = ["needs_you", "known", "junk", "hardware", "upgrade", "hotspots"];
  const REVISION_PROPERTIES = {
    repo: { type: "string", minLength: 1, maxLength: 256 },
    store_version: { type: "integer", minimum: 0 },
    snapshot_version: { type: "integer", minimum: 0 },
    view_revision: { type: "integer", minimum: 0 },
    group_snapshot_digest: { type: ["string", "null"], maxLength: 256 },
    pr_content_digest: { type: ["string", "null"], maxLength: 256 },
  };
  const DRAFT_REVISION_SCHEMA = {
    type: "object",
    properties: {
      pr_number: { type: "integer", minimum: 1 },
      head_sha: { type: "string", maxLength: 256 },
      base_sha: { type: "string", maxLength: 256 },
      additions: { type: ["integer", "null"], minimum: 0 },
      deletions: { type: ["integer", "null"], minimum: 0 },
      content_digest: { type: "string", minLength: 1, maxLength: 256 },
      evidence_complete: { type: "boolean", default: false },
      source: { type: "string", minLength: 1, maxLength: 64 },
      cache_snapshot_id: { type: "string", maxLength: 256, default: "" },
    },
    required: ["head_sha", "base_sha", "content_digest", "source"],
    additionalProperties: false,
  };
  const IF_CONTEXT_SCHEMA = {
    type: "object",
    properties: REVISION_PROPERTIES,
    required: ["repo", "store_version", "snapshot_version", "view_revision"],
    additionalProperties: false,
  };
  const VIEW_ANNOTATIONS = Object.freeze({
    readOnlyHint: false,
    consequentialHint: false,
    untrustedContentHint: true,
  });
  const READ_ANNOTATIONS = Object.freeze({
    readOnlyHint: true,
    consequentialHint: false,
    untrustedContentHint: true,
  });
  const CONTEXT_ANNOTATIONS = Object.freeze({
    readOnlyHint: true,
    consequentialHint: false,
    untrustedContentHint: true,
  });

  const FILTER_SCHEMA = {
    type: "object",
    properties: {
      tab: { type: "string", enum: ["queue", "groups", "allprs"] },
      q: { type: "string", maxLength: 256 },
      label: { type: "string", enum: FILTER_LABELS },
      pile: { type: "string", enum: QUEUE_PILES },
    },
    additionalProperties: false,
  };
  const TARGET_SCHEMA = {
    type: "object",
    properties: {
      group_id: { type: ["string", "null"], maxLength: 256 },
      pr: { type: ["integer", "null"], minimum: 1 },
      path: { type: ["string", "null"], maxLength: 1024 },
      panel: { type: ["string", "null"], enum: ["diff", "file_review", "agent_file_findings", null] },
    },
    additionalProperties: false,
  };
  const FINDING_SEVERITIES = ["blocker", "major", "minor", "nit", "question"];
  const COVERAGE_STATUSES = ["inspected", "skipped", "missing"];
  const DRAFT_SCHEMA = {
    type: "object",
    properties: {
      pr: { type: "integer", minimum: 1 },
      findings: {
        type: "array", minItems: 1, maxItems: 100,
        items: {
          type: "object", additionalProperties: false,
          properties: {
            path: { type: "string", maxLength: 1024 },
            severity: { type: "string", enum: FINDING_SEVERITIES },
            title: { type: "string", minLength: 1, maxLength: 200 },
            explanation: { type: "string", maxLength: 4000 },
            evidence: { type: "string", maxLength: 4000 },
            suggested_fix: { type: "string", maxLength: 4000 },
            line: { type: "integer", minimum: 1 },
            hunk: { type: "string", maxLength: 200 },
          },
          required: ["path", "severity", "title", "explanation"],
        },
      },
      coverage: {
        type: "array", minItems: 1, maxItems: 500,
        items: {
          type: "object", additionalProperties: false,
          properties: {
            path: { type: "string", maxLength: 1024 },
            status: { type: "string", enum: COVERAGE_STATUSES },
            note: { type: "string", maxLength: 1000 },
          },
          required: ["path", "status"],
        },
      },
      provenance: { type: "object" },
      revision: DRAFT_REVISION_SCHEMA,
      idempotency_key: { type: "string", minLength: 8, maxLength: 128 },
    },
    required: ["pr", "revision", "idempotency_key"],
    anyOf: [
      { required: ["findings"] },
      { required: ["coverage"] },
    ],
    additionalProperties: false,
  };
  const PROPOSAL_ITEM_SCHEMA = {
    type: "object", additionalProperties: false,
    properties: {
      pr: { type: "integer", minimum: 1 },
      disposition: { type: "string", enum: ["pending", "keep", "duplicate", "reject", "needs_hardware", "upgrade"] },
      reason: { type: "string", minLength: 1, maxLength: 1000 },
      revision: DRAFT_REVISION_SCHEMA,
      duplicate_of: { type: "integer", minimum: 1 },
      duplicate_of_revision: DRAFT_REVISION_SCHEMA,
    },
    required: ["pr", "disposition", "reason", "revision"],
  };
  const PROPOSAL_DRAFT_SCHEMA = {
    type: "object",
    properties: {
      group_id: { type: "string", minLength: 1, maxLength: 128, pattern: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$" },
      canonical_pr: { type: "integer", minimum: 1 },
      items: { type: "array", minItems: 1, maxItems: 200, items: PROPOSAL_ITEM_SCHEMA },
      provenance: { type: "object" },
      context: { type: "object" },
      idempotency_key: { type: "string", minLength: 8, maxLength: 128 },
    },
    required: ["group_id", "items", "idempotency_key"],
    additionalProperties: false,
  };
  const VIEW_ACTION_SCHEMAS = Object.freeze({
    get_view_context: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    set_filters: {
      type: "object",
      properties: { filters: FILTER_SCHEMA, if_context: IF_CONTEXT_SCHEMA },
      required: ["filters", "if_context"],
      additionalProperties: false,
    },
    open_target: {
      type: "object",
      properties: { target: TARGET_SCHEMA, if_context: IF_CONTEXT_SCHEMA },
      required: ["target", "if_context"],
      additionalProperties: false,
    },
    show_proposal: {
      type: "object",
      properties: {
        proposal_id: { type: "string", minLength: 1, maxLength: 256 },
        if_context: IF_CONTEXT_SCHEMA,
      },
      required: ["proposal_id", "if_context"],
      additionalProperties: false,
    },
  });
  const DRAFT_ACTION_SCHEMAS = Object.freeze({
    propose_triage: {
      type: "object",
      properties: { draft: PROPOSAL_DRAFT_SCHEMA, if_context: IF_CONTEXT_SCHEMA },
      required: ["draft", "if_context"],
      additionalProperties: false,
    },
    propose_file_review: {
      type: "object",
      properties: { draft: DRAFT_SCHEMA, if_context: IF_CONTEXT_SCHEMA },
      required: ["draft", "if_context"],
      additionalProperties: false,
    },
  });
  const DRAFT_ANNOTATIONS = Object.freeze({
    readOnlyHint: false,
    consequentialHint: true,
    untrustedContentHint: true,
  });

  let registration = null;
  let generation = 0;

  function status(message) {
    try {
      const node = typeof document !== "undefined" && document.getElementById
        ? document.getElementById("agentToolsStatus") : null;
      if (node) node.textContent = message;
    } catch (_) {
      // A status element is optional; an adapter failure must not affect UI.
    }
  }

  function controller() {
    if (typeof AbortController === "function") return new AbortController();
    return { signal: undefined, abort: function () {} };
  }

  function aborted(signal) {
    return !!(signal && signal.aborted);
  }

  function cancellationError() {
    const error = new Error("Tool execution cancelled.");
    error.name = "AbortError";
    return error;
  }

  function copySchema(value) {
    // Metadata is JSON, and a private copy prevents a model-context
    // implementation from modifying the cached definition for another page.
    return JSON.parse(JSON.stringify(value));
  }

  async function readDefinitions(signal) {
    if (typeof fetch !== "function") throw new Error("tool metadata unavailable");
    const response = await fetch("/api/tools", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
    if (!response || !response.ok) throw new Error("tool metadata unavailable");
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.tools)) {
      throw new Error("tool metadata unavailable");
    }
    const byName = new Map();
    payload.tools.forEach((definition) => {
      if (!definition || !READ_TOOL_NAMES.includes(definition.name)) return;
      if (!byName.has(definition.name)) byName.set(definition.name, definition);
    });
    const missing = READ_TOOL_NAMES.filter((name) => {
      const definition = byName.get(name);
      return !definition || !definition.inputSchema ||
        typeof definition.inputSchema !== "object" || Array.isArray(definition.inputSchema);
    });
    if (missing.length) throw new Error("tool metadata unavailable");
    return READ_TOOL_NAMES.map((name) => ({
      name,
      description: READ_DESCRIPTIONS[name] + " Responses are losslessly paginated: use retrieval.continuations " +
        "for exact follow-up arguments, including snapshot preconditions. Never infer patch equivalence " +
        "from paths. If the host clips a response, reduce page_size/member_page_size/file_page_size " +
        "or patch_limit as supported; do not retry the same oversized call.",
      inputSchema: copySchema(byName.get(name).inputSchema),
      annotations: READ_ANNOTATIONS,
    }));
  }

  function safeContext(app) {
    try {
      return app && typeof app.getViewContext === "function"
        ? app.getViewContext() : {};
    } catch (_) {
      return {};
    }
  }

  function failureEnvelope(error, app) {
    // The app seam already carries safe backend envelopes for HTTP errors.
    if (error && error.data && typeof error.data === "object" && !Array.isArray(error.data)) {
      return error.data;
    }
    return {
      ok: false,
      error: {
        code: error && typeof error.code === "string" ? error.code : "agent_tool_error",
        message: "The triage tool could not complete.",
        retryable: true,
        context: safeContext(app),
      },
    };
  }

  function readExecutor(app, name) {
    return async function (args, options) {
      const signal = options && options.signal;
      if (aborted(signal)) throw cancellationError();
      try {
        const result = await app.readTool(name, args === undefined ? {} : args);
        if (aborted(signal)) throw cancellationError();
        return result;
      } catch (error) {
        if (error && error.name === "AbortError") throw error;
        return failureEnvelope(error, app);
      }
    };
  }

  function viewExecutor(app, name) {
    return async function (args, options) {
      const signal = options && options.signal;
      if (aborted(signal)) throw cancellationError();
      try {
        const value = args && typeof args === "object" ? args : {};
        let result;
        if (name === "get_view_context") {
          result = app.getViewContext();
        } else if (name === "set_filters") {
          result = app.setFilters(value.filters, value.if_context);
        } else if (name === "open_target") {
          result = app.openTarget(value.target, value.if_context);
        } else {
          result = app.showProposal(value.proposal_id, value.if_context);
        }
        if (aborted(signal)) throw cancellationError();
        return await Promise.resolve(result);
      } catch (error) {
        if (error && error.name === "AbortError") throw error;
        return failureEnvelope(error, app);
      }
    };
  }

  function draftResultAfterCancellation(result, signal) {
    if (!aborted(signal)) return result;
    // A POST may have committed before the host cancelled the tool promise.
    // Return the identity so the caller can recover with the same stable key;
    // never encourage a retry with a newly generated key.
    if (result && result.ok === true && (result.draft || result.proposal)) {
      return {
        ...result,
        cancellation: {
          code: "cancelled_after_commit",
          message: "The draft was committed before cancellation; reuse the same idempotency_key to recover it.",
          recoverable: true,
        },
      };
    }
    throw cancellationError();
  }

  function draftExecutor(app, name) {
    return async function (args, options) {
      const signal = options && options.signal;
      if (aborted(signal)) throw cancellationError();
      try {
        const value = args && typeof args === "object" ? args : {};
        // A draft can be committed, so it is never retried automatically and
        // never reaches any review, adoption, or decision path.
        const method = name === "propose_triage" ? app.draftProposal : app.draftFileReview;
        const result = await Promise.resolve(method(value.draft, value.if_context));
        return draftResultAfterCancellation(result, signal);
      } catch (error) {
        if (error && error.name === "AbortError") throw error;
        return failureEnvelope(error, app);
      }
    };
  }

  function modelContext() {
    try {
      const value = typeof document !== "undefined" ? document.modelContext : null;
      return value && typeof value.registerTool === "function" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function invalidate(updateStatus) {
    generation += 1;
    const prior = registration;
    registration = null;
    if (prior && prior.controller) {
      try { prior.controller.abort(); } catch (_) {}
    }
    if (updateStatus) status("Browser seam ready · agent tools not registered");
  }

  async function register() {
    if (registration) return;
    const app = typeof window !== "undefined" ? window.TriageApp : null;
    if (!app || typeof app.readTool !== "function" ||
        typeof app.getViewContext !== "function" ||
        typeof app.setFilters !== "function" ||
        typeof app.openTarget !== "function" ||
        typeof app.showProposal !== "function" ||
        typeof app.draftFileReview !== "function" ||
        typeof app.draftProposal !== "function") {
      status("Browser seam waiting · dashboard actions unavailable");
      return;
    }
    const context = modelContext();
    if (!context) {
      status("Browser seam ready · WebMCP unavailable in this browser");
      return;
    }
    if (typeof fetch !== "function") {
      status("Browser seam ready · agent tools unavailable (metadata API unavailable)");
      return;
    }

    const record = { controller: controller(), context, generation: ++generation };
    registration = record;
    status("Browser seam ready · registering " + TOOL_COUNT + " agent tools…");
    try {
      const readDefinitionsValue = await readDefinitions(record.controller.signal);
      if (registration !== record || record.generation !== generation ||
          aborted(record.controller.signal)) return;
      const definitions = readDefinitionsValue.concat(VIEW_TOOL_NAMES.map((name) => ({
        name,
        description: name === "get_view_context"
          ? "Read the current triage browser view context."
          : name === "set_filters"
            ? "Change the triage browser filters after checking the current view context."
            : name === "open_target"
              ? "Open a group, pull request, or repository-relative file after checking the current view context."
              : "Request display of a local triage proposal after checking the current view context.",
        inputSchema: copySchema(VIEW_ACTION_SCHEMAS[name]),
        annotations: name === "get_view_context" ? CONTEXT_ANNOTATIONS : VIEW_ANNOTATIONS,
      }))).concat(DRAFT_TOOL_NAMES.map((name) => ({
        name,
        description: name === "propose_triage"
          ? "Draft an overall PR disposition recommendation for human review after checking the current view context. This never accepts, applies, or decides a proposal or pull request."
          : "Draft file findings and explicit inspection coverage for human review after checking the current view context. This never marks a file reviewed, never accepts a draft, and never decides a pull request.",
        inputSchema: copySchema(DRAFT_ACTION_SCHEMAS[name]),
        annotations: DRAFT_ANNOTATIONS,
      })));

      for (const definition of definitions) {
        await context.registerTool({
          name: definition.name,
          description: definition.description,
          inputSchema: definition.inputSchema,
          annotations: definition.annotations,
          execute: READ_TOOL_NAMES.includes(definition.name)
            ? readExecutor(app, definition.name)
            : DRAFT_TOOL_NAMES.includes(definition.name)
              ? draftExecutor(app, definition.name)
              : viewExecutor(app, definition.name),
        }, { signal: record.controller.signal });
        if (registration !== record || record.generation !== generation ||
            aborted(record.controller.signal)) return;
      }
      record.registered = true;
      status("Browser seam ready · " + TOOL_COUNT + " agent tools registered");
    } catch (error) {
      if (registration !== record || record.generation !== generation ||
          aborted(record.controller.signal)) return;
      // A registration may fail after a few tools were accepted.  Abort the
      // shared lifecycle signal so those partial registrations are removed.
      try { record.controller.abort(); } catch (_) {}
      registration = null;
      status("Browser seam ready · agent tools unavailable (registration failed)");
    }
  }

  function onReady() {
    // A second ready event while registration is pending is harmless and does
    // not create duplicate registrations.
    register().catch(() => {
      if (registration) invalidate(false);
      status("Browser seam ready · agent tools unavailable (registration failed)");
    });
  }

  if (typeof window === "undefined" || typeof document === "undefined") return;
  window.addEventListener("triage:ready", onReady);
  window.addEventListener("pagehide", function () {
    invalidate(false);
  });
  window.addEventListener("pageshow", function (event) {
    if (event && event.persisted) {
      invalidate(false);
      onReady();
    } else if (!registration) {
      onReady();
    }
  });

  // app.js normally dispatches triage:ready before this script is parsed.
  if (window.TriageApp) onReady();
})();
