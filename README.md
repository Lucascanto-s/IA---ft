# Gerador de imagens para anuncios

MVP local para processar imagens de autopecas a partir de um SKU do BaseLinker.

## Fluxo

1. Informe o SKU no Streamlit.
2. O sistema consulta o produto e baixa as imagens diretamente do BaseLinker.
3. O `rembg` remove o fundo das fotos da peca.
4. O Pillow gera quatro JPGs em fundo branco:
	- `1.jpg`: veiculo compativel na parte superior e peca na parte inferior;
	- `2.jpg` a `4.jpg`: diferentes angulos da peca centralizada.
5. Revise as imagens na tela.
6. Edite as imagens usando a prévia à esquerda e os controles de lado, altura e zoom à direita. Na capa, carro e peca possuem controles independentes.
7. Depois da aprovacao, o sistema envia as URLs das imagens para o BaseLinker.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Preencha `BASELINKER_TOKEN` e `BASELINKER_INVENTORY_ID` no `.env`.

## Veiculos compativeis

Edite `arquivos/mapa/vehicle_map.csv` e informe uma imagem com permissao comercial:

```csv
sku,vehicle_key,vehicle_image_url,vehicle_source,vehicle_license
PEC-001,bmw_serie5_f10,arquivos/veiculos/bmw_serie5_f10.png,Wikimedia Commons,CC BY-SA
```

O arquivo pode apontar para uma imagem hospedada na internet ou para um caminho local. Para manter o padrao, imagens locais de veiculos ficam em `arquivos/veiculos/`. A imagem do veiculo nao e pesquisada e baixada de forma indiscriminada: a fonte e a licenca precisam ser registradas para uso comercial.

## Execucao

```bash
streamlit run app.py
```

As imagens ficam em `arquivos/saida/<SKU>/`. Downloads temporarios podem ser guardados em `arquivos/downloads/`. O envio para o BaseLinker so acontece ao clicar em **Aprovar e gravar**.

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