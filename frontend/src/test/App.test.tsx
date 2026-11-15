import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// The session identification is mirrored in sessionStorage; each test starts clean.
beforeEach(() => window.sessionStorage.clear())
import App from '../App'
import { money, type Json } from '../api'

const need = { product_type: 'drinking milk', properties: ['lactose-free'], quantity: 1000, unit: 'L', deadline: '2026-11-16' }
const restatement = 'I will find 1,000 litres of lactose-free drinking milk for delivery by 2026-11-16.'
const checks = Array.from({ length: 10 }, (_, index) => ({ id: index + 1, name: `Rule ${index + 1}`, passed: true, reason: `The buyer’s rule ${index + 1} passes.` }))
const line = { id: 'milk-1', gtin: '06400000000001', name: 'Lactose-free whole milk', category: '10400', properties: { 'lactose-free': 'manufacturer' }, product_type: 'drinking milk', product_type_source: 'inferred', origin: 'fi', labels: ['FINNISH'], packs: 100, pack_size: 10, quantity: 1000, unit: 'L', unit_price: 12, total: 1200, delivery_date: '2026-11-16' }
const option = { label: 'best', lines: [line], total: 1200, overage: 0, on_time: true, delivery_date: '2026-11-16' }
const offer = { offer: 'signed.offer.fixture', payload: { ...option, exp: 1794862800 } }
const committedOrder = { id: 'order-1', customer_id: 'hotel', status: 'committed', tier: 'Commit', checks, receipt: 'signed.receipt.fixture', record_id: 'record-1', offer: offer.offer, offer_payload: offer.payload }
const heldOrder = { ...committedOrder, status: 'requires_escalation', tier: 'Hold', receipt: null, continue_url: '/?view=signoff&order=order-1', checks: checks.map(check => check.id === 6 ? { ...check, passed: false, reason: 'The total is above the signed per-order spend cap.' } : check) }
const customers = [{ id: 'hotel', name: 'Hotel chain', mandate: 'signed.hotel.mandate', bot_id: 'reference-bot' }, { id: 'restaurant', name: 'Restaurant', mandate: 'signed.restaurant.mandate', bot_id: 'reference-bot' }]
const limitRows = [{ id: 'per_order_cap', name: 'Spend per order', signed_ceiling: 2000, current_freedom: 2000, status: 'Buyer signed', reason: 'Only the buyer may raise a signed ceiling.' }, { id: 'monthly_budget', name: 'Monthly budget', signed_ceiling: 20000, current_freedom: 20000, status: 'Buyer signed', reason: 'The budget counts door orders.' }]

type Call = { path: string; method: string; body?: Json; headers: Headers }
const preferenceQuestion = { kind: 'preference', needed: 'May your agent use this offered milk brand?', product_type: 'drinking milk', preferences: [{ product_id: 'milk-1', name: 'brand', requested: 'Preferred', offered: 'Alternative' }], answers: [{ value: 'approve', label: 'Accept the displayed order', cost: { price: 1200, date: '2026-11-16' } }, { value: 'reject', label: 'Reject this order', cost: { price: 0, date: 'No delivery' } }], scope_options: ['just_this_once', 'always', 'always_ask'], scope_note: 'Only drinking milk and the displayed brand.', hash: 'question-hash' }
const exactSummary = 'Approve: remember only for drinking milk: brand=Alternative.'
const usualLines = [
  { ...line, packs: 5, pack_size: 5, quantity: 25, unit_price: 6, total: 30, cutoff: '2099-11-16T12:00:00Z' },
  { ...line, id: 'rice-1', name: 'Dry long-grain rice', product_type: 'rice', properties: {}, packs: 5, pack_size: 5, quantity: 25, unit: 'KG', unit_price: 5, total: 25, cutoff: '2099-11-16T12:00:00Z' },
  { ...line, id: 'pasta-1', name: 'Dry pasta', product_type: 'pasta', properties: {}, packs: 5, pack_size: 1, quantity: 5, unit: 'KG', unit_price: 2, total: 10, cutoff: '2099-11-16T12:00:00Z' },
]
const weeklyHistory = Array.from({ length: 12 }, (_, index) => ({ id: `synthetic-${index}`, date: new Date(Date.UTC(2026, 7, 24 + index * 7)).toISOString().slice(0, 10), lines: usualLines, total: 65, synthetic: true }))
const usualBasket = { customer_id: 'restaurant', lines: usualLines, history: weeklyHistory, synthetic: true, label: 'Synthetic order history: 12 weekly orders; simulated buyer-confirmed usual basket.', anchor_id: 'confirmed-anchor', anchor_confirmed: true }
const reorderNeed = { ...need, product_type: 'usual basket', quantity: 1, unit: 'PACK', properties: [], anchor_id: 'confirmed-anchor', lines: usualLines.map((item, index) => ({ product_id: item.id, name: item.name, product_type: item.product_type, quantity: index === 0 ? 30 : item.packs, unit: index === 0 ? 'L' : 'PACK', properties: index === 0 ? ['lactose-free'] : [] })) }
const reorderRestatement = 'I will reorder the confirmed usual basket with 5 litres more lactose-free milk; the other lines stay unchanged. The history is synthetic.'
const reorderOption = { ...option, total: 71, lines: usualLines.map((item, index) => index === 0 ? { ...item, packs: 6, quantity: 30, total: 36 } : item) }
const notifyOrder = { ...committedOrder, customer_id: 'restaurant', tier: 'Notify', notice: 'Approval by silence. Your milk increase exceeds 10% freedom but stays within the 30% ceiling.', offer_payload: { ...reorderOption, need: reorderNeed } }
type MockOptions = { budget?: Json | (() => Json); hold?: boolean; order?: Json; learned?: boolean; restaurant?: boolean; anchorProposal?: boolean; expireOnApprove?: boolean; preview?: Json; previewStatus?: number; onExecute?: () => void; matchOptions?: Json[]; recordOptions?: Json[]; buyerSession?: boolean; expiredTokens?: Set<string>; idle?: { armed: boolean; resetAt?: string | null }; converse?: (body: Json, order: Json) => Json; mandate?: { identify?: (body: Json, headers: Headers) => [number, Json]; change?: (path: string, body: Json, headers: Headers) => [number, Json] }; refuse?: (path: string, body: Json) => [number, Json] | undefined; labels?: (customer: string) => Json }

