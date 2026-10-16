import { Plugin } from "@opencode/plugin"
import os from "node:os"

const ROLE_BY_IP: Record<string, { role: string; notes: string[] }> = {
  "192.168.1.6": {
    role: "MacBook «код» (.6)",
    notes: [
      "Проект видится через SMB-шару /Volumes/Dev/Deeptrading (это диск машины .7).",
      "НЕ ставить node_modules/venv на сетевой диск; npm install только на машине, где диск локальный.",
      "Локальный dev-PG14 (127.0.0.1:5432) — ПЕСОЧНИЦА, не источник данных.",
      "Код правится через шару → сразу попадает на .7; бэк на .7 с --reload, фронт HMR.",
    ],
  },
  "192.168.1.7": {
    role: "MacBook «сервер» (.7)",
    notes: [
      "Это ЛОКАЛЬНАЯ машина проекта: код, git, backend, frontend живут здесь напрямую (без шары).",
      "Боевая PostgreSQL 16 в Docker (deeptrading-postgres, 127.0.0.1:5432, БД deeptrading) — источник данных.",
      "Запуск бэка: backend && zsh dev.sh (venv + uvicorn --reload на :8000); фронт: frontend && npm run dev (:5173).",
    ],
  },
  "192.168.1.8": {
    role: "Windows-раннер (.8)",
    notes: [
      "Здесь крутится живой бот/таски (uvicorn_test, vitebot) для продакшн-проверок.",
      "SSH: nadts@192.168.1.8. Не убивать kill таск vitebot — респавнится.",
    ],
  },
}

const UNKNOWN = {
  role: "неизвестная машина (вне штатной инфраструктуры Deeptrading)",
  notes: [
    "IP не найден в таблице ролей (.6 код / .7 сервер / .8 Windows-runner).",
    "Проверь, с какой машины запущена сессия, прежде чем считать путь к проекту или источником данных локальным.",
  ],
}

function detectIP(): string {
  const ifaces = os.networkInterfaces()
  const candidates: string[] = []
  for (const list of Object.values(ifaces)) {
    for (const i of list ?? []) {
      if (i.family === "IPv4" && !i.internal) candidates.push(i.address)
    }
  }
  return candidates.find((ip) => ip.startsWith("192.168.1.")) ?? candidates[0] ?? "unknown"
}

export default Plugin.define({
  id: "machine-context",
  async setup(ctx) {
    const ip = detectIP()
    const hostname = os.hostname()
    const info = ROLE_BY_IP[ip] ?? UNKNOWN

    const block = [
      `[machine-context] СЕССИЯ ЗАПУЩЕНА НА МАШИНЕ: ${info.role}`,
      `IP: ${ip} | hostname: ${hostname}`,
      "",
      "Важно для этого проекта (Deeptrading):",
      ...info.notes.map((n) => `- ${n}`),
      "",
      "Инфраструктура (.6 код / .7 сервер / .8 Windows-runner) может менять IP после ребута роутера;",
      "если AGENTS.md противоречит этому блоку по роли текущей машины — доверяй роли, определённой здесь,",
      "и перепроверь доступность адресов перед обращением к БД/серверу.",
      "",
    ].join("\n")

    await ctx.session.hook("context", (event) => {
      event.system.push({ type: "text", text: block })
    })
  },
})
