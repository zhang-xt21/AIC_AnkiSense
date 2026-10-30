"use strict";

// Exercise the real recommendation component with fictitious, in-memory data.
// No production store, layout parser or model service is accessed.
const assert = require("node:assert/strict");

async function checkFigureCrop(page, { check, native = false, screenshot = async () => {} }) {
  const imageRoute = "**/api/documents/crop-fixture/pages/*/image";
  let failImage = false;
  await page.route(imageRoute, route => failImage ? route.abort() : route.fulfill({
    contentType: "image/svg+xml", headers: { "Cache-Control": "no-store" },
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#f7f9fc"/><path d="M180 240h240v320H180zM180 320h240M180 400h240M180 480h240" fill="none" stroke="#4875cb" stroke-width="4"/><text x="180" y="180" font-size="28">STACK / LIFO</text></svg>',
  }));
  await page.evaluate(() => {
    const card = { id: "crop-fixture", version: 1, question: "栈如何出栈？", answer: "后进先出。", concept: "栈", kind: "basic",
      evidence: [{ block_id: "crop-source", quote: "后进先出" }], rubric: [{ point_id: "lifo", text: "指出后进先出" }], images: [] };
    const draft = { ...card, evidence: JSON.stringify(card.evidence), rubric: "lifo | 指出后进先出", images: [] };
    const candidate = { id: "crop-picture", page: 1, kind: "figure", bbox: [.2, .2, .8, .8],
      page_url: "/api/documents/crop-fixture/pages/1/image", preview_url: "/api/documents/crop-fixture/pages/1/image" };
    const job = { id: "crop-job", status: "completed", candidates: [candidate] };
    const fixture = window.cropFixture = { card, draft, candidate, requests: [], adopted: 0 };
    sessionStorage.setItem("ankisense-figure-job:crop-fixture:1", job.id);
    const host = FigureRecommendations.create({ card, draft, media: [], busy: false,
      vision: { available: true, message: "离线模拟图片检查。" },
      api: async (url, options) => {
        fixture.requests.push({ url, method: options?.method || "GET", body: options?.body ? JSON.parse(options.body) : null });
        if (!options?.method) return job;
        if (url.endsWith("/adopt")) return { id: "crop-media", source: { page: 1 } };
        if (url.endsWith("/check")) {
          await new Promise(resolve => setTimeout(resolve, 80));
          if (fixture.failCheck) { fixture.failCheck = false; throw new Error("离线模拟请求失败"); }
          return { verdict: "pass", description: "自编栈示意图", classification: { label: "diagram" },
            relation: "supported", completeness: "complete", legibility: "clear", answer_leakage: "no", reasons: ["标注完整。"] };
        }
        throw new Error("Unexpected fixture request");
      },
      run: async action => action(), onAdopt: (record, side) => {
        fixture.adopted++; draft.images.push({ media_id: record.id, side, caption: "" });
      } });
    host.id = "crop-fixture-host";
    document.getElementById("main").append(host);
  });
  const opener = page.locator("#crop-fixture-host").getByRole("button", { name: "裁剪第 1 页", exact: true });
  const dialog = () => page.locator("dialog.figure-crop-dialog[open]");
  async function open() {
    await opener.click();
    await dialog().waitFor();
  }
  async function closed() {
    await page.waitForFunction(() => !document.querySelector("dialog.figure-crop-dialog"));
    assert.equal(await opener.evaluate(node => node === document.activeElement), true, "Closing returns focus to the originating button");
  }
  try {
    await check("Crop cancel closes immediately without adopting or changing the draft", async () => {
      await open();
      await page.waitForFunction(() => document.querySelector(".figure-crop-stage img")?.naturalWidth > 0);
      if (native) {
        const regions = await page.evaluate(() => {
          const topbar = document.querySelector(".topbar"), crop = document.querySelector("dialog.figure-crop-dialog"), cancel = crop.querySelector("button");
          const header = topbar.getBoundingClientRect(), button = cancel.getBoundingClientRect();
          return { topbar: getComputedStyle(topbar).getPropertyValue("-webkit-app-region"),
            crop: getComputedStyle(crop).getPropertyValue("-webkit-app-region"),
            cancel: getComputedStyle(cancel).getPropertyValue("-webkit-app-region"), overlap: button.top < header.bottom && button.bottom > header.top };
        });
        assert.equal(regions.overlap, true, "Fixture must cover the desktop title bar's vertical region");
        assert.equal(regions.topbar, "no-drag", "A modal must suspend the title bar's native drag hit region");
        assert.equal(regions.crop, "no-drag");
        assert.equal(regions.cancel, "no-drag");
      }
      await dialog().getByText("精确调整范围", { exact: true }).click();
      await dialog().getByLabel("左边界 %", { exact: true }).fill("30");
      await dialog().getByLabel("采纳配图的显示位置").selectOption("question");
      await screenshot("figure-crop-open.png");
      assert.equal(await dialog().evaluate(node => {
        node.scrollTop = node.scrollHeight;
        const modal = node.getBoundingClientRect(), button = node.querySelector("button").getBoundingClientRect();
        return button.top >= modal.top && button.bottom <= modal.bottom;
      }), true, "Cancel must remain visible after scrolling to the crop controls");
      await dialog().getByRole("button", { name: "取消", exact: true }).click();
      await closed();
      assert.deepEqual(await page.evaluate(() => ({ images: cropFixture.draft.images, adopted: cropFixture.adopted,
        writes: cropFixture.requests.filter(request => request.method !== "GET") })), { images: [], adopted: 0, writes: [] });
      if (native) assert.equal(await page.locator(".topbar").evaluate(node => getComputedStyle(node).getPropertyValue("-webkit-app-region")), "drag");
    });
    await check("Escape dismisses the crop and reopening restores its initial selection", async () => {
      await open();
      assert.equal(await dialog().getByLabel("左边界 %", { exact: true }).inputValue(), "20");
      assert.equal(await dialog().getByLabel("采纳配图的显示位置").inputValue(), "answer");
      await dialog().getByText("精确调整范围", { exact: true }).click();
      await dialog().getByLabel("左边界 %", { exact: true }).fill("40");
      await page.keyboard.press("Escape");
      await closed();
      await open();
      assert.equal(await dialog().getByLabel("左边界 %", { exact: true }).inputValue(), "20");
      await dialog().getByRole("button", { name: "取消", exact: true }).click();
      await closed();
    });
    await check("Cancel works even when the original page image fails to load", async () => {
      failImage = true;
      await page.evaluate(() => { cropFixture.candidate.page_url = "/api/documents/crop-fixture/pages/2/image"; });
      await open();
      await dialog().getByText("原图暂时无法读取，请关闭后重试。", { exact: true }).waitFor();
      assert.equal(await dialog().getByRole("button", { name: "加入卡片", exact: true }).isDisabled(), true);
      await dialog().getByRole("button", { name: "取消", exact: true }).click();
      await closed();
      failImage = false;
      await page.evaluate(() => { cropFixture.candidate.page_url = "/api/documents/crop-fixture/pages/1/image"; });
    });
    await check("Picture checks are explicit, retryable and bound to the selected display side", async () => {
      await open();
      assert.equal(await dialog().locator(".figure-crop-precision").getAttribute("open"), null);
      assert.equal(await page.evaluate(() => cropFixture.requests.some(request => request.url.endsWith("/check"))), false);
      await page.evaluate(() => { cropFixture.failCheck = true; });
      await dialog().getByRole("button", { name: "检查配图", exact: true }).click();
      await dialog().getByText("离线模拟请求失败", { exact: true }).waitFor();
      await dialog().getByRole("button", { name: "检查配图", exact: true }).click();
      await dialog().getByText("机器检查：未发现明显问题", { exact: true }).waitFor();
      assert.equal(await dialog().getByText("检查详情", { exact: true }).evaluate(node => node.parentElement.open), false);
      await dialog().getByLabel("采纳配图的显示位置").selectOption("question");
      assert.equal(await dialog().getByText("机器检查：未发现明显问题", { exact: true }).count(), 0);
      assert.equal(await dialog().getByRole("button", { name: "检查配图", exact: true }).isEnabled(), true);
      await screenshot("figure-crop-simple.png");
      await dialog().getByRole("button", { name: "取消", exact: true }).click();
      await closed();
      const sent = await page.evaluate(() => cropFixture.requests.filter(request => request.url.endsWith("/check")));
      assert.equal(sent.length, 2); assert.equal(sent[0].body.side, "answer");
      assert.deepEqual(await page.evaluate(() => cropFixture.draft.images), []);
    });
    await check("Explicit adoption submits exactly one crop and preserves the source card", async () => {
      await open();
      const accept = dialog().getByRole("button", { name: "加入卡片", exact: true });
      await page.waitForFunction(() => ![...document.querySelectorAll("dialog button")].find(node => node.textContent === "加入卡片")?.disabled);
      await dialog().getByText("精确调整范围", { exact: true }).click();
      await dialog().getByLabel("左边界 %", { exact: true }).fill("30");
      await dialog().getByLabel("采纳配图的显示位置").selectOption("question");
      await accept.click();
      await closed();
      const result = await page.evaluate(() => ({ images: cropFixture.draft.images, sourceImages: cropFixture.card.images,
        adopted: cropFixture.adopted, writes: cropFixture.requests.filter(request => request.url.endsWith("/adopt")) }));
      assert.equal(result.adopted, 1); assert.equal(result.writes.length, 1);
      assert.deepEqual(result.writes[0].body.bbox, [.3, .2, .8, .8]);
      assert.equal(result.images[0].side, "question"); assert.deepEqual(result.sourceImages, []);
    });
  } finally {
    await page.evaluate(() => {
      document.querySelector("dialog.figure-crop-dialog")?.close();
      document.getElementById("crop-fixture-host")?.remove();
      sessionStorage.removeItem("ankisense-figure-job:crop-fixture:1"); delete window.cropFixture;
    });
    await page.unroute(imageRoute);
  }
}

module.exports = { checkFigureCrop };