function mockDoor(options: MockOptions = {}) {
  const calls: Call[] = []
  let order: Json = structuredClone(options.order || (options.hold ? heldOrder : committedOrder))
  let expired = false
  let currentFreedom = 0.1
  let proposalPending = true
  let tighteningReason = ''
  let learned = options.learned ? [{ id: 'learned-1', question: 'Which milk brand may the agent use?', answer: 'approve', scope: 'always', product_type: 'drinking milk', confirmation_summary: exactSummary }] : []
  let issued = 0
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname + input.search : input.url
    const method = init?.method || 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ path, method, body, headers: new Headers(init?.headers) })
    if (options.idle?.armed && path !== '/demo/customers') {
      // The first request after an idle auto-reset: the old order no longer exists.
      options.idle.armed = false
      order = structuredClone(committedOrder)
      return new Response(JSON.stringify({ detail: 'Order not found.' }), { status: path.startsWith('/orders/') ? 404 : 200, headers: { 'Content-Type': 'application/json', 'X-Demo-Idle-Reset': '1' } })
    }
    if (options.expiredTokens?.has(new Headers(init?.headers).get('X-Buyer-Session') || '')) {
      return new Response(JSON.stringify({ detail: 'The buyer session has expired.' }), { status: 401, headers: { 'Content-Type': 'application/json' } })
    }
    const refused = options.refuse?.(path, body)
    if (refused) return new Response(JSON.stringify(refused[1]), { status: refused[0], headers: { 'Content-Type': 'application/json' } })
    if (path === '/demo/buyer-session' && method === 'POST') {
      // Without the option this is an older door that has no buyer login yet.
      const session = options.buyerSession ? { token: `buyer-${body.customer_id}-${++issued}`, customer_id: body.customer_id, expires_at: '2026-11-15T12:00:00Z' } : { detail: 'Not Found' }
      return new Response(JSON.stringify(session), { status: options.buyerSession ? 200 : 404, headers: { 'Content-Type': 'application/json' } })
    }
    const baseRows = limitRows.map(row => row.id === 'monthly_budget' && options.budget ? { ...row, ...(typeof options.budget === 'function' ? options.budget() : options.budget) } : row)
    let response: unknown
    let status = 200
    if (path === '/healthz') response = { status: 'ok', products: 17546, fixture: true }
    else if (path === '/demo/customers') response = options.idle && 'resetAt' in options.idle ? { customers, idle_reset_at: options.idle.resetAt } : { customers }
    else if (path.startsWith('/limits?customer_id=')) response = { customer_id: path.split('=').at(-1), rows: options.restaurant && path.endsWith('restaurant') ? [...baseRows, { id: 'departure', name: 'Departure from usual', signed_ceiling: 0.3, current_freedom: currentFreedom, status: tighteningReason ? 'tightened' : currentFreedom === 0.2 ? 'Accepted by buyer' : 'Active', reason: tighteningReason || 'The last human-confirmed usual basket remains the fixed anchor.', kind: 'usual basket' }] : baseRows, proposals: options.restaurant && path.endsWith('restaurant') && proposalPending ? [{ id: 'restaurant-departure-seed', type: options.anchorProposal ? 'anchor' : 'freedom', proposed_freedom: 0.2, proposed_lines: options.anchorProposal ? reorderOption.lines : undefined, description: 'Four clean synthetic approvals support this proposal. The signed ceiling stays 30%.', evidence: weeklyHistory.slice(-4).map(entry => ({ order_id: entry.id, date: entry.date, description: 'A synthetic human approval without correction.', synthetic: true })) }] : [], learned_preferences: learned }
    else if (path === '/demo/identify' && method === 'POST') [status, response] = options.mandate?.identify ? options.mandate.identify(body, new Headers(init?.headers)) : [200, bankIdentity]
    else if (/^\/limits\/[a-z_]+\/change$/.test(path) && method === 'POST') [status, response] = options.mandate?.change ? options.mandate.change(path, body, new Headers(init?.headers)) : [404, { detail: 'Not Found' }]
    else if (path === '/limits/learned-1/reset' && method === 'POST') { learned = []; response = { status: 'reset' } }
    else if (path === '/limits/proposals/restaurant-departure-seed/accept' && method === 'POST') { currentFreedom = options.anchorProposal ? currentFreedom : 0.2; proposalPending = false; response = { status: 'accepted' } }
    else if (path === '/limits/departure/reset' && method === 'POST') { currentFreedom = 0; proposalPending = false; tighteningReason = 'The buyer reset current freedom to zero. The agent must ask next time.'; response = { status: 'reset' } }
    else if (path.startsWith('/usual-order?customer_id=')) response = options.restaurant && path.endsWith('restaurant') ? usualBasket : { lines: [], history: [], synthetic: false }
    else if (path === '/demo/reset' && method === 'POST') { order = structuredClone(options.order || (options.hold ? heldOrder : committedOrder)); response = { status: 'reset' } }
    else if (path === '/bot/chat' && body.stage === 'converse') {
      // Without the option this is a door that predates stage "converse".
      if (options.converse) response = options.converse(body, order)
      else { response = { detail: 'Stage must be preview, execute, all, interview, answer, or confirm.' }; status = 422 }
    }
    else if (path === '/bot/chat' && body.stage === 'preview') { response = options.preview || { restatement, need }; status = options.previewStatus || 200 }
    else if (path === '/bot/chat' && body.stage === 'execute') {
      options.onExecute?.()
      response = { need: body.need, restatement: body.restatement, match: { filters_applied: ['product type: drinking milk', 'lactose-free: manufacturer stated'], options: options.matchOptions || [option] }, offer, order, messages: [{ kind: 'outcome', text: 'The order has been checked.' }] }
    } else if (path === '/bot/chat' && body.stage === 'interview') {
      response = { question: 'Would the alternative brand work for your kitchen, at the displayed price and date?', question_spec: order.question_spec, accepted_answers: order.question_spec.answers, scope_options: order.question_spec.scope_options }
    } else if (path === '/bot/chat' && body.stage === 'answer') {
      const pending = { answer: 'approve', scope: body.scope, confirmation_summary: exactSummary, question_hash: order.question_spec.hash }
      order.pending_answer = pending
      response = { confirmation_summary: exactSummary, pending_answer: pending }
    } else if (path === '/bot/chat' && body.stage === 'confirm') {
      order = { ...order, status: 'committed', receipt: 'signed.interview.receipt', tier: 'Commit' }
      response = { order, messages: [{ text: 'Your confirmed answer was recorded and the door committed the order.' }] }
    } else if (path === '/orders/order-1' && method === 'GET') response = order
    else if (path === '/orders/order-1/undo' && method === 'POST') {
      currentFreedom = 0; proposalPending = false; tighteningReason = 'The buyer used undo. Current freedom is zero and the agent must ask next time.'
      order = { ...order, status: 'withdrawn', receipt: null, reserved: [] }; response = order
    }
    else if (path === '/orders/order-1/signoff' && method === 'POST') {
      if (body.action === 'acknowledge') {
        order = { ...order, acknowledged: true }; response = order
      } else if (body.action === 'prepare_answer') {
        order = { ...order, pending_answer: { answer: body.answer, scope: body.scope, confirmation_summary: exactSummary, question_hash: order.question_spec.hash }, confirmation_summary: exactSummary }
        response = order
      } else if (options.expireOnApprove && !expired && body.action === 'approve') {
        expired = true
        order = { ...order, offer: 'signed.fresh.offer', offer_payload: { ...order.offer_payload, total: 1300 }, requires_price_confirmation: true }
        response = { detail: 'The offer expired. Review its current price.' }
        status = 409
      } else {
        if (body.action === 'edit' && options.restaurant) { currentFreedom = 0; proposalPending = false; tighteningReason = 'The buyer edited this kind of order. The agent must ask next time.' }
        order = { ...order, status: body.action === 'reject' ? 'rejected' : 'committed', tier: body.action === 'reject' ? 'Hold' : 'Commit', receipt: body.action === 'reject' ? null : 'signed.approved.receipt', record_id: 'record-2' }
        response = order
      }
    } else if (path === '/records/record-1' || path === '/records/record-2') response = { id: path.split('/').at(-1), customer_id: 'hotel', request: restatement, filters_applied: ['lactose-free'], options_considered: options.recordOptions || [option], checks: order.checks, outcome: order.status, approved_by: path.endsWith('2') ? 'Demo buyer' : 'reference-bot', sentences: ['The buyer asked for 1,000 litres of lactose-free milk.', 'The door checked all ten rules before deciding.'], hash: 'record-hash', previous_hash: null, created_at: '2026-11-14T19:00:00Z' }
    else throw new Error(`Unexpected offline HTTP call: ${method} ${path}`)
    if (options.labels && response && typeof response === 'object') {
      // The mandate's scope and must-hold in words, per customer.
      if (path.startsWith('/limits?customer_id=')) Object.assign(response as Json, options.labels(path.split('=').at(-1)!))
      if (path === '/demo/customers') (response as Json).customers = customers.map(customer => ({ ...customer, ...options.labels!(customer.id) }))
    }
    return new Response(JSON.stringify(response), { status, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

// The default simulated identification from POST /demo/identify.
const bankIdentity = { proof: 'proof-default', expires_at: '2026-11-15T12:05:00Z', simulated: true, method: 'Simulated bank identification', person: { name: 'Aino Esimerkki' }, authority: 'May sign for Hotel chain (simulated company authorisation)' }

// M06: approvals, proposal acceptances and limit changes pass the mock bank window.
async function identifyInBankWindow(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
  await user.click(within(dialog).getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
  await user.click(await within(dialog).findByRole('button', { name: 'Continue' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
}

async function openConsole() {
  render(<App />)
  await screen.findByText('Door connected')
  return userEvent.setup()
}

async function sendNeed(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'I need 1,000 litres of lactose-free milk by Monday.')
  await user.click(screen.getByRole('button', { name: 'Send request' }))
  await screen.findByText(restatement)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reset demo' })).toBeEnabled())
}

async function sendReorder(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
  await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Same as last week, plus 5 litres more milk.')
  await user.click(screen.getByRole('button', { name: 'Send request' }))
  await screen.findByText(reorderRestatement)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reset demo' })).toBeEnabled())
}

describe('Purchasing console against its public HTTP contract', () => {
  it('offers all five views, real/simulated labels, customer switcher and independent verification links', async () => {
    const { calls } = mockDoor()
    const user = await openConsole()
    const navigation = screen.getByRole('navigation', { name: 'Console views' })
    for (const name of ['Chat', "Door's answer", 'Sign-off', 'Limits view', 'Record']) {
      await user.click(within(navigation).getByRole('button', { name: new RegExp(name) }))
      expect(screen.getByText(name, { selector: '.topbar strong' })).toBeVisible()
    }
    expect(screen.getByRole('combobox', { name: 'Buying for' })).toHaveValue('hotel')
    expect(screen.getByText('Real data')).toBeVisible()
    expect(screen.getByText('Simulated operations')).toBeVisible()
    expect(screen.getByText(/order history and the buyer’s signing key/i)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Explore the public API' })).toHaveAttribute('href', '/docs')
    expect(screen.getByRole('link', { name: 'Public verification keys' })).toHaveAttribute('href', '/.well-known/jwks.json')
    await user.click(within(navigation).getByRole('button', { name: /Limits view/ }))
    expect(screen.getByText('Spend per order')).toBeVisible()
    expect(screen.getByText('Your signed mandate (simulated signing key)')).toBeVisible()
    expect(calls.map(call => call.path)).toEqual(expect.arrayContaining(['/demo/customers', '/healthz', '/limits?customer_id=hotel', '/usual-order?customer_id=hotel']))
  })

  it('renders the restated quantity, property and date before sending execute; shows matching, ten checks and the signed record', async () => {
    let restatementWasVisibleBeforeExecute = false
    const { calls } = mockDoor({ onExecute: () => { restatementWasVisibleBeforeExecute = !!screen.queryByText(restatement) } })
    const user = await openConsole()
    await sendNeed(user)
    expect(restatementWasVisibleBeforeExecute).toBe(true)
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse', 'preview', 'execute'])
    expect(calls.find(call => call.body?.stage === 'execute')?.body).toMatchObject({ need, restatement, customer_id: 'hotel' })
    await user.click(screen.getByRole('button', { name: 'See the door’s answer' }))
    expect(screen.getByText('lactose-free: manufacturer stated')).toBeVisible()
    expect(screen.getByText('drinking milk (inferred)')).toBeVisible()
    expect(screen.getByText('lactose-free (manufacturer)')).toBeVisible()
    expect(screen.getByText(/100 whole packs × 10 L/)).toBeVisible()
    expect(screen.getByText('10 / 10 pass')).toBeVisible()
    expect(screen.getAllByText('Pass', { exact: true })).toHaveLength(10)
    await user.click(screen.getByText('View signed offer'))
    expect(screen.getByText('signed.offer.fixture')).toBeVisible()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Record/ }))
    expect(await screen.findByText('The buyer asked for 1,000 litres of lactose-free milk.')).toBeVisible()
    await user.click(screen.getByText('Your signed receipt'))
    expect(screen.getByText('signed.receipt.fixture')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Download your copy' })).toBeVisible()
    expect(calls.some(call => call.path === '/records/record-1')).toBe(true)
  })

  it('switches customers, sends the chosen identity, and resets the conversation and simulated stock toggle through HTTP', async () => {
    const { calls } = mockDoor()
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await waitFor(() => expect(calls.some(call => call.path === '/limits?customer_id=restaurant')).toBe(true))
    expect(screen.getByRole('textbox')).toHaveAttribute('placeholder', 'Same as last week, plus 5 litres more milk.')
    await user.click(screen.getByRole('checkbox', { name: /Simulate limited stock/ }))
    await sendNeed(user)
    expect(calls.find(call => call.body?.stage === 'execute')?.body).toMatchObject({ customer_id: 'restaurant', simulate_limited_stock: true })
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await waitFor(() => expect(screen.queryByText(restatement)).not.toBeInTheDocument())
    expect(screen.getByRole('checkbox', { name: /Simulate limited stock/ })).not.toBeChecked()
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(calls.some(call => call.path === '/demo/reset' && call.method === 'POST')).toBe(true)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'hotel')
    expect(screen.getByRole('textbox')).toHaveAttribute('placeholder', 'For example: 200 litres of oat drink by Friday')
  })

  it('links a hold to its continue URL, shows the failed reason, and obtains a receipt after sign-off', async () => {
    const { calls } = mockDoor({ hold: true })
    const user = await openConsole()
    await sendNeed(user)
    const link = screen.getByRole('link', { name: 'Review sign-off' })
    expect(link).toHaveAttribute('href', '/?view=signoff&order=order-1')
    await user.click(link)
    expect(await screen.findByText('The total is above the signed per-order spend cap.')).toBeVisible()
    expect(screen.getByText('Stock reserved, not committed')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    await user.click(screen.getByText('Your signed receipt'))
    expect(screen.getByText('signed.approved.receipt')).toBeVisible()
    expect(calls.find(call => call.path === '/orders/order-1/signoff')?.body?.action).toBe('approve')
    expect(calls.some(call => call.path === '/orders/order-1' && call.method === 'GET')).toBe(true)
  })

  it('K10: shows an approved hold as committed, without "Why this needs you" or a "not committed" label', async () => {
    mockDoor({ hold: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByText('Why this needs you')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(screen.queryByText('Why this needs you')).not.toBeInTheDocument()
    expect(screen.queryByText(/not committed/i)).not.toBeInTheDocument()
    const approved = screen.getByRole('region', { name: 'What you approved' })
    expect(within(approved).getByText('The total is above the signed per-order spend cap.')).toBeVisible()
    expect(within(approved).queryByText('Hold')).not.toBeInTheDocument()
  })

  it('K11: reveals the model probabilities from options_considered lines behind one click, in plain words', async () => {
    const labelled = { ...line, probabilities: { 'drinking milk': 0.92, 'sour milk': 0.05, cream: 0.03 } }
    const rules = { ...line, id: 'milk-2', name: 'Rules-labelled milk', probabilities: null }
    mockDoor({ recordOptions: [{ ...option, lines: [labelled, rules] }, { ...option, label: 'better', lines: [labelled] }] })
    const user = await openConsole()
    await sendNeed(user)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Record/ }))
    const summary = await screen.findByText('How sure the model was about product types')
    const disclosure = summary.closest('details')!
    expect(disclosure).not.toHaveAttribute('open')
    await user.click(summary)
    expect(disclosure).toHaveAttribute('open')
    const items = within(disclosure).getAllByRole('listitem')
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('Lactose-free whole milk: drinking milk 92%, sour milk 5%, cream 3%.')
    expect(within(disclosure).queryByText(/Rules-labelled milk/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Inferred label probabilities/)).not.toBeInTheDocument()
  })

  it('K11: shows no probability disclosure when no line in the record carries probabilities', async () => {
    mockDoor({ recordOptions: [{ ...option, lines: [{ ...line, probabilities: null }] }] })
    const user = await openConsole()
    await sendNeed(user)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Record/ }))
    expect(await screen.findByText('The buyer asked for 1,000 litres of lactose-free milk.')).toBeVisible()
    expect(screen.queryByText('How sure the model was about product types')).not.toBeInTheDocument()
  })

  it('clears the edit-quantity field on Reset demo', async () => {
    mockDoor({ hold: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.type(await screen.findByRole('spinbutton', { name: 'Edit requested quantity' }), '250')
    expect(screen.getByRole('spinbutton', { name: 'Edit requested quantity' })).toHaveValue(250)
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await screen.findByText(/Demo reset\./)
    await sendNeed(user)
    await user.click(screen.getByRole('link', { name: 'Review sign-off' }))
    expect(await screen.findByRole('spinbutton', { name: 'Edit requested quantity' })).toHaveValue(null)
    expect(screen.getByRole('button', { name: 'Edit & recheck' })).toBeDisabled()
  })

  it('opens the server continue URL directly and allows rejecting the held order', async () => {
    const { calls } = mockDoor({ hold: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByText('This order needs your sign-off.')).toBeVisible()
    expect(calls.some(call => call.path === '/orders/order-1')).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Reject order' }))
    expect(await screen.findByText('This order is closed.')).toBeVisible()
    expect(calls.find(call => call.path === '/orders/order-1/signoff')?.body?.action).toBe('reject')
    expect(screen.queryByText('Your signed receipt')).not.toBeInTheDocument()
  })

  it('does not execute an incomplete preview and presents a safe, actionable error', async () => {
    const { calls } = mockDoor({ preview: { need } })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox'), 'milk please')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your agent could not restate a complete need. No order has been placed.')
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse', 'preview'])
    expect(screen.getByRole('button', { name: 'Reset demo' })).toBeEnabled()
  })

  it.each([
    ['past deadline', 'Which future delivery date do you mean? Minkä tulevan toimituspäivän tarkoitat?'],
    ['empty model answer', 'Please restate the product, quantity and delivery date. Kerro tuote, määrä ja toimituspäivä.'],
  ])('shows the %s clarification as a conversation and never sends execute', async (_, question) => {
    const { calls } = mockDoor({ preview: { stage: 'clarification', status: 'requires_clarification', question, clarification: question, need: null, order: null, messages: [{ role: 'assistant', kind: 'question', text: question }] } })
    const user = await openConsole()
    const originalRequest = 'Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi.'
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), originalRequest)
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText(question)).toBeVisible()
    expect(screen.getByText(originalRequest, { selector: '.message p' })).toBeVisible()
    expect(screen.getByText('No order has been placed. Send a complete revised request to continue.')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse', 'preview'])
    expect(screen.getByRole('textbox', { name: 'Your purchasing request' })).toBeEnabled()
    expect(screen.queryByText('Order committed')).not.toBeInTheDocument()
  })

  it('waits for a new complete request after clarification and executes only the corrected preview', async () => {
    const question = 'Please specify a future delivery date.'
    const { calls, fetchMock } = mockDoor({ preview: { stage: 'clarification', question, need: null } })
    const originalFetch = fetchMock.getMockImplementation()!
    let previews = 0
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/bot/chat' && JSON.parse(String(init?.body)).stage === 'preview' && ++previews === 2) {
        calls.push({ path: '/bot/chat', method: 'POST', body: JSON.parse(String(init?.body)), headers: new Headers(init?.headers) })
        return new Response(JSON.stringify({ stage: 'preview', need, restatement }), { status: 200 })
      }
      return originalFetch(input, init)
    })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox'), 'Milk yesterday, please.')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText(question)).toBeVisible()
    expect(calls.filter(call => call.body?.stage === 'execute')).toHaveLength(0)
    await sendNeed(user)
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse', 'preview', 'preview', 'execute'])
    expect(await screen.findByText('Order committed')).toBeVisible()
  })

  it('does not execute after a provider HTTP failure', async () => {
    const { calls } = mockDoor({ preview: { detail: 'The agent provider is temporarily unavailable.' }, previewStatus: 503 })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox'), 'milk please')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The agent provider is temporarily unavailable.')
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse', 'preview'])
  })

  it('keeps the execute operation pending without showing an order before the HTTP response', async () => {
    const { fetchMock } = mockDoor()
    const original = fetchMock.getMockImplementation()!
    let completeExecution: (() => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/bot/chat' && JSON.parse(String(init?.body)).stage === 'execute') {
        await new Promise<void>(resolve => { completeExecution = resolve })
      }
      return original(input, init)
    })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox'), 'milk please')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText(restatement)).toBeVisible()
    expect(screen.getByText('Matching products and checking your signed limits')).toBeVisible()
    expect(screen.queryByText('Order committed')).not.toBeInTheDocument()
    await act(async () => { completeExecution!() })
    expect(await screen.findByText('Order committed')).toBeVisible()
  })

  it('acknowledges only the exact refreshed offer after its displayed price is explicitly checked', async () => {
    const { calls } = mockDoor({ order: { ...heldOrder, offer: 'signed.fresh.offer', requires_price_confirmation: true, offer_payload: { ...offer.payload, total: 1300 } } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByText('The previous offer expired')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Approve order' })).toBeDisabled()
    expect(screen.getByText(/The new total is/)).toHaveTextContent('1,300')
    expect(calls.filter(call => call.path.endsWith('/signoff'))).toHaveLength(0)
    await user.click(screen.getByRole('checkbox', { name: 'I have reviewed the refreshed offer and its price.' }))
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    const approval = calls.find(call => call.body?.action === 'approve')!
    expect(approval.body).toEqual({ action: 'approve', customer_id: 'hotel', offer: 'signed.fresh.offer' })
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
  })

  it('recovers an expiry conflict by fetching the current order without automatically approving its changed price', async () => {
    const { calls } = mockDoor({ hold: true, expireOnApprove: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('The previous offer expired')).toBeVisible()
    expect(calls.filter(call => call.body?.action === 'approve')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Approve order' })).toBeDisabled()
    expect(calls.filter(call => call.path === '/orders/order-1')).toHaveLength(2)
    await user.click(screen.getByRole('checkbox', { name: 'I have reviewed the refreshed offer and its price.' }))
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    expect(calls.filter(call => call.body?.action === 'approve').at(-1)?.body?.offer).toBe('signed.fresh.offer')
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
  })

  it('prepares a standing preference and submits exactly the server summary and question hash only after human confirmation', async () => {
    const { calls } = mockDoor({ order: { ...heldOrder, question_spec: preferenceQuestion } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'How should this answer apply?' }), 'always')
    await user.click(screen.getByRole('button', { name: 'Review answer summary' }))
    expect(await screen.findByText(exactSummary)).toBeVisible()
    expect(calls.filter(call => call.body?.action === 'approve')).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Confirm answer' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'I confirm the displayed answer and its exact scope.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm answer' }))
    await identifyInBankWindow(user)
    expect(calls.find(call => call.body?.action === 'approve')?.body).toMatchObject({ answer: 'approve', scope: 'always', confirmed: true, confirmation_summary: exactSummary, question_hash: 'question-hash' })
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
  })

  it('requires a separate exact product-property checkbox and never labels buyer confirmation as manufacturer evidence', async () => {
    const propertyQuestion = { ...preferenceQuestion, kind: 'property_confirmation', preferences: [], needed: 'Confirm lactose-free for this exact product.', missing_properties: [{ product_id: 'milk-1', product_name: 'Milk with incomplete data', property: 'lactose-free', contradicted: false }] }
    const { calls } = mockDoor({ order: { ...heldOrder, question_spec: propertyQuestion, offer_payload: { ...offer.payload, lines: [{ ...line, properties: {}, property_sources: { 'lactose-free': 'buyer_confirmation' } }] } } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByRole('button', { name: 'Approve order' })).toBeDisabled()
    expect(screen.queryByText('lactose-free (manufacturer)')).not.toBeInTheDocument()
    expect(screen.getByText('lactose-free (your confirmation, this product only)')).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: /I explicitly confirm lactose-free for Milk with incomplete data/ }))
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(calls.find(call => call.body?.action === 'approve')?.body).toEqual({ action: 'approve', customer_id: 'hotel', confirmed: true, confirmations: [{ product_id: 'milk-1', property: 'lactose-free', confirmed: true }] })
  })

  it('does not offer a confirmation checkbox to override contrary manufacturer information', async () => {
    mockDoor({ order: { ...heldOrder, question_spec: { ...preferenceQuestion, kind: 'property_confirmation', preferences: [], missing_properties: [{ product_id: 'milk-1', product_name: 'Milk', property: 'lactose-free', contradicted: true }] } } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    await openConsole()
    expect(await screen.findByText(/The manufacturer states contrary information/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Approve order' })).toBeDisabled()
    expect(screen.queryByRole('checkbox', { name: /I explicitly confirm/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject order' })).toBeEnabled()
  })

  it('shows all short-stock options and clearly labels simulated stock and delivery on their lines', async () => {
    const simulatedLine = { ...line, simulated_stock: true, stock_source: 'simulated limited-stock/restock scenario', simulation_explanation: 'Simulated replenishment one day after the requested deadline.' }
    const matchOptions = ['best', 'better', 'good'].map(label => ({ ...option, label, lines: [simulatedLine], simulation: 'A simulated stock and delivery scenario; the manufacturer data and prices remain real.' }))
    mockDoor({ matchOptions })
    const user = await openConsole()
    await sendNeed(user)
    await user.click(screen.getByRole('button', { name: 'See the door’s answer' }))
    for (const label of ['best', 'better', 'good']) expect(screen.getByRole('button', { name: new RegExp(`^${label}`) })).toBeVisible()
    expect(screen.getByText('Simulated limited stock')).toBeVisible()
    expect(screen.getByText('Simulated stock / delivery')).toBeVisible()
    expect(screen.getByText('Simulated replenishment one day after the requested deadline.')).toBeVisible()
  })

  it('interviews a held trade-off, obtains a server summary, and waits for an explicit click before confirming the answer', async () => {
    const { calls } = mockDoor({ order: { ...heldOrder, question_spec: preferenceQuestion } })
    const user = await openConsole()
    await sendNeed(user)
    expect(await screen.findByText('Would the alternative brand work for your kitchen, at the displayed price and date?')).toBeVisible()
    expect(calls.filter(call => call.body?.stage === 'interview')).toHaveLength(1)
    await user.type(screen.getByRole('textbox', { name: 'Your answer in your own words' }), 'Yes, this milk brand works for us.')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Scope of this answer' }), 'always')
    await user.click(screen.getByRole('button', { name: 'Review my answer' }))
    expect(await screen.findByText(exactSummary)).toBeVisible()
    expect(calls.filter(call => call.body?.stage === 'confirm')).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Confirm and submit answer' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this exact answer and scope.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm and submit answer' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your confirmed answer was recorded and the door committed the order.')).toBeVisible()
    expect(calls.find(call => call.body?.stage === 'confirm')?.body).toEqual({ customer_id: 'hotel', stage: 'confirm', order_id: 'order-1', confirmed: true, confirmation_summary: exactSummary, question_hash: 'question-hash' })
  })

  it('lists the question and answer behind a learned preference and removes it with one public HTTP action', async () => {
    const { calls } = mockDoor({ learned: true })
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    expect(screen.getByText('Which milk brand may the agent use?')).toBeVisible()
    expect(screen.getByText('approve (always)')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.queryByText('Which milk brand may the agent use?')).not.toBeInTheDocument())
    expect(calls.filter(call => call.path === '/limits/learned-1/reset' && call.method === 'POST')).toHaveLength(1)
  })

  it('shows twelve synthetic weeks, the fixed usual basket and readable proposal evidence alongside percentage limits', async () => {
    const { calls } = mockDoor({ restaurant: true })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    expect(await screen.findByText('12 weekly orders')).toBeVisible()
    expect(screen.getByText('Synthetic order history')).toBeVisible()
    const row = screen.getByText('Departure from usual').closest('details')!
    expect(within(row).getByText('30%')).toBeVisible()
    expect(within(row).getByText('10%')).toBeVisible()
    await user.click(within(row).getByText('Departure from usual'))
    expect(within(row).getByText('The last human-confirmed usual basket remains the fixed anchor.')).toBeVisible()
    await user.click(screen.getByText('Review 4 supporting approvals'))
    const evidence = screen.getByText('Review 4 supporting approvals').closest('details')!
    expect(within(evidence).getAllByRole('listitem')).toHaveLength(4)
    expect(within(evidence).getAllByText('A synthetic human approval without correction.')).toHaveLength(4)
    expect(within(evidence).getAllByText('Synthetic')).toHaveLength(4)
    await user.click(screen.getByText('Inspect 12 synthetic weekly orders'))
    const history = screen.getByText('Inspect 12 synthetic weekly orders').closest('details')!
    expect(within(history).getAllByRole('listitem')).toHaveLength(12)
    expect(within(history).getByText(/Historical orders and prices are simulated/)).toBeVisible()
    expect(screen.getByText('Dry long-grain rice', { selector: 'h4' })).toBeVisible()
    expect(screen.getByText('Dry pasta', { selector: 'h4' })).toBeVisible()
    expect(calls.filter(call => call.path.includes('/proposals/') && call.method === 'POST')).toHaveLength(0)
  })

  it('raises current freedom only on an explicit proposal acceptance and removes that proposal without changing the ceiling', async () => {
    const { calls } = mockDoor({ restaurant: true })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = await screen.findByText('Departure from usual')
    expect(within(row.closest('details')!).getByText('10%')).toBeVisible()
    expect(screen.getAllByRole('button', { name: 'Accept proposal' })).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Accept proposal' }))
    await identifyInBankWindow(user)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Accept proposal' })).not.toBeInTheDocument())
    const updated = screen.getByText('Departure from usual').closest('details')!
    expect(within(updated).getByText('20%')).toBeVisible()
    expect(within(updated).getByText('30%')).toBeVisible()
    expect(calls.find(call => call.path === '/limits/proposals/restaurant-departure-seed/accept')?.body).toMatchObject({ customer_id: 'restaurant', confirmed: true })
  })

  it('renders the multi-line usual basket before execution and exposes Notify undo in Chat, then refreshes the tightened limit', async () => {
    let basketWasVisibleBeforeExecute = false
    const { calls } = mockDoor({ restaurant: true, order: notifyOrder, preview: { need: reorderNeed, restatement: reorderRestatement }, matchOptions: [reorderOption], onExecute: () => { basketWasVisibleBeforeExecute = !!screen.queryByRole('region', { name: 'Restated usual basket' }) } })
    const user = await openConsole()
    await sendReorder(user)
    expect(basketWasVisibleBeforeExecute).toBe(true)
    const basket = screen.getByRole('region', { name: 'Restated usual basket' })
    expect(within(basket).getAllByRole('listitem')).toHaveLength(3)
    expect(within(basket).getByText('30 L')).toBeVisible()
    expect(within(basket).getAllByText('5 sales packs')).toHaveLength(2)
    expect(calls.find(call => call.body?.stage === 'execute')?.body?.need).toEqual(reorderNeed)
    expect(screen.getByText('Approval by silence', { selector: 'strong' })).toBeVisible()
    expect(screen.getByText(/Undo until .* Helsinki time/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Undo this order' }))
    expect(await screen.findByText('withdrawn', { selector: 'h3' })).toBeVisible()
    expect(screen.queryByText('Committed with a notice')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo this order' })).not.toBeInTheDocument()
    expect(calls.filter(call => call.path === '/orders/order-1/undo' && call.method === 'POST')).toHaveLength(1)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = screen.getByText('Departure from usual').closest('details')!
    expect(within(row).getByText('0%')).toBeVisible()
    expect(within(row).getByText('30%')).toBeVisible()
    await user.click(within(row).getByText('Departure from usual'))
    expect(within(row).getByText('The buyer used undo. Current freedom is zero and the agent must ask next time.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Accept proposal' })).not.toBeInTheDocument()
  })

  it('records an explicit clean review once without increasing current freedom or silently accepting a proposal', async () => {
    const { calls } = mockDoor({ restaurant: true, order: notifyOrder, preview: { need: reorderNeed, restatement: reorderRestatement }, matchOptions: [reorderOption] })
    const user = await openConsole()
    await sendReorder(user)
    await user.click(screen.getByRole('button', { name: 'Confirm this order was right' }))
    expect(await screen.findByRole('button', { name: 'Your review is recorded' })).toBeDisabled()
    expect(calls.filter(call => call.body?.action === 'acknowledge')).toHaveLength(1)
    expect(calls.find(call => call.body?.action === 'acknowledge')?.body).toEqual({ action: 'acknowledge', customer_id: 'restaurant', confirmed: true })
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    expect(within(screen.getByText('Departure from usual').closest('details')!).getByText('10%')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeVisible()
    expect(calls.filter(call => call.path.includes('/proposals/') && call.method === 'POST')).toHaveLength(0)
  })

  it('also accepts a voluntary human review of a silent Commit without changing its receipt or requiring approval', async () => {
    const { calls } = mockDoor()
    const user = await openConsole()
    await sendNeed(user)
    expect(screen.getByText('Order committed')).toBeVisible()
    expect(screen.getByText('Optional buyer review')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Confirm this order was right' }))
    expect(await screen.findByRole('button', { name: 'Your review is recorded' })).toBeDisabled()
    expect(calls.find(call => call.body?.action === 'acknowledge')?.body).toEqual({ action: 'acknowledge', customer_id: 'hotel', confirmed: true })
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Record/ }))
    await user.click(screen.getByText('Your signed receipt'))
    expect(screen.getByText('signed.receipt.fixture')).toBeVisible()
  })

  it('resets departure freedom in one action and preserves the signed ceiling', async () => {
    const { calls } = mockDoor({ restaurant: true })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = (await screen.findByText('Departure from usual')).closest('details')!
    await user.click(within(row).getByText('Departure from usual'))
    await user.click(within(row).getByRole('button', { name: 'Reset freedom' }))
    await waitFor(() => expect(within(row).getByText('0%')).toBeVisible())
    expect(within(row).getByText('30%')).toBeVisible()
    expect(within(row).getByText('The buyer reset current freedom to zero. The agent must ask next time.')).toBeVisible()
    expect(calls.filter(call => call.path === '/limits/departure/reset')).toHaveLength(1)
  })

  it('labels a basket edit as a basket count and refreshes the automatically tightened freedom', async () => {
    const { calls } = mockDoor({ restaurant: true, order: { ...notifyOrder, status: 'requires_escalation', tier: 'Hold', receipt: null, checks: heldOrder.checks } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    const field = await screen.findByRole('spinbutton', { name: 'Basket count (1 = original requested basket)' })
    await user.type(field, '1')
    await user.click(screen.getByRole('button', { name: 'Edit & recheck' }))
    expect(calls.find(call => call.body?.action === 'edit')?.body).toEqual({ action: 'edit', customer_id: 'restaurant', quantity: 1 })
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = (await screen.findByText('Departure from usual')).closest('details')!
    expect(within(row).getByText('0%')).toBeVisible()
    await user.click(within(row).getByText('Departure from usual'))
    expect(within(row).getByText('The buyer edited this kind of order. The agent must ask next time.')).toBeVisible()
  })

  it('shows an anchor proposal’s exact basket and keeps current freedom unchanged when accepting it', async () => {
    const { calls } = mockDoor({ restaurant: true, anchorProposal: true })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const proposal = (await screen.findByText('Make this exact basket the usual')).closest('.proposal-card') as HTMLElement
    expect(within(proposal).getByText('Lactose-free whole milk')).toBeVisible()
    expect(within(proposal).getByText('Dry long-grain rice')).toBeVisible()
    expect(within(proposal).getByText('Dry pasta')).toBeVisible()
    expect(within(proposal).getByText(/does not raise current freedom or the signed ceiling/)).toBeVisible()
    await user.click(within(proposal).getByRole('button', { name: 'Accept proposal' }))
    await identifyInBankWindow(user)
    await waitFor(() => expect(screen.queryByText('Make this exact basket the usual')).not.toBeInTheDocument())
    expect(within(screen.getByText('Departure from usual').closest('details')!).getByText('10%')).toBeVisible()
    expect(calls.filter(call => call.path === '/limits/proposals/restaurant-departure-seed/accept')).toHaveLength(1)
  })
  it('displays reported monthly budget usage, including zero, and refreshes after a commit', async () => {
    let used = 0
    mockDoor({ budget: () => ({ used, remaining: 20000 - used }), onExecute: () => { used = 1200 } })
    const user = await openConsole()
    const nav = screen.getByRole('navigation', { name: 'Console views' })
    await user.click(within(nav).getByRole('button', { name: /Limits view/ }))
    let row = screen.getByText('Monthly budget', { selector: '.limit-row strong' }).closest('details')!
    expect(within(row).getByText('€0.00')).toBeVisible()
    expect(within(row).getByText('used this month')).toBeVisible()
    expect(within(row).getByText('remaining')).toBeVisible()
    await user.click(within(nav).getByRole('button', { name: /^Chat/ }))
    await sendNeed(user)
    await user.click(within(nav).getByRole('button', { name: /Limits view/ }))
    row = screen.getByText('Monthly budget', { selector: '.limit-row strong' }).closest('details')!
    await waitFor(() => expect(within(row).getByText('€1,200.00')).toBeVisible())
    expect(within(row).getByText('€18,800.00')).toBeVisible()
    expect(within(row).getByText('€20,000.00')).toBeVisible()
  })

  it('shows unreported budget usage honestly when the API omits the new fields', async () => {
    mockDoor()
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = screen.getByText('Monthly budget', { selector: '.limit-row strong' }).closest('details')!
    expect(within(row).getAllByText('€20,000.00')).toHaveLength(1)
    expect(within(row).getByText('Usage not reported')).toBeVisible()
    expect(within(row).queryByText('used this month')).not.toBeInTheDocument()
    expect(within(row).queryByText('remaining')).not.toBeInTheDocument()
    expect(within(row).queryByText('€0.00')).not.toBeInTheDocument()
  })

  it('does not invent usage when only one budget field is reported', async () => {
    mockDoor({ budget: { used: 500 } })
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    const row = screen.getByText('Monthly budget', { selector: '.limit-row strong' }).closest('details')!
    expect(within(row).getByText('Usage not reported')).toBeVisible()
    expect(within(row).queryByText('€500.00')).not.toBeInTheDocument()
  })

})

describe('Simulated buyer login (human credential)', () => {
  const header = (call?: Call) => call?.headers.get('X-Buyer-Session') ?? null
  const latestToken = (calls: Call[], customer: string) => `buyer-${customer}-${calls.filter(call => call.path === '/demo/buyer-session').length}`

  it('fetches a buyer session on load and sends it on every human read and sign-off action, never on public reads', async () => {
    const { calls } = mockDoor({ hold: true, buyerSession: true })
    const user = await openConsole()
    await waitFor(() => expect(calls.some(call => call.path === '/usual-order?customer_id=hotel')).toBe(true))
    expect(calls.find(call => call.path === '/demo/buyer-session')?.body).toEqual({ customer_id: 'hotel' })
    expect(screen.getByText(/The buyer login is simulated too\./)).toBeVisible()
    for (const path of ['/limits?customer_id=hotel', '/usual-order?customer_id=hotel']) expect(header(calls.find(call => call.path === path))).toBe('buyer-hotel-1')
    for (const path of ['/demo/customers', '/healthz']) expect(calls.filter(call => call.path === path).every(call => header(call) === null)).toBe(true)
    await sendNeed(user)
    expect(calls.filter(call => call.path === '/bot/chat').map(header)).toEqual(['buyer-hotel-1', 'buyer-hotel-1', 'buyer-hotel-1'])
    await user.click(screen.getByRole('link', { name: 'Review sign-off' }))
    await user.type(await screen.findByRole('spinbutton', { name: 'Edit requested quantity' }), '900')
    await user.click(screen.getByRole('button', { name: 'Edit & recheck' }))
    await screen.findByText('Your order is committed.')
    expect(header(calls.find(call => call.path === '/orders/order-1' && call.method === 'GET'))).toBe('buyer-hotel-1')
    expect(header(calls.find(call => call.body?.action === 'edit'))).toBe('buyer-hotel-1')
    expect(header(calls.find(call => call.path === '/records/record-2'))).toBe('buyer-hotel-1')
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await screen.findByText(/Demo reset\./)
    expect(header(calls.find(call => call.path === '/demo/reset'))).toBe('buyer-hotel-1')
    // The door checks the session against the customer named in the body (default hotel).
    expect(calls.find(call => call.path === '/demo/reset')?.body).toEqual({ customer_id: 'hotel' })
    expect(calls.filter(call => call.path === '/demo/buyer-session')).toHaveLength(2)
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('buyer-hotel')
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain('buyer-hotel')
  })

  it('sends the session on approve with property confirmations and on reject', async () => {
    const propertyQuestion = { ...preferenceQuestion, kind: 'property_confirmation', preferences: [], missing_properties: [{ product_id: 'milk-1', product_name: 'Milk with incomplete data', property: 'lactose-free', contradicted: false }] }
    const { calls } = mockDoor({ buyerSession: true, order: { ...heldOrder, question_spec: propertyQuestion } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('checkbox', { name: /I explicitly confirm lactose-free/ }))
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    await screen.findByText('Your order is committed.')
    const approval = calls.find(call => call.body?.action === 'approve')!
    expect(approval.body?.confirmations).toHaveLength(1)
    expect(header(approval)).toBe('buyer-hotel-1')
  })

  it('switches the session with the customer and sends it on limit decisions and undo', async () => {
    const { calls } = mockDoor({ restaurant: true, buyerSession: true, learned: true, order: notifyOrder, preview: { need: reorderNeed, restatement: reorderRestatement }, matchOptions: [reorderOption] })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await waitFor(() => expect(calls.find(call => call.path === '/demo/buyer-session' && call.body?.customer_id === 'restaurant')).toBeTruthy())
    const restaurantToken = latestToken(calls, 'restaurant')
    await waitFor(() => expect(header(calls.find(call => call.path === '/limits?customer_id=restaurant'))).toBe(restaurantToken))
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await user.click(await screen.findByRole('button', { name: 'Accept proposal' }))
    await identifyInBankWindow(user)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Accept proposal' })).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(calls.some(call => call.path === '/limits/learned-1/reset')).toBe(true))
    const row = screen.getByText('Departure from usual').closest('details')!
    await user.click(within(row).getByText('Departure from usual'))
    await user.click(within(row).getByRole('button', { name: 'Reset freedom' }))
    await waitFor(() => expect(calls.some(call => call.path === '/limits/departure/reset')).toBe(true))
    for (const path of ['/limits/proposals/restaurant-departure-seed/accept', '/limits/learned-1/reset', '/limits/departure/reset']) expect(header(calls.find(call => call.path === path))).toBe(restaurantToken)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /^Chat/ }))
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Same as last week, plus 5 litres more milk.')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    await user.click(await screen.findByRole('button', { name: 'Undo this order' }))
    await waitFor(() => expect(calls.some(call => call.path === '/orders/order-1/undo')).toBe(true))
    expect(header(calls.find(call => call.path === '/orders/order-1/undo'))).toBe(restaurantToken)
  })

  it('refreshes an expired session once after a 401 and repeats the call with the new token', async () => {
    const expiredTokens = new Set<string>()
    const { calls } = mockDoor({ hold: true, buyerSession: true, expiredTokens })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    // Already identified for this session, so the approval itself meets the expired token.
    window.sessionStorage.setItem('aimo-door-identified:hotel', 'Aino Esimerkki')
    const user = await openConsole()
    const approve = await screen.findByRole('button', { name: 'Approve order' })
    expiredTokens.add('buyer-hotel-1')
    await user.click(approve)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    const approvals = calls.filter(call => call.body?.action === 'approve')
    expect(approvals.map(header)).toEqual(['buyer-hotel-1', 'buyer-hotel-2'])
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Demo access code')).not.toBeInTheDocument()
  })

  it('starts only one new session when several reads meet the same expired token', async () => {
    const { calls } = mockDoor({ buyerSession: true, expiredTokens: new Set(['buyer-hotel-1']) })
    await openConsole()
    await waitFor(() => expect(header(calls.filter(call => call.path === '/usual-order?customer_id=hotel').at(-1))).toBe('buyer-hotel-2'))
    expect(calls.filter(call => call.path === '/demo/buyer-session')).toHaveLength(2)
  })

  it('opens a continue URL for another customer by retrying the order read with that customer\'s session', async () => {
    const { calls, fetchMock } = mockDoor({ buyerSession: true, restaurant: true, order: { ...heldOrder, customer_id: 'restaurant' } })
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/orders/order-1' && !new Headers(init?.headers).get('X-Buyer-Session')?.startsWith('buyer-restaurant')) {
        calls.push({ path: String(input), method: 'GET', headers: new Headers(init?.headers) })
        return new Response(JSON.stringify({ detail: 'This order belongs to another buyer.' }), { status: 403 })
      }
      return original(input, init)
    })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    await openConsole()
    expect(await screen.findByText('This order needs your sign-off.')).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Buying for' })).toHaveValue('restaurant')
    expect(calls.filter(call => call.path === '/orders/order-1').map(header)).toEqual(['buyer-hotel-1', expect.stringMatching(/^buyer-restaurant-/)])
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('carries on without the header when the door has no buyer login yet (404)', async () => {
    const { calls } = mockDoor({ hold: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(calls.some(call => call.path === '/demo/buyer-session')).toBe(true)
    expect(calls.filter(call => call.path !== '/demo/buyer-session').every(call => header(call) === null)).toBe(true)
    expect(calls.filter(call => call.body?.action === 'approve')).toHaveLength(1)
    expect(screen.queryByText(/buyer login is simulated/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('Idle auto-reset notice', () => {
  const notice = 'The demo was reset after a period of inactivity.'

  it('reloads like Reset demo, with a new buyer session, when a response carries X-Demo-Idle-Reset', async () => {
    const idle = { armed: false }
    const { calls } = mockDoor({ buyerSession: true, idle })
    const user = await openConsole()
    await sendNeed(user)
    expect(screen.getByText('Order committed')).toBeVisible()
    const sessionsBefore = calls.filter(call => call.path === '/demo/buyer-session').length
    idle.armed = true
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Sign-off/ }))
    expect(await screen.findByText(notice)).toBeVisible()
    expect(screen.getByText('Chat', { selector: '.topbar strong' })).toBeVisible()
    expect(screen.queryByText(restatement)).not.toBeInTheDocument()
    expect(screen.queryByText('Order committed')).not.toBeInTheDocument()
    expect(screen.getByText('What does your kitchen need?')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() => expect(calls.filter(call => call.path === '/demo/buyer-session').length).toBe(sessionsBefore + 1))
    expect(calls.filter(call => call.path === '/demo/reset')).toHaveLength(0)
    await new Promise(resolve => setTimeout(resolve, 4700))
    expect(screen.getByText(notice)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Dismiss notice' }))
    expect(screen.queryByText(notice)).not.toBeInTheDocument()
  }, 10000)

  it('reloads when GET /demo/customers reports a newer idle_reset_at than the one seen at load', async () => {
    const idle = { armed: false, resetAt: '2026-11-14T08:00:00Z' as string | null }
    mockDoor({ hold: true, idle })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByText('This order needs your sign-off.')).toBeVisible()
    expect(screen.queryByText(notice)).not.toBeInTheDocument()
    idle.resetAt = '2026-11-14T22:10:00Z'
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText(notice)).toBeVisible()
    expect(screen.getByText('Chat', { selector: '.topbar strong' })).toBeVisible()
  })

  it('does nothing when neither the header nor idle_reset_at is present', async () => {
    const { calls } = mockDoor({ hold: true, idle: { armed: false, resetAt: null } })
    const user = await openConsole()
    await sendNeed(user)
    await user.click(screen.getByRole('link', { name: 'Review sign-off' }))
    await user.click(await screen.findByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(calls.filter(call => call.path === '/demo/customers').length).toBeGreaterThan(1)
    expect(screen.queryByText(notice)).not.toBeInTheDocument()
    expect(screen.getByText('Sign-off', { selector: '.topbar strong' })).toBeVisible()
  })
})

describe('L09: the Chat view is a conversation', () => {
  const capability = 'I can show what Aimo sells, check price and stock, and place orders inside your signed limits.'
  const productsReply = 'Here are lactose-free milks for delivery on Monday.'
  const catalogue = [
    { product_id: 'milk-1', name: 'Lactose-free whole milk', pack: '10 × 1 L', price_eur: 12, unit_price: '€1.20 / L', availability: 'in_stock', available_text: 'In stock', properties: ['lactose-free'] },
    { product_id: 'milk-2', name: 'Lactose-free semi-skimmed milk', pack: '12 × 1 L', price_eur: null, unit_price: null, availability: 'limited', available_text: 'Only 40 packs left', properties: ['lactose-free', 'semi-skimmed'] },
  ]
  const draft = { need, restatement: 'I will order 200 litres of lactose-free whole milk for delivery on Monday 16 November.', product_name: 'Lactose-free whole milk', pack: '10 × 1 L', packs: 20, total_eur: 240, delivery_date: '2026-11-16' }
  function conversation(body: Json, order: Json): Json {
    const text = String(body.message)
    if (/^hi/i.test(text)) return { stage: 'conversation', action: 'reply', reply: capability, model: {} }
    if (/what lactose-free milk/i.test(text)) return { stage: 'conversation', action: 'products', reply: productsReply, delivery_date: '2026-11-16', products: catalogue, model: {} }
    if (/some cheese/i.test(text)) return { stage: 'conversation', action: 'clarification', reply: 'How much cheese do you need, and by when?', model: {} }
    if (/^order 200/i.test(text)) return { stage: 'conversation', action: 'draft', reply: 'Shall I place this order?', draft, model: {} }
    return { stage: 'conversation', action: 'order', reply: 'Placed.', need, restatement, match: { filters_applied: [], options: [option] }, offer, order, messages: [], idempotency_key: 'k-1', model: {} }
  }
  const outcomeCard = () => document.querySelector('.outcome-card')
  async function say(user: ReturnType<typeof userEvent.setup>, text: string) {
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), text)
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reset demo' })).toBeEnabled())
  }

  it('invites catalogue questions in the welcome text and a suggestion', async () => {
    mockDoor({ converse: conversation })
    await openConsole()
    expect(screen.getByText(/Ask what Aimo sells, check a price or stock, or give an order/)).toBeVisible()
    expect(screen.getByRole('button', { name: /What lactose-free milk do you have\?/ })).toBeVisible()
    expect(screen.getByRole('button', { name: /I need 1,000 litres of lactose-free milk by Monday\./ })).toBeVisible()
  })

  it('shows a reply as text with no outcome card, no order calls and no Sign-off badge', async () => {
    const { calls } = mockDoor({ converse: conversation })
    const user = await openConsole()
    const limitReads = () => calls.filter(call => call.path.startsWith('/limits')).length
    await waitFor(() => expect(limitReads()).toBeGreaterThan(0))
    const before = limitReads()
    await say(user, 'Hi, what can you help me with?')
    expect(await screen.findByText(capability)).toBeVisible()
    expect(outcomeCard()).toBeNull()
    expect(screen.queryByText('Order committed')).not.toBeInTheDocument()
    expect(within(screen.getByRole('navigation')).getByRole('button', { name: /Sign-off/ })).not.toHaveTextContent('1')
    expect(calls.filter(call => call.path === '/bot/chat').map(call => call.body?.stage)).toEqual(['converse'])
    expect(limitReads()).toBe(before)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Door's answer/ }))
    expect(screen.getByText('No answer yet')).toBeVisible()
  })

  it('keeps the transcript and sends the earlier turns, with products by id and name, as history', async () => {
    const { calls } = mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'What lactose-free milk do you have?')
    await screen.findByText(productsReply)
    await say(user, 'Hi again')
    expect(await screen.findByText(capability)).toBeVisible()
    expect(screen.getByText('What lactose-free milk do you have?', { selector: '.message p' })).toBeVisible()
    expect(screen.getByText(productsReply)).toBeVisible()
    const converse = calls.filter(call => call.body?.stage === 'converse')
    expect(converse[0].body).toEqual({ stage: 'converse', customer_id: 'hotel', message: 'What lactose-free milk do you have?', history: [], simulate_limited_stock: false })
    expect(converse[1].body?.history).toEqual([
      { role: 'user', text: 'What lactose-free milk do you have?' },
      { role: 'assistant', text: productsReply, action: 'products', products: [{ product_id: 'milk-1', name: 'Lactose-free whole milk' }, { product_id: 'milk-2', name: 'Lactose-free semi-skimmed milk' }], draft: null },
    ])
  })

  it('limits the history to the last 12 turns, oldest first', async () => {
    const { calls } = mockDoor({ converse: conversation })
    const user = await openConsole()
    for (let turn = 1; turn <= 7; turn++) await say(user, `Hi ${turn}`)
    const history = calls.filter(call => call.body?.stage === 'converse').at(-1)!.body!.history
    expect(history).toHaveLength(12)
    expect(history[0]).toEqual({ role: 'user', text: 'Hi 1' })
    expect(history.at(-1)).toMatchObject({ role: 'assistant', action: 'reply' })
  })

  it('lists products compactly with pack, price per pack, unit price, availability and plain property labels', async () => {
    mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'What lactose-free milk do you have?')
    const list = await screen.findByRole('region', { name: 'Products' }).catch(() => screen.getByLabelText('Products'))
    expect(within(list).getByText(/Prices and stock for delivery Mon 16 Nov/)).toBeVisible()
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Lactose-free whole milk')
    expect(items[0]).toHaveTextContent('10 × 1 L')
    expect(items[0]).toHaveTextContent('€12.00 per pack')
    expect(items[0]).toHaveTextContent('€1.20 / L')
    expect(items[0]).toHaveTextContent('In stock')
    expect(items[1]).toHaveTextContent('Price not available')
    expect(items[1]).toHaveTextContent('Only 40 packs left')
    expect(within(items[1]).getByText('lactose-free, semi-skimmed')).toBeVisible()
    expect(outcomeCard()).toBeNull()
  })

  it('shows a clarification as the agent asking for the missing detail, with no order', async () => {
    const { calls } = mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'some cheese please')
    expect(await screen.findByText('How much cheese do you need, and by when?')).toBeVisible()
    expect(screen.getByText('Your agent needs one more detail')).toBeVisible()
    expect(outcomeCard()).toBeNull()
    expect(calls.some(call => call.body?.stage === 'execute')).toBe(false)
  })

  it('places a draft through execute with its need, restatement and original message, then renders the order turn', async () => {
    const { calls } = mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'Order 200 litres of lactose-free milk')
    const card = await screen.findByRole('region', { name: 'Draft order' })
    expect(card).toHaveTextContent(draft.restatement)
    expect(card).toHaveTextContent('Lactose-free whole milk, 10 × 1 L')
    expect(card).toHaveTextContent('20')
    expect(card).toHaveTextContent('€240.00')
    expect(card).toHaveTextContent('Mon 16 Nov')
    expect(outcomeCard()).toBeNull()
    await user.click(within(card).getByRole('button', { name: 'Change something' }))
    expect(screen.getByRole('textbox', { name: 'Your purchasing request' })).toHaveFocus()
    expect(calls.some(call => call.body?.stage === 'execute')).toBe(false)
    await user.click(within(card).getByRole('button', { name: 'Place this order' }))
    expect(await screen.findByText('Order committed')).toBeVisible()
    const execute = calls.find(call => call.body?.stage === 'execute')!
    expect(execute.body).toMatchObject({ stage: 'execute', customer_id: 'hotel', need, restatement: draft.restatement, message: 'Order 200 litres of lactose-free milk', simulate_limited_stock: false })
    expect(typeof execute.body?.idempotency_key).toBe('string')
    expect(within(card).queryByRole('button', { name: 'Place this order' })).not.toBeInTheDocument()
    expect(card).toHaveTextContent('You placed this draft.')
  })

  it('removes the buttons from a draft that a later turn superseded', async () => {
    mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'Order 200 litres of lactose-free milk')
    const card = await screen.findByRole('region', { name: 'Draft order' })
    await say(user, 'Hi, actually wait')
    await screen.findByText(capability)
    expect(within(card).queryByRole('button', { name: 'Place this order' })).not.toBeInTheDocument()
    expect(card).toHaveTextContent('Not placed.')
  })

  it('renders an order action exactly as an order turn: restatement, decision and outcome card', async () => {
    mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'I need 1,000 litres of lactose-free milk by Monday.')
    expect(await screen.findByText(restatement)).toBeVisible()
    expect(screen.getByText('Order committed')).toBeVisible()
    expect(screen.getByRole('button', { name: 'See the door’s answer' })).toBeVisible()
  })

  it('uses neutral progress wording while a converse turn is pending', async () => {
    const { fetchMock } = mockDoor({ converse: conversation })
    const original = fetchMock.getMockImplementation()!
    let release: (() => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/bot/chat' && JSON.parse(String(init?.body)).stage === 'converse') await new Promise<void>(resolve => { release = resolve })
      return original(input, init)
    })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Hi')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('Your agent is reading your message')).toBeVisible()
    await act(async () => { release!() })
    expect(await screen.findByText(capability)).toBeVisible()
  })

  it('clears the transcript on Reset demo and on a customer switch', async () => {
    mockDoor({ converse: conversation })
    const user = await openConsole()
    await say(user, 'Hi')
    await screen.findByText(capability)
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await waitFor(() => expect(screen.queryByText(capability)).not.toBeInTheDocument())
    await say(user, 'Hi')
    await screen.findByText(capability)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await waitFor(() => expect(screen.queryByText(capability)).not.toBeInTheDocument())
  })
})

describe('M05: changing a signed limit', () => {
  const identity = { proof: 'proof-1', expires_at: '2026-11-15T12:05:00Z', simulated: true, method: 'Simulated bank identification', person: { name: 'Aino Esimerkki' }, authority: 'May sign for Hotel chain (simulated company authorisation)' }
  const updatedLimits = (value: number) => ({ customer_id: 'hotel', rows: limitRows.map(row => row.id === 'per_order_cap' ? { ...row, signed_ceiling: value, current_freedom: value } : row), proposals: [], learned_preferences: [] })
  const signed = (value: number): [number, Json] => [200, { limits: updatedLimits(value), change: { limit_id: 'per_order_cap', name: 'Spend per order', old_value: 2000, new_value: value, signed_by: identity.person.name, identified_by: identity.method, at: '2026-11-15T12:01:00Z' }, mandate_version: 2, record_id: 'record-2' }]
  const changeCalls = (calls: Call[]) => calls.filter(call => call.path.endsWith('/change'))
  const identifyCalls = (calls: Call[]) => calls.filter(call => call.path === '/demo/identify')
  const row = (name = 'Spend per order') => screen.getByText(name, { selector: '.limit-row strong' }).closest('details')!

  async function openLimits(options: MockOptions = {}) {
    const door = mockDoor({ buyerSession: true, ...options })
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await screen.findByText('Spend per order', { selector: '.limit-row strong' })
    return { ...door, user }
  }
  async function toStep2(user: ReturnType<typeof userEvent.setup>, value = '3000') {
    await user.click(screen.getAllByRole('button', { name: 'Change this limit' })[0])
    await user.type(screen.getByRole('spinbutton', { name: /New spend per order, in euros/ }), value)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByRole('heading', { name: 'Identify yourself' })).toBeVisible()
  }
  async function toStep3(user: ReturnType<typeof userEvent.setup>) {
    await toStep2(user)
    await user.click(screen.getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    expect(await screen.findByText('Identified: Aino Esimerkki')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByRole('heading', { name: 'Review and sign' })).toBeVisible()
  }
  const mandate = (change: (path: string, body: Json, headers: Headers) => [number, Json] = (_path, body) => signed(body.value)) => ({ identify: (): [number, Json] => [200, identity], change })

  it('offers the change only on the spend cap and monthly budget rows', async () => {
    await openLimits({ restaurant: true })
    expect(screen.getAllByRole('button', { name: 'Change this limit' })).toHaveLength(2)
  })

  it('step 1 shows the current ceiling and enables Continue only for a positive, different amount', async () => {
    const { user, calls } = await openLimits({ mandate: mandate() })
    await user.click(screen.getAllByRole('button', { name: 'Change this limit' })[0])
    const flow = screen.getByRole('region', { name: 'Change Spend per order' })
    expect(within(flow).getByText('€2,000.00')).toBeVisible()
    expect(within(flow).getByText(/Raising a ceiling lets your agent spend more without asking; lowering it takes effect at once\./)).toBeVisible()
    const field = within(flow).getByRole('spinbutton', { name: /New spend per order, in euros/ })
    const next = within(flow).getByRole('button', { name: 'Continue' })
    expect(next).toBeDisabled()
    await user.type(field, '2000')
    expect(next).toBeDisabled()
    await user.clear(field); await user.type(field, '0')
    expect(next).toBeDisabled()
    await user.clear(field); await user.type(field, '1500')
    expect(next).toBeEnabled()
    expect(identifyCalls(calls)).toHaveLength(0)
  })

  it('step 2 is marked as a simulation and shows who was identified and their authority', async () => {
    const { user, calls } = await openLimits({ mandate: mandate() })
    await toStep2(user)
    expect(screen.getByText('Simulation: no real bank or identity service is contacted.')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    expect(await screen.findByText('Identified: Aino Esimerkki')).toBeVisible()
    expect(screen.getByText(identity.authority)).toBeVisible()
    expect(identifyCalls(calls)[0].body).toEqual({ customer_id: 'hotel', method: 'bank' })
    expect(identifyCalls(calls)[0].headers.get('X-Buyer-Session')).toMatch(/^buyer-hotel-/)
  })

  it('step 3 reviews old and new value, then signs with the session and identification proof and updates the row', async () => {
    const { user, calls } = await openLimits({ mandate: mandate() })
    await toStep3(user)
    expect(screen.getByText('Spend per order: €2,000.00 → €3,000.00')).toBeVisible()
    expect(screen.getByText('Signed by Aino Esimerkki, identified by Simulated bank identification.')).toBeVisible()
    expect(screen.getByText('You are signing a new version of your mandate. The door cannot change this limit; only you can.')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    expect(await screen.findByText('Limit changed. Your new mandate version is signed.')).toBeVisible()
    const sign = changeCalls(calls)[0]
    expect(sign.path).toBe('/limits/per_order_cap/change')
    expect(sign.body).toEqual({ customer_id: 'hotel', value: 3000 })
    expect(sign.headers.get('X-Identification-Proof')).toBe('proof-1')
    expect(sign.headers.get('X-Buyer-Session')).toMatch(/^buyer-hotel-/)
    expect(screen.queryByRole('region', { name: 'Change Spend per order' })).not.toBeInTheDocument()
    expect(within(row()).getAllByText('€3,000.00').length).toBeGreaterThan(0)
    expect(within(row()).getByText('Acts alone up to €3,000.00')).toBeVisible()
    expect(screen.getByText('€3,000.00', { selector: '.mandate-stat strong' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'See this change on the record' }))
    expect(screen.getByText('Record', { selector: '.topbar strong' })).toBeVisible()
    expect(await screen.findByText('Decision record')).toBeVisible()
    expect(calls.some(call => call.path === '/records/record-2')).toBe(true)
  })

  it.each([1, 2, 3])('Cancel at step %i closes the flow and changes nothing', async step => {
    const { user, calls } = await openLimits({ mandate: mandate() })
    if (step === 1) { await user.click(screen.getAllByRole('button', { name: 'Change this limit' })[0]); await user.type(screen.getByRole('spinbutton', { name: /New spend per order/ }), '3000') }
    if (step === 2) await toStep2(user)
    if (step === 3) await toStep3(user)
    if (step === 2) {
      // Cancelling the bank window returns to the value step; Cancel there closes the flow.
      await user.click(within(screen.getByRole('dialog', { name: 'Bank identification' })).getByRole('button', { name: 'Cancel' }))
      expect(screen.getByRole('heading', { name: 'New value' })).toBeVisible()
    }
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Change Spend per order' })).not.toBeInTheDocument()
    expect(changeCalls(calls)).toHaveLength(0)
    expect(within(row()).getAllByText('€2,000.00').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'See this change on the record' })).not.toBeInTheDocument()
  })

  it('Escape closes the flow without changing anything', async () => {
    const { user, calls } = await openLimits({ mandate: mandate() })
    await toStep3(user)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('region', { name: 'Change Spend per order' })).not.toBeInTheDocument()
    expect(changeCalls(calls)).toHaveLength(0)
  })

  it('shows a 401 detail inside the flow and keeps it open', async () => {
    const { user } = await openLimits({ mandate: mandate(() => [401, { detail: 'Identify yourself before changing a signed limit.' }]) })
    await toStep3(user)
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    expect(await screen.findByText('Identify yourself before changing a signed limit.')).toBeVisible()
    expect(screen.getByRole('region', { name: 'Change Spend per order' })).toBeVisible()
    expect(screen.queryByLabelText('Demo access code')).not.toBeInTheDocument()
  })

  it('returns to step 2 on a 403 for an expired proof', async () => {
    const { user } = await openLimits({ mandate: mandate(() => [403, { detail: 'The identification has expired. Identify yourself again.' }]) })
    await toStep3(user)
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    expect(await screen.findByText('The identification has expired. Identify yourself again.')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Identify yourself' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Identify with bank credentials (simulated)' })).toBeVisible()
    expect(screen.queryByLabelText('Demo access code')).not.toBeInTheDocument()
  })

  it('shows a 422 detail on the value step', async () => {
    const { user } = await openLimits({ mandate: mandate(() => [422, { detail: 'The new limit must be a positive amount.' }]) })
    await toStep3(user)
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    expect(await screen.findByText('The new limit must be a positive amount.')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'New value' })).toBeVisible()
  })

  it('says plainly when the door cannot identify buyers yet (404) and does not change the limit', async () => {
    const { user, calls } = await openLimits({ mandate: { identify: () => [404, { detail: 'Not Found' }] } })
    await toStep2(user)
    await user.click(screen.getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    expect(await screen.findByText('This door version cannot identify buyers yet.')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Continue without identification/ })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Change Spend per order' })).toBeVisible()
    expect(changeCalls(calls)).toHaveLength(0)
    expect(screen.queryByRole('alert', { name: /could not complete/ })).not.toBeInTheDocument()
  })
})

