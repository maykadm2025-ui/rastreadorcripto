from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit
from binance.client import Client
import time
import os
from datetime import datetime
import math
import threading
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'whale_detector_secret_production')

# Configurar SocketIO com async_mode='threading' para produção
socketio = SocketIO(
    app, 
    async_mode='threading',
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False
)

# Inicializar cliente Binance com tratamento de erro
try:
    client = Client()
    print("✅ Cliente Binance inicializado com sucesso")
except Exception as e:
    print(f"❌ Erro ao inicializar cliente Binance: {e}")
    client = None

# Obter símbolos USDT - com fallback
try:
    if client:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols'] 
                   if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        # Limitar para os 50 principais símbolos para performance
        major_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'ADAUSDT', 
                        'XRPUSDT', 'DOTUSDT', 'DOGEUSDT', 'AVAXUSDT', 'MATICUSDT',
                        'LTCUSDT', 'LINKUSDT', 'ATOMUSDT', 'XLMUSDT', 'BCHUSDT',
                        'FILUSDT', 'ETCUSDT', 'XTZUSDT', 'EOSUSDT', 'AAVEUSDT']
        symbols = [s for s in symbols if s in major_symbols] or major_symbols
        print(f"✅ Carregados {len(symbols)} símbolos USDT para monitoramento.")
    else:
        # Fallback para símbolos principais
        symbols = ['BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 
                  'DOTUSDT', 'DOGEUSDT', 'AVAXUSDT', 'MATICUSDT']
        print(f"⚠️ Usando {len(symbols)} símbolos principais (fallback)")
except Exception as e:
    print(f"❌ Erro ao carregar símbolos: {e}")
    symbols = ['BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'BNBUSDT', 'SOLUSDT']
    print(f"⚠️ Usando {len(symbols)} símbolos básicos (fallback de emergência)")

# Dicionário para intervalos de verificação otimizados
timeframe_to_check_interval = {
    '1m': 15,     # Verificar a cada 15 segundos para 1m
    '3m': 20,     # Verificar a cada 20 segundos para 3m
    '5m': 25,     # Verificar a cada 25 segundos para 5m
    '15m': 35,    # Verificar a cada 35 segundos para 15m
    '30m': 50,    # Verificar a cada 50 segundos para 30m
    '1h': 60,     # Verificar a cada 60 segundos para 1h
    '2h': 120,    # Verificar a cada 120 segundos para 2h
    '4h': 180,    # Verificar a cada 180 segundos para 4h
    '6h': 240,    # Verificar a cada 240 segundos para 6h
    '8h': 300,    # Verificar a cada 300 segundos para 8h
    '12h': 360,   # Verificar a cada 360 segundos para 12h
    '1d': 600     # Verificar a cada 600 segundos para 1d
}

# Configurações de EMA disponíveis
EMA_CONFIGS = {
    'ema_7_21': {'fast': 7, 'slow': 21, 'name': 'EMA 7x21 (Curto Prazo)'},
    'ema_20_50': {'fast': 20, 'slow': 50, 'name': 'EMA 20x50 (Swing Trade)'},
    'ema_50_200': {'fast': 50, 'slow': 200, 'name': 'EMA 50x200 (Golden Cross)'}
}

# Variáveis globais para controle de thread
monitoring_thread = None
stop_monitoring = False
current_timeframe = None
current_multiple = None
current_ema_type = None
thread_id = 0

# Dicionário para acompanhar o último timestamp processado por símbolo
last_processed = {}

def calculate_rsi(prices, period=14):
    """Calcula o RSI - Versão simplificada e robusta"""
    if len(prices) < period + 1:
        return []
    
    try:
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(delta, 0) for delta in deltas]
        losses = [max(-delta, 0) for delta in deltas]
        
        if len(gains) < period:
            return []
            
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        rsi_values = []
        
        if avg_loss == 0:
            rsi_values.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100 - (100 / (1 + rs)))
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            if avg_loss == 0:
                rsi_values.append(100)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(100 - (100 / (1 + rs)))
        
        result = [50.0] * period
        result.extend(rsi_values)
        return result[-len(prices):]
        
    except Exception as e:
        print(f"❌ Erro no cálculo RSI: {e}")
        return [50.0] * len(prices)

