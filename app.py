from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from binance.client import Client
import websocket
import json
import time
import os
from datetime import datetime
import math
import threading
import requests
import logging
from typing import List, Dict, Tuple, Optional

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'whale_detector_secret_production_2024')

# Configurar SocketIO para produção
socketio = SocketIO(
    app, 
    async_mode='threading',
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
    max_http_buffer_size=1e8
)

# Inicializar cliente Binance
try:
    client = Client()
    logger.info("✅ Cliente Binance inicializado com sucesso")
except Exception as e:
    logger.error(f"❌ Erro ao inicializar cliente Binance: {e}")
    client = None

# Dicionários para armazenar dados em tempo real
live_klines = {}  # Lista de candles fechados por símbolo
current_klines = {}  # Dados do candle atual (aberto) por símbolo
volume_history = {}  # Últimos 20 volumes de candles fechados por símbolo
last_closed_close = {}  # Último preço de close de candle fechado por símbolo
alerted_this_candle = {}  # Flag para evitar múltiplos alertas no mesmo candle
live_tickers = {}
active_websockets = []

# Dicionários para controle de volume
volume_alert_counters = {}  # Contador de alertas por símbolo

def get_trading_symbols() -> List[str]:
    """Obtém TODOS os símbolos de trading USDT disponíveis da API Binance"""
    all_symbols = []
    
    if not client:
        logger.error("❌ Cliente Binance não disponível")
        return []
    
    try:
        logger.info("🔧 Obtendo símbolos da API Binance...")
        exchange_info = client.get_exchange_info()
        
        if not exchange_info or 'symbols' not in exchange_info:
            logger.error("❌ Resposta inválida da API Binance")
            return []
        
        logger.info(f"📦 Total de símbolos na Binance: {len(exchange_info['symbols'])}")
        
        # Filtrar símbolos USDT em trading
        for symbol_info in exchange_info['symbols']:
            try:
                quote_asset = symbol_info.get('quoteAsset', '')
                status = symbol_info.get('status', '')
                symbol = symbol_info.get('symbol', '')
                
                # Filtro: apenas USDT, em TRADING, excluindo alavancados
                if (quote_asset == 'USDT' and 
                    status == 'TRADING' and
                    not symbol.endswith(('UPUSDT', 'DOWNUSDT', 'BULLUSDT', 'BEARUSDT'))):
                    all_symbols.append(symbol)
                    
            except Exception as e:
                logger.debug(f"Erro ao processar símbolo {symbol_info.get('symbol', 'UNKNOWN')}: {e}")
                continue
        
        logger.info(f"✅ API Binance: {len(all_symbols)} símbolos USDT encontrados")
        
    except Exception as e:
        logger.error(f"❌ Erro ao obter símbolos da API Binance: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []
    
    logger.info(f"🎯 Total de {len(all_symbols)} símbolos USDT carregados para monitoramento")
    return all_symbols

# Configurações de timeframe
TIMEFRAME_CONFIG = {
    '1m': {'interval': 15, 'limit': 100, 'name': '1 Minuto'},
    '3m': {'interval': 20, 'limit': 100, 'name': '3 Minutos'},
    '5m': {'interval': 25, 'limit': 100, 'name': '5 Minutos'},
    '15m': {'interval': 30, 'limit': 100, 'name': '15 Minutos'},
    '30m': {'interval': 40, 'limit': 100, 'name': '30 Minutos'},
    '1h': {'interval': 30, 'limit': 100, 'name': '1 Hora'},
    '2h': {'interval': 70, 'limit': 100, 'name': '2 Horas'},
    '4h': {'interval': 30, 'limit': 100, 'name': '4 Horas'},
    '6h': {'interval': 130, 'limit': 100, 'name': '6 Horas'},
    '8h': {'interval': 160, 'limit': 100, 'name': '8 Horas'},
    '12h': {'interval': 200, 'limit': 100, 'name': '12 Horas'},
    '1d': {'interval': 30, 'limit': 100, 'name': '1 Dia'}
}

# Configurações de EMA
EMA_CONFIGS = {
    'ema_7_21': {'fast': 7, 'slow': 21, 'name': 'EMA 7x21 (Curto Prazo)'},
    'ema_12_26': {'fast': 12, 'slow': 26, 'name': 'EMA 12x26 (MACD Base)'},
    'ema_20_50': {'fast': 20, 'slow': 50, 'name': 'EMA 20x50 (Swing Trade)'},
    'ema_50_200': {'fast': 50, 'slow': 200, 'name': 'EMA 50x200 (Golden Cross)'}
}

# Configurações de RSI
RSI_CONFIG = {
    'oversold': 30,
    'overbought': 70,
    'period': 14
}

# Variáveis globais para controle
monitoring_thread = None
stop_monitoring = False
current_timeframe = None
current_multiple = None
current_ema_type = None
thread_id = 0
last_processed = {}
alert_history = []
MAX_ALERT_HISTORY = 200

# Estatísticas
stats = {
    'volume_alerts': 0,
    'rsi_alerts': 0,
    'ema_alerts': 0,
    'macd_alerts': 0,
    'total_cycles': 0,
    'total_processed': 0,
    'start_time': None
}

# Controle de rate limiting
symbol_batches = []
current_batch_index = 0
BATCH_SIZE = 50

# Controle de último RSI por símbolo
last_rsi_values = {}

class TechnicalAnalyzer:
    """Classe para cálculos técnicos avançados"""
    
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
        """Calcula RSI com método Wilder"""
        if len(prices) < period + 1:
            return [50.0] * len(prices)
        
        try:
            deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
            gains = [max(delta, 0) for delta in deltas]
            losses = [max(-delta, 0) for delta in deltas]
            
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            
            rsi_values = [100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100]
            
            for i in range(period, len(gains)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                
                if avg_loss == 0:
                    rsi_values.append(100)
                else:
                    rs = avg_gain / avg_loss
                    rsi_values.append(100 - (100 / (1 + rs)))
            
            return [50.0] * period + rsi_values
            
        except Exception as e:
            logger.error(f"Erro RSI: {e}")
            return [50.0] * len(prices)
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calcula EMA com validação robusta"""
        if len(prices) < period:
            return [prices[0]] * len(prices) if prices else []
        
        try:
            multiplier = 2 / (period + 1)
            sma = sum(prices[:period]) / period
            ema_values = [sma]
            
            for price in prices[period:]:
                ema = (price * multiplier) + (ema_values[-1] * (1 - multiplier))
                ema_values.append(ema)
            
            return [prices[0]] * (period - 1) + ema_values
            
        except Exception as e:
            logger.error(f"Erro EMA {period}: {e}")
            return prices
    
    @staticmethod
    def calculate_macd(prices: List[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Tuple[List[float], List[float], List[float]]:
        """Calcula MACD completo (linha, sinal, histograma)"""
        try:
            ema_fast = TechnicalAnalyzer.calculate_ema(prices, fast_period)
            ema_slow = TechnicalAnalyzer.calculate_ema(prices, slow_period)
            
            min_len = min(len(ema_fast), len(ema_slow))
            macd_line = [ema_fast[i] - ema_slow[i] for i in range(min_len)]
            
            signal_line = TechnicalAnalyzer.calculate_ema(macd_line, signal_period)
            
            histogram = []
            for i in range(min(len(macd_line), len(signal_line))):
                histogram.append(macd_line[i] - signal_line[i])
            
            return macd_line, signal_line, histogram
            
        except Exception as e:
            logger.error(f"Erro MACD: {e}")
            empty = [0.0] * len(prices)
            return empty, empty, empty

def format_volume(volume: float) -> str:
    """Formata o volume para exibição mais legível"""
    if volume >= 1_000_000:
        return f"{volume/1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"{volume/1_000:.2f}K"
    else:
        return f"{volume:.2f}"

def initialize_volume_from_klines(symbol: str, klines: list):
    """Inicializa dados de volume a partir dos klines já obtidos para indicadores"""
    try:
        if len(klines) >= 20:
            # Extrair volumes dos últimos 20 candles fechados
            # kline structure: [open_time, open, high, low, close, volume, close_time, ...]
            volumes = [float(k[5]) for k in klines[-20:]]  # Últimos 20 candles fechados
            last_close = float(klines[-1][4])  # Preço de fechamento do último candle fechado
            
            # Inicializar estruturas de dados
            volume_history[symbol] = volumes
            last_closed_close[symbol] = last_close
            alerted_this_candle[symbol] = False
            
            logger.debug(f"✅ Volume inicializado para {symbol}: {len(volumes)} volumes históricos")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar volume para {symbol}: {e}")
        return False

def check_volume_alert(symbol: str, current_volume: float, current_close: float):
    """Verifica e emite alerta de volume baseado na média dos últimos 20 candles"""
    global current_multiple, volume_alert_counters
    
    if current_multiple is None:
        return
    
    # Verificar se temos histórico suficiente e se a vela está ABERTA
    if (symbol in volume_history and len(volume_history[symbol]) >= 20 and
        symbol in current_klines and not current_klines[symbol]['is_final']):
        
        # Calcular média dos últimos 20 volumes fechados
        avg_volume = sum(volume_history[symbol]) / len(volume_history[symbol])
        
        if avg_volume > 0:
            volume_ratio = current_volume / avg_volume
            
            # Verificar se o volume atual (parcial) é maior que a média multiplicada pelo múltiplo
            if volume_ratio >= current_multiple:
                # Verificar se já não alertamos para esta vela
                if not alerted_this_candle.get(symbol, False):
                    # Calcular variação de preço
                    price_change = 0
                    if symbol in last_closed_close and last_closed_close[symbol] > 0:
                        previous_close = last_closed_close[symbol]
                        price_change = ((current_close - previous_close) / previous_close) * 100
                    
                    # Incrementar contador de alertas
                    if symbol not in volume_alert_counters:
                        volume_alert_counters[symbol] = 0
                    volume_alert_counters[symbol] += 1
                    
                    alert = {
                        'type': 'VOLUME_ALERT',
                        'crypto': symbol.replace('USDT', ''),
                        'symbol': symbol,
                        'current_volume': round(current_volume, 2),
                        'current_volume_formatted': format_volume(current_volume),
                        'average_volume': round(avg_volume, 2),
                        'average_volume_formatted': format_volume(avg_volume),
                        'volume_ratio': round(volume_ratio, 2),
                        'multiple': round(current_multiple, 2),
                        'price': round(current_close, 6),
                        'price_change': round(price_change, 2),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'timeframe': current_timeframe,
                        'timeframe_name': TIMEFRAME_CONFIG[current_timeframe]['name'],
                        'thread_id': thread_id,
                        'color': 'green' if price_change > 0 else 'red',
                        'alert_count': volume_alert_counters[symbol],
                        'alert_description': f'Volume PARCIAL {volume_ratio:.1f}x maior que média histórica (20 velas) - Vela ABERTA'
                    }
                    
                    socketio.emit('volume_alert', alert)
                    add_alert_to_history(alert)
                    stats['volume_alerts'] += 1
                    
                    # Marcar que já alertamos para esta vela
                    alerted_this_candle[symbol] = True
                    
                    logger.info(f"🚨📊 VOLUME ALERT #{volume_alert_counters[symbol]}: {symbol} | Volume PARCIAL: {format_volume(current_volume)} | Média Histórica: {format_volume(avg_volume)} | Múltiplo: {volume_ratio:.1f}x | Preço: {current_close:.6f} | Variação: {price_change:+.2f}% | Vela ABERTA")

def on_websocket_message(ws, message):
    """Processa mensagens do WebSocket"""
    try:
        msg = json.loads(message)
        
        if 'stream' in msg and 'data' in msg:
            stream = msg['stream']
            data = msg['data']
            
            symbol_lower = stream.split('@')[0]
            symbol = symbol_lower.upper()
            
            if '@kline_' in stream:
                if data['e'] == 'kline':
                    k = data['k']
                    current_volume = float(k['v'])
                    current_close = float(k['c'])
                    is_final = k['x']
                    
                    # Atualizar candle atual (aberto ou fechado)
                    current_klines[symbol] = {
                        'open': float(k['o']),
                        'high': float(k['h']),
                        'low': float(k['l']),
                        'close': current_close,
                        'volume': current_volume,
                        'timestamp': k['t'],
                        'is_final': is_final
                    }
                    
                    # VERIFICAR ALERTAS DE VOLUME APENAS PARA VELAS ABERTAS
                    if not is_final:  # Apenas para velas abertas (volume parcial)
                        check_volume_alert(symbol, current_volume, current_close)
                    
                    # Se candle fechou, atualizar histórico e resetar alertas
                    if is_final:
                        # Atualizar histórico de volumes com o candle recém-fechado
                        if symbol not in volume_history:
                            volume_history[symbol] = []
                        
                        volume_history[symbol].append(current_volume)
                        
                        # Manter apenas os últimos 20 volumes
                        if len(volume_history[symbol]) > 20:
                            volume_history[symbol].pop(0)
                        
                        # Resetar flag de alerta para o próximo candle
                        alerted_this_candle[symbol] = False
                        
                        # Atualizar último preço fechado
                        last_closed_close[symbol] = current_close
                        
                        logger.debug(f"✅ Candle FECHADO {symbol}: Volume {format_volume(current_volume)} | Histórico: {len(volume_history[symbol])} velas")
            
            elif '@ticker' in stream:
                if data['e'] == '24hrTicker':
                    live_tickers[symbol] = {
                        'price': float(data['c']),
                        'volume': float(data['v']),
                        'price_change': float(data['p']),
                        'price_change_percent': float(data['P']),
                        'timestamp': datetime.now().timestamp()
                    }
                
    except Exception as e:
        logger.error(f"❌ Erro ao processar mensagem WebSocket: {e}")

def on_websocket_error(ws, error):
    """Lida com erros do WebSocket"""
    logger.error(f"❌ Erro WebSocket: {error}")

def on_websocket_close(ws, close_status_code, close_msg):
    """Lida com fechamento do WebSocket"""
    logger.info("🔌 WebSocket fechado")

def on_websocket_open(ws):
    """Lida com abertura do WebSocket"""
    logger.info("🔌 WebSocket conectado")

def initialize_websocket_connections(symbols: List[str], timeframe: str):
    """Inicializa múltiplas conexões WebSocket combinadas para todos os símbolos"""
    global active_websockets
    
    # Fechar conexões existentes
    stop_websocket_connections()
    
    # Converter timeframe para formato WebSocket
    ws_timeframe = convert_timeframe_to_websocket(timeframe)
    
    # Tamanho do chunk: 50 símbolos por conexão (100 streams: kline + ticker)
    chunk_size = 50
    symbol_chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
    
    logger.info(f"📡 Iniciando {len(symbol_chunks)} conexões WebSocket combinadas para {len(symbols)} símbolos")
    
    for idx, chunk in enumerate(symbol_chunks):
        try:
            # Criar streams para o chunk
            streams = [f"{symbol.lower()}@kline_{ws_timeframe}" for symbol in chunk]
            streams += [f"{symbol.lower()}@ticker" for symbol in chunk]
            
            stream_path = "/".join(streams)
            url = f"wss://stream.binance.com:9443/stream?streams={stream_path}"
            
            ws = websocket.WebSocketApp(
                url,
                on_message=on_websocket_message,
                on_error=on_websocket_error,
                on_close=on_websocket_close,
                on_open=on_websocket_open
            )
            
            # Iniciar em thread separada
            ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
            ws_thread.start()
            
            active_websockets.append(ws)
            
            logger.info(f"✅ Conexão {idx+1}/{len(symbol_chunks)} iniciada com {len(chunk)} símbolos")
            time.sleep(0.3)  # Pausa para evitar rate limit de conexões
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar conexão WebSocket {idx+1}: {e}")
    
    logger.info(f"✅ {len(active_websockets)} conexões WebSocket iniciadas")

def convert_timeframe_to_websocket(timeframe: str) -> str:
    """Converte timeframe para formato WebSocket da Binance"""
    timeframe_map = {
        '1m': '1m',
        '3m': '3m', 
        '5m': '5m',
        '15m': '15m',
        '30m': '30m',
        '1h': '1h',
        '2h': '2h',
        '4h': '4h',
        '6h': '6h',
        '8h': '8h',
        '12h': '12h',
        '1d': '1d'
    }
    return timeframe_map.get(timeframe, '1m')

def stop_websocket_connections():
    """Para todas as conexões WebSocket ativas"""
    global active_websockets
    
    try:
        for ws in active_websockets:
            try:
                ws.close()
            except:
                pass
        
        active_websockets.clear()
        live_klines.clear()
        live_tickers.clear()
        current_klines.clear()
        volume_history.clear()
        last_closed_close.clear()
        alerted_this_candle.clear()
        volume_alert_counters.clear()
        
        logger.info("✅ Conexões WebSocket paradas e dados limpos")
        
    except Exception as e:
        logger.error(f"❌ Erro ao parar WebSockets: {e}")

def initialize_symbol_batches():
    """Inicializa os batches de símbolos para processamento rotativo"""
    global symbol_batches, current_batch_index
    symbols = get_trading_symbols()
    
    if not symbols:
        logger.error("❌ CRÍTICO: Nenhum símbolo disponível para monitoramento!")
        symbol_batches = []
        return
    
    symbol_batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    current_batch_index = 0
    
    logger.info(f"📦 Inicializados {len(symbol_batches)} batches de {BATCH_SIZE} símbolos cada")
    return symbol_batches

def get_next_symbol_batch():
    """Obtém o próximo batch de símbolos para processamento"""
    global symbol_batches, current_batch_index
    
    if not symbol_batches:
        logger.warning("🔄 Nenhum batch disponível, inicializando...")
        initialize_symbol_batches()
        
        if not symbol_batches:
            logger.error("❌ Nenhum batch disponível mesmo após inicialização")
            return []
    
    batch = symbol_batches[current_batch_index]
    current_batch_index = (current_batch_index + 1) % len(symbol_batches)
    
    return batch

def add_alert_to_history(alert: Dict):
    """Adiciona alerta ao histórico"""
    global alert_history
    alert['id'] = len(alert_history) + 1
    alert_history.insert(0, alert)
    
    if len(alert_history) > MAX_ALERT_HISTORY:
        alert_history = alert_history[:MAX_ALERT_HISTORY]

def monitor_whales(timeframe: str, multiple: float, ema_type: str, my_thread_id: int):
    """Função principal de monitoramento"""
    global stop_monitoring, current_timeframe, current_multiple, current_ema_type, thread_id, last_processed, stats, last_rsi_values
    
    logger.info(f"🚀 THREAD {my_thread_id} INICIADA: TF={timeframe}, Múltiplo={multiple}, EMA={ema_type}")
    
    if my_thread_id != thread_id:
        logger.info(f"❌ THREAD {my_thread_id} CANCELADA")
        return
    
    current_timeframe = timeframe
    current_multiple = multiple
    current_ema_type = ema_type
    ema_config = EMA_CONFIGS[ema_type]
    timeframe_config = TIMEFRAME_CONFIG[timeframe]
    
    check_interval = timeframe_config['interval']
    
    initialize_symbol_batches()
    all_symbols = get_trading_symbols()
    
    if not all_symbols:
        logger.error("❌ IMPOSSÍVEL INICIAR: Nenhum símbolo disponível")
        socketio.emit('monitoring_error', {
            'error': 'Nenhum símbolo disponível para monitoramento',
            'thread_id': my_thread_id
        })
        return
    
    # Inicializar WebSockets para volume em tempo real
    initialize_websocket_connections(all_symbols, timeframe)
    
    stats = {
        'volume_alerts': 0,
        'rsi_alerts': 0,
        'ema_alerts': 0,
        'macd_alerts': 0,
        'total_cycles': 0,
        'total_processed': 0,
        'start_time': datetime.now().isoformat(),
        'total_symbols': len(all_symbols)
    }
    
    last_rsi_values = {}
    
    socketio.emit('monitoring_started', {
        'status': 'Iniciado', 
        'timeframe': timeframe, 
        'multiple': multiple,
        'ema_type': ema_type,
        'ema_name': ema_config['name'],
        'symbols_count': len(all_symbols),
        'thread_id': my_thread_id
    })
    
    cycle_count = 0
    analyzer = TechnicalAnalyzer()
    
    while not stop_monitoring and my_thread_id == thread_id:
        cycle_count += 1
        stats['total_cycles'] = cycle_count
        
        cycle_alerts = {
            'volume': 0,  # Volume agora é handled no WebSocket
            'rsi': 0,
            'ema': 0,
            'macd': 0
        }
        processed_count = 0
        
        current_batch = get_next_symbol_batch()
        
        if not current_batch:
            logger.error("❌ Batch vazio, aguardando e tentando novamente...")
            time.sleep(check_interval)
            continue
        
        logger.info(f"🔍 CICLO {cycle_count} - Batch {current_batch_index}/{len(symbol_batches)} - {len(current_batch)} símbolos")
        
        for symbol in current_batch:
            if stop_monitoring or my_thread_id != thread_id:
                logger.info(f"❌ THREAD {my_thread_id} INTERROMPIDA")
                return
            
            try:
                current_time = time.time()
                if symbol in last_processed:
                    time_since_last = current_time - last_processed[symbol]
                    if time_since_last < check_interval / 3:
                        continue
                
                last_processed[symbol] = current_time
                
                # Usar dados do WebSocket se disponíveis, senão usar API REST apenas para indicadores
                if symbol in live_klines and len(live_klines[symbol]) >= 50:
                    klines_data = live_klines[symbol]
                    closes = [k['close'] for k in klines_data]
                else:
                    # Fallback para API REST apenas para indicadores técnicos
                    try:
                        klines = client.get_klines(symbol=symbol, interval=timeframe, limit=100)
                        if not klines or len(klines) < 50:
                            continue
                        closes = [float(k[4]) for k in klines]
                        
                        # INICIALIZAR VOLUME A PARTIR DOS MESMOS KLINES
                        if symbol not in volume_history:
                            initialize_volume_from_klines(symbol, klines)
                            
                    except:
                        continue
                
                processed_count += 1
                stats['total_processed'] += 1
                
                # ANÁLISE RSI (apenas indicadores técnicos)
                try:
                    rsi_values = analyzer.calculate_rsi(closes, RSI_CONFIG['period'])
                    if len(rsi_values) >= 1:
                        current_rsi = rsi_values[-1]
                        
                        if symbol in last_rsi_values:
                            previous_rsi = last_rsi_values[symbol]
                            
                            if previous_rsi <= 30 and current_rsi > 31:
                                rsi_alert = {
                                    'type': 'RSI_OVERSOLD',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'previous_rsi': round(previous_rsi, 2),
                                    'current_rsi': round(current_rsi, 2),
                                    'value': round(current_rsi, 2),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'green'
                                }
                                socketio.emit('indicator_alert', rsi_alert)
                                add_alert_to_history(rsi_alert)
                                cycle_alerts['rsi'] += 1
                                stats['rsi_alerts'] += 1
                                logger.info(f"🚨📈 RSI OVERSOLD: {symbol} {previous_rsi:.1f}→{current_rsi:.1f}, TF={timeframe}")
                            
                            elif previous_rsi >= 70 and current_rsi < 69:
                                rsi_alert = {
                                    'type': 'RSI_OVERBOUGHT',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'previous_rsi': round(previous_rsi, 2),
                                    'current_rsi': round(current_rsi, 2),
                                    'value': round(current_rsi, 2),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'red'
                                }
                                socketio.emit('indicator_alert', rsi_alert)
                                add_alert_to_history(rsi_alert)
                                cycle_alerts['rsi'] += 1
                                stats['rsi_alerts'] += 1
                                logger.info(f"🚨📉 RSI OVERBOUGHT: {symbol} {previous_rsi:.1f}→{current_rsi:.1f}, TF={timeframe}")
                        
                        last_rsi_values[symbol] = current_rsi
                            
                except Exception as rsi_error:
                    logger.debug(f"Erro RSI {symbol}: {rsi_error}")
                
                # ANÁLISE EMA
                try:
                    if len(closes) >= ema_config['slow']:
                        ema_fast = analyzer.calculate_ema(closes, ema_config['fast'])
                        ema_slow = analyzer.calculate_ema(closes, ema_config['slow'])
                        
                        if (len(ema_fast) >= 2 and len(ema_slow) >= 2 and
                            not math.isnan(ema_fast[-2]) and not math.isnan(ema_slow[-2]) and
                            not math.isnan(ema_fast[-1]) and not math.isnan(ema_slow[-1])):
                            
                            if (ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]):
                                ema_alert = {
                                    'type': 'EMA_GOLDEN_CROSS',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'ema_name': ema_config['name'],
                                    'ema_fast': round(ema_fast[-1], 6),
                                    'ema_slow': round(ema_slow[-1], 6),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'green'
                                }
                                socketio.emit('indicator_alert', ema_alert)
                                add_alert_to_history(ema_alert)
                                cycle_alerts['ema'] += 1
                                stats['ema_alerts'] += 1
                                logger.info(f"🚨📈 EMA GOLDEN CROSS: {symbol} ({ema_config['name']}) {round(ema_fast[-1], 6)}/{round(ema_slow[-1], 6)}, TF={timeframe}")
                            
                            elif (ema_fast[-2] >= ema_slow[-2] and ema_fast[-1] < ema_slow[-1]):
                                ema_alert = {
                                    'type': 'EMA_DEATH_CROSS',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'ema_name': ema_config['name'],
                                    'ema_fast': round(ema_fast[-1], 6),
                                    'ema_slow': round(ema_slow[-1], 6),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'red'
                                }
                                socketio.emit('indicator_alert', ema_alert)
                                add_alert_to_history(ema_alert)
                                cycle_alerts['ema'] += 1
                                stats['ema_alerts'] += 1
                                logger.info(f"🚨📉 EMA DEATH CROSS: {symbol} ({ema_config['name']}) {round(ema_fast[-1], 6)}/{round(ema_slow[-1], 6)}, TF={timeframe}")
                                
                except Exception as ema_error:
                    logger.debug(f"Erro EMA {symbol}: {ema_error}")

                # ANÁLISE MACD
                try:
                    if len(closes) >= 35:
                        macd_line, signal_line, histogram = analyzer.calculate_macd(closes)
                        
                        if (len(macd_line) >= 2 and len(signal_line) >= 2 and
                            not math.isnan(macd_line[-2]) and not math.isnan(signal_line[-2]) and
                            not math.isnan(macd_line[-1]) and not math.isnan(signal_line[-1])):
                            
                            if (macd_line[-2] <= signal_line[-2] and macd_line[-1] > signal_line[-1]):
                                macd_alert = {
                                    'type': 'MACD_BULLISH_CROSS',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'macd_line': round(macd_line[-1], 6),
                                    'signal_line': round(signal_line[-1], 6),
                                    'histogram': round(histogram[-1], 6),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'green'
                                }
                                socketio.emit('indicator_alert', macd_alert)
                                add_alert_to_history(macd_alert)
                                cycle_alerts['macd'] += 1
                                stats['macd_alerts'] += 1
                                logger.info(f"🚨📈 MACD BULLISH: {symbol} MACD={round(macd_line[-1], 6)}, Signal={round(signal_line[-1], 6)}, Hist={round(histogram[-1], 6)}, TF={timeframe}")
                            
                            elif (macd_line[-2] >= signal_line[-2] and macd_line[-1] < signal_line[-1]):
                                macd_alert = {
                                    'type': 'MACD_BEARISH_CROSS',
                                    'crypto': symbol.replace('USDT', ''),
                                    'symbol': symbol,
                                    'macd_line': round(macd_line[-1], 6),
                                    'signal_line': round(signal_line[-1], 6),
                                    'histogram': round(histogram[-1], 6),
                                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'timeframe': timeframe,
                                    'timeframe_name': timeframe_config['name'],
                                    'thread_id': my_thread_id,
                                    'color': 'red'
                                }
                                socketio.emit('indicator_alert', macd_alert)
                                add_alert_to_history(macd_alert)
                                cycle_alerts['macd'] += 1
                                stats['macd_alerts'] += 1
                                logger.info(f"🚨📉 MACD BEARISH: {symbol} MACD={round(macd_line[-1], 6)}, Signal={round(signal_line[-1], 6)}, Hist={round(histogram[-1], 6)}, TF={timeframe}")
                                
                except Exception as macd_error:
                    logger.debug(f"Erro MACD {symbol}: {macd_error}")
                    
            except Exception as e:
                logger.error(f"❌ Erro geral {symbol}: {e}")
            
            time.sleep(0.05)  # Rate limiting
        
        if my_thread_id != thread_id:
            logger.info(f"❌ THREAD {my_thread_id} FINALIZADA")
            return
            
        logger.info(f"✅ CICLO {cycle_count} COMPLETO:")
        logger.info(f"   📊 RSI: {cycle_alerts['rsi']}, EMA: {cycle_alerts['ema']}, MACD: {cycle_alerts['macd']}")
        logger.info(f"   🔄 Símbolos processados: {processed_count}")
        logger.info(f"   ⏱️  Próxima verificação: {check_interval}s")
        logger.info("=" * 70)
        
        socketio.emit('stats_update', {
            'cycle': cycle_count,
            'alerts': cycle_alerts,
            'processed': processed_count,
            'total_alerts': stats,
            'batch_info': {
                'current_batch': current_batch_index,
                'total_batches': len(symbol_batches),
                'batch_size': BATCH_SIZE
            }
        })
        
        time.sleep(check_interval)
    
    if my_thread_id == thread_id:
        stop_websocket_connections()

@app.route('/')
def index():
    return render_template('index.html', 
                         TIMEFRAME_CONFIG=TIMEFRAME_CONFIG,
                         EMA_CONFIGS=EMA_CONFIGS)

@app.route('/status')
def status():
    ema_name = EMA_CONFIGS.get(current_ema_type, {}).get('name', '') if current_ema_type else ''
    symbols = get_trading_symbols()
    
    return {
        'monitoring_active': not stop_monitoring,
        'current_timeframe': current_timeframe,
        'current_multiple': current_multiple,
        'current_ema_type': current_ema_type,
        'current_ema_name': ema_name,
        'total_symbols': len(symbols),
        'monitoring_thread_active': monitoring_thread is not None,
        'current_thread_id': thread_id,
        'stats': stats,
        'websocket_connections': len(active_websockets),
        'live_data_symbols': len(live_klines),
        'volume_history_symbols': len(volume_history)
    }

@app.route('/start_monitoring', methods=['POST'])
def start_monitoring():
    global monitoring_thread, stop_monitoring, thread_id, current_ema_type
    try:
        timeframe = request.form['timeframe']
        multiple = float(request.form['multiple'])
        ema_type = request.form.get('ema_type', 'ema_7_21')
        
        if timeframe not in TIMEFRAME_CONFIG:
            return "Timeframe inválido.", 400
        
        if ema_type not in EMA_CONFIGS:
            return "Tipo de EMA inválido.", 400
        
        symbols = get_trading_symbols()
        if not symbols:
            return "Erro: Nenhum símbolo disponível para monitoramento.", 500
        
        stop_monitoring = True
        thread_id += 1
        new_thread_id = thread_id
        
        logger.info(f"🛑 Parando threads antigas. Nova thread: {new_thread_id}")
        time.sleep(1.0)
        
        stop_monitoring = False
        
        monitoring_thread = threading.Thread(
            target=monitor_whales, 
            args=(timeframe, multiple, ema_type, new_thread_id),
            daemon=True
        )
        monitoring_thread.start()
        
        logger.info(f"✅ Thread {new_thread_id} iniciada com sucesso")
        return f"Monitoramento iniciado - Thread {new_thread_id}", 200
        
    except Exception as e:
        logger.error(f"Erro ao iniciar monitoramento: {e}")
        return f"Erro: {str(e)}", 500

@app.route('/stop_monitoring', methods=['POST'])
def stop_monitoring_route():
    global stop_monitoring, thread_id
    stop_monitoring = True
    thread_id += 1
    
    stop_websocket_connections()
    
    logger.info("🛑 Monitoramento parado pelo usuário")
    return "Monitoramento parado", 200

@app.route('/alerts')
def get_alerts():
    return jsonify(alert_history)

@app.route('/stats')
def get_stats():
    return jsonify(stats)

@app.route('/volume_data')
def get_volume_data():
    """Retorna dados de volume atuais para debug"""
    return jsonify({
        'volume_history': {k: [format_volume(vol) for vol in v] for k, v in volume_history.items()},
        'current_volumes': {k: format_volume(v['volume']) for k, v in current_klines.items() if not v['is_final']},
        'volume_alert_counters': volume_alert_counters,
        'alerted_this_candle': alerted_this_candle
    })

@socketio.on('connect')
def handle_connect():
    logger.info('✅ Cliente conectado via WebSocket')
    emit('connection_status', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info('❌ Cliente desconectado via WebSocket')

@socketio.on('request_status')
def handle_status_request():
    ema_name = EMA_CONFIGS.get(current_ema_type, {}).get('name', '') if current_ema_type else ''
    symbols = get_trading_symbols()
    
    emit('status_update', {
        'monitoring_active': not stop_monitoring,
        'current_timeframe': current_timeframe,
        'current_multiple': current_multiple,
        'current_ema_type': current_ema_type,
        'current_ema_name': ema_name,
        'total_symbols': len(symbols),
        'stats': stats,
        'websocket_connections': len(active_websockets),
        'live_data_symbols': len(live_klines)
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    symbols = get_trading_symbols()
    logger.info(f"🚀 Iniciando Whale Detector Pro na porta {port}")
    logger.info(f"📊 {len(symbols)} símbolos carregados")
    logger.info(f"🔧 Modo: {'Desenvolvimento' if debug_mode else 'Produção'}")
    
    if debug_mode:
        socketio.run(app, debug=True, host='0.0.0.0', port=port, use_reloader=True, allow_unsafe_werkzeug=True)
    else:
        socketio.run(app, debug=False, host='0.0.0.0', port=port, use_reloader=False)