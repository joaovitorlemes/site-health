#!/usr/bin/env python3
"""
make_monitor.py - avisa no Telegram quando algum cenário do Make "cai".

Roda de hora em hora pelo GitHub Actions e compara com a rodada anterior
(guardada em make_state.json). Avisa quando:
  - a conta/organização do Make fica pausada (ex.: acabaram as operações do plano)
  - um cenário que estava ligado foi desligado (o Make desliga sozinho após erros seguidos)
  - um cenário fica inválido (conexão expirada, módulo quebrado)
  - aumentam as execuções incompletas (fila de "incomplete executions")
  - um cenário ligado teve execuções com erro desde a última verificação
E também avisa quando algo volta ao normal.

Secrets / variáveis:
  MAKE_API_TOKEN   token da API do Make (escopos: scenarios:read, organizations:read)
  MAKE_ZONE        ex.: us2.make.com (padrão)
  MAKE_TEAM_ID     padrão 7654
  MAKE_ORG_ID      padrão 1845438
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  FORCE_REPORT=1   manda o estado completo (rodar na mão)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "make_state.json"
TIMEOUT = 30

ZONE = os.environ.get("MAKE_ZONE", "").strip() or "us2.make.com"
TEAM_ID = int(os.environ.get("MAKE_TEAM_ID", "").strip() or 7654)
ORG_ID = int(os.environ.get("MAKE_ORG_ID", "").strip() or 1845438)
API = f"https://{ZONE}/api/v2"
BRT = timezone(timedelta(hours=-3))


def api_get(path: str, params: dict | None = None) -> dict:
    token = os.environ.get("MAKE_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("MAKE_API_TOKEN não definido.")
    for attempt in range(3):
        r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT,
                         headers={"Authorization": f"Token {token}"})
        if r.status_code == 429:  # plano Free: 30 req/min
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def scenario_url(sid: int) -> str:
    return f"https://{ZONE}/{TEAM_ID}/scenarios/{sid}/edit"


def fetch_scenarios() -> dict[str, dict]:
    out: dict[str, dict] = {}
    offset = 0
    while True:
        data = api_get("/scenarios", {"teamId": TEAM_ID, "pg[limit]": 100, "pg[offset]": offset})
        items = data.get("scenarios", [])
        for s in items:
            out[str(s["id"])] = {
                "name": s.get("name", "?"),
                "active": bool(s.get("isActive")),
                "invalid": bool(s.get("isinvalid")),
                "dlq": int(s.get("dlqCount") or 0),
            }
        if len(items) < 100:
            return out
        offset += 100


def fetch_errors(sid: str, since_ms: int) -> list[dict]:
    data = api_get(f"/scenarios/{sid}/logs",
                   {"status": 3, "from": since_ms, "pg[limit]": 20, "pg[sortDir]": "desc"})
    return data.get("scenarioLogs", data.get("logs", [])) or []


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram: secrets não definidos, envio pulado.\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"},
                      timeout=TIMEOUT)
    print(f"Telegram: {r.status_code} {r.text[:200]}")


def short(msg: str, n: int = 160) -> str:
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= n else msg[: n - 1] + "…"


def main() -> int:
    force = os.environ.get("FORCE_REPORT", "").strip() == "1"
    now_ms = int(time.time() * 1000)

    prev: dict = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    first_run = not prev
    prev_sc: dict = prev.get("scenarios", {})
    since_ms = int(prev.get("last_check_ms") or now_ms - 3600_000)

    org = api_get(f"/organizations/{ORG_ID}").get("organization", {})
    org_paused = bool(org.get("isPaused"))
    plan = org.get("serviceName") or org.get("productName") or "?"
    scenarios = fetch_scenarios()

    down, up, warn = [], [], []

    # 1) Conta pausada
    if org_paused and not prev.get("org_paused"):
        down.append(f"🔴 A conta do Make está PAUSADA (plano {plan}). Nenhum cenário roda até "
                    "renovar as operações/créditos ou fazer upgrade.")
    elif not org_paused and prev.get("org_paused"):
        up.append("🟢 A conta do Make voltou a ficar ativa.")

    # 2) Mudanças por cenário
    for sid, s in scenarios.items():
        p = prev_sc.get(sid)
        name = s["name"]
        if p is None:
            continue  # cenário novo: só registra
        if p["active"] and not s["active"]:
            down.append(f"🔴 Caiu: {name} (foi desligado — por erro ou manualmente)\n   {scenario_url(int(sid))}")
        elif not p["active"] and s["active"]:
            up.append(f"🟢 Voltou: {name}")
        if s["invalid"] and not p["invalid"]:
            down.append(f"🔴 Inválido: {name} (conexão/módulo com problema)\n   {scenario_url(int(sid))}")
        elif p["invalid"] and not s["invalid"]:
            up.append(f"🟢 Corrigido: {name} não está mais inválido")
        if s["dlq"] > p["dlq"]:
            warn.append(f"🟡 {name}: {s['dlq']} execuções incompletas (+{s['dlq'] - p['dlq']})\n   {scenario_url(int(sid))}")

    # 3) Execuções com erro (só cenários ligados, para respeitar o limite da API)
    for sid, s in scenarios.items():
        if not s["active"] or first_run:
            continue
        try:
            errs = fetch_errors(sid, since_ms)
        except requests.RequestException as e:
            print(f"Falha ao ler logs de {s['name']}: {e}")
            continue
        if errs:
            last = errs[0].get("error") or {}
            mod = (last.get("causeModule") or {})
            where = f" [{mod.get('appName')}:{mod.get('name')}]" if mod else ""
            warn.append(f"🟠 {s['name']}: {len(errs)} execução(ões) com erro desde a última checagem{where}\n"
                        f"   {short(last.get('message', 'sem mensagem'))}\n   {scenario_url(int(sid))}")

    # Salva o estado novo
    state = {"last_check_ms": now_ms, "org_paused": org_paused, "scenarios": scenarios}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    stamp = datetime.now(BRT).strftime("%d/%m %H:%M")
    ativos = [s["name"] for s in scenarios.values() if s["active"]]

    if first_run or force:
        lines = [f"⚙️ Make - estado atual ({stamp})",
                 f"Conta: {'🔴 PAUSADA' if org_paused else '🟢 ativa'} (plano {plan})",
                 f"Cenários ligados: {len(ativos)} de {len(scenarios)}"]
        lines += [f"  • {n}" for n in ativos]
        inval = [s["name"] for s in scenarios.values() if s["invalid"]]
        if inval:
            lines.append("Inválidos: " + ", ".join(inval))
        dlq = [f"{s['name']} ({s['dlq']})" for s in scenarios.values() if s["dlq"]]
        if dlq:
            lines.append("Com execuções incompletas: " + ", ".join(dlq))
        if first_run:
            lines.append("\nA partir de agora aviso só quando algo cair ou voltar.")
        send_telegram("\n".join(lines))
        return 0

    if down or warn or up:
        msg = [f"⚙️ Make - alerta ({stamp})", ""] + down + warn + up
        send_telegram("\n".join(msg))
    else:
        print(f"Tudo certo: {len(ativos)} cenário(s) ligados, conta {'pausada' if org_paused else 'ativa'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
