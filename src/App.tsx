import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createUserWithEmailAndPassword, onAuthStateChanged, onIdTokenChanged, signInWithEmailAndPassword, signOut, updateProfile, type User } from 'firebase/auth'
import { ensureFirebaseAuthPersistence, firebaseAuth } from './lib/firebase'
import { assignStaffComplaint, getAnalyticsOverview, getCurrentUser, getFinancialSummary, getPublicDashboardSummary, listEmergencyReviews, listEscalations, listNotifications, listPublicIssues, listStaffComplaints, listStaffDepartments, reviewDuplicateCandidate, setAuthToken, submitComplaint, trackComplaint, updateEmergencyReview, updateStaffComplaintStatus, type AnalyticsOverview, type AppNotification, type AuthenticatedUser, type ComplaintCategory, type ComplaintReceipt, type ComplaintTracking, type EmergencyReview, type Escalation, type FinancialSummary, type PublicDashboardSummary, type PublicIssue, type SavedComplaint, type StaffComplaint } from './lib/api'
import './App.css'

type View = 'overview' | 'operations' | 'report' | 'track' | 'my-complaints' | 'issues' | 'escalations' | 'transparency' | 'security'

const navigation: { id: View; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: '◉' },
  { id: 'operations', label: 'Operations queue', icon: '▦' },
  { id: 'report', label: 'Report an issue', icon: '+' },
  { id: 'track', label: 'Track complaint', icon: '⌕' },
  { id: 'my-complaints', label: 'My complaints', icon: '▤' },
  { id: 'issues', label: 'Issue registry', icon: '◌' },
  { id: 'escalations', label: 'Escalations', icon: '↗' },
  { id: 'transparency', label: 'Transparency centre', icon: '▧' },
  { id: 'security', label: 'Security centre', icon: '◇' },
]

const publicNavigation: View[] = ['overview', 'report', 'track', 'my-complaints', 'issues']
const staffOnlyViews: View[] = ['operations', 'escalations', 'transparency', 'security']

type Issue = PublicIssue & { area: string; reports: number; age: string; color: string }

function toIssue(publicIssue: PublicIssue): Issue {
  return {
    ...publicIssue,
    area: 'Location withheld',
    reports: publicIssue.report_count,
    age: formatAge(publicIssue.updated_at),
    color: priorityColor(publicIssue.priority),
  }
}

function readSavedComplaints(firebaseUid: string | undefined): SavedComplaint[] {
  if (!firebaseUid) return []

  const storageKey = `civiclens.myComplaints.${firebaseUid}`
  const saved = localStorage.getItem(storageKey)
  if (!saved) return []

  try {
    const parsed = JSON.parse(saved) as unknown
    return Array.isArray(parsed) ? parsed as SavedComplaint[] : []
  } catch {
    localStorage.removeItem(storageKey)
    return []
  }
}

const priorityClasses: Record<string, string> = {
  Critical: 'priority critical',
  High: 'priority high',
  Medium: 'priority medium',
  Low: 'priority low',
}

