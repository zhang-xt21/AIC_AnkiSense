const test = require("node:test");
const assert = require("node:assert/strict");
const { parts } = require("../ankisense/static/math-text.js");
test("mixed prose and four math delimiters preserve their exact source", () => {
  const source = String.raw`行内 $x_1^2$，\(a+b\)，块 $$\frac{1}{2}$$ 与 \[x=y\]。`;
  const parsed = parts(source);
  assert.equal(parsed.map(p=>p.text).join(""), source);
  assert.equal(parsed.filter(p=>p.expression).length, 4);
  assert.equal(parsed.filter(p=>p.display).length, 2);
});
test("prices and incomplete delimiters remain plain text", () => {
  for(const source of ["$5 and $10", String.raw`价格 \$5`, "$x+1", "普通文本"]) {
    assert.equal(parts(source).some(p=>p.expression), false);
    assert.equal(parts(source).map(p=>p.text).join(""), source);
  }
});
