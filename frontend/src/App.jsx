import { useCallback, useEffect, useState } from 'react'
import { api, MOCK_AREAS, USE_MOCK } from './api'
import './App.css'
import ChatPanel from './components/ChatPanel'
import DataPanel from './components/DataPanel'
import TaskWorkspace from './components/TaskWorkspace'
import TaskHistoryPanel from './components/TaskHistoryPanel'
import S3SyncPanel from './components/S3SyncPanel'
import WorkflowPanel from './components/WorkflowPanel'
import { Card, EmptyState } from './components/ui'
import { useLiquidGlassInteractions } from './hooks/useLiquidGlassInteractions'

const TABS = [
  { id: 'workflows', label: 'Workflows' },
  { id: 'tasks', label: 'Task history' },
  { id: 'data', label: 'Connected data' },
  { id: 'storage', label: 'S3 backup' },
]

const NAV_ITEMS = [
  { id: 'chat', label: 'Chat', icon: '▤' },
  { id: 'tasks', label: 'Tasks', icon: '▱' },
  { id: 'browser', label: 'Browser', icon: '▣' },
  { id: 'evidence', label: 'Evidence', icon: '⛨' },
  { id: 'history', label: 'History', icon: '◷' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
]

const QUICK_ACTIONS = [
  { label: 'Start Web Research', icon: '◎', prompt: 'Research the latest developments in AI agents using the configured public browser. Read source pages and return findings with source URLs and evidence.' },
  { label: 'Analyze a Webpage', icon: '▱', prompt: 'Analyze the webpage at https://example.com. Extract its title and main visible content, and include the source evidence.' },
  { label: 'Capture & Extract', icon: '▣', prompt: 'Open https://example.com, extract its page title and main visible text, and capture a screenshot.' },
  { label: 'Summarize Sources', icon: '▤', prompt: 'Summarize the sources and verified browser evidence from my latest completed research task.' },
]

export default function App() {
  useLiquidGlassInteractions()
  const [messages, setMessages] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [conversationLoading, setConversationLoading] = useState(true)
  const [conversationError, setConversationError] = useState(null)
  const [activeTaskId, setActiveTaskId] = useState(null)
  const [activeTask, setActiveTask] = useState(null) // latest polled copy, used for voice narration
  const [unavailableTaskCount, setUnavailableTaskCount] = useState(0)
  const [tab, setTab] = useState('workflows')
  const [activeView, setActiveView] = useState('chat')
  const [draftPrompt, setDraftPrompt] = useState(null)
  const [health, setHealth] = useState('checking')
  const [browserStatus, setBrowserStatus] = useState(null)
  const localDemoAvailable = browserStatus?.browser_local_demo_enabled === true
    || browserStatus?.browser_integration_status === 'local_demo_only'

  useEffect(() => {
    let alive = true
    api.health().then((result) => {
      if (alive) {
        setHealth('online')
        setBrowserStatus(result)
      }
    }, () => alive && setHealth('offline'))
    const restoreConversation = async () => {
      try {
        const savedId = window.localStorage.getItem('unbound.conversation_id')
        if (savedId) {
          try {
            const conversation = await api.getConversation(savedId)
            if (!alive) return
            setConversationId(conversation.session_id)
            setMessages(conversation.messages)
            const taskReferences = await api.getConversationTaskReferences(savedId)
            if (!alive) return
            const latestTaskId = taskReferences.available_task_ids?.at(-1)
            if (latestTaskId) setActiveTaskId(latestTaskId)
            setUnavailableTaskCount(taskReferences.unavailable_task_ids?.length ?? 0)
            setConversationLoading(false)
            return
          } catch (err) {
            if (err?.status !== 404) throw err
            window.localStorage.removeItem('unbound.conversation_id')
          }
        }
        const conversation = await api.createConversation()
        if (!alive) return
        window.localStorage.setItem('unbound.conversation_id', conversation.session_id)
        setConversationId(conversation.session_id)
        setMessages([])
        setConversationLoading(false)
      } catch (err) {
        if (!alive) return
        setConversationError(err)
        setConversationLoading(false)
      }
    }
    restoreConversation()
    return () => {
      alive = false
    }
  }, [])

  const handleSubmit = useCallback(async (text) => {
    const approvalCommand = text.trim().toLowerCase()
    if (approvalCommand === 'approve' || approvalCommand === 'reject') {
      const latestMessage = messages.at(-1)
      const linkedTaskId = activeTaskId && latestMessage?.role === 'assistant' && latestMessage.task_id === activeTaskId
        ? activeTaskId
        : null
      const appendApprovalReply = (reply, task = null) => {
        const messageKey = `${Date.now()}-${Math.random().toString(16).slice(2)}`
        setMessages((prev) => [...prev,
          { id: `${messageKey}-user`, role: 'user', content: text },
          { id: `${messageKey}-assistant`, role: 'assistant', content: reply, task_id: task?.task_id, task_status: task?.status },
        ])
      }

      // Typed approval is bound only to the task attached to the immediately
      // preceding assistant task response. It records consent through the
      // existing TaskManager endpoint and never executes the task.
      if (!linkedTaskId) {
        const reply = 'I could not match that response to a task just presented in this chat. Select the specific task and use its approval control; nothing was approved or executed.'
        appendApprovalReply(reply)
        return { reply, task: null }
      }

      const currentTask = await api.getTask(linkedTaskId)
      setActiveTask(currentTask)
      const pending = currentTask.pending_approvals ?? []
      if (currentTask.status !== 'awaiting_approval' || pending.length !== 1) {
        const reply = pending.length > 1
          ? 'This task has multiple pending approvals. Use the approval control for the exact action you want to resolve.'
          : 'This task has no pending approval yet. Use Run plan to request its approval; nothing was approved or executed.'
        appendApprovalReply(reply, currentTask)
        return { reply, task: currentTask }
      }

      const updatedTask = await api.decideApproval(
        linkedTaskId,
        pending[0].approval_id,
        approvalCommand === 'approve' ? 'approved' : 'rejected',
      )
      setActiveTask(updatedTask)
      setActiveTaskId(linkedTaskId)
      const reply = approvalCommand === 'approve'
        ? 'Approval was recorded for the exact pending action. Nothing was executed. Select Run plan separately to continue.'
        : 'The exact pending action was rejected. It was not executed.'
      appendApprovalReply(reply, updatedTask)
      return { reply, task: updatedTask }
    }

    // Conversation persistence is optional. A failed restore must not prevent
    // independent task planning; the backend accepts requests without a session.
    const sessionId = conversationId ?? null
    // Use one authoritative backend decision for every turn. The assistant
    // route classifies, answers or prepares a plan and persists the exchange;
    // the former classify-then-route flow called the model twice and could
    // disagree or produce unnecessary clarification loops. It never executes
    // a prepared plan: the user starts it with Run plan in the task workspace.
    const response = await api.assistantRequest(text, sessionId)
    const messageKey = `${Date.now()}-${Math.random().toString(16).slice(2)}`
    setMessages((prev) => [...prev,
      { id: `${messageKey}-user`, role: 'user', content: text },
      {
        id: `${messageKey}-assistant`,
        role: 'assistant',
        content: response.reply,
        task_id: response.task?.task_id,
        task_status: response.task?.status,
      },
    ])
    if (response.task) {
      setActiveTaskId(response.task.task_id)
      setActiveTask(response.task)
    }
    setConversationError(null)
    return response
  }, [activeTaskId, conversationId, messages])

  const startNewConversation = useCallback(async () => {
    setConversationLoading(true)
    setConversationError(null)
    try {
      const conversation = await api.createConversation()
      window.localStorage.setItem('unbound.conversation_id', conversation.session_id)
      setConversationId(conversation.session_id)
      setMessages([])
      setActiveTaskId(null)
      setActiveTask(null)
      setUnavailableTaskCount(0)
    } catch (err) {
      setConversationError(err)
    } finally {
      setConversationLoading(false)
    }
  }, [])

  const trackTask = useCallback((task) => {
    setActiveTaskId(task.task_id)
    setActiveTask(task)
    setActiveView('tasks')
  }, [])

  const handleTaskChange = useCallback((task) => {
    setActiveTask(task)
    setMessages((prev) => prev.map((message) => message.task_id === task.task_id
      ? { ...message, task_status: task.status }
      : message))
  }, [])

  const openTask = useCallback((taskId) => {
    setActiveTaskId(taskId)
    setActiveView('tasks')
  }, [])

  const startQuickAction = useCallback((prompt) => {
    setActiveView('chat')
    setDraftPrompt({ id: `${Date.now()}-${Math.random()}`, text: prompt })
  }, [])

  return (
    <div className="unbound-app liquid-dashboard">
      <div className="dashboard-shell">
        <aside className="left-rail" aria-label="Primary navigation">
          <a className="unbound-brand" href="#dashboard" aria-label="UNBOUND AI dashboard">
            <span className="unbound-brand-mark" aria-hidden="true">∞</span>
            <span><span className="unbound-brand-name">UNBOUND <b>AI</b></span><span className="unbound-brand-caption">Intelligent Web Agent</span></span>
          </a>
          <nav className="primary-nav" aria-label="Main navigation">
            {NAV_ITEMS.map((item) => (
              <button key={item.id} type="button" className={`nav-item ${activeView === item.id ? 'active' : ''}`} aria-current={activeView === item.id ? 'page' : undefined} onClick={() => setActiveView(item.id)}>
                <span className="nav-icon" aria-hidden="true">{item.icon}</span><span>{item.label}</span>
              </button>
            ))}
          </nav>
          <div className="local-agent-card">
            <span className={`status-light ${localDemoAvailable ? 'is-ready' : 'is-muted'}`} />
            <div><strong>Local Demo</strong><span>{localDemoAvailable ? 'Browser agent ready' : 'Demo mode unavailable'}</span></div>
            <span className="agent-chevron" aria-hidden="true">⌄</span>
          </div>
        </aside>

        <main className="center-workspace" id="dashboard">
          <div className="workspace-topbar">
            <div className="topbar-status">
              {(USE_MOCK || MOCK_AREAS.size > 0) && <span className="unbound-mock-notice" title="Mock responses are simulated and do not perform real actions.">{USE_MOCK ? 'Mock data: all areas' : `Mock data: ${[...MOCK_AREAS].join(', ')}`}</span>}
              <span className={`backend-pill ${health}`}><i aria-hidden="true" />Backend {health === 'online' ? 'online' : health === 'offline' ? 'offline' : 'checking'}</span>
            </div>
          </div>
          {health === 'offline' && !USE_MOCK && <div className="unbound-offline-banner"><p role="alert">The backend is not reachable. Start it, or set VITE_USE_MOCK=true in frontend/.env.local to use mock data.</p></div>}

          <section className="unbound-hero" aria-labelledby="dashboard-title">
            <div className="unbound-hero-copy">
              <span className="unbound-hero-logo" aria-hidden="true">∞</span>
              <h1 id="dashboard-title">Hello, ready to <em>explore?</em></h1>
              <p>I can browse the web, gather information, and deliver verified, evidence-backed research.</p>
            </div>
          </section>

          {activeView === 'chat' && (
            <section className="workspace-pane chat-pane" aria-label="Chat workspace">
              <ChatPanel
                key={draftPrompt?.id ?? 'default-chat'}
                history={messages} activeTaskId={activeTaskId}
                activeTask={activeTask?.task_id === activeTaskId ? activeTask : null}
                onSelect={openTask} onSubmit={handleSubmit} onNewConversation={startNewConversation}
                loading={conversationLoading} conversationError={conversationError}
                unavailableTaskCount={unavailableTaskCount} draftPrompt={draftPrompt}
              />
              {activeTaskId ? <TaskWorkspace key={activeTaskId} taskId={activeTaskId} onTaskChange={handleTaskChange} /> : <Card title="Live execution"><EmptyState title="No task running" hint="Send a request above or select a saved task to review its plan, approvals, live steps and results." /></Card>}
            </section>
          )}

          {(activeView === 'tasks' || activeView === 'browser' || activeView === 'evidence') && (
            <section className="workspace-pane" aria-label={`${activeView} workspace`}>
              {activeTaskId ? <TaskWorkspace key={`${activeTaskId}-${activeView}`} taskId={activeTaskId} onTaskChange={handleTaskChange} initialView={activeView === 'browser' ? 'live' : 'details'} /> : <Card title={activeView === 'browser' ? 'Browser' : activeView === 'evidence' ? 'Evidence' : 'Tasks'}><EmptyState title="Choose a task to inspect" hint="Select a recent task on the right, or start a request in Chat." /></Card>}
            </section>
          )}

          {activeView === 'history' && <section className="workspace-pane" aria-label="Task history"><TaskHistoryPanel onSelect={openTask} active /></section>}

          {activeView === 'settings' && (
            <section className="workspace-pane settings-pane" aria-label="Workspace settings">
              <RuntimeOverview health={browserStatus} backendHealth={health} />
              <div className="section-kicker"><span>Workspace tools</span><span className="section-kicker-line" /></div>
              <div className="unbound-tabs" role="tablist" aria-label="Workspace tools">
                {TABS.map((t) => <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)} className={`unbound-tab ${tab === t.id ? 'active' : ''}`}>{t.label}</button>)}
              </div>
              {tab === 'workflows' && <WorkflowPanel onTaskStarted={trackTask} />}
              {tab === 'tasks' && <TaskHistoryPanel onSelect={openTask} active />}
              {tab === 'data' && <DataPanel />}
              {tab === 'storage' && <S3SyncPanel />}
              <details className="browser-status-details"><summary>Self-hosted worker and security gates</summary><div className="browser-status-content"><p>Local Demo {localDemoAvailable ? 'available' : 'unavailable'}</p><p>Ubuntu installation: {browserStatus?.browser_worker?.installation ?? 'unknown'}</p><p>Worker service: {browserStatus?.browser_worker?.service ?? 'unknown'}</p><p>Guest connection: {browserStatus?.browser_worker?.connection ?? 'untested'}</p><p>Worker authentication: {browserStatus?.browser_worker?.authenticated === true ? 'verified' : 'unverified'}</p><p>Guest proxy: {browserStatus?.browser_worker?.guest_proxy ?? 'unknown'}</p><p className="status-blocked-note">Self-hosted public browsing is blocked pending independent containment verification.</p></div></details>
            </section>
          )}
          <footer className="unbound-footer"><span>UNBOUND AI</span><span>Evidence first · Actions stay reviewable</span></footer>
        </main>

        <aside className="right-rail" aria-label="Agent status and quick actions">
          <BrowserAgentCard health={browserStatus} backendHealth={health} />
          <div className="right-rail-card recent-tasks-card"><div className="rail-card-heading"><h2>Recent Tasks</h2><button type="button" className="text-action" onClick={() => setActiveView('history')}>View All</button></div><TaskHistoryPanel compact onSelect={openTask} active /></div>
          <section className="right-rail-card quick-actions"><h2>Quick Actions</h2><div className="quick-action-list">{QUICK_ACTIONS.map((action) => <button type="button" className="quick-action" key={action.label} onClick={() => startQuickAction(action.prompt)}><span className="quick-action-icon" aria-hidden="true">{action.icon}</span><span>{action.label}</span></button>)}</div></section>
        </aside>
      </div>
    </div>
  )
}