describe('Polish: product rows and limit-change records', () => {
  it('shows at most four properties on one line, then +N more, and drops a bare ISO date from availability', async () => {
    const products = [
      { product_id: 'a', name: 'Many-property milk', pack: '10 × 1 L', price_eur: 12, unit_price: '€1.20 / L', availability: 'in_stock', available_text: 'Available for 2026-11-16.', properties: ['lactose-free', 'free-from-additives', 'free-from-colouring-agents', 'organic', 'UHT', 'finnish'] },
      { product_id: 'b', name: 'Sold-out milk', pack: '12 × 1 L', price_eur: 15, unit_price: null, availability: 'unavailable', available_text: 'Unavailable for 2026-11-16.', properties: [] },
      { product_id: 'c', name: 'Late milk', pack: '6 × 1 L', price_eur: 9, unit_price: null, availability: 'unavailable', available_text: 'The order cutoff for this delivery has passed.', properties: ['lactose-free'] },
    ]
    mockDoor({ converse: () => ({ stage: 'conversation', action: 'products', reply: 'Here they are.', delivery_date: '2026-11-16', products, model: {} }) })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'What milk do you have?')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    const items = within(await screen.findByLabelText('Products')).getAllByRole('listitem')
    expect(within(items[0]).getByText('lactose-free, free-from-additives, free-from-colouring-agents, organic +2 more')).toBeVisible()
    expect(within(items[0]).getByText('Available', { exact: true })).toBeVisible()
    expect(items[0]).not.toHaveTextContent('2026-11-16')
    expect(within(items[1]).getByText('Unavailable', { exact: true })).toBeVisible()
    expect(within(items[2]).getByText('The order cutoff for this delivery has passed.')).toBeVisible()
    expect(within(items[2]).getByText('lactose-free')).toBeVisible()
  })

  it('titles a limit-change record in sentence case and hides the checks section when it has none', async () => {
    const { fetchMock } = mockDoor({ buyerSession: true, mandate: { identify: () => [200, { proof: 'p', method: 'Simulated bank identification', person: { name: 'Aino Esimerkki' }, authority: 'May sign for Hotel chain (simulated company authorisation)' }], change: (_path, body) => [200, { limits: { rows: limitRows.map(row => row.id === 'per_order_cap' ? { ...row, signed_ceiling: body.value } : row), proposals: [], learned_preferences: [] }, record_id: 'limit-record' }] } })
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => String(input) === '/records/limit-record'
      ? new Response(JSON.stringify({ id: 'limit-record', outcome: 'limit_changed', approved_by: 'Aino Esimerkki', sentences: ['The buyer raised the per-order cap.'], checks: [], hash: 'h', previous_hash: 'p' }), { status: 200 })
      : original(input, init))
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await user.click((await screen.findAllByRole('button', { name: 'Change this limit' }))[0])
    await user.type(screen.getByRole('spinbutton', { name: /New spend per order/ }), '3000')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    await user.click(await screen.findByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    await user.click(await screen.findByRole('button', { name: 'See this change on the record' }))
    const title = await screen.findByText('Limit changed', { selector: '.record-banner strong' })
    expect(title).toHaveClass('sentence-case')
    expect(screen.queryByText('The ten checks, as decided')).not.toBeInTheDocument()
    expect(screen.queryByText('0 checks')).not.toBeInTheDocument()
  })

  it('keeps an order record exactly as before, with its checks section', async () => {
    mockDoor()
    const user = await openConsole()
    await sendNeed(user)
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Record/ }))
    expect(await screen.findByText('The ten checks, as decided')).toBeVisible()
    expect(screen.getByText('10 checks')).toBeVisible()
    const title = screen.getByText('committed', { selector: '.record-banner strong' })
    expect(title).not.toHaveClass('sentence-case')
  })
})

