#!/usr/bin/env python3
"""
site_health.py - verifica a saude dos sites listados em sites.txt.

Para cada site:
  1. Home: status HTTP e tempo de resposta
  2. Sitemap (sitemap_index.xml / sitemap.xml / wp-sitemap.xml / robots.txt): ate 50 URLs
  3. Cada URL: status, <title>, meta description, og:image, links internos quebrados (404)
  4. Certificado SSL: dias ate expirar

Gera relatorio_AAAA-MM-DD.html e telegram_AAAA-MM-DD.txt (resumo curto).

Uso:
    python site_health.py                # usa sites.txt
    python site_health.py -f outros.txt  # outro arquivo
    python site_health.py --max-urls 20
"""
from __future__ import annotations

import argparse
import os
import html
import socket
import ssl
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- config
TIMEOUT = 10
MAX_URLS = 50
MAX_LINKS_PER_SITE = 400      # limite de links internos checados por site
PAGE_WORKERS = 10
LINK_WORKERS = 20
SITE_WORKERS = 5
SLOW_HOME_SECONDS = 3.0
SSL_WARN_DAYS = 30
SSL_CRIT_DAYS = 7
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 SiteHealth/1.0"
)
SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4",
    ".mp3", ".css", ".js", ".ico", ".xml", ".avif", ".woff", ".woff2",
)
SKIP_PATH_PARTS = ("/wp-admin", "/wp-login", "/cart", "/carrinho", "/checkout",
                   "/my-account", "/minha-conta", "/feed", "/wp-json", "/xmlrpc")

_local = threading.local()


def session() -> requests.Session:
    """Uma Session por thread (requests.Session nao e thread-safe)."""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT,
                          "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _local.s = s
    return s


# ----------------------------------------------------------------- modelos
@dataclass
class PageResult:
    url: str
    status: int | None = None
    error: str | None = None
    title: str | None = None
    description: str | None = None
    og_image: str | None = None
    is_html: bool = False
    anchors: int = 0               # total de <a href> no HTML recebido
    spa_shell: bool = False        # HTML parece casca de SPA (div#root/#app sem links)
    internal_links: set[str] = field(default_factory=set)


@dataclass
class SiteResult:
    url: str
    home_status: int | None = None
    home_time: float | None = None
    home_error: str | None = None
    final_url: str | None = None
    ssl_days: int | None = None
    ssl_expires: str | None = None
    ssl_error: str | None = None
    sitemap_url: str | None = None
    pages: list[PageResult] = field(default_factory=list)
    broken_links: list[tuple[str, int | str, str]] = field(default_factory=list)  # (link, status, origem)
    links_checked: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)  # (nivel, texto)
    level: str = "green"

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or self.url


# ----------------------------------------------------------------- checagens
def check_ssl(hostname: str, port: int = 443) -> tuple[int | None, str | None, str | None]:
    """Retorna (dias_restantes, data_expiracao, erro)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
        not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (not_after - datetime.now(timezone.utc)).days
        return days, not_after.strftime("%d/%m/%Y"), None
    except ssl.SSLCertVerificationError as e:
        return None, None, f"Certificado invalido: {e.verify_message}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def fetch(url: str, method: str = "GET", **kw) -> requests.Response:
    return session().request(method, url, timeout=TIMEOUT, allow_redirects=True, **kw)


def _xml_locs(content: bytes) -> tuple[list[str], list[str]]:
    """Retorna (sitemaps_filhos, urls) de um XML de sitemap."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        # fallback tolerante (BOM, lixo antes do XML, etc.)
        soup = BeautifulSoup(content, "html.parser")
        children = [l.get_text(strip=True) for sm in soup.find_all("sitemap")
                    for l in sm.find_all("loc", recursive=False)]
        urls = [l.get_text(strip=True) for u in soup.find_all("url") for l in u.find_all("loc", recursive=False)]
        return children, urls
    def split(t: str) -> tuple[str, str]:
        ns, _, name = t.rpartition("}")
        return ns, name.lower()

    # So o <loc> filho direto de <url>/<sitemap> no mesmo namespace;
    # ignora <image:loc>, <video:loc> etc. que o Yoast/RankMath colocam dentro de <url>.
    root_ns, tag = split(root.tag)
    locs = []
    for entry in root:
        for child in entry:
            ns, name = split(child.tag)
            if name == "loc" and ns == root_ns and child.text:
                locs.append(child.text.strip())
                break
    return (locs, []) if tag == "sitemapindex" else ([], locs)


