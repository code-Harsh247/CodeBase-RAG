import type { CSSProperties } from "react";
import { STAGE_LABELS, STAGE_ORDER } from "../ingestStages";
import type { IngestMsg } from "../state/types";

/**
 * The indexing trail, as the first message in a thread.
 *
 * Cloning and embedding produce no output of their own and together take most
 * of the ~15s, so the stages are shown explicitly: a bare spinner here is
 * indistinguishable from a hang.
 */
export function IngestMessage({ message }: { message: IngestMsg }) {
  const reached = new Set(message.stages.map((item) => item.stage));
  const current = message.stages[message.stages.length - 1];
  const failed = message.status === "error";

  // The rail fills to the centre of the furthest stage reached, so the line
  // grows downward as the work progresses rather than jumping per step.
  const index = current ? STAGE_ORDER.indexOf(current.stage) : -1;
  const progress =
    index < 0 ? 0 : ((index + 0.5) / STAGE_ORDER.length) * 100;

  return (
    <section className="msg msg--ingest">
      <p className="msg-label">Indexing {message.url}</p>

      <ol
        className={`stages${failed ? " failed" : ""}`}
        style={{ "--progress": `${progress}%` } as CSSProperties}
      >
        {STAGE_ORDER.map((stage) => {
          const done = reached.has(stage);
          const active = message.status === "running" && current?.stage === stage;
          return (
            <li
              key={stage}
              className={`stage${done ? " done" : ""}${active ? " active" : ""}`}
            >
              <span className="stage-dot" />
              {STAGE_LABELS[stage] ?? stage}
            </li>
          );
        })}
      </ol>

      {message.summary && (
        <p className="ingest-done">
          Indexed <strong>{message.summary.repo_id}</strong> in{" "}
          {message.summary.seconds}s — {message.summary.files} files,{" "}
          {message.summary.nodes.toLocaleString()} nodes,{" "}
          {message.summary.edges.toLocaleString()} edges, {message.summary.embedded}{" "}
          definitions embedded.
        </p>
      )}

      {failed && message.error && <p className="error">{message.error}</p>}
      {message.status === "aborted" && (
        <p className="notice">Indexing was interrupted.</p>
      )}
    </section>
  );
}
