import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

const views = ['Chat', "Door's answer", 'Sign-off', 'Limits view', 'Record']
const hotelRequest = 'I need 1,000 litres of lactose-free milk by Monday.'

// Human actions and reads need the door's simulated buyer login. The tests'
// own direct calls obtain one for the customer concerned, like the console does.
async function buyerHeaders(request: APIRequestContext, customerId: string) {
  const response = await request.post('/demo/buyer-session', { data: { customer_id: customerId } })
  expect(response.ok(), `POST /demo/buyer-session returned ${response.status()}`).toBeTruthy()
  const session = await response.json()
  expect(session.customer_id).toBe(customerId)
  expect(typeof session.token).toBe('string')
  return { 'X-Buyer-Session': session.token as string }
}

test.beforeEach(async ({ page, request, baseURL }) => {
  const health = await request.get('/healthz')
  expect(health.ok()).toBeTruthy()
  expect((await health.json()).fixture).toBe(true)
  expect((await request.post('/demo/reset', { data: { customer_id: 'hotel' }, headers: await buyerHeaders(request, 'hotel') })).ok()).toBeTruthy()
  // A real Chromium page calls the actual local FastAPI app. External fonts or
  // scripts cannot create network dependencies or accidentally call a provider.
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    return url.origin === baseURL || url.protocol === 'data:' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.goto('/')
  await expect(page.getByText('Door connected', { exact: true })).toBeVisible()
})

// M06: the mock bank window. It shows who was identified for a moment, then closes.
// "Identified: <name>" shows for only 700 ms, which a loaded machine can miss
// between polls, so the page records every text the window shows.
async function recordBankWindowText(page: Page) {
  await page.evaluate(() => {
    const seen: string[] = []
    ;(window as unknown as { __bankTexts: string[] }).__bankTexts = seen
    new MutationObserver(() => {
      const dialog = document.querySelector('[role="dialog"]')
      if (dialog?.textContent) seen.push(dialog.textContent)
    }).observe(document.body, { subtree: true, childList: true, characterData: true })
  })
}
const bankWindowTexts = (page: Page) => page.evaluate(() => (window as unknown as { __bankTexts?: string[] }).__bankTexts || [])

async function passBankWindow(page: Page, check?: (dialog: ReturnType<Page['getByRole']>) => Promise<void>) {
  const dialog = page.getByRole('dialog', { name: 'Bank identification' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('Simulation: no real bank or identity service is contacted.')).toBeVisible()
  if (check) await check(dialog)
  await recordBankWindowText(page)
  await dialog.getByRole('button', { name: 'Identify with bank credentials (simulated)' }).click()
  await expect(dialog).toHaveCount(0)
  expect((await bankWindowTexts(page)).some(text => /Identified: \S/.test(text))).toBe(true)
}

async function navigate(page: Page, name: string) {
  const nav = page.getByRole('navigation', { name: 'Console views' })
  await nav.getByRole('button', { name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }).click()
  await expect(page.locator('.topbar strong')).toHaveText(name)
}

async function sendRequest(page: Page, message = hotelRequest) {
  await navigate(page, 'Chat')
  await page.getByRole('textbox', { name: 'Your purchasing request' }).fill(message)
  await page.getByRole('button', { name: 'Send request', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Reset demo', exact: true })).toBeEnabled()
  await expect(page.locator('.outcome-card')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
}

async function smallRenderedText(page: Page) {
  return page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    const failures: { text: string, size: number, element: string }[] = []
    let node: Node | null
    while ((node = walker.nextNode())) {
      const text = node.textContent?.trim()
      const parent = node.parentElement
      if (!text || !parent || parent.closest('script,style,noscript,svg,.sr-only')) continue
      const style = getComputedStyle(parent)
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) continue
      const range = document.createRange()
      range.selectNodeContents(node)
      if (![...range.getClientRects()].some(rect => rect.width > 0 && rect.height > 0)) continue
      const size = parseFloat(style.fontSize)
      if (size < 16) failures.push({ text: text.slice(0, 90), size, element: `${parent.tagName.toLowerCase()}.${parent.className}` })
    }
    for (const element of document.querySelectorAll<HTMLElement>('input:not([type=checkbox]),textarea,select')) {
      const style = getComputedStyle(element)
      if (element.getClientRects().length && style.visibility === 'visible' && parseFloat(style.fontSize) < 16) {
        failures.push({ text: element.getAttribute('aria-label') || element.id || element.tagName, size: parseFloat(style.fontSize), element: element.tagName.toLowerCase() })
      }
    }
    return failures
  })
}

test('@J01 Chat remains usable at both narrow widths', async ({ page }) => {
  for (const width of [380, 626]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.goto('/')
    await expect(page.getByText('Door connected', { exact: true })).toBeVisible()
    const heading = page.locator('.welcome h1')
    await expect(heading).toBeVisible()
    await heading.scrollIntoViewIfNeeded()
    await expect(heading).toBeInViewport()
    const requestBox = page.getByRole('textbox', { name: 'Your purchasing request' })
    await expect(requestBox).toBeVisible()
    await requestBox.scrollIntoViewIfNeeded()
    await expect(requestBox).toBeInViewport()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `Horizontal overflow at ${width}px`).toBe(true)
    for (const selector of ['.topbar strong', '.wordmark']) {
    const title = page.locator(selector)
    await title.scrollIntoViewIfNeeded()
    const uncovered = await title.evaluate(element => {
      const rect = element.getBoundingClientRect()
      return [.2, .5, .8].every(fraction => {
        const hit = document.elementFromPoint(rect.left + rect.width * fraction, rect.top + rect.height / 2)
        return hit === element || (!!hit && element.contains(hit))
      })
    })
    expect(uncovered, `The ${selector} title is covered at ${width}px`).toBe(true)
    }
    const picker = page.getByRole('combobox', { name: 'Buying for' })
    await expect(picker).toBeVisible()
    for (const button of await page.getByRole('navigation', { name: 'Console views' }).getByRole('button').all()) {
      const rect = await button.boundingBox()
      expect(rect!.x).toBeGreaterThanOrEqual(0)
      expect(rect!.x + rect!.width).toBeLessThanOrEqual(width)
    }
    await page.screenshot({ path: `../data/verification/chat-${width}px.png`, fullPage: true })
  }
})

