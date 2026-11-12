# Studio de imagens para anuncios

Ferramenta Streamlit para processar imagens de autopecas a partir de SKUs do BaseLinker,
com revisao visual, fila de trabalho e publicacao protegida.

## Fluxo

1. Informe um ou mais SKUs na fila lateral.
2. O sistema consulta o produto e baixa as imagens diretamente do BaseLinker.
3. O `rembg` remove o fundo das fotos da peca.
4. O Pillow gera quatro JPGs em fundo branco:
	- `1.jpg`: veiculo compativel na parte superior e peca na parte inferior;
	- `2.jpg` a `4.jpg`: diferentes angulos da peca centralizada.
5. Revise as quatro imagens nas abas da tela.
6. Edite as imagens usando a previa limpa e os controles de lado, altura e zoom. Na capa,
   carro e peca possuem controles independentes.
7. Marque a confirmacao, publique e, se necessario, use **Desfazer ultima publicacao**.

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

Edite `arquivos/mapa/vehicle_map.csv` e informe uma imagem com permissao comercial:

```csv
sku,vehicle_key,vehicle_image_url,vehicle_source,vehicle_license
PEC-001,bmw_serie5_f10,arquivos/veiculos/bmw_serie5_f10.png,Wikimedia Commons,CC BY-SA
```

O arquivo pode apontar para uma imagem hospedada na internet ou para um caminho local. Para manter o padrao, imagens locais de veiculos ficam em `arquivos/veiculos/`. A imagem do veiculo nao e pesquisada e baixada de forma indiscriminada: a fonte e a licenca precisam ser registradas para uso comercial.

A resolucao segue esta ordem: imagem cadastrada para o SKU em `vehicle_map.csv`,
imagem compativel cadastrada em `vehicles.csv` e, somente se nao houver nenhuma
das duas, busca no Wikimedia Commons com a especificacao de carro em fundo branco.

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

O BaseLinker precisa receber URLs acessiveis. O MVP aceita URLs publicas definidas em `PUBLIC_IMAGE_BASE_URL` e salva os arquivos no diretorio correspondente. Para producao, esse diretorio deve ser publicado por um servidor ou storage com HTTPS.

`PUBLIC_IMAGE_BASE_URL` nao e o caminho local da pasta. E a URL base pela qual o
servidor publica `OUTPUT_DIR`. Por exemplo, se `OUTPUT_DIR=arquivos/saida` for
servido em `https://imagens.exemplo.com/saida`, configure:

```env
PUBLIC_IMAGE_BASE_URL=https://imagens.exemplo.com/saida
```

Para o SKU `00010278`, a imagem final sera publicada em uma URL como
`https://imagens.exemplo.com/saida/00010278/1.jpg`. O servidor precisa servir
essa pasta com HTTPS e permitir acesso sem login para o BaseLinker.