def calculate_ema(prices, period):
    """Calcula EMA para uma lista de preços"""
    if len(prices) < period:
        return [prices[0]] * len(prices) if prices else []
    
    multiplier = 2 / (period + 1)
    sma_initial = sum(prices[:period]) / period
    ema_values = [None] * (period - 1) + [sma_initial]
    
    for i in range(period, len(prices)):
        ema = (prices[i] * multiplier) + (ema_values[-1] * (1 - multiplier))
        ema_values.append(ema)
    
    for i in range(period - 1):
        ema_values[i] = prices[0]
    
    return ema_values

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    """Calcula MACD - Versão simplificada"""
    if len(prices) < max(fast_period, slow_period, signal_period):
        return [], []
    
    try:
        ema_fast = calculate_ema(prices, fast_period)
        ema_slow = calculate_ema(prices, slow_period)
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(ema_fast))]
        signal_line = calculate_ema(macd_line, signal_period)
        return macd_line, signal_line
    except Exception as e:
        print(f"❌ Erro no cálculo MACD: {e}")
        return [0.0] * len(prices), [0.0] * len(prices)

def safe_binance_request(func, *args, **kwargs):
    """Wrapper seguro para requests da Binance"""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"❌ Erro na requisição Binance: {e}")
        return None

def monitor_whales(timeframe, multiple, ema_type, my_thread_id):
    global stop_monitoring, current_timeframe, current_multiple, current_ema_type, thread_id, last_processed
    
    print(f"🚀 THREAD {my_thread_id} INICIADA:")
    print(f"   - Timeframe: {timeframe}")
    print(f"   - Múltiplo mínimo: {multiple}")
    print(f"   - EMA Type: {ema_type} ({EMA_CONFIGS[ema_type]['name']})")
    
    if my_thread_id != thread_id:
        print(f"❌ THREAD {my_thread_id} CANCELADA - Nova thread {thread_id} iniciada")
        return
    
    current_timeframe = timeframe
    current_multiple = multiple
    current_ema_type = ema_type
    ema_config = EMA_CONFIGS[ema_type]
    fast_period = ema_config['fast']
    slow_period = ema_config['slow']
    
    check_interval = timeframe_to_check_interval.get(timeframe, 20)
    
    socketio.emit('monitoring_started', {
        'status': 'Iniciado', 
        'timeframe': timeframe, 
        'multiple': multiple,
        'ema_type': ema_type,
        'ema_name': ema_config['name']
    })
    
    cycle_count = 0
    while not stop_monitoring and my_thread_id == thread_id:
        cycle_count += 1
        alerts_count = 0
        rsi_alerts = 0
        ema_alerts = 0
        macd_alerts = 0
        processed_count = 0
        
        print(f"🔍 THREAD {my_thread_id} - CICLO {cycle_count} - Múltiplo: {multiple} - EMA: {ema_config['name']}")
        
        for symbol in symbols:
            if stop_monitoring or my_thread_id != thread_id:
                print(f"❌ THREAD {my_thread_id} INTERROMPIDA")
                return
                
            try:
                current_time = time.time()
                if symbol in last_processed and (current_time - last_processed[symbol]) < check_interval/2:
                    continue
                
                last_processed[symbol] = current_time
                
                # Usar wrapper seguro para request Binance
                klines = safe_binance_request(client.get_klines, symbol=symbol, interval=timeframe, limit=100)
                if not klines or len(klines) < 50:
                    continue
                
                closes = [float(k[4]) for k in klines]
                processed_count += 1
                
                # 1. VERIFICAÇÃO RSI
                try:
                    rsi_values = calculate_rsi(closes, period=14)
                    if len(rsi_values) >= 2:
                        current_rsi = rsi_values[-1]
                        previous_rsi = rsi_values[-2]
                        
                        if previous_rsi <= 30 and current_rsi > 31:
                            rsi_alert = {
                                'type': 'RSI',
                                'crypto': symbol.replace('USDT', ''),
                                'previous_rsi': round(previous_rsi, 2),
                                'current_rsi': round(current_rsi, 2),
                                'timestamp': datetime.now().strftime('%H:%M:%S'),
                                'timeframe': timeframe,
                                'thread_id': my_thread_id
                            }
                            socketio.emit('indicator_alert', rsi_alert)
                            rsi_alerts += 1
                            print(f"🚨📈 RSI ALERT: {symbol} - {previous_rsi:.2f} → {current_rsi:.2f}")
                except Exception as rsi_error:
                    pass
                
                # 2. VERIFICAÇÃO EMA
                try:
                    if len(closes) >= slow_period:  
                        ema_fast_values = calculate_ema(closes, fast_period)
                        ema_slow_values = calculate_ema(closes, slow_period)
                        
                        if (len(ema_fast_values) >= 2 and len(ema_slow_values) >= 2 and
                            not math.isnan(ema_fast_values[-2]) and not math.isnan(ema_slow_values[-2]) and
                            not math.isnan(ema_fast_values[-1]) and not math.isnan(ema_slow_values[-1])):
                            
                            if (ema_fast_values[-2] <= ema_slow_values[-2] and 
                                ema_fast_values[-1] > ema_slow_values[-1]):
                                ema_alert = {
                                    'type': 'EMA',
                                    'crypto': symbol.replace('USDT', ''),
                                    'ema_name': ema_config['name'],
                                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                                    'timeframe': timeframe,
                                    'thread_id': my_thread_id
                                }
                                socketio.emit('indicator_alert', ema_alert)
                                ema_alerts += 1
                                print(f"🚨📈 EMA ALERT ({ema_config['name']}): {symbol}")
                except Exception as ema_error:
                    pass

                # 3. VERIFICAÇÃO MACD
                try:
                    if len(closes) >= 35:
                        macd_line, signal_line = calculate_macd(closes)
                        
                        if (len(macd_line) >= 2 and len(signal_line) >= 2 and
                            not math.isnan(macd_line[-2]) and not math.isnan(signal_line[-2]) and
                            not math.isnan(macd_line[-1]) and not math.isnan(signal_line[-1])):
                            
                            if (macd_line[-2] <= signal_line[-2] and 
                                macd_line[-1] > signal_line[-1]):
                                macd_alert = {
                                    'type': 'MACD',
                                    'crypto': symbol.replace('USDT', ''),
                                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                                    'timeframe': timeframe,
                                    'thread_id': my_thread_id
                                }
                                socketio.emit('indicator_alert', macd_alert)
                                macd_alerts += 1
                                print(f"🚨📈 MACD ALERT: {symbol}")
                except Exception as macd_error:
                    pass

                # 4. VERIFICAÇÃO VOLUME
                try:
                    klines_volume = safe_binance_request(client.get_klines, symbol=symbol, interval=timeframe, limit=21)
                    if not klines_volume or len(klines_volume) < 21:
                        continue
                    
                    current_volume = float(klines_volume[-1][5])
                    historical_volumes = [float(k[5]) for k in klines_volume[-21:-1]]
                    avg_volume = sum(historical_volumes) / len(historical_volumes) if historical_volumes else 0
                    
                    if avg_volume > 0:
                        calculated_multiple = current_volume / avg_volume
                        
                        if calculated_multiple >= multiple and my_thread_id == thread_id:
                            alert = {
                                'crypto': symbol.replace('USDT', ''),
                                'volume': round(current_volume, 2),
                                'avg_volume': round(avg_volume, 2),
                                'multiple': round(calculated_multiple, 2),
                                'timestamp': datetime.now().strftime('%H:%M:%S'),
                                'timeframe': timeframe,
                                'thread_id': my_thread_id
                            }
                            socketio.emit('whale_alert', alert)
                            alerts_count += 1
                            print(f"🚨📊 VOLUME ALERT: {symbol} = {calculated_multiple:.2f}x")
                
                except Exception as volume_error:
                    pass
                    
            except Exception as e:
                print(f"❌ Erro geral {symbol}: {e}")
            
            time.sleep(0.01)
        
        if my_thread_id != thread_id:
            print(f"❌ THREAD {my_thread_id} FINALIZADA")
            return
            
        print(f"✅ CICLO {cycle_count} COMPLETO - Volume: {alerts_count}, RSI: {rsi_alerts}, EMA: {ema_alerts}, MACD: {macd_alerts}")
        print(f"   Símbolos processados: {processed_count}, Próxima verificação: {check_interval}s")
        print("=" * 60)
        
        time.sleep(check_interval)

