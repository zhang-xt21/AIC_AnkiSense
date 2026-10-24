"use strict";

// The default import flow stays compact; the original source and generation
// controls remain available inside the optional advanced tools.
async function openMaterials(page) {
  await page.locator('button[data-view="materials"]').click();
  const advanced = page.locator("#manual-tools");
  await advanced.waitFor();
  if (!(await advanced.evaluate(node => node.open))) await advanced.locator(":scope > summary").click();
}

async function openReview(page) {
  await page.locator('button[data-view="review"]').click();
  await page.locator(".workbench-grid").waitFor();
}

async function stage(page, name) {
  if (name === "source") return openMaterials(page);
  return openReview(page);
}

module.exports = { openMaterials, openReview, stage };
