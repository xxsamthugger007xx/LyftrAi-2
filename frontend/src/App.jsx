import { useEffect, useRef, useState } from "react";
import Markdown from "./Markdown.jsx";

const API = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

const REQUEST_TIMEOUT_MS = 90_000; // covers a Render cold start (~50 s) plus a slow model call
const HEALTH_TIMEOUT_MS = 90_000;

const newSessionId = () =>
  "s-" + (crypto.randomUUID ? crypto.randomUUID().slice(0, 12) : Math.random().toString(36).slice(2, 14));

const PROMPTS = [
  ["Explain", "Explain how API key failover works"],
  ["Create", "Write a haiku about caching"],
  ["Compare", "Summarize the pros and cons of SQLite"],
  ["Ask", "What is the capital of France?"],
];

const Icon = {
  plus: <path d="M12 5v14M5 12h14" />,
  send: <path d="M5 12h14M13 6l6 6-6 6" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  alert: <><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16h.01" /></>,
  paw: <><circle cx="6.5" cy="10" r="1.8" /><circle cx="10.5" cy="6.2" r="1.8" /><circle cx="15" cy="6.8" r="1.8" /><circle cx="18" cy="11" r="1.8" /><path d="M12.2 11.2c-2.4 0-5 2.7-5 5 0 1.6 1.2 2.4 2.7 2.4 1 0 1.5-.4 2.3-.4s1.3.4 2.3.4c1.5 0 2.7-.8 2.7-2.4 0-2.3-2.6-5-5-5Z" /></>,
};
const Svg = ({ name, size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {Icon[name]}
  </svg>
);

function Metadata({ meta, clientMs }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span className={`tag ${cached ? "cache" : "live"}`} data-testid="meta-source">{meta.resolution_source}</span>
      <span className="kv" title="Model that answered"><em>Model</em><b data-testid="meta-model">{meta.model}</b></span>
      <span className="kv" title="Backend latency (server-measured, from the API response)"><em>Backend</em><b data-testid="meta-latency">{meta.latency_ms} ms</b></span>
      {clientMs != null && (
        <span className="kv" title="Client-observed round trip: network + any server wake-up + backend (diagnostic)">
          <em>Client</em><b data-testid="meta-client-latency">{clientMs} ms</b>
        </span>
      )}
      <span className="kv" title="Input tokens (this request only)"><em>In</em><b data-testid="meta-input-tokens">{meta.input_tokens}</b></span>
      <span className="kv" title="Output tokens (this request only)"><em>Out</em><b data-testid="meta-output-tokens">{meta.output_tokens}</b></span>
      {meta.key_id && (
        <span className="kv faint" title="API key that answered">
          <em>Key</em>
          <b>{meta.key_id}{meta.fallback_attempts > 0 ? ` · ${meta.fallback_attempts} failover${meta.fallback_attempts > 1 ? "s" : ""}` : ""}</b>
        </span>
      )}
    </div>
  );
}

function Pending({ service, retrying }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const t0 = Date.now();
    const id = setInterval(() => setSecs(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(id);
  }, []);
  // Only states the client can actually know: whether the service has been confirmed reachable, and elapsed time.
  const label = service === "ready" ? "Waiting for the model's response…" : "Connecting to AI service…";
  return (
    <div className="pending">
      <span className="dots"><i /><i /><i /></span>
      <span className="pending-text">
        {retrying ? "Retrying · " : ""}{label}
        {secs >= 3 && <span className="pending-secs"> {secs}s</span>}
      </span>
      {secs >= 12 && (
        <span className="pending-note">
          {service === "ready"
            ? "Taking longer than usual — the model is responding slowly."
            : "The free server may be waking up — this can take up to a minute."}
        </span>
      )}
    </div>
  );
}