test('@J02 every rendered text label and table cell is at least 16px', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await sendRequest(page)
  for (const name of views) {
    await navigate(page, name)
    // Include expanded limit explanations, signed documents and record details.
    for (const details of await page.locator('details').all()) {
      if (await details.isVisible()) await details.evaluate(element => element.setAttribute('open', ''))
    }
    expect(await smallRenderedText(page), `Text smaller than 16px in ${name}`).toEqual([])
  }
  await navigate(page, 'Chat')
  await page.getByRole('checkbox', { name: /Simulate limited stock/ }).check()
  await sendRequest(page)
  await navigate(page, 'Sign-off')
  expect(await smallRenderedText(page), 'Held-order reasons and sign-off labels must also be legible').toEqual([])
  await page.getByRole('combobox', { name: 'Buying for' }).selectOption('restaurant')
  await navigate(page, 'Limits view')
  for (const details of await page.locator('details').all()) {
    if (await details.isVisible()) await details.evaluate(element => element.setAttribute('open', ''))
  }
  expect(await smallRenderedText(page), 'Restaurant proposals and synthetic history must also be legible').toEqual([])
})

test('@J03 navigation marks exactly the opened view active', async ({ page }) => {
  const nav = page.getByRole('navigation', { name: 'Console views' })
  for (const name of views) {
    await navigate(page, name)
    const active = nav.locator('button[aria-current="page"]')
    await expect(active).toHaveCount(1)
    await expect(active).toContainText(name)
    // The nav colours have a 0.2s CSS transition; compare the settled colours.
    await active.evaluate(element => Promise.all([...element.parentElement!.querySelectorAll('button')].flatMap(button => button.getAnimations()).map(animation => animation.finished)))
    const colours = await active.evaluate(element => {
      const normal = [...element.parentElement!.querySelectorAll('button')].find(button => button !== element)!
      const selectedStyle = getComputedStyle(element)
      const normalStyle = getComputedStyle(normal)
      return { selected: `${selectedStyle.backgroundColor}|${selectedStyle.color}|${selectedStyle.borderColor}`, normal: `${normalStyle.backgroundColor}|${normalStyle.color}|${normalStyle.borderColor}` }
    })
    expect(colours.selected).not.toBe(colours.normal)
  }
  await navigate(page, 'Chat')
  await page.getByRole('button', { name: 'See all limits', exact: true }).click()
  await expect(nav.locator('[aria-current="page"]')).toContainText('Limits view')
  await page.goto('/?view=signoff')
  await expect(page.locator('.topbar strong')).toHaveText('Sign-off')
  await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
  await expect(nav.locator('[aria-current="page"]')).toContainText('Sign-off')
})