function App() {
  const [firebaseUser, setFirebaseUser] = useState<User | null>(null)
  const [currentUser, setCurrentUser] = useState<AuthenticatedUser | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [authError, setAuthError] = useState<string | null>(null)
  const [sessionRetry, setSessionRetry] = useState(0)
  const authRequestSequence = useRef(0)
  const [activeView, setActiveView] = useState<View>('overview')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showReportForm, setShowReportForm] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [receipt, setReceipt] = useState<ComplaintReceipt | null>(null)
  const [submissionError, setSubmissionError] = useState<string | null>(null)
  const [trackingResult, setTrackingResult] = useState<ComplaintTracking | null>(null)
  const [, refreshSavedComplaints] = useState(0)
  const savedComplaints = readSavedComplaints(firebaseUser?.uid)
  const [selectedIssue, setSelectedIssue] = useState<Issue | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [issuesError, setIssuesError] = useState<string | null>(null)
  const [summary, setSummary] = useState<PublicDashboardSummary | null>(null)
  const [staffComplaints, setStaffComplaints] = useState<StaffComplaint[]>([])
  const [staffDepartments, setStaffDepartments] = useState<{ id: string; name: string; description: string | null }[]>([])
  const [staffError, setStaffError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    let signedOutTimer: number | undefined
    let unsubscribeAuth: (() => void) | undefined
    let unsubscribeToken: (() => void) | undefined
    void ensureFirebaseAuthPersistence().then(() => {
      if (!active) return
      unsubscribeAuth = onAuthStateChanged(firebaseAuth, async (observedUser) => {
        const requestSequence = ++authRequestSequence.current
        const user = observedUser ?? firebaseAuth.currentUser
        if (!user) {
          if (signedOutTimer !== undefined) window.clearTimeout(signedOutTimer)
          setAuthLoading(true)
          signedOutTimer = window.setTimeout(() => {
            if (!active || requestSequence !== authRequestSequence.current || firebaseAuth.currentUser) return
            setFirebaseUser(null)
            setAuthToken(null)
            setCurrentUser(null)
            setAuthError(null)
            setAuthLoading(false)
          }, 300)
          return
        }

        if (signedOutTimer !== undefined) {
          window.clearTimeout(signedOutTimer)
          signedOutTimer = undefined
        }
        setFirebaseUser(user)
        setCurrentUser(null)
        setAuthError(null)
        setAuthLoading(true)
        try {
          let token = await user.getIdToken()
          let verifiedUser: AuthenticatedUser
          try {
            verifiedUser = await getCurrentUser(token)
          } catch {
            token = await user.getIdToken(true)
            verifiedUser = await getCurrentUser(token)
          }
          if (active && requestSequence === authRequestSequence.current) {
            setAuthToken(token)
            setCurrentUser(verifiedUser)
            setAuthError(null)
          }
        } catch (caught) {
          if (active && requestSequence === authRequestSequence.current) {
            setAuthToken(null)
            setAuthError(caught instanceof Error ? caught.message : 'Your session could not be verified. Please retry.')
          }
        } finally {
          if (active && requestSequence === authRequestSequence.current) setAuthLoading(false)
        }
      })
      unsubscribeToken = onIdTokenChanged(firebaseAuth, async (user) => {
        const activeUser = user ?? firebaseAuth.currentUser
        if (!activeUser) {
          setAuthToken(null)
          return
        }
        try {
          const token = await activeUser.getIdToken()
          if (active && firebaseAuth.currentUser?.uid === activeUser.uid) setAuthToken(token)
        } catch {
          if (active && firebaseAuth.currentUser?.uid === activeUser.uid) setAuthToken(null)
        }
      })
    }).catch((caught: unknown) => {
      if (active) {
        setAuthError(caught instanceof Error ? caught.message : 'Persistent sign-in is unavailable in this browser.')
        setAuthLoading(false)
      }
    })
    return () => {
      active = false
      if (signedOutTimer !== undefined) window.clearTimeout(signedOutTimer)
      unsubscribeAuth?.()
      unsubscribeToken?.()
    }
  }, [sessionRetry])

  useEffect(() => {
    getPublicDashboardSummary().then(setSummary).catch(() => setSummary(null))
  }, [])

  useEffect(() => {
    if (!currentUser?.is_staff) return
    Promise.all([listStaffComplaints(), listStaffDepartments()])
      .then(([complaints, departments]) => {
        setStaffComplaints(complaints)
        setStaffDepartments(departments)
      })
      .catch((error: unknown) => setStaffError(error instanceof Error ? error.message : 'Operations data could not be loaded.'))
  }, [currentUser?.is_staff])

  useEffect(() => {
    listPublicIssues()
      .then((publicIssues) => setIssues(publicIssues.map(toIssue)))
      .catch((error: unknown) => setIssuesError(error instanceof Error ? error.message : 'Public issues could not be loaded.'))
  }, [])
  const [search, setSearch] = useState('')

  const filteredIssues = useMemo(
    () => issues.filter((issue) => `${issue.title} ${issue.category} ${issue.area}`.toLowerCase().includes(search.toLowerCase())),
    [issues, search],
  )

  function openReport() {
    setActiveView('report')
    setShowReportForm(true)
    setSubmitted(false)
    setReceipt(null)
    setSubmissionError(null)
  }

  async function changeStaffStatus(id: string, status: string, note: string) {
    await updateStaffComplaintStatus(id, status, note)
    setStaffComplaints(await listStaffComplaints())
    setSummary(await getPublicDashboardSummary())
    setIssues((await listPublicIssues()).map(toIssue))
  }

  async function assignStaffIssue(id: string, departmentId: string) {
    await assignStaffComplaint(id, departmentId)
    setStaffComplaints(await listStaffComplaints())
    setSummary(await getPublicDashboardSummary())
    setIssues((await listPublicIssues()).map(toIssue))
  }

  async function reviewStaffCandidate(id: string, decision: string) {
    await reviewDuplicateCandidate(id, decision, 'Reviewed by staff')
    setStaffComplaints(await listStaffComplaints())
    setSummary(await getPublicDashboardSummary())
    setIssues((await listPublicIssues()).map(toIssue))
  }

  if (authLoading) return <div className="auth-shell"><p>Checking your secure session...</p></div>
  if (!firebaseUser) return <AuthView />
  if (!currentUser) {
    return <SessionRecoveryView
      error={authError}
      loading={authLoading}
      onRetry={() => {
        setAuthLoading(true)
        setSessionRetry((retry) => retry + 1)
      }}
      onSignOut={() => signOut(firebaseAuth)}
    />
  }

  const visibleNavigation = currentUser.is_staff
    ? navigation.filter((item) => item.id !== 'report' && item.id !== 'my-complaints')
    : navigation.filter((item) => publicNavigation.includes(item.id))
  const visibleView = !currentUser.is_staff && staffOnlyViews.includes(activeView) ? 'overview' : activeView
  const activeNavigation = navigation.find((item) => item.id === visibleView)
  const userInitials = (currentUser.display_name ?? currentUser.email ?? 'C')
    .split(/\s+/)
    .map((part) => part[0] ?? '')
    .join('')
    .slice(0, 2)
    .toUpperCase()

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="sidebar-backdrop" aria-label="Close navigation menu" onClick={() => setSidebarOpen(false)} />}
      <aside className={sidebarOpen ? 'sidebar sidebar-open' : 'sidebar'}>
        <div className="brand">
          <div className="brand-mark">C</div>
          <div>
            <strong>CivicLens</strong>
          </div>
        </div>

        <nav className="main-nav" aria-label="Main navigation">
          {visibleNavigation.map((item) => (
            <button className={visibleView === item.id ? 'nav-item active' : 'nav-item'} key={item.id} onClick={() => { setActiveView(item.id); setSidebarOpen(false) }}>
              <span className="nav-icon" aria-hidden="true">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <div className="profile">
            <div className="avatar">{(currentUser.display_name ?? currentUser.email ?? 'C').slice(0, 2).toUpperCase()}</div>
            <div><strong>{currentUser.display_name ?? currentUser.email}</strong><span>{currentUser.role}</span></div>
            <button className="text-button profile-signout" onClick={() => void signOut(firebaseAuth)}>Sign out</button>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="topbar-heading">
            <button className="menu-button" aria-label="Open navigation menu" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}>Menu</button>
            <div className="topbar-title">
              <p className="breadcrumb">CivicLens workspace</p>
              <h1>{activeNavigation?.label ?? 'CivicLens'}</h1>
            </div>
          </div>
          <div className="top-actions">
            <NotificationsMenu />
            <div className="user-avatar" aria-label={`Signed in as ${currentUser.display_name ?? currentUser.email ?? 'user'}`}>{userInitials}</div>
          </div>
        </header>

        <div className="content">
          {selectedIssue ? <IssueDetail issue={selectedIssue} onBack={() => setSelectedIssue(null)} /> : <>
          {visibleView === 'overview' && <Overview issues={issues} summary={summary} isStaff={currentUser.is_staff} displayName={currentUser.display_name ?? currentUser.email ?? 'Citizen'} onReport={openReport} onNavigate={(view) => setActiveView(currentUser.is_staff || !staffOnlyViews.includes(view) ? view : 'overview')} onSelectIssue={setSelectedIssue} />}
          {visibleView === 'operations' && currentUser.is_staff && <OperationsView role={currentUser.role} complaints={staffComplaints} departments={staffDepartments} error={staffError} onRefresh={async () => setStaffComplaints(await listStaffComplaints())} onStatus={changeStaffStatus} onAssign={assignStaffIssue} onReviewCandidate={reviewStaffCandidate} />}
          {visibleView === 'report' && !currentUser.is_staff && <ReportView submitted={submitted} receipt={receipt} submissionError={submissionError} onSubmit={async (payload) => {
            setSubmissionError(null)
            try {
              const nextReceipt = await submitComplaint(payload)
              setReceipt(nextReceipt)
              setSubmitted(true)
              const now = new Date().toISOString()
              const savedReceipt: SavedComplaint = {
                public_reference: nextReceipt.public_reference,
                category: payload.category,
                status: nextReceipt.status,
                submitted_at: now,
                updated_at: now,
                tracking_token: nextReceipt.tracking_token,
                history: [],
              }

              try {
                const nextSaved = [
                  savedReceipt,
                  ...savedComplaints.filter((item) => item.public_reference !== nextReceipt.public_reference),
                ].slice(0, 20)
                localStorage.setItem(`civiclens.myComplaints.${firebaseUser.uid}`, JSON.stringify(nextSaved))
                refreshSavedComplaints((version) => version + 1)
              } catch {
                setSubmissionError('Your report was submitted, but could not be saved to My complaints. Save the tracking token below.')
              }
            } catch (error) {
              setSubmissionError(error instanceof Error ? error.message : 'The complaint could not be submitted.')
            }
          }} onOpenForm={() => setShowReportForm(true)} showForm={showReportForm} />}
          {visibleView === 'track' && <TrackingView result={trackingResult} onTrack={async (token) => {
            setTrackingResult(null)
            const tracked = await trackComplaint(token)
            setTrackingResult(tracked)
            const savedComplaint: SavedComplaint = { ...tracked, tracking_token: token }
            const nextSaved = [
              savedComplaint,
              ...savedComplaints.filter((item) => item.public_reference !== tracked.public_reference),
            ].slice(0, 20)
            localStorage.setItem(`civiclens.myComplaints.${firebaseUser.uid}`, JSON.stringify(nextSaved))
            refreshSavedComplaints((version) => version + 1)
          }} />}
          {visibleView === 'my-complaints' && !currentUser.is_staff && <MyComplaintsView complaints={savedComplaints} onSelect={async (complaint) => {
            setTrackingResult(await trackComplaint(complaint.tracking_token))
            setActiveView('track')
          }} onRefresh={async () => {
            const refreshed = await Promise.all(savedComplaints.map(async (complaint) => ({
              ...await trackComplaint(complaint.tracking_token),
              tracking_token: complaint.tracking_token,
            })))
            localStorage.setItem(`civiclens.myComplaints.${firebaseUser.uid}`, JSON.stringify(refreshed))
            refreshSavedComplaints((version) => version + 1)
          }} />}
          {visibleView === 'issues' && <IssuesView issues={filteredIssues} search={search} onSearch={setSearch} error={issuesError} onSelectIssue={setSelectedIssue} />}
          {visibleView === 'escalations' && currentUser.is_staff && <EscalationsView />}
          {visibleView === 'transparency' && currentUser.is_staff && <TransparencyView />}
          {visibleView === 'security' && currentUser.is_staff && <SecurityView />}
          </>}
        </div>
      </main>
    </div>
  )
}