function BrowserAgentCard({ health, backendHealth }) {
  const cloud = health?.browserless_cloud_demo ?? {}
  const cloudSelected = cloud.selected === true
  const cloudConfigured = cloudSelected && cloud.configured === true
  const localDemo = health?.browser_local_demo_enabled === true || health?.browser_integration_status === 'local_demo_only'
  const status = cloudConfigured ? (cloud.connection_status === 'failed' ? 'Connection failed' : 'Ready to run tasks') : localDemo ? 'Local Demo ready' : 'Offline'
  const baselineCount = Array.isArray(cloud.baseline_allowed_hosts)
    ? cloud.baseline_allowed_hosts.length
    : Array.isArray(cloud.allowed_hosts) ? cloud.allowed_hosts.length : 0
  const subline = cloudConfigured
    ? `Browserless Cloud · ${baselineCount} baseline host(s) · ${cloud.task_scoped_domain_authorization_supported === true ? 'per-task review required' : 'per-task authorization unavailable'}`
    : localDemo ? 'Synthetic fixture only' : backendHealth === 'offline' ? 'Backend unavailable' : 'No browser provider configured'
  return <section className="browser-agent-card"><span className={`status-light ${cloudConfigured || localDemo ? 'is-ready' : 'is-muted'}`} /><div><strong>Browser Agent</strong><span>{status}</span><small>{subline}</small></div><span className="agent-chevron" aria-hidden="true">⌄</span><p className="cloud-policy-note">Cloud demo uses application-level URL controls; self-hosted network containment remains separate.</p></section>
}