def discover_sitemap(base: str) -> list[str]:
    cands = [urljoin(base, p) for p in ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")]
    try:
        r = fetch(urljoin(base, "/robots.txt"))
        if r.ok:
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm = line.split(":", 1)[1].strip()
                    if sm and sm not in cands:
                        cands.insert(0, sm)
    except requests.RequestException:
        pass
    return cands


def collect_sitemap_urls(base: str, limit: int) -> tuple[str | None, list[str]]:
    for cand in discover_sitemap(base):
        try:
            r = fetch(cand)
        except requests.RequestException:
            continue
        if r.status_code != 200 or b"<" not in r.content[:500]:
            continue
        children, urls = _xml_locs(r.content)
        if not children and not urls:
            continue
        found: list[str] = list(dict.fromkeys(urls))
        queue = list(children)
        seen = {cand}
        while queue and len(found) < limit:
            child = queue.pop(0)
            if child in seen:
                continue
            seen.add(child)
            try:
                cr = fetch(child)
                if cr.status_code != 200:
                    continue
                sub_children, sub_urls = _xml_locs(cr.content)
                queue.extend(sub_children)
                for u in sub_urls:
                    if u not in found:
                        found.append(u)
            except requests.RequestException:
                continue
        return cand, found[:limit]
    return None, []


def same_site(url: str, host: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    host = host.lower()
    return h == host or h == f"www.{host}" or f"www.{h}" == host


def check_page(url: str, host: str) -> PageResult:
    pr = PageResult(url=url)
    try:
        r = fetch(url)
        pr.status = r.status_code
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "html" not in ctype:
            return pr
        pr.is_html = True
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.find_all("a", href=True)
        pr.anchors = len(anchors)
        pr.spa_shell = not anchors and bool(soup.find(id=["root", "app", "__next"]))
        if soup.title and soup.title.get_text(strip=True):
            pr.title = soup.title.get_text(strip=True)
        md = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
        if md and md.get("content", "").strip():
            pr.description = md["content"].strip()
        og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
        if og and og.get("content", "").strip():
            pr.og_image = og["content"].strip()
        for a in anchors:
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#", "whatsapp:")):
                continue
            link = urldefrag(urljoin(r.url, href))[0]
            if not link.startswith("http") or not same_site(link, host):
                continue
            path = urlparse(link).path.lower()
            if path.endswith(SKIP_EXT) or any(p in path for p in SKIP_PATH_PARTS):
                continue
            pr.internal_links.add(link)
    except requests.RequestException as e:
        pr.error = short_err(e)
    return pr


def check_link(url: str) -> int | str:
    try:
        r = fetch(url, "HEAD")
        if r.status_code in (403, 405, 501) or r.status_code >= 500:
            r = fetch(url, "GET", stream=True)
            r.close()
        return r.status_code
    except requests.RequestException as e:
        return short_err(e)


def short_err(e: Exception) -> str:
    if isinstance(e, requests.Timeout):
        return "timeout"
    if isinstance(e, requests.exceptions.SSLError):
        return "erro SSL"
    if isinstance(e, requests.ConnectionError):
        return "falha de conexao"
    return type(e).__name__


# ----------------------------------------------------------------- por site
def check_site(url: str, max_urls: int) -> SiteResult:
    url = url.rstrip("/")
    sr = SiteResult(url=url)
    host = sr.host

    # SSL e home em paralelo
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ssl = ex.submit(check_ssl, host) if url.startswith("https") else None
        try:
            t0 = time.perf_counter()
            r = fetch(url + "/")
            sr.home_time = time.perf_counter() - t0
            sr.home_status = r.status_code
            sr.final_url = r.url
        except requests.RequestException as e:
            sr.home_error = short_err(e)
        if f_ssl:
            sr.ssl_days, sr.ssl_expires, sr.ssl_error = f_ssl.result()

    if sr.home_status is None:
        evaluate(sr)
        return sr

    base = sr.final_url or url
    sr.sitemap_url, urls = collect_sitemap_urls(base, max_urls)
    if not urls:
        urls = [base]  # sem sitemap: ao menos checa a home

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        sr.pages = list(ex.map(lambda u: check_page(u, host), urls))

    # links internos (deduplicados) -> origem
    origin: dict[str, str] = {}
    page_urls = {p.url for p in sr.pages}
    for p in sr.pages:
        for l in p.internal_links:
            if l not in origin and l not in page_urls:
                origin[l] = p.url
    # paginas do sitemap ja foram checadas: reaproveita status
    for p in sr.pages:
        if p.status in (404, 410):
            sr.broken_links.append((p.url, p.status, "sitemap"))
    links = list(origin)[:MAX_LINKS_PER_SITE]
    sr.links_checked = len(links)
    with ThreadPoolExecutor(max_workers=LINK_WORKERS) as ex:
        for link, st in zip(links, ex.map(check_link, links)):
            if st in (404, 410):
                sr.broken_links.append((link, st, origin[link]))

    evaluate(sr)
    return sr


def evaluate(sr: SiteResult) -> None:
    P = sr.problems
    # home
    if sr.home_status is None:
        P.append(("red", f"Home fora do ar ({sr.home_error})"))
    elif sr.home_status >= 400:
        P.append(("red", f"Home retornou HTTP {sr.home_status}"))
    elif sr.home_time and sr.home_time > SLOW_HOME_SECONDS:
        P.append(("yellow", f"Home lenta: {sr.home_time:.1f}s"))
    # ssl
    if sr.url.startswith("https"):
        if sr.ssl_error:
            P.append(("red", f"SSL: {sr.ssl_error}"))
        elif sr.ssl_days is not None:
            if sr.ssl_days < 0:
                P.append(("red", f"SSL EXPIRADO em {sr.ssl_expires}"))
            elif sr.ssl_days <= SSL_CRIT_DAYS:
                P.append(("red", f"SSL expira em {sr.ssl_days} dias ({sr.ssl_expires})"))
            elif sr.ssl_days <= SSL_WARN_DAYS:
                P.append(("yellow", f"SSL expira em {sr.ssl_days} dias ({sr.ssl_expires})"))
    if sr.home_status is None:
        sr.level = "red"
        return
    # sitemap
    if not sr.sitemap_url:
        P.append(("yellow", "Sitemap nao encontrado"))
    # paginas
    bad = [p for p in sr.pages if p.error or (p.status and p.status >= 400)]
    ok_html = [p for p in sr.pages if p.is_html]
    if bad:
        lvl = "red" if len(bad) > max(1, len(sr.pages) * 0.2) else "yellow"
        P.append((lvl, f"{len(bad)} de {len(sr.pages)} paginas com erro"))
    for attr, label in (("title", "sem <title>"), ("description", "sem meta description"),
                        ("og_image", "sem og:image")):
        n = sum(1 for p in ok_html if not getattr(p, attr))
        if n:
            P.append(("yellow", f"{n} pagina(s) {label}"))
    # SPA (React/Vite etc.): o HTML servido nao tem links nem meta por pagina
    if ok_html and all(p.spa_shell for p in ok_html):
        P.append(("info", "Site SPA (React): HTML sem links, links internos nao verificados"))
    if len(ok_html) >= 3:
        titles = [p.title for p in ok_html if p.title]
        top = max(set(titles), key=titles.count) if titles else None
        if top and titles.count(top) > len(ok_html) * 0.5:
            P.append(("info", f"{titles.count(top)} paginas com o mesmo <title> no HTML inicial"))
    if sr.broken_links:
        lvl = "red" if len(sr.broken_links) >= 10 else "yellow"
        P.append((lvl, f"{len(sr.broken_links)} link(s) interno(s) quebrado(s) (404)"))
    levels = {lvl for lvl, _ in P}
    sr.level = "red" if "red" in levels else "yellow" if "yellow" in levels else "green"


# ----------------------------------------------------------------- saida
EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
LABEL = {"green": "OK", "yellow": "Atenção", "red": "Crítico"}
RANK = {"red": 0, "yellow": 1, "info": 2}


def telegram_text(results: list[SiteResult], date: str) -> str:
    lines = [f"🩺 Saude dos sites - {date}", ""]
    for sr in results:
        head = f"{EMOJI[sr.level]} {sr.host}"
        if sr.home_status is not None:
            head += f" - {sr.home_status} em {sr.home_time:.1f}s"
        if sr.ssl_days is not None:
            head += f" - SSL {sr.ssl_days}d"
        lines.append(head)
        for lvl, txt in sorted(sr.problems, key=lambda x: RANK[x[0]])[:4]:
            lines.append(f"   {'ℹ️' if lvl == 'info' else '•'} {txt}")
    counts = {k: sum(1 for r in results if r.level == k) for k in EMOJI}
    lines += ["", f"Total: 🟢 {counts['green']}  🟡 {counts['yellow']}  🔴 {counts['red']}"]
    return "\n".join(lines)


def render_html(results: list[SiteResult], date: str, elapsed: float) -> str:
    e = html.escape
    counts = {k: sum(1 for r in results if r.level == k) for k in EMOJI}
    cards, details = [], []
    for i, sr in enumerate(results):
        home = (f"HTTP {sr.home_status} · {sr.home_time:.2f}s" if sr.home_status is not None
                else f"Fora do ar ({e(sr.home_error or '')})")
        ssl_txt = (f"{sr.ssl_days} dias ({sr.ssl_expires})" if sr.ssl_days is not None
                   else e(sr.ssl_error or "n/a"))
        cards.append(f"""
<a class="card {sr.level}" href="#s{i}">
  <div class="card-top"><span class="dot"></span><span class="badge">{LABEL[sr.level]}</span></div>
  <h3>{e(sr.host)}</h3>
  <dl>
    <dt>Home</dt><dd>{home}</dd>
    <dt>SSL</dt><dd>{ssl_txt}</dd>
    <dt>Páginas</dt><dd>{len(sr.pages)}</dd>
    <dt>Links checados</dt><dd>{sr.links_checked}</dd>
    <dt>Problemas</dt><dd>{sum(1 for l, _ in sr.problems if l != "info")}</dd>
  </dl>
</a>""")
        probs = "".join(f'<li class="{l}">{e(t)}</li>' for l, t in
                        sorted(sr.problems, key=lambda x: RANK[x[0]])) or '<li class="green">Nenhum problema encontrado</li>'
        rows = []
        for p in sr.pages:
            st = p.error or p.status
            st_cls = "bad" if p.error or (p.status or 0) >= 400 else ""
            def mark(v):
                return '<span class="ok">✓</span>' if v else '<span class="no">✗</span>'
            rows.append(f'<tr><td class="url"><a href="{e(p.url)}" target="_blank">{e(p.url)}</a></td>'
                        f'<td class="{st_cls}">{e(str(st))}</td><td>{mark(p.title)}</td>'
                        f'<td>{mark(p.description)}</td><td>{mark(p.og_image)}</td></tr>')
        broken = "".join(
            f'<tr><td class="url"><a href="{e(l)}" target="_blank">{e(l)}</a></td><td class="bad">{e(str(s))}</td>'
            f'<td class="url">{e(o)}</td></tr>' for l, s, o in sr.broken_links)
        broken_html = (f'<h4>Links quebrados</h4><div class="tw"><table><thead><tr><th>Link</th><th>Status</th>'
                       f'<th>Encontrado em</th></tr></thead><tbody>{broken}</tbody></table></div>') if broken else ""
        details.append(f"""
<section id="s{i}" class="site {sr.level}">
  <h2><span class="dot"></span>{e(sr.host)}</h2>
  <p class="meta">Sitemap: {e(sr.sitemap_url or 'não encontrado')}</p>
  <ul class="problems">{probs}</ul>
  {broken_html}
  <details><summary>Páginas verificadas ({len(sr.pages)})</summary>
  <div class="tw"><table><thead><tr><th>URL</th><th>Status</th><th>Title</th><th>Descr.</th><th>og:image</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table></div></details>
</section>""")

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Saúde dos Sites {date}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--line:#e5e7eb;
--green:#16a34a;--yellow:#d97706;--red:#dc2626;--green-bg:#dcfce7;--yellow-bg:#fef3c7;--red-bg:#fee2e2;--info-bg:#eef2f7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#181b22;--text:#e6e8ec;--muted:#9aa1ad;--line:#2a2f3a;
--green-bg:#12351f;--yellow-bg:#3a2c0d;--red-bg:#3d1515;--info-bg:#20252f}}}}
*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 64px}}
header h1{{margin:0;font-size:26px}} header p{{color:var(--muted);margin:4px 0 0}}
.totals{{display:flex;gap:12px;margin:16px 0 24px;flex-wrap:wrap}}
.totals span{{padding:6px 12px;border-radius:999px;font-weight:600}}
.t-green{{background:var(--green-bg);color:var(--green)}} .t-yellow{{background:var(--yellow-bg);color:var(--yellow)}}
.t-red{{background:var(--red-bg);color:var(--red)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}}
.card{{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--line);
border-top:5px solid var(--c);border-radius:12px;padding:14px 16px}}
.card:hover{{box-shadow:0 4px 14px rgba(0,0,0,.08)}}
.card h3{{margin:6px 0 10px;font-size:16px;word-break:break-all}}
.card-top{{display:flex;align-items:center;gap:8px}}
.badge{{font-size:12px;font-weight:700;text-transform:uppercase;color:var(--c)}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0;font-size:13px}}
dt{{color:var(--muted)}} dd{{margin:0;text-align:right}}
.green{{--c:var(--green)}} .yellow{{--c:var(--yellow)}} .red{{--c:var(--red)}}
.dot{{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--c);margin-right:8px}}
.site{{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:12px;
padding:16px 20px;margin-top:20px}}
.site h2{{margin:0;font-size:19px;display:flex;align-items:center}}
.meta{{color:var(--muted);font-size:13px;margin:4px 0 10px;word-break:break-all}}
.problems{{list-style:none;padding:0;margin:0 0 10px}}
.problems li{{padding:6px 10px;border-radius:8px;margin:4px 0}}
.problems li.red{{background:var(--red-bg)}} .problems li.yellow{{background:var(--yellow-bg)}}
.problems li.green{{background:var(--green-bg)}}
.problems li.info{{background:var(--info-bg);color:var(--muted)}}
h4{{margin:14px 0 6px}}
summary{{cursor:pointer;color:var(--muted);margin-top:8px}}
.tw{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--muted);font-weight:600}}
td.url{{max-width:420px;word-break:break-all}} td a{{color:inherit}}
.ok{{color:var(--green);font-weight:700}} .no{{color:var(--red);font-weight:700}} .bad{{color:var(--red);font-weight:700}}
footer{{color:var(--muted);font-size:12px;margin-top:32px}}
</style></head><body><main>
<header><h1>🩺 Saúde dos Sites</h1>
<p>Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')} · {len(results)} sites · {elapsed:.0f}s de verificação</p></header>
<div class="totals"><span class="t-green">🟢 {counts['green']} OK</span>
<span class="t-yellow">🟡 {counts['yellow']} Atenção</span><span class="t-red">🔴 {counts['red']} Crítico</span></div>
<div class="grid">{''.join(cards)}</div>
{''.join(details)}
<footer>Critérios: vermelho = home fora/erro, SSL &lt;= {SSL_CRIT_DAYS} dias ou inválido, &gt;20% das páginas com erro ou
10+ links 404. Amarelo = home &gt; {SLOW_HOME_SECONDS:.0f}s, SSL &lt;= {SSL_WARN_DAYS} dias, sem sitemap, páginas sem
title/description/og:image ou algum link 404.</footer>
</main></body></html>"""


# ----------------------------------------------------------------- telegram
def send_telegram(text: str, html_path: Path) -> None:
    """Envia o resumo e o relatorio HTML se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID existirem."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nao definidos, envio pulado.")
        return
    api = f"https://api.telegram.org/bot{token}"
    try:
        r = requests.post(f"{api}/sendMessage", data={"chat_id": chat_id, "text": text[:4000],
                          "disable_web_page_preview": "true"}, timeout=20)
        print(f"Telegram sendMessage: {r.status_code} {r.text[:200]}")
        with open(html_path, "rb") as fh:
            r = requests.post(f"{api}/sendDocument", data={"chat_id": chat_id, "caption": "Relatorio completo"},
                              files={"document": (html_path.name, fh, "text/html")}, timeout=60)
        print(f"Telegram sendDocument: {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"Telegram: falha no envio: {e}")


# ----------------------------------------------------------------- main
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # emojis no console do Windows
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Verifica a saude dos sites.")
    ap.add_argument("-f", "--file", default="sites.txt")
    ap.add_argument("--max-urls", type=int, default=MAX_URLS)
    ap.add_argument("-o", "--outdir", default=".")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    sites_file = Path(args.file)
    if not sites_file.is_absolute() and not sites_file.exists():
        sites_file = here / sites_file
    sites = [l.strip() for l in sites_file.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    sites = [s if s.startswith("http") else "https://" + s for s in sites]

    t0 = time.perf_counter()
    print(f"Verificando {len(sites)} sites...")
    results: dict[str, SiteResult] = {}

    def run(u):
        try:
            res = check_site(u, args.max_urls)
        except Exception as e:  # noqa: BLE001  - nunca derruba o relatorio inteiro
            res = SiteResult(url=u, home_error=f"erro interno: {type(e).__name__}: {e}")
            res.problems.append(("red", res.home_error))
            res.level = "red"
        print(f"  {EMOJI[res.level]} {res.host} ({len(res.pages)} paginas, {res.links_checked} links)")
        return res

    with ThreadPoolExecutor(max_workers=SITE_WORKERS) as ex:
        for res in ex.map(run, sites):
            results[res.url] = res
    ordered = [results[s.rstrip("/")] for s in sites]
    elapsed = time.perf_counter() - t0

    date = datetime.now().strftime("%Y-%m-%d")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    html_path = outdir / f"relatorio_{date}.html"
    txt_path = outdir / f"telegram_{date}.txt"
    html_path.write_text(render_html(ordered, datetime.now().strftime("%d/%m/%Y"), elapsed), encoding="utf-8")
    tg = telegram_text(ordered, datetime.now().strftime("%d/%m/%Y"))
    txt_path.write_text(tg, encoding="utf-8")

    print(f"\nRelatorio: {html_path.resolve()}")
    print(f"Telegram:  {txt_path.resolve()}\n")
    print(tg)
    send_telegram(tg, html_path)
    return 1 if any(r.level == "red" for r in ordered) else 0


if __name__ == "__main__":
    sys.exit(main())
