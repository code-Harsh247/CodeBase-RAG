import { toText } from "hast-util-to-text";
import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import yaml from "highlight.js/lib/languages/yaml";
import { visit } from "unist-util-visit";
import type { Element, ElementContent, Root } from "hast";

/**
 * Syntax-highlights fenced code blocks, registering only what this project
 * can actually produce — it reads Python repos; the odd fenced block is a
 * config file or a shell command it grepped — rather than the ~35-language
 * `common` bundle other highlighters default to.
 *
 * Deliberately not `rehype-highlight`: that package imports `lowlight`'s
 * `common` export as a fallback default, and having it in the module graph
 * at all defeats tree-shaking even when it's never the value actually used
 * (the fallback branch is enough to keep it reachable) — an unused ~35-
 * language bundle for a project that only ever needs four.
 */
const lowlight = createLowlight({ python, bash, json, yaml });

function languageOf(node: Element): string | undefined {
  const classes = node.properties?.className;
  if (!Array.isArray(classes)) return undefined;
  const tag = classes.find((c) => typeof c === "string" && c.startsWith("language-"));
  return typeof tag === "string" ? tag.slice("language-".length) : undefined;
}

export function highlightCode() {
  return (tree: Root) => {
    visit(tree, "element", (node: Element, _index, parent) => {
      if (node.tagName !== "code" || parent?.type !== "element" || parent.tagName !== "pre") {
        return;
      }
      const lang = languageOf(node);
      if (!lang || !lowlight.registered(lang)) return;

      const text = toText(node, { whitespace: "pre" });
      const result = lowlight.highlight(lang, text);

      const classes = Array.isArray(node.properties.className)
        ? node.properties.className
        : [];
      node.properties.className = [...classes, "hljs"];
      // lowlight's Root.children is typed for a document (allows Doctype);
      // highlight() only ever emits element/text nodes, valid inside <code>.
      node.children = result.children as ElementContent[];
    });
  };
}
