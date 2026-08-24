import type { Thread } from "../state/types";

export interface SidebarRow {
  /** Present when a local thread exists; absent for a server repo never opened. */
  thread: Thread | null;
  repoId: string | null;
  label: string;
  nodes: number | null;
  /** Indexed on the server. False means the graph no longer has it. */
  available: boolean;
  running: boolean;
}

export function Sidebar({
  rows,
  activeThreadId,
  activeRepoId,
  reposError,
  deleting,
  onSelect,
  onNew,
  onDelete,
}: {
  rows: SidebarRow[];
  activeThreadId: string | null;
  activeRepoId: string | null;
  reposError: string;
  deleting: string | null;
  onSelect: (row: SidebarRow) => void;
  onNew: () => void;
  onDelete: (row: SidebarRow) => void;
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <span className="brand">CodeGraph</span>
      </div>

      <button type="button" className="new-project" onClick={onNew}>
        + New project
      </button>

      <nav className="project-list">
        {rows.map((row) => {
          const active = row.thread
            ? row.thread.id === activeThreadId
            : row.repoId === activeRepoId && !activeThreadId;
          const busy = row.repoId !== null && row.repoId === deleting;
          return (
            <div
              key={row.thread?.id ?? row.repoId ?? row.label}
              className={[
                "project",
                active ? "active" : "",
                row.available ? "" : "stale",
                busy ? "busy" : "",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              <button
                type="button"
                className="project-open"
                onClick={() => onSelect(row)}
                title={row.available ? row.label : `${row.label} — no longer indexed`}
                disabled={busy}
              >
                <span className="project-name">{row.label}</span>
                {row.running && <span className="dot" />}
                {!row.available && <span className="badge subtle">not indexed</span>}
                {row.available && row.nodes !== null && !row.running && (
                  <span className="muted count">{row.nodes.toLocaleString()}</span>
                )}
              </button>

              <button
                type="button"
                className="project-delete"
                // Deleting drops the graph, the embeddings and the clone, so it
                // is confirmed by the caller rather than fired on one click.
                onClick={() => onDelete(row)}
                disabled={busy}
                title={`Delete ${row.label}`}
                aria-label={`Delete ${row.label}`}
              >
                {busy ? "…" : "×"}
              </button>
            </div>
          );
        })}

        {!rows.length && <p className="muted empty-hint">No projects indexed yet.</p>}
      </nav>

      {reposError && (
        <p className="sidebar-error muted">
          Could not reach the server; showing local history only.
        </p>
      )}
    </aside>
  );
}
