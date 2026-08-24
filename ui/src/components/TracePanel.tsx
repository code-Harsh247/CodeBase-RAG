import { useEffect } from "react";
import type { HopEvent } from "../api";
import { HopRow } from "./HopTrace";

/**
 * The retrieval trace for one answer, in a slide-in panel.
 *
 * Positioned fixed and moved with `transform` rather than `width`, so opening
 * it neither reflows the thread nor animates an unanimatable property.
 */
export function TracePanel({
  hops,
  running,
  onClose,
}: {
  hops: HopEvent[] | null;
  running: boolean;
  onClose: () => void;
}) {
  const open = hops !== null;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      {open && <div className="scrim" onClick={onClose} />}
      <aside
        className={`trace-panel${open ? " open" : ""}`}
        aria-hidden={!open}
        aria-label="Retrieval trace"
      >
        <div className="trace-head">
          <h2>
            Retrieval trace
            <span className="muted">
              {hops?.length ?? 0} step{hops?.length === 1 ? "" : "s"}
            </span>
          </h2>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Close trace"
          >
            ×
          </button>
        </div>

        <div className="trace-body">
          {hops && hops.length > 0 ? (
            <ol>
              {hops.map((hop, index) => (
                <HopRow key={`${hop.n}-${index}`} hop={hop} />
              ))}
            </ol>
          ) : (
            <p className="muted">No retrieval steps recorded.</p>
          )}
          {running && (
            <p className="thinking">
              <span className="dot" />
              still investigating…
            </p>
          )}
        </div>
      </aside>
    </>
  );
}
