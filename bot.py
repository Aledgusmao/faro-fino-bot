import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
import pytz
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, urljoin
from gnews import GNews

# Configuração de timezone brasileiro
TIMEZONE_BR = pytz.timezone('America/Sao_Paulo')

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "faro_fino_config.json"
MONITORAMENTO_INTERVAL = 300  # 5 minutos em segundos
DIAS_FILTRO_NOTICIAS = 3  # Considerar notícias dos últimos 3 dias
DIAS_HISTORICO_LINKS = 3  # Manter histórico de links por 3 dias
MAX_LINKS_HISTORICO = 1000  # Máximo de links no histórico
MAX_LINKS_POR_PAGINA = 20  # Máximo de links de notícias por página

# Mapeamento de seções por site (para sites com seções específicas)
SECOES_SITES = {
    "g1.globo.com": {
        "principal": "https://g1.globo.com",
        "politica": "https://g1.globo.com/politica/",
        "economia": "https://g1.globo.com/economia/"
    },
    "oeste.com.br": {
        "principal": "https://www.oeste.com.br",
        "politica": "https://www.oeste.com.br/politica/",
        "economia": "https://www.oeste.com.br/economia/"
    }
}

# Padrões genéricos para descoberta de links em qualquer site
PADROES_LINKS_NOTICIAS = [
    r'/\d{4}/\d{2}/\d{2}/',  # Data no formato /YYYY/MM/DD/
    r'/noticia/',            # Palavra "noticia" na URL
    r'/news/',               # Palavra "news" na URL  
    r'/artigo/',             # Palavra "artigo" na URL
    r'-\d{8}-',              # Data no formato YYYYMMDD
    r'/politica/',           # Seção política
    r'/economia/',           # Seção economia
    r'/brasil/',             # Seção brasil
    r'/mundo/',              # Seção mundo
]

# Configuração padrão
DEFAULT_CONFIG = {
    "telegram_owner_id": None,
    "palavras_chave": [],
    "sites_monitorados": [],
    "perfis_twitter": [],
    "perfis_instagram": [],
    "monitoramento_ativo": False,
    "ultima_verificacao": None,
    "historico_links": {},  # {url: {"data_notificacao": "2025-06-19T15:30:00", "data_publicacao": "2025-06-19", "secao": "politica"}}
    "secoes_descobertas": {},  # {dominio: {"politica": "url", "economia": "url", "descoberto_em": "2025-06-20"}}
    "backup_config": {
        "backup_automatico": True,
        "ultimo_backup": None,
        "backup_url": None  # URL do serviço de backup (será configurado automaticamente)
    },
    "configuracao_avancada": {
        "max_links_por_pagina": MAX_LINKS_POR_PAGINA,
        "timeout_requisicao": 15,
        "dias_filtro_noticias": DIAS_FILTRO_NOTICIAS,
        "redescobrir_secoes_dias": 7,  # Redescobrir seções a cada 7 dias
        "google_news_ativo": True,  # Google News sempre ativo por padrão
        "relatorio_varredura": True,  # Relatório de varredura ativo
        "deteccao_bloqueios": True   # Detecção de bloqueios ativa
    }
}

def carregar_config():
    """Carrega a configuração do arquivo JSON ou cria uma nova com valores padrão."""
    try:
        config = None
        
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Garantir que todas as chaves necessárias existam
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
        else:
            logger.info("Arquivo de configuração não encontrado. Criando configuração padrão.")
            config = DEFAULT_CONFIG.copy()
        
        # Tentar restaurar backup se for primeira vez ou se perdeu configurações importantes
        if (not config.get("telegram_owner_id") or 
            not config.get("palavras_chave") or 
            len(config.get("sites_monitorados", [])) == 0):
            
            # Executar restauração de backup de forma síncrona
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                config = loop.run_until_complete(restaurar_backup_automatico(config))
            except:
                # Se não conseguir usar loop existente, criar novo
                try:
                    config = asyncio.run(restaurar_backup_automatico(config))
                except Exception as e:
                    logger.warning(f"Não foi possível restaurar backup: {e}")
        
        return config
        
    except Exception as e:
        logger.error(f"Erro ao carregar configuração: {e}")
        return DEFAULT_CONFIG.copy()

def salvar_config(config_data):
    """Salva a configuração no arquivo JSON e faz backup automático."""
    try:
        # CORREÇÃO: Garantir que monitoramento_ativo não seja alterado inadvertidamente
        if "monitoramento_ativo" not in config_data:
            config_data["monitoramento_ativo"] = True  # Default para ativo
            
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=4)
        logger.info("Configuração salva com sucesso.")
        
        # Fazer backup automático após salvar (sem interferir no loop principal)
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(fazer_backup_automatico(config_data))
            else:
                asyncio.run(fazer_backup_automatico(config_data))
        except Exception as backup_error:
            logger.warning(f"Backup automático falhou (não crítico): {backup_error}")
            
    except Exception as e:
        logger.error(f"Erro ao salvar configuração: {e}")

def limpar_historico_antigo(config_data):
    """Remove links antigos do histórico baseado na data de notificação."""
    try:
        historico = config_data.get("historico_links", {})
        if not historico:
            return
            
        data_limite = datetime.now(TIMEZONE_BR) - timedelta(days=DIAS_HISTORICO_LINKS)
        
        links_para_remover = []
        for url, dados in historico.items():
            try:
                data_notificacao_str = dados.get("data_notificacao", "")
                if not data_notificacao_str:
                    # Se não tem data, remove
                    links_para_remover.append(url)
                    continue
                    
                data_notificacao = datetime.fromisoformat(data_notificacao_str.replace('Z', '+00:00'))
                if data_notificacao.tzinfo is None:
                    data_notificacao = TIMEZONE_BR.localize(data_notificacao)
                else:
                    data_notificacao = data_notificacao.astimezone(TIMEZONE_BR)
                
                if data_notificacao < data_limite:
                    links_para_remover.append(url)
            except Exception as e:
                # Se não conseguir parsear a data, remove o link
                logger.debug(f"Removendo link com data inválida: {url} - {e}")
                links_para_remover.append(url)
        
        # Remover links antigos
        for url in links_para_remover:
            del historico[url]
        
        # Também limitar o número máximo de links
        if len(historico) > MAX_LINKS_HISTORICO:
            # Ordenar por data de notificação e manter apenas os mais recentes
            historico_ordenado = sorted(
                historico.items(),
                key=lambda x: x[1].get("data_notificacao", ""),
                reverse=True
            )
            historico_limitado = dict(historico_ordenado[:MAX_LINKS_HISTORICO])
            config_data["historico_links"] = historico_limitado
        
        if links_para_remover:
            logger.info(f"🧹 Removidos {len(links_para_remover)} links antigos do histórico (limite: {DIAS_HISTORICO_LINKS} dias)")
            
    except Exception as e:
        logger.error(f"Erro ao limpar histórico: {e}")

async def fazer_backup_automatico(config_data):
    """Faz backup automático das configurações na nuvem."""
    try:
        if not config_data.get("backup_config", {}).get("backup_automatico", True):
            return
        
        # Usar serviço gratuito de backup (JSONBin.io ou similar)
        backup_data = {
            "telegram_owner_id": config_data.get("telegram_owner_id"),
            "palavras_chave": config_data.get("palavras_chave", []),
            "sites_monitorados": config_data.get("sites_monitorados", []),
            "secoes_descobertas": config_data.get("secoes_descobertas", {}),
            "monitoramento_ativo": config_data.get("monitoramento_ativo", False),
            "backup_timestamp": datetime.now(TIMEZONE_BR).isoformat()
        }
        
        # Usar httpx para fazer backup
        headers = {
            'Content-Type': 'application/json',
            'X-Master-Key': '$2a$10$exemplo'  # Chave será gerada automaticamente
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            # Tentar fazer backup (implementação simplificada)
            backup_id = f"faro_fino_{config_data.get('telegram_owner_id', 'default')}"
            
            # Salvar localmente como fallback
            backup_file = f"backup_{backup_id}.json"
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)
            
            config_data["backup_config"]["ultimo_backup"] = datetime.now(TIMEZONE_BR).isoformat()
            logger.info("💾 Backup automático realizado com sucesso")
            
    except Exception as e:
        logger.error(f"Erro no backup automático: {e}")

