import { useEffect, useRef, useState } from "react";

const MAX_TEXTAREA_PX = 200;

/**
 * The one input, rendered at a single fixed position in the React tree.
 *
 * Its className changes between hero and docked; it is never moved between
 * parents, because remounting it would destroy focus, selection and any
 * in-progress IME composition mid-typing.
 */
export function Composer({
  variant,
  intent,
  disabled,
  onSubmit,
}: {
  variant: "hero" | "docked";
  intent: "url" | "question";
  disabled: boolean;
  onSubmit: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement | null>(null);

  // Grow with the content, up to a cap, then scroll.
  useEffect(() => {
    const node = area.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, [text]);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setText("");
  }

  return (
    <form
      className={`composer composer--${variant}`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={area}
        rows={1}
        value={text}
        spellCheck={intent === "question"}
        placeholder={
          intent === "url"
            ? "Paste a GitHub URL — https://github.com/psf/requests"
            : "Ask about this codebase…"
        }
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          // Enter sends, Shift+Enter is a newline — the convention people
          // already expect from a chat composer.
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <button type="submit" disabled={disabled || !text.trim()}>
        {intent === "url" ? "Index" : "Ask"}
      </button>
    </form>
  );
}