describe('M06: the mock bank window', () => {
  const cleanupRender = () => cleanup()
  const bank = () => screen.queryByRole('dialog', { name: 'Bank identification' })
  const approvals = (calls: Call[]) => calls.filter(call => call.body?.action === 'approve')
  const identifies = (calls: Call[]) => calls.filter(call => call.path === '/demo/identify')
  const sessionLine = /Identified for this session: Aino Esimerkki \(simulated\)/
  async function openHeld(options: MockOptions = {}) {
    const door = mockDoor({ hold: true, buyerSession: true, ...options })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await screen.findByRole('button', { name: 'Approve order' })
    return { ...door, user }
  }

  it('is a modal window marked as a simulation, saying what is being confirmed', async () => {
    const { user } = await openHeld()
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByText('Simulation: no real bank or identity service is contacted.')).toBeVisible()
    expect(within(dialog).getByText('Approve this order: €1,200.00, delivery Mon 16 Nov')).toBeVisible()
    expect(within(dialog).getByText('This stands for strong identification and your right to sign for the company.')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Identify with bank credentials (simulated)' })).toHaveFocus()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeVisible()
  })

  it('opens on the first approval of the session and not on the second', async () => {
    const { user, calls } = await openHeld({ expireOnApprove: true })
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('The previous offer expired')).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: 'I have reviewed the refreshed offer and its price.' }))
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(bank()).toBeNull()
    expect(identifies(calls)).toHaveLength(1)
    expect(approvals(calls)).toHaveLength(2)
  })

  it('cancelling (button, close control or Escape) leaves the order unapproved without an error', async () => {
    const { user, calls } = await openHeld()
    for (const close of ['Cancel', 'Close', 'Escape']) {
      await user.click(screen.getByRole('button', { name: 'Approve order' }))
      const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
      if (close === 'Escape') await user.keyboard('{Escape}')
      else await user.click(within(dialog).getByRole('button', { name: close }))
      await waitFor(() => expect(bank()).toBeNull())
    }
    expect(approvals(calls)).toHaveLength(0)
    expect(identifies(calls)).toHaveLength(0)
    expect(screen.getByText('This order needs your sign-off.')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(sessionLine)).not.toBeInTheDocument()
  })

  it('keeps focus inside the window', async () => {
    const { user } = await openHeld()
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
    for (let step = 0; step < 5; step++) {
      await user.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
  })

  it('needs no identification to reject an order', async () => {
    const { user, calls } = await openHeld()
    await user.click(screen.getByRole('button', { name: 'Reject order' }))
    expect(await screen.findByText('This order is closed.')).toBeVisible()
    expect(bank()).toBeNull()
    expect(identifies(calls)).toHaveLength(0)
  })

  it('shows who is identified, writes the session key, and a reload reads it without asking again', async () => {
    const first = await openHeld()
    await first.user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(first.user)
    expect(await screen.findByText(sessionLine)).toBeVisible()
    expect(window.sessionStorage.getItem('aimo-door-identified:hotel')).toBe('Aino Esimerkki')
    // A reload with the key set: no window for an approval.
    cleanupRender()
    const second = await openHeld()
    expect(screen.getByText(sessionLine)).toBeVisible()
    await second.user.click(screen.getByRole('button', { name: 'Approve order' }))
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(bank()).toBeNull()
    expect(identifies(second.calls)).toHaveLength(0)
  })

  it('Reset demo and a customer switch clear the identification and its key', async () => {
    const { user } = await openHeld()
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText(sessionLine)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await waitFor(() => expect(screen.queryByText(sessionLine)).not.toBeInTheDocument())
    expect(window.sessionStorage.getItem('aimo-door-identified:hotel')).toBeNull()
    window.sessionStorage.setItem('aimo-door-identified:hotel', 'Aino Esimerkki')
    cleanupRender()
    const again = await openHeld()
    expect(screen.getByText(sessionLine)).toBeVisible()
    await again.user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await waitFor(() => expect(screen.queryByText(sessionLine)).not.toBeInTheDocument())
    expect(window.sessionStorage.getItem('aimo-door-identified:hotel')).toBeNull()
  })

  it('opens on the first proposal acceptance with the proposal title', async () => {
    const { calls } = mockDoor({ restaurant: true, buyerSession: true })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await user.click(await screen.findByRole('button', { name: 'Accept proposal' }))
    const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
    expect(within(dialog).getByText('Raise current freedom to 20%')).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(calls.filter(call => call.path.includes('/proposals/'))).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Accept proposal' }))
    await identifyInBankWindow(user)
    await waitFor(() => expect(calls.filter(call => call.path.includes('/proposals/'))).toHaveLength(1))
  })

  it('opens before every limit change and sends that identification\'s proof', async () => {
    let proof = 0
    const { calls } = mockDoor({ buyerSession: true, mandate: { identify: () => [200, { ...bankIdentity, proof: `proof-${++proof}` }], change: (_path, body) => [200, { limits: { rows: limitRows.map(row => row.id === 'per_order_cap' ? { ...row, signed_ceiling: body.value } : row), proposals: [], learned_preferences: [] }, record_id: `record-change-${proof}` }] } })
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    for (const [from, to] of [['€2,000.00', '3000'], ['€3,000.00', '3500']]) {
      await user.click((await screen.findAllByRole('button', { name: 'Change this limit' }))[0])
      await user.type(screen.getByRole('spinbutton', { name: /New spend per order/ }), to)
      await user.click(screen.getByRole('button', { name: 'Continue' }))
      const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
      expect(within(dialog).getByText(`Spend per order: ${from} → ${money(Number(to))}`)).toBeVisible()
      expect(screen.getByRole('list').querySelector('[aria-current="step"]')).toHaveTextContent('Identify yourself')
      await identifyInBankWindow(user)
      await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: 'Change Spend per order' })).not.toBeInTheDocument())
    }
    const changes = calls.filter(call => call.path.endsWith('/change'))
    expect(changes.map(call => call.headers.get('X-Identification-Proof'))).toEqual(['proof-1', 'proof-2'])
  })

  it('shows an identification error inside the window and lets the buyer retry', async () => {
    let attempts = 0
    const { user, calls } = await openHeld({ mandate: { identify: () => ++attempts === 1 ? [503, { detail: 'The simulated identification service is busy.' }] : [200, bankIdentity] } })
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
    await user.click(within(dialog).getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    expect(await within(dialog).findByText('The simulated identification service is busy.')).toBeVisible()
    expect(approvals(calls)).toHaveLength(0)
    await identifyInBankWindow(user)
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
  })

  it('on an older door, says so and lets an approval proceed without identification', async () => {
    const { user, calls } = await openHeld({ mandate: { identify: () => [404, { detail: 'Not Found' }] } })
    await user.click(screen.getByRole('button', { name: 'Approve order' }))
    const dialog = await screen.findByRole('dialog', { name: 'Bank identification' })
    await user.click(within(dialog).getByRole('button', { name: 'Identify with bank credentials (simulated)' }))
    expect(await within(dialog).findByText('This door version cannot identify buyers yet.')).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Continue without identification' }))
    expect(await screen.findByText('Your order is committed.')).toBeVisible()
    expect(approvals(calls)).toHaveLength(1)
    expect(screen.queryByText(/Identified for this session/)).not.toBeInTheDocument()
  })
})

