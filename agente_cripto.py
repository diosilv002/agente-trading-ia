"""
Agente de trading de CRIPTO com dinheiro FICTÍCIO (Alpaca paper trading + Claude).
Usa a conta paper "Cripto Account" (chaves próprias no .env):
    ALPACA_CRIPTO_API_KEY=...
    ALPACA_CRIPTO_SECRET_KEY=...
    ANTHROPIC_API_KEY=...

Correr:
    venv/bin/python agente_cripto.py
"""

import os
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import anthropic
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# Carrega as chaves do .env que está ao lado do script (funciona também no cron)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------- Configuração ----------
MODELO = "claude-haiku-5-5"
LIMITE_POR_ORDEM = 0.10          # máximo de 10% da carteira por compra
MAX_PASSOS = 10
CRIPTOS_PERMITIDAS = ["BTC/USD", "ETH/USD", "SOL/USD"]   # cripto usa símbolos com "/"

ALPACA_KEY = os.environ["ALPACA_CRIPTO_API_KEY"]         # chaves da conta Cripto
ALPACA_SECRET = os.environ["ALPACA_CRIPTO_SECRET_KEY"]

trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
dados = CryptoHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)  # cliente de dados de cripto
claude = anthropic.Anthropic()


# ---------- Ferramentas ----------
def ver_carteira():
    conta = trading.get_account()
    posicoes = [
        {"simbolo": p.symbol, "qtd": p.qty, "valor": p.market_value,
         "lucro": p.unrealized_pl,
         "lucro_pct": round(float(p.unrealized_plpc) * 100, 2)}
        for p in trading.get_all_positions()
    ]
    return {"dinheiro": conta.cash, "valor_total": conta.equity,
            "posicoes": posicoes}


def ver_preco(simbolo):
    req = CryptoLatestQuoteRequest(symbol_or_symbols=simbolo)
    q = dados.get_crypto_latest_quote(req)[simbolo]
    return {"simbolo": simbolo, "compra": q.ask_price, "venda": q.bid_price}


def ver_historico(simbolo, horas=24):
    """Preço de fecho de cada hora nas últimas `horas` horas."""
    horas = min(int(horas), 168)                     # no máximo 1 semana
    inicio = datetime.now(timezone.utc) - timedelta(hours=horas)
    req = CryptoBarsRequest(symbol_or_symbols=simbolo,
                            timeframe=TimeFrame.Hour, start=inicio)
    barras = dados.get_crypto_bars(req).data.get(simbolo, [])
    return [{"hora": b.timestamp.strftime("%d/%m %H:%M"), "fecho": b.close}
            for b in barras]


def calcular_media(precos):
    """Média de uma lista de números: soma a dividir pela quantidade."""
    if len(precos) == 0:
        return 0
    return sum(precos) / len(precos)


def ver_sinais(simbolo):
    """Calcula os sinais da estratégia de médias móveis para uma cripto."""
    historico = ver_historico(simbolo, horas=24)
    fechos = [h["fecho"] for h in historico]          # só os preços
    if len(fechos) < 6:
        return {"erro": "Histórico insuficiente para calcular sinais."}

    preco = ver_preco(simbolo)["compra"]
    media_6h = calcular_media(fechos[-6:])           # últimas 6 horas
    media_24h = calcular_media(fechos)               # últimas 24 horas
    variacao_24h = (preco - fechos[0]) / fechos[0] * 100

    if preco > media_24h and media_6h > media_24h:
        sinal = "SUBIDA"
    elif preco < media_24h and media_6h < media_24h:
        sinal = "DESCIDA"
    else:
        sinal = "INDEFINIDO"

    return {"simbolo": simbolo, "preco": preco,
            "media_6h": round(media_6h, 4), "media_24h": round(media_24h, 4),
            "variacao_24h_pct": round(variacao_24h, 2), "sinal": sinal}


