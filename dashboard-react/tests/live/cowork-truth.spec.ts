import { readFile } from "node:fs/promises"
import { expect, test, type Page, type Locator } from "@playwright/test"

const fixtureFile = process.env.WB_LIVE_FIXTURE_FILE
const baseURL = process.env.WB_LIVE_BASE_URL
const nonce = process.env.WB_LIVE_HARNESS_NONCE
if (!fixtureFile || !baseURL || !nonce || new URL(baseURL).port === "5127") {
  throw new Error("Truth scenarios require the isolated live harness")
}
const fixture = JSON.parse(await readFile(fixtureFile, "utf8")).truth_panel as {
  app_path: string
  store_id: string
  document_id: string
  claim_ids: Record<string, string>
  passages: Record<string, string>
}
if (!fixture) throw new Error("Run with --scenario truth-panel")

const mark = (page: Page, id: string) => page.locator(`[data-wb-anchor-kind='expression'][data-wb-claim-ids*='${id}']`)
const details = (page: Page) => page.getByRole("region", { name: "Claim details", exact: true })
const selectClaim = async (page: Page, id: string) => {
  if (await details(page).isVisible()) await details(page).getByRole("button", { name: "Close", exact: true }).click()
  await page.locator(`[data-truth-claim-id='${id}']`).click()
  await expect(details(page)).toBeVisible()
}
const inEditorViewport = (target: Locator) => target.evaluate((element) => {
  const root = element.closest(".wb-cowork__editor-region")!
  const viewport = root.getBoundingClientRect()
  return [...element.getClientRects()].some((rect) => rect.bottom > viewport.top && rect.top < viewport.bottom)
})
const open = async (page: Page) => {
  await page.goto(`${baseURL}/app/cowork?mode=launcher`)
  const token = await page.evaluate(async (control) => {
    const response = await fetch("/api/_live/identity-bootstrap", {
      method: "POST", headers: { "Content-Type": "application/json", "X-WB-Live-Control": control },
      body: JSON.stringify({ origin: location.origin }),
    })
    if (!response.ok) throw new Error("Isolated bootstrap failed")
    return (await response.json()).token as string
  }, nonce!)
  await page.goto(`${baseURL}${fixture.app_path}#wb-bootstrap=${encodeURIComponent(token)}`)
  await expect(page.locator(".tiptap")).toContainText(fixture.passages.with_evidence)
  await expect.poll(() => page.evaluate(async () => {
    const session = await (await fetch("/api/local-identity/session")).json()
    return session.authenticated === true && session.principal?.origin === location.origin
  })).toBe(true)
  await page.getByRole("tab", { name: "Truth", exact: true }).click()
  await expect(mark(page, fixture.claim_ids.with_evidence)).toHaveCount(1)
}