describe('N04: refusals in place, and holds that cannot be approved', () => {
  const noLaterOffer = 'No later fully available offer with an open order cutoff is currently available.'
  const objectRefusal: [number, Json] = [409, { detail: { message: noLaterOffer, earliest_orderable_date: null } }]
  const approvalsSent = (calls: Call[]) => calls.filter(call => call.body?.action === 'approve')
  const undeliverable = { ...preferenceQuestion, kind: 'delivery', needed: 'No delivery date with the full quantity is available before the order cutoff.', answers: [{ value: 'reject', label: 'Reject this order', cost: { price: 0, date: 'No delivery' } }], scope_options: ['just_this_once'], preferences: [] }

  it('shows an object-shaped 409 next to "Approve order", keeps the button usable, and a second click sends again', async () => {
    const { calls } = mockDoor({ hold: true, buyerSession: true, refuse: (path, body) => path.endsWith('/signoff') && body?.action === 'approve' ? objectRefusal : undefined })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    const alert = await screen.findByText(noLaterOffer)
    expect(alert.closest('.signoff-actions')).not.toBeNull()
    const approve = screen.getByRole('button', { name: 'Approve order' })
    await waitFor(() => expect(approve).toBeEnabled())
    await user.click(approve)
    await waitFor(() => expect(approvalsSent(calls)).toHaveLength(2))
    expect(await screen.findByText(noLaterOffer)).toBeVisible()
  })

  it('shows a string-shaped 4xx next to "Confirm answer"', async () => {
    mockDoor({ order: { ...heldOrder, question_spec: preferenceQuestion }, buyerSession: true, refuse: (path, body) => path.endsWith('/signoff') && body?.action === 'approve' ? [422, { detail: 'The answer no longer matches the current question.' }] : undefined })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('button', { name: 'Review answer summary' }))
    await user.click(await screen.findByRole('checkbox', { name: 'I confirm the displayed answer and its exact scope.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm answer' }))
    await identifyInBankWindow(user)
    const alert = await screen.findByText('The answer no longer matches the current question.')
    expect(alert.closest('.question-panel')).not.toBeNull()
  })

  it('shows a refusal of "Confirm and submit answer" in Chat', async () => {
    mockDoor({ order: { ...heldOrder, question_spec: preferenceQuestion }, refuse: (path, body) => path === '/bot/chat' && body?.stage === 'confirm' ? objectRefusal : undefined })
    const user = await openConsole()
    await sendNeed(user)
    await user.type(await screen.findByRole('textbox', { name: 'Your answer in your own words' }), 'Yes, accept it.')
    await user.click(screen.getByRole('button', { name: 'Review my answer' }))
    await user.click(await screen.findByRole('checkbox', { name: 'I confirm this exact answer and scope.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm and submit answer' }))
    await identifyInBankWindow(user)
    const alert = await screen.findByText(noLaterOffer)
    expect(alert.closest('.interview-panel')).not.toBeNull()
  })

  it('shows a refusal of "Accept proposal" on the proposal and keeps it usable', async () => {
    const { calls } = mockDoor({ restaurant: true, refuse: path => path.includes('/proposals/') ? objectRefusal : undefined })
    const user = await openConsole()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await user.click(await screen.findByRole('button', { name: 'Accept proposal' }))
    await identifyInBankWindow(user)
    const alert = await screen.findByText(noLaterOffer)
    expect(alert.closest('.proposal-card')).not.toBeNull()
    await user.click(screen.getByRole('button', { name: 'Accept proposal' }))
    await waitFor(() => expect(calls.filter(call => call.path.includes('/proposals/'))).toHaveLength(2))
  })

  it('shows a refusal of "Place this order" on the draft card', async () => {
    const draft = { need, restatement: 'I will order 200 litres.', product_name: 'Milk', pack: '10 × 1 L', packs: 20, total_eur: 240, delivery_date: '2026-11-16' }
    mockDoor({ converse: () => ({ stage: 'conversation', action: 'draft', reply: 'Shall I?', draft, model: {} }), refuse: (path, body) => path === '/bot/chat' && body?.stage === 'execute' ? [409, { detail: 'The offer for this draft has expired. Ask again.' }] : undefined })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Order 200 litres of milk')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    const card = await screen.findByRole('region', { name: 'Draft order' })
    await user.click(within(card).getByRole('button', { name: 'Place this order' }))
    expect(await within(card).findByText('The offer for this draft has expired. Ask again.')).toBeVisible()
    expect(within(card).getByRole('button', { name: 'Place this order' })).toBeEnabled()
  })

  it('uses a short generic sentence when a refusal has no readable reason', async () => {
    mockDoor({ hold: true, refuse: (path, body) => path.endsWith('/signoff') && body?.action === 'approve' ? [409, { detail: { code: 'x' } }] : undefined })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    await user.click(await screen.findByRole('button', { name: 'Approve order' }))
    await identifyInBankWindow(user)
    expect(await screen.findByText('The door refused this step. Nothing was changed.')).toBeVisible()
  })

  it('offers no approval for an order that cannot be delivered, explains why, and still rejects without the bank window', async () => {
    const { calls } = mockDoor({ order: { ...heldOrder, approvable: false, question_spec: undeliverable } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    const user = await openConsole()
    expect(await screen.findByRole('heading', { name: 'This order cannot be delivered.' })).toBeVisible()
    expect(screen.getByText('It is on hold. You can reject it, or ask your agent for another product or date.')).toBeVisible()
    // The door's own explanation, shown as it is, with no prefix and not repeated as the question heading.
    expect(screen.getByText('No delivery date with the full quantity is available before the order cutoff.', { selector: '.cannot-deliver span' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Reject this order' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'No delivery date with the full quantity is available before the order cutoff.' })).not.toBeInTheDocument()
    expect(screen.queryByText('This order needs your sign-off.')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve order' })).not.toBeInTheDocument()
    const select = screen.getByRole('combobox', { name: 'Choose an answer' })
    expect(within(select).queryByRole('option', { name: /Accept/ })).not.toBeInTheDocument()
    expect(select).toHaveValue('reject')
    expect(screen.getByRole('spinbutton', { name: 'Edit requested quantity' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Reject order' }))
    expect(await screen.findByText('This order is closed.')).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(calls.filter(call => call.path === '/demo/identify')).toHaveLength(0)
    expect(approvalsSent(calls)).toHaveLength(0)
  })

  it('shows the same explanation in Chat for an order that cannot be delivered, without an approve answer', async () => {
    mockDoor({ order: { ...heldOrder, approvable: false, question_spec: undeliverable } })
    const user = await openConsole()
    await sendNeed(user)
    const panel = (await screen.findByText('No delivery date with the full quantity is available before the order cutoff.', { selector: '.cannot-deliver span' })).closest('.interview-panel') as HTMLElement
    expect(within(panel).getByRole('heading', { name: 'This order cannot be delivered.' })).toBeVisible()
    expect(within(panel).getByText('It is on hold. You can reject it, or ask your agent for another product or date.')).toBeVisible()
    expect(panel).not.toBeNull()
    expect(within(panel).queryByText('Accept the displayed order')).not.toBeInTheDocument()
    expect(within(panel).getByText('Reject this order')).toBeVisible()
  })

  it('leaves an approvable hold exactly as before', async () => {
    mockDoor({ hold: true })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    await openConsole()
    expect(await screen.findByRole('button', { name: 'Approve order' })).toBeVisible()
    expect(screen.queryByText(/This order cannot be delivered/)).not.toBeInTheDocument()
  })
})

describe('Last polish: undeliverable wording, draft line, limits suggestion', () => {
  it('falls back to a plain sentence when an undeliverable order has no explanation', async () => {
    mockDoor({ order: { ...heldOrder, approvable: false, question_spec: { ...preferenceQuestion, kind: 'delivery', needed: '', answers: [{ value: 'reject', label: 'Reject this order', cost: { price: 0, date: 'No delivery' } }], preferences: [] } } })
    window.history.replaceState(null, '', '/?view=signoff&order=order-1')
    await openConsole()
    expect(await screen.findByText('The door cannot deliver this order as it stands.')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'This order cannot be delivered.' })).toBeVisible()
  })

  it('shows one short line above a draft card and still sends the server reply in history', async () => {
    const draft = { need, restatement: 'I will order 200 litres.', product_name: 'Milk', pack: '10 × 1 L', packs: 20, total_eur: 240, delivery_date: '2026-11-16' }
    const serverReply = 'Draft only: 20 whole packs of Milk, total €240.00. Say "yes, place it" to confirm.'
    const { calls } = mockDoor({ converse: body => /^hi/i.test(String(body.message)) ? { stage: 'conversation', action: 'reply', reply: 'Hello.', model: {} } : { stage: 'conversation', action: 'draft', reply: serverReply, draft, model: {} } })
    const user = await openConsole()
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Order 200 litres of milk')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('Here is a draft. Nothing is reserved yet. Place it with the button, or say “yes, place it”.')).toBeVisible()
    expect(screen.queryByText(serverReply)).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reset demo' })).toBeEnabled())
    await user.type(screen.getByRole('textbox', { name: 'Your purchasing request' }), 'Hi')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    await screen.findByText('Hello.')
    const history = calls.filter(call => call.body?.stage === 'converse').at(-1)!.body!.history
    expect(history[1]).toEqual({ role: 'assistant', text: serverReply, action: 'draft', draft })
  })

  it('offers "What are my limits?" as a third welcome suggestion', async () => {
    const { calls } = mockDoor({ converse: () => ({ stage: 'conversation', action: 'reply', reply: 'Your per-order cap is €2,000.00.', model: {} }) })
    const user = await openConsole()
    const suggestion = screen.getByRole('button', { name: /What are my limits\?/ })
    expect(suggestion).toHaveTextContent('Your agent reads them from the door. Nothing is ordered.')
    expect(screen.getByRole('button', { name: /What lactose-free milk do you have\?/ })).toBeVisible()
    expect(screen.getByRole('button', { name: /I need 1,000 litres of lactose-free milk by Monday\./ })).toBeVisible()
    await user.click(suggestion)
    expect(await screen.findByText('Your per-order cap is €2,000.00.')).toBeVisible()
    expect(calls.find(call => call.body?.stage === 'converse')?.body?.message).toBe('What are my limits?')
  })
})

describe('Mandate scope and must-hold in words', () => {
  const words = (customer: string) => customer === 'hotel'
    ? { product_scope_label: 'Food and drink', must_hold_label: 'Lactose-free for dairy products' }
    : { product_scope_label: 'Food and drink for the kitchen', must_hold_label: 'Gluten-free for bakery products' }
  const stat = (name: string) => screen.queryByText(name, { selector: '.mandate-stat span' })?.closest('.mandate-stat')

  it('shows product scope and must hold under the money rows, and one sentence in the Limits view', async () => {
    mockDoor({ labels: words })
    const user = await openConsole()
    await waitFor(() => expect(stat('Product scope')).toHaveTextContent('Food and drink'))
    expect(stat('Must hold')).toHaveTextContent('Lactose-free for dairy products')
    expect(screen.getByText('Manufacturer-stated properties')).toBeVisible()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    expect(screen.getByText('Your agent may buy: Food and drink. Always required: Lactose-free for dairy products.')).toBeVisible()
  })

  it('leaves the rows and the sentence out when the door sends no labels', async () => {
    mockDoor()
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await screen.findByText('Spend per order', { selector: '.limit-row strong' })
    expect(stat('Product scope')).toBeUndefined()
    expect(stat('Must hold')).toBeUndefined()
    expect(screen.queryByText(/Your agent may buy/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Always required/)).not.toBeInTheDocument()
  })

  it('shows only the half that the door labels', async () => {
    mockDoor({ labels: () => ({ product_scope_label: 'Food and drink' }) })
    const user = await openConsole()
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    expect(await screen.findByText('Your agent may buy: Food and drink.')).toBeVisible()
    expect(stat('Must hold')).toBeUndefined()
  })

  it('updates on a customer switch and when a limit change returns new limits', async () => {
    mockDoor({ labels: words, buyerSession: true, mandate: { change: (_path, body) => [200, { limits: { rows: limitRows.map(row => row.id === 'per_order_cap' ? { ...row, signed_ceiling: body.value } : row), proposals: [], learned_preferences: [], product_scope_label: 'Dairy only', must_hold_label: 'Lactose-free for dairy products' }, record_id: 'record-scope' }] } })
    const user = await openConsole()
    await waitFor(() => expect(stat('Product scope')).toHaveTextContent('Food and drink'))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'restaurant')
    await waitFor(() => expect(stat('Must hold')).toHaveTextContent('Gluten-free for bakery products'))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Buying for' }), 'hotel')
    await user.click(within(screen.getByRole('navigation')).getByRole('button', { name: /Limits view/ }))
    await user.click((await screen.findAllByRole('button', { name: 'Change this limit' }))[0])
    await user.type(screen.getByRole('spinbutton', { name: /New spend per order/ }), '3000')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await identifyInBankWindow(user)
    await user.click(screen.getByRole('button', { name: 'Sign the new limit' }))
    await waitFor(() => expect(stat('Product scope')).toHaveTextContent('Dairy only'))
    expect(screen.getByText('Your agent may buy: Dairy only. Always required: Lactose-free for dairy products.')).toBeVisible()
  })
})
