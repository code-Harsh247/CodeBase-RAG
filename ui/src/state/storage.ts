import { emptyStore, type Thread, type ThreadStore } from "./types";

const THREADS_KEY = "codegraph.threads.v1";
const UI_KEY = "codegraph.ui.v1";

/** A long Cypher query is unbounded; the rest of a hop is already small. */
const MAX_ARGUMENT = 2000;
const MAX_MESSAGES = 100;
const MAX_THREADS = 30;

/**
 * Trim only what is written to disk. In-memory objects keep full fidelity for
 * the current session — truncation is a storage concern, not a display one.
 */
function trim(store: ThreadStore): ThreadStore {
  const recent = Object.values(store.threads)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_THREADS);

  const threads: Record<string, Thread> = {};
  for (const thread of recent) {
    threads[thread.id] = {
      ...thread,
      messages: thread.messages.slice(-MAX_MESSAGES).map((message) =>
        message.kind === "assistant"
          ? {
              ...message,
              hops: message.hops.map((hop) => ({
                ...hop,
                argument: hop.argument.slice(0, MAX_ARGUMENT),
              })),
            }
          : message,
      ),
    };
  }
  return { ...store, threads };
}

export function loadStore(): ThreadStore {
  try {
    const raw = localStorage.getItem(THREADS_KEY);
    if (!raw) return emptyStore();
    const parsed = JSON.parse(raw) as ThreadStore;
    // A version mismatch is not worth migrating for a local dev tool; starting
    // clean beats crashing on a shape that no longer exists.
    if (parsed?.version !== 1 || typeof parsed.threads !== "object") return emptyStore();
    // Always land on the hero: history is kept and reachable from the sidebar,
    // but opening the app shows the title, description and a centered input
    // rather than dropping you back into the middle of an old conversation.
    return { ...parsed, activeThreadId: null };
  } catch {
    return emptyStore();
  }
}

export function saveStore(store: ThreadStore): void {
  const trimmed = trim(store);
  try {
    localStorage.setItem(THREADS_KEY, JSON.stringify(trimmed));
  } catch {
    // Most likely the quota. Drop the oldest thread and try once more; if that
    // still fails, give up silently rather than throwing into React.
    const remaining = Object.values(trimmed.threads).sort(
      (a, b) => b.updatedAt - a.updatedAt,
    );
    remaining.pop();
    const threads: Record<string, Thread> = {};
    for (const thread of remaining) threads[thread.id] = thread;
    try {
      localStorage.setItem(THREADS_KEY, JSON.stringify({ ...trimmed, threads }));
    } catch {
      /* persistence unavailable this session */
    }
  }
}

export function loadSidebarOpen(): boolean {
  try {
    const raw = localStorage.getItem(UI_KEY);
    return raw ? (JSON.parse(raw).sidebarOpen ?? true) : true;
  } catch {
    return true;
  }
}

export function saveSidebarOpen(sidebarOpen: boolean): void {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify({ sidebarOpen }));
  } catch {
    /* ignore */
  }
}