function RuntimeOverview({ health, backendHealth }) {
  const worker = health?.browser_worker ?? {}
  const localDemo = health?.browser_local_demo_enabled === true || health?.browser_integration_status === 'local_demo_only'
  const cloud = health?.browserless_cloud_demo ?? {}
  const cloudConfigured = cloud.selected === true && cloud.configured === true
  const workerReady = worker.configured === true && worker.reachable === true && worker.authenticated === true && worker.execution_enabled === true
  const requiredContainmentSignals = [
    'browser_public_execution_enabled', 'browser_process_containment_verified',
    'browser_protected_deployment_verified', 'browser_deployment_acl_verified',
    'browser_egress_proxy_active', 'browser_egress_proxy_identity_verified',
    'browser_egress_proxy_validation_working',
    'browser_wfp_policy_installed', 'browser_wfp_policy_verified',
    'browser_wfp_policy_integrity_verified', 'browser_wfp_policy_ownership_verified',
    'browser_chromium_identity_verified', 'browser_ipv4_containment_verified',
    'browser_ipv6_containment_verified', 'browser_dns_bypass_blocked',
    'browser_udp_quic_bypass_blocked', 'browser_direct_bypass_blocked',
    'browser_proxy_outbound_restrictions_verified', 'browser_proxy_shutdown_fail_closed_verified',
  ]
  const containmentVerified = requiredContainmentSignals.every((signal) => health?.[signal] === true)
  const publicReady = containmentVerified && worker.public_browsing_enabled === true && workerReady
  const allowedDomains = Array.isArray(worker.allowed_domains) ? worker.allowed_domains : null
  const workerState = workerReady ? 'Ready'
    : worker.configured !== true ? 'Not configured'
      : worker.reachable !== true ? 'Offline'
        : 'Blocked'

  const cards = [
    { label: 'Browserless Cloud', value: cloudConfigured ? (cloud.connection_status === 'failed' ? 'Failed' : 'Ready') : cloud.selected ? 'Unavailable' : 'Not selected', detail: cloudConfigured ? 'Session opens when a task runs' : 'Cloud browser is not configured', tone: cloudConfigured ? (cloud.connection_status === 'failed' ? 'blocked' : 'ready') : 'muted' },
    { label: 'Local Demo', value: localDemo ? 'Ready' : 'Unavailable', detail: localDemo ? 'Synthetic browser workflows' : 'Not confirmed by backend', tone: localDemo ? 'ready' : 'muted' },
    { label: 'Ubuntu worker', value: workerState, detail: worker.authenticated === true ? 'Worker authenticated' : worker.configured === true ? 'Authentication not verified' : 'Isolated execution not integrated', tone: workerReady ? 'ready' : 'blocked' },
    { label: 'Public browsing', value: publicReady ? 'Enabled' : 'Disabled', detail: publicReady ? 'Security gates verified' : 'Containment verification required', tone: publicReady ? 'ready' : 'blocked' },
  ]

  return (
    <section className="runtime-overview" aria-label="System overview">
      <div className="overview-heading"><div><span className="unbound-eyebrow">SYSTEM OVERVIEW</span><h2>Workspace status</h2></div><span className={`overview-live ${backendHealth === 'online' ? 'is-online' : ''}`}><i />{backendHealth === 'online' ? 'Backend status received' : backendHealth === 'offline' ? 'Backend unreachable' : 'Checking backend'}</span></div>
      <div className="runtime-cards">
        {cards.map((card, index) => (
          <article className={`runtime-card tone-${card.tone}`} key={card.label}>
            <div className="runtime-card-top"><span className={`runtime-icon runtime-icon-${index}`} aria-hidden="true">{['◈', '⌘', '◉', '⛨'][index]}</span><span className="runtime-label">{card.label}</span></div>
            <div className="runtime-value">{card.value}</div>
            <div className="runtime-detail">{card.detail}</div>
          </article>
        ))}
      </div>
      <article className="worker-policy-card">
        <div className="policy-title-row"><div className="policy-shield" aria-hidden="true">⛨</div><div><span className="unbound-eyebrow">ISOLATED BROWSER</span><h3>Ubuntu worker & security policy</h3></div><span className={`policy-state ${publicReady ? 'policy-enabled' : 'policy-disabled'}`}>{publicReady ? 'Ready' : workerState}</span></div>
        <div className="policy-facts">
          <div><span>Installation</span><b>{health?.browser_worker?.installation ?? 'Unknown'}</b></div>
          <div><span>Service</span><b>{health?.browser_worker?.service ?? 'Unknown'}</b></div>
          <div><span>Connection</span><b>{health?.browser_worker?.connection ?? 'Untested'}</b></div>
          <div><span>Authentication</span><b>{worker.authenticated === true ? 'Verified' : 'Unverified'}</b></div>
          <div><span>Active public domains</span><b>{publicReady ? (allowedDomains?.length ? allowedDomains.join(', ') : 'Not exposed by backend') : 'None while browsing is disabled'}</b></div>
          <div><span>Process containment</span><b>{health?.browser_process_containment_verified === true ? 'Verified' : 'Not verified'}</b></div>
        </div>
        <p className="policy-footnote">Browser outcomes and security decisions appear in the selected task’s execution log. Worker status never enables browsing by itself.</p>
      </article>
    </section>
  )
}
