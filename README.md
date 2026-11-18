# site-health

Verificação semanal de saúde/SEO dos sites: status, sitemap, title/description/og:image, links 404 e SSL.
Roda todo domingo às 12h (Brasília) via GitHub Actions e envia o resumo + relatório HTML no Telegram.

- Sites verificados: `sites.txt` (um por linha)
- Secrets do repositório: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Rodar na hora: aba **Actions → Saude dos sites → Run workflow**
- Relatórios antigos: em cada execução, seção **Artifacts** (guardados por 60 dias)
- Rodar local: `pip install -r requirements.txt` e `python site_health.py`

## Vencimentos (todo dia ~9h)

`vencimentos.py` consulta a data de expiração dos domínios (`dominios.txt`, via Registro.br/RDAP), a validade do SSL
e os itens manuais de `vencimentos.csv` (hospedagem, assinaturas, tokens). Só manda mensagem quando faltam
30 ou 15 dias, e todo dia quando faltam 7 dias ou menos.
Rodar na mão (**Actions → Vencimentos → Run workflow**) manda a lista completa.

## Monitor do Make (de hora em hora)

`make_monitor.py` consulta a API do Make (uma ou várias contas, zonas descobertas sozinhas) e avisa no Telegram quando:
a conta fica pausada (operações do plano acabaram), um cenário ligado é desligado, fica inválido,
ganha execuções incompletas ou tem execuções com erro. Também avisa quando volta ao normal.

- Secret extra: `MAKE_API_TOKEN` (Make → perfil → **API access** → Add token, escopos `scenarios:read` e `organizations:read`)
- Outras contas: secret `MAKE_TOKENS`, uma por linha no formato `Apelido|token` (ex.: `Cliente X|abc123...`)
- O estado da última checagem fica no cache do Actions (`make_state.json`), sem gerar commits
- Rodar na mão (**Actions → Monitor Make → Run workflow**) manda o estado completo
