#!/usr/bin/env python3
"""
make_monitor.py - avisa no Telegram quando algum cenário do Make "cai".
Suporta várias contas do Make no mesmo relatório.

Roda de hora em hora pelo GitHub Actions e compara com a rodada anterior
(guardada em make_state.json). Avisa quando:
  - uma organização do Make fica pausada (ex.: acabaram as operações do plano)
  - um cenário que estava ligado foi desligado (o Make desliga sozinho após erros seguidos)
  - um cenário fica inválido (conexão expirada, módulo quebrado)
  - aumentam as execuções incompletas
  - um cenário ligado teve execuções com erro desde a última verificação
E também avisa quando algo volta ao normal.

Contas (secrets do GitHub):
  MAKE_TOKENS      uma conta por linha, no formato  Apelido|token
                   (o apelido é opcional; uma linha só com o token também vale)
  MAKE_API_TOKEN   (opcional) conta principal, usada com o apelido "Lemes"
Cada token precisa dos escopos: scenarios:read e organizations:read.
Organizações e zonas (us1, us2, eu1, eu2...) são descobertos sozinhos.

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
STATE_VERSION = 2
TIMEOUT = 30
ZONES = ["us2.make.com", "us1.make.com", "eu1.make.com", "eu2.make.com"]
BRT = timezone(timedelta(hours=-3))


class MakeError(Exception):
    pass


def api_get(zone: str, token: str, path: str, params: dict | None = None) -> dict:
    r = None
    for attempt in range(3):
        r = requests.get(f"https://{zone}/api/v2{path}", params=params, timeout=TIMEOUT,
                         headers={"Authorization": f"Token {token}"})
        if r.status_code == 429:  # plano Free: 30 req/min
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise MakeError(f"token sem permissão ({r.status_code}) em {path}")
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def read_accounts() -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    main_token = os.environ.get("MAKE_API_TOKEN", "").strip()
    if main_token:
        accounts.append(("Lemes", main_token))
    for i, line in enumerate(os.environ.get("MAKE_TOKENS", "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, tok = line.rpartition("|")
        accounts.append((name.strip() or f"Conta {i}", tok.strip()))
    seen, out = set(), []
    for name, tok in accounts:
        if tok and tok not in seen:
            seen.add(tok)
            out.append((name, tok))
    return out


def discover_orgs(token: str) -> list[dict]:
    """Lista as organizações do token (qualquer zona responde com todas)."""
    last_err: Exception | None = None
    for zone in ZONES:
        try:
            return api_get(zone, token, "/organizations").get("organizations", [])
        except (MakeError, requests.RequestException) as e:
            last_err = e
    raise MakeError(f"não consegui listar organizações: {last_err}")


def fetch_account(name: str, token: str) -> tuple[dict, dict]:
    """Retorna (orgs, scenarios) desta conta, com chaves globais."""
    orgs: dict[str, dict] = {}
    scenarios: dict[str, dict] = {}
    for o in discover_orgs(token):
        zone = o.get("zone") or ZONES[0]
        org_full = api_get(zone, token, f"/organizations/{o['id']}").get("organization", o)
        okey = f"{zone}:{o['id']}"
        orgs[okey] = {"account": name, "name": o.get("name", "?"), "zone": zone,
                      "paused": bool(org_full.get("isPaused")),
                      "plan": org_full.get("serviceName") or org_full.get("productName") or "?"}
        offset = 0
        while True:
            data = api_get(zone, token, "/scenarios",
                           {"organizationId": o["id"], "pg[limit]": 100, "pg[offset]": offset})
            items = data.get("scenarios", [])
            for s in items:
                scenarios[f"{zone}:{s['id']}"] = {
                    "account": name, "org": okey, "zone": zone, "team": s.get("teamId"), "id": s["id"],
                    "name": s.get("name", "?"),
                    "active": bool(s.get("isActive")),
                    "invalid": bool(s.get("isinvalid")),
                    "dlq": int(s.get("dlqCount") or 0),
                }
            if len(items) < 100:
                break
            offset += 100
    return orgs, scenarios


def scenario_url(s: dict) -> str:
    return f"https://{s['zone']}/{s['team']}/scenarios/{s['id']}/edit"


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
    accounts = read_accounts()
    if not accounts:
        raise SystemExit("Nenhuma conta: defina MAKE_API_TOKEN e/ou MAKE_TOKENS.")

    prev: dict = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    if prev.get("version") != STATE_VERSION:
        prev = {}
    prev_orgs: dict = prev.get("orgs", {})
    prev_sc: dict = prev.get("scenarios", {})
    prev_accounts: list = prev.get("accounts", [])
    since_ms = int(prev.get("last_check_ms") or now_ms - 3600_000)

    orgs: dict[str, dict] = {}
    scenarios: dict[str, dict] = {}
    down, up, warn, new_accounts = [], [], [], []
    failed_accounts: list[str] = []

    for name, token in accounts:
        try:
            o, s = fetch_account(name, token)
        except (MakeError, requests.RequestException) as e:
            failed_accounts.append(name)
            warn.append(f"⚠️ [{name}] não consegui consultar a conta: {short(e)}")
            # mantém o estado anterior dessa conta para não gerar falsos "caiu"
            orgs.update({k: v for k, v in prev_orgs.items() if v.get("account") == name})
            scenarios.update({k: v for k, v in prev_sc.items() if v.get("account") == name})
            continue
        orgs.update(o)
        scenarios.update(s)
        if prev and name not in prev_accounts:
            new_accounts.append(name)

    first_run = not prev
    tag = (lambda acc: f"[{acc}] ") if len(accounts) > 1 else (lambda acc: "")

    # 1) Organização pausada
    for k, o in orgs.items():
        p = prev_orgs.get(k)
        if p is None or o["account"] in failed_accounts:
            continue
        if o["paused"] and not p["paused"]:
            down.append(f"🔴 {tag(o['account'])}Organização \"{o['name']}\" PAUSADA (plano {o['plan']}). "
                        "Nenhum cenário dela roda até renovar operações/créditos.")
        elif not o["paused"] and p["paused"]:
            up.append(f"🟢 {tag(o['account'])}Organização \"{o['name']}\" voltou a ficar ativa.")

    # 2) Mudanças por cenário
    for k, s in scenarios.items():
        p = prev_sc.get(k)
        if p is None or s["account"] in failed_accounts:
            continue
        t, url = tag(s["account"]), scenario_url(s)
        if p["active"] and not s["active"]:
            down.append(f"🔴 {t}Caiu: {s['name']} (foi desligado — por erro ou manualmente)\n   {url}")
        elif not p["active"] and s["active"]:
            up.append(f"🟢 {t}Voltou: {s['name']}")
        if s["invalid"] and not p["invalid"]:
            down.append(f"🔴 {t}Inválido: {s['name']} (conexão/módulo com problema)\n   {url}")
        elif p["invalid"] and not s["invalid"]:
            up.append(f"🟢 {t}Corrigido: {s['name']} não está mais inválido")
        if s["dlq"] > p["dlq"]:
            warn.append(f"🟡 {t}{s['name']}: {s['dlq']} execuções incompletas (+{s['dlq'] - p['dlq']})\n   {url}")

    # 3) Execuções com erro (só cenários ligados, para respeitar o limite da API)
    tokens = dict(accounts)
    if not first_run:
        for k, s in scenarios.items():
            if not s["active"] or s["account"] in failed_accounts:
                continue
            try:
                data = api_get(s["zone"], tokens[s["account"]], f"/scenarios/{s['id']}/logs",
                               {"status": 3, "from": since_ms, "pg[limit]": 20, "pg[sortDir]": "desc"})
            except (MakeError, requests.RequestException) as e:
                print(f"Falha ao ler logs de {s['name']}: {e}")
                continue
            errs = data.get("scenarioLogs", data.get("logs", [])) or []
            if errs:
                last = errs[0].get("error") or {}
                mod = last.get("causeModule") or {}
                where = f" [{mod.get('appName')}:{mod.get('name')}]" if mod else ""
                warn.append(f"🟠 {tag(s['account'])}{s['name']}: {len(errs)} execução(ões) com erro "
                            f"desde a última checagem{where}\n   {short(last.get('message', 'sem mensagem'))}\n"
                            f"   {scenario_url(s)}")

    state = {"version": STATE_VERSION, "last_check_ms": now_ms,
             "accounts": [n for n, _ in accounts], "orgs": orgs, "scenarios": scenarios}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    stamp = datetime.now(BRT).strftime("%d/%m %H:%M")

    def summary(names: list[str]) -> list[str]:
        lines = []
        for acc in names:
            acc_orgs = [o for o in orgs.values() if o["account"] == acc]
            acc_sc = [s for s in scenarios.values() if s["account"] == acc]
            ativos = [s["name"] for s in acc_sc if s["active"]]
            lines.append(f"\n👤 {acc}")
            for o in acc_orgs:
                lines.append(f"  Org \"{o['name']}\": {'🔴 PAUSADA' if o['paused'] else '🟢 ativa'} (plano {o['plan']})")
            lines.append(f"  Cenários ligados: {len(ativos)} de {len(acc_sc)}")
            lines += [f"   • {n}" for n in ativos]
            inval = [s["name"] for s in acc_sc if s["invalid"]]
            if inval:
                lines.append("  Inválidos: " + ", ".join(inval))
            dlq = [f"{s['name']} ({s['dlq']})" for s in acc_sc if s["dlq"]]
            if dlq:
                lines.append("  Com execuções incompletas: " + ", ".join(dlq))
        return lines

    if first_run or force:
        ok = [n for n, _ in accounts if n not in failed_accounts]
        lines = [f"⚙️ Make - estado atual ({stamp})"] + summary(ok)
        lines += [""] + warn if failed_accounts else []
        if first_run:
            lines.append("\nA partir de agora aviso só quando algo cair ou voltar.")
        send_telegram("\n".join(lines))
        return 0

    msg: list[str] = []
    if down or warn or up:
        msg += [f"⚙️ Make - alerta ({stamp})", ""] + down + warn + up
    if new_accounts:
        msg += [f"\n➕ Conta(s) nova(s) no monitor: {', '.join(new_accounts)}"] + summary(new_accounts)
    if msg:
        send_telegram("\n".join(msg))
    else:
        print(f"Tudo certo: {len(accounts)} conta(s), "
              f"{sum(s['active'] for s in scenarios.values())} cenário(s) ligados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
