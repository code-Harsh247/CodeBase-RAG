/** Human labels for the stages `/api/ingest` streams, in the order they arrive. */
export const STAGE_LABELS: Record<string, string> = {
  start: "Reading repository",
  clone: "Cloning",
  parse: "Parsing with tree-sitter",
  resolve: "Resolving references",
  load: "Building the graph",
  embed: "Indexing for semantic search",
};

/** Lets the trail render stages that have not been reached yet as pending. */
export const STAGE_ORDER = ["start", "clone", "parse", "resolve", "load", "embed"];
