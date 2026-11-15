import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { flushSync } from 'react-dom'
import { ArrowDownToLine, ArrowUpRight, Check, CheckCheck, ChevronDown, ChevronRight, CircleHelp, Clock3, DoorOpen, FileCheck2, Fingerprint, History, LoaderCircle, LockKeyhole, MessageSquare, Package, RotateCcw, Send, ShieldCheck, SlidersHorizontal, Sparkles, TriangleAlert, X } from 'lucide-react'
import { api, amount, money, onIdleReset, prettyDate, readable, type Json } from './api'

const views = ['Chat', "Door's answer", 'Sign-off', 'Limits view', 'Record'] as const
type View = typeof views[number]
type LimitChange = { rowId: string; step: 1 | 2 | 3; value: string; identity?: Json; error?: string; working?: boolean }
const changeableLimits = ['per_order_cap', 'monthly_budget']
const cannotChangeLimits = 'This door version cannot change limits yet.'
const cannotIdentify = 'This door version cannot identify buyers yet.'
// The door's reason for refusing a step: detail.message, a string detail, or a short generic sentence.
const genericRefusal = 'The door refused this step. Nothing was changed.'
function refusalReason(err: unknown) {
  const detail = (err as { data?: Json }).data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) return detail.message
  const status = (err as { status?: number }).status
  return status ? genericRefusal : err instanceof Error && err.message ? err.message : genericRefusal
}
const isRefusal = (err: unknown) => { const status = (err as { status?: number }).status || 0; return status >= 400 && status < 500 }
// The simulated bank identification for this buyer session, mirrored per customer
// in sessionStorage (the recording script pre-identifies through this key).
const identityKey = (customer: string) => `aimo-door-identified:${customer}`
const readIdentity = (customer: string) => { try { return window.sessionStorage.getItem(identityKey(customer)) || '' } catch { return '' } }
const writeIdentity = (customer: string, name: string) => { try { if (name) window.sessionStorage.setItem(identityKey(customer), name); else window.sessionStorage.removeItem(identityKey(customer)) } catch { /* storage unavailable: memory only */ } }
// How long "Identified: <name>" stays in view before the action continues. Unit
// tests click Continue instead, so they do not race a timer.
const identifiedPause = import.meta.env.MODE === 'test' ? null : 700
type BankKind = 'session' | 'limit'
type BankWindow = { kind: BankKind; summary: string; working?: boolean; error?: string; identity?: Json; unsupported?: boolean }
type BankResult = { status: 'identified'; identity: Json } | { status: 'cancelled' } | { status: 'unsupported' }
type Action = 'reply' | 'products' | 'clarification' | 'draft' | 'order'
type Message = { role: 'user' | 'assistant'; content: string; kind?: string; action?: Action; products?: Json[]; deliveryDate?: string; draft?: Json; draftState?: 'open' | 'placed' | 'superseded'; source?: string; draftKey?: string; serverText?: string }
const converseActions: Action[] = ['reply', 'products', 'clarification', 'draft', 'order']
const newKey = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
// Earlier turns for stage "converse": oldest first, at most the last 12.
// Order-turn messages are sent as action "order"; an interview question as a reply.
function conversationHistory(messages: Message[]) {
  return messages.slice(-12).map(message => message.role === 'user'
    ? { role: 'user', text: message.content }
    : {
        role: 'assistant', text: message.serverText ?? message.content,
        action: message.action || (message.kind === 'clarification' ? 'clarification' : message.kind === 'question' ? 'reply' : 'order'),
        ...(message.products?.length ? { products: message.products.map(product => ({ product_id: product.product_id, name: product.name })) } : {}),
        draft: message.draft ?? null,
      })
}
// One quiet line: at most four properties, then "+N more".
const propertyLine = (properties: string[]) => properties.slice(0, 4).map(readable).join(', ') + (properties.length > 4 ? ` +${properties.length - 4} more` : '')
// The list heading carries the delivery date, so a bare "Available for <date>." loses the date.
const availabilityText = (product: Json) => {
  const text = String(product.available_text || readable(product.availability || 'unknown'))
  const bare = text.trim().match(/^(Available|Unavailable) for \d{4}-\d{2}-\d{2}\.?$/)
  return bare ? bare[1] : text
}
const proposalTitle = (proposal: Json) => proposal.type === 'anchor' ? 'Make this exact basket the usual' : `Raise current freedom to ${percent(proposal.proposed_freedom ?? proposal.new_freedom)}`
const availabilityTone = (value?: string) => value === 'in_stock' ? 'in-stock' : value === 'limited' || value === 'unavailable' ? 'short' : 'unknown'
const hotelPrompt = 'I need 1,000 litres of lactose-free milk by Monday.'
const restaurantPrompt = 'Same as last week, plus 5 litres more milk.'
const catalogueQuestion = 'What lactose-free milk do you have?'
const limitsQuestion = 'What are my limits?'
const draftLine = 'Here is a draft. Nothing is reserved yet. Place it with the button, or say “yes, place it”.'
const cannotDeliverTitle = 'This order cannot be delivered.'
const cannotDeliverNote = 'It is on hold. You can reject it, or ask your agent for another product or date.'
const idleResetNotice = 'The demo was reset after a period of inactivity.'
const scopeLabels: Record<string, string> = { just_this_once: 'Just this once', always: 'Always for this kind of product', always_ask: 'Always ask me' }
const answerCost = (cost?: Json) => cost ? `${money(cost.price)}, ${cost.date === 'No delivery' ? 'no delivery' : `delivery ${prettyDate(cost.date)}`}` : ''
const percent = (value: unknown) => new Intl.NumberFormat('en-GB', { style: 'percent', maximumFractionDigits: 1 }).format(Number(value || 0))
const limitValue = (row: Json, value: unknown) => row.id === 'departure' ? percent(value) : ['per_order_cap', 'monthly_budget'].includes(row.id) ? money(value) : readable(value)
const readInitialOrderId = () => new URLSearchParams(window.location.search).get('order_id') || new URLSearchParams(window.location.search).get('order') || window.location.pathname.match(/(?:signoff|sign-off)\/([^/]+)/)?.[1]

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

// Reads the monthly budget row's `used` and `remaining` fields from GET /limits.
// When the door does not report them, show only the signed ceiling (in its own
// column) and no invented usage figures.
function BudgetPosition({ row }: { row: Json }) {
  if (!finite(row.used) || !finite(row.remaining)) return <span className="budget-position unreported">Usage not reported</span>
  return <span className="budget-position" data-label="This month"><span><strong>{money(row.used)}</strong><span>used this month</span></span><span><strong>{money(row.remaining)}</strong><span>remaining</span></span></span>
}

// The discretion bar: the track is the signed ceiling, the solid part is the
// freedom the agent has earned, the hatched rest is where it acts with a notice,
// and the end mark is where orders hold for the buyer.
function FreedomBar({ row }: { row: Json }) {
  const ceiling = Number(row.signed_ceiling)
  const freedom = Number(row.current_freedom)
  if (!finite(ceiling) || !finite(freedom) || ceiling <= 0) return null
  const share = Math.max(0, Math.min(1, freedom / ceiling))
  return <span className="freedom-bar" aria-hidden="true"><span className="freedom-track"><span className="freedom-fill" style={{ width: `${share * 100}%` }} /></span><span className="freedom-scale"><span>{freedom > 0 ? `Acts alone up to ${limitValue(row, freedom)}` : 'No freedom yet: your agent asks first'}</span><span>{share < 1 ? `With a notice up to ${limitValue(row, ceiling)}, then hold` : `Holds above ${limitValue(row, ceiling)}`}</span></span></span>
}