# Template HTML
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Whale Detector - Binance</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        .alert-fade-in { animation: fadeIn 0.5s ease-in; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
        .pulse-alert { animation: pulse 2s infinite; }
        @keyframes pulse { 
            0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
            70% { box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }
            100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
        }
    </style>
</head>
<body class="bg-gray-900 text-white">
    <div class="container mx-auto px-4 py-8">
        <div class="text-center mb-8">
            <h1 class="text-4xl font-bold text-green-400 mb-2">
                <i class="fas fa-whale mr-3"></i>Whale Detector
            </h1>
            <p class="text-gray-400">Monitoramento em tempo real - Binance</p>
        </div>

        <div class="bg-gray-800 rounded-lg p-6 mb-8">
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-2">Timeframe</label>
                    <select id="timeframe" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white">
                        <option value="1m">1 Minuto</option>
                        <option value="5m">5 Minutos</option>
                        <option value="15m">15 Minutos</option>
                        <option value="1h">1 Hora</option>
                        <option value="4h">4 Horas</option>
                        <option value="1d">1 Dia</option>
                    </select>
                </div>
                
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-2">Múltiplo Mínimo</label>
                    <select id="multiple" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white">
                        <option value="3">3x</option>
                        <option value="5">5x</option>
                        <option value="8">8x</option>
                        <option value="10">10x</option>
                    </select>
                </div>

                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-2">Configuração EMA</label>
                    <select id="ema_type" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white">
                        <option value="ema_7_21">EMA 7x21 (Curto Prazo)</option>
                        <option value="ema_20_50">EMA 20x50 (Swing Trade)</option>
                        <option value="ema_50_200">EMA 50x200 (Golden Cross)</option>
                    </select>
                </div>

                <div class="flex items-end space-x-2">
                    <button id="startBtn" class="flex-1 bg-green-600 hover:bg-green-700 text-white font-bold py-2 px-4 rounded-lg">
                        <i class="fas fa-play mr-2"></i>Iniciar
                    </button>
                    <button id="stopBtn" class="flex-1 bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded-lg">
                        <i class="fas fa-stop mr-2"></i>Parar
                    </button>
                </div>
            </div>
        </div>

        <div id="status" class="bg-gray-800 rounded-lg p-4 mb-8 hidden">
            <div class="flex items-center justify-between">
                <div>
                    <h3 class="text-lg font-semibold text-green-400">
                        <i class="fas fa-circle animate-pulse text-green-500 mr-2"></i>
                        Monitoramento Ativo
                    </h3>
                    <p id="statusDetails" class="text-gray-400 text-sm"></p>
                </div>
                <div id="connectionStatus" class="flex items-center text-green-500">
                    <i class="fas fa-wifi mr-2"></i>
                    <span>Conectado</span>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="bg-gray-800 rounded-lg p-6">
                <h2 class="text-xl font-bold text-yellow-400 mb-4">
                    <i class="fas fa-chart-bar mr-2"></i>Alertas de Volume
                </h2>
                <div id="volumeAlerts" class="space-y-3 max-h-96 overflow-y-auto">
                    <div class="text-gray-500 text-center py-4">Aguardando alertas...</div>
                </div>
            </div>

            <div class="bg-gray-800 rounded-lg p-6">
                <h2 class="text-xl font-bold text-blue-400 mb-4">
                    <i class="fas fa-chart-line mr-2"></i>Alertas de Indicadores
                </h2>
                <div id="indicatorAlerts" class="space-y-3 max-h-96 overflow-y-auto">
                    <div class="text-gray-500 text-center py-4">Aguardando alertas...</div>
                </div>
            </div>
        </div>

        <div class="mt-8 grid grid-cols-1 md:grid-cols-4 gap-4">
            <div class="bg-gray-800 rounded-lg p-4 text-center">
                <div class="text-2xl font-bold text-green-400" id="totalSymbols">0</div>
                <div class="text-gray-400 text-sm">Símbolos</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 text-center">
                <div class="text-2xl font-bold text-yellow-400" id="volumeAlertsCount">0</div>
                <div class="text-gray-400 text-sm">Alertas Volume</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 text-center">
                <div class="text-2xl font-bold text-blue-400" id="indicatorAlertsCount">0</div>
                <div class="text-gray-400 text-sm">Alertas Indicadores</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 text-center">
                <div class="text-2xl font-bold text-purple-400" id="lastUpdate">-</div>
                <div class="text-gray-400 text-sm">Última Atualização</div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let volumeAlertCount = 0;
        let indicatorAlertCount = 0;

        const startBtn = document.getElementById('startBtn');
        const stopBtn = document.getElementById('stopBtn');
        const statusDiv = document.getElementById('status');
        const statusDetails = document.getElementById('statusDetails');
        const volumeAlertsDiv = document.getElementById('volumeAlerts');
        const indicatorAlertsDiv = document.getElementById('indicatorAlerts');
        const totalSymbolsSpan = document.getElementById('totalSymbols');
        const volumeAlertsCountSpan = document.getElementById('volumeAlertsCount');
        const indicatorAlertsCountSpan = document.getElementById('indicatorAlertsCount');
        const lastUpdateSpan = document.getElementById('lastUpdate');

        startBtn.addEventListener('click', () => {
            const timeframe = document.getElementById('timeframe').value;
            const multiple = document.getElementById('multiple').value;
            const emaType = document.getElementById('ema_type').value;

            fetch('/start_monitoring', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: `timeframe=${timeframe}&multiple=${multiple}&ema_type=${emaType}`
            }).then(response => response.text()).then(data => {
                console.log('Iniciado:', data);
            }).catch(error => {
                console.error('Erro:', error);
            });
        });

        stopBtn.addEventListener('click', () => {
            fetch('/stop_monitoring', {method: 'POST'})
            .then(response => response.text())
            .then(data => console.log('Parado:', data))
            .catch(error => console.error('Erro:', error));
        });

        socket.on('connect', () => {
            console.log('Conectado ao servidor');
            document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-wifi mr-2"></i><span>Conectado</span>';
        });

        socket.on('disconnect', () => {
            console.log('Desconectado');
            document.getElementById('connectionStatus').innerHTML = '<i class="fas fa-wifi mr-2"></i><span class="text-red-500">Desconectado</span>';
        });

        socket.on('monitoring_started', (data) => {
            statusDiv.classList.remove('hidden');
            statusDetails.innerHTML = `Timeframe: ${data.timeframe} | Múltiplo: ${data.multiple}x | EMA: ${data.ema_name}`;
        });

        socket.on('monitoring_stopped', (data) => {
            statusDiv.classList.add('hidden');
        });

        socket.on('whale_alert', (alert) => {
            volumeAlertCount++;
            addVolumeAlert(alert);
            updateStats();
        });

        socket.on('indicator_alert', (alert) => {
            indicatorAlertCount++;
            addIndicatorAlert(alert);
            updateStats();
        });

        function addVolumeAlert(alert) {
            const alertElement = document.createElement('div');
            alertElement.className = 'alert-fade-in pulse-alert bg-yellow-900 border-l-4 border-yellow-500 p-3 rounded';
            alertElement.innerHTML = `
                <div class="flex justify-between items-start">
                    <div>
                        <div class="font-bold text-yellow-300 text-lg">${alert.crypto}</div>
                        <div class="text-yellow-200 text-sm">Volume: ${formatNumber(alert.volume)}</div>
                        <div class="text-gray-300 text-xs">Média: ${formatNumber(alert.avg_volume)} (${alert.multiple}x)</div>
                    </div>
                    <div class="text-right">
                        <div class="text-yellow-400 font-bold">${alert.multiple}x</div>
                        <div class="text-gray-400 text-xs">${alert.timestamp}</div>
                    </div>
                </div>
                <div class="text-gray-400 text-xs mt-1">${alert.timeframe}</div>
            `;

            if (volumeAlertsDiv.firstChild?.className?.includes('text-gray-500')) {
                volumeAlertsDiv.innerHTML = '';
            }
            volumeAlertsDiv.insertBefore(alertElement, volumeAlertsDiv.firstChild);

            if (volumeAlertsDiv.children.length > 20) {
                volumeAlertsDiv.removeChild(volumeAlertsDiv.lastChild);
            }
        }

        function addIndicatorAlert(alert) {
            const colors = {RSI: 'green', EMA: 'purple', MACD: 'pink'};
            const color = colors[alert.type] || 'blue';
            
            const alertElement = document.createElement('div');
            alertElement.className = `alert-fade-in pulse-alert bg-${color}-900 border-l-4 border-${color}-500 p-3 rounded`;
            alertElement.innerHTML = `
                <div class="flex justify-between items-start">
                    <div>
                        <div class="font-bold text-${color}-300 text-lg">${alert.crypto}</div>
                        <div class="text-${color}-200 text-sm">${alert.type}</div>
                        ${alert.ema_name ? `<div class="text-gray-300 text-xs">${alert.ema_name}</div>` : ''}
                    </div>
                    <div class="text-right">
                        <div class="text-${color}-400 font-bold">${alert.type}</div>
                        <div class="text-gray-400 text-xs">${alert.timestamp}</div>
                    </div>
                </div>
                <div class="text-gray-400 text-xs mt-1">${alert.timeframe}</div>
            `;

            if (indicatorAlertsDiv.firstChild?.className?.includes('text-gray-500')) {
                indicatorAlertsDiv.innerHTML = '';
            }
            indicatorAlertsDiv.insertBefore(alertElement, indicatorAlertsDiv.firstChild);

            if (indicatorAlertsDiv.children.length > 20) {
                indicatorAlertsDiv.removeChild(indicatorAlertsDiv.lastChild);
            }
        }

        function formatNumber(num) {
            if (num >= 1000000) return (num / 1000000).toFixed(2) + 'M';
            if (num >= 1000) return (num / 1000).toFixed(2) + 'K';
            return num.toFixed(2);
        }

        function updateStats() {
            volumeAlertsCountSpan.textContent = volumeAlertCount;
            indicatorAlertsCountSpan.textContent = indicatorAlertCount;
            lastUpdateSpan.textContent = new Date().toLocaleTimeString();
        }

        updateStats();
        
        fetch('/status')
            .then(response => response.json())
            .then(data => {
                totalSymbolsSpan.textContent = data.total_symbols;
                if (data.monitoring_active) {
                    showStatus(data);
                }
            });
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/status')
def status():
    ema_name = EMA_CONFIGS.get(current_ema_type, {}).get('name', '') if current_ema_type else ''
    return {
        'monitoring_active': not stop_monitoring,
        'current_timeframe': current_timeframe,
        'current_multiple': current_multiple,
        'current_ema_type': current_ema_type,
        'current_ema_name': ema_name,
        'total_symbols': len(symbols),
        'monitoring_thread_active': monitoring_thread is not None,
        'current_thread_id': thread_id
    }

