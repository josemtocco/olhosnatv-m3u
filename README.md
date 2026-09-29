# Olhos na TV → M3U automática

Projeto para construir e atualizar uma playlist M3U a partir dos canais públicos listados em
[Olhos na TV](https://www.olhosnatv.com.br/).

A playlist final é gerada no diretório raiz:

```text
lista.m3u
```

## Como funciona

A cada execução o programa:

1. acessa a página principal;
2. encontra as páginas de canais;
3. visita cada página;
4. procura URLs de players/streams;
5. mantém somente entradas com URL utilizável;
6. remove duplicidades;
7. preserva canais descobertos anteriormente quando ainda estão disponíveis;
8. grava `lista.m3u` atomicamente.

O GitHub Actions executa o processo a cada 6 horas e, quando houver alteração, faz commit
da nova `lista.m3u`.

> Observação: o site pode alterar a estrutura HTML, os players ou os mecanismos de transmissão.
> O extrator foi feito para ser tolerante a diferentes formatos, mas nenhuma rotina de scraping
> consegue garantir compatibilidade permanente sem ajustes quando a fonte muda.

## Estrutura

```text
.
├── atualizar.py
├── canais.json
├── lista.m3u
├── requirements.txt
├── README.md
├── .gitignore
└── .github/
    └── workflows/
        └── atualizar.yml
```

## Rodar localmente

Requer Python 3.11+.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Instale:

```bash
pip install -r requirements.txt
```

Execute:

```bash
python atualizar.py
```

A saída será:

```text
lista.m3u
```

## Configuração

As variáveis de ambiente opcionais são:

```text
SOURCE_URL=https://www.olhosnatv.com.br/
OUTPUT_FILE=lista.m3u
STATE_FILE=canais.json
REQUEST_TIMEOUT=20
MAX_WORKERS=8
CHECK_STREAMS=false
```

Por padrão, `CHECK_STREAMS=false`, porque verificar cada stream com uma requisição HTTP pode
ser lento e alguns servidores bloqueiam HEAD/GET automáticos.

Para habilitar uma verificação simples:

```bash
CHECK_STREAMS=true python atualizar.py
```

## GitHub Actions

O workflow `.github/workflows/atualizar.yml` possui:

```yaml
schedule:
  - cron: "17 */6 * * *"
```

O `17` é proposital: evita rodar exatamente na virada da hora junto com muitos outros workflows.

O GitHub Actions usa UTC. Portanto, os horários locais podem variar conforme horário de verão.

Também é possível executar manualmente em:

**GitHub → Actions → Atualizar playlist M3U → Run workflow**

### Permissão necessária

Em:

**Settings → Actions → General → Workflow permissions**

deixe habilitada a opção para o workflow poder escrever no repositório
(`Read and write permissions`).

O workflow também declara:

```yaml
permissions:
  contents: write
```

## Publicar a playlist

Depois que o Actions fizer o primeiro commit, a playlist estará no endereço bruto do arquivo
do seu próprio repositório. No README do GitHub, você pode usar o endereço `raw.githubusercontent.com`
correspondente ao seu usuário, repositório e branch.

Exemplo de formato:

```text
https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPOSITORIO/main/lista.m3u
```

## Importante

Use somente canais e transmissões que você esteja autorizado a acessar e redistribuir. O projeto
não fornece conteúdo de TV por conta própria; ele apenas coleta referências públicas encontradas
na fonte configurada.

Se o site mudar sua estrutura, consulte os logs da Action para identificar qual etapa deixou de
encontrar os links.
