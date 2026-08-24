import { useState } from "react";
import { streamIngest, type DoneEvent, type ProgressEvent } from "../api";

const STAGE_LABELS: Record<string, string> = {
  start: "Reading repository",
  clone: "Cloning",
  parse: "Parsing with tree-sitter",
  resolve: "Resolving references",
  load: "Building the graph",
  embed: "Indexing for semantic search",
};

/**
 * Paste a GitHub URL, watch it get indexed, then ask about it.
 *
 * Ingestion takes tens of seconds, and its two slowest stages — cloning and
 * embedding — produce no output of their own, so the stages are streamed and
 * shown rather than hidden behind a spinner that looks identical to a hang.
 */
export function AddRepo({ onIngested }: { onIngested: (repoId: string) => void }) {
  const [url, setUrl] = useState("");
  const [running, setRunning] = useState(false);
  const [stages, setStages] = useState<ProgressEvent[]>([]);
  const [done, setDone] = useState<DoneEvent | null>(null);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = url.trim();
    if (!trimmed || running) return;

    setRunning(true);
    setStages([]);
    setDone(null);
    setError("");

    try {
      await streamIngest({ url: trimmed, refresh: false }, (item) => {
        if (item.type === "progress") setStages((current) => [...current, item]);
        else if (item.type === "done") {
          setDone(item);
          onIngested(item.repo_id);
        } else setError(item.message);
      });
    } catch (exc) {
      setError(String(exc));
    } finally {
      setRunning(false);
    }
  }

  const current = stages[stages.length - 1];

  return (
    <section className="add-repo">
      <form onSubmit={submit}>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/psf/requests"
          disabled={running}
          spellCheck={false}
        />
        <button type="submit" disabled={running || !url.trim()}>
          {running ? "Indexing…" : "Index repository"}
        </button>
      </form>

      {running && current && (
        <p className="thinking">
          <span className="dot" />
          {STAGE_LABELS[current.stage] ?? current.stage} — <span className="muted">{current.detail}</span>
        </p>
      )}

      {done && (
        <p className="ingest-done">
          Indexed <strong>{done.repo_id}</strong> in {done.seconds}s —{" "}
          {done.files} files, {done.nodes.toLocaleString()} nodes,{" "}
          {done.edges.toLocaleString()} edges, {done.embedded} definitions embedded.
        </p>
      )}

      {error && <p className="error">{error}</p>}
    </section>
  );
}