@app.route('/start_monitoring', methods=['POST'])
def start_monitoring():
    global monitoring_thread, stop_monitoring, thread_id, current_ema_type
    try:
        timeframe = request.form['timeframe']
        multiple = float(request.form['multiple'])
        ema_type = request.form.get('ema_type', 'ema_7_21')
        
        if timeframe not in timeframe_to_check_interval:
            return "Timeframe inválido.", 400
        
        if ema_type not in EMA_CONFIGS:
            return "Tipo de EMA inválido.", 400
        
        stop_monitoring = True
        thread_id += 1
        new_thread_id = thread_id
        
        print(f"🛑 Parando threads antigas. Nova thread: {new_thread_id}")
        time.sleep(1.0)
        
        stop_monitoring = False
        
        monitoring_thread = threading.Thread(
            target=monitor_whales, 
            args=(timeframe, multiple, ema_type, new_thread_id),
            daemon=True
        )
        monitoring_thread.start()
        
        print(f"✅ Thread {new_thread_id} iniciada")
        return f"Monitoramento iniciado - Thread {new_thread_id}", 200
        
    except Exception as e:
        print(f"Erro ao iniciar monitoramento: {e}")
        return f"Erro: {str(e)}", 500

@app.route('/stop_monitoring', methods=['POST'])
def stop_monitoring_route():
    global stop_monitoring, monitoring_thread, thread_id, current_ema_type
    
    print("🛑 Parando monitoramento")
    stop_monitoring = True
    thread_id += 1
    
    monitoring_thread = None
    current_ema_type = None
    socketio.emit('monitoring_stopped', {'status': 'Parado'})
    print("✅ Monitoramento parado")
    
    return "Monitoramento parado.", 200

@socketio.on('connect')
def handle_connect():
    print('Cliente conectado')
    if current_timeframe and current_multiple:
        emit('current_status', {
            'monitoring': not stop_monitoring,
            'timeframe': current_timeframe,
            'multiple': current_multiple,
            'ema_type': current_ema_type,
            'ema_name': EMA_CONFIGS.get(current_ema_type, {}).get('name', '') if current_ema_type else ''
        })

@socketio.on('disconnect')
def handle_disconnect():
    print('Cliente desconectado')

@app.route('/health')
def health_check():
    return {'status': 'healthy', 'symbols': len(symbols)}, 200

# Para produção, usar Gunicorn
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print(f"🚀 Iniciando Whale Detector na porta {port}...")
    print(f"📊 Símbolos carregados: {len(symbols)}")
    
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    # Para desenvolvimento local apenas
    if debug_mode:
        socketio.run(
            app, 
            debug=True, 
            host='0.0.0.0', 
            port=port, 
            use_reloader=False,
            allow_unsafe_werkzeug=True
        )
    else:
        # Em produção, usar Gunicorn
        print("✅ Aplicação pronta para produção com Gunicorn")