export interface Repo {
  repo_id: string;
  nodes: number;
  has_source: boolean;
}

export interface HopEvent {
  type: "hop";
  n: number;
  tool: string;
  argument: string;
  ok: boolean;
  locations: string[];
  preview: string;
}

export interface AnswerDeltaEvent {
  type: "answer_delta";
  text: string;
}

export interface AnswerEvent {
  type: "answer";
  answer: string;
  locations: string[];
  usage: { calls: number; tokens: number };
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export type StreamEvent = HopEvent | AnswerDeltaEvent | AnswerEvent | ErrorEvent;

export interface ProgressEvent {
  type: "progress";
  stage: string;
  detail: string;
}

export interface DoneEvent {
  type: "done";
  repo_id: string;
  files: number;
  nodes: number;
  edges: number;
  embedded: number;
  seconds: number;
}

export type IngestEvent = ProgressEvent | DoneEvent | ErrorEvent;

export type Mode = "multi_hop" | "single_hop";

export interface HistoryTurn {
  role: "user" | "assistant";
  content: string;
}

/**
 * Fold recent turns into a thread's running summary.
 *
 * Called in the background once an answer lands, so the cost never sits in
 * front of the next question. A plain request/response, not a stream — the
 * result is a few sentences.
 */
export async function summarizeHistory(body: {
  prior_summary: string;
  turns: HistoryTurn[];
}): Promise<string> {
  const response = await fetch("/api/summarize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Could not summarize history (${response.status})`);
  return (await response.json()).summary;
}

export async function fetchRepos(): Promise<Repo[]> {
  const response = await fetch("/api/repos");
  if (!response.ok) throw new Error(`Could not load repositories (${response.status})`);
  return (await response.json()).repos;
}

/**
 * POST a JSON body and stream the Server-Sent Events that come back.
 *
 * Uses fetch rather than EventSource because these are POSTs with a JSON body,
 * which EventSource cannot make.
 */
async function streamPost<T>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Request failed (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line; a partial frame stays in the
    // buffer until the rest of it arrives.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const line = frame.split("\n").find((item) => item.startsWith("data: "));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as T);
      } catch {
        // A malformed frame should not abort a run that is otherwise working.
      }
    }
  }
}

/**
 * Permanently remove a repository: its graph nodes, embeddings and clone.
 *
 * Deleting only local history would not work — the repository reappears from
 * /api/repos on the next refresh — so this removes the thing itself.
 */
export async function deleteRepo(repoId: string): Promise<void> {
  const response = await fetch(`/api/repos/${repoId}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(`Could not delete ${repoId} (${response.status})`);
  }
}

/**
 * Ask a question; `onEvent` fires per retrieval hop, then per text fragment as
 * the answer streams in, then once more with the complete, cleaned-up answer.
 */
export function streamQuery(
  body: {
    repo_id: string;
    question: string;
    mode?: Mode;
    history?: HistoryTurn[];
    history_summary?: string;
  },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPost("/api/query", body, onEvent, signal);
}

/** Clone and index a repository; `onEvent` fires per stage, then once when done. */
export function streamIngest(
  body: { url: string; refresh: boolean },
  onEvent: (event: IngestEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPost("/api/ingest", body, onEvent, signal);
}