function NotificationsMenu() {
 const [open, setOpen] = useState(false)
 const [notifications, setNotifications] = useState<AppNotification[]>([])
 const [loading, setLoading] = useState(false)
 const [error, setError] = useState<string | null>(null)
 const unreadCount = notifications.filter((notification) => !notification.read_at).length

 useEffect(() => {
   if (!open) return
   let active = true
   listNotifications()
     .then((items) => { if (active) setNotifications(items) })
     .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : 'Notifications could not be loaded.') })
     .finally(() => { if (active) setLoading(false) })
   return () => { active = false }
 }, [open])

 useEffect(() => {
   if (!open) return
   const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
   window.addEventListener('keydown', closeOnEscape)
   return () => window.removeEventListener('keydown', closeOnEscape)
 }, [open])

 function toggleNotifications() {
   const nextOpen = !open
   setOpen(nextOpen)
   setLoading(nextOpen)
   if (nextOpen) setError(null)
 }

 return <div className="notification-menu">
   <button className="icon-button notification-trigger" aria-label="Notifications" aria-expanded={open} aria-haspopup="dialog" onClick={toggleNotifications}>
     Notifications
     {unreadCount > 0 && <span className="notification-dot" aria-label={`${unreadCount} unread notification${unreadCount === 1 ? '' : 's'}`} />}
   </button>
   {open && <section className="notification-popover" role="dialog" aria-label="Notifications">
     <div className="notification-header"><div><h2>Notifications</h2><p>{unreadCount ? `${unreadCount} unread` : 'All caught up'}</p></div><button className="text-button" onClick={() => setOpen(false)}>Close</button></div>
     {loading && <p className="notification-state">Loading notifications...</p>}
     {!loading && error && <div className="notification-state notification-error"><p>{error}</p><button className="text-button" onClick={() => setOpen(false)}>Dismiss</button></div>}
     {!loading && !error && notifications.length === 0 && <div className="notification-state"><strong>No notifications yet</strong><p>Updates about complaint assignments and status changes will appear here.</p></div>}
     {!loading && !error && notifications.length > 0 && <div className="notification-list">{notifications.map((notification) => <article className={notification.read_at ? 'notification-item' : 'notification-item unread'} key={notification.id}><span className="notification-marker" /><div><strong>{notification.title}</strong><p>{notification.body}</p><time dateTime={notification.created_at}>{formatNotificationDate(notification.created_at)}</time></div></article>)}</div>}
   </section>}
 </div>
}

