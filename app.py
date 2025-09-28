# Importante: eventlet.monkey_patch() deve ser a PRIMEIRA linha antes de qualquer import
import eventlet
eventlet.monkey_patch(all=True, thread=False)  # thread=False para evitar conflitos

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from binance.client import Client
import time
import os
from datetime import datetime
import numpy as np

app = Flask(__name__)
app.config['SECRET_KEY'] = 'whale_detector_secret'

# Configurar SocketIO com configurações otimizadas para produção
socketio = SocketIO(
    app, 
    async_mode='eventlet', 
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False
)

client = Client()  # Cliente público da Binance, sem API key

# Obter todos os símbolos USDT spot em trading
try:
    exchange_info = client.get_exchange_info()
    symbols = [s['symbol'] for s in exchange_info['symbols'] 
               if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    print(f"Carregados {len(symbols)} símbolos USDT para monitoramento.")
except Exception as e:
    print(f"Erro ao carregar símbolos: {e}")
    symbols = []

# Dicionário para intervalos de verificação otimizados
timeframe_to_check_interval = {
    '1m': 10,     # Verificar a cada 10 segundos para 1m
    '3m': 15,     # Verificar a cada 15 segundos para 3m
    '5m': 20,     # Verificar a cada 20 segundos para 5m
    '15m': 30,    # Verificar a cada 30 segundos para 15m
    '30m': 45,    # Verificar a cada 45 segundos para 30m
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
thread_id = 0  # ID único para cada thread

# Dicionário para acompanhar o último timestamp processado por símbolo
last_processed = {}

def calculate_rsi(prices, period=14):
    """Calcula o RSI - Versão simplificada e robusta"""
    if len(prices) < period + 1:
        return []
    
    try:
        # Calcular mudanças de preço
        deltas = []
        for i in range(1, len(prices)):
            deltas.append(prices[i] - prices[i-1])
        
        # Separar ganhos e perdas
        gains = []
        losses = []
        
        for delta in deltas:
            if delta > 0:
                gains.append(delta)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(-delta)
        
        # Calcular médias móveis simples iniciais
        if len(gains) < period:
            return []
            
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        rsi_values = []
        
        # Primeira valor RSI
        if avg_loss == 0:
            rs = 0
        else:
            rs = avg_gain / avg_loss
        rsi_values.append(100 - (100 / (1 + rs)))
        
        # Calcular RSI para os valores restantes usando Wilder's smoothing
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            if avg_loss == 0:
                rs = 0
            else:
                rs = avg_gain / avg_loss
            rsi_values.append(100 - (100 / (1 + rs)))
        
        # Preencher os valores iniciais com None para manter o tamanho correto
        result = [50.0] * period  # Valores neutros para as primeiras posições
        result.extend(rsi_values)
        
        return result[-len(prices):]  # Retornar apenas o tamanho dos preços
        
    except Exception as e:
        print(f"❌ Erro no cálculo RSI: {e}")
        return [50.0] * len(prices)  # Valores neutros em caso de erro

def calculate_ema(prices, period):
    """Calcula EMA para uma lista de preços - Implementação sem pandas"""
    if len(prices) < period:
        return [prices[0]] * len(prices) if prices else []
    
    # Calcular o multiplicador (smoothing factor)
    multiplier = 2 / (period + 1)
    
    # Inicializar EMA com a média simples dos primeiros 'period' valores
    sma_initial = sum(prices[:period]) / period
    ema_values = [None] * (period - 1) + [sma_initial]
    
    # Calcular EMA para o restante dos valores
    for i in range(period, len(prices)):
        ema = (prices[i] * multiplier) + (ema_values[-1] * (1 - multiplier))
        ema_values.append(ema)
    
    # Preencher os valores iniciais com o primeiro preço para manter consistência
    for i in range(period - 1):
        ema_values[i] = prices[0]
    
    return ema_values

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    """Calcula MACD - Versão simplificada e robusta"""
    if len(prices) < max(fast_period, slow_period, signal_period):
        return [], []
    
    try:
        # Calcular EMAs rápida e lenta
        ema_fast = calculate_ema(prices, fast_period)
        ema_slow = calculate_ema(prices, slow_period)
        
        # Calcular linha MACD
        macd_line = []
        for i in range(len(ema_fast)):
            macd_line.append(ema_fast[i] - ema_slow[i])
        
        # Calcular linha de sinal (EMA do MACD)
        signal_line = calculate_ema(macd_line, signal_period)
        
        return macd_line, signal_line
        
    except Exception as e:
        print(f"❌ Erro no cálculo MACD: {e}")
        return [0.0] * len(prices), [0.0] * len(prices)

def monitor_whales(timeframe, multiple, ema_type, my_thread_id):
    global stop_monitoring, current_timeframe, current_multiple, current_ema_type, thread_id, last_processed
    
    print(f"🚀 THREAD {my_thread_id} INICIADA:")
    print(f"   - Timeframe: {timeframe}")
    print(f"   - Múltiplo mínimo: {multiple}")
    print(f"   - EMA Type: {ema_type} ({EMA_CONFIGS[ema_type]['name']})")
    
    # Verificar se esta thread ainda é válida
    if my_thread_id != thread_id:
        print(f"❌ THREAD {my_thread_id} CANCELADA - Nova thread {thread_id} iniciada")
        return
    
    current_timeframe = timeframe
    current_multiple = multiple
    current_ema_type = ema_type
    ema_config = EMA_CONFIGS[ema_type]
    fast_period = ema_config['fast']
    slow_period = ema_config['slow']
    
    check_interval = timeframe_to_check_interval.get(timeframe, 20)  # Padrão de 20 segundos
    
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
            # VERIFICAR A CADA SÍMBOLO se ainda é a thread ativa
            if stop_monitoring or my_thread_id != thread_id:
                print(f"❌ THREAD {my_thread_id} INTERROMPIDA")
                return
                
            try:
                # Verificar se precisamos processar este símbolo (evitar processamento excessivo)
                current_time = time.time()
                if symbol in last_processed and (current_time - last_processed[symbol]) < check_interval/2:
                    continue
                
                last_processed[symbol] = current_time
                
                # Fetch mais velas para cálculos de indicadores
                klines = client.get_klines(symbol=symbol, interval=timeframe, limit=100)
                if len(klines) < 50:  # Mínimo necessário para cálculos
                    continue
                
                # Extrair preços de fechamento
                closes = [float(k[4]) for k in klines]
                
                processed_count += 1
                
                # Debug para alguns símbolos específicos
                is_debug_symbol = symbol in ['BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'BNBUSDT', 'SOLUSDT']
                
                # =========================
                # 1. VERIFICAÇÃO RSI
                # =========================
                try:
                    rsi_values = calculate_rsi(closes, period=14)
                    
                    if len(rsi_values) >= 2:
                        current_rsi = rsi_values[-1]
                        previous_rsi = rsi_values[-2]
                        
                        # CONDIÇÃO RSI: RSI anterior <= 30 E RSI atual > 31
                        if previous_rsi <= 30 and current_rsi > 31:
                            rsi_alert = {
                                'type': 'RSI',
                                'crypto': symbol.replace('USDT', ''),
                                'previous_rsi': round(previous_rsi, 2),
                                'current_rsi': round(current_rsi, 2),
                                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'timeframe': timeframe,
                                'thread_id': my_thread_id
                            }
                            socketio.emit('indicator_alert', rsi_alert)
                            rsi_alerts += 1
                            print(f"🚨📈 RSI ALERT: {symbol} - Anterior: {previous_rsi:.2f} → Atual: {current_rsi:.2f}")
                        
                        # Debug adicional para valores baixos
                        if (previous_rsi <= 35 or current_rsi <= 35) and is_debug_symbol:
                            print(f"⚠️ RSI BAIXO {symbol}: Anterior={previous_rsi:.2f}, Atual={current_rsi:.2f}")
                    
                    else:
                        if is_debug_symbol:
                            print(f"❌ DEBUG RSI {symbol}: Dados insuficientes - {len(rsi_values)} valores")
                    
                except Exception as rsi_error:
                    print(f"❌ Erro RSI {symbol}: {rsi_error}")
                
                # =========================
                # 2. VERIFICAÇÃO EMA
                # =========================
                try:
                    # Garantir dados suficientes para a EMA mais lenta
                    if len(closes) >= slow_period:  
                        ema_fast_values = calculate_ema(closes, fast_period)
                        ema_slow_values = calculate_ema(closes, slow_period)
                        
                        if len(ema_fast_values) >= 2 and len(ema_slow_values) >= 2:
                            current_ema_fast = ema_fast_values[-1]
                            previous_ema_fast = ema_fast_values[-2]
                            current_ema_slow = ema_slow_values[-1]
                            previous_ema_slow = ema_slow_values[-2]
                            
                            # CONDIÇÃO EMA: EMA rápida anterior <= EMA lenta anterior E EMA rápida atual > EMA lenta atual
                            if (not np.isnan(previous_ema_fast) and not np.isnan(previous_ema_slow) and 
                                not np.isnan(current_ema_fast) and not np.isnan(current_ema_slow)):
                                
                                if previous_ema_fast <= previous_ema_slow and current_ema_fast > current_ema_slow:
                                    ema_alert = {
                                        'type': 'EMA',
                                        'crypto': symbol.replace('USDT', ''),
                                        'previous_ema_fast': round(previous_ema_fast, 6),
                                        'previous_ema_slow': round(previous_ema_slow, 6),
                                        'current_ema_fast': round(current_ema_fast, 6),
                                        'current_ema_slow': round(current_ema_slow, 6),
                                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        'timeframe': timeframe,
                                        'ema_type': ema_type,
                                        'ema_name': ema_config['name'],
                                        'thread_id': my_thread_id
                                    }
                                    socketio.emit('indicator_alert', ema_alert)
                                    ema_alerts += 1
                                    print(f"🚨📈 EMA ALERT ({ema_config['name']}): {symbol}")
                                    print(f"   Anterior: EMA{fast_period}({previous_ema_fast:.6f}) <= EMA{slow_period}({previous_ema_slow:.6f})")
                                    print(f"   Atual: EMA{fast_period}({current_ema_fast:.6f}) > EMA{slow_period}({current_ema_slow:.6f})")
                
                except Exception as ema_error:
                    if is_debug_symbol:
                        print(f"❌ Erro EMA {symbol}: {ema_error}")

                # =========================
                # 3. VERIFICAÇÃO MACD
                # =========================
                try:
                    # Garantir dados suficientes para MACD (precisa de pelo menos 26 + 9 = 35 períodos)
                    if len(closes) >= 35:
                        macd_line, signal_line = calculate_macd(closes)
                        
                        if len(macd_line) >= 2 and len(signal_line) >= 2:
                            current_macd = macd_line[-1]
                            previous_macd = macd_line[-2]
                            current_signal = signal_line[-1]
                            previous_signal = signal_line[-2]
                            
                            # CONDIÇÃO MACD: MACD anterior <= Signal anterior E MACD atual > Signal atual
                            if (not np.isnan(previous_macd) and not np.isnan(previous_signal) and 
                                not np.isnan(current_macd) and not np.isnan(current_signal)):
                                
                                if previous_macd <= previous_signal and current_macd > current_signal:
                                    macd_alert = {
                                        'type': 'MACD',
                                        'crypto': symbol.replace('USDT', ''),
                                        'previous_macd': round(previous_macd, 8),
                                        'previous_signal': round(previous_signal, 8),
                                        'current_macd': round(current_macd, 8),
                                        'current_signal': round(current_signal, 8),
                                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        'timeframe': timeframe,
                                        'thread_id': my_thread_id
                                    }
                                    socketio.emit('indicator_alert', macd_alert)
                                    macd_alerts += 1
                                    print(f"🚨📈 MACD ALERT: {symbol}")
                                    print(f"   Anterior: MACD({previous_macd:.8f}) <= Signal({previous_signal:.8f})")
                                    print(f"   Atual: MACD({current_macd:.8f}) > Signal({current_signal:.8f})")
                                
                                # Debug adicional para valores próximos
                                if abs(current_macd - current_signal) < abs(previous_macd - previous_signal) * 0.1 and is_debug_symbol:
                                    print(f"⚠️ MACD PRÓXIMO {symbol}: MACD={current_macd:.8f}, Signal={current_signal:.8f}")
                
                except Exception as macd_error:
                    if is_debug_symbol:
                        print(f"❌ Erro MACD {symbol}: {macd_error}")

                # =========================
                # 4. VERIFICAÇÃO VOLUME
                # =========================
                try:
                    # Buscar dados específicos para volume
                    klines_volume = client.get_klines(symbol=symbol, interval=timeframe, limit=21)
                    if len(klines_volume) < 21:
                        continue
                    
                    # Usar a vela mais recente para volume atual
                    current_volume = float(klines_volume[-1][5])
                    
                    # Calcular média das últimas 20 velas (excluindo a atual)
                    historical_volumes = [float(k[5]) for k in klines_volume[-21:-1]]
                    
                    # Média das velas HISTÓRICAS
                    avg_volume = sum(historical_volumes) / len(historical_volumes) if historical_volumes else 0
                    
                    if avg_volume > 0:
                        calculated_multiple = current_volume / avg_volume
                        
                        # Debug volume para alguns símbolos
                        if is_debug_symbol and cycle_count % 5 == 0:  # Debug a cada 5 ciclos
                            print(f"🔍 DEBUG VOLUME {symbol}: Atual={current_volume:.0f}, Média={avg_volume:.0f}, Múltiplo={calculated_multiple:.2f}")
                        
                        # CONDIÇÃO VOLUME: Volume atual > múltiplo * média
                        if calculated_multiple >= multiple and my_thread_id == thread_id:
                            alert = {
                                'crypto': symbol.replace('USDT', ''),
                                'volume': round(current_volume, 2),
                                'avg_volume': round(avg_volume, 2),
                                'multiple': round(calculated_multiple, 2),
                                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'timeframe': timeframe,
                                'thread_id': my_thread_id
                            }
                            socketio.emit('whale_alert', alert)
                            alerts_count += 1
                            print(f"🚨📊 VOLUME ALERT: {symbol} = {calculated_multiple:.2f}x (mín: {multiple}x)")
                
                except Exception as volume_error:
                    if is_debug_symbol:
                        print(f"❌ Erro VOLUME {symbol}: {volume_error}")
                    
            except Exception as e:
                print(f"❌ Erro geral thread {my_thread_id} - {symbol}: {e}")
                eventlet.sleep(0.1)
            
            eventlet.sleep(0.01)  # Pequena pausa entre símbolos
        
        # Verificar se ainda é thread ativa antes de esperar
        if my_thread_id != thread_id:
            print(f"❌ THREAD {my_thread_id} FINALIZADA - Nova thread ativa")
            return
            
        print(f"✅ THREAD {my_thread_id} - CICLO {cycle_count} COMPLETO:")
        print(f"   - Alertas Volume: {alerts_count} (múltiplo {multiple})")
        print(f"   - Alertas RSI: {rsi_alerts}")
        print(f"   - Alertas EMA: {ema_alerts}")
        print(f"   - Alertas MACD: {macd_alerts}")
        print(f"   - Símbolos processados: {processed_count}")
        print(f"   - Próxima verificação: {check_interval}s")
        print("=" * 60)
        
        # Espera adaptativa baseada no timeframe
        eventlet.sleep(check_interval)

@app.route('/')
def index():
    return render_template('index.html', 
                         timeframes=list(timeframe_to_check_interval.keys()),
                         ema_configs=EMA_CONFIGS)

@app.route('/status')
def status():
    """Rota para verificar status do monitoramento"""
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
        ema_type = request.form.get('ema_type', 'ema_7_21')  # Default para EMA 7x21
        
        print(f"🔧 NOVO PEDIDO:")
        print(f"   - Timeframe: {timeframe}")
        print(f"   - Multiple: {multiple}")
        print(f"   - EMA Type: {ema_type} ({EMA_CONFIGS.get(ema_type, {}).get('name', 'Desconhecido')})")
        
        if timeframe not in timeframe_to_check_interval:
            return "Timeframe inválido.", 400
        
        if ema_type not in EMA_CONFIGS:
            return "Tipo de EMA inválido.", 400
        
        # PARAR TUDO IMEDIATAMENTE
        stop_monitoring = True
        thread_id += 1  # Incrementar ID para invalidar threads antigas
        new_thread_id = thread_id
        
        print(f"🛑 PARANDO TODAS AS THREADS ANTIGAS")
        print(f"🆔 NOVO THREAD ID: {new_thread_id}")
        
        # Aguardar threads antigas pararem
        eventlet.sleep(1.0)  
        
        # RESETAR controle
        stop_monitoring = False
        
        # INICIAR NOVA THREAD com ID único
        monitoring_thread = eventlet.spawn(monitor_whales, timeframe, multiple, ema_type, new_thread_id)
        
        print(f"✅ THREAD {new_thread_id} INICIADA COM MÚLTIPLO {multiple} e EMA {ema_type}")
        return f"Monitoramento iniciado - Thread {new_thread_id}", 200
        
    except Exception as e:
        print(f"Erro ao iniciar monitoramento: {e}")
        return f"Erro: {str(e)}", 500

@app.route('/stop_monitoring', methods=['POST'])
def stop_monitoring_route():
    global stop_monitoring, monitoring_thread, thread_id, current_ema_type
    
    print(f"🛑 COMANDO DE PARADA RECEBIDO")
    
    # Parar todas as threads
    stop_monitoring = True
    thread_id += 1  # Invalidar todas as threads existentes
    
    if monitoring_thread:
        try:
            monitoring_thread.kill()
            print("Thread principal encerrada")
        except:
            pass
        monitoring_thread = None
    
    current_ema_type = None
    socketio.emit('monitoring_stopped', {'status': 'Parado'})
    print(f"✅ MONITORAMENTO PARADO - Novo thread_id: {thread_id}")
    
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

# Adicionar rota de health check para Koyeb
@app.route('/health')
def health_check():
    return {'status': 'healthy', 'symbols': len(symbols)}, 200

if __name__ == '__main__':
    # Usar porta do ambiente ou padrão 8080
    port = int(os.environ.get('PORT', 8080))
    print(f"Iniciando servidor Flask-SocketIO na porta {port}...")
    
    # Em produção, não usar debug mode
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    socketio.run(
        app, 
        debug=debug_mode, 
        host='0.0.0.0', 
        port=port, 
        use_reloader=False
    )