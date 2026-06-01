/**
 * Workflow Builder — Zapier/n8n-style visual automation
 *
 * Each workflow has three layers:
 *   TRIGGER    — what event starts the automation
 *   CONDITIONS — optional rules that must pass before running
 *   ACTIONS    — ordered tasks to execute
 */
import { useState, useEffect, useRef } from 'react'
import {
  Zap, Plus, Play, Trash2, Sparkles, CheckCircle2, XCircle,
  Loader2, Clock, ChevronRight, ChevronDown, ArrowLeft, X,
  Mail, Globe, Brain, Bell, Link2, Database, FileText,
  GitBranch, Settings2, RefreshCw, Save,
} from 'lucide-react'

const API = 'http://127.0.0.1:8000'

// ─── meta ────────────────────────────────────────────────────────────────────

const DOMAINS = [
  { key: 'general',   label: 'General',   color: 'bg-slate-600'  },
  { key: 'admission', label: 'Admission', color: 'bg-blue-600'   },
  { key: 'medical',   label: 'Medical',   color: 'bg-teal-600'   },
  { key: 'hr',        label: 'HR',        color: 'bg-purple-600' },
  { key: 'property',  label: 'Property',  color: 'bg-amber-600'  },
]

const TRIGGER_TYPES = [
  { key: 'manual',      label: 'Manual',          icon: '👆', desc: 'Run by clicking a button',              fields: []                              },
  { key: 'keyword',     label: 'Chat Keyword',     icon: '💬', desc: 'Triggered when user types a keyword',   fields: ['keyword']                     },
  { key: 'schedule',    label: 'Schedule',         icon: '⏰', desc: 'Run on a time schedule',               fields: ['label', 'cron']               },
  { key: 'webhook',     label: 'Webhook',          icon: '🔗', desc: 'Triggered by external HTTP POST',       fields: ['endpoint_id']                 },
  { key: 'form_submit', label: 'Form Submission',  icon: '📝', desc: 'When a specific form is submitted',     fields: ['form_name', 'form_id']        },
]

const OPERATORS = [
  { key: 'eq',           label: 'equals'              },
  { key: 'neq',          label: 'not equals'          },
  { key: 'contains',     label: 'contains'            },
  { key: 'not_contains', label: 'does not contain'    },
  { key: 'gt',           label: 'greater than'        },
  { key: 'lt',           label: 'less than'           },
  { key: 'gte',          label: '≥ greater or equal'  },
  { key: 'lte',          label: '≤ less or equal'     },
  { key: 'starts_with',  label: 'starts with'         },
  { key: 'ends_with',    label: 'ends with'           },
]

const ACTION_TYPES = [
  { key: 'send_email',         label: 'Send Email',         Icon: Mail,      color: 'text-blue-400',   fields: ['to','subject','body']         },
  { key: 'browser_automation', label: 'Browser Automation', Icon: Globe,     color: 'text-green-400',  fields: ['task']                        },
  { key: 'llm_generate',       label: 'AI Generate',        Icon: Brain,     color: 'text-purple-400', fields: ['prompt']                      },
  { key: 'notification',       label: 'Notification',       Icon: Bell,      color: 'text-amber-400',  fields: ['message','channel']           },
  { key: 'api_call',           label: 'API Call',           Icon: Link2,     color: 'text-cyan-400',   fields: ['url','method']                },
  { key: 'db_write',           label: 'Save to Database',   Icon: Database,  color: 'text-red-400',    fields: ['table']                       },
  { key: 'create_record',      label: 'Create Record',      Icon: FileText,  color: 'text-orange-400', fields: ['entity']                      },
]

function actionMeta(type) {
  return ACTION_TYPES.find(a => a.key === type) || { key: type, label: type, Icon: Settings2, color: 'text-slate-400', fields: [] }
}

// ─── small helpers ────────────────────────────────────────────────────────────