const SERVICE_TEXT = {
  connecting: "Connecting to AI service…",
  ready: "AI service ready",
  unreachable: "AI service unreachable",
};

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId); // new session on every page load
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [sending, setSending] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [service, setService] = useState("connecting"); // connecting | ready | unreachable
  const endRef = useRef(null);
  const inputRef = useRef(null);
  const inFlight = useRef(false);   // synchronous guard against double submits
  const abortRef = useRef(null);
  const sessionRef = useRef(sessionId);

  // One lightweight health check on load (wakes a sleeping free-tier backend). Never polls, never calls the model.
  useEffect(() => {
    let alive = true;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
    fetch(`${API}/health`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("bad status"))))
      .then((d) => alive && setService(d && d.status === "ok" ? "ready" : "unreachable"))
      .catch(() => alive && setService("unreachable"))
      .finally(() => clearTimeout(timer));
    return () => {
      alive = false;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [input]);

  async function submit(text, { retry = false } = {}) {
    if (!text || inFlight.current) return; // exactly one request per submission
    inFlight.current = true;
    const sid = sessionRef.current;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, REQUEST_TIMEOUT_MS);

    if (!retry) setInput("");
    setSending(true);
    setRetrying(retry);
    setMessages((m) => (retry ? m.filter((x) => x.role !== "error") : [...m, { role: "user", content: text }]));

    const fail = (content) => setMessages((m) => [...m, { role: "error", content, retryText: text }]);
    const t0 = performance.now();
    try {
      const body = { session_id: sid, message: text };
      if (systemPrompt.trim()) body.system_prompt = systemPrompt.trim();
      const res = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      const data = await res.json().catch(() => null);
      const clientMs = Math.round(performance.now() - t0);
      if (sessionRef.current !== sid) return; // user started a new chat meanwhile: drop the late reply
      if (res.status === 422) {
        fail(`The message was rejected: ${typeof data?.detail === "string" ? data.detail : "invalid input"}.`);
      } else if (res.status === 503) {
        fail("The AI providers are temporarily unavailable. Your message was not saved — please retry in a moment.");
      } else if (!res.ok) {
        fail(`The server returned an error (HTTP ${res.status}${typeof data?.detail === "string" ? `: ${data.detail}` : ""}).`);
      } else if (!data || typeof data.reply !== "string" || !data.metadata || typeof data.metadata.resolution_source !== "string") {
        fail("The server sent a response in an unexpected format.");
      } else {
        setService("ready");
        setMessages((m) => [...m, { role: "assistant", content: data.reply, meta: data.metadata, clientMs }]);
      }
    } catch (err) {
      if (sessionRef.current !== sid) return;
      if (timedOut) fail(`No response after ${REQUEST_TIMEOUT_MS / 1000} seconds. The free server may be waking up or the model is very slow.`);
      else if (err?.name === "AbortError") fail("The request was cancelled.");
      else fail("Could not reach the server. Check your connection and try again.");
    } finally {
      clearTimeout(timer);
      if (abortRef.current === ctrl) {
        abortRef.current = null;
        inFlight.current = false;
        setSending(false);
        setRetrying(false);
        inputRef.current?.focus();
      }
    }
  }

  const send = (textOverride) => submit((textOverride ?? input).trim());
  const retryLast = (text) => submit(text, { retry: true });

  function newChat() {
    abortRef.current?.abort(); // cancel any pending reply so it cannot land in the new chat
    abortRef.current = null;
    inFlight.current = false;
    const sid = newSessionId();
    sessionRef.current = sid;
    setMessages([]);
    setSending(false);
    setRetrying(false);
    setSessionId(sid);
    setPanelOpen(false);
    inputRef.current?.focus();
  }

  const replies = messages.filter((m) => m.role === "assistant");
  const cacheHits = replies.filter((m) => m.meta.resolution_source === "LLM_CACHE").length;

  return (
    <div className="app">
      <aside className={`side ${panelOpen ? "open" : ""}`}>
        <div className="brand">
          <div className="mark"><Svg name="paw" size={20} /></div>
          <div>
            <h1>Lyftr Chat</h1>
            <p>AI assistant</p>
          </div>
          <button className="icon-btn side-close" onClick={() => setPanelOpen(false)} aria-label="Close panel"><Svg name="close" /></button>
        </div>

        <button className="new" onClick={newChat}><Svg name="plus" /> New conversation</button>

        <section className="panel">
          <h2>Service</h2>
          <div className={`service ${service}`} role="status" title="Backend health check (GET /health)">
            <i className="svc-dot" /> {SERVICE_TEXT[service]}
          </div>
        </section>

        <section className="panel">
          <h2>Session</h2>
          <code className="sid" title="Current session id">{sessionId}</code>
          <dl className="stats">
            <div><dt>Replies</dt><dd>{replies.length}</dd></div>
            <div><dt>From cache</dt><dd>{cacheHits}</dd></div>
          </dl>
        </section>

        <section className="panel grow">
          <label htmlFor="sp"><h2>System prompt</h2></label>
          <textarea
            id="sp"
            data-testid="system-prompt-input"
            rows={4}
            placeholder="Optional — e.g. You are a concise assistant that answers in one sentence."
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
          <p className="help">Applies to this conversation.</p>
        </section>

        <div className="side-foot">FastAPI · LiteLLM · Gemini · SQLite</div>
      </aside>
      {panelOpen && <div className="scrim" onClick={() => setPanelOpen(false)} />}

      <section className="main">
        <header className="mobilebar">
          <button className="icon-btn" onClick={() => setPanelOpen(true)} aria-label="Open panel"><Svg name="menu" size={20} /></button>
          <strong>Lyftr Chat</strong>
          <span className={`service mini ${service}`} role="status"><i className="svc-dot" /> {service === "ready" ? "Ready" : service === "connecting" ? "Connecting…" : "Unreachable"}</span>
        </header>

        <main className="thread" aria-live="polite">
          <div className="thread-inner">
            {messages.length === 0 && (
              <div className="welcome">
                <p className="eyebrow">New conversation</p>
                <h2>What can I help you with?</h2>
                <p className="lead">Every answer shows where it came from, which model produced it, how long it took and its token usage.</p>
                <ol className="prompts">
                  {PROMPTS.map(([tag, text]) => (
                    <li key={text}>
                      <button onClick={() => send(text)}><span className="ptag">{tag}</span><span>{text}</span><Svg name="send" size={15} /></button>
                    </li>
                  ))}
                </ol>
              </div>
            )}

            {messages.map((m, i) => {
              if (m.role === "user")
                return (
                  <article key={i} className="msg user">
                    <div className="who">You</div>
                    <div className="body plain">{m.content}</div>
                  </article>
                );
              if (m.role === "error")
                return (
                  <div key={i} className="notice" role="alert">
                    <Svg name="alert" size={18} />
                    <span>{m.content}</span>
                    {m.retryText && i === messages.length - 1 && (
                      <button className="retry" onClick={() => retryLast(m.retryText)} disabled={sending}>Retry</button>
                    )}
                  </div>
                );
              return (
                <article key={i} className="msg assistant" data-testid="assistant-message">
                  <div className="who"><span className="who-mark"><Svg name="paw" size={13} /></span>Assistant</div>
                  <div className="body">
                    <div className="text"><Markdown text={m.content} /></div>
                    <Metadata meta={m.meta} clientMs={m.clientMs} />
                  </div>
                </article>
              );
            })}

            {sending && (
              <article className="msg assistant">
                <div className="who"><span className="who-mark"><Svg name="paw" size={13} /></span>Assistant</div>
                <div className="body"><Pending service={service} retrying={retrying} /></div>
              </article>
            )}
            <div ref={endRef} />
          </div>
        </main>

        <footer className="composer-wrap">
          <div className="composer">
            <textarea
              ref={inputRef}
              data-testid="chat-input"
              rows={1}
              autoFocus
              placeholder="Write a message…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button className="send" data-testid="send-button" onClick={() => send()} disabled={sending || !input.trim()} aria-label="Send">
              Send <Svg name="send" size={16} />
            </button>
          </div>
          <p className="hint">Enter to send · Shift+Enter for a new line</p>
        </footer>
      </section>
    </div>
  );
}
