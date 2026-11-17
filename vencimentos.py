#!/usr/bin/env python3
"""
vencimentos.py - avisa no Telegram antes de domínios, SSL e contas vencerem.

Fontes:
  dominios.txt     um domínio por linha -> data de expiração consultada via RDAP
                   (Registro.br para .br, rdap.org para o resto) + validade do SSL
  vencimentos.csv  itens manuais (hospedagem, assinaturas, tokens): nome,data,obs
                   data no formato AAAA-MM-DD

Regra de aviso (para não virar spam):
  - faltando 30 ou 15 dias: um aviso
  - faltando 7 dias ou menos (ou já vencido): aviso todo dia
  - rodando manualmente (FORCE_REPORT=1): manda a lista completa
"""
from __future__ import annotations

import csv
import os
import socket
import ssl
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ALERT_DAYS = {30, 15}
DAILY_FROM = 7
TIMEOUT = 20


def rdap_expiration(domain: str) -> tuple[date | None, str | None]:
    url = (f"https://rdap.registro.br/domain/{domain}" if domain.endswith(".br")
           else f"https://rdap.org/domain/{domain}")
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/rdap+json"})
        if r.status_code == 404:
            return None, "domínio não encontrado no RDAP"
        r.raise_for_status()
        for ev in r.json().get("events", []):
            if ev.get("eventAction") == "expiration":
                return datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00")).date(), None
        return None, "RDAP sem data de expiração"
    except Exception as e:  # noqa: BLE001
        return None, f"falha no RDAP: {type(e).__name__}"


def ssl_expiration(host: str) -> tuple[date | None, str | None]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return exp.date(), None
    except Exception as e:  # noqa: BLE001
        return None, f"falha no SSL: {type(e).__name__}"


def read_lines(name: str) -> list[str]:
    p = HERE / name
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def read_manual() -> tuple[list[tuple[str, date, str]], list[str]]:
    items, errors = [], []
    p = HERE / "vencimentos.csv"
    if not p.exists():
        return items, errors
    rows = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    for row in csv.DictReader(rows):
        nome = (row.get("nome") or "").strip()
        try:
            d = date.fromisoformat((row.get("data") or "").strip())
            items.append((nome, d, (row.get("obs") or "").strip()))
        except ValueError:
            errors.append(f"data inválida em vencimentos.csv: {nome or row}")
    return items, errors


def should_alert(days: int) -> bool:
    return days <= DAILY_FROM or days in ALERT_DAYS


def emoji(days: int) -> str:
    return "⛔" if days < 0 else "🔴" if days <= DAILY_FROM else "🟡" if days <= 30 else "🟢"


def fmt(label: str, exp: date, days: int, obs: str = "") -> str:
    when = f"venceu há {-days}d" if days < 0 else "vence HOJE" if days == 0 else f"vence em {days}d"
    extra = f" ({obs})" if obs else ""
    return f"{emoji(days)} {label}: {when} - {exp.strftime('%d/%m/%Y')}{extra}"


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram: secrets não definidos, envio pulado.")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"},
                      timeout=TIMEOUT)
    print(f"Telegram: {r.status_code} {r.text[:200]}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    force = os.environ.get("FORCE_REPORT", "").strip() in {"1", "true", "yes"}
    today = date.today()
    entries: list[tuple[int, str]] = []   # (dias, linha)
    problems: list[str] = []

    for domain in read_lines("dominios.txt"):
        domain = domain.lower().removeprefix("https://").removeprefix("http://").strip("/")
        exp, err = rdap_expiration(domain)
        if exp:
            entries.append(((exp - today).days, fmt(f"Domínio {domain}", exp, (exp - today).days)))
        else:
            problems.append(f"⚠️ Domínio {domain}: {err}")
        sexp, serr = ssl_expiration(domain)
        if sexp:
            entries.append(((sexp - today).days, fmt(f"SSL {domain}", sexp, (sexp - today).days)))
        else:
            problems.append(f"⚠️ SSL {domain}: {serr}")

    manual, errs = read_manual()
    problems += [f"⚠️ {e}" for e in errs]
    for nome, d, obs in manual:
        entries.append(((d - today).days, fmt(nome, d, (d - today).days, obs)))

    entries.sort()
    for _, line in entries:
        print(line)
    for p in problems:
        print(p)

    alerts = [line for days, line in entries if should_alert(days)]
    if force:
        body = [l for _, l in entries] + problems
        send_telegram(f"📅 Vencimentos - lista completa ({today.strftime('%d/%m/%Y')})\n\n" + "\n".join(body))
    elif alerts:
        send_telegram("📅 Atenção, vencimentos próximos:\n\n" + "\n".join(alerts)
                      + "\n\nRenove antes do prazo para não perder o site.")
    else:
        print("Nada vencendo nos próximos dias. Nenhuma mensagem enviada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
