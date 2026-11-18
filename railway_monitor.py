#!/usr/bin/env python3
"""
railway_monitor.py - vigia o n8n hospedado no Railway e avisa no Telegram.

Roda de hora em hora pelo GitHub Actions e compara com a rodada anterior
(guardada em railway_state.json). Avisa quando:
  - o n8n não responde (o "Alerta de Erros" do próprio n8n não funciona se ele cair)
  - algum serviço do projeto (n8n, Postgres) não está rodando
  - algum volume (disco) passa de DISK_ALERT_PCT % de uso
  - a memória do n8n passa de MEM_ALERT_GB na última hora
E avisa também quando volta ao normal.

Secrets do GitHub:
  RAILWAY_TOKEN        token de conta/workspace do Railway (Account Settings → Tokens)
                       ou RAILWAY_PROJECT_TOKEN (Project Settings → Tokens, só deste projeto)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  FORCE_REPORT=1       manda o estado completo (rodar na mão)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "railway_state.json"
API = "https://backboard.railway.com/graphql/v2"
TIMEOUT = 30
BRT = timezone(timedelta(hours=-3))

PROJECT_ID = "9d0c342b-a2e3-4d06-8286-fa067aa5c451"          # awake-cat
ENVIRONMENT_ID = "139550f0-c024-4755-b80d-0245027f59fe"      # production
N8N_SERVICE_ID = "afa788d7-b1ee-4903-ada5-95f4422ce411"
N8N_HEALTH_URL = "https://n8n-production-2e39.up.railway.app/healthz"

DISK_ALERT_PCT = float(os.environ.get("DISK_ALERT_PCT", "70"))
MEM_ALERT_GB = float(os.environ.get("MEM_ALERT_GB", "4"))
RUNNING_STATUSES = {"SUCCESS", "SLEEPING"}


class RailwayError(Exception):
    pass


def headers() -> dict:
    tok = os.environ.get("RAILWAY_TOKEN", "").strip()
    ptok = os.environ.get("RAILWAY_PROJECT_TOKEN", "").strip()
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    elif ptok:
        h["Project-Access-Token"] = ptok
    else:
        raise SystemExit("Defina o secret RAILWAY_TOKEN (ou RAILWAY_PROJECT_TOKEN).")
    return h


def gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(API, json={"query": query, "variables": variables or {}},
                      headers=headers(), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise RailwayError(f"token sem permissão ({r.status_code})")
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RailwayError("; ".join(e.get("message", "?") for e in data["errors"])[:300])
    return data.get("data") or {}


def check_health() -> tuple[bool, str]:
    try:
        r = requests.get(N8N_HEALTH_URL, timeout=20)
        return r.status_code == 200, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, type(e).__name__


def fetch_services() -> dict[str, str]:
    q = """
    query($id: String!) {
      project(id: $id) {
        services { edges { node { name serviceInstances { edges { node {
          environmentId latestDeployment { status }
        } } } } } }
      }
    }"""
    data = gql(q, {"id": PROJECT_ID})
    out: dict[str, str] = {}
    for e in data["project"]["services"]["edges"]:
        n = e["node"]
        status = "SEM DEPLOY"
        for ie in n["serviceInstances"]["edges"]:
            inst = ie["node"]
            if inst.get("environmentId") == ENVIRONMENT_ID:
                status = (inst.get("latestDeployment") or {}).get("status") or "SEM DEPLOY"
        out[n["name"]] = status
    return out


def fetch_volumes() -> dict[str, dict]:
    q = """
    query($id: String!) {
      project(id: $id) {
        volumes { edges { node { name volumeInstances { edges { node {
          environmentId currentSizeMB sizeMB
        } } } } } }
      }
    }"""
    data = gql(q, {"id": PROJECT_ID})
    out: dict[str, dict] = {}
    for e in data["project"]["volumes"]["edges"]:
        n = e["node"]
        for ie in n["volumeInstances"]["edges"]:
            inst = ie["node"]
            if inst.get("environmentId") != ENVIRONMENT_ID:
                continue
            used = float(inst.get("currentSizeMB") or 0)
            size = float(inst.get("sizeMB") or 0)
            out[n["name"]] = {"used_mb": round(used), "size_mb": round(size),
                              "pct": round(100 * used / size, 1) if size else 0.0}
    return out


def fetch_n8n_memory_gb() -> float | None:
    q = """
    query($p: String!, $s: String!, $e: String!, $start: DateTime!) {
      metrics(projectId: $p, serviceId: $s, environmentId: $e, startDate: $start,
              measurements: [MEMORY_USAGE_GB], sampleRateSeconds: 300) {
        measurement values { ts value }
      }
    }"""
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    data = gql(q, {"p": PROJECT_ID, "s": N8N_SERVICE_ID, "e": ENVIRONMENT_ID, "start": start})
    vals = [v["value"] for m in data.get("metrics") or [] for v in m.get("values") or []]
    return round(max(vals), 2) if vals else None


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


def resolved_label(key: str) -> str:
    kind, _, name = key.partition(":")
    if kind == "health":
        return "n8n voltou a responder."
    if kind == "svc":
        return f"Serviço \"{name}\" voltou a rodar."
    if kind == "vol":
        return f"Disco \"{name}\" voltou para menos de {DISK_ALERT_PCT:.0f}% de uso."
    if kind == "mem":
        return f"Memória do n8n voltou para menos de {MEM_ALERT_GB} GB."
    return f"Resolvido: {key}"


def main() -> int:
    force = os.environ.get("FORCE_REPORT", "").strip() == "1"
    prev: dict = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    prev_problems: dict = prev.get("problems", {})

    problems: dict[str, str] = {}
    info: list[str] = []
    api_errors: list[str] = []

    ok, detail = check_health()
    info.append(f"n8n online: {'sim' if ok else 'NÃO'} ({detail})")
    if not ok:
        problems["health"] = f"🔴 n8n não responde ({detail}). Nenhum workflow está rodando.\n   {N8N_HEALTH_URL}"

    try:
        for name, status in fetch_services().items():
            info.append(f"Serviço {name}: {status}")
            if status not in RUNNING_STATUSES:
                problems[f"svc:{name}"] = (f"🔴 Serviço \"{name}\" não está rodando (status {status}). "
                                           "Abra o projeto no Railway e faça Redeploy.")
    except (RailwayError, requests.RequestException, KeyError, TypeError) as e:
        api_errors.append(f"serviços: {e}")

    try:
        for name, v in fetch_volumes().items():
            info.append(f"Disco {name}: {v['used_mb']} MB de {v['size_mb']} MB ({v['pct']}%)")
            if v["pct"] >= DISK_ALERT_PCT:
                problems[f"vol:{name}"] = (f"🟠 Disco \"{name}\" com {v['pct']}% de uso "
                                           f"({v['used_mb']} de {v['size_mb']} MB). "
                                           "Aumente o volume no Railway antes de encher.")
    except (RailwayError, requests.RequestException, KeyError, TypeError) as e:
        api_errors.append(f"volumes: {e}")

    try:
        mem = fetch_n8n_memory_gb()
        if mem is not None:
            info.append(f"Memória do n8n (pico 1h): {mem} GB")
            if mem >= MEM_ALERT_GB:
                problems["mem"] = (f"🟠 Memória do n8n chegou a {mem} GB na última hora "
                                   f"(limite de alerta {MEM_ALERT_GB} GB). Veja se algum workflow está pesado.")
    except (RailwayError, requests.RequestException, KeyError, TypeError) as e:
        api_errors.append(f"memória: {e}")

    STATE_FILE.write_text(json.dumps({"problems": problems, "checked": datetime.now(BRT).isoformat()},
                                     ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    stamp = datetime.now(BRT).strftime("%d/%m %H:%M")
    if force or not prev:
        lines = [f"🚂 Railway / n8n - estado atual ({stamp})", ""] + [f"• {i}" for i in info]
        if problems:
            lines += ["", "Problemas:"] + list(problems.values())
        if api_errors:
            lines += ["", "⚠️ Não consegui ler: " + " | ".join(api_errors)]
        if not prev:
            lines.append("\nA partir de agora aviso só quando algo der problema ou voltar ao normal.")
        send_telegram("\n".join(lines))
        return 0

    new = [msg for k, msg in problems.items() if k not in prev_problems]
    solved = [k for k in prev_problems if k not in problems]
    msg: list[str] = []
    if new:
        msg += [f"🚂 Railway / n8n - alerta ({stamp})", ""] + new
    if solved:
        msg += ([""] if msg else [f"🚂 Railway / n8n ({stamp})", ""]) + \
               [f"🟢 {resolved_label(k)}" for k in solved]
    if msg:
        send_telegram("\n".join(msg))
    else:
        print("Sem mudanças. " + " | ".join(info) + (f" | erros API: {api_errors}" if api_errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
