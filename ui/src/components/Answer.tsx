import type { ReactNode } from "react";
import type { AnswerEvent } from "../api";

/**
 * Inline formatting the model actually emits: `code`, **bold**, and
 * `path/to/file.py:42` citations.
 *
 * Deliberately not a full markdown library. The answer format is constrained
 * by the prompt to plain prose with citations, so three rules cover it — and
 * the citations are the part worth making visually distinct, since traceability
 * to source is the claim this project makes.
 */
const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|[\w./\\-]+\.py(?::\d+)?)/g;

function renderInline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={index}>{renderInline(part.slice(2, -2))}</strong>;
    }
    if (/^[\w./\\-]+\.py(?::\d+)?$/.test(part)) {
      return (
        <code className="citation" key={index}>
          {part}
        </code>
      );
    }
    return <span key={index}>{part}</span>;
  });
}

const BULLET = /^\s*[-*]\s+/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
//: The |---|---| separator under a table header.
const TABLE_RULE = /^\s*\|[\s:|-]+\|\s*$/;

function cells(row: string): string[] {
  return row
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

/**
 * Blocks the model actually produces: bullet lists and markdown tables.
 *
 * Which one it picks varies between runs for the same question, and this model
 * has repeatedly ignored prompt instructions about output format, so the
 * renderer handles both rather than the prompt trying to forbid one.
 */
function renderBody(text: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let bullets: string[] = [];

  const flushBullets = () => {
    if (!bullets.length) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`}>
        {bullets.map((item, index) => (
          <li key={index}>{renderInline(item)}</li>
        ))}
      </ul>,
    );
    bullets = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (BULLET.test(line)) {
      bullets.push(line.replace(BULLET, ""));
      continue;
    }
    flushBullets();

    // A table is a header row, a separator, then rows until the block ends.
    const next = lines.slice(index + 1).find((item) => item.trim());
    if (TABLE_ROW.test(line) && next && TABLE_RULE.test(next)) {
      const header = cells(line);
      const rows: string[][] = [];
      let cursor = lines.indexOf(next, index + 1) + 1;
      for (; cursor < lines.length; cursor += 1) {
        if (!lines[cursor].trim()) continue;
        if (!TABLE_ROW.test(lines[cursor])) break;
        rows.push(cells(lines[cursor]));
      }
      blocks.push(
        <div className="table-wrap" key={`t-${blocks.length}`}>
          <table>
            <thead>
              <tr>
                {header.map((cell, i) => (
                  <th key={i}>{renderInline(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{renderInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      index = cursor - 1;
      continue;
    }

    if (line.trim()) {
      blocks.push(<p key={`p-${blocks.length}`}>{renderInline(line)}</p>);
    }
  }
  flushBullets();
  return blocks;
}

export function Answer({ answer }: { answer: AnswerEvent }) {
  return (
    <section className="answer">
      <h2>
        Answer
        <span className="muted">
          {answer.usage.calls} model call{answer.usage.calls === 1 ? "" : "s"} ·{" "}
          {answer.usage.tokens.toLocaleString()} tokens
        </span>
      </h2>

      {renderBody(answer.answer)}

      {answer.locations.length > 0 && (
        <footer>
          <span className="muted">Retrieved from</span>
          <div className="chips">
            {answer.locations.slice(0, 12).map((location) => (
              <code key={location}>{location}</code>
            ))}
            {answer.locations.length > 12 && (
              <span className="muted">+{answer.locations.length - 12} more</span>
            )}
          </div>
        </footer>
      )}
    </section>
  );
}
