# Olhos na TV → M3U para SS IPTV

Versão corrigida do coletor automático.

## O que foi corrigido

- A descoberta dos canais agora usa o **feed Atom do Blogger**, em vez de depender da navegação das páginas de categorias.
- As categorias vêm das tags/labels de cada postagem.
- O coletor percorre várias páginas do feed para buscar todos os posts.
- Há 3 tentativas para cada requisição HTTP.
- O workflow **falha** quando o feed está vazio ou quando a coleta inteira falha.
- Uma falha transitória não apaga uma playlist válida.
- Canais antigos podem ser mantidos quando a página do canal falha temporariamente, desde que o stream antigo ainda responda.
- Canais sem stream válido são removidos.
- Canais novos são acrescentados.
- Um canal em várias categorias aparece em cada categoria no M3U.
- O workflow usa versões atuais das actions com Node.js 24 (`checkout@v5` e `setup-python@v6`).
- Atualização automática a cada 6 horas.

## Instalação

Substitua os arquivos do repositório por estes arquivos e faça commit/push.

Depois execute:

**GitHub → Actions → Atualizar playlist → Run workflow**

## Playlist para SS IPTV

Depois da primeira execução bem-sucedida:

`https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPOSITORIO/main/playlist.m3u`

No seu caso:

`https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/playlist.m3u`

## Diagnóstico

No log do Actions devem aparecer mensagens semelhantes a:

- `Lendo feed Blogger`
- `Entradas acumuladas: ...`
- `[1/... ] NOME DO CANAL`
- `RESULTADO: descobertos=... ativos=... falhas=... novos=... removidos=...`

Se aparecer `0 canais ativos`, o workflow será interrompido e a playlist anterior não será substituída.

## Fonte

https://www.olhosnatv.com.br/

O projeto somente referencia streams encontrados nas páginas públicas da fonte; não hospeda os vídeos.


## Formato específico para SS IPTV

A `playlist.m3u` é a playlist raiz: ela apresenta as categorias como itens do tipo `playlist`. Cada categoria é gravada como `categoria-*.m3u` na raiz do repositório. O SS IPTV abre a categoria e então mostra os canais.

No SS IPTV, adicione a URL pública da `playlist.m3u` em **Settings → Content → External Playlists**. A playlist usa URLs `raw.githubusercontent.com` para as categorias.

O `config.json` contém `github_raw_base`; se o repositório for renomeado ou transferido, altere esse valor para a URL RAW da nova branch.
