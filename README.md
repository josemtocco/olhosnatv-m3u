# Olhos na TV → M3U para SS IPTV

Projeto para gerar automaticamente uma playlist M3U a partir dos canais publicados no [Olhos na TV](https://www.olhosnatv.com.br/).

## O que faz

- Descobre páginas de canais automaticamente a partir do site.
- Descobre as categorias publicadas no site e preserva os nomes delas.
- Extrai players/streams das páginas dos canais.
- Tenta resolver players incorporados recursivamente até encontrar URLs de mídia (`.m3u8`, `.mpd`, `.mp4`, etc.).
- Testa os streams encontrados.
- Gera `playlist.m3u` somente com canais atualmente utilizáveis.
- Remove canais que deixaram de estar ativos.
- Acrescenta canais novos automaticamente.
- Mantém o estado em `channels.json` para detectar entradas novas/removidas e evitar duplicatas.
- Executa automaticamente a cada 6 horas via GitHub Actions.
- Também pode ser executado manualmente.

> **Importante:** o projeto não hospeda nem redistribui os vídeos. A playlist aponta para as URLs de mídia encontradas nas páginas públicas da fonte. Verifique os direitos de uso e as condições da fonte antes de redistribuir a playlist.

## Estrutura

```text
.
├── .github/workflows/update.yml
├── config.json
├── channels.json
├── playlist.m3u
├── requirements.txt
├── scraper.py
├── test_scraper.py
└── README.md
```

## Uso local

Requer Python 3.11+.

```bash
python -m pip install -r requirements.txt
python scraper.py
```

Para validar o projeto:

```bash
python -m unittest -v
```

Para forçar uma execução mais detalhada:

```bash
python scraper.py --verbose
```

## GitHub Actions

O workflow roda a cada 6 horas e também pode ser iniciado em **Actions → Atualizar playlist → Run workflow**.

O horário do cron é UTC. O workflow usa `0 */6 * * *`.

A cada execução ele:

1. baixa as categorias;
2. percorre as páginas de canais;
3. extrai os players/streams;
4. valida os streams;
5. compara com `channels.json`;
6. remove inativos;
7. acrescenta novos;
8. reescreve `playlist.m3u`;
9. faz commit somente se houver alteração.

## URL para o SS IPTV

Depois que o repositório estiver publicado, use a URL **Raw** do arquivo `playlist.m3u`, por exemplo:

```text
https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPOSITORIO/main/playlist.m3u
```

Substitua `SEU_USUARIO/SEU_REPOSITORIO` pelos dados do seu repositório.

## Configuração

Edite `config.json` para alterar:

- URL da fonte;
- intervalo de requisições;
- timeout;
- número máximo de páginas;
- profundidade de resolução de iframes;
- extensões de mídia aceitas;
- User-Agent;
- comportamento de validação.

O scraper foi projetado para tolerar mudanças moderadas no HTML do Blogger. Se a fonte passar a depender exclusivamente de JavaScript ou alterar radicalmente o player, pode ser necessário adaptar `extract_media_urls()`.
