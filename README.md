# site-health

Verificação semanal de saúde/SEO dos sites: status, sitemap, title/description/og:image, links 404 e SSL.
Roda todo domingo às 12h (Brasília) via GitHub Actions e envia o resumo + relatório HTML no Telegram.

- Sites verificados: `sites.txt` (um por linha)
- Secrets do repositório: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Rodar na hora: aba **Actions → Saude dos sites → Run workflow**
- Relatórios antigos: em cada execução, seção **Artifacts** (guardados por 60 dias)
- Rodar local: `pip install -r requirements.txt` e `python site_health.py`
