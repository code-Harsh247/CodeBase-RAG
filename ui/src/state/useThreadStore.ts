import { useCallback, useEffect, useReducer, useRef } from "react";
import { streamIngest, streamQuery, summarizeHistory } from "../api";
import { deriveHistory, newestSummarizableId, shouldSummarize } from "./history";
import { threadReducer } from "./reducer";
import { loadStore, saveStore } from "./storage";
import { newId, type Thread } from "./types";

const PERSIST_DEBOUNCE_MS = 400;

export function useThreadStore() {
  // Read synchronously in the initializer, not an effect: the first paint
  // already knows whether to show the hero or a restored thread.
  const [store, dispatch] = useReducer(threadReducer, undefined, loadStore);

  // One controller per thread, so runs in different threads stay independent.
  const runs = useRef(new Map<string, AbortController>());

  useEffect(() => {
    const timer = setTimeout(() => saveStore(store), PERSIST_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [store]);

  // A debounce loses the last write if the tab closes first; visibilitychange
  // is more reliable than beforeunload for this.
  const latest = useRef(store);
  latest.current = store;
  useEffect(() => {
    const flush = () => {
      if (document.visibilityState === "hidden") saveStore(latest.current);
    };
    document.addEventListener("visibilitychange", flush);
    return () => document.removeEventListener("visibilitychange", flush);
  }, []);

  useEffect(() => {
    const inFlight = runs.current;
    return () => inFlight.forEach((controller) => controller.abort());
  }, []);

  /**
   * `dispatch` is referentially stable for the component's lifetime, so these
   * event callbacks can never close over stale state — which is the whole
   * reason this is a reducer rather than a pile of useState setters.
   */
  const startIngest = useCallback(async (url: string) => {
    const threadId = newId();
    const messageId = newId();
    dispatch({ type: "threadCreated", threadId, messageId, url });

    const controller = new AbortController();
    runs.current.set(threadId, controller);

    try {
      await streamIngest(
        { url, refresh: false },
        (event) => {
          if (event.type === "progress") {
            dispatch({
              type: "ingestProgress",
              threadId,
              messageId,
              stage: event.stage,
              detail: event.detail,
            });
            // `start` carries owner/name, so the repo is identified before the
            // clone even begins — no client-side URL parsing needed.
            if (event.stage === "start") {
              dispatch({ type: "repoIdentified", threadId, repoId: event.detail });
            }
          } else if (event.type === "done") {
            dispatch({ type: "repoIdentified", threadId, repoId: event.repo_id });
            dispatch({ type: "ingestDone", threadId, messageId, summary: event });
          } else {
            dispatch({ type: "runFailed", threadId, messageId, message: event.message });
          }
        },
        controller.signal,
      );
    } catch (exc) {
      if (controller.signal.aborted) {
        dispatch({ type: "runAborted", threadId, messageId });
      } else {
        dispatch({ type: "runFailed", threadId, messageId, message: String(exc) });
      }
    } finally {
      runs.current.delete(threadId);
    }
    return threadId;
  }, []);

  const ask = useCallback(async (thread: Thread, text: string) => {
    if (!thread.repoId) return;
    const threadId = thread.id;
    const userId = newId();
    const assistantId = newId();
    dispatch({ type: "askStarted", threadId, userId, assistantId, text });

    const controller = new AbortController();
    runs.current.set(threadId, controller);

    // Snapshot taken before this question was dispatched, so it carries only
    // completed earlier turns — the in-flight one is not in it yet.
    const history = deriveHistory(thread);
    let answerText: string | null = null;

    try {
      await streamQuery(
        {
          repo_id: thread.repoId,
          question: text,
          history: history.turns,
          history_summary: history.summary,
        },
        (event) => {
          if (event.type === "hop") {
            dispatch({ type: "hopReceived", threadId, messageId: assistantId, hop: event });
          } else if (event.type === "answer_delta") {
            dispatch({ type: "answerDelta", threadId, messageId: assistantId, text: event.text });
          } else if (event.type === "answer") {
            answerText = event.answer;
            dispatch({
              type: "answerReceived",
              threadId,
              messageId: assistantId,
              answer: {
                text: event.answer,
                locations: event.locations,
                usage: event.usage,
              },
            });
          } else {
            dispatch({
              type: "runFailed",
              threadId,
              messageId: assistantId,
              message: event.message,
            });
          }
        },
        controller.signal,
      );
    } catch (exc) {
      if (controller.signal.aborted) {
        dispatch({ type: "runAborted", threadId, messageId: assistantId });
      } else {
        dispatch({
          type: "runFailed",
          threadId,
          messageId: assistantId,
          message: String(exc),
        });
      }
    } finally {
      runs.current.delete(threadId);
    }

    // Fold older turns into the running summary once the thread has grown past
    // the threshold — but only the ones that were already there, never the
    // exchange that just finished. That one stays raw so the next question has
    // verbatim context to resolve against; see `newestSummarizableId`.
    if (answerText === null) return;
    const foldThroughId = newestSummarizableId(thread);
    if (!foldThroughId || !history.turns.length) return;

    const weight = [
      ...history.turns,
      { role: "user" as const, content: text },
      { role: "assistant" as const, content: answerText },
    ];
    if (!shouldSummarize(weight)) return;

    // Deliberately not awaited by the caller: this runs behind the UI so the
    // next question never waits on it. If it fails the thread simply keeps
    // sending these turns verbatim until a later fold-in succeeds.
    void summarizeHistory({ prior_summary: history.summary, turns: history.turns })
      .then((summary) => {
        dispatch({
          type: "historySummarized",
          threadId,
          summary,
          throughMessageId: foldThroughId,
        });
      })
      .catch((exc) => {
        console.warn("could not summarize conversation history", exc);
      });
  }, []);

  return { store, dispatch, startIngest, ask, isRunning: (id: string) => runs.current.has(id) };
}
