import type { AssistantMsg } from "../state/types";
import { AnswerBody, Locations } from "./Answer";

/**
 * One assistant turn.
 *
 * The wrapper must keep `className="answer"`: every rule that formats answer
 * content is descendant-scoped (`.answer p`, `.answer code`, `.answer table`),
 * so dropping it would silently strip all formatting.
 */
export function AnswerMessage({
  message,
  onOpenTrace,
}: {
  message: AssistantMsg;
  onOpenTrace: (messageId: string) => void;
}) {
  const steps = message.hops.length;
  const isStreaming = message.status === "running" && message.streamingText.length > 0;

  if (message.status === "running" && !message.answer && !isStreaming) {
    return (
      <section className="msg msg--assistant answer">
        <p className="thinking">
          <span className="dot" />
          {steps
            ? `investigating — ${steps} step${steps === 1 ? "" : "s"} so far`
            : "starting…"}
        </p>
      </section>
    );
  }

  return (
    <section className="msg msg--assistant answer">
      {message.error && <p className="error">{message.error}</p>}
      {message.status === "aborted" && !message.answer && (
        <p className="notice">This answer was interrupted.</p>
      )}

      {message.answer ? (
        <>
          <AnswerBody text={message.answer.text} />
          <Locations locations={message.answer.locations} />
        </>
      ) : (
        isStreaming && (
          // Raw text as it streams in — the cleaned-up version from the
          // server (dedup'd trailing citations, etc.) replaces it above once
          // the run finishes, which is why this branch only renders while
          // `message.answer` is still null.
          <>
            <AnswerBody text={message.streamingText} />
            <span className="stream-cursor" aria-hidden="true" />
          </>
        )
      )}

      {steps > 0 && (
        <div className="msg-actions">
          <button type="button" className="trace-button" onClick={() => onOpenTrace(message.id)}>
            View retrieval trace
          </button>
          {message.answer && (
            <span className="muted">
              {message.answer.usage.calls} model call
              {message.answer.usage.calls === 1 ? "" : "s"} ·{" "}
              {message.answer.usage.tokens.toLocaleString()} tokens
            </span>
          )}
        </div>
      )}
    </section>
  );
}
