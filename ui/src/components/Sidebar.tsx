import type { Thread } from "../state/types";

export interface SidebarRow {
  /** Present when a local thread exists; absent for a server repo never opened. */
  thread: Thread | null;
  repoId: string | null;
  label: string;
  /** Indexed on the server. False means the graph no longer has it. */
  available: boolean;
  running: boolean;
}

/**
 * Split a row label into the repository name and its owner.
 *
 * The name is what distinguishes one project from another in a list; the owner
 * only matters when two repos share a name, so it goes underneath in small
 * type. A thread shows its URL until the ingest `start` event supplies the repo
 * id, so URLs are handled too.
 */
function splitLabel(label: string): { name: string; owner: string | null } {
  const path = label.replace(/^https?:\/\/[^/]+\//, "").replace(/\.git$/, "");
  const parts = path.split("/").filter(Boolean);
  if (parts.length >= 2) {
    return { owner: parts[parts.length - 2], name: parts[parts.length - 1] };
  }
  return { owner: null, name: label };
}

export function Sidebar({
  rows,
  activeThreadId,
  activeRepoId,
  reposError,
  actionError,
  onSelect,
  onNew,
  onDelete,
}: {
  rows: SidebarRow[];
  activeThreadId: string | null;
  activeRepoId: string | null;
  reposError: string;
  /** A failed action, shown verbatim — it names the repo and the cause. */
  actionError: string;
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
          const { name, owner } = splitLabel(row.label);
          const active = row.thread
            ? row.thread.id === activeThreadId
            : row.repoId === activeRepoId && !activeThreadId;
          return (
            <div
              key={row.thread?.id ?? row.repoId ?? row.label}
              className={[
                "project",
                active ? "active" : "",
                row.available ? "" : "stale",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              <button
                type="button"
                className="project-open"
                onClick={() => onSelect(row)}
                title={row.available ? row.label : `${row.label} — no longer indexed`}
              >
                <span className="project-text">
                  <span className="project-name">{name}</span>
                  {owner && <span className="project-owner">{owner}</span>}
                </span>
                {row.running && <span className="dot" />}
                {!row.available && <span className="badge subtle">not indexed</span>}
              </button>

              <button
                type="button"
                className="project-delete"
                // Deleting drops the graph, the embeddings and the clone, so it
                // is confirmed by the caller rather than fired on one click.
                onClick={() => onDelete(row)}
                title={`Delete ${row.label}`}
                aria-label={`Delete ${row.label}`}
              >
                ×
              </button>
            </div>
          );
        })}

        {!rows.length && <p className="muted empty-hint">No projects indexed yet.</p>}
      </nav>

      {actionError && <p className="sidebar-error">{actionError}</p>}

      {reposError && (
        <p className="sidebar-error muted">
          Could not reach the server; showing local history only.
        </p>
      )}
    </aside>
  );
}
