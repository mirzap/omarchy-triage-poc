/**
 * Tiny re-export of TanStack Virtual onto window for the dashboard.
 * Built once with esbuild; triage serve needs no npm at runtime.
 */
import {
  Virtualizer,
  elementScroll,
  observeElementOffset,
  observeElementRect,
  measureElement,
} from "@tanstack/virtual-core";

window.TanStackVirtual = {
  Virtualizer,
  elementScroll,
  observeElementOffset,
  observeElementRect,
  measureElement,
};
