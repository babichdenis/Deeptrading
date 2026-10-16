export function $(id: string): HTMLElement | null { return document.getElementById(id); }

export function setText(id: string, text: string): void { const el = $(id); if (el) el.textContent = text; }

export function pct(n: number): string { return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }

export function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