function DomainPill({ domain }) {
  const d = DOMAINS.find(x => x.key === domain) || DOMAINS[0]
  return <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full text-white ${d.color}`}>{d.label}</span>
}

function SectionCard({ icon, title, subtitle, children, accent = 'border-slate-700' }) {
  return (
    <div className={`rounded-2xl border ${accent} bg-slate-800/40 overflow-hidden`}>
      <div className="flex items-center gap-3 px-5 py-3.5 border-b border-slate-700 bg-slate-800/60">
        <span className="text-lg">{icon}</span>
        <div>
          <p className="text-sm font-bold text-white">{title}</p>
          {subtitle && <p className="text-[11px] text-slate-500">{subtitle}</p>}
        </div>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  )
}

// ─── TRIGGER editor ──────────────────────────────────────────────────────────

function TriggerEditor({ trigger, onChange }) {
  const type   = trigger?.type   || 'manual'
  const config = trigger?.config || {}
  const meta   = TRIGGER_TYPES.find(t => t.key === type) || TRIGGER_TYPES[0]

  function set(field, val) {
    onChange({ type, config: { ...config, [field]: val } })
  }

  const FIELD_LABELS = {
    keyword:   { label: 'Trigger keyword',  placeholder: 'e.g. apply for admission' },
    label:     { label: 'Schedule label',   placeholder: 'e.g. Every day at 9am' },
    cron:      { label: 'Cron expression',  placeholder: 'e.g. 0 9 * * *' },
    endpoint_id:{ label: 'Webhook ID',      placeholder: 'Auto-generated on save' },
    form_name: { label: 'Form name',        placeholder: 'e.g. Admission Application Form' },
    form_id:   { label: 'Form ID / URL',    placeholder: 'e.g. admission_form or /apply' },
  }

  return (
    <div className="space-y-4">
      {/* Type selector */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        {TRIGGER_TYPES.map(t => (
          <button
            key={t.key}
            onClick={() => onChange({ type: t.key, config: {} })}
            className={`flex items-start gap-2.5 p-3 rounded-xl border text-left transition-all ${
              type === t.key
                ? 'bg-purple-900/40 border-purple-600/60 shadow-lg shadow-purple-900/20'
                : 'bg-slate-800/60 border-slate-700 hover:border-slate-500'
            }`}
          >
            <span className="text-xl mt-0.5">{t.icon}</span>
            <div>
              <p className={`text-xs font-semibold ${type === t.key ? 'text-purple-200' : 'text-slate-300'}`}>{t.label}</p>
              <p className="text-[10px] text-slate-600 leading-tight mt-0.5">{t.desc}</p>
            </div>
          </button>
        ))}
      </div>

      {/* Config fields */}
      {meta.fields.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
          {meta.fields.map(f => (
            <div key={f}>
              <label className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider block mb-1.5">
                {FIELD_LABELS[f]?.label || f}
              </label>
              <input
                value={config[f] || ''}
                onChange={e => set(f, e.target.value)}
                placeholder={FIELD_LABELS[f]?.placeholder || ''}
                disabled={f === 'endpoint_id'}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-purple-500 disabled:opacity-40"
              />
            </div>
          ))}
        </div>
      )}

      {type === 'manual' && (
        <p className="text-xs text-slate-600 italic">
          This workflow runs when you click <strong className="text-slate-400">Run</strong> manually or via the REST API.
        </p>
      )}
      {type === 'webhook' && (
        <p className="text-xs text-slate-600 italic">
          After saving, a unique webhook URL will be generated: <code className="text-slate-400">POST /api/builder/trigger/:id</code>
        </p>
      )}
    </div>
  )
}

// ─── CONDITIONS editor ────────────────────────────────────────────────────────

function ConditionsEditor({ conditions, onChange }) {
  function add() {
    onChange([...conditions, { id: Date.now().toString(), field: '', operator: 'eq', value: '', logic: 'AND' }])
  }
  function remove(id) { onChange(conditions.filter(c => c.id !== id)) }
  function update(id, patch) { onChange(conditions.map(c => c.id === id ? { ...c, ...patch } : c)) }

  return (
    <div className="space-y-3">
      {conditions.length === 0 ? (
        <p className="text-xs text-slate-600 italic">No conditions — workflow runs for every trigger event.</p>
      ) : (
        conditions.map((c, i) => (
          <div key={c.id} className="flex items-center gap-2 flex-wrap">
            {i > 0 && (
              <select
                value={c.logic}
                onChange={e => update(c.id, { logic: e.target.value })}
                className="w-16 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-300 focus:outline-none"
              >
                <option value="AND">AND</option>
                <option value="OR">OR</option>
              </select>
            )}
            {i === 0 && <span className="text-xs text-slate-600 font-semibold w-12">IF</span>}

            <input
              value={c.field}
              onChange={e => update(c.id, { field: e.target.value })}
              placeholder="field name"
              className="flex-1 min-w-24 bg-slate-900 border border-slate-700 rounded-xl px-3 py-1.5 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
            />
            <select
              value={c.operator}
              onChange={e => update(c.id, { operator: e.target.value })}
              className="bg-slate-900 border border-slate-700 rounded-xl px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-purple-500"
            >
              {OPERATORS.map(op => <option key={op.key} value={op.key}>{op.label}</option>)}
            </select>
            <input
              value={c.value}
              onChange={e => update(c.id, { value: e.target.value })}
              placeholder="value"
              className="flex-1 min-w-24 bg-slate-900 border border-slate-700 rounded-xl px-3 py-1.5 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
            />
            <button onClick={() => remove(c.id)} className="text-slate-600 hover:text-red-400 p-1 shrink-0 transition-colors">
              <X size={13} />
            </button>
          </div>
        ))
      )}
      <button
        onClick={add}
        className="flex items-center gap-1.5 text-xs text-purple-400 hover:text-purple-300 font-semibold transition-colors mt-1"
      >
        <Plus size={12} /> Add Condition
      </button>
    </div>
  )
}

// ─── ACTIONS editor ───────────────────────────────────────────────────────────

function ActionRow({ action, index, total, onChange, onRemove, onMoveUp, onMoveDown }) {
  const [expanded, setExpanded] = useState(false)
  const meta = actionMeta(action.type)
  const config = action.config || {}

  function setConfig(k, v) { onChange({ ...action, config: { ...config, [k]: v } }) }

  const CONFIG_UI = {
    send_email: [
      { key: 'to',      label: 'To (email)',  placeholder: '{email} or name@example.com' },
      { key: 'subject', label: 'Subject',     placeholder: 'Your application has been received' },
      { key: 'body',    label: 'Body',        placeholder: 'Dear {name}, your application…', textarea: true },
    ],
    browser_automation: [
      { key: 'task', label: 'What to automate', placeholder: 'e.g. Submit admission form on riphah.edu.pk for {name}', textarea: true },
    ],
    llm_generate: [
      { key: 'prompt',     label: 'Prompt',         placeholder: 'Generate a welcome message for {name} applying for {program}…', textarea: true },
      { key: 'output_var', label: 'Save result as', placeholder: 'e.g. welcome_message' },
    ],
    notification: [
      { key: 'message', label: 'Message',   placeholder: 'Application submitted for {name}' },
      { key: 'channel', label: 'Channel',   placeholder: 'in-app / email / sms' },
    ],
    api_call: [
      { key: 'url',    label: 'URL',    placeholder: 'https://api.example.com/endpoint' },
      { key: 'method', label: 'Method', placeholder: 'POST' },
      { key: 'body',   label: 'Body (JSON)', placeholder: '{"key": "{value}"}', textarea: true },
    ],
    db_write: [
      { key: 'table', label: 'Table',     placeholder: 'admissions / appointments / etc.' },
      { key: 'data',  label: 'Data (JSON)', placeholder: '{"name": "{name}", "email": "{email}"}', textarea: true },
    ],
    create_record: [
      { key: 'entity', label: 'Entity type', placeholder: 'e.g. admission_application' },
    ],
  }

  const configFields = CONFIG_UI[action.type] || []

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-2xl overflow-hidden">
      {/* Header row */}
      <div className="flex items-center gap-3 px-4 py-3">
        {/* Order controls */}
        <div className="flex flex-col gap-0.5 shrink-0">
          <button onClick={onMoveUp}   disabled={index === 0}         className="text-slate-700 hover:text-slate-400 disabled:opacity-20 transition-colors"><ChevronRight size={11} className="-rotate-90" /></button>
          <button onClick={onMoveDown} disabled={index === total - 1} className="text-slate-700 hover:text-slate-400 disabled:opacity-20 transition-colors"><ChevronRight size={11} className="rotate-90" /></button>
        </div>

        {/* Number */}
        <span className="w-5 h-5 rounded-full bg-slate-700 text-slate-400 text-[10px] font-bold flex items-center justify-center shrink-0">
          {index + 1}
        </span>

        {/* Type selector */}
        <select
          value={action.type}
          onChange={e => onChange({ ...action, type: e.target.value, config: {} })}
          className="bg-slate-900 border border-slate-700 rounded-xl px-2 py-1.5 text-xs font-semibold text-slate-200 focus:outline-none focus:border-purple-500 shrink-0"
        >
          {ACTION_TYPES.map(a => <option key={a.key} value={a.key}>{a.label}</option>)}
        </select>

        {/* Action icon */}
        <meta.Icon size={14} className={`${meta.color} shrink-0`} />

        {/* Name input */}
        <input
          value={action.name || ''}
          onChange={e => onChange({ ...action, name: e.target.value })}
          placeholder="Action name…"
          className="flex-1 min-w-0 bg-transparent text-sm text-white placeholder-slate-600 focus:outline-none"
        />

        {/* Configure + delete */}
        <button
          onClick={() => setExpanded(v => !v)}
          className={`text-xs px-2.5 py-1 rounded-lg border transition-colors shrink-0 ${
            expanded ? 'border-purple-600/50 text-purple-300 bg-purple-900/30' : 'border-slate-700 text-slate-500 hover:text-slate-300'
          }`}
        >
          {expanded ? 'Close' : 'Configure'}
        </button>
        <button onClick={onRemove} className="text-slate-600 hover:text-red-400 p-1 transition-colors shrink-0">
          <X size={13} />
        </button>
      </div>

      {/* Config panel */}
      {expanded && (
        <div className="px-4 pb-4 border-t border-slate-700 pt-3 space-y-3">
          {configFields.length === 0 ? (
            <p className="text-xs text-slate-600 italic">No configuration needed for this action type.</p>
          ) : (
            configFields.map(f => (
              <div key={f.key}>
                <label className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider block mb-1">
                  {f.label}
                </label>
                {f.textarea ? (
                  <textarea
                    value={config[f.key] || ''}
                    onChange={e => setConfig(f.key, e.target.value)}
                    placeholder={f.placeholder}
                    rows={2}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white placeholder-slate-600 resize-none focus:outline-none focus:border-purple-500"
                  />
                ) : (
                  <input
                    value={config[f.key] || ''}
                    onChange={e => setConfig(f.key, e.target.value)}
                    placeholder={f.placeholder}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
                  />
                )}
              </div>
            ))
          )}
          <p className="text-[10px] text-slate-600 italic">
            Use <code className="text-slate-500">{'{field_name}'}</code> to reference trigger data (e.g. <code className="text-slate-500">{'{email}'}</code>)
          </p>
        </div>
      )}
    </div>
  )
}

function ActionsEditor({ actions, onChange }) {
  function add() {
    const order = actions.length > 0 ? Math.max(...actions.map(a => a.order || 0)) + 1 : 1
    onChange([...actions, { id: Date.now().toString(), type: 'llm_generate', name: '', config: {}, order }])
  }
  function remove(id)         { onChange(actions.filter(a => a.id !== id)) }
  function update(id, patch)  { onChange(actions.map(a => a.id === id ? patch : a)) }
  function move(index, dir) {
    const arr = [...actions]
    const swap = index + dir
    if (swap < 0 || swap >= arr.length) return
    ;[arr[index], arr[swap]] = [arr[swap], arr[index]]
    onChange(arr.map((a, i) => ({ ...a, order: i + 1 })))
  }

  return (
    <div className="space-y-2.5">
      {actions.length === 0 ? (
        <p className="text-xs text-slate-600 italic">No actions yet. Add at least one action to execute.</p>
      ) : (
        actions.map((a, i) => (
          <ActionRow
            key={a.id}
            action={a}
            index={i}
            total={actions.length}
            onChange={patch => update(a.id, patch)}
            onRemove={() => remove(a.id)}
            onMoveUp={() => move(i, -1)}
            onMoveDown={() => move(i, 1)}
          />
        ))
      )}
      <button
        onClick={add}
        className="flex items-center gap-1.5 text-xs text-purple-400 hover:text-purple-300 font-semibold transition-colors"
      >
        <Plus size={12} /> Add Action
      </button>
    </div>
  )
}

// ─── run results ─────────────────────────────────────────────────────────────

function RunPanel({ run, onClose }) {
  const [expanded, setExpanded] = useState(null)
  if (!run) return null

  const steps  = run.result?.steps || []
  const done   = run.status === 'completed'
  const failed = run.status === 'error' || run.status === 'skipped'
  const busy   = ['queued', 'running'].includes(run.status)

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-xl bg-[#1e293b] rounded-2xl border border-slate-700 shadow-2xl flex flex-col max-h-[85vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700 shrink-0">
          <div className="flex items-center gap-2.5">
            {busy   && <Loader2 size={15} className="text-blue-400 animate-spin" />}
            {done   && <CheckCircle2 size={15} className="text-green-400" />}
            {failed && <XCircle size={15} className="text-red-400" />}
            <span className={`text-sm font-bold ${done ? 'text-green-300' : failed ? 'text-red-300' : 'text-blue-300'}`}>
              {busy ? 'Running automation…' : done ? 'Completed' : run.status === 'skipped' ? 'Skipped — conditions not met' : 'Failed'}
            </span>
          </div>
          {!busy && (
            <button onClick={onClose} className="text-slate-500 hover:text-white transition-colors">
              <X size={16} />
            </button>
          )}
        </div>

        <div className="overflow-y-auto flex-1 px-5 py-4 space-y-3">
          {/* Run ID */}
          {run.run_id && <p className="text-[10px] font-mono text-slate-600">{run.run_id}</p>}

          {/* Summary */}
          {run.result?.summary && (
            <div className={`px-4 py-3 rounded-xl border ${done ? 'bg-green-500/10 border-green-500/20' : 'bg-slate-800 border-slate-700'}`}>
              <p className="text-sm text-slate-200 leading-relaxed">{run.result.summary}</p>
            </div>
          )}

          {/* Error */}
          {(run.error || run.result?.error) && (
            <div className="bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
              <p className="text-xs text-red-400">{run.error || run.result?.error}</p>
            </div>
          )}

          {/* Step results */}
          {steps.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-[10px] font-bold uppercase tracking-widest text-slate-600">Action Results</p>
              {steps.map((s, i) => (
                <div key={i} className="bg-slate-800 rounded-xl border border-slate-700 overflow-hidden">
                  <button
                    onClick={() => setExpanded(expanded === i ? null : i)}
                    className="w-full flex items-center gap-3 px-4 py-2.5 text-left"
                  >
                    <span className={`w-4 h-4 rounded-full flex items-center justify-center shrink-0 ${
                      s.status === 'ok' ? 'bg-green-500' : s.status === 'error' ? 'bg-red-500' : 'bg-slate-600'
                    }`}>
                      {s.status === 'ok'    ? <CheckCircle2 size={9} className="text-white" />
                      : s.status === 'error' ? <XCircle size={9} className="text-white" />
                      : <Clock size={9} className="text-white" />}
                    </span>
                    <span className="flex-1 text-xs font-medium text-white">{s.name || s.type}</span>
                    {expanded === i ? <ChevronDown size={11} className="text-slate-600" /> : <ChevronRight size={11} className="text-slate-600" />}
                  </button>
                  {expanded === i && s.output && (
                    <div className="px-4 pb-3 border-t border-slate-700">
                      <p className="text-xs text-slate-400 mt-2 whitespace-pre-wrap leading-relaxed">{s.output}</p>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {busy && !steps.length && (
            <div className="flex items-center gap-3 py-4">
              <Loader2 size={16} className="text-purple-400 animate-spin shrink-0" />
              <p className="text-sm text-slate-400">Executing actions, please wait…</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── domain input schemas ─────────────────────────────────────────────────────

const DOMAIN_INPUT_FIELDS = {
  admission: [
    // ── Personal ────────────────────────────────────────────────────────────
    { key: 'full_name',       label: 'Full Name (as on CNIC)',           required: true,  placeholder: 'Muhammad Ahmad Khan' },
    { key: 'father_name',     label: "Father's Name",                    required: false, placeholder: 'Muhammad Imran Khan' },
    { key: 'cnic',            label: 'CNIC / B-Form',                    required: true,  placeholder: '35202-1234567-9' },
    { key: 'dob',             label: 'Date of Birth (DD/MM/YYYY)',        required: false, placeholder: '15/06/2000' },
    { key: 'gender',          label: 'Gender',                           required: false, placeholder: 'Male / Female' },
    // ── Contact ─────────────────────────────────────────────────────────────
    { key: 'email',           label: 'Email Address',                    required: true,  placeholder: 'ahmad@gmail.com' },
    { key: 'phone',           label: 'Mobile Number',                    required: false, placeholder: '0300-1234567' },
    { key: 'alternate_phone', label: 'Alternate / WhatsApp Number',      required: false, placeholder: '0311-1234567' },
    { key: 'address',         label: 'Residential Address',              required: false, placeholder: 'House 5, Street 3, G-6, Islamabad' },
    { key: 'city',            label: 'City',                             required: false, placeholder: 'Islamabad' },
    // ── Academic ────────────────────────────────────────────────────────────
    { key: 'last_institute',  label: 'College / Last Institute Attended', required: false, placeholder: 'Government College Islamabad' },
    { key: 'matric_marks',    label: 'Matric Marks / %',                 required: false, placeholder: '850/1100 or 77%' },
    { key: 'inter_marks',     label: 'Intermediate Marks / %',           required: false, placeholder: '900/1100 or 82%' },
    { key: 'entry_test',      label: 'Entry Test Score',                 required: false, placeholder: '85 or Not yet appeared' },
    // ── Program ─────────────────────────────────────────────────────────────
    { key: 'campus',          label: 'Campus',                           required: false, placeholder: 'Islamabad / Lahore / Malakand' },
    { key: 'level',           label: 'Program Level',                    required: false, placeholder: 'Undergraduate / Postgraduate / MS / PhD' },
    { key: 'program',         label: 'Program — 1st choice',             required: true,  placeholder: 'MBBS / BS CS / BBA / MS Data Science' },
    { key: 'program2',        label: 'Program — 2nd choice (optional)',  required: false, placeholder: 'BS Biomedical Engineering' },
    { key: 'program3',        label: 'Program — 3rd choice (optional)',  required: false, placeholder: 'BS Software Engineering' },
    // ── Portal ──────────────────────────────────────────────────────────────
    { key: 'portal_password', label: 'Portal Password (min 8 chars)',    required: false, placeholder: 'Riphah@2026' },
    { key: 'heard_from',      label: 'How did you hear about Riphah?',   required: false, placeholder: 'Friend or Family / Facebook / Google' },
  ],
  medical: [
    // ── Doctor (matches DOCTOR_OPTIONS in medical_appointment_agent.py) ─────
    {
      key: 'doctor', label: 'Doctor (RMC Portal)', required: false,
      placeholder: 'Dr. Arooj Arshad / Ms. Sidrah Kanwal / Dr Muhammad Hashim PT / Dr Mehar un nisa PT',
    },
    // ── Patient details ──────────────────────────────────────────────────────
    { key: 'patient_name', label: 'Patient Full Name', required: true,  placeholder: 'Ahmad Khan' },
    { key: 'age',          label: 'Patient Age',        required: true,  placeholder: '30' },
    { key: 'phone',        label: 'Phone Number',       required: true,  placeholder: '03001234567' },
    { key: 'email',        label: 'Email Address',      required: false, placeholder: 'patient@gmail.com' },
    // ── Appointment slot (Flatpickr formats) ──────────────────────────────────
    { key: 'date',         label: 'Appointment Date (MM-DD-YYYY or YYYY-MM-DD)', required: false, placeholder: '06-20-2026' },
    { key: 'time_slot',    label: 'Appointment Time (12-hour, e.g. 10:30 AM)',   required: false, placeholder: '10:30 AM' },
    // ── Message / symptoms ───────────────────────────────────────────────────
    { key: 'message',      label: 'Symptoms / Reason for Visit', required: false, placeholder: 'Knee pain, follow-up after physiotherapy session' },
  ],
  hr: [
    { key: 'employee_name',  label: 'Employee Name',     required: true,  placeholder: 'Ali Hassan' },
    { key: 'employee_email', label: 'Employee Email',    required: true,  placeholder: 'ali@company.com' },
    { key: 'days_requested', label: 'Days Requested',    required: true,  placeholder: '3' },
    { key: 'start_date',     label: 'Start Date',        required: true,  placeholder: '01/06/2026' },
    { key: 'end_date',       label: 'End Date',          required: false, placeholder: '03/06/2026' },
    { key: 'reason',         label: 'Reason',            required: false, placeholder: 'Medical / Personal / Vacation' },
    { key: 'leave_balance',  label: 'Leave Balance (days)', required: false, placeholder: '10' },
  ],
  general: [
    { key: 'task', label: 'Task Description', required: true, placeholder: 'Describe what data the workflow should process…' },
  ],
  property: [
    { key: 'client_name',  label: 'Client Name',   required: true,  placeholder: 'Ahmad Khan' },
    { key: 'client_email', label: 'Client Email',  required: true,  placeholder: 'ahmad@gmail.com' },
    { key: 'property',     label: 'Property',      required: false, placeholder: 'Apartment / House / Plot' },
    { key: 'location',     label: 'Location',      required: false, placeholder: 'F-7, Islamabad' },
    { key: 'budget',       label: 'Budget',        required: false, placeholder: '5000000' },
    { key: 'message',      label: 'Message',       required: false, placeholder: 'Interested in 3-bed apartment' },
  ],
}

// ─── run input modal ──────────────────────────────────────────────────────────

function RunInputModal({ domain, onRun, onClose }) {
  const fields = DOMAIN_INPUT_FIELDS[domain] || DOMAIN_INPUT_FIELDS.general
  const [values, setValues] = useState(() => Object.fromEntries(fields.map(f => [f.key, ''])))
  const [error, setError]   = useState('')

  function set(key, val) { setValues(p => ({ ...p, [key]: val })) }

  function handleSubmit() {
    const missing = fields.filter(f => f.required && !values[f.key]?.trim())
    if (missing.length) {
      setError(`Required: ${missing.map(f => f.label).join(', ')}`)
      return
    }
    setError('')
    // Pass only non-empty fields
    const data = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()))
    onRun(data)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-lg bg-[#1e293b] rounded-2xl border border-slate-700 shadow-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700 shrink-0">
          <div>
            <p className="text-sm font-bold text-white">Provide Input Data</p>
            <p className="text-xs text-slate-500 mt-0.5">These values will be used by the automation actions</p>
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-white transition-colors">
            <X size={16} />
          </button>
        </div>

        {/* Fields */}
        <div className="overflow-y-auto flex-1 px-5 py-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {fields.map(f => (
              <div key={f.key} className={['task', 'symptoms', 'message'].includes(f.key) ? 'col-span-2' : ''}>
                <label className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 block mb-1">
                  {f.label} {f.required && <span className="text-red-400">*</span>}
                </label>
                {['task', 'symptoms', 'message'].includes(f.key) ? (
                  <textarea
                    value={values[f.key] || ''}
                    onChange={e => set(f.key, e.target.value)}
                    placeholder={f.placeholder}
                    rows={3}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white placeholder-slate-600 resize-none focus:outline-none focus:border-purple-500"
                  />
                ) : (
                  <input
                    value={values[f.key] || ''}
                    onChange={e => set(f.key, e.target.value)}
                    placeholder={f.placeholder}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
                  />
                )}
              </div>
            ))}
          </div>
          {error && <p className="text-xs text-red-400 mt-3">{error}</p>}
        </div>

        {/* Footer */}
        <div className="px-5 py-4 border-t border-slate-700 flex items-center justify-between shrink-0">
          <p className="text-[10px] text-slate-600">
            Fields marked <span className="text-red-400">*</span> are required
          </p>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors">
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              className="flex items-center gap-1.5 px-5 py-2 rounded-xl bg-green-600 hover:bg-green-500 text-white text-sm font-semibold transition-colors"
            >
              <Play size={13} /> Run Now
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── templates ───────────────────────────────────────────────────────────────

const TEMPLATES = [
  {
    id: 'admission',
    icon: '🎓',
    label: 'Admission Application',
    desc: 'Auto-fill & submit Riphah admission portal',
    wf: {
      name:        'Riphah Admission Portal Submission',
      description: 'Automatically fill and submit the Riphah International University admission portal with applicant data',
      domain:      'admission',
      trigger: { type: 'keyword', config: { keyword: 'apply for admission' } },
      conditions: [
        { id: 'c1', field: 'program', operator: 'contains', value: '', logic: 'AND' },
      ],
      actions: [
        {
          id: 'a1', order: 1,
          type: 'browser_automation',
          name: 'Submit Riphah Admission Portal',
          config: { task: 'Submit admission application on riphah.edu.pk for {full_name}, CNIC {cnic}, email {email}, program {program}, campus {campus}' },
        },
        {
          id: 'a2', order: 2,
          type: 'send_email',
          name: 'Send Confirmation Email',
          config: { to: '{email}', subject: 'Your Riphah Admission Application Has Been Submitted', body: 'Dear {full_name},\n\nYour application for {program} at Riphah International University has been submitted successfully.\n\nApplication ID: {application_id}\n\nRegards,\nRiphah Admissions Team' },
        },
        {
          id: 'a3', order: 3,
          type: 'notification',
          name: 'Admin Notification',
          config: { message: 'New admission application submitted for {full_name} — {program}', channel: 'in-app' },
        },
      ],
    },
  },
  {
    id: 'riphah_clinic',
    icon: '🏥',
    label: 'RMC Medical Appointment',
    desc: 'Auto-book at rmc.riphah.edu.pk/appointment (MetForm portal)',
    wf: {
      name:        'RMC Riphah Medical Appointment Booking',
      description: 'Automatically fill and submit the Riphah Medical Centre appointment form (MetForm, React-Select doctor dropdown, Flatpickr date/time)',
      domain:      'medical',
      trigger: { type: 'keyword', config: { keyword: 'book appointment' } },
      conditions: [],
      actions: [
        {
          id: 'a1', order: 1,
          type: 'browser_automation',
          name: 'Book Appointment on RMC Portal',
          config: {
            task: 'Book appointment at rmc.riphah.edu.pk for patient {patient_name}, age {age}, phone {phone}, doctor {doctor}, date {date}, time {time_slot}. Message: {message}',
          },
        },
        {
          id: 'a2', order: 2,
          type: 'send_email',
          name: 'Send Appointment Confirmation',
          config: {
            to:      '{email}',
            subject: 'Your RMC Appointment is Confirmed',
            body:    'Dear {patient_name},\n\nYour appointment at Riphah Medical Centre has been booked.\n\nDoctor: {doctor}\nDate: {date}\nTime: {time_slot}\nReason: {message}\n\nPlease arrive 15 minutes early with your CNIC.\n\nRMC Riphah Medical Centre',
          },
        },
        {
          id: 'a3', order: 3,
          type: 'notification',
          name: 'Staff Notification',
          config: {
            message: 'New appointment booked — {patient_name} | Doctor: {doctor} | {date} {time_slot}',
            channel: 'in-app',
          },
        },
      ],
    },
  },
  {
    id: 'hr_leave',
    icon: '👔',
    label: 'HR Leave Request',
    desc: 'Process employee leave requests automatically',
    wf: {
      name:        'HR Leave Request Processing',
      description: 'Automatically process and approve/reject employee leave requests based on policy rules',
      domain:      'hr',
      trigger: { type: 'form_submit', config: { form_name: 'Leave Request Form', form_id: 'hr_leave_form' } },
      conditions: [
        { id: 'c1', field: 'days_requested', operator: 'lte', value: '3', logic: 'AND' },
        { id: 'c2', field: 'leave_balance',  operator: 'gte', value: '1', logic: 'AND' },
      ],
      actions: [
        {
          id: 'a1', order: 1,
          type: 'llm_generate',
          name: 'Check Policy & Generate Decision',
          config: { prompt: 'Employee {employee_name} is requesting {days_requested} days leave from {start_date} to {end_date}. Reason: {reason}. Leave balance: {leave_balance} days. Based on HR policy, generate an approval decision and reason.' },
        },
        {
          id: 'a2', order: 2,
          type: 'db_write',
          name: 'Record Leave in System',
          config: { table: 'leave_requests', data: '{"employee": "{employee_name}", "days": "{days_requested}", "start": "{start_date}", "status": "approved"}' },
        },
        {
          id: 'a3', order: 3,
          type: 'send_email',
          name: 'Notify Employee',
          config: { to: '{employee_email}', subject: 'Leave Request Update', body: 'Dear {employee_name},\n\nYour leave request for {days_requested} days ({start_date} to {end_date}) has been processed.\n\nDecision: {action_a1}\n\nHR Department' },
        },
      ],
    },
  },
  {
    id: 'enquiry',
    icon: '📩',
    label: 'Student Enquiry Reply',
    desc: 'Auto-respond to student enquiries with AI',
    wf: {
      name:        'Automated Student Enquiry Response',
      description: 'Automatically generate and send personalised replies to student enquiries using AI',
      domain:      'university',
      trigger: { type: 'form_submit', config: { form_name: 'Contact / Enquiry Form', form_id: 'student_enquiry' } },
      conditions: [],
      actions: [
        {
          id: 'a1', order: 1,
          type: 'llm_generate',
          name: 'Generate Personalised Reply',
          config: { prompt: 'A student named {name} sent this enquiry: "{message}". They are interested in {program}. Write a warm, professional reply from Riphah International University admissions team. Include relevant program details and next steps.' },
        },
        {
          id: 'a2', order: 2,
          type: 'send_email',
          name: 'Send AI-Generated Reply',
          config: { to: '{email}', subject: 'Re: Your Enquiry About {program} — Riphah International University', body: '{action_a1}' },
        },
        {
          id: 'a3', order: 3,
          type: 'create_record',
          name: 'Log Enquiry in CRM',
          config: { entity: 'student_enquiry' },
        },
      ],
    },
  },
  {
    id: 'invoice',
    icon: '💰',
    label: 'Invoice Paid Action',
    desc: 'Trigger actions when invoice is paid',
    wf: {
      name:        'Invoice Paid — Auto Follow-up',
      description: 'When an invoice is marked as paid and amount > 500, send receipt and create record',
      domain:      'general',
      trigger: { type: 'webhook', config: { endpoint_id: '' } },
      conditions: [
        { id: 'c1', field: 'amount',  operator: 'gt',  value: '500', logic: 'AND' },
        { id: 'c2', field: 'status',  operator: 'eq',  value: 'paid', logic: 'AND' },
      ],
      actions: [
        {
          id: 'a1', order: 1,
          type: 'send_email',
          name: 'Send Payment Receipt',
          config: { to: '{client_email}', subject: 'Payment Receipt — Invoice #{invoice_id}', body: 'Dear {client_name},\n\nThank you for your payment of PKR {amount}.\n\nInvoice: #{invoice_id}\nDate: {payment_date}\n\nThis email serves as your official receipt.' },
        },
        {
          id: 'a2', order: 2,
          type: 'db_write',
          name: 'Record Payment',
          config: { table: 'payments', data: '{"invoice_id": "{invoice_id}", "amount": "{amount}", "client": "{client_name}", "date": "{payment_date}"}' },
        },
        {
          id: 'a3', order: 3,
          type: 'notification',
          name: 'Notify Finance Team',
          config: { message: 'Payment received: PKR {amount} from {client_name} for invoice #{invoice_id}', channel: 'in-app' },
        },
      ],
    },
  },
]

// ─── main page ────────────────────────────────────────────────────────────────

const BLANK_WF = () => ({
  name:       '',
  description:'',
  domain:     'general',
  trigger:    { type: 'manual', config: {} },
  conditions: [],
  actions:    [],
})

export default function WorkflowBuilderPage({ token, user, onBack }) {
  const [workflows,    setWorkflows]    = useState([])
  const [loadingList,  setLoadingList]  = useState(true)
  const [wf,           setWf]           = useState(BLANK_WF())   // draft being edited
  const [editingId,    setEditingId]    = useState(null)          // null = new, else WF-xxx
  const [showTemplates,setShowTemplates] = useState(false)
  const [showRunInput, setShowRunInput] = useState(false)
  const [saving,       setSaving]       = useState(false)
  const [generating,   setGenerating]   = useState(false)
  const [aiDesc,       setAiDesc]       = useState('')
  const [showAiInput,  setShowAiInput]  = useState(false)
  const [run,          setRun]          = useState(null)
  const [running,      setRunning]      = useState(false)
  const [deleteId,     setDeleteId]     = useState(null)
  const [saveError,    setSaveError]    = useState('')
  const pollRef = useRef(null)

  useEffect(() => { loadWorkflows() }, [])
  useEffect(() => () => clearInterval(pollRef.current), [])

  async function loadWorkflows() {
    setLoadingList(true)
    try {
      const res  = await fetch(`${API}/api/builder/workflows`, { headers: { Authorization: `Bearer ${token}` } })
      const data = await res.json()
      setWorkflows(data.workflows || [])
    } catch {}
    setLoadingList(false)
  }

  async function handleLoadWorkflow(wfSummary) {
    try {
      const res  = await fetch(`${API}/api/builder/workflows/${wfSummary.id}`, { headers: { Authorization: `Bearer ${token}` } })
      const full = await res.json()
      setWf({
        name:        full.name        || '',
        description: full.description || '',
        domain:      full.domain      || 'general',
        trigger:     full.trigger     || { type: 'manual', config: {} },
        conditions:  full.conditions  || [],
        actions:     full.actions     || [],
      })
      setEditingId(full.id)
      setSaveError('')
    } catch {}
  }

  async function handleAiGenerate() {
    if (!aiDesc.trim()) return
    setGenerating(true)
    setSaveError('')
    try {
      const res  = await fetch(`${API}/api/builder/generate`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body:    JSON.stringify({ description: aiDesc.trim(), domain: wf.domain }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Generation failed')
      const g = data.workflow
      setWf(prev => ({
        ...prev,
        name:        g.name        || prev.name,
        description: g.description || prev.description,
        domain:      g.domain      || prev.domain,
        trigger:     g.trigger     || prev.trigger,
        conditions:  g.conditions  || [],
        actions:     g.actions     || [],
      }))
      setShowAiInput(false)
      setAiDesc('')
    } catch (e) {
      setSaveError(`AI generation failed: ${e.message}`)
    }
    setGenerating(false)
  }

  // Returns the saved workflow ID (existing or newly created), or null on error.
  async function handleSave() {
    if (!wf.name.trim())    { setSaveError('Workflow name is required.');  return null }
    if (!wf.actions.length) { setSaveError('Add at least one action.');    return null }
    setSaving(true); setSaveError('')

    const body = { ...wf }
    try {
      let res, data, savedId
      if (editingId) {
        res  = await fetch(`${API}/api/builder/workflows/${editingId}`, {
          method:  'PUT',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body:    JSON.stringify(body),
        })
        data = await res.json()
        if (!res.ok) throw new Error(data.detail || 'Update failed')
        setWorkflows(prev => prev.map(w => w.id === editingId ? { ...w, ...data.workflow, action_count: wf.actions.length } : w))
        savedId = editingId
      } else {
        res  = await fetch(`${API}/api/builder/workflows`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body:    JSON.stringify(body),
        })
        data = await res.json()
        if (!res.ok) throw new Error(data.detail || 'Save failed')
        const saved = data.workflow
        setWorkflows(prev => [{ ...saved, action_count: wf.actions.length }, ...prev])
        setEditingId(saved.id)
        savedId = saved.id
      }
      setSaving(false)
      return savedId
    } catch (e) {
      setSaveError(e.message)
      setSaving(false)
      return null
    }
  }

  async function handleSaveAndRun() {
    const savedId = await handleSave()
    if (!savedId) return
    setShowRunInput(true)
  }

  async function handleRun(inputData = null, overrideId = null) {
    const id = overrideId || editingId
    if (!id) { setSaveError('Save the workflow first before running.'); return }
    // If no input data provided, show the input modal
    if (!inputData) { setShowRunInput(true); return }

    setShowRunInput(false)
    clearInterval(pollRef.current)
    setRun(null)
    setRunning(true)

    // Merge task description as fallback
    const payload = { task: wf.description || wf.name, ...inputData }

    try {
      const res  = await fetch(`${API}/api/builder/workflows/${id}/run`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body:    JSON.stringify({ input_data: payload }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Run failed')
      setRun({ run_id: data.run_id, status: 'queued', result: {}, error: '' })
      setRunning(false)
      pollRef.current = setInterval(async () => {
        try {
          const pr   = await fetch(`${API}/api/builder/runs/${data.run_id}`, { headers: { Authorization: `Bearer ${token}` } })
          const poll = await pr.json()
          setRun(poll)
          if (['completed', 'error', 'skipped'].includes(poll.status)) clearInterval(pollRef.current)
        } catch {}
      }, 2000)
    } catch (e) {
      setRunning(false)
      setRun({ status: 'error', error: e.message, result: {} })
    }
  }

  async function handleDelete(id) {
    try {
      await fetch(`${API}/api/builder/workflows/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } })
      setWorkflows(prev => prev.filter(w => w.id !== id))
      if (editingId === id) { setWf(BLANK_WF()); setEditingId(null) }
    } catch {}
    setDeleteId(null)
  }

  const triggerMeta = TRIGGER_TYPES.find(t => t.key === (wf.trigger?.type || 'manual')) || TRIGGER_TYPES[0]

  return (
    <div className="flex flex-col h-screen" style={{ background: '#0f172a', color: '#e2e8f0' }}>

      {/* ── Top bar ─────────────────────────────────────────────────────── */}
      <header className="border-b border-slate-700 flex items-center px-4 gap-3 shrink-0" style={{ minHeight: 52 }}>
        <button onClick={onBack} className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition-colors">
          <ArrowLeft size={15} />
        </button>
        <Zap size={15} className="text-purple-400" />
        <span className="font-semibold text-white text-sm">Workflow Builder</span>
        <span className="hidden sm:block text-xs text-slate-600">Trigger → Conditions → Actions</span>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-slate-500">{user?.username}</span>
          <button
            onClick={() => { setWf(BLANK_WF()); setEditingId(null); setSaveError('') }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-slate-700 hover:border-slate-500 text-slate-400 hover:text-white transition-colors"
          >
            <Plus size={12} /> New
          </button>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">

        {/* ── LEFT — saved workflows ─────────────────────────────────────── */}
        <aside className="w-56 shrink-0 border-r border-slate-700 flex flex-col overflow-hidden">
          <div className="px-3 py-2.5 border-b border-slate-700 shrink-0">
            <p className="text-[10px] font-bold uppercase tracking-widest text-slate-600">Saved ({workflows.length})</p>
          </div>
          <div className="flex-1 overflow-y-auto py-2 px-2 space-y-1">
            {loadingList
              ? <div className="flex justify-center py-10"><Loader2 size={14} className="text-slate-700 animate-spin" /></div>
              : workflows.length === 0
              ? <p className="text-xs text-slate-700 text-center py-8">No workflows yet</p>
              : workflows.map(w => {
                  const tMeta = TRIGGER_TYPES.find(t => t.key === w.trigger?.type) || TRIGGER_TYPES[0]
                  return (
                    <div
                      key={w.id}
                      onClick={() => handleLoadWorkflow(w)}
                      className={`group relative px-3 py-2.5 rounded-xl cursor-pointer transition-all border ${
                        editingId === w.id ? 'bg-purple-900/40 border-purple-700/50' : 'hover:bg-slate-800 border-transparent hover:border-slate-700'
                      }`}
                    >
                      <p className="text-xs font-semibold text-white truncate leading-snug">{w.name}</p>
                      <div className="flex items-center gap-1.5 mt-1">
                        <span className="text-sm">{tMeta.icon}</span>
                        <DomainPill domain={w.domain} />
                        <span className="text-[10px] text-slate-600">{w.action_count} actions</span>
                      </div>
                      <button
                        onClick={e => { e.stopPropagation(); setDeleteId(w.id) }}
                        className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 text-slate-600 hover:text-red-400 transition-all p-0.5"
                      >
                        <Trash2 size={11} />
                      </button>
                    </div>
                  )
                })
            }
          </div>
        </aside>

        {/* ── RIGHT — builder ────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-2xl mx-auto px-6 py-6 space-y-4">

            {/* ── Templates picker ─────────────────────────────────────── */}
            <div className="rounded-2xl border border-slate-700 bg-slate-800/30 overflow-hidden">
              <button
                onClick={() => setShowTemplates(v => !v)}
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-slate-800/50 transition-colors"
              >
                <div className="flex items-center gap-2.5">
                  <span className="text-base">📋</span>
                  <div>
                    <p className="text-sm font-bold text-white">Start from a Template</p>
                    <p className="text-[11px] text-slate-500">Pre-built Trigger → Conditions → Actions — click to load</p>
                  </div>
                </div>
                {showTemplates
                  ? <ChevronDown size={14} className="text-slate-500" />
                  : <ChevronRight size={14} className="text-slate-500" />
                }
              </button>

              {showTemplates && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 px-4 pb-4">
                  {TEMPLATES.map(t => (
                    <button
                      key={t.id}
                      onClick={() => {
                        setWf({ ...t.wf })
                        setEditingId(null)
                        setSaveError('')
                        setShowTemplates(false)
                      }}
                      className="flex items-start gap-3 p-3 rounded-xl bg-slate-800 border border-slate-700 hover:border-purple-600/50 hover:bg-purple-900/20 text-left transition-all group"
                    >
                      <span className="text-2xl mt-0.5 shrink-0">{t.icon}</span>
                      <div className="min-w-0">
                        <p className="text-xs font-bold text-white group-hover:text-purple-200 transition-colors">{t.label}</p>
                        <p className="text-[10px] text-slate-500 mt-0.5 leading-relaxed">{t.desc}</p>
                        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                          <DomainPill domain={t.wf.domain} />
                          <span className="text-[9px] text-slate-600">
                            {TRIGGER_TYPES.find(x => x.key === t.wf.trigger.type)?.icon} {t.wf.conditions.length} conditions · {t.wf.actions.length} actions
                          </span>
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Workflow name + domain */}
            <div className="flex gap-3">
              <input
                value={wf.name}
                onChange={e => setWf(p => ({ ...p, name: e.target.value }))}
                placeholder="Workflow name…"
                className="flex-1 bg-slate-800 border border-slate-700 rounded-xl px-4 py-2.5 text-sm font-semibold text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
              />
              <select
                value={wf.domain}
                onChange={e => setWf(p => ({ ...p, domain: e.target.value }))}
                className="bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs font-semibold text-slate-300 focus:outline-none focus:border-purple-500 shrink-0"
              >
                {DOMAINS.map(d => <option key={d.key} value={d.key}>{d.label}</option>)}
              </select>
            </div>

            <input
              value={wf.description}
              onChange={e => setWf(p => ({ ...p, description: e.target.value }))}
              placeholder="Description (optional — used as default task input when running)"
              className="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-purple-500"
            />

            {/* AI Generate bar */}
            <div className="flex items-center gap-2">
              {!showAiInput ? (
                <button
                  onClick={() => setShowAiInput(true)}
                  className="flex items-center gap-1.5 text-xs text-purple-400 hover:text-purple-300 font-semibold transition-colors"
                >
                  <Sparkles size={12} /> Fill with AI
                </button>
              ) : (
                <>
                  <input
                    autoFocus
                    value={aiDesc}
                    onChange={e => setAiDesc(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleAiGenerate()}
                    placeholder="Describe what this workflow should do…"
                    className="flex-1 bg-slate-800 border border-purple-600/60 rounded-xl px-3 py-2 text-xs text-white placeholder-slate-600 focus:outline-none"
                  />
                  <button
                    onClick={handleAiGenerate}
                    disabled={!aiDesc.trim() || generating}
                    className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-white text-xs font-semibold transition-colors shrink-0"
                  >
                    {generating ? <Loader2 size={11} className="animate-spin" /> : <Sparkles size={11} />}
                    {generating ? 'Generating…' : 'Generate'}
                  </button>
                  <button onClick={() => { setShowAiInput(false); setAiDesc('') }} className="text-slate-500 hover:text-white transition-colors"><X size={14} /></button>
                </>
              )}
            </div>

            {/* ── TRIGGER ─────────────────────────────────────────────────── */}
            <SectionCard
              icon={triggerMeta.icon}
              title="Trigger"
              subtitle="The event that starts this workflow"
              accent="border-purple-700/40"
            >
              <TriggerEditor
                trigger={wf.trigger}
                onChange={t => setWf(p => ({ ...p, trigger: t }))}
              />
            </SectionCard>

            {/* ── CONDITIONS ──────────────────────────────────────────────── */}
            <SectionCard
              icon="🔀"
              title="Conditions"
              subtitle="Run only if these rules pass (optional)"
              accent="border-blue-700/30"
            >
              <ConditionsEditor
                conditions={wf.conditions}
                onChange={c => setWf(p => ({ ...p, conditions: c }))}
              />
            </SectionCard>

            {/* ── ACTIONS ─────────────────────────────────────────────────── */}
            <SectionCard
              icon="▶"
              title="Actions"
              subtitle="Tasks to execute in order"
              accent="border-green-700/30"
            >
              <ActionsEditor
                actions={wf.actions}
                onChange={a => setWf(p => ({ ...p, actions: a }))}
              />
            </SectionCard>

            {/* Error */}
            {saveError && (
              <div className="flex items-center gap-2 px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-xl">
                <XCircle size={13} className="text-red-400 shrink-0" />
                <p className="text-xs text-red-400">{saveError}</p>
              </div>
            )}

            {/* ── Action bar ─────────────────────────────────────────────── */}
            <div className="flex items-center gap-2 pb-8">
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-1.5 px-4 py-2.5 rounded-xl border border-slate-600 hover:border-slate-400 text-sm text-slate-300 hover:text-white font-semibold transition-colors disabled:opacity-40"
              >
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
                {editingId ? 'Update' : 'Save'}
              </button>

              <button
                onClick={handleRun}
                disabled={!editingId || running}
                className="flex items-center gap-1.5 px-4 py-2.5 rounded-xl bg-green-600/20 hover:bg-green-600/30 border border-green-600/40 text-sm text-green-300 font-semibold transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                Run
              </button>

              <button
                onClick={handleSaveAndRun}
                disabled={saving || running}
                className="flex items-center gap-1.5 px-5 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-sm text-white font-semibold transition-colors disabled:opacity-40 shadow-lg shadow-purple-900/30"
              >
                {saving || running ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
                {editingId ? 'Save & Run' : 'Create & Run'}
              </button>

              {editingId && (
                <span className="ml-auto text-[10px] font-mono text-slate-700">{editingId}</span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Run results modal ──────────────────────────────────────────────── */}
      {run && <RunPanel run={run} onClose={() => { setRun(null); clearInterval(pollRef.current) }} />}

      {/* ── Run input modal ────────────────────────────────────────────────── */}
      {showRunInput && (
        <RunInputModal
          domain={wf.domain}
          onRun={inputData => handleRun(inputData)}
          onClose={() => setShowRunInput(false)}
        />
      )}

      {/* ── Delete confirm ─────────────────────────────────────────────────── */}
      {deleteId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-[#1e293b] border border-slate-700 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="font-bold text-white mb-2">Delete Workflow?</h3>
            <p className="text-sm text-slate-400 mb-5">This cannot be undone.</p>
            <div className="flex gap-3 justify-end">
              <button onClick={() => setDeleteId(null)} className="px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors">Cancel</button>
              <button onClick={() => handleDelete(deleteId)} className="px-4 py-2 rounded-xl bg-red-600 hover:bg-red-500 text-white text-sm font-semibold transition-colors">Delete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
