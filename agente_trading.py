"""
Agente de trading com dinheiro FICTÍCIO (Alpaca paper trading + Claude).

As chaves ficam no ficheiro .env, na mesma pasta deste script:
    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ANTHROPIC_API_KEY=...

Correr:
    python3 agente_trading.py
"""

import os
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import anthropic
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# Carrega as chaves do .env que está ao lado do script (funciona também no cron)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------- Configuração ----------
MODELO = "claude-haiku-5-5"      # o mais barato; troca por claude-sonnet-5-5 se quiseres
LIMITE_POR_ORDEM = 0.10          # máximo de 10% da carteira por compra
MAX_PASSOS = 10                  # trava de segurança para o ciclo não ficar preso
ACOES_PERMITIDAS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "SPY"]

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]

# paper=True -> dinheiro fictício. NUNCA mudes isto sem saberes o que estás a fazer.
trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
dados = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
claude = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente


# ---------- Ferramentas (funções Python reais) ----------
def ver_carteira():
    conta = trading.get_account()
    posicoes = [
        {"simbolo": p.symbol, "qtd": p.qty, "valor": p.market_value,
         "lucro": p.unrealized_pl}
        for p in trading.get_all_positions()
    ]
    return {"dinheiro": conta.cash, "valor_total": conta.equity,
            "posicoes": posicoes}


def ver_preco(simbolo):
    req = StockLatestQuoteRequest(symbol_or_symbols=simbolo, feed=DataFeed.IEX)
    q = dados.get_stock_latest_quote(req)[simbolo]
    return {"simbolo": simbolo, "compra": q.ask_price, "venda": q.bid_price}


def ver_historico(simbolo, dias=5):
    """Preço de fecho de cada hora (com a bolsa aberta) nos últimos `dias` dias."""
    dias = min(int(dias), 30)                        # no máximo 30 dias
    inicio = datetime.now(timezone.utc) - timedelta(days=dias)
    req = StockBarsRequest(symbol_or_symbols=simbolo, timeframe=TimeFrame.Hour,
                           start=inicio, feed=DataFeed.IEX)
    barras = dados.get_stock_bars(req).data.get(simbolo, [])
    return [{"hora": b.timestamp.strftime("%d/%m %H:%M"), "fecho": b.close}
            for b in barras]


def comprar(simbolo, qtd):
    if simbolo not in ACOES_PERMITIDAS:
        return {"erro": f"{simbolo} não está na lista de ações permitidas."}
    preco = ver_preco(simbolo)["compra"]
    total = float(trading.get_account().equity)
    custo = preco * qtd
    if custo > total * LIMITE_POR_ORDEM:
        return {"erro": f"Recusado: {custo:.2f}$ ultrapassa {LIMITE_POR_ORDEM:.0%} da carteira."}
    ordem = trading.submit_order(MarketOrderRequest(
        symbol=simbolo, qty=qtd, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    return {"ok": True, "ordem": str(ordem.id), "estado": str(ordem.status)}


def vender(simbolo, qtd):
    # 1. Descobrir quantas ações deste símbolo temos
    tenho = 0
    for p in trading.get_all_positions():
        if p.symbol == simbolo:
            tenho = float(p.qty)

    # 2. Recusar se não chegar
    if qtd > tenho:
        return {"erro": f"Recusado: queres vender {qtd} {simbolo}, mas só tens {tenho}."}

    # 3. Se passou nas verificações, vende (isto já estava feito)
    ordem = trading.submit_order(MarketOrderRequest(
        symbol=simbolo, qty=qtd, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
    return {"ok": True, "ordem": str(ordem.id), "estado": str(ordem.status)}


FUNCOES = {"ver_carteira": ver_carteira, "ver_preco": ver_preco,
           "ver_historico": ver_historico,
           "comprar": comprar, "vender": vender}

# ---------- Descrição das ferramentas para o Claude ----------
TOOLS = [
    {"name": "ver_carteira",
     "description": "Mostra o dinheiro disponível, o valor total e as posições abertas.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ver_preco",
     "description": "Preço atual de uma ação (ex.: AAPL).",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"}},
                      "required": ["simbolo"]}},
    {"name": "ver_historico",
     "description": "Preços de fecho hora a hora de uma ação nos últimos N dias "
                    "(padrão 5, máximo 30). Serve para ver a tendência.",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "dias": {"type": "integer"}},
                      "required": ["simbolo"]}},
    {"name": "comprar",
     "description": "Compra uma quantidade inteira de ações.",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "qtd": {"type": "integer"}},
                      "required": ["simbolo", "qtd"]}},
    {"name": "vender",
     "description": "Vende uma quantidade de ações que já tens.",
     "input_schema": {"type": "object",
                      "properties": {"simbolo": {"type": "string"},
                                     "qtd": {"type": "integer"}},
                      "required": ["simbolo", "qtd"]}},
]

SYSTEM = f"""És um agente de trading a treinar com dinheiro fictício.
Só podes negociar estas ações: {", ".join(ACOES_PERMITIDAS)}.
Começa sempre por ver a carteira. Antes de decidir, usa ver_historico
para perceber a tendência de cada ação (está a subir, a descer ou estável?). Sê prudente: não gastes mais de
{LIMITE_POR_ORDEM:.0%} da carteira numa só ordem. No fim, explica em
português, em 3-4 frases, o que decidiste e porquê."""


# ---------- O ciclo do agente ----------
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
    print(f"\n##### {datetime.now():%Y-%m-%d %H:%M} #####")
    # Se a bolsa estiver fechada, sai logo (não gasta créditos do Claude)
    if not trading.get_clock().is_open:
        print("Bolsa fechada. Nada a fazer.")
        raise SystemExit
    correr_agente("Analisa a carteira e os preços atuais e decide se "
                  "compras, vendes ou não fazes nada.")
