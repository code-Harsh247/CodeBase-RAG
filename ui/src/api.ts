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

export type StreamEvent = HopEvent | AnswerEvent | ErrorEvent;

export type Mode = "multi_hop" | "single_hop";

export async function fetchRepos(): Promise<Repo[]> {
  const response = await fetch("/api/repos");
  if (!response.ok) throw new Error(`Could not load repositories (${response.status})`);
  return (await response.json()).repos;
}

/**
 * Stream a question's answer, calling `onEvent` as each hop lands.
 *
 * The server sends Server-Sent Events, but this uses fetch rather than
 * EventSource because the request is a POST with a JSON body, which
 * EventSource cannot make.
 */
export async function streamQuery(
  body: { repo_id: string; question: string; mode: Mode },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/query", {
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
        onEvent(JSON.parse(line.slice(6)) as StreamEvent);
      } catch {
        // A malformed frame should not abort a run that is otherwise working.
      }
    }
  }
}
