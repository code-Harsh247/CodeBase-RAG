import type { AnchorHTMLAttributes, ReactNode, TableHTMLAttributes } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** A `path/to/file.py` or `path/to/file.py:42` token — the citation format
 * the system prompt asks for. */
const CITATION = /^[\w./\\-]+\.py(?::\d+)?$/;

/** Same shape, unanchored, for finding citations embedded in prose. */
const CITATION_TOKEN = /[\w./\\-]+\.py(?::\d+)?/g;

/**
 * The model backtick-wraps citations roughly half the time and writes the
 * other half as bare prose ("...defined in src/x.py:61)"). Markdown only
 * turns backtick spans into `code` elements, so bare citations need to be
 * wrapped before they reach the parser or they render as plain text with no
 * highlighting — the one thing this tool's traceability claim depends on.
 *
 * Splits on fenced code blocks first and leaves those untouched (a citation-
 * shaped string inside real code is code, not a citation), then on existing
 * inline code spans within what's left, so already-backtick-wrapped
 * citations are never double-wrapped.
 */
function linkifyBareCitations(text: string): string {
  return text
    .split(/(```[\s\S]*?```)/g)
    .map((segment) =>
      segment.startsWith("```")
        ? segment
        : segment
            .split(/(`[^`\n]*`)/g)
            .map((piece) =>
              piece.startsWith("`") ? piece : piece.replace(CITATION_TOKEN, "`$&`"),
            )
            .join(""),
    )
    .join("");
}

/**
 * Citations get the accent-coloured pill (`.citation`); every other span —
 * inline code and fenced blocks alike — gets the plain code look. No need to
 * tell inline and block code apart here: a fenced block's content is never a
 * single bare filename, so `CITATION` can only ever match a real citation.
 */
function CodeSpan({ className, children }: { className?: string; children?: ReactNode }) {
  const text = String(children).replace(/\n$/, "");
  if (CITATION.test(text)) {
    return <code className="citation">{text}</code>;
  }
  return <code className={className}>{children}</code>;
}

/** Wrapped in `.table-wrap` so a wide table scrolls in its own box rather
 * than the page — the same rule already applied to fenced code and hop
 * output. */
function TableWrap({
  children,
  ...props
}: TableHTMLAttributes<HTMLTableElement> & { children?: ReactNode }) {
  return (
    <div className="table-wrap">
      <table {...props}>{children}</table>
    </div>
  );
}

/** Answers can reference external docs or PyPI/GitHub links; open them
 * without leaving the app. */
function ExternalLink({
  children,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { children?: ReactNode }) {
  return (
    <a {...props} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

const COMPONENTS = { code: CodeSpan, table: TableWrap, a: ExternalLink };

/**
 * The formatted answer text: full markdown (headings, fenced code, lists,
 * tables, links) via `react-markdown`, plus citation highlighting on top.
 *
 * Callers must render this inside an element carrying `className="answer"`:
 * every style rule for answer content is descendant-scoped (`.answer p`,
 * `.answer code`, `.answer table`…), so without that wrapper the formatting
 * silently disappears. Re-parses from scratch on every call, which is what
 * lets this render safely mid-stream as `text` grows — an unclosed fence or
 * unfinished table just renders as it stands and corrects itself once the
 * rest arrives.
 */
export function AnswerBody({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
      {linkifyBareCitations(text)}
    </ReactMarkdown>
  );
}

/** The "retrieved from" footer of file:line chips. */
export function Locations({ locations }: { locations: string[] }) {
  if (!locations.length) return null;
  return (
    <footer>
      <span className="muted">Retrieved from</span>
      <div className="chips">
        {locations.slice(0, 12).map((location) => (
          <code key={location}>{location}</code>
        ))}
        {locations.length > 12 && (
          <span className="muted">+{locations.length - 12} more</span>
        )}
      </div>
    </footer>
  );
}