function formatNotificationDate(timestamp: string): string {
 const date = new Date(timestamp)
 return Number.isNaN(date.getTime()) ? 'Recently' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

function AuthView() {
  const [mode, setMode] = useState<'sign-in' | 'sign-up'>('sign-in')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  function authErrorMessage(error: unknown): string {
    const code = typeof error === 'object' && error !== null && 'code' in error ? String(error.code) : ''
    if (code.includes('email-already-in-use')) return 'This email already has an account. Sign in instead.'
    if (code.includes('invalid-credential') || code.includes('user-not-found') || code.includes('wrong-password')) return 'Email or password is incorrect.'
    if (code.includes('weak-password')) return 'Use a stronger password with at least 8 characters.'
    if (code.includes('network-request-failed')) return 'Could not reach Firebase Authentication. Check your connection and try again.'
    if (code.includes('too-many-requests')) return 'Too many attempts. Wait a few minutes, then try again.'
    if (code.includes('user-disabled')) return 'This account has been disabled. Contact the project administrator.'
    if (code.includes('invalid-email')) return 'Enter a valid email address.'
    if (code.includes('operation-not-allowed')) return 'Email and password sign-in is disabled for this Firebase project.'
    if (code.includes('unauthorized-domain')) return 'This website domain is not authorized in Firebase Authentication settings.'
    if (code.includes('invalid-api-key')) return 'Firebase Authentication is misconfigured for this build. Contact the project administrator.'
    if (code.includes('unsupported-persistence') || code.includes('web-storage-unsupported')) return 'This browser cannot keep a persistent sign-in session. Enable site storage and try again.'
    return 'Authentication failed. Please try again.'
  }

  return <div className="auth-shell"><div className="auth-card panel"><div className="brand"><div className="brand-mark">C</div><div><strong>CivicLens</strong></div></div><h1>{mode === 'sign-in' ? 'Sign in to CivicLens' : 'Create your citizen account'}</h1><p className="muted">Your account keeps complaint reporting and status history private.</p><form onSubmit={async (event) => {
    event.preventDefault()
    setError(null)
    setLoading(true)
    const form = new FormData(event.currentTarget)
    const email = String(form.get('email'))
    const password = String(form.get('password'))
    try {
      await ensureFirebaseAuthPersistence()
      if (mode === 'sign-in') {
        await signInWithEmailAndPassword(firebaseAuth, email, password)
      } else {
        const credential = await createUserWithEmailAndPassword(firebaseAuth, email, password)
        const displayName = String(form.get('displayName') ?? '').trim()
        if (displayName) await updateProfile(credential.user, { displayName })
      }
    } catch (caught) {
      setError(authErrorMessage(caught))
    } finally {
      setLoading(false)
    }
  }}><label>{mode === 'sign-up' && 'Name'}{mode === 'sign-up' && <input name="displayName" required minLength={2} />}</label><label>Email<input name="email" type="email" required /></label><label>Password<input name="password" type="password" required minLength={8} /></label>{error && <p className="emergency-note" role="alert">{error}</p>}<button className="primary-button" disabled={loading}>{loading ? 'Please wait...' : mode === 'sign-in' ? 'Sign in' : 'Create account'}</button></form><button className="text-button" onClick={() => setMode(mode === 'sign-in' ? 'sign-up' : 'sign-in')}>{mode === 'sign-in' ? 'Create a citizen account' : 'Already have an account? Sign in'}</button></div></div>
}

function SessionRecoveryView({ error, loading, onRetry, onSignOut }: {
  error: string | null
  loading: boolean
  onRetry: () => void
  onSignOut: () => Promise<void>
}) {
  return <div className="auth-shell"><div className="auth-card panel">
    <div className="brand"><div className="brand-mark">C</div><div><strong>CivicLens</strong></div></div>
    <h1>Reconnecting your session</h1>
    <p className="muted">{loading ? 'Verifying your account with CivicLens...' : error ?? 'Your account is signed in, but verification is taking longer than expected.'}</p>
    {!loading && <div className="form-actions"><button className="primary-button" onClick={onRetry}>Retry verification</button><button className="text-button" onClick={() => { void onSignOut() }}>Sign out</button></div>}
  </div></div>
}

function formatAge(timestamp: string): string {
  const elapsedHours = Math.max(0, Math.round((Date.now() - new Date(timestamp).getTime()) / 3_600_000))
  return elapsedHours < 1 ? 'recently' : elapsedHours < 24 ? `${elapsedHours}h ago` : `${Math.round(elapsedHours / 24)}d ago`
}

function priorityColor(priority: string): string {
  return priority === 'critical' ? 'coral' : priority === 'high' ? 'amber' : priority === 'medium' ? 'blue' : 'purple'
}

function TrackingView({ result, onTrack }: { result: ComplaintTracking | null; onTrack: (token: string) => Promise<void> }) {
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  return <section className="form-layout"><div className="panel form-panel"><div className="panel-heading"><div><h3>Track a complaint</h3><p>Use the private token you received after submission.</p></div><span className="secure-badge">Private lookup</span></div><form onSubmit={async (event) => { event.preventDefault(); setError(null); setLoading(true); try { const form = new FormData(event.currentTarget); await onTrack(String(form.get('trackingToken'))) } catch (caught) { setError(caught instanceof Error ? caught.message : 'The complaint could not be found.') } finally { setLoading(false) } }}><label>Private tracking token<input name="trackingToken" required minLength={32} maxLength={128} placeholder="Paste your tracking token" /></label>{error && <p className="emergency-note" role="alert">{error}</p>}<button type="submit" className="primary-button" disabled={loading}>{loading ? 'Checking...' : 'Check status'}</button></form>{result && <div className="success-state"><div className="success-icon">✓</div><h2>{result.public_reference}</h2><p>Category: <strong>{result.category}</strong></p><p>Status: <strong>{result.status.replaceAll('_', ' ')}</strong></p>{result.master_issue_reference && <div className="linked-issue"><h3>Linked civic issue</h3><p><strong>{result.master_issue_reference}</strong> · {result.master_issue_status?.replaceAll('_', ' ')}</p><p className="muted">{result.report_count} related report{result.report_count === 1 ? '' : 's'}</p></div>}<p className="muted">Submitted {new Date(result.submitted_at).toLocaleString()}</p><div className="status-history"><h3>Progress history</h3>{result.history.length === 0 ? <p className="muted">No status updates recorded yet.</p> : result.history.map((entry) => <div className="history-entry" key={`${entry.status}-${entry.created_at}`}><strong>{entry.status.replaceAll('_', ' ')}</strong><span>{entry.note ?? 'Status updated'}</span><small>{new Date(entry.created_at).toLocaleString()}</small></div>)}</div></div>}</div><aside className="panel guidance-panel"><div className="guidance-icon">Private</div><h3>Private by design</h3><p>Only the cryptographically protected tracking token can retrieve this status. Reporter identity is not displayed.</p></aside></section>
}

function MyComplaintsView({ complaints, onSelect, onRefresh }: { complaints: SavedComplaint[]; onSelect: (complaint: SavedComplaint) => Promise<void>; onRefresh: () => Promise<void> }) {
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  async function refresh() {
    setRefreshing(true)
    setError(null)
    try {
      await onRefresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Complaint updates could not be loaded.')
    } finally {
      setRefreshing(false)
    }
  }

  async function selectComplaint(complaint: SavedComplaint) {
    setSelected(complaint.public_reference)
    setError(null)
    try {
      await onSelect(complaint)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Complaint details could not be loaded.')
    } finally {
      setSelected(null)
    }
  }

  return <section className="page-intro">
    <div>
      <p className="eyebrow">PRIVATE CITIZEN VIEW</p>
      <h2>My complaints</h2>
      <p className="muted">Reports submitted from this browser are listed here. Your private tracking tokens are stored only on this device.</p>
    </div>
    <button className="secondary-button" onClick={refresh} disabled={refreshing}>{refreshing ? 'Refreshing...' : 'Refresh status'}</button>
    {error && <p className="emergency-note" role="alert">{error}</p>}
    {complaints.length === 0
      ? <div className="empty-state panel"><div className="empty-icon">No data</div><h3>No saved complaints</h3><p>After submitting a report, keep the tracking token to view its status here.</p></div>
      : <div className="issue-list panel">{complaints.map((complaint) => <button className="issue-row" key={complaint.public_reference} onClick={() => void selectComplaint(complaint)} disabled={selected !== null}><div className="issue-main"><div className="issue-title">{complaint.public_reference}</div><div className="issue-meta">{complaint.category.replaceAll('_', ' ')} · Submitted {new Date(complaint.submitted_at).toLocaleString()}</div></div><span className="priority medium">{selected === complaint.public_reference ? 'Loading...' : complaint.status.replaceAll('_', ' ')}</span></button>)}</div>}
  </section>
}

function Overview({ issues, summary, isStaff, displayName, onReport, onNavigate, onSelectIssue }: { issues: Issue[]; summary: PublicDashboardSummary | null; isStaff: boolean; displayName: string; onReport: () => void; onNavigate: (view: View) => void; onSelectIssue: (issue: Issue) => void }) {
  const statusCounts = issues.reduce<Record<string, number>>((counts, issue) => {
    counts[issue.status] = (counts[issue.status] ?? 0) + 1
    return counts
  }, {})
  const maxStatusCount = Math.max(1, ...Object.values(statusCounts))

  return (
    <>
      <section className="welcome-row">
        <div>
          <p className="eyebrow">{isStaff ? 'CIVIC OPERATIONS' : 'CITIZEN WORKSPACE'}</p>
          <h2>Good afternoon, {displayName.split(' ')[0]}</h2>
          <p className="muted">{isStaff ? 'Here’s what’s happening across the civic network today.' : 'Report local issues and keep track of your complaints securely.'}</p>
        </div>
        {!isStaff && <button className="primary-button" onClick={onReport}>Report an issue</button>}
      </section>

      <section className="stat-grid">
        <StatCard label="Open master issues" value={summary ? summary.open_master_issues.toLocaleString() : '—'} change="Live" note="current registry" icon="IR" accent="blue" />
        <StatCard label="Duplicate reports merged" value={summary ? summary.duplicate_reports.toLocaleString() : '—'} change="Live" note="linked reports" icon="⛓" accent="purple" />
        <StatCard label="Critical priority" value={summary ? summary.critical_issues.toLocaleString() : '—'} change="Live" note="open issues" icon="!" accent="coral" />
        <StatCard label="Resolved issues" value={summary ? summary.resolved_issues.toLocaleString() : '—'} change="Live" note="completed records" icon="✓" accent="green" />
      </section>

      <section className="dashboard-grid">
        <div className="panel issue-panel">
          <div className="panel-heading">
            <div><h3>Priority queue</h3><p>Master issues requiring attention</p></div>
            <button className="text-button" onClick={() => onNavigate('issues')}>View all</button>
          </div>
          <div className="issue-list">
            {issues.slice(0, 3).map((issue) => <IssueRow issue={issue} key={issue.id} onSelect={onSelectIssue} />)}
          </div>
        </div>
        <div className="panel">
          <div className="panel-heading">
            <div><h3>Registry status</h3><p>Live public issue records</p></div>
            <button className="text-button" onClick={() => onNavigate('issues')}>View registry</button>
          </div>
          <div className="registry-summary"><strong>{issues.length}</strong><span>visible issues</span></div>
          {Object.keys(statusCounts).length === 0
            ? <p className="registry-status-empty">Registry status will appear when public issue records are available.</p>
            : <div className="registry-status-list">{Object.entries(statusCounts).sort(([left], [right]) => left.localeCompare(right)).map(([status, count]) => <div className="registry-status-row" key={status}>
              <span className="registry-status-label">{status.replaceAll('_', ' ')}</span>
              <span className="registry-status-track"><span className="registry-status-fill" style={{ width: `${(count / maxStatusCount) * 100}%` }} /></span>
              <span className="registry-status-count">{count}</span>
            </div>)}</div>}
        </div>
      </section>

      <section className="dashboard-grid lower-grid">
        <div className="panel privacy-note">
          <div className="panel-heading"><div><h3>Location privacy</h3><p>Public registry safeguards</p></div></div>
          <p>Precise complaint coordinates and reporter details are not included in public issue records. Civic teams use protected location data for operations.</p>
          <button className="text-button" onClick={() => onNavigate('issues')}>Browse public issues</button>
        </div>
        {isStaff && <div className="panel escalation-panel">
          <div className="panel-heading"><div><h3>Escalation workflow</h3><p>Review live escalations and urgent reports</p></div></div>
          <p className="muted">Open the operations queue to view current assignment and escalation records.</p>
          <button className="text-button" onClick={() => onNavigate('escalations')}>Open escalation queue</button>
        </div>}
      </section>
    </>
  )
}

function StatCard({ label, value, change, note, icon, accent }: { label: string; value: string; change: string; note: string; icon: string; accent: string }) {
  return <div className="stat-card"><div className={`stat-icon ${accent}`}>{icon}</div><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className={`stat-change ${change.startsWith('-') ? 'negative' : ''}`}>{change} <span>{note}</span></div></div>
}

function IssueRow({ issue, onSelect }: { issue: Issue; onSelect?: (issue: Issue) => void }) {
  return <button className="issue-row" onClick={() => onSelect?.(issue)}><div className={`issue-marker ${issue.color}`} /><div className="issue-main"><div className="issue-title">{issue.title}</div><div className="issue-meta">{issue.id} · {issue.area}</div></div><div className="issue-reports"><strong>{issue.reports}</strong><span>reports</span></div><span className={priorityClasses[issue.priority]}>{issue.priority}</span><span className="issue-time">{issue.age}</span></button>
}

function ReportView({ submitted, receipt, submissionError, onSubmit, onOpenForm, showForm }: { submitted: boolean; receipt: ComplaintReceipt | null; submissionError: string | null; onSubmit: (payload: { category: ComplaintCategory; description: string; latitude: number; longitude: number; locationLabel: string; isAnonymous: boolean; images: File[] }) => Promise<void>; onOpenForm: () => void; showForm: boolean }) {
  const [latitude, setLatitude] = useState('')
  const [longitude, setLongitude] = useState('')
  const [locationStatus, setLocationStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [locationError, setLocationError] = useState('Location access was unavailable. Allow location permission or enter coordinates manually.')
  const [submitting, setSubmitting] = useState(false)

  const fetchCurrentLocation = useCallback(() => {
    if (!navigator.geolocation) {
      setLocationError('This browser does not provide location access. Enter coordinates manually.')
      setLocationStatus('error')
      return
    }

    setLocationStatus('loading')
    setLocationError('')
    const handleError = (error: GeolocationPositionError) => {
      if (error.code === error.PERMISSION_DENIED) {
        setLocationError('Location access is blocked for this site. In Chrome, open the site controls beside the address bar, set Location to Allow, then retry. Also check that Windows location services are enabled, or enter coordinates manually.')
      } else if (error.code === error.TIMEOUT) {
        setLocationError('Location lookup timed out. Check device location services and try again, or enter coordinates manually.')
      } else {
        setLocationError('Your device could not determine a location. Check device location services and try again, or enter coordinates manually.')
      }
      setLocationStatus('error')
    }
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLatitude(position.coords.latitude.toFixed(6))
        setLongitude(position.coords.longitude.toFixed(6))
        setLocationStatus('ready')
      },
      handleError,
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 120000 },
    )
  }, [])

  useEffect(() => {
    if (!showForm || locationStatus !== 'idle') return
    const request = window.setTimeout(fetchCurrentLocation, 0)
    return () => window.clearTimeout(request)
  }, [fetchCurrentLocation, locationStatus, showForm])

  if (!showForm) return <section className="empty-state panel"><div className="empty-icon">＋</div><h2>Report a civic issue</h2><p>Submit a complaint with an image, description and location. The system will protect your identity and look for related reports.</p><button className="primary-button" onClick={onOpenForm}>Start a report</button></section>
  return <section className="form-layout">
    <div className="panel form-panel">
      <div className="panel-heading">
        <div><h3>New civic issue report</h3><p>Your report will receive a private, pseudonymous tracking ID.</p></div>
        <span className="secure-badge">⌁ Secure intake</span>
      </div>
      {submitted && receipt
        ? <div className="success-state">
          <div className="success-icon">✓</div>
          <h2>Report received</h2>
          {submissionError && <p role="alert" className="emergency-note">{submissionError}</p>}
          <p>Your pseudonymous tracking ID is <strong>{receipt.public_reference}</strong>. Save the tracking token below to check updates privately.</p>
          <p><strong>{receipt.tracking_token}</strong></p>
          <button className="secondary-button" onClick={() => window.location.reload()}>Create another report</button>
        </div>
        : <form onSubmit={async (event) => {
          event.preventDefault()
          setSubmitting(true)
          try {
            const form = new FormData(event.currentTarget)
            await onSubmit({
              category: form.get('category') as ComplaintCategory,
              description: String(form.get('description')).trim(),
              latitude: Number(latitude),
              longitude: Number(longitude),
              locationLabel: String(form.get('locationLabel')).trim(),
              isAnonymous: form.get('anonymous') === 'on',
              images: form.getAll('images').filter((file): file is File => file instanceof File && file.size > 0),
            })
          } finally {
            setSubmitting(false)
          }
        }}>
          {submissionError && <p role="alert" className="emergency-note">{submissionError}</p>}
          <label>Issue category
            <select name="category" required defaultValue="">
              <option value="" disabled>Select a category</option>
              <option value="road_damage">Road damage / pothole</option>
              <option value="garbage">Garbage accumulation</option>
              <option value="water_leakage">Water leakage</option>
              <option value="drainage">Drainage problem</option>
              <option value="streetlight">Streetlight failure</option>
              <option value="sanitation">Public sanitation</option>
              <option value="other">Other civic issue</option>
            </select>
          </label>
          <label>Describe the issue
            <textarea name="description" required minLength={10} placeholder="What happened? Include useful landmarks, but avoid sharing personal information." rows={5} />
          </label>
          <div className="form-two-col">
            <label>Approximate location
              <input name="locationLabel" required minLength={2} pattern=".*\S.*" title="Enter a location or nearby landmark." placeholder="City, locality or landmark" />
            </label>
            <label>Latitude
              <input name="latitude" type="number" step="any" min="-90" max="90" required value={latitude} onChange={(event) => setLatitude(event.target.value)} placeholder="Enter latitude" />
            </label>
            <label>Longitude
              <input name="longitude" type="number" step="any" min="-180" max="180" required value={longitude} onChange={(event) => setLongitude(event.target.value)} placeholder="Enter longitude" />
            </label>
            <button type="button" className="secondary-button" onClick={fetchCurrentLocation} disabled={locationStatus === 'loading'}>{locationStatus === 'loading' ? 'Fetching location...' : 'Use current location'}</button>
            <label>Evidence image (required)
              <input name="images" type="file" multiple accept="image/jpeg,image/png,image/webp" required />
            </label>
          </div>
          {locationStatus === 'loading' && <p className="muted" role="status">Requesting device location. Allow the browser location prompt if it appears.</p>}
          {locationStatus === 'ready' && <p className="muted" role="status">Current device location captured.</p>}
          {locationStatus === 'error' && <p className="emergency-note" role="alert">{locationError}</p>}
          <label className="check-label"><input name="anonymous" type="checkbox" defaultChecked required /> I confirm this report will use a pseudonymous tracking ID.</label>
          <div className="form-actions"><button type="submit" className="primary-button" disabled={submitting}>{submitting ? 'Submitting...' : 'Submit securely →'}</button></div>
        </form>}
    </div>
    <aside className="panel guidance-panel">
      <div className="guidance-icon">✦</div>
      <h3>What happens next?</h3>
      <ol>
        <li><strong>Secure intake</strong><span>Your details are separated from the public issue record.</span></li>
        <li><strong>Duplicate check</strong><span>Image, text and nearby reports are compared.</span></li>
        <li><strong>Human verification</strong><span>Potential matches are reviewed before merging.</span></li>
        <li><strong>Progress updates</strong><span>Track status with your private ID.</span></li>
      </ol>
      <div className="emergency-note"><strong>Immediate danger?</strong><span>Contact local emergency services first. This portal is not a replacement for emergency response.</span></div>
    </aside>
  </section>
}

