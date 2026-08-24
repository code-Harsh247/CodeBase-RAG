import { useEffect, useRef } from "react";
import { examplesFor } from "../examples";
import type { Message, Thread as ThreadModel } from "../state/types";
import { AnswerMessage } from "./AnswerMessage";
import { IngestMessage } from "./IngestMessage";

/** Only autoscroll when the reader is already at the bottom. */
const STICK_THRESHOLD_PX = 64;

export function Thread({
  thread,
  nodes,
  hasSource,
  onAsk,
  onOpenTrace,
}: {
  thread: ThreadModel;
  nodes: number | null;
  hasSource: boolean;
  onAsk: (text: string) => void;
  onOpenTrace: (messageId: string) => void;
}) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);

  useEffect(() => {
    const node = scroller.current;
    if (!node || !stick.current) return;
    // "auto", not "smooth": a smooth scroll retriggered on every hop event
    // fights the user and stutters.
    node.scrollTo({ top: node.scrollHeight, behavior: "auto" });
  }, [thread.messages]);

  // Jump to the bottom instantly when switching threads.
  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
    stick.current = true;
  }, [thread.id]);

  const isEmpty = thread.messages.length === 0;
  // While indexing is all there is, centre it rather than pinning it to the
  // top of an otherwise empty column. Once a question is asked the thread
  // becomes a normal top-anchored transcript.
  const onlyIngest =
    thread.messages.length === 1 && thread.messages[0].kind === "ingest";

  return (
    <div
      className="thread-scroll"
      ref={scroller}
      onScroll={(event) => {
        const node = event.currentTarget;
        stick.current =
          node.scrollHeight - node.scrollTop - node.clientHeight < STICK_THRESHOLD_PX;
      }}
    >
      <div className={`thread-inner${onlyIngest ? " thread-inner--centered" : ""}`}>
        {!hasSource && thread.repoId && (
          <p className="notice">
            No local checkout for {thread.repoId} — reading source and grep are
            unavailable, so the agent can only use the graph and semantic search.
          </p>
        )}

        {isEmpty && thread.repoId && (
          <div className="thread-empty">
            <h2>{thread.repoId}</h2>
            {nodes !== null && (
              <p className="muted">{nodes.toLocaleString()} nodes indexed</p>
            )}
            <div className="examples">
              {examplesFor(thread.repoId).map((example) => (
                <button key={example} type="button" onClick={() => onAsk(example)}>
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {thread.messages.map((message) => (
          <MessageView key={message.id} message={message} onOpenTrace={onOpenTrace} />
        ))}
      </div>
    </div>
  );
}

function MessageView({
  message,
  onOpenTrace,
}: {
  message: Message;
  onOpenTrace: (messageId: string) => void;
}) {
  if (message.kind === "ingest") return <IngestMessage message={message} />;
  if (message.kind === "user") {
    return (
      <section className="msg msg--user">
        <p>{message.text}</p>
      </section>
    );
  }
  return <AnswerMessage message={message} onOpenTrace={onOpenTrace} />;
}
