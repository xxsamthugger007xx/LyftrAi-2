// Tiny safe renderer (no innerHTML): fenced code, inline code, **bold**, bullet lists, paragraphs.
function inline(text, keyBase) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("`") && p.endsWith("`") && p.length > 2) return <code key={keyBase + i}>{p.slice(1, -1)}</code>;
    if (p.startsWith("**") && p.endsWith("**") && p.length > 4) return <strong key={keyBase + i}>{p.slice(2, -2)}</strong>;
    return p;
  });
}

export default function Markdown({ text }) {
  const blocks = [];
  const segments = text.split(/```/);
  segments.forEach((seg, si) => {
    if (si % 2 === 1) {
      const code = seg.replace(/^[^\n]*\n/, "");
      blocks.push(<pre key={`c${si}`}><code>{code.replace(/\n$/, "")}</code></pre>);
      return;
    }
    let list = [];
    const flush = (k) => {
      if (list.length) blocks.push(<ul key={`u${si}-${k}`}>{list.map((l, i) => <li key={i}>{inline(l, `l${i}`)}</li>)}</ul>);
      list = [];
    };
    seg.split("\n").forEach((line, li) => {
      const m = line.match(/^\s*[-*]\s+(.*)/);
      if (m) {
        list.push(m[1]);
      } else {
        flush(li);
        if (line.trim()) blocks.push(<p key={`p${si}-${li}`}>{inline(line, `i${li}`)}</p>);
      }
    });
    flush("end");
  });
  return <>{blocks}</>;
}
