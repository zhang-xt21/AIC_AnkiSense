(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MathText = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";
  function parts(value) {
    const text = String(value ?? "");
    const pattern = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^$\n]+?)\$/g;
    const result = []; let offset = 0;
    for (const match of text.matchAll(pattern)) {
      const before = text.slice(0, match.index).match(/\\+$/)?.[0].length || 0;
      if (before % 2) continue;
      const expression = match[1] ?? match[2] ?? match[3] ?? match[4];
      // A pair of prices is prose, not a mathematical expression.
      if (match[4] && (/^\s*[\d.,]+\s*(?:and|to|元|美元|人民币|USD|至|到|，|、)?\s*$/i.test(expression))) continue;
      if (match.index > offset) result.push({ text: text.slice(offset, match.index) });
      result.push({ text: match[0], expression, display: match[1] !== undefined || match[2] !== undefined });
      offset = match.index + match[0].length;
    }
    if (offset < text.length) result.push({ text: text.slice(offset) });
    return result;
  }
  function set(node, value) {
    node.textContent = String(value ?? "");
    if (!root.katex || ["INPUT", "TEXTAREA", "SELECT", "OPTION", "PRE", "CODE"].includes(node.tagName)) return node;
    const document = node.ownerDocument;
    const fragment = document.createDocumentFragment();
    for (const part of parts(value)) {
      if (!part.expression) { fragment.append(document.createTextNode(part.text)); continue; }
      const span = document.createElement("span");
      span.className = part.display ? "math-block" : "math-inline";
      try {
        if (part.expression.length > 4000) throw new Error("length");
        root.katex.render(part.expression, span, { displayMode: part.display, throwOnError: true,
          trust: false, strict: "ignore", maxExpand: 1000, maxSize: 20, output: "htmlAndMathml" });
      } catch {
        span.textContent = part.text; span.className = "math-source-fallback";
        span.title = "公式未能渲染，已保留原文。";
      }
      fragment.append(span);
    }
    node.replaceChildren(fragment);
    return node;
  }
  return { parts, set };
});
