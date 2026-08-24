import type { DoneEvent, HopEvent } from "../api";

export type RunStatus = "running" | "done" | "error" | "aborted";

export interface IngestMsg {
  id: string;
  kind: "ingest";
  createdAt: number;
  url: string;
  stages: { stage: string; detail: string }[];
  summary: DoneEvent | null;
  error: string | null;
  status: RunStatus;
}

export interface UserMsg {
  id: string;
  kind: "user";
  createdAt: number;
  text: string;
}

export interface AssistantMsg {
  id: string;
  kind: "assistant";
  createdAt: number;
  /** The UserMsg this answers, so a retry can be tied back to its question. */
  questionId: string;
  hops: HopEvent[];
  answer: {
    text: string;
    locations: string[];
    usage: { calls: number; tokens: number };
  } | null;
  error: string | null;
  status: RunStatus;
}

export type Message = IngestMsg | UserMsg | AssistantMsg;

export interface Thread {
  /**
   * A local id, deliberately not the repo_id: a thread starts streaming its
   * ingest trail before the repository is identified, and an unparseable URL
   * means it may never be identified at all.
   */
  id: string;
  repoId: string | null;
  url: string | null;
  createdAt: number;
  updatedAt: number;
  messages: Message[];
}

export interface ThreadStore {
  version: 1;
  threads: Record<string, Thread>;
  activeThreadId: string | null;
}

export const emptyStore = (): ThreadStore => ({
  version: 1,
  threads: {},
  activeThreadId: null,
});

/** `crypto.randomUUID` is undefined outside a secure context. */
export function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}
