import type { DoneEvent, HopEvent } from "../api";
import type { AssistantMsg, IngestMsg, Message, Thread, ThreadStore } from "./types";

export type Action =
  | { type: "hydrate"; store: ThreadStore }
  | { type: "threadCreated"; threadId: string; messageId: string; url: string }
  | { type: "threadAdopted"; threadId: string; repoId: string }
  | { type: "repoIdentified"; threadId: string; repoId: string }
  | { type: "ingestProgress"; threadId: string; messageId: string; stage: string; detail: string }
  | { type: "ingestDone"; threadId: string; messageId: string; summary: DoneEvent }
  | { type: "askStarted"; threadId: string; userId: string; assistantId: string; text: string }
  | { type: "hopReceived"; threadId: string; messageId: string; hop: HopEvent }
  | { type: "answerDelta"; threadId: string; messageId: string; text: string }
  | { type: "answerReceived"; threadId: string; messageId: string; answer: AssistantMsg["answer"] }
  | { type: "runFailed"; threadId: string; messageId: string; message: string }
  | { type: "runAborted"; threadId: string; messageId: string }
  | { type: "threadRemoved"; threadId: string }
  | { type: "activeThreadSet"; threadId: string | null };

/** Apply `change` to one thread, refreshing its sort key. */
function patchThread(
  store: ThreadStore,
  threadId: string,
  change: (thread: Thread) => Thread,
): ThreadStore {
  const thread = store.threads[threadId];
  if (!thread) return store;
  return {
    ...store,
    threads: { ...store.threads, [threadId]: { ...change(thread), updatedAt: Date.now() } },
  };
}

/** Apply `change` to one message inside one thread. */
function patchMessage(
  store: ThreadStore,
  threadId: string,
  messageId: string,
  change: (message: Message) => Message,
): ThreadStore {
  return patchThread(store, threadId, (thread) => ({
    ...thread,
    messages: thread.messages.map((message) =>
      message.id === messageId ? change(message) : message,
    ),
  }));
}

export function threadReducer(store: ThreadStore, action: Action): ThreadStore {
  switch (action.type) {
    case "hydrate":
      return action.store;

    case "threadCreated": {
      const now = Date.now();
      const message: IngestMsg = {
        id: action.messageId,
        kind: "ingest",
        createdAt: now,
        url: action.url,
        stages: [],
        summary: null,
        error: null,
        status: "running",
      };
      const thread: Thread = {
        id: action.threadId,
        repoId: null,
        url: action.url,
        createdAt: now,
        updatedAt: now,
        messages: [message],
      };
      return {
        ...store,
        threads: { ...store.threads, [action.threadId]: thread },
        activeThreadId: action.threadId,
      };
    }

    case "threadAdopted": {
      const now = Date.now();
      const thread: Thread = {
        id: action.threadId,
        repoId: action.repoId,
        url: null,
        createdAt: now,
        updatedAt: now,
        messages: [],
      };
      return {
        ...store,
        threads: { ...store.threads, [action.threadId]: thread },
        activeThreadId: action.threadId,
      };
    }

    case "repoIdentified":
      return patchThread(store, action.threadId, (thread) => ({
        ...thread,
        repoId: action.repoId,
      }));

    case "ingestProgress":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "ingest"
          ? {
              ...message,
              stages: [...message.stages, { stage: action.stage, detail: action.detail }],
            }
          : message,
      );

    case "ingestDone":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "ingest"
          ? { ...message, summary: action.summary, status: "done" }
          : message,
      );

    case "askStarted": {
      const now = Date.now();
      return patchThread(store, action.threadId, (thread) => ({
        ...thread,
        messages: [
          ...thread.messages,
          { id: action.userId, kind: "user", createdAt: now, text: action.text },
          {
            id: action.assistantId,
            kind: "assistant",
            createdAt: now,
            questionId: action.userId,
            hops: [],
            streamingText: "",
            answer: null,
            error: null,
            status: "running",
          },
        ],
      }));
    }

    case "hopReceived":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "assistant"
          ? // A tool-calling turn often narrates a sentence or two before the
            // call ("Let me check X…") and that text streams in exactly like
            // the real answer does — there is no way to tell them apart while
            // it's arriving. Once the hop lands, that turn is over and its
            // narration is spent (it went into tool-call history, not the
            // answer), so clear it here rather than letting it and every
            // later turn's narration glue together into one runaway string.
            { ...message, hops: [...message.hops, action.hop], streamingText: "" }
          : message,
      );

    case "answerDelta":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "assistant"
          ? { ...message, streamingText: message.streamingText + action.text }
          : message,
      );

    case "answerReceived":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "assistant"
          ? { ...message, answer: action.answer, streamingText: "", status: "done" }
          : message,
      );

    case "runFailed":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "user"
          ? message
          : { ...message, error: action.message, status: "error" },
      );

    case "runAborted":
      return patchMessage(store, action.threadId, action.messageId, (message) =>
        message.kind === "user" ? message : { ...message, status: "aborted" },
      );

    case "threadRemoved": {
      const threads = { ...store.threads };
      delete threads[action.threadId];
      return {
        ...store,
        threads,
        activeThreadId:
          store.activeThreadId === action.threadId ? null : store.activeThreadId,
      };
    }

    case "activeThreadSet":
      return { ...store, activeThreadId: action.threadId };

    default:
      return store;
  }
}
