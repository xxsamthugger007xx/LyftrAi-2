import { useEffect, useRef, useState } from "react";
import Markdown from "./Markdown.jsx";

const API = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

const REQUEST_TIMEOUT_MS = 90_000; // covers a Render cold start (~50 s) plus a slow model call
const HEALTH_TIMEOUT_MS = 90_000;

const newSessionId = () =>
  "s-" + (crypto.randomUUID ? crypto.randomUUID().slice(0, 12) : Math.random().toString(36).slice(2, 14));

const SUGGESTIONS = [
  "What is the capital of France?",
  "Explain how API key failover works",
  "Write a haiku about caching",
  "Summarize the pros and cons of SQLite",
];

const Icon = {
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  chip: <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" /></>,
  db: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>,
  send: <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z" />,
  plus: <path d="M12 5v14M5 12h14" />,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" /></>,
  alert: <><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16h.01" /></>,
};
const Svg = ({ name, size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {Icon[name]}
  </svg>
);

function Metadata({ meta, clientMs }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span className={`chip source ${cached ? "cache" : "live"}`} data-testid="meta-source">
        {meta.resolution_source}
      </span>
      <span className="chip" title="Model">
        <Svg name="chip" /> <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="chip" title="Backend latency (server-measured, from the API response)">
        <Svg name="clock" /> <span data-testid="meta-latency">{meta.latency_ms} ms</span>
      </span>
      {clientMs != null && (
        <span className="chip subtle" title="Client-observed round trip: network + any server wake-up + backend (diagnostic)">
          client <span data-testid="meta-client-latency">{clientMs} ms</span>
        </span>
      )}
      <span className="chip" title="Input tokens (this request)">
        in <b data-testid="meta-input-tokens">{meta.input_tokens}</b>
      </span>
      <span className="chip" title="Output tokens (this request)">
        out <b data-testid="meta-output-tokens">{meta.output_tokens}</b>
      </span>
      {meta.key_id && (
        <span className="chip subtle" title="API key that answered">
          {meta.key_id}
          {meta.fallback_attempts > 0 && ` · ${meta.fallback_attempts} failover${meta.fallback_attempts > 1 ? "s" : ""}`}
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
    <span className="pending">
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
    </span>
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
  const [service, setService] = useState("connecting"); // connecting | ready | unreachable
  const endRef = useRef(null);
  const inputRef = useRef(null);
  const inFlight = useRef(false);      // synchronous guard: state alone could let a fast double-submit through
  const abortRef = useRef(null);       // controller of the request currently in flight
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
    inputRef.current?.focus();
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="logo"><Svg name="bolt" size={18} /></div>
          <div>
            <h1>Lyftr Chat</h1>
            <p className="tagline">Resilient LLM gateway · key failover · response caching</p>
          </div>
        </div>
        <div className="actions">
          <span className={`service ${service}`} role="status" title="Backend health check (GET /health)">
            <i className="svc-dot" /> {SERVICE_TEXT[service]}
          </span>
          <span className="session" title="Current session id">
            <Svg name="db" /> {sessionId}
          </span>
          <button className="ghost" onClick={newChat}>
            <Svg name="plus" /> New chat
          </button>
        </div>
      </header>

      <section className="settings">
        <label htmlFor="sp"><Svg name="gear" size={12} /> System prompt</label>
        <input
          id="sp"
          data-testid="system-prompt-input"
          placeholder="Optional — e.g. You are a concise assistant that answers in one sentence."
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
      </section>

      <main className="thread" aria-live="polite">
        <div className="thread-inner">
          {messages.length === 0 && (
            <div className="hero">
              <div className="hero-logo"><Svg name="bolt" size={26} /></div>
              <h2>How can I help you today?</h2>
              <p>Every reply shows where it came from, which model answered, how long it took, and its token usage.</p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="suggestion" onClick={() => send(s)}>{s}</button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => {
            if (m.role === "user")
              return (
                <div key={i} className="row user">
                  <div className="bubble user">{m.content}</div>
                </div>
              );
            if (m.role === "error")
              return (
                <div key={i} className="row">
                  <div className="notice" role="alert">
                    <Svg name="alert" size={16} /> <span>{m.content}</span>
                    {m.retryText && i === messages.length - 1 && (
                      <button className="retry" onClick={() => retryLast(m.retryText)} disabled={sending}>
                        Retry
                      </button>
                    )}
                  </div>
                </div>
              );
            return (
              <div key={i} className="row">
                <div className="avatar"><Svg name="bolt" size={14} /></div>
                <div className="bubble assistant" data-testid="assistant-message">
                  <div className="text"><Markdown text={m.content} /></div>
                  <Metadata meta={m.meta} clientMs={m.clientMs} />
                </div>
              </div>
            );
          })}

          {sending && (
            <div className="row">
              <div className="avatar"><Svg name="bolt" size={14} /></div>
              <div className="bubble assistant"><Pending service={service} retrying={retrying} /></div>
            </div>
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
            placeholder="Message Lyftr Chat…"
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
            <Svg name="send" size={18} />
            <span>Send</span>
          </button>
        </div>
        <p className="hint">Enter to send · Shift+Enter for a new line</p>
      </footer>
    </div>
  );
}