function OperationsView({ role, complaints, departments, error, onRefresh, onStatus, onAssign, onReviewCandidate }: {
  role: AuthenticatedUser['role']
  complaints: StaffComplaint[]
  departments: { id: string; name: string; description: string | null }[]
  error: string | null
  onRefresh: () => Promise<void>
  onStatus: (id: string, status: string, note: string) => Promise<void>
  onAssign: (id: string, departmentId: string) => Promise<void>
  onReviewCandidate: (id: string, decision: string) => Promise<void>
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [departmentId, setDepartmentId] = useState('')
  const selected = complaints.find((complaint) => complaint.id === selectedId) ?? null
  const canEditStatus = role === 'officer' || role === 'supervisor' || role === 'administrator'
  const canAssign = role === 'supervisor' || role === 'administrator'
  const canReviewDuplicates = role === 'supervisor' || role === 'administrator'
  async function run(action: () => Promise<void>) {
    setBusy(true)
    setActionError(null)
    try { await action() } catch (caught) { setActionError(caught instanceof Error ? caught.message : 'Staff action failed.') } finally { setBusy(false) }
  }
  return <section className="page-intro"><div><p className="eyebrow">STAFF OPERATIONS</p><h2>Complaint operations queue</h2><p className="muted">Review reports, assign departments, and record auditable status changes.</p></div><button className="secondary-button" onClick={() => void run(onRefresh)} disabled={busy}>{busy ? 'Working...' : 'Refresh queue'}</button>{(error || actionError) && <p className="emergency-note" role="alert">{error ?? actionError}</p>}<div className="operations-layout"><div className="issue-list panel">{complaints.length === 0 ? <div className="empty-state"><h3>No complaints found</h3><p>New citizen reports will appear here.</p></div> : complaints.map((complaint) => <button className={`issue-row ${selectedId === complaint.id ? 'selected' : ''}`} aria-pressed={selectedId === complaint.id} key={complaint.id} onClick={() => { setSelectedId(complaint.id); setDepartmentId('') }}><div className={`issue-marker ${complaint.emergency_flag ? 'coral' : 'blue'}`} /><div className="issue-main"><div className="issue-title">{complaint.public_reference} · {complaint.category.replaceAll('_', ' ')}</div><div className="issue-meta">{complaint.description}</div>{complaint.assigned_department_name && <div className="issue-meta">Assigned department: {complaint.assigned_department_name}</div>}</div><span className="priority medium">{complaint.status.replaceAll('_', ' ')}</span></button>)}</div>{selected && <div className="panel operations-detail"><button className="text-button" onClick={() => setSelectedId(null)}>← Close details</button><p className="eyebrow">COMPLAINT DETAIL</p><h3>{selected.public_reference}</h3><p>{selected.description}</p><p className="muted">Location: {selected.location_label ?? 'Not provided'} · Reports: {selected.report_count}</p><div className="note-panel"><strong>Current status: {selected.status.replaceAll('_', ' ')}</strong>{selected.assigned_department_name && <p>Assigned department: <strong>{selected.assigned_department_name}</strong></p>}</div>  {canEditStatus ? <label>Status<select value={selected.status} disabled={busy} onChange={(event) => void run(() => onStatus(selected.id, event.target.value, 'Status updated by staff'))}>{['submitted', 'under_verification', 'verified', 'assigned', 'in_progress', 'resolved', 'closed', 'reopened', 'rejected'].map((status) => <option value={status} key={status}>{status.replaceAll('_', ' ')}</option>)}</select></label> : <p className="muted">Read-only role: status changes are disabled.</p>}{canAssign && <><label>Assign department<select disabled={busy || departments.length === 0} value={departmentId} onChange={(event) => setDepartmentId(event.target.value)}><option value="">{departments.length ? 'Select department' : 'No departments configured'}</option>{departments.map((department) => <option value={department.id} key={department.id}>{department.name}</option>)}</select></label><button className="primary-button" disabled={busy || !departmentId} onClick={() => void run(async () => { await onAssign(selected.id, departmentId); setDepartmentId('') })}>{selected.assigned_department_name ? 'Reassign department' : 'Assign department'}</button></>}<div className="note-panel"><p>Priority: <strong>{selected.priority ?? 'unassigned'}</strong></p><p>Master issue: <strong>{selected.master_issue_reference ?? 'not linked'}</strong></p><p>Evidence files: <strong>{selected.evidence.length}</strong></p><p>Duplicate candidates: <strong>{selected.duplicate_candidates.length}</strong></p></div>{selected.duplicate_candidates.length > 0 && <div className="note-panel"><h3>Duplicate review</h3>{selected.duplicate_candidates.map((candidate) => <div className="history-entry" key={candidate.candidate_master_issue_id}><span>Score: {candidate.combined_score ?? '—'} · {candidate.decision}</span>{  canReviewDuplicates && candidate.decision === 'potential' && <span><button className="text-button" onClick={() => void run(() => onReviewCandidate(candidate.id, 'duplicate'))}>Confirm duplicate</button><button className="text-button" onClick={() => void run(() => onReviewCandidate(candidate.id, 'new_issue'))}>Mark new issue</button></span>}</div>)}</div>}<div className="status-history"><h3>Status history</h3>{selected.history.length ? selected.history.map((entry) => <div className="history-entry" key={entry.created_at}><strong>{entry.to_status.replaceAll('_', ' ')}</strong><span>{entry.note ?? 'Status updated'}</span><small>{new Date(entry.created_at).toLocaleString()}</small></div>) : <p className="muted">No status updates recorded.</p>}</div></div>}</div></section>
}

function IssuesView({ issues: visibleIssues, search, onSearch, error, onSelectIssue }: { issues: Issue[]; search: string; onSearch: (value: string) => void; error: string | null; onSelectIssue: (issue: Issue) => void }) {
  return <><section className="page-intro"><div><p className="eyebrow">PUBLIC REGISTRY · REDACTED VIEW</p><h2>Master issue registry</h2><p className="muted">Browse verified civic issues without exposing reporter identities or exact locations.</p></div><div className="search-wrap"><span>⌕</span><input aria-label="Search public issues" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Search issues" /></div></section>{error && <div className="panel note-panel" role="alert"><p>{error}</p></div>}<div className="panel table-panel"><div className="table-header"><span>Issue</span><span>Reports</span><span>Priority</span><span>Status</span><span>Age</span></div>{visibleIssues.length === 0 && !error && <div className="empty-state"><h3>No public master issues yet</h3><p>Verified civic issues will appear here after review.</p></div>}{visibleIssues.map((issue) => <button className="table-row" key={issue.id} onClick={() => onSelectIssue(issue)}><div className="table-issue"><div className={`issue-marker ${issue.color}`} /><div><strong>{issue.title}</strong><span>{issue.public_reference} · {issue.category} · {issue.area}</span></div></div><strong>{issue.reports}</strong><span className={priorityClasses[issue.priority.charAt(0).toUpperCase() + issue.priority.slice(1)]}>{issue.priority}</span><span className="status-pill">{issue.status.replaceAll('_', ' ')}</span><span className="muted">{issue.age}</span></button>)}</div></>
}

function IssueDetail({ issue, onBack }: { issue: Issue; onBack: () => void }) {
  return <section className="form-layout"><div className="panel form-panel"><button className="text-button" onClick={onBack}>← Back to issue registry</button><p className="eyebrow">PUBLIC ISSUE DETAIL</p><h2>{issue.title}</h2><p className="muted">{issue.public_reference}</p><div className="detail-grid"><div><span className="metric-label">Category</span><strong>{issue.category.replaceAll('_', ' ')}</strong></div><div><span className="metric-label">Priority</span><strong>{issue.priority}</strong></div><div><span className="metric-label">Status</span><strong>{issue.status.replaceAll('_', ' ')}</strong></div><div><span className="metric-label">Reports</span><strong>{issue.reports}</strong></div><div><span className="metric-label">Actions recorded</span><strong>{issue.action_count}</strong></div><div><span className="metric-label">Last updated</span><strong>{new Date(issue.updated_at).toLocaleString()}</strong></div></div><div className="note-panel"><p>Location: {issue.area}. Public views use approximate locations to protect residents.</p><p>This issue was last updated {issue.age}.</p></div></div><aside className="panel guidance-panel"><div className="guidance-icon">◈</div><h3>Privacy-safe issue view</h3><p>Reporter identities, exact coordinates, evidence images and internal assignments are restricted from the public registry.</p></aside></section>
}

function EscalationsView() {
  const [escalations, setEscalations] = useState<Escalation[] | null>(null)
  const [reviews, setReviews] = useState<EmergencyReview[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const load = async () => {
    setBusy(true)
    try {
      const [nextEscalations, nextReviews] = await Promise.all([listEscalations(), listEmergencyReviews()])
      setEscalations(nextEscalations)
      setReviews(nextReviews)
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Workflow data could not be loaded.')
    } finally { setBusy(false) }
  }
  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => window.clearTimeout(timer)
  }, [])
  const review = async (id: string, reviewStatus: string) => {
    setBusy(true)
    try { await updateEmergencyReview(id, reviewStatus); await load() }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Emergency review could not be updated.'); setBusy(false) }
  }
  const visibleEscalations = escalations ?? []
  const visibleReviews = reviews ?? []
  const activeEscalations = visibleEscalations.filter((item) => !['resolved', 'cancelled'].includes(item.status))
  const [now] = useState(() => Date.now())
  const overdue = activeEscalations.filter((item) => item.sla_due_at && new Date(item.sla_due_at).getTime() < now).length
  return <><section className="page-intro"><div><p className="eyebrow">MUNICIPAL WORKFLOW</p><h2>Escalations & emergency review</h2><p className="muted">Review urgent reports and track issues that require action within an SLA.</p></div><button className="secondary-button" onClick={() => void load()} disabled={busy}>{busy ? 'Refreshing...' : 'Refresh workflow'}</button></section>{error && <div className="panel note-panel" role="alert">{error}</div>}<div className="escalation-cards"><EscalationCard level="OPEN" title="Open escalations" owner="Operations management" sla="Active" count={String(activeEscalations.length)} /><EscalationCard level="SLA" title="Overdue review" owner="Requires attention" sla="Past due" count={String(overdue)} /><EscalationCard level="URGENT" title="Emergency queue" owner="Human review required" sla="Pending" count={String(visibleReviews.length)} /></div><div className="panel escalation-list"><div className="panel-heading"><div><h3>Emergency review queue</h3><p>Emergency classification is never silently accepted or discarded.</p></div><span className="secure-badge">Live workflow</span></div>{visibleReviews.length === 0 ? <p className="muted">No pending emergency reviews.</p> : visibleReviews.map((item) => <div className="escalation-item" key={item.id}><span className="level-dot coral">{item.severity}</span><div><strong>{item.complaint?.public_reference ?? item.complaint_id}</strong><span>{item.complaint?.description ?? 'Complaint details unavailable'}</span></div><span><button className="text-button" disabled={busy} onClick={() => void review(item.id, 'verified')}>Verify</button><button className="text-button" disabled={busy} onClick={() => void review(item.id, 'not_emergency')}>Dismiss</button></span></div>)}</div><div className="panel escalation-list"><div className="panel-heading"><div><h3>Escalation queue</h3><p>Active escalations and their SLA deadlines.</p></div></div>{visibleEscalations.length === 0 ? <p className="muted">No escalations recorded.</p> : visibleEscalations.map((item) => <div className="escalation-item" key={item.id}><span className="level-dot amber">L{item.level}</span><div><strong>{item.reason}</strong><span>{item.authority} · {item.status} · {item.sla_due_at ? `Due ${new Date(item.sla_due_at).toLocaleString()}` : 'No SLA deadline'}</span></div></div>)}</div></>
}

