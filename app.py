from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from binance.client import Client
import time
import os
from datetime import datetime
import math
import threading
import requests
import json
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

# Inicializar cliente Binance com configurações robustas
try:
    client = Client()
    logger.info("✅ Cliente Binance inicializado com sucesso")
except Exception as e:
    logger.error(f"❌ Erro ao inicializar cliente Binance: {e}")
    client = None

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
BATCH_SIZE = 130  # Processar 130 símbolos por ciclo para não sobrecarregar a API

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
            
            # Preencher valores iniciais
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
            
            # Garantir mesmo tamanho
            min_len = min(len(ema_fast), len(ema_slow))
            macd_line = [ema_fast[i] - ema_slow[i] for i in range(min_len)]
            
            signal_line = TechnicalAnalyzer.calculate_ema(macd_line, signal_period)
            
            # Calcular histograma
            histogram = []
            for i in range(min(len(macd_line), len(signal_line))):
                histogram.append(macd_line[i] - signal_line[i])
            
            return macd_line, signal_line, histogram
            
        except Exception as e:
            logger.error(f"Erro MACD: {e}")
            empty = [0.0] * len(prices)
            return empty, empty, empty

class BinanceAPI:
    """Classe para interações com API Binance"""
    
    @staticmethod
    def safe_request(func, *args, **kwargs):
        """Wrapper seguro para requests da Binance"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = func(*args, **kwargs)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Tentativa {attempt + 1} falhou: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    logger.error(f"Todas as tentativas falharam para {func.__name__}")
                    return None
        return None
    
    @staticmethod
    def get_klines(symbol: str, interval: str, limit: int = 100):
        """Obtém klines com tratamento de erro"""
        return BinanceAPI.safe_request(client.get_klines, symbol=symbol, interval=interval, limit=limit)
    
    @staticmethod
    def get_symbol_ticker(symbol: str):
        """Obtém ticker do símbolo"""
        return BinanceAPI.safe_request(client.get_symbol_ticker, symbol=symbol)

def initialize_symbol_batches():
    """Inicializa os batches de símbolos para processamento rotativo"""
    global symbol_batches, current_batch_index
    symbols = get_trading_symbols()
    
    if not symbols:
        logger.error("❌ CRÍTICO: Nenhum símbolo disponível para monitoramento!")
        symbol_batches = []
        return
    
    # Criar batches menores para não sobrecarregar a API
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
        
        # Se ainda não houver batches, retornar lista vazia
        if not symbol_batches:
            logger.error("❌ Nenhum batch disponível mesmo após inicialização")
            return []
    
    # Obter batch atual e avançar índice
    batch = symbol_batches[current_batch_index]
    current_batch_index = (current_batch_index + 1) % len(symbol_batches)
    
    return batch

def add_alert_to_history(alert: Dict):
    """Adiciona alerta ao histórico"""
    global alert_history
    alert['id'] = len(alert_history) + 1
    alert_history.insert(0, alert)
    
    # Manter apenas os últimos alertas
    if len(alert_history) > MAX_ALERT_HISTORY:
        alert_history = alert_history[:MAX_ALERT_HISTORY]

def monitor_whales(timeframe: str, multiple: float, ema_type: str, my_thread_id: int):
    """Função principal de monitoramento para TODAS as criptomoedas"""
    global stop_monitoring, current_timeframe, current_multiple, current_ema_type, thread_id, last_processed, stats, last_rsi_values
    
    logger.info(f"🚀 THREAD {my_thread_id} INICIADA: TF={timeframe}, Múltiplo={multiple}, EMA={ema_type}")
    
    # Verificar se thread ainda é válida
    if my_thread_id != thread_id:
        logger.info(f"❌ THREAD {my_thread_id} CANCELADA")
        return
    
    # Configurações atuais
    current_timeframe = timeframe
    current_multiple = multiple
    current_ema_type = ema_type
    ema_config = EMA_CONFIGS[ema_type]
    timeframe_config = TIMEFRAME_CONFIG[timeframe]
    
    check_interval = timeframe_config['interval']
    
    # Inicializar batches de símbolos
    initialize_symbol_batches()
    all_symbols = get_trading_symbols()
    
    if not all_symbols:
        logger.error("❌ IMPOSSÍVEL INICIAR: Nenhum símbolo disponível")
        socketio.emit('monitoring_error', {
            'error': 'Nenhum símbolo disponível para monitoramento',
            'thread_id': my_thread_id
        })
        return
    
    # Inicializar estatísticas
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
    
    # Resetar controle de RSI
    last_rsi_values = {}
    
    # Notificar início
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
            'volume': 0,
            'rsi': 0,
            'ema': 0,
            'macd': 0
        }
        processed_count = 0
        
        # Obter batch atual de símbolos
        current_batch = get_next_symbol_batch()
        
        if not current_batch:
            logger.error("❌ Batch vazio, aguardando e tentando novamente...")
            time.sleep(check_interval)
            continue
        
        logger.info(f"🔍 CICLO {cycle_count} - Batch {current_batch_index}/{len(symbol_batches)} - {len(current_batch)} símbolos - Múltiplo: {multiple}")
        
        for symbol in current_batch:
            # Verificar se deve continuar
            if stop_monitoring or my_thread_id != thread_id:
                logger.info(f"❌ THREAD {my_thread_id} INTERROMPIDA")
                return
            
            try:
                # Rate limiting inteligente
                current_time = time.time()
                if symbol in last_processed:
                    time_since_last = current_time - last_processed[symbol]
                    if time_since_last < check_interval / 3:
                        continue
                
                last_processed[symbol] = current_time
                
                # Obter dados
                klines = BinanceAPI.get_klines(symbol, timeframe, 100)
                if not klines or len(klines) < 50:
                    continue
                
                # Extrair dados das klines
                closes = [float(k[4]) for k in klines]
                volumes = [float(k[5]) for k in klines]
                
                processed_count += 1
                stats['total_processed'] += 1
                
                # 1. ANÁLISE RSI - CORRIGIDA
                try:
                    rsi_values = analyzer.calculate_rsi(closes, RSI_CONFIG['period'])
                    if len(rsi_values) >= 1:
                        current_rsi = rsi_values[-1]
                        
                        # Verificar se já temos um valor anterior armazenado
                        if symbol in last_rsi_values:
                            previous_rsi = last_rsi_values[symbol]
                            
                            # Alerta RSI Oversold - Cruzamento de baixo de 30 acima de 31
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
                                logger.info(f"🚨📈 RSI OVERSOLD: {symbol} {previous_rsi:.1f}→{current_rsi:.1f}")
                            
                            # Alerta RSI Overbought - Cruzamento de cima de 70 abaixo de 69
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
                                logger.info(f"🚨📉 RSI OVERBOUGHT: {symbol} {previous_rsi:.1f}→{current_rsi:.1f}")
                        
                        # Armazenar RSI atual para próxima comparação
                        last_rsi_values[symbol] = current_rsi
                            
                except Exception as rsi_error:
                    logger.debug(f"Erro RSI {symbol}: {rsi_error}")
                
                # 2. ANÁLISE EMA
                try:
                    if len(closes) >= ema_config['slow']:
                        ema_fast = analyzer.calculate_ema(closes, ema_config['fast'])
                        ema_slow = analyzer.calculate_ema(closes, ema_config['slow'])
                        
                        if (len(ema_fast) >= 2 and len(ema_slow) >= 2 and
                            not math.isnan(ema_fast[-2]) and not math.isnan(ema_slow[-2]) and
                            not math.isnan(ema_fast[-1]) and not math.isnan(ema_slow[-1])):
                            
                            # Golden Cross
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
                                logger.info(f"🚨📈 EMA GOLDEN CROSS: {symbol} ({ema_config['name']})")
                            
                            # Death Cross
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
                                logger.info(f"🚨📉 EMA DEATH CROSS: {symbol} ({ema_config['name']})")
                                
                except Exception as ema_error:
                    logger.debug(f"Erro EMA {symbol}: {ema_error}")

                # 3. ANÁLISE MACD
                try:
                    if len(closes) >= 35:
                        macd_line, signal_line, histogram = analyzer.calculate_macd(closes)
                        
                        if (len(macd_line) >= 2 and len(signal_line) >= 2 and
                            not math.isnan(macd_line[-2]) and not math.isnan(signal_line[-2]) and
                            not math.isnan(macd_line[-1]) and not math.isnan(signal_line[-1])):
                            
                            # Bullish Cross
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
                                logger.info(f"🚨📈 MACD BULLISH: {symbol}")
                            
                            # Bearish Cross
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
                                logger.info(f"🚨📉 MACD BEARISH: {symbol}")
                                
                except Exception as macd_error:
                    logger.debug(f"Erro MACD {symbol}: {macd_error}")

                # 4. ANÁLISE DE VOLUME
                try:
                    volume_klines = BinanceAPI.get_klines(symbol, timeframe, 21)
                    if not volume_klines or len(volume_klines) < 21:
                        continue
                    
                    current_volume = float(volume_klines[-1][5])
                    current_close = float(volume_klines[-1][4])
                    
                    # Calcular volume médio (excluindo atual)
                    historical_volumes = [float(k[5]) for k in volume_klines[-21:-1]]
                    avg_volume = sum(historical_volumes) / len(historical_volumes) if historical_volumes else 0
                    
                    if avg_volume > 0:
                        volume_ratio = current_volume / avg_volume
                        
                        # Alerta de volume alto
                        if volume_ratio >= multiple and my_thread_id == thread_id:
                            price_change = ((current_close - float(volume_klines[-2][4])) / float(volume_klines[-2][4])) * 100
                            
                            volume_alert = {
                                'crypto': symbol.replace('USDT', ''),
                                'symbol': symbol,
                                'volume': round(current_volume, 2),
                                'avg_volume': round(avg_volume, 2),
                                'multiple': round(volume_ratio, 2),
                                'price': round(current_close, 6),
                                'price_change': round(price_change, 2),
                                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'timeframe': timeframe,
                                'timeframe_name': timeframe_config['name'],
                                'thread_id': my_thread_id,
                                'color': 'green' if price_change > 0 else 'red'
                            }
                            socketio.emit('whale_alert', volume_alert)
                            add_alert_to_history(volume_alert)
                            cycle_alerts['volume'] += 1
                            stats['volume_alerts'] += 1
                            logger.info(f"🚨📊 VOLUME ALERT: {symbol} {volume_ratio:.1f}x ({price_change:+.1f}%)")
                
                except Exception as volume_error:
                    logger.debug(f"Erro Volume {symbol}: {volume_error}")
                    
            except Exception as e:
                logger.error(f"❌ Erro geral {symbol}: {e}")
            
            # Pequena pausa entre símbolos para rate limiting
            time.sleep(0.05)
        
        # Verificar se thread ainda é válida
        if my_thread_id != thread_id:
            logger.info(f"❌ THREAD {my_thread_id} FINALIZADA")
            return
            
        # Estatísticas do ciclo
        logger.info(f"✅ CICLO {cycle_count} COMPLETO:")
        logger.info(f"   📊 Volume: {cycle_alerts['volume']}, RSI: {cycle_alerts['rsi']}, EMA: {cycle_alerts['ema']}, MACD: {cycle_alerts['macd']}")
        logger.info(f"   🔄 Símbolos processados: {processed_count}")
        logger.info(f"   ⏱️  Próxima verificação: {check_interval}s")
        logger.info("=" * 70)
        
        # Emitir estatísticas
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
        
        # Espera adaptativa
        time.sleep(check_interval)

@app.route('/')
def index():
    return render_template('index.html', 
                         TIMEFRAME_CONFIG=TIMEFRAME_CONFIG,
                         EMA_CONFIGS=EMA_CONFIGS)

@app.route('/status')
def status():
    """Rota para verificar status do monitoramento"""
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
        'stats': stats
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
        
        # Verificar se há símbolos disponíveis
        symbols = get_trading_symbols()
        if not symbols:
            return "Erro: Nenhum símbolo disponível para monitoramento.", 500
        
        # Parar monitoramento atual
        stop_monitoring = True
        thread_id += 1
        new_thread_id = thread_id
        
        logger.info(f"🛑 Parando threads antigas. Nova thread: {new_thread_id}")
        time.sleep(1.0)
        
        # Reiniciar monitoramento
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
    logger.info("🛑 Monitoramento parado pelo usuário")
    return "Monitoramento parado", 200

@app.route('/alerts')
def get_alerts():
    """Rota para obter histórico de alertas"""
    return jsonify(alert_history)

@app.route('/stats')
def get_stats():
    """Rota para obter estatísticas"""
    return jsonify(stats)

@socketio.on('connect')
def handle_connect():
    logger.info('✅ Cliente conectado via WebSocket')
    emit('connection_status', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info('❌ Cliente desconectado via WebSocket')

@socketio.on('request_status')
def handle_status_request():
    """Handle de requisição de status via WebSocket"""
    ema_name = EMA_CONFIGS.get(current_ema_type, {}).get('name', '') if current_ema_type else ''
    symbols = get_trading_symbols()
    
    emit('status_update', {
        'monitoring_active': not stop_monitoring,
        'current_timeframe': current_timeframe,
        'current_multiple': current_multiple,
        'current_ema_type': current_ema_type,
        'current_ema_name': ema_name,
        'total_symbols': len(symbols),
        'stats': stats
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    # Testar carregamento de símbolos na inicialização
    symbols = get_trading_symbols()
    logger.info(f"🚀 Iniciando Whale Detector Pro na porta {port}")
    logger.info(f"📊 {len(symbols)} símbolos carregados")
    logger.info(f"🔧 Modo: {'Desenvolvimento' if debug_mode else 'Produção'}")
    
    if debug_mode:
        socketio.run(app, debug=True, host='0.0.0.0', port=port, use_reloader=True, allow_unsafe_werkzeug=True)
    else:
        socketio.run(app, debug=False, host='0.0.0.0', port=port, use_reloader=False)