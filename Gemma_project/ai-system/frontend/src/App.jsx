import { useState, useEffect } from 'react'
import Sidebar from './components/Sidebar'
import ChatWindow from './components/ChatWindow'
import ChatInput from './components/ChatInput'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import AdminPanel from './pages/AdminPanel'
import CategoryPage from './pages/CategoryPage'
import WorkflowBuilderPage from './pages/WorkflowBuilderPage'

const API = 'http://127.0.0.1:8000'

export default function App() {
  // ── Auth ──────────────────────────────────────────────────────────────
  const [authToken, setAuthToken] = useState(() => localStorage.getItem('auth_token') || null)
  const [authUser,  setAuthUser]  = useState(() => {
    try { return JSON.parse(localStorage.getItem('auth_user') || 'null') } catch { return null }
  })

  // ── Category ──────────────────────────────────────────────────────────
  const [activeCategory, setActiveCategory] = useState(
    () => localStorage.getItem('active_category') || null
  )

  const [page, setPage] = useState(() => {
    const hasToken    = localStorage.getItem('auth_token') && localStorage.getItem('auth_user')
    const hasCategory = localStorage.getItem('active_category')
    if (hasToken && hasCategory) return 'chat'
    if (hasToken) return 'category'
    return 'login'
  })

  // ── Chat ─────────────────────────────────────────────────────────────
  const [sessions,        setSessions]      = useState([])
  const [activeSessionId, setActiveSession] = useState(null)
  const [messages,        setMessages]      = useState([])
  const [currentDocId,    setCurrentDocId]  = useState(null)
  const [isLoading,       setIsLoading]     = useState(false)

  // Load sessions on login
  useEffect(() => {
    if (!authToken) return
    fetch(`${API}/api/sessions`, { headers: { Authorization: `Bearer ${authToken}` } })
      .then(r => r.json()).then(setSessions).catch(() => {})
  }, [authToken])

  // ── Auth handlers ─────────────────────────────────────────────────────
  function handleLogin(token, user) {
    localStorage.setItem('auth_token', token)
    localStorage.setItem('auth_user', JSON.stringify(user))
    setAuthToken(token)
    setAuthUser(user)
    const savedCat = localStorage.getItem('active_category')
    setPage(savedCat ? 'chat' : 'category')
  }

  function handleLogout() {
    localStorage.removeItem('auth_token')
    localStorage.removeItem('auth_user')
    localStorage.removeItem('active_category')
    setAuthToken(null)
    setAuthUser(null)
    setActiveCategory(null)
    setSessions([])
    setMessages([])
    setCurrentDocId(null)
    setPage('login')
  }

  // ── Category handlers ─────────────────────────────────────────────────
  function handleSelectCategory(categoryId) {
    localStorage.setItem('active_category', categoryId)
    setActiveCategory(categoryId)
    setMessages([])
    setActiveSession(null)
    setCurrentDocId(null)
    setPage('chat')
  }

  function handleChangeCategory() {
    localStorage.removeItem('active_category')
    setActiveCategory(null)
    setMessages([])
    setActiveSession(null)
    setCurrentDocId(null)
    setPage('category')
  }

  // ── Page routing (ALL hooks above this line) ──────────────────────────
  if (page === 'login')     return <LoginPage           onLogin={handleLogin} onGoRegister={() => setPage('register')} />
  if (page === 'register')  return <RegisterPage         onLogin={handleLogin} onGoLogin={() => setPage('login')} />
  if (page === 'admin')     return <AdminPanel            token={authToken} user={authUser} onBack={() => setPage('chat')} />
  if (page === 'category')  return <CategoryPage          user={authUser} onSelectCategory={handleSelectCategory} onLogout={handleLogout} />
  if (page === 'workflows') return <WorkflowBuilderPage  token={authToken} user={authUser} onBack={() => setPage('chat')} />

  // ── Helpers ───────────────────────────────────────────────────────────
  const authHead = { Authorization: `Bearer ${authToken}` }

  function sysMsg(content, systemType = 'info') {
    setMessages(prev => [...prev, { role: 'system', content, systemType }])
  }

  function handleNewChat() {
    setActiveSession(null)
    setMessages([])
    setCurrentDocId(null)
  }

  async function handleSelectSession(id) {
    setActiveSession(id)
    setCurrentDocId(null)
    try {
      const res  = await fetch(`${API}/api/sessions/${id}`, { headers: authHead })
      const data = await res.json()
      setMessages(data.messages || [])
    } catch { setMessages([]) }
  }

  async function handleDeleteSession(id) {
    try { await fetch(`${API}/api/sessions/${id}`, { method: 'DELETE', headers: authHead }) } catch {}
    setSessions(prev => prev.filter(s => s.id !== id))
    if (activeSessionId === id) handleNewChat()
  }

  async function handleSend(text, docId = undefined) {
    if (!text.trim() || isLoading) return
    const documentId = docId !== undefined ? docId : currentDocId
    setMessages(prev => [...prev, { role: 'user', content: text, workflow: 'general', sources: [] }])
    setIsLoading(true)

    try {
      const res = await fetch(`${API}/api/chat`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', ...authHead },
        body:    JSON.stringify({
          text,
          session_id:  activeSessionId,
          document_id: documentId,
          category:    activeCategory,
        }),
      })
      const data = await res.json()

      if (!activeSessionId) {
        setActiveSession(data.session_id)
        setSessions(prev => [
          { id: data.session_id, title: text.slice(0, 60), updated_at: new Date().toISOString() },
          ...prev,
        ])
      } else {
        setSessions(prev =>
          prev.map(s => s.id === data.session_id ? { ...s, updated_at: new Date().toISOString() } : s)
        )
      }

      setMessages(prev => [...prev, {
        role:                  'assistant',
        content:               data.response || '(no response)',
        workflow:              data.workflow  || 'general',
        sources:               data.sources   || [],
        slots:                 data.slots     || null,
        appointmentWorkflowId: data.appointment_workflow_id || null,
        booking:               data.booking   || null,
        download_url:          data.download_url || null,
        admission_session_id:  data.admission_session_id || null,
      }])
    } catch {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: 'Error: Could not reach the backend. Is it running on port 8000?',
        workflow: 'general', sources: [],
      }])
    } finally {
      setIsLoading(false)
    }
  }

  async function handleFileUpload(file, messageText) {
    const userText = messageText || `Attached: ${file.name}`
    setMessages(prev => [...prev, { role: 'user', content: userText, workflow: 'general', sources: [] }])
    setIsLoading(true)

    const form = new FormData()
    form.append('file', file)
    try {
      const res  = await fetch(`${API}/api/upload`, { method: 'POST', headers: authHead, body: form })
      if (!res.ok) throw new Error('upload failed')
      const data = await res.json()

      setCurrentDocId(data.document_id)
      sysMsg(`${file.name} processed (${data.chunks} chunks) — document is now active`, 'file')

      if (messageText.trim()) {
        await _sendToBackend(messageText.trim(), data.document_id)
      } else {
        setMessages(prev => [...prev, {
          role:     'assistant',
          content:  `I've read **${file.name}** (${data.chunks} chunks). What would you like to know about it?`,
          workflow: 'document_chat',
          sources:  [],
        }])
        setIsLoading(false)
      }
    } catch {
      sysMsg(`Failed to upload ${file.name}. Please try again.`, 'error')
      setIsLoading(false)
    }
  }

  async function _sendToBackend(text, docId) {
    try {
      const res = await fetch(`${API}/api/chat`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', ...authHead },
        body:    JSON.stringify({
          text,
          session_id:  activeSessionId,
          document_id: docId,
          category:    activeCategory,
        }),
      })
      const data = await res.json()
      if (!activeSessionId) {
        setActiveSession(data.session_id)
        setSessions(prev => [
          { id: data.session_id, title: text.slice(0, 60), updated_at: new Date().toISOString() },
          ...prev,
        ])
      }
      setMessages(prev => [...prev, {
        role:     'assistant',
        content:  data.response || '(no response)',
        workflow: data.workflow || 'general',
        sources:  data.sources  || [],
      }])
    } catch {
      sysMsg('Error processing your question. Please try again.', 'error')
    } finally {
      setIsLoading(false)
    }
  }

  function handleSlotConfirmed(slot) {
    const booking = {
      appointment_id: `APT-${slot.slot_id}`,
      slot_id:     slot.slot_id,
      doctor_name: slot.doctor_name,
      specialty:   slot.specialty,
      date:        slot.date,
      time:        slot.time,
    }
    setMessages(prev => [...prev, {
      role:     'assistant',
      content:  'Your appointment has been confirmed! Here are your booking details:',
      workflow: 'medical_appointment',
      sources:  [],
      booking,
    }])
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="flex h-screen bg-white overflow-hidden">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onNewChat={handleNewChat}
        onSelectSession={handleSelectSession}
        onDeleteSession={handleDeleteSession}
        activeCategory={activeCategory}
        onChangeCategory={handleChangeCategory}
      />

      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <header className="h-12 border-b border-slate-200 flex items-center px-4 gap-3 shrink-0">
          <span className="font-semibold text-slate-800 text-sm">AskRiphah</span>

          {currentDocId && (
            <button
              onClick={() => { setCurrentDocId(null); sysMsg('Document context cleared.', 'info') }}
              className="text-xs text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full hover:bg-blue-100 transition-colors"
              title="Click to clear document context"
            >
              📎 Document active · clear
            </button>
          )}

          <div className="ml-auto flex items-center gap-3">
            <button
              onClick={() => setPage('workflows')}
              className="text-xs px-3 py-1.5 rounded-lg bg-purple-600 text-white hover:bg-purple-500 font-semibold transition-colors flex items-center gap-1.5"
            >
              ⚡ Workflows
            </button>
            {authUser?.role === 'admin' && (
              <button
                onClick={() => setPage('admin')}
                className="text-xs px-3 py-1.5 rounded-lg bg-purple-100 text-purple-700 hover:bg-purple-200 font-medium transition-colors"
              >
                Admin Panel
              </button>
            )}
            <span className="text-xs text-slate-500">{authUser?.username}</span>
            <button
              onClick={handleLogout}
              className="text-xs px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 transition-colors"
            >
              Logout
            </button>
          </div>
        </header>

        <ChatWindow
          messages={messages}
          isLoading={isLoading}
          onPromptClick={handleSend}
          onSlotConfirmed={handleSlotConfirmed}
          activeCategory={activeCategory}
        />

        <ChatInput
          onSend={handleSend}
          onFileUpload={handleFileUpload}
          disabled={isLoading}
          category={activeCategory}
        />
      </div>
    </div>
  )
}