def comprar(simbolo, qtd):
    if simbolo not in CRIPTOS_PERMITIDAS:
        return {"erro": f"{simbolo} não está na lista de criptos permitidas."}
    preco = ver_preco(simbolo)["compra"]
    total = float(trading.get_account().equity)
    custo = preco * qtd
    if custo > total * LIMITE_POR_ORDEM:
        return {"erro": f"Recusado: {custo:.2f}$ ultrapassa {LIMITE_POR_ORDEM:.0%} da carteira."}
    # Cripto: quantidades fracionadas e ordens GTC (válidas até serem canceladas)
    ordem = trading.submit_order(MarketOrderRequest(
        symbol=simbolo, qty=qtd, side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
    return {"ok": True, "ordem": str(ordem.id), "estado": str(ordem.status)}


def vender(simbolo, qtd):
    # 1. Descobrir quantas ações deste símbolo temos
    tenho = 0
    for p in trading.get_all_positions():
        if p.symbol == simbolo.replace("/", ""):
            tenho = float(p.qty)

    # 2. Recusar se não chegar
    if qtd > tenho:
        return {"erro": f"Recusado: queres vender {qtd} {simbolo}, mas só tens {tenho}."}

    # 3. Se passou nas verificações, vende (isto já estava feito)
    ordem = trading.submit_order(MarketOrderRequest(
        symbol=simbolo, qty=qtd, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
    return {"ok": True, "ordem": str(ordem.id), "estado": str(ordem.status)}    

FUNCOES = {"ver_carteira": ver_carteira, "ver_preco": ver_preco,
           "ver_historico": ver_historico, "ver_sinais": ver_sinais,
           "comprar": comprar, "vender": vender}

TOOLS = [
    {"name": "ver_carteira",
     "description": "Mostra o dinheiro disponível, o valor total e as posições abertas.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ver_preco",
     "description": "Preço atual de uma cripto (ex.: BTC/USD).",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"}},
                      "required": ["simbolo"]}},
    {"name": "ver_historico",
     "description": "Preços de fecho hora a hora de uma cripto nas últimas N horas "
                    "(padrão 24, máximo 168). Serve para ver a tendência.",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "horas": {"type": "integer"}},
                      "required": ["simbolo"]}},
    {"name": "ver_sinais",
     "description": "Sinais da estratégia de médias móveis: preço atual, média das "
                    "últimas 6h e 24h, variação em 24h e o sinal (SUBIDA, DESCIDA "
                    "ou INDEFINIDO).",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"}},
                      "required": ["simbolo"]}},
    {"name": "comprar",
     "description": "Compra uma quantidade de cripto (aceita frações, ex.: 0.05).",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "qtd": {"type": "number"}},
                      "required": ["simbolo", "qtd"]}},
    {"name": "vender",
     "description": "Vende uma quantidade de cripto que já tens (aceita frações). "
                    "Usa o símbolo com '/', ex.: BTC/USD.",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "qtd": {"type": "number"}},
                      "required": ["simbolo", "qtd"]}},
]

SYSTEM = f"""És um agente de trading de criptomoedas a treinar com dinheiro fictício.
Só podes negociar: {", ".join(CRIPTOS_PERMITIDAS)}.
Segues uma ESTRATÉGIA DE MÉDIAS MÓVEIS com regras fixas. Não inventes outras regras.

PASSOS, sempre por esta ordem:
1. ver_carteira.
2. ver_sinais para cada cripto permitida.
3. Aplica as regras abaixo e executa as ordens.

REGRAS DE VENDA (para criptos que já tens):
- TAKE-PROFIT: se lucro_pct >= +5, vende a posição toda.
- STOP-LOSS: se lucro_pct <= -3, vende a posição toda.
- TENDÊNCIA: se o sinal for DESCIDA, vende a posição toda.

REGRAS DE COMPRA (só para criptos que ainda NÃO tens):
- Se o sinal for SUBIDA, compra o equivalente a 5% do valor_total da carteira.
  Quantidade = (valor_total x 0.05) / preço, arredondada a 4 casas decimais.
- Se o sinal for INDEFINIDO ou DESCIDA, não compres.

Nunca compres mais de uma vez a mesma cripto. Limite de segurança: {LIMITE_POR_ORDEM:.0%} por ordem.
Na carteira os símbolos aparecem sem '/' (ex.: BTCUSD), mas nas ordens usa sempre
com '/' (ex.: BTC/USD). Para vender a posição toda, usa a qtd exata da carteira.
No fim, explica em português, em 3-4 frases, que regra aplicaste a cada cripto."""


def correr_agente(pedido):
    msgs = [{"role": "user", "content": pedido}]
    for passo in range(MAX_PASSOS):
        r = claude.messages.create(model=MODELO, max_tokens=1024,
                                   system=SYSTEM, tools=TOOLS, messages=msgs)
        msgs.append({"role": "assistant", "content": r.content})

        if r.stop_reason != "tool_use":
            print("\n=== Decisão do agente ===")
            print("".join(b.text for b in r.content if b.type == "text"))
            return

        resultados = []
        for b in r.content:
            if b.type == "tool_use":
                print(f"[passo {passo + 1}] {b.name}({b.input})")
                try:
                    res = FUNCOES[b.name](**b.input)
                except Exception as e:
                    res = {"erro": str(e)}
                print(f"    -> {res}")
                resultados.append({"type": "tool_result", "tool_use_id": b.id,
                                   "content": json.dumps(res, default=str)})
        msgs.append({"role": "user", "content": resultados})

    print("Parei: atingi o limite de passos.")


if __name__ == "__main__":
    # Cripto negocia 24/7: não há verificação de "bolsa fechada"
    print(f"\n##### {datetime.now():%Y-%m-%d %H:%M} #####")
    correr_agente("Analisa a carteira e os preços atuais e decide se "
                  "compras, vendes ou não fazes nada.")
