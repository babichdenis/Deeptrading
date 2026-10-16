"use strict";

(() => {
  const groups = Array.from(document.querySelectorAll("details.quick-group"));
  const field = (id) => document.getElementById(id);
  const checked = (id) => Boolean(field(id)?.checked);
  const activeNames = (prefix, choices) => choices.filter(([key]) => checked(`${prefix}-${key}`)).map(([,label]) => label);
  const sessionChoices = [["morning","Утро"],["day","День"],["evening","Вечер"]];
  const runtimeFields = field("quick-runtime-fields");
  const modes = field("mode-switch");
  const ai = document.querySelector(".hdr-ai-mode");
  if (modes && runtimeFields) {
    const label = document.createElement("div");
    label.className = "quick-field-label";
    label.textContent = "Режим счёта";
    runtimeFields.append(label, modes);
    modes.querySelector('[data-mode="sandbox"]')?.classList.add("active");
  }
  if (ai && runtimeFields) runtimeFields.append(ai);

  function summary(key, text) {
    const el = field(`quick-value-${key}`);
    if (!el) return;
    el.textContent = text;
    el.parentElement.title = `${el.parentElement.querySelector(".quick-label").textContent}: ${text}`;
  }

  function refresh() {
    const sessions = activeNames("sg",sessionChoices);
    summary("sessions", `${sessions.join(" + ") || "Нет сессий"}${checked("sg-overnight") ? " · ночь вкл" : " · ночь выкл"}${checked("rec-on") ? " · сверка вкл" : " · сверка выкл"}`);
    const directions = [checked("dg-long") ? "Long" : "",checked("dg-short") ? "Short" : ""].filter(Boolean);
    summary("direction", directions.join(" + ") || "Входы запрещены");
    const margin = activeNames("mg",sessionChoices);
    summary("margin", margin.length ? `${margin.join(" + ")} · ${field("mg-lev-label").textContent}` : "Без маржи");
    const regimes = [["neutral","Нейтр"],["trendup","Тренд↑"],["trenddown","Тренд↓"],["highvol","Волат"],["range","Флэт"]].filter(([id])=>checked(`rg-${id}`));
    summary("regimes", regimes.length===5 ? "Все 5 режимов" : regimes.length ? regimes.map(([,label])=>label).join(" · ") : "Входы запрещены");
    summary("sizing", `${field("sz-pos").value}% · ${field("sz-max").value} поз. · ×EQ ${field("sz-eq").value}`);
    const mode = modes?.querySelector(".mode-btn.active")?.dataset.mode || "sandbox";
    const aiMode = {bot:"Бот","bot+":"Бот+","bot++":"Бот++","bot+++":"Бот+++",ai:"AI-трейдер"}[field("ai-mode-select")?.value] || "AI не задан";
    summary("runtime", `${mode === "sandbox" ? "Sandbox" : mode === "live" ? "Live" : "Тест"} · ${aiMode}`);
  }

  function position(group) {
    const box = group.querySelector(".quick-popover");
    const rect = group.querySelector("summary").getBoundingClientRect();
    const width = Math.min(group.id === "quick-runtime" ? 440 : 540, window.innerWidth-32);
    box.style.width = `${width}px`;
    box.style.left = `${Math.max(16,Math.min(rect.left,window.innerWidth-width-16))}px`;
    const top = Math.min(rect.bottom+8,window.innerHeight-150);
    box.style.top = `${Math.max(16,top)}px`;
    box.style.maxHeight = `${Math.max(120,window.innerHeight-top-16)}px`;
  }

  groups.forEach((group)=>{
    group.addEventListener("toggle",()=>{
      if (!group.open) return;
      groups.filter((other)=>other!==group).forEach((other)=>other.open=false);
      position(group);
    });
  });
  document.addEventListener("pointerdown",(e)=>{
    if (!e.target.closest(".quick-group")) groups.forEach((group)=>group.open=false);
  });
  document.addEventListener("keydown",(e)=>{
    if (e.key!=="Escape") return;
    const open = groups.find((group)=>group.open);
    if (open) { open.open=false;open.querySelector("summary").focus(); }
  });
  document.addEventListener("change",refresh);
  document.addEventListener("input",refresh);
  document.addEventListener("click",(e)=>{if (e.target.closest("[data-mode]")) refresh();});
  window.addEventListener("resize",()=>groups.filter((group)=>group.open).forEach(position));
  refresh();
})();