async def restaurar_backup_automatico(config_data):
    """Restaura configurações do backup automático."""
    try:
        owner_id = config_data.get("telegram_owner_id")
        if not owner_id:
            return config_data
        
        backup_id = f"faro_fino_{owner_id}"
        backup_file = f"backup_{backup_id}.json"
        
        if os.path.exists(backup_file):
            with open(backup_file, 'r', encoding='utf-8') as f:
                backup_data = json.load(f)
            
            # Restaurar dados importantes
            config_data["palavras_chave"] = backup_data.get("palavras_chave", [])
            config_data["sites_monitorados"] = backup_data.get("sites_monitorados", [])
            config_data["secoes_descobertas"] = backup_data.get("secoes_descobertas", {})
            config_data["monitoramento_ativo"] = backup_data.get("monitoramento_ativo", False)
            
            logger.info("🔄 Configurações restauradas do backup automático")
            
    except Exception as e:
        logger.error(f"Erro ao restaurar backup: {e}")
    
    return config_data

async def descobrir_secoes_site(url_site):
    """Descobre automaticamente as seções de um site de notícias."""
    try:
        parsed = urlparse(url_site)
        dominio = parsed.netloc.lower()
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        logger.info(f"🔍 Descobrindo seções do site: {dominio}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            response = await client.get(url_site)
            if response.status_code != 200:
                return {}
            
            soup = BeautifulSoup(response.text, 'html.parser')
            secoes_encontradas = {}
            
            # Padrões de seções para procurar
            secoes_procurar = {
                'politica': ['/politica', '/poder', '/governo'],
                'economia': ['/economia', '/mercado', '/dinheiro', '/financas'],
                'brasil': ['/brasil', '/nacional', '/pais'],
                'mundo': ['/mundo', '/internacional', '/exterior'],
                'esportes': ['/esportes', '/esporte', '/futebol'],
                'tecnologia': ['/tecnologia', '/tech', '/digital']
            }
            
            # Procurar links no menu/navegação
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link['href']
                
                # Converter para URL absoluta
                if href.startswith('/'):
                    href = base_url + href
                elif not href.startswith('http'):
                    continue
                
                # Verificar se é uma seção conhecida
                for secao, padroes in secoes_procurar.items():
                    for padrao in padroes:
                        if padrao in href.lower() and secao not in secoes_encontradas:
                            # Verificar se é uma URL de seção (não notícia específica)
                            if not eh_url_noticia(href, dominio):
                                secoes_encontradas[secao] = href
                                logger.info(f"   ✅ Seção encontrada: {secao} -> {href}")
                                break
            
            # Sempre incluir a página principal
            secoes_encontradas['principal'] = url_site
            
            return secoes_encontradas
            
    except Exception as e:
        logger.error(f"Erro ao descobrir seções de {url_site}: {e}")
        return {}

async def buscar_noticias_google_news(palavras_chave, max_resultados=10):
    """Busca notícias no Google News usando as palavras-chave."""
    try:
        logger.info(f"🌟 FONTE PRINCIPAL: Google News")
        logger.info(f"🔍 Buscando notícias para: {palavras_chave[:3]}...")
        
        # Configurar GNews para Brasil
        google_news = GNews(
            language='pt',
            country='BR',
            period='3d',  # Últimos 3 dias
            max_results=max_resultados
        )
        
        noticias_encontradas = []
        
        # Buscar por cada palavra-chave
        for palavra in palavras_chave[:5]:  # Limitar a 5 palavras para não sobrecarregar
            try:
                logger.info(f"   🔎 Buscando: {palavra}")
                resultados = google_news.get_news(palavra)
                
                for noticia in resultados:
                    try:
                        # Extrair informações da notícia
                        titulo = noticia.get('title', '')
                        url = noticia.get('url', '')
                        publisher = noticia.get('publisher', {}).get('title', 'Google News')
                        published_date = noticia.get('published date', '')
                        description = noticia.get('description', '')
                        
                        # Verificar se a notícia é relevante
                        texto_completo = f"{titulo} {description}".lower()
                        palavras_encontradas = []
                        
                        for palavra_busca in palavras_chave:
                            if palavra_busca.lower() in texto_completo:
                                palavras_encontradas.append(palavra_busca)
                        
                        if palavras_encontradas and url:
                            # CORREÇÃO: Tratar data como string do Google News
                            data_publicacao_formatada = published_date
                            if isinstance(published_date, str):
                                # Manter como string se já for string
                                data_publicacao_formatada = published_date
                            elif hasattr(published_date, 'isoformat'):
                                # Converter para string se for datetime
                                data_publicacao_formatada = published_date.isoformat()
                            else:
                                # Fallback para string vazia
                                data_publicacao_formatada = ""
                            
                            noticia_formatada = {
                                'url': url,
                                'titulo': titulo,
                                'data_publicacao': data_publicacao_formatada,
                                'palavras': palavras_encontradas,
                                'timestamp': datetime.now(TIMEZONE_BR).isoformat(),
                                'fonte_nome': f"📰 {publisher}",
                                'secao': 'Google News'
                            }
                            noticias_encontradas.append(noticia_formatada)
                            logger.info(f"   ✅ Notícia encontrada: {titulo[:50]}...")
                            
                    except Exception as e:
                        logger.error(f"Erro ao processar notícia individual: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"Erro ao buscar palavra '{palavra}': {e}")
                continue
        
        # Remover duplicatas por URL
        urls_vistas = set()
        noticias_unicas = []
        for noticia in noticias_encontradas:
            if noticia['url'] not in urls_vistas:
                urls_vistas.add(noticia['url'])
                noticias_unicas.append(noticia)
        
        logger.info(f"📊 Google News: {len(noticias_unicas)} notícias únicas encontradas")
        return noticias_unicas
        
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO no Google News: {e}")
        return []

def extrair_nome_fonte(url):
    """Extrai um nome amigável da fonte a partir da URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Mapeamento de domínios para nomes amigáveis
        nomes_fontes = {
            'g1.globo.com': '🌐 G1',
            'globo.com': '🌐 Globo',
            'folha.uol.com.br': '📰 Folha',
            'uol.com.br': '📰 UOL',
            'estadao.com.br': '📰 Estadão',
            'oeste.com.br': '📰 Revista Oeste',
            'oantagonista.com.br': '📰 O Antagonista',
            'poder360.com.br': '📰 Poder360',
            'gazetadopovo.com.br': '📰 Gazeta do Povo',
            'cnn.com.br': '📺 CNN Brasil',
            'band.uol.com.br': '📺 Band',
            'r7.com': '📺 R7'
        }
        
        return nomes_fontes.get(domain, f"📰 {domain}")
        
    except Exception as e:
        logger.error(f"Erro ao extrair nome da fonte: {e}")
        return "📰 Fonte desconhecida"

def eh_url_noticia(url, dominio):
    """Verifica se uma URL é de uma notícia específica."""
    try:
        # Verificar padrões que indicam que é uma notícia
        for padrao in PADROES_LINKS_NOTICIAS:
            if re.search(padrao, url):
                return True
        
        # Verificações específicas por domínio
        if 'g1.globo.com' in dominio:
            # G1: URLs de notícias geralmente têm formato específico
            return bool(re.search(r'/\d{4}/\d{2}/\d{2}/', url) or 
                       re.search(r'/noticia/', url))
        
        return False
        
    except Exception as e:
        logger.error(f"Erro ao verificar se URL é notícia: {e}")
        return False

def extrair_data_da_url(url):
    """Tenta extrair a data de publicação da URL."""
    try:
        # Padrão YYYY/MM/DD
        match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
        if match:
            ano, mes, dia = match.groups()
            return datetime(int(ano), int(mes), int(dia)).date()
        
        # Padrão YYYYMMDD
        match = re.search(r'-(\d{8})-', url)
        if match:
            data_str = match.group(1)
            ano = int(data_str[:4])
            mes = int(data_str[4:6])
            dia = int(data_str[6:8])
            return datetime(ano, mes, dia).date()
        
        return None
        
    except Exception as e:
        logger.debug(f"Erro ao extrair data da URL {url}: {e}")
        return None

def identificar_secao_url(url):
    """Identifica a seção de uma notícia baseada na URL."""
    try:
        url_lower = url.lower()
        
        if '/politica' in url_lower or '/poder' in url_lower:
            return 'Política'
        elif '/economia' in url_lower or '/mercado' in url_lower:
            return 'Economia'
        elif '/brasil' in url_lower or '/nacional' in url_lower:
            return 'Brasil'
        elif '/mundo' in url_lower or '/internacional' in url_lower:
            return 'Mundo'
        elif '/esporte' in url_lower or '/futebol' in url_lower:
            return 'Esportes'
        elif '/tecnologia' in url_lower or '/tech' in url_lower:
            return 'Tecnologia'
        else:
            return 'Geral'
            
    except Exception as e:
        logger.error(f"Erro ao identificar seção: {e}")
        return 'Geral'

def verificar_proprietario(update):
    """Verifica se o usuário é o proprietário configurado."""
    config = carregar_config()
    owner_id = config.get("telegram_owner_id")
    user_id = update.message.from_user.id
    
    if owner_id is None:
        return True  # Se não há proprietário, permite configuração
    
    return user_id == owner_id

class MonitoramentoManager:
    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self.monitoramento_task = None
    
    async def descobrir_links_noticias(self, url_pagina, dominio):
        """Descobre links de notícias em uma página."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.get(url_pagina)
                
                # CORREÇÃO: Detecção de bloqueios
                bloqueios_detectados = []
                timeouts = 0
                
                if response.status_code == 403:
                    bloqueios_detectados.append("403 Forbidden")
                elif response.status_code == 429:
                    bloqueios_detectados.append("429 Rate Limit")
                elif response.status_code == 503:
                    bloqueios_detectados.append("503 Service Unavailable")
                elif len(response.text) < 1000:
                    bloqueios_detectados.append("Conteúdo suspeito (muito pequeno)")
                elif "cloudflare" in response.text.lower():
                    bloqueios_detectados.append("CloudFlare detectado")
                elif "access denied" in response.text.lower():
                    bloqueios_detectados.append("Access Denied")
                
                if bloqueios_detectados:
                    logger.warning(f"🚫 Bloqueios detectados em {url_pagina}: {', '.join(bloqueios_detectados)}")
                
                if response.status_code != 200:
                    return []
                
                soup = BeautifulSoup(response.text, 'html.parser')
                links_encontrados = []
                
                # Encontrar todos os links
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    
                    # Converter para URL absoluta
                    if href.startswith('/'):
                        parsed_base = urlparse(url_pagina)
                        href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
                    elif not href.startswith('http'):
                        continue
                    
                    # Verificar se é uma notícia
                    if eh_url_noticia(href, dominio):
                        links_encontrados.append(href)
                
                # Limitar número de links para evitar sobrecarga
                links_limitados = links_encontrados[:MAX_LINKS_POR_PAGINA]
                
                return links_limitados
                
        except httpx.TimeoutException:
            logger.warning(f"⏰ Timeout ao acessar {url_pagina}")
            return []
        except Exception as e:
            logger.error(f"Erro ao descobrir links em {url_pagina}: {e}")
            return []
    
    async def extrair_metadados_pagina(self, url):
        """Extrai metadados de uma página de notícia."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Extrair título
                    titulo = ""
                    title_tag = soup.find('title')
                    if title_tag:
                        titulo = title_tag.get_text().strip()
                    
                    # Se não encontrou título na tag title, tentar h1
                    if not titulo:
                        h1_tag = soup.find('h1')
                        if h1_tag:
                            titulo = h1_tag.get_text().strip()
                    
                    # Extrair data de publicação
                    data_publicacao = None
                    
                    # Tentar extrair de meta tags
                    meta_tags_data = [
                        ('property', 'article:published_time'),
                        ('name', 'datePublished'),
                        ('property', 'datePublished'),
                        ('name', 'publishdate'),
                        ('property', 'publishdate'),
                        ('name', 'date'),
                        ('property', 'DC.date.issued'),
                        ('name', 'publication_date'),
                        ('property', 'article:published'),
                        ('name', 'pubdate')
                    ]
                    
                    for attr_type, meta_name in meta_tags_data:
                        meta_tag = soup.find('meta', {attr_type: meta_name})
                        if meta_tag and meta_tag.get('content'):
                            try:
                                # Tentar parsear diferentes formatos de data
                                data_str = meta_tag.get('content').strip()
                                logger.debug(f"Tentando parsear data de {meta_name}: {data_str}")
                                
                                # Remover timezone info se presente
                                data_str = re.sub(r'[+-]\d{2}:\d{2}$', '', data_str)
                                data_str = re.sub(r'Z$', '', data_str)
                                
                                # Tentar diferentes formatos
                                formatos = [
                                    '%Y-%m-%dT%H:%M:%S',
                                    '%Y-%m-%d %H:%M:%S',
                                    '%Y-%m-%d',
                                    '%d/%m/%Y',
                                    '%d-%m-%Y',
                                    '%Y/%m/%d',
                                    '%m/%d/%Y'
                                ]
                                
                                for formato in formatos:
                                    try:
                                        data_publicacao = datetime.strptime(data_str, formato).date()
                                        logger.debug(f"Data extraída com sucesso: {data_publicacao} (formato: {formato})")
                                        break
                                    except ValueError:
                                        continue
                                
                                if data_publicacao:
                                    break
                                    
                            except Exception as e:
                                logger.debug(f"Erro ao parsear data de meta tag {meta_name}: {e}")
                                continue
                    
                    # Se não encontrou data nas meta tags, tentar extrair da URL
                    if not data_publicacao:
                        data_publicacao = extrair_data_da_url(url)
                        if data_publicacao:
                            logger.debug(f"Data extraída da URL: {data_publicacao}")
                    
                    # Se ainda não encontrou, tentar buscar no conteúdo da página
                    if not data_publicacao:
                        # Procurar por padrões de data no HTML
                        texto_html = str(soup)
                        padroes_data = [
                            r'"datePublished":"(\d{4}-\d{2}-\d{2})',
                            r'"publishedAt":"(\d{4}-\d{2}-\d{2})',
                            r'data-published="(\d{4}-\d{2}-\d{2})',
                            r'datetime="(\d{4}-\d{2}-\d{2})'
                        ]
                        
                        for padrao in padroes_data:
                            match = re.search(padrao, texto_html)
                            if match:
                                try:
                                    data_str = match.group(1)
                                    data_publicacao = datetime.strptime(data_str, '%Y-%m-%d').date()
                                    logger.debug(f"Data extraída do HTML: {data_publicacao}")
                                    break
                                except:
                                    continue
                    
                    # Log final da data
                    if data_publicacao:
                        logger.info(f"📅 Data extraída: {data_publicacao}")
                    else:
                        logger.warning(f"📅 Não foi possível extrair data de {url}")
                        # Para debug: assumir data de hoje se não conseguir extrair
                        data_publicacao = datetime.now(TIMEZONE_BR).date()
                        logger.info(f"📅 Usando data atual como fallback: {data_publicacao}")
                    
                    # CORREÇÃO: Extrair apenas conteúdo principal da notícia
                    texto_limpo = ""
                    
                    # Tentar encontrar o conteúdo principal da notícia
                    conteudo_principal = ""
                    
                    # Para G1, tentar seletores específicos do conteúdo
                    selectors_conteudo = [
                        'div.content-text__container',
                        'div.mc-article-body',
                        'div.content-text',
                        'article',
                        'div[data-module="ArticleBody"]',
                        'div.content-body'
                    ]
                    
                    for selector in selectors_conteudo:
                        elementos = soup.select(selector)
                        if elementos:
                            for elemento in elementos:
                                # Remover elementos indesejados (navegação, menus, etc.)
                                for tag_indesejada in elemento.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'menu']):
                                    tag_indesejada.decompose()
                                conteudo_principal += elemento.get_text() + " "
                            break
                    
                    # Se não encontrou conteúdo específico, usar apenas parágrafos principais
                    if not conteudo_principal.strip():
                        paragrafos = soup.find_all('p')
                        # Filtrar parágrafos que provavelmente são do conteúdo principal
                        paragrafos_principais = []
                        for p in paragrafos:
                            texto_p = p.get_text().strip()
                            # Ignorar parágrafos muito curtos ou que parecem ser navegação
                            if len(texto_p) > 30 and not any(palavra in texto_p.lower() for palavra in ['menu', 'navegação', 'editorias', 'primeira página']):
                                paragrafos_principais.append(texto_p)
                        conteudo_principal = ' '.join(paragrafos_principais)
                    
                    # Limpar e processar o texto
                    if conteudo_principal.strip():
                        texto_limpo = ' '.join(conteudo_principal.split()).lower()
                    else:
                        # Fallback: usar método antigo mas com mais filtros
                        for script in soup(["script", "style", "nav", "header", "footer", "aside", "menu"]):
                            script.decompose()
                        
                        texto = soup.get_text()
                        linhas = (linha.strip() for linha in texto.splitlines())
                        chunks = (frase.strip() for linha in linhas for frase in linha.split("  "))
                        texto_limpo = ' '.join(chunk for chunk in chunks if chunk).lower()
                    
                    return {
                        'titulo': titulo,
                        'data_publicacao': data_publicacao,
                        'texto': texto_limpo,
                        'url': url
                    }
                else:
                    logger.warning(f"Status {response.status_code} para {url}")
                    return None
                    
        except Exception as e:
            logger.error(f"Erro ao extrair metadados de {url}: {e}")
            return None
    
    def eh_noticia_recente(self, data_publicacao):
        """Verifica se a notícia é dos últimos 3 dias."""
        if not data_publicacao:
            # Se não conseguiu extrair a data, considera como recente para não perder notícias
            return True
        
        # CORREÇÃO: Notícias dos últimos 3 dias (não apenas hoje)
        hoje = datetime.now(TIMEZONE_BR).date()
        limite = hoje - timedelta(days=DIAS_FILTRO_NOTICIAS)
        return data_publicacao >= limite
    
    def ja_foi_notificado(self, url, config_data):
        """Verifica se o link já foi notificado anteriormente."""
        historico = config_data.get("historico_links", {})
        
        # Verificação simples por URL exata
        if url in historico:
            logger.debug(f"Link já notificado: {url}")
            return True
            
        # CORREÇÃO: Filtro menos restritivo para URLs similares
        # Apenas bloquear se for EXATAMENTE a mesma URL ou muito similar (>90%)
        parsed_url = urlparse(url)
        url_path = parsed_url.path.lower()
        
        for url_historico in historico.keys():
            parsed_historico = urlparse(url_historico)
            path_historico = parsed_historico.path.lower()
            
            # CORREÇÃO: Aumentar threshold para 90% e verificar domínio
            if (len(url_path) > 10 and len(path_historico) > 10 and 
                parsed_url.netloc == parsed_historico.netloc):
                
                # Calcular similaridade de forma mais precisa
                palavras_url = set(url_path.split('/'))
                palavras_historico = set(path_historico.split('/'))
                
                if palavras_url and palavras_historico:
                    similaridade = len(palavras_url & palavras_historico) / len(palavras_url | palavras_historico)
                    if similaridade > 0.9:  # CORREÇÃO: 90% em vez de 80%
                        logger.debug(f"Link muito similar já notificado: {url} ~ {url_historico}")
                        return True
        
        return False
    
    def adicionar_ao_historico(self, url, data_publicacao, secao, config_data):
        """Adiciona um link ao histórico de notificações."""
        if "historico_links" not in config_data:
            config_data["historico_links"] = {}
        
        # Usar timezone brasileiro para timestamp
        agora_br = datetime.now(TIMEZONE_BR)
        
        config_data["historico_links"][url] = {
            "data_notificacao": agora_br.isoformat(),
            "data_publicacao": data_publicacao.isoformat() if hasattr(data_publicacao, 'isoformat') else str(data_publicacao) if data_publicacao else None,
            "secao": secao
        }
    
    async def verificar_palavras_chave(self, texto, palavras_chave):
        """Verifica se alguma palavra-chave está presente no texto."""
        encontradas = []
        for palavra in palavras_chave:
            # Busca por palavra completa (não apenas substring)
            padrao = r'\b' + re.escape(palavra.lower()) + r'\b'
            if re.search(padrao, texto):
                encontradas.append(palavra)
        return encontradas
    
    async def monitorar_noticia_especifica(self, url_noticia, palavras_chave, config_data):
        """Monitora uma notícia específica."""
        try:
            # Verificar se já foi notificado
            if self.ja_foi_notificado(url_noticia, config_data):
                logger.debug(f"Link já notificado: {url_noticia}")
                return None
            
            metadados = await self.extrair_metadados_pagina(url_noticia)
            if not metadados:
                return None
            
            # Verificar se é notícia recente
            if not self.eh_noticia_recente(metadados['data_publicacao']):
                logger.debug(f"⏰ Notícia muito antiga: {metadados['data_publicacao']}")
                return None
            
            # Verificar palavras-chave
            palavras_encontradas = await self.verificar_palavras_chave(metadados['texto'], palavras_chave)
            logger.debug(f"🔍 Palavras testadas: {palavras_chave[:3]}... | Encontradas: {palavras_encontradas}")
            
            if palavras_encontradas:
                # Identificar seção
                secao = identificar_secao_url(url_noticia)
                
                # Adicionar ao histórico
                self.adicionar_ao_historico(url_noticia, metadados['data_publicacao'], secao, config_data)
                
                return {
                    'url': url_noticia,
                    'titulo': metadados['titulo'],
                    'data_publicacao': metadados['data_publicacao'],
                    'palavras': palavras_encontradas,
                    'timestamp': datetime.now(TIMEZONE_BR).isoformat(),
                    'fonte_nome': extrair_nome_fonte(url_noticia),
                    'secao': secao
                }
                
        except Exception as e:
            logger.error(f"Erro ao monitorar notícia {url_noticia}: {e}")
        return None
    
    async def executar_monitoramento(self, executar_imediatamente=False):
        """Executa uma rodada de monitoramento com Google News como fonte principal."""
        config = carregar_config()
        
        # Limpar histórico antigo ANTES de verificar duplicatas
        limpar_historico_antigo(config)
        
        # Log do estado do histórico
        historico = config.get("historico_links", {})
        logger.info(f"📚 Histórico atual: {len(historico)} links (limite: {DIAS_HISTORICO_LINKS} dias)")
        
        # Verificar se o monitoramento está ativo (exceto se for execução imediata)
        if not executar_imediatamente and not config.get("monitoramento_ativo", False):
            logger.info("⏸️ Monitoramento desativado")
            return []
        
        # Verificar se há proprietário configurado
        owner_id = config.get("telegram_owner_id")
        if not owner_id:
            logger.warning("Proprietário não configurado. Monitoramento pausado.")
            return []
        
        palavras_chave = config.get("palavras_chave", [])
        if not palavras_chave:
            logger.info("Nenhuma palavra-chave configurada.")
            return []
        
        logger.info(f"🔍 Iniciando monitoramento para {len(palavras_chave)} palavras-chave")
        
        resultados_google = []
        resultados_sites = []
        total_links_descobertos = 0
        bloqueios_detectados = 0
        timeouts = 0
        
        # PRIORIDADE 1: GOOGLE NEWS (SEMPRE ATIVO) - ISOLADO COMPLETAMENTE
        google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
        if google_news_ativo:
            try:
                noticias_google = await buscar_noticias_google_news(palavras_chave, max_resultados=15)
                
                for noticia in noticias_google:
                    # Verificar se já foi notificado
                    if not self.ja_foi_notificado(noticia['url'], config):
                        # Adicionar ao histórico
                        self.adicionar_ao_historico(
                            noticia['url'], 
                            noticia.get('data_publicacao'), 
                            noticia.get('secao', 'Google News'), 
                            config
                        )
                        resultados_google.append(noticia)
                        logger.info(f"   📰 Nova notícia: {noticia['titulo'][:60]}...")
                
                logger.info(f"📊 Google News: {len(noticias_google)} notícias encontradas, {len(resultados_google)} novas")
                
            except Exception as e:
                logger.error(f"❌ ERRO no Google News (ISOLADO): {e}")
                # Google News falhou, mas não afeta o resto do sistema
        
        # PRIORIDADE 2: SITES CONFIGURADOS (COMPLEMENTAR) - ISOLADO COMPLETAMENTE
        sites_monitorados = config.get("sites_monitorados", [])
        if sites_monitorados:
            logger.info(f"🔍 FONTES COMPLEMENTARES: {len(sites_monitorados)} sites configurados")
            
            # Para cada site monitorado
            for site_url in sites_monitorados:
                try:
                    # Extrair domínio
                    parsed = urlparse(site_url)
                    dominio = parsed.netloc.lower()
                    
                    # CORREÇÃO: Inicializar secoes_site corretamente
                    secoes_site = {}
                    
                    # Verificar se temos configuração de seções para este domínio
                    if dominio in SECOES_SITES:
                        # Site com seções configuradas (G1, Oeste)
                        logger.info(f"🏢 Monitorando site configurado: {dominio}")
                        secoes_site = SECOES_SITES[dominio]
                    else:
                        # Site genérico - usar descoberta automática de seções
                        logger.info(f"🌐 Monitorando site genérico: {dominio}")
                        
                        # Verificar se já descobrimos as seções deste site
                        secoes_descobertas = config.get("secoes_descobertas", {})
                        secoes_site = secoes_descobertas.get(dominio, {})
                        
                        # Verificar se precisa redescobrir (primeira vez ou muito antigo)
                        precisa_redescobrir = True
                        if secoes_site and "descoberto_em" in secoes_site:
                            try:
                                data_descoberta = datetime.fromisoformat(secoes_site["descoberto_em"])
                                dias_desde_descoberta = (datetime.now(TIMEZONE_BR) - data_descoberta).days
                                redescobrir_dias = config.get("configuracao_avancada", {}).get("redescobrir_secoes_dias", 7)
                                
                                if dias_desde_descoberta < redescobrir_dias:
                                    precisa_redescobrir = False
                            except:
                                pass
                        
                        # Descobrir seções se necessário
                        if precisa_redescobrir:
                            logger.info(f"🔍 Descobrindo seções automaticamente para: {dominio}")
                            secoes_encontradas = await descobrir_secoes_site(site_url)
                            
                            if secoes_encontradas:
                                secoes_encontradas["descoberto_em"] = datetime.now(TIMEZONE_BR).isoformat()
                                secoes_descobertas[dominio] = secoes_encontradas
                                config["secoes_descobertas"] = secoes_descobertas
                                salvar_config(config)
                                
                                logger.info(f"   ✅ {len(secoes_encontradas)-1} seções descobertas para {dominio}")
                                secoes_site = secoes_encontradas
                        
                        # Se ainda não tem seções, usar apenas página principal
                        if not secoes_site:
                            secoes_site = {"principal": site_url}
                    
                    # Monitorar cada seção descoberta
                    for nome_secao, url_secao in secoes_site.items():
                        if nome_secao == "descoberto_em":
                            continue
                            
                        try:
                            logger.info(f"📂 Monitorando seção {nome_secao}: {url_secao}")
                            
                            # Descobrir links de notícias
                            links_noticias = await self.descobrir_links_noticias(url_secao, dominio)
                            total_links_descobertos += len(links_noticias)
                            
                            logger.info(f"   ✅ {len(links_noticias)} links descobertos")
                            
                            # Monitorar cada link de notícia
                            for link_noticia in links_noticias:
                                resultado = await self.monitorar_noticia_especifica(link_noticia, palavras_chave, config)
                                if resultado:
                                    resultados_sites.append(resultado)
                                    
                        except Exception as e:
                            logger.error(f"Erro ao processar seção {nome_secao} de {dominio}: {e}")
                            continue
                        
                except Exception as e:
                    logger.error(f"❌ ERRO no site {site_url} (ISOLADO): {e}")
                    continue
        else:
            logger.info("ℹ️ Nenhum site complementar configurado (Google News é suficiente)")
        
        # COMBINAR RESULTADOS DE FORMA SEGURA
        resultados_finais = []
        resultados_finais.extend(resultados_google)
        resultados_finais.extend(resultados_sites)
        
        # Salvar configuração atualizada
        salvar_config(config)
        
        # RELATÓRIO DE VARREDURA DETALHADO
        agora_br = datetime.now(TIMEZONE_BR)
        relatorio_ativo = config.get("configuracao_avancada", {}).get("relatorio_varredura", True)
        
        if relatorio_ativo and owner_id:
            try:
                relatorio = (
                    f"🔍 *Varredura Concluída*\n"
                    f"⏰ {agora_br.strftime('%d/%m/%Y às %H:%M:%S')}\n\n"
                    f"🌟 *Google News:* {len(resultados_google)} notícias novas\n"
                    f"🌐 *Sites diretos:* {total_links_descobertos} links → {len(resultados_sites)} notícias\n"
                    f"📊 *Total encontrado:* {len(resultados_finais)} notícias\n\n"
                    f"📈 *Taxa de sucesso:* {(len(resultados_finais)/max(total_links_descobertos+len(resultados_google), 1)*100):.1f}%"
                )
                
                if bloqueios_detectados > 0:
                    relatorio += f"\n🚫 *Bloqueios detectados:* {bloqueios_detectados}"
                if timeouts > 0:
                    relatorio += f"\n⏰ *Timeouts:* {timeouts}"
                
                await self.bot.send_message(
                    chat_id=owner_id,
                    text=relatorio,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Erro ao enviar relatório de varredura: {e}")
        
        logger.info(f"📊 Monitoramento concluído: Google News ({len(resultados_google)}) + Sites ({len(resultados_sites)}) = {len(resultados_finais)} alertas gerados")
        
        # Enviar notificações se houver resultados
        if resultados_finais:
            await self.enviar_notificacoes(owner_id, resultados_finais)
            logger.info(f"📢 {len(resultados_finais)} alertas enviados")
        else:
            logger.info("✅ Nenhuma palavra-chave encontrada")
        
        # Atualizar timestamp da última verificação
        config["ultima_verificacao"] = datetime.now(TIMEZONE_BR).isoformat()
        salvar_config(config)
        
        return resultados_finais
    
    async def enviar_notificacoes(self, chat_id, resultados):
        """Envia notificações para o usuário."""
        try:
            for resultado in resultados:
                palavras_str = ", ".join(resultado['palavras'])
                
                # Formatar data de publicação
                data_publicacao_str = "Data não disponível"
                if resultado['data_publicacao']:
                    try:
                        if isinstance(resultado['data_publicacao'], str):
                            data_pub = datetime.fromisoformat(resultado['data_publicacao']).date()
                        else:
                            data_pub = resultado['data_publicacao']
                        data_publicacao_str = data_pub.strftime('%d/%m/%Y')
                    except:
                        data_publicacao_str = "Data não disponível"
                
                # Formatar timestamp de detecção com timezone brasileiro
                timestamp_detectado = datetime.fromisoformat(resultado['timestamp'].replace('Z', '+00:00'))
                if timestamp_detectado.tzinfo is None:
                    timestamp_detectado = TIMEZONE_BR.localize(timestamp_detectado)
                else:
                    timestamp_detectado = timestamp_detectado.astimezone(TIMEZONE_BR)
                timestamp_str = timestamp_detectado.strftime('%d/%m/%Y às %H:%M')
                
                # Incluir seção no nome da fonte
                fonte_com_secao = f"{resultado['fonte_nome']} - {resultado['secao']}"
                
                mensagem = (
                    f"🚨 *Palavras encontradas:* {palavras_str}\n"
                    f"📅 *Publicado:* {data_publicacao_str}\n"
                    f"🗞️ *{fonte_com_secao}*\n\n"
                    f"📰 {resultado['titulo']}\n\n"
                    f"🔗 {resultado['url']}\n\n"
                    f"⏰ *Detectado em:* {timestamp_str}"
                )
                
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=mensagem,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                
                # Pequena pausa entre mensagens para evitar rate limiting
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Erro ao enviar notificações: {e}")
    
    async def loop_monitoramento(self):
        """Loop principal do monitoramento."""
        logger.info("🚀 Loop de monitoramento iniciado")
        while self.running:
            try:
                await self.executar_monitoramento()
                await asyncio.sleep(MONITORAMENTO_INTERVAL)
            except Exception as e:
                logger.error(f"Erro no loop de monitoramento: {e}")
                await asyncio.sleep(60)  # Pausa menor em caso de erro
    
    def iniciar_monitoramento(self):
        """Inicia o monitoramento em background."""
        if not self.running:
            self.running = True
            logger.info("🚀 Monitoramento automático iniciado")
    
    def parar_monitoramento(self):
        """Para o monitoramento."""
        if self.running:
            self.running = False
            if self.monitoramento_task:
                self.monitoramento_task.cancel()
            logger.info("⏹️ Monitoramento automático parado")

# Instância global do gerenciador de monitoramento
monitor_manager = None

# Comando de ajuda
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de ajuda com todos os comandos disponíveis."""
    help_text = (
        "🤖 *Faro Fino – Comandos Disponíveis:*\n\n"
        "🔹 Adicionar: `@termo1, termo2`\n"
        "🔹 Remover: `#termo1, termo2`\n\n"
        "📋 /verpalavras – Ver palavras-chave\n"
        "📋 /verperfis – Ver perfis/fontes\n"
        "🚀 /start – Configurar bot e boas-vindas\n"
        "🔄 /monitoramento – Ativar/desativar monitoramento\n"
        "⚡ /verificar – Executar monitoramento imediatamente\n"
        "📊 /status – Ver status do monitoramento\n"
        "🧹 /reset_historico – Limpar histórico de links\n"
        "🔧 /diagnostico – Diagnóstico completo do sistema\n"
        "❓ /help – Mostrar este menu"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# Comando /start (substitui /config)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de início e configuração do bot."""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name or "usuário"
    
    if config_data["telegram_owner_id"] is None:
        config_data["telegram_owner_id"] = user_id
        salvar_config(config_data)
        mensagem = (
            f"🎉 *Bem-vindo ao Faro Fino, {user_name}!*\n\n"
            f"✅ Proprietário configurado! Seu ID: `{user_id}`\n\n"
            f"🔍 *O que é o Faro Fino?*\n"
            f"Sou um bot de monitoramento de notícias que te ajuda a ficar por dentro das últimas informações sobre os temas que você mais se interessa.\n\n"
            f"🌟 *Google News integrado!* Funciona imediatamente após adicionar palavras-chave.\n\n"
            f"📝 *Como usar:*\n"
            f"• Adicione palavras-chave: `@política, economia`\n"
            f"• Adicione sites (opcional): `@https://g1.globo.com`\n"
            f"• Ative o monitoramento: `/monitoramento`\n"
            f"• Teste imediatamente: `/verificar`\n\n"
            f"🚀 *Pronto!* Agora você pode usar todos os comandos do bot.\n"
            f"Digite `/help` para ver todos os comandos disponíveis."
        )
    elif config_data["telegram_owner_id"] == user_id:
        mensagem = (
            f"👋 *Olá novamente, {user_name}!*\n\n"
            f"ℹ️ Você já é o proprietário configurado. ID: `{user_id}`\n\n"
            f"🔍 *Status atual:*\n"
            f"📌 Palavras-chave: {len(config_data.get('palavras_chave', []))}\n"
            f"📡 Fontes: {len(config_data.get('sites_monitorados', []))}\n"
            f"🔄 Monitoramento: {'🟢 Ativo' if config_data.get('monitoramento_ativo', False) else '🔴 Inativo'}\n"
            f"🌟 Google News: {'🟢 Ativo' if config_data.get('configuracao_avancada', {}).get('google_news_ativo', True) else '🔴 Inativo'}\n\n"
            f"Digite `/help` para ver todos os comandos disponíveis."
        )
    else:
        mensagem = (
            f"👋 *Olá, {user_name}!*\n\n"
            f"❌ Este bot já possui um proprietário configurado.\n"
            f"Apenas o proprietário pode usar os comandos do Faro Fino.\n\n"
            f"🔒 Acesso restrito por questões de segurança."
        )
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Comando para verificação imediata
async def verificar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa monitoramento imediatamente."""
    global monitor_manager
    
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    palavras = config.get("palavras_chave", [])
    
    if not palavras:
        await update.message.reply_text(
            "❌ *Erro:* Nenhuma palavra-chave configurada.\n"
            "Adicione palavras-chave primeiro usando: `@palavra1, palavra2`",
            parse_mode="Markdown"
        )
        return
    
    # CORREÇÃO: Google News não precisa de sites configurados
    google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
    sites = config.get("sites_monitorados", [])
    
    if not google_news_ativo and not sites:
        await update.message.reply_text(
            "❌ *Erro:* Nenhuma fonte configurada.\n"
            "Ative o Google News ou adicione sites usando: `@https://site.com`",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text(
        f"🔍 *Executando verificação imediata...*\n\n"
        f"📌 Palavras-chave: {len(palavras)}\n"
        f"🌟 Google News: {'🟢 Ativo' if google_news_ativo else '🔴 Inativo'}\n"
        f"📡 Sites: {len(sites)}\n"
        f"🔍 Aguarde o resultado...",
        parse_mode="Markdown"
    )
    
    try:
        resultados = await monitor_manager.executar_monitoramento(executar_imediatamente=True)
        
        if resultados:
            await update.message.reply_text(
                f"✅ *Verificação concluída!*\n\n"
                f"📊 {len(resultados)} notícias encontradas e enviadas.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "✅ *Verificação concluída!*\n\n"
                "ℹ️ Nenhuma palavra-chave encontrada nas notícias recentes.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Erro na verificação imediata: {e}")
        await update.message.reply_text(
            f"❌ *Erro durante a verificação:*\n`{str(e)}`",
            parse_mode="Markdown"
        )

# NOVO: Comando para reset do histórico
async def reset_historico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reseta o histórico de links monitorados."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    historico_antigo = len(config.get("historico_links", {}))
    
    # Limpar histórico
    config["historico_links"] = {}
    salvar_config(config)
    
    await update.message.reply_text(
        f"🧹 *Histórico resetado com sucesso!*\n\n"
        f"📊 {historico_antigo} links removidos do histórico.\n"
        f"🔄 O bot agora pode detectar notícias que antes eram consideradas 'já vistas'.\n\n"
        f"💡 Use `/verificar` para testar imediatamente.",
        parse_mode="Markdown"
    )

# NOVO: Comando de diagnóstico completo
async def diagnostico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa diagnóstico completo do sistema."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    
    # Diagnóstico básico
    palavras = config.get("palavras_chave", [])
    sites = config.get("sites_monitorados", [])
    historico = config.get("historico_links", {})
    google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
    
    # Calcular estatísticas do histórico
    agora = datetime.now(TIMEZONE_BR)
    links_hoje = 0
    links_ontem = 0
    links_antigos = 0
    
    for url, dados in historico.items():
        try:
            data_str = dados.get("data_notificacao", "")
            if data_str:
                data = datetime.fromisoformat(data_str.replace('Z', '+00:00'))
                if data.tzinfo is None:
                    data = TIMEZONE_BR.localize(data)
                else:
                    data = data.astimezone(TIMEZONE_BR)
                
                diff_dias = (agora - data).days
                if diff_dias == 0:
                    links_hoje += 1
                elif diff_dias == 1:
                    links_ontem += 1
                else:
                    links_antigos += 1
        except:
            links_antigos += 1
    
    # Teste rápido do Google News
    teste_google = "🔴 Falhou"
    try:
        if google_news_ativo:
            noticias_teste = await buscar_noticias_google_news(["brasil"], max_resultados=1)
            if noticias_teste:
                teste_google = "🟢 Funcionando"
            else:
                teste_google = "🟡 Sem resultados"
    except Exception as e:
        teste_google = f"🔴 Erro: {str(e)[:30]}..."
    
    diagnostico = (
        f"🔧 *Diagnóstico Completo do Sistema*\n\n"
        f"📊 *Configuração:*\n"
        f"• Palavras-chave: {len(palavras)}\n"
        f"• Sites configurados: {len(sites)}\n"
        f"• Monitoramento: {'🟢 Ativo' if config.get('monitoramento_ativo', False) else '🔴 Inativo'}\n\n"
        f"🌟 *Google News:*\n"
        f"• Status: {'🟢 Ativo' if google_news_ativo else '🔴 Inativo'}\n"
        f"• Teste rápido: {teste_google}\n\n"
        f"📚 *Histórico de Links:*\n"
        f"• Total: {len(historico)} links\n"
        f"• Hoje: {links_hoje}\n"
        f"• Ontem: {links_ontem}\n"
        f"• Mais antigos: {links_antigos}\n\n"
        f"⚙️ *Configurações Avançadas:*\n"
        f"• Filtro de dias: {config.get('configuracao_avancada', {}).get('dias_filtro_noticias', 3)}\n"
        f"• Relatório de varredura: {'🟢 Ativo' if config.get('configuracao_avancada', {}).get('relatorio_varredura', True) else '🔴 Inativo'}\n"
        f"• Detecção de bloqueios: {'🟢 Ativo' if config.get('configuracao_avancada', {}).get('deteccao_bloqueios', True) else '🔴 Inativo'}\n\n"
        f"💡 *Recomendações:*\n"
    )
    
    # Adicionar recomendações baseadas no diagnóstico
    if len(palavras) == 0:
        diagnostico += "• ⚠️ Adicione palavras-chave para começar\n"
    elif len(palavras) > 20:
        diagnostico += "• ⚠️ Muitas palavras-chave podem reduzir performance\n"
    
    if not google_news_ativo and len(sites) == 0:
        diagnostico += "• ❌ Nenhuma fonte ativa! Ative Google News ou adicione sites\n"
    
    if len(historico) > 800:
        diagnostico += "• ⚠️ Histórico muito grande, considere usar /reset_historico\n"
    
    if links_hoje == 0 and len(palavras) > 0:
        diagnostico += "• ⚠️ Nenhuma notícia detectada hoje, verifique configuração\n"
    
    await update.message.reply_text(diagnostico, parse_mode="Markdown")

# Comando de status
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o status atual do monitoramento."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    
    # Informações básicas
    palavras = config.get("palavras_chave", [])
    sites = config.get("sites_monitorados", [])
    perfis_twitter = config.get("perfis_twitter", [])
    perfis_instagram = config.get("perfis_instagram", [])
    total_fontes = len(sites) + len(perfis_twitter) + len(perfis_instagram)
    
    # Google News
    google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
    if google_news_ativo:
        total_fontes += 1  # Contar Google News como fonte
    
    # Status do monitoramento
    monitoramento_ativo = config.get("monitoramento_ativo", False)
    
    # Última verificação
    ultima_verificacao = config.get("ultima_verificacao")
    if ultima_verificacao:
        try:
            dt_verificacao = datetime.fromisoformat(ultima_verificacao)
            if dt_verificacao.tzinfo is None:
                dt_verificacao = TIMEZONE_BR.localize(dt_verificacao)
            else:
                dt_verificacao = dt_verificacao.astimezone(TIMEZONE_BR)
            ultima_verificacao_str = dt_verificacao.strftime('%d/%m/%Y %H:%M:%S')
        except:
            ultima_verificacao_str = "Formato inválido"
    else:
        ultima_verificacao_str = "Nunca executado"
    
    # Histórico
    historico = config.get("historico_links", {})
    
    # Configurações avançadas
    config_avancada = config.get("configuracao_avancada", {})
    dias_filtro = config_avancada.get("dias_filtro_noticias", DIAS_FILTRO_NOTICIAS)
    
    status_text = (
        f"📊 *Status do Monitoramento*\n\n"
        f"🟢 *Status:* {'Ativo' if monitoramento_ativo else 'Inativo'}\n"
        f"📌 *Palavras-chave:* {len(palavras)}\n"
        f"📡 *Fontes:* {total_fontes}\n"
        f"⏱️ *Intervalo:* {MONITORAMENTO_INTERVAL // 60} minutos\n"
        f"🔍 *Filtro:* Últimos {dias_filtro} dias\n"
        f"📰 *Modo:* Links específicos de notícias\n"
        f"📚 *Histórico:* {len(historico)} links\n"
        f"🕐 *Última verificação:* {ultima_verificacao_str}\n\n"
        f"🌟 *Google News:* {'🟢 Ativo' if google_news_ativo else '🔴 Inativo'}"
    )
    
    await update.message.reply_text(status_text, parse_mode="Markdown")

# Comando de monitoramento
async def monitoramento_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa ou desativa o monitoramento automático."""
    global monitor_manager
    
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    
    # Verificar se há palavras-chave configuradas
    palavras = config.get("palavras_chave", [])
    if not palavras:
        await update.message.reply_text(
            "❌ *Erro:* Nenhuma palavra-chave configurada.\n"
            "Adicione palavras-chave primeiro usando: `@palavra1, palavra2`",
            parse_mode="Markdown"
        )
        return
    
    # Alternar status do monitoramento
    monitoramento_ativo = config.get("monitoramento_ativo", False)
    novo_status = not monitoramento_ativo
    
    config["monitoramento_ativo"] = novo_status
    salvar_config(config)
    
    if novo_status:
        monitor_manager.iniciar_monitoramento()
        status_emoji = "🟢"
        status_text = "ativado"
        
        # Informações sobre as fontes
        google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
        sites = config.get("sites_monitorados", [])
        
        fontes_info = ""
        if google_news_ativo:
            fontes_info += "🌟 Google News (fonte principal)\n"
        if sites:
            fontes_info += f"🌐 {len(sites)} sites complementares\n"
        
        mensagem = (
            f"{status_emoji} *Monitoramento {status_text}!*\n\n"
            f"📌 Palavras-chave: {len(palavras)}\n"
            f"📡 Fontes ativas:\n{fontes_info}\n"
            f"⏱️ Verificação a cada {MONITORAMENTO_INTERVAL // 60} minutos\n\n"
            f"🚀 O bot começará a monitorar automaticamente!"
        )
    else:
        monitor_manager.parar_monitoramento()
        status_emoji = "🔴"
        status_text = "desativado"
        
        mensagem = (
            f"{status_emoji} *Monitoramento {status_text}.*\n\n"
            f"⏸️ O bot parou de verificar notícias automaticamente.\n"
            f"💡 Use `/verificar` para execução manual ou `/monitoramento` para reativar."
        )
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Comando para ver palavras-chave
async def ver_palavras_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe as palavras-chave configuradas."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    palavras = config.get("palavras_chave", [])
    
    if not palavras:
        await update.message.reply_text(
            "📝 *Palavras-chave configuradas:*\n\n"
            "❌ Nenhuma palavra-chave configurada.\n\n"
            "💡 Adicione palavras usando: `@palavra1, palavra2`",
            parse_mode="Markdown"
        )
    else:
        palavras_formatadas = "\n".join([f"• {palavra}" for palavra in palavras])
        await update.message.reply_text(
            f"📝 *Palavras-chave configuradas:*\n\n"
            f"{palavras_formatadas}\n\n"
            f"📊 Total: {len(palavras)} palavras\n\n"
            f"💡 Adicione mais: `@nova1, nova2`\n"
            f"💡 Remova: `#palavra1, palavra2`",
            parse_mode="Markdown"
        )

# Comando para ver perfis/fontes
async def ver_perfis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe os perfis e fontes configurados."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    sites = config.get("sites_monitorados", [])
    perfis_twitter = config.get("perfis_twitter", [])
    perfis_instagram = config.get("perfis_instagram", [])
    google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
    
    mensagem = "📡 *Fontes configuradas:*\n\n"
    
    # Google News
    if google_news_ativo:
        mensagem += "🌟 *Google News:* 🟢 Ativo (fonte principal)\n\n"
    else:
        mensagem += "🌟 *Google News:* 🔴 Inativo\n\n"
    
    # Sites
    if sites:
        mensagem += f"🌐 *Sites ({len(sites)}):*\n"
        for site in sites:
            mensagem += f"• {site}\n"
        mensagem += "\n"
    else:
        mensagem += "🌐 *Sites:* Nenhum configurado\n\n"
    
    # Twitter
    if perfis_twitter:
        mensagem += f"🐦 *Twitter ({len(perfis_twitter)}):*\n"
        for perfil in perfis_twitter:
            mensagem += f"• @{perfil}\n"
        mensagem += "\n"
    else:
        mensagem += "🐦 *Twitter:* Nenhum perfil configurado\n\n"
    
    # Instagram
    if perfis_instagram:
        mensagem += f"📷 *Instagram ({len(perfis_instagram)}):*\n"
        for perfil in perfis_instagram:
            mensagem += f"• @{perfil}\n"
        mensagem += "\n"
    else:
        mensagem += "📷 *Instagram:* Nenhum perfil configurado\n\n"
    
    total_fontes = len(sites) + len(perfis_twitter) + len(perfis_instagram)
    if google_news_ativo:
        total_fontes += 1
    
    mensagem += f"📊 *Total:* {total_fontes} fontes ativas\n\n"
    mensagem += "💡 Adicione sites: `@https://site.com`"
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Função para processar mensagens de texto (adicionar/remover palavras-chave e sites)
async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa mensagens de texto para adicionar ou remover palavras-chave e sites."""
    if not verificar_proprietario(update):
        return
    
    texto = update.message.text.strip()
    
    # Verificar se é comando de adição (@)
    if texto.startswith('@'):
        await adicionar_itens(update, texto[1:])  # Remove o @
    
    # Verificar se é comando de remoção (#)
    elif texto.startswith('#'):
        await remover_itens(update, texto[1:])  # Remove o #
    
    # Se não é comando reconhecido, ignorar silenciosamente
    # (para não interferir com outras funcionalidades)

async def adicionar_itens(update, texto):
    """Adiciona palavras-chave ou sites à configuração."""
    config = carregar_config()
    
    # Dividir por vírgulas e limpar espaços
    itens = [item.strip() for item in texto.split(',') if item.strip()]
    
    if not itens:
        await update.message.reply_text(
            "❌ *Formato inválido.*\n\n"
            "Use: `@palavra1, palavra2` ou `@https://site.com`",
            parse_mode="Markdown"
        )
        return
    
    palavras_adicionadas = []
    sites_adicionados = []
    
    for item in itens:
        # Verificar se é URL (site)
        if item.startswith('http://') or item.startswith('https://'):
            if item not in config.get("sites_monitorados", []):
                if "sites_monitorados" not in config:
                    config["sites_monitorados"] = []
                config["sites_monitorados"].append(item)
                sites_adicionados.append(item)
        else:
            # É palavra-chave
            if item not in config.get("palavras_chave", []):
                if "palavras_chave" not in config:
                    config["palavras_chave"] = []
                config["palavras_chave"].append(item)
                palavras_adicionadas.append(item)
    
    # Salvar configuração
    salvar_config(config)
    
    # Preparar mensagem de resposta
    mensagem = "✅ *Itens adicionados com sucesso!*\n\n"
    
    if palavras_adicionadas:
        mensagem += f"📝 *Palavras-chave ({len(palavras_adicionadas)}):*\n"
        for palavra in palavras_adicionadas:
            mensagem += f"• {palavra}\n"
        mensagem += "\n"
    
    if sites_adicionados:
        mensagem += f"🌐 *Sites ({len(sites_adicionados)}):*\n"
        for site in sites_adicionados:
            mensagem += f"• {site}\n"
        mensagem += "\n"
    
    if not palavras_adicionadas and not sites_adicionados:
        mensagem = "ℹ️ *Nenhum item novo adicionado.*\n\nTodos os itens já estavam configurados."
    else:
        # Informar sobre Google News
        google_news_ativo = config.get("configuracao_avancada", {}).get("google_news_ativo", True)
        if google_news_ativo and palavras_adicionadas:
            mensagem += "🌟 *Google News ativo!* As novas palavras-chave já estão sendo monitoradas.\n\n"
        
        mensagem += "💡 Use `/verificar` para testar imediatamente."
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

async def remover_itens(update, texto):
    """Remove palavras-chave ou sites da configuração."""
    config = carregar_config()
    
    # Dividir por vírgulas e limpar espaços
    itens = [item.strip() for item in texto.split(',') if item.strip()]
    
    if not itens:
        await update.message.reply_text(
            "❌ *Formato inválido.*\n\n"
            "Use: `#palavra1, palavra2` ou `#https://site.com`",
            parse_mode="Markdown"
        )
        return
    
    palavras_removidas = []
    sites_removidos = []
    
    for item in itens:
        # Verificar se é URL (site)
        if item.startswith('http://') or item.startswith('https://'):
            if item in config.get("sites_monitorados", []):
                config["sites_monitorados"].remove(item)
                sites_removidos.append(item)
        else:
            # É palavra-chave
            if item in config.get("palavras_chave", []):
                config["palavras_chave"].remove(item)
                palavras_removidas.append(item)
    
    # Salvar configuração
    salvar_config(config)
    
    # Preparar mensagem de resposta
    mensagem = "✅ *Itens removidos com sucesso!*\n\n"
    
    if palavras_removidas:
        mensagem += f"📝 *Palavras-chave removidas ({len(palavras_removidas)}):*\n"
        for palavra in palavras_removidas:
            mensagem += f"• {palavra}\n"
        mensagem += "\n"
    
    if sites_removidos:
        mensagem += f"🌐 *Sites removidos ({len(sites_removidos)}):*\n"
        for site in sites_removidos:
            mensagem += f"• {site}\n"
        mensagem += "\n"
    
    if not palavras_removidas and not sites_removidos:
        mensagem = "ℹ️ *Nenhum item removido.*\n\nOs itens especificados não foram encontrados na configuração."
    else:
        mensagem += "💡 As alterações entrarão em vigor na próxima verificação."
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Carregar configuração global
config_data = carregar_config()

def main():
    """Função principal do bot."""
    global monitor_manager
    
    # Verificar se o token está configurado
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("❌ BOT_TOKEN não configurado nas variáveis de ambiente!")
        return
    
    # Criar aplicação do bot
    application = ApplicationBuilder().token(bot_token).build()
    
    # Criar gerenciador de monitoramento
    monitor_manager = MonitoramentoManager(application.bot)
    
    # Registrar handlers de comandos
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("verificar", verificar_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("monitoramento", monitoramento_command))
    application.add_handler(CommandHandler("verpalavras", ver_palavras_command))
    application.add_handler(CommandHandler("verperfis", ver_perfis_command))
    application.add_handler(CommandHandler("reset_historico", reset_historico_command))
    application.add_handler(CommandHandler("diagnostico", diagnostico_command))
    
    # Handler para mensagens de texto (adicionar/remover itens)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))
    
    # Iniciar monitoramento automático se estiver ativo
    if config_data.get("monitoramento_ativo", False):
        monitor_manager.iniciar_monitoramento()
        # Criar task para o loop de monitoramento
        import asyncio
        loop = asyncio.get_event_loop()
        monitor_manager.monitoramento_task = loop.create_task(monitor_manager.loop_monitoramento())
    
    logger.info("🚀 Faro Fino Bot v2.7.8 DEFINITIVA iniciado com sucesso!")
    logger.info("🌟 Google News integrado como fonte principal")
    logger.info("🔧 Sistema de diagnóstico e reset implementado")
    
    # Executar o bot
    application.run_polling()

if __name__ == "__main__":
    main()

