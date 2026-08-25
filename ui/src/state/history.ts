import type { Thread } from "./types";

export interface HistoryTurn {
  role: "user" | "assistant";
  content: string;
}

export interface DerivedHistory {
  /** The thread's running summary of everything already folded in. */
  summary: string;
  /** Completed turns after that summary, sent verbatim. */
  turns: HistoryTurn[];
}

/**
 * Fold-in threshold, in characters of un-summarized turn text.
 *
 * A deliberate approximation: deciding *whether* to make a network call does
 * not justify shipping a second tokenizer to the browser. The count that
 * actually bounds the prompt is done server-side with the real tokenizer for
 * the model in use (see `agent/summarize.py`'s `trim_to_budget`).
 */
export const SUMMARIZE_AFTER_CHARS = 6000;

/**
 * Prior turns of a thread, as the query API wants them.
 *
 * Pairs each completed assistant message with its question through the
 * `questionId` link rather than by position — that skips ingest messages and
 * any turn still running, errored or aborted, without needing to special-case
 * them. Only clean Q&A crosses turns: the model does not need to re-see how a
 * past answer was retrieved, and replaying hops would multiply every later
 * prompt by the tool output of every earlier one.
 */
export function deriveHistory(thread: Thread): DerivedHistory {
  const questions = new Map(
    thread.messages.filter((message) => message.kind === "user").map((m) => [m.id, m]),
  );

  const turns: HistoryTurn[] = [];
  // Nullish means nothing has been folded in yet, so every completed turn
  // counts. Storage backfills this, but tolerate a missing value here too —
  // the failure mode is silent (no history sent at all) rather than loud.
  let reached = !thread.summarizedThroughMessageId;

  for (const message of thread.messages) {
    if (message.kind !== "assistant" || !message.answer) continue;

    // Everything up to and including the summarized message is already
    // represented by `historySummary`; collect only what comes after it.
    if (!reached) {
      if (message.id === thread.summarizedThroughMessageId) reached = true;
      continue;
    }

    const question = questions.get(message.questionId);
    if (!question) continue;
    turns.push(
      { role: "user", content: question.text },
      { role: "assistant", content: message.answer.text },
    );
  }

  return { summary: thread.historySummary, turns };
}

export function shouldSummarize(turns: HistoryTurn[]): boolean {
  return turns.reduce((total, turn) => total + turn.content.length, 0) > SUMMARIZE_AFTER_CHARS;
}

/**
 * The newest completed assistant message, or null if there is none.
 *
 * Marks how far a fold-in reaches. Deliberately the newest message *as of
 * before the current question* — the exchange that just finished stays raw so
 * the next question has verbatim context to resolve a pronoun against.
 * Folding everything in leaves nothing but the summary, and a summary
 * compresses away which subject was most recently in focus: tested live,
 * "what calls it?" after asking about `build_model` resolved "it" to a
 * different function the summary happened to lead with.
 */
export function newestSummarizableId(thread: Thread): string | null {
  for (let i = thread.messages.length - 1; i >= 0; i -= 1) {
    const message = thread.messages[i];
    if (message.kind === "assistant" && message.answer) return message.id;
  }
  return null;
}