test.describe("Truth passage actions", { tag: ["@live", "@no-ci"] }, () => {
  test.describe.configure({ mode: "serial" })

  test("marks explain claims and confirmation changes the authoritative fact presentation", async ({ page }) => {
    await open(page)
    await expect(page.getByLabel("Truth mark legend")).toContainText("5 claims in view")
    await expect(page.getByLabel("Truth mark legend")).toContainText("1 fact")
    const target = mark(page, fixture.claim_ids.with_evidence)
    await expect(target).toHaveAttribute("data-wb-is-fact", "false")
    await target.hover()
    const explanation = page.getByRole("complementary", { name: "Claim at this passage" })
    await expect(explanation).toContainText(fixture.passages.with_evidence)
    await expect(explanation).toContainText("1 evidence receipt")
    await explanation.getByRole("button", { name: "Open in Truth" }).click()
    await expect(details(page)).toBeVisible()
    await details(page).getByRole("button", { name: "Confirm", exact: true }).click()
    await expect(details(page)).toContainText("Confirm this exact claim?")
    await expect(target).toHaveAttribute("data-wb-is-fact", "false")
    await details(page).getByRole("button", { name: "Confirm claim", exact: true }).click()
    await expect(target).toHaveAttribute("data-wb-is-fact", "true")
    await expect(page.getByLabel("Truth mark legend")).toContainText("2 facts")
  })

  test("rejection removes the active mark and correction preserves the passage and original wording", async ({ page }) => {
    await open(page)
    await mark(page, fixture.claim_ids.without_evidence).click()
    await details(page).getByRole("button", { name: "Reject", exact: true }).click()
    await details(page).getByRole("button", { name: "Reject claim", exact: true }).click()
    await expect(mark(page, fixture.claim_ids.without_evidence)).toHaveCount(0)
    await details(page).getByRole("button", { name: "Add a corrected claim", exact: true }).click()
    const composer = page.getByRole("region", { name: "Propose a claim", exact: true })
    await expect(composer.getByRole("textbox", { name: "Claim", exact: true })).toHaveValue(fixture.passages.without_evidence)
    await composer.getByRole("textbox", { name: "Claim", exact: true }).fill("The fixture study reached the recorded participants.")
    await composer.getByRole("button", { name: "Propose and connect", exact: true }).click()
    await expect(details(page)).toContainText("The fixture study reached the recorded participants.")
  })

  test("card selection outlines both passages and navigation reaches them on its first activation", async ({ page }) => {
    await open(page)
    const id = fixture.claim_ids.multiple
    const targets = mark(page, id)
    await expect(targets).toHaveCount(2)
    const region = page.locator(".wb-cowork__editor-region")
    await region.evaluate((element) => { element.scrollTop = 0 })
    const before = await region.evaluate((element) => element.scrollTop)
    await selectClaim(page, id)
    await expect.poll(() => region.evaluate((element) => element.scrollTop)).toBe(before)
    await expect(page.locator(`[data-wb-anchor-kind='expression'][data-wb-claim-ids*='${id}'].wb-cowork-anchor--active`)).toHaveCount(2)
    await details(page).getByRole("button", { name: "Show in document", exact: true }).nth(1).click()
    await expect.poll(() => inEditorViewport(targets.nth(1))).toBe(true)
    await details(page).getByRole("button", { name: "Next passage", exact: true }).click()
    await expect.poll(() => inEditorViewport(targets.first())).toBe(true)
    await details(page).getByRole("button", { name: "Next passage", exact: true }).click()
    await expect.poll(() => inEditorViewport(targets.nth(1))).toBe(true)
    await region.evaluate((element) => { element.scrollTop = element.scrollHeight })
    await selectClaim(page, fixture.claim_ids.with_evidence)
    await expect.poll(() => inEditorViewport(mark(page, fixture.claim_ids.with_evidence))).toBe(true)
  })

  test("the shared passage menu targets Chat, captures a change request, and supports keyboard claim entry", async ({ page }) => {
    await open(page)
    const target = mark(page, fixture.claim_ids.confirmed)
    await target.click({ button: "right" })
    await expect(page.getByRole("menu", { name: "Actions for this passage" })).toBeVisible()
    await page.getByRole("menuitem", { name: /^Ask about this part/ }).click()
    await expect(page.getByRole("tab", { name: /^Chat/ })).toHaveAttribute("aria-selected", "true")
    await expect(page.locator(".wb-cowork-chat-target")).toContainText("About:")
    await expect(page.locator(".wb-cowork-chat-target")).not.toContainText("Whole document")
    await page.locator(".tiptap p").filter({ hasText: fixture.passages.confirmed }).selectText()
    await page.getByRole("button", { name: "Passage actions", exact: true }).click()
    await page.getByRole("menuitem", { name: /^Request a change here/ }).click()
    await page.getByRole("textbox", { name: "Change request", exact: true }).fill("Please clarify the observation window in this passage.")
    await page.getByRole("button", { name: "Send change request", exact: true }).click()
    await expect(page.locator(".wb-chat-msg").filter({ hasText: "Please clarify the observation window in this passage." })).toBeVisible()
    const paragraph = page.locator(".tiptap p").filter({ hasText: fixture.passages.confirmed })
    await paragraph.click()
    await page.keyboard.press("Shift+F10")
    await expect(page.getByRole("menuitem", { name: /^Show claim/ })).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(page.locator(".tiptap")).toBeFocused()
    await page.keyboard.press("Alt+Enter")
    await expect(details(page)).toContainText(fixture.passages.confirmed)
  })

  test("a mobile selection exposes passage actions and the Truth pane", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await open(page)
    await page.getByRole("tab", { name: "Editor", exact: true }).click()
    await page.locator(".tiptap p").filter({ hasText: fixture.passages.with_evidence }).selectText()
    await page.getByRole("button", { name: "Passage actions", exact: true }).click()
    await page.getByRole("menuitem", { name: /^Show claim/ }).click()
    await expect(page.getByRole("tab", { name: "Truth", exact: true })).toHaveAttribute("aria-selected", "true")
    await expect(details(page)).toContainText(fixture.passages.with_evidence)
  })

  test("editing a connected passage leaves an explicit stale mark", async ({ page }) => {
    await open(page)
    const target = mark(page, fixture.claim_ids.confirmed)
    await target.click()
    await page.locator(".tiptap").focus()
    await target.selectText()
    await page.keyboard.press("ArrowLeft")
    await page.keyboard.press("ArrowRight")
    await page.keyboard.press("Delete")
    await expect(target).toHaveText(fixture.passages.confirmed[0] + fixture.passages.confirmed.slice(2))
    await expect(mark(page, fixture.claim_ids.confirmed)).toHaveAttribute("data-wb-stale", "span_missing")
  })

  test("typing without identity is retained for explicit attribution after reconnect", async ({ page }) => {
    await open(page)
    await page.context().clearCookies()
    await page.reload()
    await expect(page.locator(".tiptap")).toBeVisible()
    await expect.poll(() => page.evaluate(async () =>
      (await (await fetch("/api/local-identity/session")).json()).authenticated,
    )).toBe(false)
    const requests: { source_kind?: string; expected_actor_ref?: string }[] = []
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/authorship-attestations")) {
        requests.push(request.postDataJSON())
      }
    })
    const typing = " Ordinary typing during missing identity."
    await page.locator(".tiptap").focus()
    await page.keyboard.press("Control+End")
    await page.keyboard.type(typing)
    await page.getByRole("tab", { name: "Provenance", exact: true }).click()
    await expect(page.getByText("Recent typing provenance is awaiting confirmation.", { exact: false })).toBeVisible()
    await page.evaluate(async (control) => {
      const response = await fetch("/api/_live/identity-bootstrap", {
        method: "POST", headers: { "Content-Type": "application/json", "X-WB-Live-Control": control },
        body: JSON.stringify({ origin: location.origin }),
      })
      if (!response.ok) throw new Error("Isolated bootstrap failed")
      location.hash = `wb-bootstrap=${encodeURIComponent((await response.json()).token)}`
    }, nonce!)
    await expect(page.getByRole("heading", { name: "Recent typing needs attribution" })).toBeVisible()
    expect(requests).toEqual([])
    await page.getByRole("button", { name: "Keep for later", exact: true }).click()
    await page.getByRole("button", { name: "Review pending attribution", exact: true }).click()
    const receiptResponse = page.waitForResponse((response) =>
      response.request().method() === "POST" && response.url().includes("/authorship-attestations"),
    )
    await page.getByRole("button", { name: "Confirm attribution", exact: true }).click()
    const receipt = await receiptResponse
    expect(receipt.status()).toBe(201)
    expect(await receipt.json()).toMatchObject({ ok: true, attestation_id: expect.any(String) })
    await expect(page.getByRole("dialog")).toHaveCount(0)
    expect(requests).toHaveLength(1)
    expect(requests[0]).toMatchObject({ source_kind: "legacy", expected_actor_ref: expect.any(String) })
    await expect(page.locator(".tiptap")).toContainText(typing.trim())
  })
})
