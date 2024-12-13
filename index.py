from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_caching import Cache
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import concurrent.futures
import hashlib
import logging
from functools import lru_cache
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

app = Flask(__name__)

# Configuração de logs aprimorada
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuração do cache com opção Redis
configuracao_cache = {
    'CACHE_TYPE': 'SimpleCache',  # Para produção: 'redis'
    'CACHE_DEFAULT_TIMEOUT': 86400,
    'CACHE_KEY_PREFIX': 'santos_',
    'CACHE_REDIS_URL': 'redis://localhost:6379/0',  # Descomentar para Redis
    'CACHE_OPTIONS': {'compression': True}  # Habilita compressão
}
app.config.from_mapping(configuracao_cache)
cache = Cache(app)

# Sessão para pool de conexões
sessao = requests.Session()
sessao.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'
})

# Configuração CORS
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET"],
        "allow_headers": ["Content-Type"],
        "expose_headers": ["Content-Type", "X-Status-Cache"],
        "supports_credentials": False,
        "max_age": 3600
    }
})

def criar_chave_cache(*args, **kwargs):
    """Cria uma chave única de cache baseada nos argumentos e data atual"""
    partes_chave = [str(arg) for arg in args]
    partes_chave.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
    partes_chave.append(datetime.now().strftime("%Y-%m-%d"))

    string_chave = "_".join(partes_chave)
    return hashlib.md5(string_chave.encode()).hexdigest()

@lru_cache(maxsize=128)
def buscar_url(url):
    """Busca URL com tratamento de erro e tentativas"""
    try:
        resposta = sessao.get(url, timeout=10)
        resposta.raise_for_status()
        return resposta.text
    except requests.RequestException as e:
        logger.error(f"Erro ao buscar URL {url}: {str(e)}")
        raise

def extrair_info_santo(soup):
    """Extrai informações do santo com tratamento de erro"""
    try:
        nome_santo = soup.find('div', class_='feature__name').text.strip()
        infos_santo = soup.find('div', class_='wg-text').find_all('p')
        elemento_imagem = soup.find("div", class_="feature").find(class_="feature__portrait")
        imagem = "https://www.a12.com" + elemento_imagem["src"] if elemento_imagem else None

        return {
            'nome': nome_santo,
            'imagem': imagem,
            'historia': "\n\n".join(p.text.strip() for p in infos_santo[:-4] if p.text.strip()),
            'reflexao': "\n\n".join(p.text.strip() for p in infos_santo[-3:-2] if p.text.strip()),
            'oracao': "\n\n".join(p.text.strip() for p in infos_santo[-1:] if p.text.strip())
        }
    except Exception as e:
        logger.error(f"Erro ao extrair informações do santo: {str(e)}")
        return None

def buscar_dados_santo(url):
    """Busca e processa dados do santo"""
    try:
        html = buscar_url(url)
        soup = BeautifulSoup(html, 'html.parser')
        return extrair_info_santo(soup)
    except Exception as e:
        logger.error(f"Erro ao processar URL {url}: {str(e)}")
        return None

def limpar_e_atualizar_cache():
    """Limpa cache e pré-carrega novos dados"""
    try:
        logger.info("Iniciando limpeza e atualização diária do cache...")
        
        # Limpa todos os caches
        cache.clear()
        buscar_url.cache_clear()
        
        # Pré-carrega dados do dia
        hoje = datetime.now()
        urls = [
            'https://www.a12.com/reze-no-santuario/santo-do-dia',
            f'https://www.a12.com/reze-no-santuario/santo-do-dia?day={hoje.day}&month={hoje.month}'
        ]
        
        for url in urls:
            try:
                html = buscar_url(url)
                soup = BeautifulSoup(html, 'html.parser')
                lista_santos = soup.find("div", class_="saints-list")
                
                if lista_santos:
                    urls_santos = [a['href'] for a in lista_santos.find_all('a', href=True)]
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        santos = list(filter(None, executor.map(buscar_dados_santo, urls_santos)))
                else:
                    info_santo = extrair_info_santo(soup)
                    if info_santo:
                        santos = [info_santo]
                        
                # Armazena os novos dados em cache
                chave_cache = criar_chave_cache('inicio' if 'day' not in url else f'data_{hoje.day}_{hoje.month}')
                cache.set(chave_cache, santos)
                
            except Exception as e:
                logger.error(f"Erro ao pré-carregar dados para URL {url}: {str(e)}")
                
        logger.info("Limpeza e atualização diária do cache concluída com sucesso")
    except Exception as e:
        logger.error(f"Erro em limpar_e_atualizar_cache: {str(e)}")