function Pill({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill ${tone}`}>{children}</span>
}

function Empty({ icon: Icon = Package, title, children }: { icon?: typeof Package; title: string; children: React.ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={27} strokeWidth={1.5} /></span><h3>{title}</h3><p>{children}</p></div>
}

function CheckList({ checks }: { checks: Json[] }) {
  return <div className="check-list">{checks.map((check, index) => {
    const passed = check.passed ?? check.pass ?? check.status === 'pass'
    return <div className={`check-row ${passed ? 'passed' : 'failed'}`} key={check.id || index}><span className="check-number">{String(check.id || index + 1).padStart(2, '0')}</span><span className="check-icon">{passed ? <Check size={17} /> : <TriangleAlert size={17} />}</span><div><strong>{check.name || `Check ${index + 1}`}</strong><p>{check.reason || check.message}</p></div><Pill tone={passed ? 'green' : 'amber'}>{passed ? 'Pass' : 'Hold'}</Pill></div>
  })}</div>
}

function SignedDocument({ label, token }: { label: string; token?: string }) {
  const [copied, setCopied] = useState(false)
  if (!token) return null
  async function copy() {
    await navigator.clipboard.writeText(token!)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }
  return <details className="signed-document"><summary><span><Fingerprint size={17} />{label}</span><span className="signature-label">ES256 JWS <ChevronDown size={15} /></span></summary><div className="signed-content"><p>A signed copy for the buyer. Verify independently with the door’s public key.</p><code>{token}</code><div className="inline-actions"><button className="button subtle small" onClick={copy}>{copied ? <Check size={15} /> : <FileCheck2 size={15} />}{copied ? 'Copied' : 'Copy signed document'}</button><a href="/.well-known/jwks.json" target="_blank" rel="noreferrer" className="text-link">Public verification key <ArrowUpRight size={14} /></a></div></div></details>
}

function ProductLine({ line }: { line: Json }) {
  const claims = [...new Set([...Object.keys(line.properties || {}), ...Object.keys(line.property_sources || {})])]
  const origin = typeof line.origin === 'object' ? line.origin?.value || line.origin?.country || line.origin?.countries : line.origin
  return <div className="product-line"><span className="product-icon"><Package size={23} strokeWidth={1.5} /></span><div className="product-details"><h4>{line.name || line.names?.en || line.names?.fi || line.product_id || line.id}</h4><p>{amount(line.packs)} whole packs × {amount(line.pack_size)} {line.unit || 'L'} <span className="separator">/</span> {amount(line.quantity)} {line.unit || 'L'}</p><div className="line-tags">{claims.map((claim: string) => <span key={claim}><Check size={12} />{readable(claim)} ({line.properties?.[claim] === 'manufacturer' ? 'manufacturer' : line.property_sources?.[claim] === 'buyer_confirmation' ? 'your confirmation, this product only' : readable(line.property_sources?.[claim] || line.properties?.[claim])})</span>)}{line.product_type && <span>{readable(line.product_type)} (inferred)</span>}{origin && <span>Origin {readable(origin).toUpperCase()} (manufacturer)</span>}</div>{(line.simulated_stock || line.simulated_delivery) && <div className="line-simulation"><Pill tone="amber">Simulated stock / delivery</Pill><p>{line.simulation_explanation || line.stock_source}</p></div>}<details className="line-identity"><summary>Product identity <ChevronDown size={12} /></summary><p>Product {line.product_id || line.id}, GTIN {line.gtin || 'not supplied'}</p>{line.labels && <p>Manufacturer labels: {readable(line.labels)}</p>}{line.subject_to_supplier && <p>Subject to supplier confirmation.</p>}</details></div><div className="line-price"><strong>{money(line.total)}</strong><span>{money(line.unit_price)} / pack</span></div></div>
}

// The record keeps the decision model's product-type probabilities on each line
// of each option considered (options_considered[].lines[].probabilities). Lines
// labelled by rules carry none; then nothing is shown.
function probabilityPairs(value: unknown): [string, number][] {
  const pairs: [string, unknown][] = Array.isArray(value)
    ? value.map((item: Json) => [String(item?.label ?? item?.choice ?? item?.name ?? ''), item?.probability ?? item?.p ?? item?.value])
    : value && typeof value === 'object' ? Object.entries(value as Json) : []
  const valid = pairs.filter((pair): pair is [string, number] => !!pair[0] && finite(pair[1]))
  const scale = valid.some(([, value]) => value > 1) ? 100 : 1
  return valid.map(([label, value]): [string, number] => [label, value / scale]).sort((a, b) => b[1] - a[1])
}

function ModelProbabilities({ record }: { record: Json }) {
  const seen = new Set<string>()
  const lines: { key: string; name: string; pairs: [string, number][] }[] = []
  for (const option of Array.isArray(record.options_considered) ? record.options_considered : []) {
    for (const line of Array.isArray(option?.lines) ? option.lines : []) {
      const key = String(line?.product_id || line?.id || line?.name || '')
      const pairs = probabilityPairs(line?.probabilities)
      if (!pairs.length || seen.has(key)) continue
      seen.add(key)
      lines.push({ key, name: line.name || line.names?.en || line.names?.fi || line.product_id || line.id || 'A product', pairs })
    }
  }
  if (!lines.length) return null
  return <details className="technical-detail model-probabilities"><summary><span><Sparkles size={17} />How sure the model was about product types</span><ChevronDown size={16} /></summary><div><p>The decision model only names the product type. It never decides allergens or lactose claims; those come from the manufacturer.</p><ul>{lines.map(line => <li key={line.key}><p><strong>{line.name}</strong>: {line.pairs.map(([label, value]) => `${readable(label)} ${percent(value)}`).join(', ')}.</p></li>)}</ul></div></details>
}

export default function App() {
  const [initialOrderId] = useState(readInitialOrderId)
  const [view, setView] = useState<View>(() => {
    const requested = (new URLSearchParams(window.location.search).get('view') || '').toLowerCase().replace(/[^a-z]/g, '')
    return initialOrderId ? 'Sign-off' : views.find(name => name.toLowerCase().replace(/[^a-z]/g, '') === requested) || (requested === 'limits' ? 'Limits view' : 'Chat')
  })
  const [customers, setCustomers] = useState<Json[]>([])
  const [customerId, setCustomerId] = useState('hotel')
  const [health, setHealth] = useState<Json | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const [idleNotice, setIdleNotice] = useState(false)
  const [result, setResult] = useState<Json | null>(null)
  const [previewNeed, setPreviewNeed] = useState<Json | null>(null)
  const [order, setOrder] = useState<Json | null>(null)
  const [record, setRecord] = useState<Json | null>(null)
  const [limits, setLimits] = useState<Json | null>(null)
  const [usual, setUsual] = useState<Json | null>(null)
  const [selectedOption, setSelectedOption] = useState(0)
  const [limitedStock, setLimitedStock] = useState(false)
  const [accessCode, setAccessCode] = useState('')
  const [gate, setGate] = useState(false)
  const [gateInput, setGateInput] = useState('')
  const [editQuantity, setEditQuantity] = useState('')
  const [answerScope, setAnswerScope] = useState('just_this_once')
  const [answer, setAnswer] = useState('approve')
  const [standingConfirmed, setStandingConfirmed] = useState(false)
  const [priceConfirmed, setPriceConfirmed] = useState(false)
  const [propertyConfirmations, setPropertyConfirmations] = useState<Record<string, boolean>>({})
  const [interview, setInterview] = useState<Json | null>(null)
  const [interviewAnswer, setInterviewAnswer] = useState('')
  const [interviewScope, setInterviewScope] = useState('just_this_once')
  const [pendingChatAnswer, setPendingChatAnswer] = useState<Json | null>(null)
  const [chatConfirmed, setChatConfirmed] = useState(false)
  const messageEnd = useRef<HTMLDivElement>(null)
  const requestBox = useRef<HTMLTextAreaElement>(null)
  const converseUnsupported = useRef(false)
  // Changing a signed ceiling: new value, simulated identification, review and sign.
  const [limitChange, setLimitChange] = useState<LimitChange | null>(null)
  const [changeRecords, setChangeRecords] = useState<Record<string, string>>({})
  const [identifiedName, setIdentifiedName] = useState(() => readIdentity('hotel'))
  // A refusal shown next to the control that was pressed.
  const [refusal, setRefusal] = useState<{ where: string; message: string } | null>(null)
  const refusalAlert = (where: string) => refusal?.where === where ? <p className="refusal" role="alert"><TriangleAlert size={17} /><span>{refusal.message}</span></p> : null
  const [bankWindow, setBankWindow] = useState<BankWindow | null>(null)
  const bankResolve = useRef<((result: BankResult) => void) | null>(null)
  const bankReturnFocus = useRef<HTMLElement | null>(null)
  const bankRef = useRef<HTMLDivElement>(null)
  const currentCustomer = customers.find(customer => customer.id === customerId)
  const currentMandate = currentCustomer?.mandate
  // The mandate's product scope and must-hold, in words (GET /limits, else the customer list).
  const labelOf = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim() : ''
  const scopeLabel = labelOf(limits?.product_scope_label) || labelOf(currentCustomer?.product_scope_label)
  const mustHoldLabel = labelOf(limits?.must_hold_label) || labelOf(currentCustomer?.must_hold_label)
  const mandateSentence = [scopeLabel && `Your agent may buy: ${scopeLabel.replace(/\.$/, '')}.`, mustHoldLabel && `Always required: ${mustHoldLabel.replace(/\.$/, '')}.`].filter(Boolean).join(' ')
  const options: Json[] = result?.match?.options || []
  const checks: Json[] = order?.checks || record?.checks || []
  const failedChecks = checks.filter(check => !(check.passed ?? check.pass ?? check.status === 'pass'))
  const offerPayload = order?.offer_payload || result?.offer?.payload
  const consideredOptions: Json[] = options.length ? options : offerPayload?.options_considered || []
  const held = order?.status === 'requires_escalation'
  // A held order the door cannot deliver can only be rejected or changed.
  const approvable = order?.approvable !== false
  const notified = order?.status === 'committed' && String(order?.tier || '').toLowerCase() === 'notify'
  const committed = order?.status === 'committed'
  const missingProperties: Json[] = order?.question_spec?.missing_properties || []
  const propertiesConfirmed = missingProperties.length > 0 && missingProperties.every(item => !item.contradicted && propertyConfirmations[`${item.product_id}:${item.property}`])
  const pendingAnswer = order?.pending_answer
  const pendingAnswerMatches = pendingAnswer?.answer === answer && pendingAnswer?.scope === answerScope && pendingAnswer?.question_hash === order?.question_spec?.hash
  const basketNeed = result?.need?.lines?.length ? result.need : previewNeed?.lines?.length ? previewNeed : null
  const departureLimit = limits?.rows?.find((row: Json) => row.id === 'departure')
  const cutoffTimes = (offerPayload?.lines || []).map((line: Json) => Date.parse(line.cutoff)).filter(Number.isFinite)
  const cutoff = cutoffTimes.length ? new Date(Math.min(...cutoffTimes)) : null
  const undoExpired = cutoff ? cutoff.getTime() <= Date.now() : false

  const request = useCallback((path: string, body?: unknown) => api(path, body, accessCode), [accessCode])

  // Simulated buyer login. Human actions and human reads carry X-Buyer-Session so
  // the door can tell the buyer from the bot. Tokens live in memory only, one per
  // customer. A door without POST /demo/buyer-session (404) is used exactly as before.
  const buyerSessions = useRef(new Map<string, Promise<string>>())
  const buyerSessionUnsupported = useRef(false)
  const [buyerSessionActive, setBuyerSessionActive] = useState(false)
  const startBuyerSession = useCallback((id: string) => {
    const pending = api('/demo/buyer-session', { customer_id: id }, accessCode).then(session => {
      const token = typeof session?.token === 'string' ? session.token : ''
      if (token) { buyerSessionUnsupported.current = false; setBuyerSessionActive(true) }
      return token
    }, err => {
      if ([404, 405].includes((err as { status?: number }).status || 0)) { buyerSessionUnsupported.current = true; setBuyerSessionActive(false) }
      return ''
    })
    buyerSessions.current.set(id, pending)
    return pending
  }, [accessCode])
  const humanRequest = useCallback(async (sessionCustomer: string, path: string, body?: unknown, extraHeaders: Record<string, string> = {}) => {
    const send = (token: string) => api(path, body, accessCode, token ? { ...extraHeaders, 'X-Buyer-Session': token } : extraHeaders)
    const used = buyerSessions.current.get(sessionCustomer) || startBuyerSession(sessionCustomer)
    const token = await used
    try { return await send(token) } catch (err) {
      if ((err as { status?: number }).status !== 401 || buyerSessionUnsupported.current) throw err
      // Several calls can meet the same expired token; only the first starts a new session.
      const current = buyerSessions.current.get(sessionCustomer)
      const fresh = await (current && current !== used ? current : startBuyerSession(sessionCustomer))
      if (!fresh) throw err
      return send(fresh)
    }
  }, [accessCode, startBuyerSession])
  const showError = useCallback((err: unknown) => {
    if ((err as { idleReset?: boolean }).idleReset) return
    if ((err as { status?: number }).status === 401 || (err as { status?: number }).status === 403) setGate(true)
    setError(err instanceof Error ? err.message : 'The request could not be completed. Please try again.')
  }, [])

  const refreshContext = useCallback(async () => {
    const context = await Promise.allSettled([humanRequest(customerId, `/limits?customer_id=${customerId}`), humanRequest(customerId, `/usual-order?customer_id=${customerId}`)])
    if (context[0].status === 'fulfilled') setLimits(context[0].value)
    if (context[1].status === 'fulfilled') setUsual(context[1].value)
  }, [customerId, humanRequest])

  useEffect(() => { void startBuyerSession(customerId) }, [customerId, startBuyerSession])
  useEffect(() => { setIdentifiedName(readIdentity(customerId)) }, [customerId])

  // Idle auto-reset: the X-Demo-Idle-Reset header on any response, or a newer
  // idle_reset_at from GET /demo/customers (another viewer met the reset first),
  // reloads the console as after "Reset demo". Absent header and field: nothing.
  const idleResetSeen = useRef<string | null | undefined>(undefined)
  const idleResetting = useRef(false)
  const idleResetHandler = useRef<() => void>(() => {})
  const applyCustomers = (demo: Json) => {
    setCustomers(demo.customers || demo)
    const at = typeof demo?.idle_reset_at === 'string' ? demo.idle_reset_at : null
    const changed = idleResetSeen.current !== undefined && at !== null && at !== idleResetSeen.current
    if (idleResetSeen.current === undefined || at !== null) idleResetSeen.current = at
    if (changed) idleResetHandler.current()
  }
  idleResetHandler.current = () => {
    if (idleResetting.current) return
    idleResetting.current = true
    setMessages([]); setResult(null); setOrder(null); setRecord(null); setInput(''); setEditQuantity(''); setLimitedStock(false); setView('Chat'); setLimitChange(null); setChangeRecords({}); clearIdentities()
    setPreviewNeed(null)
    setInterview(null); setPendingChatAnswer(null); setInterviewAnswer(''); setChatConfirmed(false)
    setError('')
    setIdleNotice(true)
    void (async () => {
      try {
        await startBuyerSession(customerId)
        applyCustomers(await request('/demo/customers'))
        await refreshContext()
      } catch (err) { showError(err) } finally { idleResetting.current = false }
    })()
  }
  useEffect(() => { onIdleReset(() => idleResetHandler.current()); return () => onIdleReset(null) }, [])

  useEffect(() => {
    let active = true
    Promise.all([request('/demo/customers'), request('/healthz')]).then(([demo, status]) => {
      if (!active) return
      applyCustomers(demo)
      setHealth(status)
      setGate(false)
      setError('')
    }).catch(err => { if (active) showError(err) })
    return () => { active = false }
  }, [request, showError])

  useEffect(() => { void refreshContext() }, [refreshContext])
  useEffect(() => { messageEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [messages, busy])
  useEffect(() => { if (toast) { const timer = window.setTimeout(() => setToast(''), 4500); return () => window.clearTimeout(timer) } }, [toast])
  useEffect(() => {
    if (!initialOrderId) return
    const path = `/orders/${encodeURIComponent(initialOrderId)}`
    // The order's customer is unknown until it loads, so a refused read is tried
    // once with each other demo customer's buyer session.
    const load = async () => {
      try { return await humanRequest(customerId, path) } catch (err) {
        if (buyerSessionUnsupported.current || ![401, 403].includes((err as { status?: number }).status || 0)) throw err
        for (const other of ['hotel', 'restaurant'].filter(id => id !== customerId)) {
          try { return await humanRequest(other, path) } catch { /* try the next customer */ }
        }
        throw err
      }
    }
    load().then(next => { setOrder(next); if (next.customer_id) setCustomerId(next.customer_id) }).catch(showError)
    // Only on the first load (and after a new access code).
  }, [humanRequest, showError])
  useEffect(() => {
    if (order?.record_id) humanRequest(order.customer_id || customerId, `/records/${encodeURIComponent(order.record_id)}`).then(setRecord).catch(showError)
  }, [order?.record_id, humanRequest, showError])

  useEffect(() => {
    setStandingConfirmed(false); setPriceConfirmed(false); setPropertyConfirmations({}); setPendingChatAnswer(null); setChatConfirmed(false)
    setAnswer(order?.approvable === false ? 'reject' : 'approve'); setAnswerScope('just_this_once'); setInterviewScope('just_this_once'); setRefusal(null)
  }, [order?.id, order?.offer, order?.question_spec?.hash, order?.approvable])

  function changeCustomer(id: string) {
    setCustomerId(id)
    setMessages([])
    setResult(null)
    setPreviewNeed(null)
    setOrder(null)
    setRecord(null)
    setLimits(null)
    setUsual(null)
    setError('')
    setInput('')
    setEditQuantity('')
    setView('Chat')
    setLimitedStock(false)
    setLimitChange(null); setChangeRecords({})
    writeIdentity(customerId, ''); setIdentifiedName(readIdentity(id))
    setInterview(null); setPendingChatAnswer(null); setInterviewAnswer(''); setChatConfirmed(false)
  }

  // Order turns: the previous order's state is replaced, exactly as before.
  function clearOrderState() {
    setResult(null)
    setPreviewNeed(null)
    setOrder(null)
    setRecord(null)
    setInterview(null); setPendingChatAnswer(null); setInterviewAnswer(''); setChatConfirmed(false)
    setSelectedOption(0)
  }

  async function applyOrderResponse(response: Json, restatement?: string) {
    setResult({ ...response, restatement: response.restatement || restatement })
    setOrder(response.order || null)
    const outcome = response.order?.status === 'requires_escalation'
      ? 'Your order needs a sign-off. The door has kept the reasons and reserved the available stock. Review the choices in Sign-off.'
      : response.order?.status === 'committed'
        ? String(response.order?.tier).toLowerCase() === 'notify'
          ? 'Your order is committed with a notice. This is approval by silence: you can undo it before the order cutoff.'
          : 'Your order is committed. Every check passed within your signed limits. Your signed receipt and decision record are ready.'
        : response.message || response.messages?.at(-1)?.text || 'The door has answered. Review the options and the decision details.'
    setMessages(previous => [...previous, { role: 'assistant', content: outcome, kind: 'outcome', action: 'order' }])
    if (response.order?.status === 'requires_escalation' && response.order.question_spec && response.order.question_spec.kind !== 'property_confirmation') {
      setBusyLabel('Your agent is preparing the question')
      const conversation = await humanRequest(customerId, '/bot/chat', { customer_id: customerId, stage: 'interview', order_id: response.order.id })
      setInterview(conversation)
      setMessages(previous => [...previous, { role: 'assistant', content: conversation.question, kind: 'question' }])
    }
    await refreshContext()
  }

  // The earlier two-step flow, used when the door predates stage "converse".
  async function runLegacyOrder(message: string) {
    clearOrderState()
    setBusyLabel('Understanding your request')
    const base = { customer_id: customerId, message, simulate_limited_stock: limitedStock }
    const preview = await humanRequest(customerId, '/bot/chat', { ...base, stage: 'preview' })
    if (preview.stage === 'clarification' || preview.status === 'requires_clarification') {
      const question = preview.question || preview.clarification || preview.messages?.at(-1)?.text || 'Please send a complete request with the product, quantity, required properties and a future delivery date.'
      setMessages(previous => [...previous, { role: 'assistant', content: question, kind: 'clarification' }])
      return
    }
    if (!preview.restatement || !preview.need) throw new Error('Your agent could not restate a complete need. No order has been placed.')
    flushSync(() => { setMessages(previous => [...previous, { role: 'assistant', content: preview.restatement, kind: 'restatement', action: 'order' }]); setPreviewNeed(preview.need) })
    setBusyLabel('Matching products and checking your signed limits')
    const response = await humanRequest(customerId, '/bot/chat', { ...base, stage: 'execute', need: preview.need, restatement: preview.restatement })
    await applyOrderResponse(response, preview.restatement)
  }

  async function runRequest(event?: FormEvent, suggestion?: string) {
    event?.preventDefault()
    const message = (suggestion || input).trim()
    if (!message || busy) return
    const history = conversationHistory(messages)
    setBusy(true)
    setError('')
    setInput('')
    // A new turn supersedes any draft that is still open.
    setMessages(previous => [...previous.map(item => item.draftState === 'open' ? { ...item, draftState: 'superseded' as const } : item), { role: 'user', content: message }])
    try {
      if (converseUnsupported.current) { await runLegacyOrder(message); return }
      setBusyLabel('Your agent is reading your message')
      let response: Json
      try {
        response = await humanRequest(customerId, '/bot/chat', { stage: 'converse', customer_id: customerId, message, history, simulate_limited_stock: limitedStock })
      } catch (err) {
        const failure = err as { status?: number; message?: string }
        if (failure.status === 422 && /stage must be/i.test(failure.message || '')) {
          converseUnsupported.current = true
          await runLegacyOrder(message)
          return
        }
        throw err
      }
      const action: Action = converseActions.includes(response.action) ? response.action : 'reply'
      if (action === 'order') {
        clearOrderState()
        const restatement = response.restatement || response.reply
        if (restatement) setMessages(previous => [...previous, { role: 'assistant', content: restatement, kind: 'restatement', action: 'order' }])
        await applyOrderResponse(response, restatement)
        return
      }
      const reply = String(response.reply || response.clarification || response.question || '').trim() || (action === 'draft' ? 'Here is a draft order for you to check.' : 'Your agent has no answer to that yet.')
      const products = Array.isArray(response.products) ? response.products.slice(0, 6) : undefined
      setMessages(previous => [...previous, {
        // A draft card says it all; the server's own reply still goes back in history.
        role: 'assistant', content: action === 'draft' && response.draft ? draftLine : reply, action, source: message,
        ...(action === 'draft' && response.draft ? { serverText: reply } : {}),
        ...(products?.length ? { products, deliveryDate: response.delivery_date } : {}),
        ...(action === 'draft' && response.draft ? { draft: response.draft, draftState: 'open' as const, draftKey: newKey() } : {}),
      }])
    } catch (err) { showError(err) } finally { setBusy(false); setBusyLabel('') }
  }

  async function placeDraft(index: number) {
    const turn = messages[index]
    if (!turn?.draft || turn.draftState !== 'open' || busy) return
    setBusy(true); setError(''); setRefusal(null)
    setBusyLabel('Placing your order and checking your signed limits')
    setMessages(previous => previous.map((item, position) => position === index ? { ...item, draftState: 'placed' as const } : item.draftState === 'open' ? { ...item, draftState: 'superseded' as const } : item).concat({ role: 'user', content: 'Place this order.' }))
    try {
      clearOrderState()
      const response = await humanRequest(customerId, '/bot/chat', { stage: 'execute', customer_id: customerId, need: turn.draft.need, restatement: turn.draft.restatement, message: turn.source || turn.draft.restatement, simulate_limited_stock: limitedStock, idempotency_key: turn.draftKey })
      setMessages(previous => [...previous, { role: 'assistant', content: response.restatement || turn.draft!.restatement, kind: 'restatement', action: 'order' }])
      await applyOrderResponse(response, turn.draft.restatement)
    } catch (err) {
      // The order was not confirmed: let the buyer try again.
      setMessages(previous => previous.map((item, position) => position === index ? { ...item, draftState: 'open' as const } : item))
      if (isRefusal(err)) setRefusal({ where: `draft-${index}`, message: refusalReason(err) })
      if (!isRefusal(err) || [401, 403].includes((err as { status?: number }).status || 0)) showError(err)
    } finally { setBusy(false); setBusyLabel('') }
  }

  async function resetDemo() {
    setBusy(true)
    setError('')
    try {
      await humanRequest(customerId, '/demo/reset', { customer_id: customerId })
      // A reset may end every buyer session; start a fresh one for this customer.
      await startBuyerSession(customerId)
      applyCustomers(await request('/demo/customers'))
      setMessages([]); setResult(null); setOrder(null); setRecord(null); setInput(''); setEditQuantity(''); setLimitedStock(false); setView('Chat'); setLimitChange(null); setChangeRecords({}); clearIdentities()
      setPreviewNeed(null)
      setInterview(null); setPendingChatAnswer(null); setInterviewAnswer(''); setChatConfirmed(false)
      await refreshContext()
      setIdleNotice(false)
      setToast('Demo reset. Orders, reservations and learning restored to the initial state.')
    } catch (err) { showError(err) } finally { setBusy(false) }
  }

  async function refreshBuyerContext() {
    applyCustomers(await request('/demo/customers'))
    await refreshContext()
  }

  async function recoverChangedOrder(err: unknown, where = 'signoff') {
    // A refusal (4xx) is shown in place, next to the control; 401/403 keep the access check.
    if (isRefusal(err)) setRefusal({ where, message: refusalReason(err) })
    if (!isRefusal(err) || [401, 403].includes((err as { status?: number }).status || 0)) showError(err)
    if (order && [409, 502].includes((err as { status?: number }).status || 0)) {
      try {
        const latest = await humanRequest(order.customer_id || customerId, `/orders/${order.id}`)
        setOrder(latest)
        setPriceConfirmed(false); setStandingConfirmed(false); setPendingChatAnswer(null); setChatConfirmed(false)
        if (latest.requires_price_confirmation) {
          setView('Sign-off')
          setToast('The offer changed. Review the new price and confirm it before approving.')
        }
      } catch (refreshError) { showError(refreshError) }
    }
  }

  function clearIdentities() {
    for (const id of ['hotel', 'restaurant', customerId]) writeIdentity(id, '')
    setIdentifiedName('')
  }

  function askBank(kind: BankKind, summary: string, error?: string) {
    bankReturnFocus.current = document.activeElement as HTMLElement | null
    return new Promise<BankResult>(resolve => { bankResolve.current = resolve; setBankWindow({ kind, summary, error }) })
  }

  function finishBank(result: BankResult) {
    bankResolve.current?.(result)
    bankResolve.current = null
    setBankWindow(null)
    window.setTimeout(() => bankReturnFocus.current?.focus?.(), 0)
  }

  async function identifyInBank() {
    if (!bankWindow || bankWindow.working) return
    setBankWindow(current => current && { ...current, working: true, error: undefined })
    try {
      const identity = await humanRequest(customerId, '/demo/identify', { customer_id: customerId, method: 'bank' })
      const name = String(identity?.person?.name || '')
      setIdentifiedName(name); writeIdentity(customerId, name)
      setBankWindow(current => current && { ...current, working: false, identity })
      if (identifiedPause !== null) window.setTimeout(() => { if (bankResolve.current) finishBank({ status: 'identified', identity }) }, identifiedPause)
    } catch (err) {
      const status = (err as { status?: number }).status || 0
      setBankWindow(current => current && ([404, 405].includes(status) ? { ...current, working: false, unsupported: true, error: undefined } : { ...current, working: false, error: err instanceof Error && err.message ? err.message : 'The identification did not complete. Try again or cancel.' }))
    }
  }

  // Approvals and proposal acceptances: identify once per buyer session.
  async function ensureSessionIdentity(summary: string) {
    if (identifiedName) return true
    const result = await askBank('session', summary)
    return result.status !== 'cancelled'
  }

  const approvalSummary = () => `Approve this order: ${money(offerPayload?.total ?? order?.offer_payload?.total)}, delivery ${prettyDate(offerPayload?.delivery_date ?? order?.offer_payload?.delivery_date)}`

  async function orderAction(action: 'approve' | 'reject' | 'edit' | 'undo' | 'prepare_answer' | 'acknowledge', confirmAnswer = false) {
    if (!order) return
    const where = confirmAnswer || action === 'prepare_answer' ? 'signoff-answer' : action === 'undo' || action === 'acknowledge' ? 'notice' : 'signoff'
    setBusy(true)
    setError(''); setRefusal(null)
    try {
      const body: Json = { action, customer_id: order.customer_id || customerId }
      if (action === 'acknowledge') body.confirmed = true
      if (action === 'edit') body.quantity = Number(editQuantity)
      if (action === 'prepare_answer') Object.assign(body, { answer, scope: answerScope })
      if (confirmAnswer) {
        if (!pendingAnswerMatches || !standingConfirmed) throw new Error('Review and confirm the door’s current answer summary first.')
        Object.assign(body, { answer: pendingAnswer.answer, scope: pendingAnswer.scope, confirmed: true, confirmation_summary: pendingAnswer.confirmation_summary, question_hash: pendingAnswer.question_hash })
      }
      if (action === 'approve') {
        if (order.requires_price_confirmation) {
          if (!priceConfirmed) throw new Error('Review the refreshed offer and its price before approving.')
          body.offer = order.offer
        }
        if (missingProperties.length) {
          if (!propertiesConfirmed) throw new Error('Confirm each exact product and property before approving. Manufacturer contradictions cannot be overridden.')
          body.confirmed = true
          body.confirmations = missingProperties.map(item => ({ product_id: item.product_id, property: item.property, confirmed: true }))
        }
      }
      if (action === 'approve' && !approvable) throw new Error('This order cannot be delivered, so it cannot be approved. Reject it or change it.')
      if (action === 'approve' && !(await ensureSessionIdentity(approvalSummary()))) return
      const response = await humanRequest(order.customer_id || customerId, `/orders/${order.id}/${action === 'undo' ? 'undo' : 'signoff'}`, body)
      const next = response.order || response
      setOrder(next)
      if (action === 'prepare_answer') {
        setStandingConfirmed(false)
        setToast('Review the exact summary from the door before confirming your answer.')
      } else {
        await refreshBuyerContext()
        if (action === 'acknowledge') setToast('Your review is recorded. Current freedom changes only after you accept a proposal.')
        else if (action === 'undo') setToast('Order undone. Its stock is released and current freedom is tightened.')
        else if (action === 'reject') setToast('Order rejected. Its stock reservation has been released.')
        else if (next.status === 'requires_escalation') setToast('The order is still held. Review the current reasons and answer summary.')
        else setToast(action === 'approve' ? 'Order approved. Your receipt is ready.' : 'Edited order checked again.')
        if (next.status !== 'requires_escalation') { setInterview(null); setPendingChatAnswer(null) }
      }
    } catch (err) { await recoverChangedOrder(err, where) } finally { setBusy(false) }
  }

  async function answerInterview(event: FormEvent) {
    event.preventDefault()
    if (!order || !interviewAnswer.trim()) return
    setBusy(true); setError(''); setRefusal(null); setBusyLabel('Your agent is preparing your answer for review')
    try {
      const message = interviewAnswer.trim()
      setMessages(previous => [...previous, { role: 'user', content: message }])
      const response = await humanRequest(customerId, '/bot/chat', { customer_id: customerId, stage: 'answer', order_id: order.id, message, scope: interviewScope })
      if (response.pending_answer && response.confirmation_summary) {
        setPendingChatAnswer({ ...response.pending_answer, confirmation_summary: response.confirmation_summary })
        setChatConfirmed(false)
      } else if (response.clarification) {
        setMessages(previous => [...previous, { role: 'assistant', content: response.clarification, kind: 'question' }])
      }
    } catch (err) { await recoverChangedOrder(err, 'chat-answer') } finally { setBusy(false); setBusyLabel('') }
  }

  async function confirmInterview() {
    if (!order || !pendingChatAnswer || !chatConfirmed) return
    setBusy(true); setError(''); setRefusal(null); setBusyLabel('Submitting your confirmed answer')
    try {
      if (pendingChatAnswer.answer !== 'reject' && approvable && !(await ensureSessionIdentity(approvalSummary()))) return
      const response = await humanRequest(customerId, '/bot/chat', { customer_id: customerId, stage: 'confirm', order_id: order.id, confirmed: true, confirmation_summary: pendingChatAnswer.confirmation_summary, question_hash: pendingChatAnswer.question_hash })
      setOrder(response.order)
      setMessages(previous => [...previous, { role: 'assistant', content: response.messages?.at(-1)?.text || 'Your confirmed answer is recorded.', kind: 'outcome' }])
      setPendingChatAnswer(null); setChatConfirmed(false); setInterview(null); setInterviewAnswer('')
      await refreshBuyerContext()
    } catch (err) { await recoverChangedOrder(err, 'chat-confirm') } finally { setBusy(false); setBusyLabel('') }
  }

  async function limitAction(path: string, confirmTitle?: string, where = 'limits') {
    setBusy(true)
    setError(''); setRefusal(null)
    try {
      if (confirmTitle && !(await ensureSessionIdentity(confirmTitle))) return
      await humanRequest(customerId, path, { customer_id: customerId, mandate: currentMandate, confirmed: true })
      applyCustomers(await request('/demo/customers'))
      await refreshContext()
      setToast('Limits updated.')
    } catch (err) {
      if (isRefusal(err)) setRefusal({ where, message: refusalReason(err) })
      if (!isRefusal(err) || [401, 403].includes((err as { status?: number }).status || 0)) showError(err)
    } finally { setBusy(false) }
  }

  async function openRecord(recordId: string) {
    setView('Record')
    try { setRecord(await humanRequest(customerId, `/records/${encodeURIComponent(recordId)}`)) } catch (err) { showError(err) }
  }

  const changeFailure = (err: unknown) => {
    const status = (err as { status?: number }).status || 0
    return [404, 405].includes(status) ? cannotChangeLimits : refusalReason(err)
  }

  // A limit change always identifies afresh: the proof is single-use.
  async function identifyForLimit(error?: string) {
    const change = limitChange
    const row = limits?.rows?.find((item: Json) => item.id === change?.rowId)
    if (!change || !row) return
    setLimitChange({ ...change, step: 2, identity: undefined, working: false, error: undefined })
    const result = await askBank('limit', `${row.name}: ${money(row.signed_ceiling)} → ${money(Number(change.value))}`, error)
    setLimitChange(current => current && (result.status === 'identified' ? { ...current, step: 3, identity: result.identity, error: undefined } : { ...current, step: 1, identity: undefined }))
  }

  async function signLimitChange() {
    if (!limitChange?.identity) return
    const { rowId, value, identity } = limitChange
    setLimitChange({ ...limitChange, working: true, error: undefined })
    try {
      const response = await humanRequest(customerId, `/limits/${encodeURIComponent(rowId)}/change`, { customer_id: customerId, value: Number(value) }, { 'X-Identification-Proof': String(identity.proof) })
      if (response.limits) setLimits(response.limits)
      if (response.record_id) setChangeRecords(previous => ({ ...previous, [rowId]: String(response.record_id) }))
      setLimitChange(null)
      setToast('Limit changed. Your new mandate version is signed.')
      try { applyCustomers(await request('/demo/customers')) } catch { /* the mandate copy refreshes on the next load */ }
    } catch (err) {
      const status = (err as { status?: number }).status
      // An expired, used or wrong-customer proof: identify again.
      if (status === 403) { await identifyForLimit(changeFailure(err)); return }
      setLimitChange(current => current && (status === 422 ? { ...current, step: 1, working: false, error: changeFailure(err) } : { ...current, working: false, error: changeFailure(err) }))
    }
  }

  async function openSignoff() {
    setView('Sign-off')
    if (order) {
      try { setOrder(await humanRequest(order.customer_id || customerId, `/orders/${order.id}`)); setPriceConfirmed(false) } catch (err) { showError(err) }
    }
  }

  function downloadRecord() {
    if (!record) return
    const url = URL.createObjectURL(new Blob([JSON.stringify({ record, receipt: order?.receipt }, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = `aimos-door-record-${record.id}.json`; anchor.click()
    URL.revokeObjectURL(url)
  }

  const renderInterview = () => !held || !interview ? null : <section className="panel interview-panel">
    <div className="panel-heading"><div><h3>{approvable ? 'Answer your agent’s question' : cannotDeliverTitle}</h3><p>{approvable ? 'The order stays on hold until you confirm the door’s summary of your answer.' : cannotDeliverNote}</p></div><MessageSquare size={22} /></div>
    <div className="question-body">
      {!approvable && renderCannotDeliver()}
      <ul className="answer-costs">{(interview.accepted_answers || []).filter((item: Json) => approvable || item.value !== 'approve').map((item: Json) => <li key={item.value || item.id}><strong>{item.label}</strong><span>{answerCost(item.cost)}</span></li>)}</ul>
      {pendingChatAnswer ? <div className="summary-review"><p className="summary-label">The door’s summary of your answer</p><p className="confirmation-summary">{pendingChatAnswer.confirmation_summary}</p><label className="checkbox-label"><input type="checkbox" checked={chatConfirmed} onChange={event => setChatConfirmed(event.target.checked)} />I confirm this exact answer and scope.</label><div className="inline-actions"><button className="button primary" disabled={busy || !chatConfirmed} onClick={() => void confirmInterview()}>Confirm and submit answer <Check size={16} /></button><button className="button subtle" disabled={busy} onClick={() => { setPendingChatAnswer(null); setChatConfirmed(false) }}>Change answer</button></div></div>
        : <form onSubmit={event => void answerInterview(event)}><label htmlFor="interview-answer">Your answer in your own words</label><textarea id="interview-answer" value={interviewAnswer} onChange={event => setInterviewAnswer(event.target.value)} rows={2} placeholder="Tell your agent which option you accept or reject." disabled={busy} required /><label htmlFor="interview-scope">Scope of this answer</label><select id="interview-scope" value={interviewScope} onChange={event => setInterviewScope(event.target.value)} disabled={busy}>{(interview.scope_options || ['just_this_once']).map((scope: string) => <option key={scope} value={scope} disabled={scope !== 'just_this_once' && !interview.question_spec?.preferences?.length}>{scopeLabels[scope] || scope}</option>)}</select><button type="submit" className="button primary" disabled={busy || !interviewAnswer.trim()}>Review my answer</button>{refusalAlert('chat-answer')}</form>}
      {refusalAlert('chat-confirm')}
    </div>
  </section>

  const renderCannotDeliver = () => <p className="refusal cannot-deliver" role="note"><TriangleAlert size={17} /><span>{String(order?.question_spec?.needed || '').trim() || 'The door cannot deliver this order as it stands.'}</span></p>

  const renderQuestion = () => !held || !order?.question_spec ? null : <section className="panel question-panel">
    <div className="panel-heading"><div><h3>{approvable ? order.question_spec.needed : 'Reject this order'}</h3></div><CircleHelp size={23} /></div>
    <div className="question-body">
      {order.question_spec.kind === 'property_confirmation' ? <div className="property-confirmations"><p className="muted small-text">The manufacturer has not stated these properties. Confirm only from your own product-specific knowledge. No answer applies to another product or property.</p>{missingProperties.map(item => <div key={`${item.product_id}:${item.property}`} className="property-confirmation">{item.contradicted ? <p className="contradiction"><TriangleAlert size={17} />The manufacturer states contrary information for {readable(item.property)} on {item.product_name || item.product_id}. Approval is unavailable.</p> : <label className="checkbox-label"><input type="checkbox" checked={!!propertyConfirmations[`${item.product_id}:${item.property}`]} onChange={event => setPropertyConfirmations(previous => ({ ...previous, [`${item.product_id}:${item.property}`]: event.target.checked }))} /><span>I explicitly confirm {readable(item.property)} for {item.product_name || item.product_id} (product {item.product_id}) only.</span></label>}</div>)}</div>
        : <><label>Choose an answer<select value={answer} onChange={event => { setAnswer(event.target.value); setStandingConfirmed(false) }}>{(order.question_spec.answers || []).filter((item: Json) => approvable || item.value !== 'approve').map((item: Json) => <option key={item.value} value={item.value}>{item.label} ({answerCost(item.cost)})</option>)}</select></label><label>How should this answer apply?<select value={answerScope} onChange={event => { setAnswerScope(event.target.value); setStandingConfirmed(false) }}>{(order.question_spec.scope_options || ['just_this_once']).map((scope: string) => <option key={scope} value={scope} disabled={scope !== 'just_this_once' && !order.question_spec.preferences?.length}>{scopeLabels[scope] || scope}</option>)}</select></label><p className="muted small-text">{order.question_spec.scope_note}</p><button className="button subtle" disabled={busy} onClick={() => void orderAction('prepare_answer')}>Review answer summary</button>{pendingAnswerMatches && <div className="summary-review"><p className="summary-label">The door’s summary of your answer</p><p className="confirmation-summary">{pendingAnswer.confirmation_summary}</p><label className="checkbox-label"><input type="checkbox" checked={standingConfirmed} onChange={event => setStandingConfirmed(event.target.checked)} />I confirm the displayed answer and its exact scope.</label><button className="button primary" disabled={busy || !standingConfirmed || (answer === 'approve' && order.requires_price_confirmation && !priceConfirmed)} onClick={() => void orderAction(answer === 'reject' ? 'reject' : 'approve', true)}>Confirm answer <Check size={16} /></button></div>}{refusalAlert('signoff-answer')}</>}
    </div>
  </section>

  const renderAcknowledgmentButton = () => <button className="button subtle" disabled={busy || order?.acknowledged} onClick={() => void orderAction('acknowledge')}><Check size={17} />{order?.acknowledged ? 'Your review is recorded' : 'Confirm this order was right'}</button>

  const renderCommittedReview = () => !committed || notified ? null : <div className="committed-review"><div><strong>Optional buyer review</strong><p>A clean review can support a future proposal. It never increases freedom on its own.</p></div>{renderAcknowledgmentButton()}</div>

  const renderNotice = () => !notified ? null : <div className="notice-box"><div><Clock3 size={21} /><div><strong>Approval by silence</strong><p>{typeof order?.notice === 'string' ? order.notice : order?.notice?.message || 'The order is committed. Undo before the order cutoff to withdraw it and make your agent ask next time.'}</p>{cutoff && <p className="notice-cutoff">Undo until {cutoff.toLocaleString('en-GB', { timeZone: 'Europe/Helsinki', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })} Helsinki time.</p>}</div></div><div className="notice-actions"><button className="button dark" disabled={busy || undoExpired} onClick={() => void orderAction('undo')}><RotateCcw size={17} />{undoExpired ? 'Undo cutoff has passed' : 'Undo this order'}</button>{renderAcknowledgmentButton()}</div>{refusalAlert('notice')}</div>

  const renderBasketPreview = () => !basketNeed ? null : <section className="panel basket-preview" aria-label="Restated usual basket"><div className="panel-heading"><div><h3>Your usual basket, restated</h3><p>Delivery {prettyDate(basketNeed.deadline)}. The door rounds each line to whole sales packs.</p></div><Pill tone="neutral">Synthetic history</Pill></div><ul>{basketNeed.lines.map((line: Json) => <li key={line.product_id}><div><strong>{line.name || line.product_type}</strong><p>{line.properties?.join(', ') || 'No added must-hold property'}</p></div><span>{amount(line.quantity)} {line.unit === 'PACK' ? 'sales packs' : line.unit}</span></li>)}</ul><p className="basket-anchor">Anchored to the last pattern confirmed by the demo buyer. Other lines stay unchanged unless you ask.</p></section>

  const lastOutcome = messages.map(message => message.kind).lastIndexOf('outcome')
  const lastRestatement = messages.map(message => message.kind).lastIndexOf('restatement')
  const messageLabel = (message: Message) => message.role === 'user' ? 'Your message'
    : message.kind === 'restatement' ? 'Your agent restated your need'
    : message.kind === 'clarification' || message.action === 'clarification' ? 'Your agent needs one more detail'
    : message.kind === 'question' ? 'Your agent asks you'
    : message.kind === 'outcome' ? 'The door’s decision'
    : message.action === 'draft' ? 'Your agent drafted an order'
    : 'Your agent'

  const renderProducts = (message: Message) => <section className="product-answer" aria-label="Products">
    {message.deliveryDate && <p className="product-answer-date">Prices and stock for delivery {prettyDate(message.deliveryDate)}, excluding VAT</p>}
    <ul>{message.products!.map((product, index) => <li key={product.product_id || index}>
      <div className="product-answer-name"><strong>{product.name}</strong><span>{product.pack}</span>{!!product.properties?.length && <span className="product-answer-labels">{propertyLine(product.properties)}</span>}</div>
      <div className="product-answer-price"><strong>{product.price_eur == null ? 'Price not available' : `${money(product.price_eur)} per pack`}</strong>{product.unit_price && <span>{product.unit_price}</span>}<span className={`availability ${availabilityTone(product.availability)}`}>{availabilityText(product)}</span></div>
    </li>)}</ul>
  </section>

  const renderDraft = (message: Message, index: number) => {
    const draft = message.draft!
    return <section className="draft-card" aria-label="Draft order">
      <p className="draft-note">{message.draftState === 'placed' ? 'You placed this draft.' : message.draftState === 'superseded' ? 'Not placed. A later message replaced this draft.' : 'A draft. Nothing is reserved and no order exists yet.'}</p>
      {draft.restatement && <p className="draft-restatement">{draft.restatement}</p>}
      <dl>
        <div><dt>Product</dt><dd>{draft.product_name}{draft.pack ? `, ${draft.pack}` : ''}</dd></div>
        <div><dt>Packs</dt><dd>{amount(draft.packs)}</dd></div>
        <div><dt>Total, excluding VAT</dt><dd>{money(draft.total_eur)}</dd></div>
        <div><dt>Delivery</dt><dd>{prettyDate(draft.delivery_date)}</dd></div>
      </dl>
      {message.draftState === 'open' && <div className="inline-actions"><button className="button primary" disabled={busy} onClick={() => void placeDraft(index)}><Check size={16} />Place this order</button><button className="button subtle" disabled={busy} onClick={() => requestBox.current?.focus()}>Change something</button></div>}
      {refusalAlert(`draft-${index}`)}
    </section>
  }

  const renderOrderBlocks = () => <>{order && <div className={`outcome-card ${held || notified ? 'attention' : ''}`}><span className="outcome-icon">{held ? <Clock3 size={24} /> : <CheckCheck size={24} />}</span><div><h3>{held ? 'Your sign-off is needed' : notified ? 'Committed with a notice' : committed ? 'Order committed' : readable(order.status)}</h3><p>{held ? 'The order is outside a signed limit. Its stock is reserved while you decide.' : notified ? 'Inside the signed ceiling but beyond current freedom. You can undo it until the cutoff.' : committed ? `All ${checks.filter(check => check.passed).length} checks passed. Your signed receipt is in Record.` : 'The decision is saved in Record.'}</p></div><button className="button subtle" onClick={() => held ? void openSignoff() : setView('Record')}>{held ? 'Open sign-off' : 'Open record'}</button></div>}{renderNotice()}{renderCommittedReview()}{renderInterview()}</>

  const renderChat = () => <div className="chat-view">
    {!messages.length ? <>
      <div className="welcome"><h1>What does your kitchen need?</h1><p>Ask what Aimo sells, check a price or stock, or give an order: your agent orders only when your instruction is complete, and the door holds anything outside the limits you signed.</p></div>
      <div className="suggestions"><button className="suggestion" onClick={() => void runRequest(undefined, customerId === 'restaurant' ? restaurantPrompt : hotelPrompt)} disabled={busy}><span className="suggestion-icon"><Package size={20} /></span><span><small>{customerId === 'restaurant' ? 'Try your usual order' : 'Try a first order'}</small><strong>{customerId === 'restaurant' ? restaurantPrompt : hotelPrompt}</strong><p>{customerId === 'restaurant' ? 'Your agent reorders the basket you last confirmed, with one change.' : 'Your agent finds a match and orders it inside your signed limits.'}</p></span></button><button className="suggestion" onClick={() => void runRequest(undefined, catalogueQuestion)} disabled={busy}><span className="suggestion-icon"><MessageSquare size={20} /></span><span><small>Ask about the catalogue</small><strong>{catalogueQuestion}</strong><p>Your agent lists matching products with pack, price and stock. Nothing is ordered.</p></span></button><button className="suggestion" onClick={() => void runRequest(undefined, limitsQuestion)} disabled={busy}><span className="suggestion-icon"><SlidersHorizontal size={20} /></span><span><small>Ask about your limits</small><strong>{limitsQuestion}</strong><p>Your agent reads them from the door. Nothing is ordered.</p></span></button></div>
      
    </> : <div className="conversation" aria-live="polite">{messages.map((message, index) => <div key={index}><div className={`message ${message.role}`}><div className="message-avatar">{message.role === 'user' ? 'You' : <DoorOpen size={18} />}</div><div><div className="message-label">{messageLabel(message)}</div><p>{message.content}</p>{message.kind === 'clarification' && <p className="clarification-note">No order has been placed. Send a complete revised request to continue.</p>}{message.products?.length ? renderProducts(message) : null}{message.draft ? renderDraft(message, index) : null}{message.kind === 'outcome' && order && index === lastOutcome && <div className="message-actions"><button className="text-link" onClick={() => setView("Door's answer")}>See the door’s answer</button>{held && <a className="button primary small" href={order.continue_url || `/?view=signoff&order=${encodeURIComponent(order.id)}`} onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); void openSignoff() } }}>Review sign-off</a>}</div>}</div></div>{index === lastRestatement && renderBasketPreview()}{index === lastOutcome && renderOrderBlocks()}</div>)}{busy && <div className="working" role="status"><LoaderCircle size={18} className="spin" />{busyLabel || 'Working with the door'}</div>}<div ref={messageEnd} /></div>}
    {lastRestatement < 0 && renderBasketPreview()}
    {lastOutcome < 0 && renderOrderBlocks()}
    <form className="composer" onSubmit={event => void runRequest(event)}><label className="sr-only" htmlFor="request">Your purchasing request</label><textarea id="request" ref={requestBox} value={input} onChange={event => setInput(event.target.value)} placeholder={customerId === 'restaurant' ? restaurantPrompt : 'For example: 200 litres of oat drink by Friday'} rows={2} disabled={busy} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void runRequest() } }} /><div className="composer-bottom"><span><ShieldCheck size={15} />Checked against your signed limits</span><button type="submit" className="button primary" disabled={busy || !input.trim()}>{busy ? <LoaderCircle size={17} className="spin" /> : <Send size={17} />}Send request</button></div></form>
    <label className="simulation-toggle"><input type="checkbox" checked={limitedStock} onChange={event => setLimitedStock(event.target.checked)} disabled={busy} /><span>Simulate limited stock</span><Pill tone="amber">Demo simulation</Pill><span className="simulation-note">The door runs short of stock, so the order waits for your sign-off.</span></label>
  </div>

  const renderAnswer = () => !result ? <Empty title="No answer yet">Send a request in Chat. The door’s filters, ranked options, whole packs and signed offer appear here.</Empty> : <div className="answer-view">
    <div className="section-heading"><div><h2>The door’s answer</h2><p>{result.restatement}</p></div><Pill tone="green"><ShieldCheck size={14} />Checked against your mandate</Pill></div>
    <section className="filter-section"><h3>Filters applied</h3><div className="filter-tags">{(result.match?.filters_applied || result.need?.properties || []).map((filter: unknown, index: number) => <span key={index}><Check size={14} />{readable(filter)}</span>)}</div><p className="muted small-text">Must-hold claims come from the manufacturer. Product type is inferred and shown separately.</p></section>
    <div className="option-tabs">{options.map((option, index) => <button key={index} onClick={() => setSelectedOption(index)} className={selectedOption === index ? 'selected' : ''}><span className="option-label">{option.label || ['best', 'better', 'good'][index]}</span><strong>{money(option.total)}</strong><span>{option.on_time ? 'On time' : 'Later than asked'}, {basketNeed ? `${option.lines?.length || 0} basket lines` : `${amount(option.overage)} ${result.need?.unit || 'L'} over`}</span>{index === 0 && <span className="recommended">Recommended</span>}</button>)}</div>
    {options[selectedOption] && <section className="panel option-panel"><div className="panel-heading"><div><h3>{options[selectedOption].description || `${readable(options[selectedOption].label)} match`}</h3><p>Delivery {prettyDate(options[selectedOption].delivery_date)}</p></div><Pill tone={options[selectedOption].on_time ? 'green' : 'amber'}>{options[selectedOption].on_time ? 'Meets your deadline' : 'Review delivery date'}</Pill></div>{options[selectedOption].simulation && <div className="option-simulation"><Pill tone="amber">Simulated limited stock</Pill><p>{options[selectedOption].simulation}</p></div>}{options[selectedOption].lines?.map((line: Json, index: number) => <ProductLine line={line} key={line.product_id || line.id || index} />)}<div className="option-totals"><span>Whole packs, rounded up <strong>{basketNeed ? `${options[selectedOption].lines?.length || 0} basket lines, checked individually` : `${amount(options[selectedOption].overage)} ${result.need?.unit || 'L'} over your request`}</strong></span><span>Total, excluding VAT<strong>{money(options[selectedOption].total)}</strong></span></div></section>}
    {offerPayload && <section className="offer-summary"><div><span className="mini-icon"><FileCheck2 size={22} /></span><div><h3>Signed offer</h3><p>{money(offerPayload.total)}, delivery {prettyDate(offerPayload.delivery_date)}, {offerPayload.exp ? `valid until ${new Date(offerPayload.exp * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}` : `valid until ${readable(offerPayload.expires_at)}`}</p><small>An offer reserves no stock. Stock is reserved when the order is placed.</small></div></div><Pill tone="green">Signed by the door</Pill></section>}
    <SignedDocument label="View signed offer" token={order?.offer || (typeof result.offer === 'string' ? result.offer : result.offer?.offer)} />
    {!!checks.length && <section className="panel"><div className="panel-heading"><div><h3>The ten checks</h3><p>The door runs all ten on every order, even after one fails.</p></div><Pill tone={held ? 'amber' : 'green'}>{checks.filter(check => check.passed).length} / {checks.length} pass</Pill></div><CheckList checks={checks} /></section>}
  </div>

  const renderSignoff = () => !order ? <Empty icon={FileCheck2} title="Nothing needs your sign-off">When an order falls outside your signed limits, it waits here with its reasons and your choices, and its stock stays reserved.</Empty> : <div>
    <div className="section-heading"><div><h2>{held && !approvable ? cannotDeliverTitle : held ? 'This order needs your sign-off.' : notified ? 'Committed with a notice you can undo.' : committed ? 'Your order is committed.' : 'This order is closed.'}</h2><p>{held && !approvable ? cannotDeliverNote : held ? 'The whole order is on hold and its stock is reserved. Read why, then approve, edit or reject it.' : notified ? 'Your agent went beyond current freedom but stayed inside the signed ceiling. Undo it before the cutoff if it is wrong.' : 'Your signed receipt and the full decision are in Record.'}</p></div><Pill tone={held || notified ? 'amber' : 'green'}>{readable(order.tier || order.status)}</Pill></div>
    {order.requires_price_confirmation && <div className="alert warning"><TriangleAlert size={21} /><div><strong>The previous offer expired</strong><p>The door has refreshed the offer. The new total is {money(order.offer_payload?.total)}. Read the new price before approving.</p><label className="checkbox-label"><input type="checkbox" checked={priceConfirmed} onChange={event => setPriceConfirmed(event.target.checked)} />I have reviewed the refreshed offer and its price.</label></div></div>}
    {held && !approvable && renderCannotDeliver()}
    {held && !!failedChecks.length && <section className="panel needs-you"><div className="panel-heading"><h3>Why this needs you</h3><Pill tone="amber">Stock reserved, not committed</Pill></div><CheckList checks={failedChecks} /></section>}
    {committed && !!failedChecks.length && <section className="panel decided" aria-label="What you approved"><div className="panel-heading"><h3>What you approved</h3><Pill tone="green">Approved by you</Pill></div><ul className="considered-options">{failedChecks.map((check, index) => <li key={check.id || index}><strong>{check.name || `Check ${index + 1}`}</strong><p>{check.reason || check.message}</p></li>)}</ul></section>}
    {!held && !committed && !!failedChecks.length && <section className="panel"><div className="panel-heading"><h3>Why it was held</h3></div><CheckList checks={failedChecks} /></section>}
    {held && consideredOptions.length > 0 && <section className="panel"><div className="panel-heading"><div><h3>Options the door considered</h3><p>Ranked by deadline, preferences, overage and price. The order below is the first one.</p></div></div><ul className="considered-options">{consideredOptions.map((option, index) => <li key={option.option_id || index}><Pill tone={option.on_time ? 'green' : 'amber'}>{option.label || ['best', 'better', 'good'][index]}</Pill><div><strong>{option.description || readable(option.kind || 'Product match')}</strong><p>{money(option.total)}, delivery {prettyDate(option.delivery_date)}, {option.on_time ? 'on time' : 'later than asked'}{option.simulation ? ' (simulated stock and delivery)' : ''}</p></div></li>)}</ul></section>}
    {offerPayload && <section className="panel"><div className="panel-heading"><div><h3>Review the order</h3><p>Delivery {prettyDate(offerPayload.delivery_date)}. Total excludes VAT.</p></div><strong className="large-price">{money(offerPayload.total)}</strong></div>{offerPayload.lines?.map((line: Json, index: number) => <ProductLine line={line} key={line.id || index} />)}</section>}
    {renderQuestion()}
    <SignedDocument label="Current signed offer" token={order.offer} />
    {held && <div className="signoff-actions"><div className="edit-action"><label htmlFor="quantity-edit">{offerPayload?.need?.lines?.length ? 'Basket count (1 = original requested basket)' : 'Edit requested quantity'}</label><div><input id="quantity-edit" type="number" min="0.001" step="any" value={editQuantity} onChange={event => setEditQuantity(event.target.value)} placeholder={String(offerPayload?.need?.quantity || result?.need?.quantity || '')} /><button className="button subtle" disabled={busy || !(Number(editQuantity) > 0)} onClick={() => void orderAction('edit')}>Edit & recheck</button></div></div><div className="decision-actions"><button className="button danger" disabled={busy} onClick={() => void orderAction('reject')}><X size={17} />Reject order</button>{approvable && (!order.question_spec || order.question_spec.kind === 'property_confirmation') && <button className="button primary" disabled={busy || (order.requires_price_confirmation && !priceConfirmed) || (missingProperties.length > 0 && !propertiesConfirmed)} onClick={() => void orderAction('approve')}><Check size={17} />Approve order</button>}</div>{refusalAlert('signoff')}</div>}
    {renderNotice()}
    {renderCommittedReview()}
    <SignedDocument label="Your signed receipt" token={order.receipt} />
    {order.record_id && <button className="text-link section-link" onClick={() => setView('Record')}>Read the decision record</button>}
  </div>

  const renderProposal = (proposal: Json) => <div className="proposal-card" key={proposal.id}><span className="proposal-icon"><Sparkles size={23} /></span><div><p className="proposal-kicker">A proposal from the door. Nothing changes until you accept it.</p><h3>{proposalTitle(proposal)}</h3><p>{proposal.description || proposal.reason}</p>{proposal.type === 'anchor' ? <><p>Review every line below. Accepting changes the usual-order anchor; it does not raise current freedom or the signed ceiling.</p><div className="proposal-anchor">{(proposal.proposed_lines || []).map((line: Json, index: number) => <ProductLine line={line} key={line.id || index} />)}</div></> : <p className="proposal-numbers">Current freedom goes from {percent(proposal.current_freedom ?? departureLimit?.current_freedom)} to {percent(proposal.proposed_freedom ?? proposal.new_freedom)}. The signed ceiling stays {percent(departureLimit?.signed_ceiling)}.</p>}<details className="proposal-evidence"><summary>Review {proposal.evidence?.length || 0} supporting approvals <ChevronDown size={14} /></summary><ol>{(proposal.evidence || []).map((evidence: Json, index: number) => <li key={evidence.order_id || index}><div><strong>{evidence.date ? prettyDate(evidence.date) : `Approval ${index + 1}`}</strong><p>{evidence.description || evidence.outcome || 'A human approval without correction.'}</p><small>Order {evidence.order_id}</small></div>{evidence.synthetic && <Pill tone="neutral">Synthetic</Pill>}</li>)}</ol></details><button className="button primary small" disabled={busy} onClick={() => void limitAction(`/limits/proposals/${encodeURIComponent(proposal.id)}/accept`, proposalTitle(proposal), `proposal-${proposal.id}`)}>Accept proposal <Check size={15} /></button>{refusalAlert(`proposal-${proposal.id}`)}</div></div>

  const renderLimitChange = (row: Json) => {
    const change = limitChange!
    const current = Number(row.signed_ceiling)
    const next = Number(change.value)
    const valid = change.value.trim() !== '' && Number.isFinite(next) && next > 0 && next !== current
    const close = () => setLimitChange(null)
    const steps = ['New value', 'Identify yourself', 'Review and sign']
    return <section className={`limit-change ${change.step === 3 ? 'review' : ''}`} aria-label={`Change ${row.name}`} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); close() } }}>
      <ol className="limit-change-steps">{steps.map((name, index) => <li key={name} aria-current={change.step === index + 1 ? 'step' : undefined}>{name}</li>)}</ol>
      {change.step === 1 && <form onSubmit={event => { event.preventDefault(); if (valid) void identifyForLimit() }}>
        <h3>New value</h3>
        <p>Signed ceiling now: <strong>{money(current)}</strong></p>
        <label htmlFor="limit-change-value">New {row.name.toLowerCase()}, in euros</label>
        <input id="limit-change-value" type="number" inputMode="decimal" min="0.01" step="0.01" value={change.value} autoFocus onChange={event => setLimitChange({ ...change, value: event.target.value, error: undefined })} />
        <p className="muted">Raising a ceiling lets your agent spend more without asking; lowering it takes effect at once.</p>
        {change.error && <p className="limit-change-error" role="alert">{change.error}</p>}
        <div className="inline-actions"><button type="submit" className="button primary" disabled={!valid}>Continue</button><button type="button" className="button subtle" onClick={close}>Cancel</button></div>
      </form>}
      {change.step === 2 && <div>
        <h3>Identify yourself</h3>
        <p className="muted">Waiting for the bank identification window.</p>
      </div>}
      {change.step === 3 && change.identity && <div>
        <h3>Review and sign</h3>
        <p className="limit-change-diff">{row.name}: {money(current)} → {money(next)}</p>
        <p>Signed by {change.identity.person?.name}, identified by {change.identity.method}.</p>
        <p className="muted">You are signing a new version of your mandate. The door cannot change this limit; only you can.</p>
        {change.error && <p className="limit-change-error" role="alert">{change.error}</p>}
        <div className="inline-actions"><button className="button primary" autoFocus disabled={change.working} onClick={() => void signLimitChange()}>{change.working && <LoaderCircle size={16} className="spin" />}Sign the new limit</button><button className="button subtle" onClick={close}>Cancel</button></div>
      </div>}
    </section>
  }

  const bankKeys = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!bankWindow?.working) finishBank({ status: 'cancelled' }); return }
    if (event.key !== 'Tab' || !bankRef.current) return
    const focusable = [...bankRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input, [tabindex]:not([tabindex="-1"])')]
    if (!focusable.length) return
    const first = focusable[0], last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  const renderBankWindow = () => !bankWindow ? null : <div className="bank-backdrop">
    <div className="bank-window" role="dialog" aria-modal="true" aria-labelledby="bank-window-title" aria-describedby="bank-window-summary" ref={bankRef} onKeyDown={bankKeys}>
      <div className="bank-titlebar"><span className="bank-titlebar-icon"><LockKeyhole size={15} /></span><span id="bank-window-title">Bank identification</span><button aria-label="Close" disabled={bankWindow.working} onClick={() => finishBank({ status: 'cancelled' })}><X size={17} /></button></div>
      <div className="bank-body">
        <p className="bank-simulation"><Pill tone="amber">Simulation: no real bank or identity service is contacted.</Pill></p>
        <p className="bank-summary" id="bank-window-summary">{bankWindow.summary}</p>
        <p className="muted">This stands for strong identification and your right to sign for the company.</p>
        {bankWindow.error && <p className="limit-change-error" role="alert">{bankWindow.error}</p>}
        {bankWindow.unsupported && <p className="limit-change-error" role="alert">{cannotIdentify}</p>}
        {bankWindow.identity && <div className="limit-change-identity" role="status"><p><strong>Identified: {bankWindow.identity.person?.name}</strong></p><p>{bankWindow.identity.authority}</p></div>}
        <div className="inline-actions">
          {bankWindow.identity ? <button className="button primary" autoFocus onClick={() => finishBank({ status: 'identified', identity: bankWindow.identity! })}>Continue</button>
            : bankWindow.unsupported ? <>{bankWindow.kind === 'session' && <button className="button primary" autoFocus onClick={() => finishBank({ status: 'unsupported' })}>Continue without identification</button>}<button className="button subtle" autoFocus={bankWindow.kind !== 'session'} onClick={() => finishBank({ status: 'cancelled' })}>Cancel</button></>
            : <><button className="button primary" autoFocus disabled={bankWindow.working} onClick={() => void identifyInBank()}>{bankWindow.working && <LoaderCircle size={16} className="spin" />}Identify with bank credentials (simulated)</button><button className="button subtle" disabled={bankWindow.working} onClick={() => finishBank({ status: 'cancelled' })}>Cancel</button></>}
        </div>
      </div>
    </div>
  </div>

  const renderLimits = () => <div>
    <div className="section-heading"><div><h2>What your agent may do</h2><p>You sign a ceiling for each limit. Like a new hire, your agent starts with less freedom inside it, gains more only when you accept a proposal, and loses it when you correct an order.</p>{mandateSentence && <p className="mandate-words">{mandateSentence}</p>}</div><Pill tone="neutral"><LockKeyhole size={14} />Only you can change these</Pill></div>
    <div className="limits-explainer" aria-label="How to read the bars"><div><span className="zone-swatch commit" /><strong>Commit</strong><p>Inside current freedom. Your agent acts alone and the door logs it.</p></div><div><span className="zone-swatch notify" /><strong>Notify</strong><p>Inside the signed ceiling. Your agent acts and you get a notice with an undo.</p></div><div><span className="zone-swatch hold" /><strong>Hold</strong><p>Beyond the signed ceiling. The order waits for your sign-off.</p></div></div>
    <section className="panel limits-panel"><div className="limit-table-head"><span>Limit</span><span>Signed ceiling</span><span>Freedom or usage</span><span>Status</span></div>{limits?.rows?.length ? limits.rows.map((row: Json) => <Fragment key={row.id}><details className="limit-row"><summary><strong>{row.name}</strong><span data-label="Signed ceiling">{limitValue(row, row.signed_ceiling)}</span>{row.id === 'monthly_budget' ? <BudgetPosition row={row} /> : <span data-label="Current freedom">{limitValue(row, row.current_freedom)}</span>}<span className="limit-status"><Pill tone={row.status === 'tightened' ? 'amber' : 'green'}>{readable(row.status)}</Pill><ChevronDown size={15} /></span><FreedomBar row={row} /></summary><div className="limit-reason"><div><strong>Why this limit is here</strong><p>{row.reason || 'This limit comes from the buyer’s signed mandate.'}</p>{row.evidence && <p>{readable(row.evidence)}</p>}</div>{(row.id === 'departure' || row.resettable) && <button className="button subtle small" disabled={busy} onClick={() => void limitAction(`/limits/${encodeURIComponent(row.id)}/reset`)}><RotateCcw size={14} />Reset freedom</button>}</div></details>{changeableLimits.includes(row.id) && <div className="limit-change-bar">{changeRecords[row.id] && <button className="text-link" onClick={() => void openRecord(changeRecords[row.id])}>See this change on the record</button>}<button className="button subtle small" disabled={busy || limitChange?.rowId === row.id} onClick={() => setLimitChange({ rowId: row.id, step: 1, value: '' })}>Change this limit</button></div>}{limitChange?.rowId === row.id && renderLimitChange(row)}</Fragment>) : <div className="table-empty">The buyer’s limits will appear when the door is connected.</div>}</section>
    {!!limits?.proposals?.length && <section className="proposals"><div className="section-heading compact"><h3>Ready for your decision</h3><Pill tone="neutral">{limits.proposals.length} proposal{limits.proposals.length > 1 ? 's' : ''}</Pill></div>{limits.proposals.map(renderProposal)}</section>}
    <section className="panel"><div className="panel-heading"><div><h3>Answers your agent remembers</h3><p>Each answer keeps the exact scope you confirmed.</p></div><MessageSquare size={20} /></div>{limits?.learned_preferences?.length ? limits.learned_preferences.map((preference: Json) => <div className="learned-row" key={preference.id}><div><strong>{preference.question || preference.name}</strong><p>{preference.answer} ({readable(preference.scope || preference.product_type)})</p>{preference.confirmation_summary && <p>{preference.confirmation_summary}</p>}</div><button className="button subtle small" disabled={busy} onClick={() => void limitAction(`/limits/${encodeURIComponent(preference.id)}/reset`)}>Remove</button></div>) : <p className="panel-empty">No standing answers yet. Your agent will ask when a trade-off needs your decision.</p>}</section>
    {customerId === 'restaurant' && <section className="panel"><div className="panel-heading"><div><h3>Your usual order</h3><p>The basket you last confirmed. It changes only when you accept a proposal, never by averaging.</p></div><Pill tone="neutral">Synthetic order history</Pill></div><div className="history-summary"><History size={23} /><div><strong>{usual?.history?.length || usual?.history_count || 0} weekly orders</strong><p>{usual?.label || usual?.history_label || 'Synthetic history for this restaurant demo.'}</p></div></div>{(usual?.lines || usual?.basket || usual?.usual_order?.lines || []).map((line: Json, index: number) => <ProductLine line={line} key={line.id || index} />)}{usual && usual.history?.length > 0 && <details className="history-list"><summary>Inspect {usual.history.length} synthetic weekly orders <ChevronDown size={15} /></summary><p>Historical orders and prices are simulated from the seed’s quote. The product identities come from the catalog.</p><ol>{usual.history.map((entry: Json, index: number) => <li key={entry.id || index}><div><strong>{prettyDate(entry.date || entry.delivery_date)}</strong><p>{entry.lines?.map((line: Json) => `${line.name}: ${amount(line.packs)} packs`).join(' · ')}</p></div><span>{money(entry.total)}<small>Synthetic</small></span></li>)}</ol></details>}</section>}
    <SignedDocument label="Your signed mandate (simulated signing key)" token={currentMandate} />
  </div>

  const renderRecord = () => !record ? <Empty icon={History} title="No decision recorded yet">Send a request in Chat. When the door decides, the request, filters, ten checks, approver and your signed copy appear here.</Empty> : <div>
    <div className="section-heading"><div><h2>Decision record</h2><p>You and the door keep the same copy of this record.</p></div><button className="button subtle" onClick={downloadRecord}><ArrowDownToLine size={17} />Download your copy</button></div>
    <div className="record-banner"><div><span className="mini-icon"><Fingerprint size={25} /></span><div><strong className={record.outcome === 'limit_changed' ? 'sentence-case' : undefined}>{record.outcome === 'limit_changed' ? 'Limit changed' : readable(record.outcome)}</strong><p>Approved by {readable(record.approved_by || 'the agent within the buyer’s mandate')}</p></div></div><span>{record.created_at ? new Date(record.created_at).toLocaleString('en-GB') : ''}</span></div>
    <section className="panel record-narrative"><div className="panel-heading"><h3>What happened</h3><Pill tone="green">Buyer’s copy</Pill></div>{record.sentences?.length ? <ol>{record.sentences.map((sentence: string, index: number) => <li key={index}>{sentence}</li>)}</ol> : <div className="record-fallback"><p><strong>Your request.</strong> {readable(record.request)}</p><p><strong>The filters.</strong> {(record.filters_applied || []).map(readable).join(', ')}.</p><p><strong>Options considered.</strong> {readable(record.options_considered)}</p><p><strong>The outcome.</strong> {readable(record.outcome)}. Approved by {readable(record.approved_by)}.</p></div>}</section>
    {!!record.checks?.length && <section className="panel"><div className="panel-heading"><h3>The ten checks, as decided</h3><Pill tone="neutral">{record.checks?.length || 0} checks</Pill></div><CheckList checks={record.checks || []} /></section>}
    <SignedDocument label="Your signed receipt" token={record.id === order?.record_id ? order?.receipt : undefined} />
    <ModelProbabilities record={record} />
    <details className="technical-detail"><summary><span><Fingerprint size={17} />Record hash and chain</span><ChevronDown size={16} /></summary><div><p>The receipt carries this record’s hash. This record carries the hash of the previous record.</p><label>Record hash<code>{record.hash}</code></label><label>Previous record hash<code>{record.previous_hash || 'First record in this chain'}</code></label><a href={`/records/${record.id}`} target="_blank" rel="noreferrer" className="text-link">Open the machine-readable record <ArrowUpRight size={14} /></a></div></details>
  </div>

  return <div className="app-shell">
    <aside className="sidebar"><a href="/" className="wordmark" aria-label="Aimo's Door home"><span className="brand-mark"><DoorOpen size={22} strokeWidth={1.75} /></span><span className="wordmark-text">Aimo's Door</span></a><div className="workspace-card"><span className="workspace-avatar">{customerId === 'hotel' ? 'H' : 'R'}</span><div><label htmlFor="customer-select">Buying for</label><select id="customer-select" value={customerId} onChange={event => changeCustomer(event.target.value)} disabled={busy}>{customers.length ? customers.map(customer => <option key={customer.id} value={customer.id}>{customer.name}</option>) : <><option value="hotel">Hotel chain</option><option value="restaurant">Restaurant</option></>}</select></div></div><p className="workspace-caption">{customerId === 'hotel' ? 'No order history. A fresh start.' : usual?.history?.length ? `${usual.history.length} weeks of synthetic history.` : 'Synthetic history demo customer.'}</p><nav className="sidebar-nav" aria-label="Console views">{views.map((name, index) => { const Icon = [MessageSquare, Package, FileCheck2, SlidersHorizontal, History][index]; return <button key={name} type="button" aria-current={view === name ? 'page' : undefined} onClick={() => name === 'Sign-off' ? void openSignoff() : setView(name)} className={view === name ? 'active' : ''}><Icon size={19} /><span>{name}</span>{name === 'Sign-off' && held && <span className="nav-count" aria-label="1 waiting">1</span>}</button> })}</nav><div className="sidebar-bottom"><a className="api-link" href="/docs" target="_blank" rel="noreferrer">Explore the public API <ArrowUpRight size={15} /></a><div className="sidebar-footer">Junction 2026 demo</div></div></aside>
    <main className="main"><header className="topbar"><div><span className="breadcrumb">Purchasing console</span><ChevronRight size={15} /><strong>{view}</strong></div><div className="topbar-actions"><span className="connection"><span className={`connection-dot ${health ? '' : 'offline'}`} />{health ? 'Door connected' : 'Connecting to door'}</span><button className="button ghost small" onClick={() => void resetDemo()} disabled={busy}><RotateCcw size={15} />Reset demo</button></div></header>
      <div className="content-grid"><div className="main-content">{error && <div className="alert error" role="alert"><TriangleAlert size={21} /><div><strong>The door could not complete this step</strong><p>{error}</p></div><button aria-label="Dismiss error" onClick={() => setError('')}><X size={17} /></button></div>}{view === 'Chat' ? renderChat() : view === "Door's answer" ? renderAnswer() : view === 'Sign-off' ? renderSignoff() : view === 'Limits view' ? renderLimits() : renderRecord()}</div>
        <aside className="context-panel"><div className="context-header"><ShieldCheck size={20} /><h3>Your signed limits</h3><Pill tone="green">Signed</Pill></div><p className="context-intro">The door checks every order against these before it commits.</p>{identifiedName && <p className="identified-line"><ShieldCheck size={15} />Identified for this session: {identifiedName} (simulated)</p>}<div className="mandate-stat"><span>Per-order spend cap</span><strong>{money(limits?.rows?.find((row: Json) => /per.order|spend|order.cap/i.test(row.name || row.id))?.signed_ceiling ?? 2000)}</strong></div><div className="mandate-stat"><span>Monthly budget</span><strong>{money(limits?.rows?.find((row: Json) => /monthly/i.test(row.name || row.id))?.signed_ceiling ?? 20000)}</strong></div>{scopeLabel && <div className="mandate-stat words"><span>Product scope</span><strong>{scopeLabel}</strong></div>}{mustHoldLabel && <div className="mandate-stat words"><span>Must hold</span><strong>{mustHoldLabel}</strong></div>}<div className="must-hold"><span>Never traded for price or speed</span><p><Check size={15} />Manufacturer-stated properties</p><p><Check size={15} />Your approved product scope</p><p><Check size={15} />Your signed spend limits</p></div><button className="text-link" onClick={() => setView('Limits view')}>See all limits</button><div className="context-divider" /><div className="reality-label"><span className="tiny-dot" /><strong>Real data</strong><p>Product catalog, prices and stock from Aimo’s mock API.</p></div><div className="reality-label simulated"><span className="tiny-dot" /><strong>Simulated operations</strong><p>Order book, stock reservations, order history and the buyer’s signing key.{buyerSessionActive && ' The buyer login is simulated too.'}</p></div>{(health?.fixture || health?.mode === 'fixture') && <div className="fixture-warning"><TriangleAlert size={16} />Fixture mode: product responses also use test data.</div>}<div className="context-footnote"><LockKeyhole size={14} />No real purchase is placed.</div></aside>
      </div><footer className="page-footer"><span>Any purchasing agent can use the door through its public API.</span><a href="/.well-known/jwks.json" target="_blank" rel="noreferrer"><Fingerprint size={14} />Public verification keys</a></footer>
    </main>
    {renderBankWindow()}
    {(idleNotice || toast) && <div className="toast" role="status"><Check size={18} />{idleNotice ? idleResetNotice : toast}<button onClick={() => idleNotice ? setIdleNotice(false) : setToast('')} aria-label="Dismiss notice"><X size={15} /></button></div>}
    {gate && <div className="gate-backdrop"><form className="gate-card" onSubmit={event => { event.preventDefault(); setAccessCode(gateInput); setGate(false) }}><span className="gate-icon"><LockKeyhole size={29} /></span><h2>Enter the demo access code</h2><p>This demo is private. Your presenter gives you the code.</p><label htmlFor="access-code">Demo access code</label><input id="access-code" type="password" autoComplete="off" value={gateInput} onChange={event => setGateInput(event.target.value)} autoFocus required /><button className="button primary" type="submit">Open console</button>{error && <p className="gate-error">{error}</p>}</form></div>}
  </div>
}