function EscalationCard({ level, title, owner, sla, count }: { level: string; title: string; owner: string; sla: string; count: string }) {
  return <div className="panel escalation-card"><div className="level-badge">{level}</div><h3>{title}</h3><p>{owner}</p><div className="card-divider" /><div className="card-detail"><span>SLA trigger</span><strong>{sla}</strong></div><div className="card-detail"><span>Active cases</span><strong>{count}</strong></div></div>
}

function TransparencyView() {
  const [analytics, setAnalytics] = useState<AnalyticsOverview | null>(null)
  const [financial, setFinancial] = useState<FinancialSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    void Promise.all([getAnalyticsOverview(), getFinancialSummary()])
      .then(([nextAnalytics, nextFinancial]) => { setAnalytics(nextAnalytics); setFinancial(nextFinancial) })
      .catch((caught) => setError(caught instanceof Error ? caught.message : 'Transparency data could not be loaded.'))
  }, [])
  const resolved = analytics?.master_issues.by_status.resolved ?? 0
  const totalSpend = financial ? Object.values(financial.totals_by_currency_category).reduce((sum, amount) => sum + amount, 0) : 0
  return <><section className="page-intro"><div><p className="eyebrow">PUBLIC ACCOUNTABILITY</p><h2>Transparency centre</h2><p className="muted">Verified actions, recurring issues and public expenditure summaries.</p></div></section>{error && <div className="panel note-panel" role="alert">{error}</div>}<div className="transparency-grid"><div className="panel transparency-metric"><span className="metric-label">Resolved issues</span><strong>{resolved}</strong><span className="metric-change">From the current issue registry</span></div><div className="panel transparency-metric"><span className="metric-label">Recorded expenditure</span><strong>₹{totalSpend.toLocaleString('en-IN')}</strong><span className="metric-change">{financial?.record_count ?? 0} verified records</span></div><div className="panel transparency-metric"><span className="metric-label">Emergency reports</span><strong>{analytics?.complaints.emergency ?? 0}</strong><span className="metric-change warning">{analytics?.escalations.overdue_sla ?? 0} overdue escalations</span></div></div><div className="panel"><div className="panel-heading"><div><h3>Operational summary</h3><p>Aggregated records only; reporter identities and private evidence remain restricted.</p></div><span className="secure-badge">Verified records</span></div><div className="fund-table"><div><span>Metric</span><span>Value</span><span>Scope</span></div><div><strong>Total complaints</strong><span>{analytics?.complaints.total ?? '—'}</span><span>All submitted reports</span></div><div><strong>Master issues</strong><span>{analytics?.master_issues.total ?? '—'}</span><span>Verified issue groups</span></div><div><strong>Escalations</strong><span>{analytics?.escalations.total ?? '—'}</span><span>Municipal workflow</span></div></div></div></>
}