# Inicializa o agendador
agendador = BackgroundScheduler()
agendador.add_job(
    func=limpar_e_atualizar_cache,
    trigger=CronTrigger(hour=0, minute=0),  # Executa à meia-noite
    id='tarefa_limpar_cache',
    name='Limpar cache e atualizar dados à meia-noite',
    replace_existing=True
)

# Inicia o agendador
agendador.start()

@app.before_request
def antes_requisicao():
    """Log de estatísticas do cache antes de cada requisição"""
    logger.info(f"Estatísticas do cache: {cache.get_stats()}")

@app.after_request
def apos_requisicao(resposta):
    """Adiciona status do cache ao cabeçalho da resposta"""
    resposta.headers['X-Status-Cache'] = 'HIT' if resposta.cache_control.public else 'MISS'
    return resposta

@app.route("/")
def inicio():
    chave_cache = criar_chave_cache('inicio')
    dados_cache = cache.get(chave_cache)

    if dados_cache:
        return jsonify(resultados=dados_cache)

    try:
        html = buscar_url('https://www.a12.com/reze-no-santuario/santo-do-dia')
        soup = BeautifulSoup(html, 'html.parser')
        lista_santos = soup.find("div", class_="saints-list")

        urls = []
        if lista_santos:
            urls = [a['href'] for a in lista_santos.find_all('a', href=True)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            santos = list(filter(None, executor.map(buscar_dados_santo, urls)))

        if not santos and not lista_santos:
            santos = [extrair_info_santo(soup)]

        cache.set(chave_cache, santos)
        return jsonify(resultados=santos)
    except Exception as e:
        logger.error(f"Erro na rota inicial: {str(e)}")
        return jsonify(erro=str(e)), 500

@app.route("/dia=<int:dia>&mes=<int:mes>")
def data(dia, mes):
    chave_cache = criar_chave_cache('data', dia, mes)
    dados_cache = cache.get(chave_cache)

    if dados_cache:
        return jsonify(resultados=dados_cache)

    try:
        url = f'https://www.a12.com/reze-no-santuario/santo-do-dia?day={dia}&month={mes}'
        html = buscar_url(url)
        soup = BeautifulSoup(html, 'html.parser')
        lista_santos = soup.find("div", class_="saints-list")

        santos = []
        if lista_santos:
            urls = [a['href'] for a in lista_santos.find_all('a', href=True)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                santos = list(filter(None, executor.map(buscar_dados_santo, urls)))
        else:
            info_santo = extrair_info_santo(soup)
            if info_santo:
                santos = [info_santo]

        cache.set(chave_cache, santos)
        return jsonify(resultados=santos)
    except Exception as e:
        logger.error(f"Erro na rota de data: {str(e)}")
        return jsonify(erro=str(e)), 500

@app.route("/limpar-cache")
def limpar_cache():
    """Limpa todos os caches manualmente"""
    try:
        cache.clear()
        buscar_url.cache_clear()
        return jsonify(mensagem="Todos os caches foram limpos com sucesso")
    except Exception as e:
        logger.error(f"Erro ao limpar cache: {str(e)}")
        return jsonify(erro=str(e)), 500

# Registra o desligamento do agendador quando a aplicação for encerrada
atexit.register(lambda: agendador.shutdown())

if __name__ == "__main__":
    try:
        app.run(debug=True, port=5000)  # Substitua 5000 pela porta desejada
    except (KeyboardInterrupt, SystemExit):
        # Garante que o agendador seja encerrado adequadamente
        agendador.shutdown()
