# LMR Edit — imagens para anúncios de autopeças

Ferramenta Streamlit para processar imagens de autopecas a partir de SKUs do BaseLinker,
com revisao visual, fila de trabalho e publicacao protegida.

## Fluxo

1. Informe de 5 a 10 SKUs na fila lateral (um SKU também pode ser processado sozinho).
2. Confirme ou ajuste a lista de veículos compatíveis, uma linha por marca, modelo e anos.
3. Para cada veículo, o sistema procura primeiro no catálogo local e depois na internet; a busca online confere o modelo, o ano e o fundo branco.
4. Aprove ou descarte cada imagem. A aba manual permite adicionar ou substituir uma imagem;
   você pode salvá-la no catálogo para reutilização.
5. O Pillow gera quatro JPGs em fundo branco:
	- `1.jpg`: os veículos aprovados na parte superior e a peça na parte inferior;
	- `2.jpg` a `4.jpg`: diferentes angulos da peca centralizada.
6. Revise as quatro imagens nas abas da tela.
7. Edite as imagens usando a prévia e os controles de posição e zoom.
8. Confirme a publicação e, no lote, avance para o próximo SKU ou pule o atual. Se necessário, use **Desfazer última publicação**.

Antes de publicar, o sistema salva um backup das imagens originais. As posicoes 5 a 16
tambem sao preservadas; somente as quatro primeiras sao substituidas.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Preencha `BASELINKER_TOKEN` e `BASELINKER_INVENTORY_ID` no `.env`.

`DOWNLOAD_CACHE_DIR` define o cache local das imagens baixadas.

## Veiculos compativeis

Para associar uma imagem diretamente a um SKU, edite `arquivos/mapa/vehicle_map.csv`:

```csv
sku,vehicle_key,vehicle_image_url
PEC-001,bmw_serie5_f10,arquivos/veiculos/bmw_serie5_f10.png
```

O caminho pode ser uma URL ou um arquivo local em `arquivos/veiculos/`.

Antes da busca, confirme a lista de veículos: há uma linha separada para cada marca, modelo e período de anos, e ela pode ser editada para incluir compatibilidades múltiplas. Para cada linha, o app consulta primeiro o catálogo local `vehicles.csv`; se não encontrar uma imagem aplicável, procura online no Wikimedia Commons, um ano por vez. A busca exige marca e modelo no nome da foto, descarta anos fora do intervalo e fotos de corrida, interior ou motor, e prefere fotos com fundo branco (se não houver, o fundo é removido automaticamente na capa). A imagem ainda precisa ser aprovada ou descartada. A aba manual permite enviar ou substituir uma imagem e, opcionalmente, salvá-la no catálogo para a próxima busca.

## Execucao

```bash
streamlit run app.py
```

The application is available on port `8501` by default. In GitHub Codespaces,
open the forwarded port URL shown by VS Code.

As imagens ficam em `arquivos/saida/<SKU>/`, os backups em `arquivos/backup/` e os
downloads temporarios em `arquivos/downloads/`. O envio so acontece apos confirmacao
explicita na tela.

## Estrutura de arquivos

```text
arquivos/
├── downloads/  # fotos baixadas do BaseLinker
├── mapa/       # associacao SKU -> veiculo
├── saida/      # imagens finais por SKU
└── veiculos/   # fotos dos veiculos compatíveis
```

## Hospedagem das imagens

O envio funciona localmente sem hospedagem: quando `PUBLIC_IMAGE_BASE_URL` estiver vazio, as imagens seguem em base64 na chamada da API (limite de 2 MB por arquivo). Se definir `PUBLIC_IMAGE_BASE_URL`, o sistema envia links e o diretorio de saida precisa estar publicado por um servidor ou storage com HTTPS.

`PUBLIC_IMAGE_BASE_URL` nao e o caminho local da pasta. E a URL base pela qual o
servidor publica `OUTPUT_DIR`. Por exemplo, se `OUTPUT_DIR=arquivos/saida` for
servido em `https://imagens.exemplo.com/saida`, configure:

```env
PUBLIC_IMAGE_BASE_URL=https://imagens.exemplo.com/saida
```

Para o SKU `00010278`, a imagem final sera publicada em uma URL como
`https://imagens.exemplo.com/saida/00010278/1.jpg`. O servidor precisa servir
essa pasta com HTTPS e permitir acesso sem login para o BaseLinker.

## Visual (LMR Edit)

- Tema escuro com azul neon em `.streamlit/config.toml`; brilho, cantos HUD e carregamento em `ui/style.css`.
- Fontes (Inter, Chakra Petch, JetBrains Mono, Orbitron) e ícones Tabler ficam em `static/`, servidos localmente (funciona offline).
- Logo e ícone: `python tools/make_brand.py` gera `static/branding/` (PNG e `.ico`).
- Depois de alterar arquivos em `ui/` ou `.streamlit/config.toml`, reinicie o `streamlit run` para garantir que carregou.

## Login, permissões e configurações

- No primeiro acesso deste computador, o app pede para criar o **administrador**.
- Os logins, as permissões e as configurações ficam em `dados/` (fora do git). O token do BaseLinker e as opções são
  salvos **criptografados**, e a chave fica em `dados/chave.key`. Senhas são guardadas só como hash (scrypt).
- Na tela de login, **Criar conta** gera um login sem permissões. O administrador libera o acesso em **Usuários**.
- O token, o catálogo e as estrelas são configurados na página **Configurações**. Elas valem mais que o `.env`.

## Instalar em outro computador

1. Neste computador (com o seu login de administrador já criado), rode `python tools/empacotar.py`.
   O zip sai em `dist/` com o código, o catálogo de veículos e os administradores (somente hash da senha).
   Ficam de fora o `.env`, o `dados/`, os downloads, as saídas e os backups.
2. No outro computador, extraia o zip e abra `instalar.bat` (instala o Python se precisar e cria o atalho
   **LMR Edit** na área de trabalho).
3. A pessoa abre o atalho e clica em **Criar conta**.
4. O administrador entra com o login dele nesse computador, libera as permissões em **Usuários** e coloca o token
   do BaseLinker em **Configurações**.