function SecurityView() {
  return <><section className="page-intro"><div><p className="eyebrow">PROTECTION & AUDITABILITY</p><h2>Security centre</h2><p className="muted">A clear view of security controls protecting the complaint lifecycle.</p></div><span className="secure-badge large">● All core controls healthy</span></section><div className="security-grid"><SecurityItem title="Authentication" detail="Firebase Auth · session protection" status="Healthy" /><SecurityItem title="Data access" detail="Role-based access · restricted identities" status="Healthy" /><SecurityItem title="Upload safety" detail="Type validation · size limits · quarantine" status="Configured" /><SecurityItem title="Abuse protection" detail="Rate limits · anomaly review queue" status="Configured" /><SecurityItem title="Audit trail" detail="Administrative actions are recorded" status="Healthy" /><SecurityItem title="Emergency routing" detail="Human review required · no silent discard" status="Healthy" /></div><div className="panel security-callout"><div className="callout-icon">⌁</div><div><h3>Privacy boundary</h3><p>Public issue records contain approximate locations and pseudonymous IDs. Identity records and sensitive complaints are restricted to authorized personnel.</p></div><button className="text-button">View policy →</button></div></>
}

function SecurityItem({ title, detail, status }: { title: string; detail: string; status: string }) {
  return <div className="panel security-item"><div className="security-check">✓</div><div><strong>{title}</strong><span>{detail}</span></div><span className="healthy">{status}</span></div>
}

export default App