test('@J04 primary colour is blue on light surfaces without trademark assets', async ({ page }) => {
  const fontResponses: { url: string, status: number }[] = []
  page.on('response', response => { if (response.url().includes('.woff2')) fontResponses.push({ url: response.url(), status: response.status() }) })
  await page.route('**/*', async route => {
    if (route.request().resourceType() === 'document') {
      const response = await route.fetch()
      await route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:" } })
    } else await route.continue()
  })
  await page.goto('/')
  await expect(page.getByText('Door connected', { exact: true })).toBeVisible()
  const fonts = await page.evaluate(async () => {
    const loaded = await Promise.all([document.fonts.load('400 16px Inter'), document.fonts.load('500 24px "Space Grotesk"'), document.fonts.load('400 30px Lobster')])
    await document.fonts.ready
    return loaded.map(faces => faces.map(face => ({ family: face.family, status: face.status })))
  })
  expect(fonts.map(faces => faces.length)).toEqual([1, 1, 1])
  expect(fonts.flat().every(face => face.status === 'loaded')).toBe(true)
  expect(fontResponses.length).toBeGreaterThanOrEqual(3)
  expect(fontResponses.every(response => response.status === 200 && new URL(response.url).origin === new URL(page.url()).origin)).toBe(true)

  const colours = await page.locator('.button.primary').first().evaluate(element => {
    const primary = getComputedStyle(element).backgroundColor
    const body = getComputedStyle(document.body).backgroundColor
    return { primary, body }
  })
  function hsl(value: string) {
    const components = value.match(/[\d.]+/g)?.slice(0, 3).map(Number)
    expect(components).toHaveLength(3)
    const [r, g, b] = components!.map(component => component / 255)
    const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min
    const hue = delta === 0 ? 0 : (max === r ? (g - b) / delta + (g < b ? 6 : 0) : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4) * 60
    return { hue, lightness: (max + min) / 2 }
  }
  expect(hsl(colours.primary).hue).toBeGreaterThanOrEqual(185)
  expect(hsl(colours.primary).hue).toBeLessThanOrEqual(235)
  expect(hsl(colours.body).lightness).toBeGreaterThanOrEqual(.94)
  await expect(page.locator('img,image')).toHaveCount(0)
  const assets = fs.readdirSync(path.resolve('dist/assets'))
  expect(assets.filter(name => /(?:valio|aimo).*(?:logo|wordmark|trademark)|(?:logo|wordmark|trademark).*(?:valio|aimo)/i.test(name))).toEqual([])
  expect(assets.filter(name => /\.(?:png|jpe?g|gif|webp|avif|ico|svg)$/i.test(name))).toEqual([])
  const bundle = assets.filter(name => /\.(?:js|css)$/.test(name)).map(name => fs.readFileSync(path.resolve('dist/assets', name), 'utf8')).join('\n')
  expect(bundle).not.toMatch(/fonts\.googleapis\.com|fonts\.gstatic\.com/)
  expect(bundle).not.toMatch(/(?:src|url|href)\s*[:=(][^;\n]{0,200}(?:valio|aimo)[^;\n]{0,100}(?:logo|wordmark|trademark)/i)
})

test('@J07 budget row exposes used and remaining amounts after a real HTTP commit', async ({ page, request }) => {
  await navigate(page, 'Limits view')
  const budgetRow = page.locator('.limit-row').filter({ hasText: 'Monthly budget' })
  await budgetRow.locator('summary').click()
  const hotel = await buyerHeaders(request, 'hotel')
  const baseline = await (await request.get('/limits?customer_id=hotel', { headers: hotel })).json()
  const oldBudget = baseline.rows.find((row: { id: string }) => row.id === 'monthly_budget')
  expect(typeof oldBudget.used).toBe('number')
  expect(typeof oldBudget.remaining).toBe('number')
  await expect(budgetRow).toContainText(/used/i)
  await expect(budgetRow).toContainText(/remaining/i)
  const beforeText = await budgetRow.innerText()
  await sendRequest(page)
  await navigate(page, 'Limits view')
  await budgetRow.locator('summary').click()
  const current = await (await request.get('/limits?customer_id=hotel', { headers: hotel })).json()
  const budget = current.rows.find((row: { id: string }) => row.id === 'monthly_budget')
  expect(budget.used).toBeGreaterThan(oldBudget.used)
  expect(budget.remaining).toBeLessThan(oldBudget.remaining)
  expect(budget.used + budget.remaining).toBeCloseTo(budget.signed_ceiling, 2)
  await expect(budgetRow).not.toHaveText(beforeText)
  const text = await budgetRow.innerText()
  for (const value of [budget.used, budget.remaining]) {
    expect(text.replace(/[\s,\u00a0\u202f]/g, '')).toContain(value.toFixed(2))
  }
})

test('@K10 an approved hold shows as committed without "Why this needs you" or "not committed"', async ({ page }) => {
  await page.getByRole('checkbox', { name: /Simulate limited stock/ }).check()
  await sendRequest(page)
  await navigate(page, 'Sign-off')
  await expect(page.getByRole('heading', { name: 'This order needs your sign-off.' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Why this needs you' })).toBeVisible()
  await expect(page.getByText('Stock reserved, not committed', { exact: true })).toBeVisible()
  // The fixture hold is a delivery trade-off, which the buyer answers on this view.
  await page.getByRole('combobox', { name: 'Choose an answer' }).selectOption('approve')
  await page.getByRole('button', { name: 'Review answer summary' }).click()
  await page.getByRole('checkbox', { name: 'I confirm the displayed answer and its exact scope.' }).check()
  const approval = page.waitForResponse(response => response.url().endsWith('/signoff') && response.request().postDataJSON()?.action === 'approve')
  await page.getByRole('button', { name: 'Confirm answer' }).click()
  await passBankWindow(page)
  // The API answers committed but still carries the failed check.
  const approved = await (await approval).json()
  const order = approved.order || approved
  expect(order.status).toBe('committed')
  expect(order.checks.some((check: { passed: boolean }) => !check.passed)).toBe(true)
  await expect(page.getByRole('heading', { name: 'Your order is committed.' })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByText('Why this needs you')).toHaveCount(0)
  await expect(page.getByText(/not committed/i)).toHaveCount(0)
  const summary = page.getByRole('region', { name: 'What you approved' })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText(order.checks.find((check: { passed: boolean }) => !check.passed).reason)
})

test('@K11 the Record view reveals model probabilities from options_considered lines, one click behind', async ({ page }) => {
  // The headless fixture has no decision model, so its record lines carry no
  // probabilities: the view must then show nothing rather than an empty box.
  await sendRequest(page)
  await navigate(page, 'Record')
  await expect(page.getByRole('heading', { name: 'What happened' })).toBeVisible()
  await expect(page.getByText('How sure the model was about product types')).toHaveCount(0)

  // Then the record the API actually sends, with probabilities set on its own
  // options_considered[].lines[] the way the decision model stores them.
  let shapeSeen = false
  await page.route('**/records/*', async route => {
    const response = await route.fetch()
    const record = await response.json()
    expect(record).not.toHaveProperty('probabilities')
    expect(Array.isArray(record.options_considered)).toBe(true)
    const first = record.options_considered[0]
    expect(Array.isArray(first.lines)).toBe(true)
    first.lines[0].probabilities = { 'drinking milk': 0.9, 'sour milk': 0.07, cream: 0.03 }
    shapeSeen = true
    await route.fulfill({ response, json: record })
  })
  await sendRequest(page)
  await navigate(page, 'Record')
  const summary = page.getByText('How sure the model was about product types')
  await expect(summary).toBeVisible()
  expect(shapeSeen).toBe(true)
  const disclosure = page.locator('details', { has: summary })
  await expect(disclosure).not.toHaveAttribute('open', '')
  await expect(disclosure.getByText(/drinking milk 90%/)).toBeHidden()
  await summary.click()
  await expect(disclosure.getByText(/drinking milk 90%, sour milk 7%, cream 3%\./)).toBeVisible()
})

test('@L09 the Chat view keeps a conversation: reply, products, draft placed through execute', async ({ page, request }) => {
  // The fixture server does not implement stage "converse" yet: this test answers
  // converse turns itself and lets every other stage reach the real door.
  const orderText = 'Order 200 litres of lactose-free milk by Monday'
  const preview = await (await request.post('/bot/chat', { data: { stage: 'preview', customer_id: 'hotel', message: orderText }, headers: await buyerHeaders(request, 'hotel') })).json()
  expect(preview.need).toBeTruthy()
  const products = [
    { product_id: 'fixture-milk-10', name: 'Fixture lactose-free milk 10 L', pack: '1 × 10 L', price_eur: 12, unit_price: '€1.20 / L', availability: 'in_stock', available_text: 'In stock for this date', properties: ['lactose-free'] },
    { product_id: 'fixture-milk-1', name: 'Fixture lactose-free milk 1 L', pack: '12 × 1 L', price_eur: 15.6, unit_price: '€1.30 / L', availability: 'limited', available_text: 'Only 30 packs for this date', properties: ['lactose-free', 'UHT'] },
  ]
  const converse: { message: string, history: { role: string, text: string, action?: string, products?: unknown[] }[] }[] = []
  await page.route('**/bot/chat', async route => {
    const body = route.request().postDataJSON()
    if (body?.stage !== 'converse') return route.fallback()
    converse.push(body)
    const text = String(body.message)
    const answer = /^hi/i.test(text)
      ? { action: 'reply', reply: 'I can show what Aimo sells, check price and stock, and place orders inside your signed limits.' }
      : /what lactose-free milk/i.test(text)
        ? { action: 'products', reply: 'These lactose-free milks can be delivered on Monday.', delivery_date: '2026-11-16', products }
        : { action: 'draft', reply: 'Here is a draft. Shall I place it?', draft: { need: preview.need, restatement: preview.restatement, product_name: products[0].name, pack: products[0].pack, packs: 20, total_eur: 240, delivery_date: '2026-11-16' } }
    await route.fulfill({ json: { stage: 'conversation', model: {}, ...answer } })
  })
  const box = page.getByRole('textbox', { name: 'Your purchasing request' })
  async function say(text: string) {
    await box.fill(text)
    await page.getByRole('button', { name: 'Send request', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Reset demo', exact: true })).toBeEnabled()
  }
  await expect(page.getByRole('button', { name: /What lactose-free milk do you have\?/ })).toBeVisible()

  await say('Hi, what can you help me with?')
  await expect(page.getByText(/I can show what Aimo sells/)).toBeVisible()
  await expect(page.locator('.outcome-card')).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Console views' }).locator('.nav-count')).toHaveCount(0)

  await say('What lactose-free milk do you have?')
  const list = page.getByRole('region', { name: 'Products' })
  await expect(list.getByRole('listitem')).toHaveCount(2)
  await expect(list).toContainText('Prices and stock for delivery Mon 16 Nov')
  await expect(list.getByRole('listitem').first()).toContainText('€12.00 per pack')
  await expect(list.getByRole('listitem').nth(1)).toContainText('Only 30 packs for this date')
  await expect(page.locator('.outcome-card')).toHaveCount(0)
  if (process.env.AIMOS_CONVERSATION_SHOTS) await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/1440-products.png`, fullPage: true })

  await say(orderText)
  // The transcript persists, and the later request carries the earlier turns.
  for (const text of ['Hi, what can you help me with?', 'What lactose-free milk do you have?', orderText]) await expect(page.getByText(text, { exact: true })).toBeVisible()
  expect(converse).toHaveLength(3)
  expect(converse[0].history).toEqual([])
  expect(converse[2].history.map(turn => [turn.role, turn.action])).toEqual([['user', undefined], ['assistant', 'reply'], ['user', undefined], ['assistant', 'products']])
  expect(converse[2].history[3].products).toEqual(products.map(({ product_id, name }) => ({ product_id, name })))
  const card = page.getByRole('region', { name: 'Draft order' })
  await expect(card).toContainText(preview.restatement)
  await expect(card).toContainText('€240.00')
  await expect(page.locator('.outcome-card')).toHaveCount(0)
  if (process.env.AIMOS_CONVERSATION_SHOTS) {
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/1440-draft.png` })
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/1440-transcript.png`, fullPage: true })
    await page.setViewportSize({ width: 380, height: 800 })
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/380-transcript-draft.png`, fullPage: true })
    await list.scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/380-products.png` })
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/380-draft.png` })
    await page.setViewportSize({ width: 1440, height: 1000 })
  }

  const execute = page.waitForRequest(r => r.url().endsWith('/bot/chat') && r.postDataJSON()?.stage === 'execute')
  await card.getByRole('button', { name: 'Place this order' }).click()
  const sent = (await execute).postDataJSON()
  expect(sent).toMatchObject({ stage: 'execute', customer_id: 'hotel', need: preview.need, restatement: preview.restatement, message: orderText, simulate_limited_stock: false })
  await expect(page.locator('.outcome-card')).toBeVisible()
  await expect(page.locator('.outcome-card h3')).toHaveText('Order committed')
  await expect(card.getByRole('button', { name: 'Place this order' })).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  if (process.env.AIMOS_CONVERSATION_SHOTS) {
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/1440-transcript-placed.png`, fullPage: true })
    await page.setViewportSize({ width: 380, height: 800 })
    await page.screenshot({ path: `${process.env.AIMOS_CONVERSATION_SHOTS}/380-transcript-placed.png`, fullPage: true })
  }
})

test('@M05 a signed limit changes only through value, simulated identification and the buyer\'s signature', async ({ page, request }) => {
  // The fixture door has no /demo/identify or /limits/{id}/change yet: answer them here.
  const shots = process.env.AIMOS_MANDATE_SHOTS
  const hotel = await buyerHeaders(request, 'hotel')
  const identity = { proof: 'proof-m05', expires_at: new Date(Date.now() + 300_000).toISOString(), simulated: true, method: 'Simulated bank identification', person: { name: 'Aino Esimerkki' }, authority: 'May sign for Hotel chain (simulated company authorisation)' }
  const identifyBodies: unknown[] = []
  const changes: { body: unknown, proof: string | null, session: string | null }[] = []
  await page.route('**/demo/identify', async route => { identifyBodies.push(route.request().postDataJSON()); await route.fulfill({ json: identity }) })
  await page.route('**/limits/*/change', async route => {
    const body = route.request().postDataJSON()
    changes.push({ body, proof: await route.request().headerValue('x-identification-proof'), session: await route.request().headerValue('x-buyer-session') })
    const limits = await (await request.get('/limits?customer_id=hotel', { headers: hotel })).json()
    for (const row of limits.rows) if (row.id === 'per_order_cap') { row.signed_ceiling = body.value; row.current_freedom = body.value }
    await route.fulfill({ json: { limits, change: { limit_id: 'per_order_cap', name: 'Spend per order', old_value: 2000, new_value: body.value, signed_by: identity.person.name, identified_by: identity.method, at: new Date().toISOString() }, mandate_version: 2, record_id: 'mandate-change-m05' } })
  })
  await page.route('**/records/mandate-change-m05', route => route.fulfill({ json: { id: 'mandate-change-m05', outcome: 'mandate changed', approved_by: identity.person.name, sentences: ['The buyer raised the per-order spend cap from €2,000.00 to €3,000.00.'], checks: [], hash: 'hash-m05', previous_hash: 'hash-before', created_at: new Date().toISOString() } }))

  for (const width of [1440, 380]) {
    await page.setViewportSize({ width, height: width === 380 ? 800 : 1000 })
    await page.goto('/')
    await expect(page.getByText('Door connected', { exact: true })).toBeVisible()
    await navigate(page, 'Limits view')
    const changeButtons = page.getByRole('button', { name: 'Change this limit' })
    await expect(changeButtons).toHaveCount(2)
    const flow = page.getByRole('region', { name: 'Change Spend per order' })

    // Cancel at step 1 changes nothing.
    await changeButtons.first().click()
    await flow.getByRole('spinbutton').fill('2500')
    await flow.getByRole('button', { name: 'Cancel' }).click()
    await expect(flow).toHaveCount(0)

    await changeButtons.first().click()
    await expect(flow.getByRole('button', { name: 'Continue' })).toBeDisabled()
    await flow.getByRole('spinbutton').fill('3000')
    expect(await smallRenderedText(page), `Text smaller than 16px in the flow at ${width}px`).toEqual([])
    if (shots) await flow.screenshot({ path: `${shots}/${width}-step1-new-value.png` })
    await flow.getByRole('button', { name: 'Continue' }).click()
    // Step 2 is the mock bank window (M06).
    const dialog = page.getByRole('dialog', { name: 'Bank identification' })
    await expect(dialog.getByText('Simulation: no real bank or identity service is contacted.')).toBeVisible()
    expect(await smallRenderedText(page), `Text smaller than 16px in the flow at ${width}px`).toEqual([])
    if (shots) await dialog.screenshot({ path: `${shots}/${width}-step2-identify.png` })
    await recordBankWindowText(page)
    await dialog.getByRole('button', { name: 'Identify with bank credentials (simulated)' }).click()
    await expect(dialog).toHaveCount(0)
    const shown = await bankWindowTexts(page)
    expect(shown.some(text => text.includes('Identified: Aino Esimerkki') && text.includes(identity.authority))).toBe(true)
    await expect(flow.getByText('Spend per order: €2,000.00 → €3,000.00')).toBeVisible()
    expect(await smallRenderedText(page), `Text smaller than 16px in the flow at ${width}px`).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `Horizontal overflow at ${width}px`).toBe(true)
    if (shots) await flow.screenshot({ path: `${shots}/${width}-step3-review.png` })
    await flow.getByRole('button', { name: 'Sign the new limit' }).click()
    await expect(page.getByText('Limit changed. Your new mandate version is signed.')).toBeVisible()
    await expect(flow).toHaveCount(0)
    const capRow = page.locator('.limit-row').filter({ hasText: 'Spend per order' })
    await expect(capRow).toContainText('€3,000.00')
    await expect(page.locator('.mandate-stat').filter({ hasText: 'Per-order spend cap' })).toContainText('€3,000.00')
    if (shots) await page.screenshot({ path: `${shots}/${width}-changed-row.png`, fullPage: true })
    await page.getByRole('button', { name: 'See this change on the record' }).click()
    await expect(page.locator('.topbar strong')).toHaveText('Record')
    await expect(page.getByText('The buyer raised the per-order spend cap from €2,000.00 to €3,000.00.')).toBeVisible()
  }
  expect(identifyBodies).toEqual([{ customer_id: 'hotel', method: 'bank' }, { customer_id: 'hotel', method: 'bank' }])
  expect(changes).toHaveLength(2)
  for (const change of changes) {
    expect(change.body).toEqual({ customer_id: 'hotel', value: 3000 })
    expect(change.proof).toBe('proof-m05')
    expect(change.session).toBeTruthy()
  }
})

test('@M06 approval and limit change pass a mock bank window, against the real door', async ({ page }) => {
  const shots = process.env.AIMOS_BANK_SHOTS
  for (const width of [1440, 380]) {
    await page.setViewportSize({ width, height: width === 380 ? 800 : 1000 })
    if (width !== 1440) {
      // Reset demo clears the session identification, so the window opens again.
      await page.getByRole('button', { name: 'Reset demo', exact: true }).click()
      await expect(page.getByText(/Demo reset\./)).toBeVisible()
    }
    await expect(page.getByText(/Identified for this session/)).toHaveCount(0)
    // A held order: approve it through the window.
    await page.getByRole('checkbox', { name: /Simulate limited stock/ }).check()
    await sendRequest(page)
    await navigate(page, 'Sign-off')
    await page.getByRole('combobox', { name: 'Choose an answer' }).selectOption('approve')
    await page.getByRole('button', { name: 'Review answer summary' }).click()
    await page.getByRole('checkbox', { name: 'I confirm the displayed answer and its exact scope.' }).check()
    await page.getByRole('button', { name: 'Confirm answer' }).click()
    await passBankWindow(page, async dialog => {
      await expect(dialog).toHaveAttribute('aria-modal', 'true')
      await expect(dialog.getByText(/^Approve this order: €[\d,.]+, delivery /)).toBeVisible()
      await expect(dialog.getByRole('button', { name: 'Identify with bank credentials (simulated)' })).toBeFocused()
      expect(await smallRenderedText(page), `Text smaller than 16px with the window open at ${width}px`).toEqual([])
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `Horizontal overflow at ${width}px`).toBe(true)
      if (shots) await page.screenshot({ path: `${shots}/${width}-approval-window.png` })
    })
    await expect(page.getByRole('heading', { name: 'Your order is committed.' })).toBeVisible()
    await expect(page.getByText(/Identified for this session: .+ \(simulated\)/)).toHaveCount(1)
    expect(await page.evaluate(() => sessionStorage.getItem('aimo-door-identified:hotel'))).toBeTruthy()

    // A limit change always identifies afresh; its proof goes to the real /limits/{id}/change.
    await navigate(page, 'Limits view')
    await page.getByRole('button', { name: 'Change this limit' }).first().click()
    const flow = page.getByRole('region', { name: 'Change Spend per order' })
    await flow.getByRole('spinbutton').fill('3000')
    await flow.getByRole('button', { name: 'Continue' }).click()
    await passBankWindow(page, async dialog => {
      await expect(dialog.getByText('Spend per order: €2,000.00 → €3,000.00')).toBeVisible()
      expect(await smallRenderedText(page), `Text smaller than 16px with the window open at ${width}px`).toEqual([])
      if (shots) await page.screenshot({ path: `${shots}/${width}-limit-window.png` })
    })
    const signed = page.waitForResponse(response => response.url().endsWith('/limits/per_order_cap/change'))
    await flow.getByRole('button', { name: 'Sign the new limit' }).click()
    expect((await signed).status()).toBe(200)
    await expect(page.getByText('Limit changed. Your new mandate version is signed.')).toBeVisible()
    await expect(page.locator('.limit-row').filter({ hasText: 'Spend per order' })).toContainText('€3,000.00')
    await expect(page.getByRole('alert')).toHaveCount(0)
  }
})

test('@N04 a refused sign-off says why in place, and an undeliverable hold can only be rejected', async ({ page }) => {
  const reason = 'No later fully available offer with an open order cutoff is currently available.'
  let refusals = 0
  await page.route('**/orders/*/signoff', async route => {
    if (route.request().postDataJSON()?.action !== 'approve') return route.fallback()
    refusals += 1
    await route.fulfill({ status: 409, json: { detail: { message: reason, earliest_orderable_date: null } } })
  })
  await page.getByRole('checkbox', { name: /Simulate limited stock/ }).check()
  await sendRequest(page)
  await navigate(page, 'Sign-off')
  await page.getByRole('combobox', { name: 'Choose an answer' }).selectOption('approve')
  await page.getByRole('button', { name: 'Review answer summary' }).click()
  const confirm = async () => {
    await page.getByRole('checkbox', { name: 'I confirm the displayed answer and its exact scope.' }).check()
    await page.getByRole('button', { name: 'Confirm answer' }).click()
  }
  await confirm()
  await passBankWindow(page)
  const refusal = page.locator('.question-panel').getByRole('alert')
  await expect(refusal).toHaveText(reason)
  await expect(refusal).toBeInViewport()
  expect(refusals).toBe(1)
  // The controls stay usable: a second confirmation reaches the door again.
  await confirm()
  await expect.poll(() => refusals).toBe(2)
  await expect(refusal).toHaveText(reason)
  expect(await smallRenderedText(page)).toEqual([])

  // The same held order, as the door reports it once it cannot be delivered.
  await page.unroute('**/orders/*/signoff')
  const needed = 'No delivery date with the full quantity is available before the order cutoff.'
  await page.route(/\/orders\/[^/]+$/, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const order = await response.json()
    order.approvable = false
    order.question_spec = { ...order.question_spec, needed, answers: order.question_spec.answers.filter((answer: { value: string }) => answer.value === 'reject') }
    await route.fulfill({ response, json: order })
  })
  await navigate(page, 'Chat')
  await navigate(page, 'Sign-off')
  await expect(page.getByRole('heading', { name: 'This order cannot be delivered.' })).toBeVisible()
  await expect(page.getByText('It is on hold. You can reject it, or ask your agent for another product or date.')).toBeVisible()
  await expect(page.locator('.cannot-deliver')).toHaveText(needed)
  await expect(page.getByRole('heading', { name: 'Reject this order' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Approve order' })).toHaveCount(0)
  await expect(page.getByRole('combobox', { name: 'Choose an answer' }).locator('option')).toHaveText([/^Reject this order/])
  await expect(page.getByRole('spinbutton', { name: 'Edit requested quantity' })).toBeVisible()
  await page.getByRole('button', { name: 'Reject order' }).click()
  await expect(page.getByRole('heading', { name: 'This order is closed.' })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})
