/** Human labels for the stages `/api/ingest` streams, in the order they arrive. */
export const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting in line",
  start: "Reading repository",
  clone: "Cloning",
  parse: "Parsing with tree-sitter",
  resolve: "Resolving references",
  load: "Building the graph",
  embed: "Indexing for semantic search",
};

/**
 * Lets the trail render stages that have not been reached yet as pending.
 * "queued" is deliberately absent: it is a wait, not a step of the work, and
 * is shown as a note above the trail instead.
 */
export const STAGE_ORDER = ["start", "clone", "parse", "resolve", "load", "embed"];
