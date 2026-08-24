import { useState } from "react";
import type { HopEvent, Mode } from "../api";

const TOOL_LABELS: Record<string, string> = {
  graph_query: "graph query",
  semantic_search: "semantic search",
  read_code: "read source",
  grep: "grep",
};

/**
 * The investigation, shown as it happens.
 *
 * This is the part worth looking at: it is the evidence that an answer came
 * from the repository rather than from the model's memory, and it is what
 * distinguishes multi-hop retrieval from a single lookup.
 */
export function HopTrace({
  hops,
  running,
  mode,
}: {
  hops: HopEvent[];
  running: boolean;
  mode: Mode;
}) {
  if (!hops.length && !running) return null;

  return (
    <section className="trace">
      <h2>
        Retrieval trace
        <span className="muted">
          {mode === "single_hop" ? "one graph query" : `${hops.length} step${hops.length === 1 ? "" : "s"}`}
        </span>
      </h2>
      <ol>
        {hops.map((hop, index) => (
          <HopRow key={`${hop.n}-${index}`} hop={hop} />
        ))}
      </ol>
      {running && (
        <p className="thinking">
          <span className="dot" />
          {hops.length ? "deciding what to look at next…" : "starting…"}
        </p>
      )}
    </section>
  );
}

function HopRow({ hop }: { hop: HopEvent }) {
  const [open, setOpen] = useState(false);
  const label = TOOL_LABELS[hop.tool] ?? hop.tool;

  return (
    <li className={hop.ok ? "hop" : "hop failed"}>
      <button type="button" className="hop-head" onClick={() => setOpen(!open)}>
        <span className={`tool tool-${hop.tool}`}>{label}</span>
        <code className="argument">{hop.argument.replace(/\s+/g, " ").slice(0, 110)}</code>
        {!hop.ok && <span className="badge">failed</span>}
        {hop.locations.length > 0 && (
          <span className="badge subtle">
            {hop.locations.length} location{hop.locations.length === 1 ? "" : "s"}
          </span>
        )}
        <span className="chevron">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="hop-body">
          <pre className="full-argument">{hop.argument}</pre>
          {hop.preview && <pre className="preview">{hop.preview}</pre>}
          {hop.locations.length > 0 && (
            <ul className="locations">
              {hop.locations.map((location) => (
                <li key={location}>
                  <code>{location}</code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}
