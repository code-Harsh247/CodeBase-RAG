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
  open,
  reposError,
  onToggle,
  onSelect,
  onNew,
}: {
  rows: SidebarRow[];
  activeThreadId: string | null;
  activeRepoId: string | null;
  open: boolean;
  reposError: string;
  onToggle: () => void;
  onSelect: (row: SidebarRow) => void;
  onNew: () => void;
}) {
  return (
    <aside className="sidebar" aria-hidden={!open}>
      <div className="sidebar-head">
        <span className="brand">CodeGraph</span>
        <button
          type="button"
          className="icon-button"
          onClick={onToggle}
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
        >
          ‹
        </button>
      </div>

      <button type="button" className="new-project" onClick={onNew}>
        + New project
      </button>

      <nav className="project-list">
        {rows.map((row) => {
          const active = row.thread
            ? row.thread.id === activeThreadId
            : row.repoId === activeRepoId && !activeThreadId;
          return (
            <button
              key={row.thread?.id ?? row.repoId ?? row.label}
              type="button"
              className={[
                "project",
                active ? "active" : "",
                row.available ? "" : "stale",
              ]
                .filter(Boolean)
                .join(" ")}
              onClick={() => onSelect(row)}
              title={row.available ? row.label : `${row.label} — no longer indexed`}
            >
              <span className="project-name">{row.label}</span>
              {row.running && <span className="dot" />}
              {!row.available && <span className="badge subtle">not indexed</span>}
              {row.available && row.nodes !== null && (
                <span className="muted">{row.nodes.toLocaleString()}</span>
              )}
            </button>
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
